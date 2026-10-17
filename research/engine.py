import json
import os
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).parent
raw=json.loads(Path(os.environ.get('STRATEGY_DATA', str(ROOT.parent/'data'/'market.json'))).read_text())
frames={}
for s,d in raw.items():
    ix=pd.to_datetime(d['time'],utc=True).tz_convert(None).normalize()
    f=pd.DataFrame({k:d[k] for k in ['open','close','high']},index=ix)
    assert f.index.is_unique and (f>0).all().all()
    div=pd.Series(0.,index=ix)
    splits=[(pd.Timestamp(a['date']),float(a['value'])) for a in d['corp_actions'] if a['type']=='Splits']
    for a in d['corp_actions']:
        if a['type']=='CashDividends':
            dt=pd.Timestamp(a['date'])
            if dt in ix:
                div.loc[dt]+=float(a['value'])/np.prod([v for t,v in splits if t>dt])
    # IBKR history is split adjusted. Build forward total-return sleeve prices.
    # At the open only the prior close's scale is known; ex-day dividend enters
    # the closing sleeve value and is reinvested at the close, without fees.
    tr=(f.close.add(div).div(f.close.shift())).fillna(1.).cumprod()
    f['tc']=tr
    f['to']=f.open*(tr.shift()/f.close.shift()).fillna(1/f.close.iloc[0])
    frames[s]=f
idx=frames['QQQ'].index.intersection(frames['TQQQ'].index)
C=pd.DataFrame({s:frames[s].tc.reindex(idx) for s in frames})
O=pd.DataFrame({s:frames[s].to.reindex(idx) for s in frames})
signal=frames['QQQ'].close.reindex(idx)
high252=signal.rolling(252).max()
levels=[(.30,.70),(.25,.65),(.20,.60),(.15,.40),(.10,.30),(.05,.10)]
def weight(dd):
    return next((w for d,w in levels if dd>=d-1e-12),0.)

def run(mode,start='2023-01-03',cost=.001,reference_reset=False,lookback=252,exclude_signal_day=False,reference_field='close',weight_model=None,reentry_mode='peak'):
    first=np.flatnonzero(idx>=pd.Timestamp(start))[0]
    assert first>=max(lookback+int(exclude_signal_day),175)
    reference_prices=frames['QQQ'][reference_field].reindex(idx)
    rolling_high=reference_prices.rolling(lookback).max().shift(int(exclude_signal_day))
    q=np.zeros(2);cash=1.;state='wait';H=None;peak=None;unlock=None;tier=0.;deepest_base=0.
    eq=[];dates=[];events=[];weights=[];fees=0.;lockdays=0
    def target(w,i,why):
        nonlocal q,cash,fees
        p=O.iloc[i].values;before=q*p;v=cash+before.sum();wvec=np.array([1-w,w]);post=v
        for _ in range(30):post=v-cost*np.abs(wvec*post-before).sum()
        fee=v-post;fees+=fee
        q=wvec*post/p;cash=0.
        events.append(dict(date=str(idx[i].date()),reason=why,w=w,fee=fee))
    sma=signal.rolling(50 if mode=='sma50' else 175).mean()
    for i in range(first,len(idx)):
        if i==first:
            target(1. if mode=='TQQQ' else .3 if mode=='fixed30' else 0.,i,'initial')
        elif mode not in ['QQQ','TQQQ','fixed30']:
            j=i-1;p=float(signal.iloc[j]);rh=float(rolling_high.iloc[j])
            if reference_reset:
                end=j+1-int(exclude_signal_day)
                rh=float(reference_prices.iloc[max(first,end-lookback):end].max()) if end>first else p
            if state=='locked':
                lockdays+=1
                if reentry_mode=='next_signal' or p>=unlock:
                    state='wait';H=None;peak=None;tier=0.
            elif state=='wait':
                w=weight(1-p/rh)
                if weight_model is not None:w=weight_model(1-p/rh,j)
                gate=(mode not in ['sma50','sma175'] or p>=sma.iloc[j]*(.98 if mode=='sma175' else 1.))
                if w>0 and gate:
                    H=rh;peak=p;tier=w;deepest_base=weight(1-p/rh);state='active';target(w,i,'entry')
            elif state=='half':
                peak=max(peak,p)
                threshold=.05 if mode=='half5' else .08
                if p<=peak*(1-threshold):
                    target(0.,i,'trailing_exit');state='locked';unlock=peak
            elif state=='active':
                peak=max(peak,p)
                dd=1-p/H;w=weight(dd);exit_now=False
                if weight_model is not None:
                    w=weight_model(dd,j) if weight(dd)>deepest_base+1e-10 else tier
                if mode in ['half8','half5','priorall'] and p>=H:
                    if mode=='priorall':exit_now=True
                    else:
                        # Sell half of the actual TQQQ units at the next open;
                        # purchase QQQ with net proceeds, keeping the other half.
                        sell=q[1]/2;proceeds=sell*O.TQQQ.iloc[i]
                        sellfee=cost*proceeds;buy=(proceeds-sellfee)/(1+cost)
                        fee=sellfee+cost*buy;fees+=fee
                        q[1]-=sell;q[0]+=buy/O.QQQ.iloc[i]
                        events.append(dict(date=str(idx[i].date()),reason='half',w=float(q[1]*O.TQQQ.iloc[i]/(q@O.iloc[i].values)),fee=fee))
                        state='half';peak=p
                elif mode in ['trail5','trail8']:
                    exit_now=p<=peak*(.95 if mode=='trail5' else .92)
                elif mode in ['sma50','sma175']:
                    exit_now=p<sma.iloc[j]*(.98 if mode=='sma175' else 1.)
                elif mode=='symmetric':
                    if w==0:exit_now=True
                    elif abs(w-tier)>1e-10:target(w,i,'symmetric');tier=w
                if exit_now:
                    target(0.,i,'exit');state='locked';unlock=max(H,peak)
                elif state=='active' and mode!='symmetric' and w>tier+1e-10:
                    target(w,i,'deeper_entry');tier=w;deepest_base=weight(dd)
        v=float(cash+q@C.iloc[i].values)
        assert v>0 and (q>=-1e-12).all() and cash>=-1e-12
        eq.append(v);dates.append(idx[i]);weights.append(float(q[1]*C.TQQQ.iloc[i]/v))
    e=pd.Series(eq,index=dates);r=e.pct_change();r.iloc[0]=e.iloc[0]-1
    path=np.r_[1.,e.values];dd=path/np.maximum.accumulate(path)-1
    years=(idx[-1]-idx[first]).days/365.25
    out=dict(mode=mode,start=str(idx[first].date()),end=str(idx[-1].date()),days=len(e),CAGR=(e.iloc[-1]**(1/years)-1)*100,MDD=dd.min()*100,Sharpe_rf0=r.mean()/r.std(ddof=1)*np.sqrt(252),vol=r.std(ddof=1)*np.sqrt(252)*100,final100k=e.iloc[-1]*100000,rebalances=len(events)-1,meanTQQQ=np.mean(weights)*100,maxTQQQ=max(weights)*100,lockdays=lockdays)
    prev=1.
    for y in sorted(set(e.index.year)):
        last=e[e.index.year==y].iloc[-1];out[str(y)]=(last/prev-1)*100;prev=last
    return out,e,events

