import math
from datetime import date

TRADE_START = date(2020, 1, 3)
TRADE_END = date(2026, 10, 16)
INVESTED = 0.995
TRAIL = 0.08
NEAR_HIGH = 0.995
REENTRY_GAP = 0.05
LEVELS = ((0.30,0.70),(0.25,0.65),(0.20,0.60),(0.15,0.40),(0.10,0.30),(0.05,0.10))

def initialize(context):
    context.qqq = symbol('QQQ')
    context.tqqq = symbol('TQQQ')
    set_benchmark(context.qqq)
    set_max_leverage(1.0)
    set_slippage(0.0)
    context.w = 0.0
    context.high = None
    context.entry_high = None
    context.peak = None
    context.half_weight = None
    context.lock_price = None
    context.lock_high = None
    context.prev_signal = None
    context.last_day = None
    context.first_day = None
    context.days = 0
    context.pending = []
    log.info('NATIVE V2 | cash-funded limit buys | sells settle before buys')
    log.info('Native next-bar fills; limit=signal-day open*1.01; reserve at least USD20')

def entry_weight(dd):
    for level,weight in LEVELS:
        if dd+1e-10 >= level:
            return weight
    return 0.0

def scale_state(context,factor):
    if context.high is not None:
        context.high *= factor
    if context.entry_high is not None:
        context.entry_high *= factor
    if context.peak is not None:
        context.peak *= factor
    if context.lock_price is not None:
        context.lock_price *= factor
        context.lock_high *= factor

def signal(context,prices):
    price=prices[-1]
    reference=max(prices)
    if context.w==0.0:
        if context.lock_price is not None:
            if price >= NEAR_HIGH*context.lock_high:
                context.lock_price=None
                context.lock_high=None
            elif price <= context.lock_price-REENTRY_GAP*context.lock_high+1e-10:
                reference=max(reference,context.lock_high)
                context.lock_price=None
                context.lock_high=None
            else:
                return 0.0,'LOCKED'
        weight=entry_weight(max(0.0,1.0-price/reference))
        if weight==0.0:
            return 0.0,'WAIT'
        context.w=weight
        context.high=reference
        context.entry_high=reference
        context.peak=price
        context.half_weight=None
        return weight,'ENTRY'
    context.high=max(context.high,price)
    context.peak=max(context.peak,price)
    dd=max(0.0,1.0-price/context.high)
    candidate=max(context.w,entry_weight(dd))
    if price <= context.peak*(1.0-TRAIL):
        context.lock_price=price
        context.lock_high=context.high
        context.w=0.0
        context.half_weight=None
        return 0.0,'TRAIL_EXIT'
    reason='HOLD_OR_ADD'
    if context.half_weight is None:
        if price >= NEAR_HIGH*context.entry_high:
            context.half_weight=candidate*0.5
            reason='PRIOR_HIGH_HALF'
    if context.half_weight is not None:
        candidate=context.half_weight
    context.w=min(0.70,max(0.0,candidate))
    return context.w,reason

def check_orders(context):
    waiting=[]
    for req_id in context.pending:
        ticket=get_order(req_id)
        if ticket is None:
            raise ValueError('Missing order: '+str(req_id))
        if ticket.status==6:
            if context.days<=5:
                log.info('FILL|%s|%s|%s|%s' % (req_id,ticket.dt,ticket.filled,ticket.filled_price))
        elif ticket.status==4:
            log.info('CANCELLED|'+str(req_id))
        elif ticket.status==8:
            message='REJECTED|%s|%s' % (req_id,ticket.reason)
            log.info(message)
            raise ValueError(message)
        else:
            cancel_order(req_id)
            waiting.append(req_id)
            log.info('CANCEL_REQUEST|%s|status=%s' % (req_id,ticket.status))
    context.pending=waiting

def held_amount(context,code):
    holding=context.portfolio.positions.get(code)
    if holding is None:
        return 0
    return int(holding.amount)

def rebalance(context,q_open,t_open,weight):
    if len(context.pending)>0:
        log.info('WAIT_FOR_ORDER_CONFIRMATION')
        return 0
    q_amount=held_amount(context,'QQQ')
    t_amount=held_amount(context,'TQQQ')
    if min(q_amount,t_amount)<0:
        raise ValueError('Unexpected short holding')
    cash=float(context.portfolio.cash)
    equity=cash+q_amount*q_open+t_amount*t_open
    if equity<=0:
        raise ValueError('Invalid account equity')
    if cash < -0.01:
        raise ValueError('Negative cash; review fills and fees')
    targets=[int(equity*INVESTED*(1-weight)/q_open),int(equity*INVESTED*weight/t_open)]
    changes=[targets[0]-q_amount,targets[1]-t_amount]
    assets=[context.qqq,context.tqqq]
    selling=False
    for i in range(2):
        if changes[i]<0:
            req_id=order(assets[i],changes[i])
            if req_id is None:
                raise ValueError('Sell order creation failed')
            context.pending.append(req_id)
            selling=True
    if selling:
        return 0
    available=max(0.0,cash-max(20.0,equity*(1-INVESTED)))
    caps=[q_open*1.01,t_open*1.01]
    needed=[max(0,changes[0]),max(0,changes[1])]
    required=needed[0]*caps[0]+needed[1]*caps[1]
    if required<=0:
        return 0
    scale=min(1.0,available/required)
    for i in range(2):
        quantity=int(needed[i]*scale)
        quantity=min(quantity,int(available/caps[i]))
        if quantity>0:
            req_id=order(assets[i],quantity,style='limit',limit_price=caps[i])
            if req_id is None:
                raise ValueError('Buy order creation failed')
            context.pending.append(req_id)
            available-=quantity*caps[i]

def handle_data(context,data):
    day=context.get_datetime('US/Eastern').date()
    if day==context.last_day:
        return 0
    check_orders(context)
    if day<TRADE_START:
        return 0
    if day>TRADE_END:
        return 0
    q_history=data.history(context.qqq,'close',252,'1d')
    t_history=data.history(context.tqqq,'close',1,'1d')
    if len(q_history)!=252:
        raise ValueError('QQQ needs 252 completed daily bars')
    if len(t_history)!=1:
        raise ValueError('Missing TQQQ prior daily bar')
    q_day=q_history.index[-1].date()
    t_day=t_history.index[-1].date()
    if q_day!=t_day:
        raise ValueError('QQQ and TQQQ calendars differ')
    if q_day>=day:
        raise ValueError('History must precede the trading day')
    if context.last_day is not None:
        if q_day!=context.last_day:
            raise ValueError('Missing trading-day callback')
    if context.first_day is None:
        if day!=TRADE_START:
            raise ValueError('Wrong first trading day: '+str(day))
        context.first_day=day
    prices=[float(x) for x in q_history]
    t_prior=float(t_history.iloc[-1])
    q_open=float(data.current(context.qqq,'open'))
    t_open=float(data.current(context.tqqq,'open'))
    values=prices+[t_prior,q_open,t_open]
    if any(not math.isfinite(x) or x<=0 for x in values):
        raise ValueError('Invalid market data on '+str(day))
    q_gap=q_open/prices[-1]-1.0
    t_gap=t_open/t_prior-1.0
    if abs(t_gap-3.0*q_gap)>0.25:
        raise ValueError('Review price adjustment on '+str(day))
    if context.prev_signal is not None:
        factor=prices[-2]/context.prev_signal
        scale_state(context,factor)
    old_weight=context.w
    weight,reason=signal(context,prices)
    if day<TRADE_END:
        rebalance(context,q_open,t_open,weight)
    context.prev_signal=prices[-1]
    context.last_day=day
    context.days+=1
    if abs(weight-old_weight)>1e-10:
        log.info('SIGNAL|%s|%s|QQQ=%.4f|TQQQ_TARGET=%.2f%%' % (day,reason,prices[-1],weight*INVESTED*100))
    if context.days==1:
        log.info('ACTUAL_START|'+str(day))
    if day==TRADE_END:
        log.info('SIGNALS_COMPLETE|%s..%s|days=%d|pending_order_ids=%s' % (context.first_day,day,context.days,str(context.pending)))
        log.info('Check final fills in platform trade details; no forced liquidation.')
