"""只读资金预算与收盘证据检查，不改变策略持仓或提交订单。"""

from trading_system.market import Calendar, InvalidInput, number, stamp


def cash_review(allocation, phase, price_decision, config, conflict, mismatch):
    if config.get("cash_allocation_policy") != "EXCESS_CASH_TO_QQQ_IN_NORMAL_STATE":
        return {"status": "DISABLED"}
    if not allocation.get("confirmed"):
        return {"status": "WAIT", "reason": "ALLOCATION_UNVERIFIED"}
    if mismatch or phase not in ("MIXED", "QQQ_ONLY") or price_decision["reason"] != "HOLD":
        return {"status": "DEFER", "reason": "STRATEGY_ACTION_HAS_PRIORITY"}
    if conflict:
        return {"status": "WAIT", "reason": "ORDER_OR_INSTRUCTION_CONFLICT"}
    if allocation.get("settled_cash_usd") is None:
        return {"status": "WAIT", "reason": "SETTLED_CASH_UNVERIFIED"}
    reserve = max(number("20"), number(allocation["total_usd"]) * number(".02"))
    budget = max(number("0"), min(number(allocation["values_usd"]["USD"]),
                                  number(allocation["settled_cash_usd"])) - reserve)
    return {"status": "REVIEW" if budget >= 1 else "WAIT", "reason": "ADVISORY_ONLY" if budget >= 1 else "NO_SETTLED_SURPLUS",
            "asset": "QQQ", "reserve_usd": str(reserve), "budget_before_fees_usd": str(budget),
            "preserve_tqqq": True, "executable": False}


def close_review(trades, signal_day, now, request):
    """成交检查不等于完整收盘对账；连接器没有现金/资产流水。"""
    _, close = Calendar().session(signal_day)
    reasons = ["CASH_FLOW_LEDGER_UNAVAILABLE", "CLOSE_POSITIONS_NOT_INDEPENDENTLY_VERIFIED"]
    if not request:
        reasons.append("TRADE_REQUEST_COVERAGE_UNVERIFIED")
    count = 0
    for trade in trades:
        try:
            at = stamp(trade["trade_time"])
            if at > stamp(now):
                raise InvalidInput("FUTURE_TRADE")
            if at > close:
                count += 1
        except (InvalidInput, KeyError, TypeError, ValueError):
            if "TRADE_TIME_UNVERIFIED" not in reasons:
                reasons.append("TRADE_TIME_UNVERIFIED")
    if count:
        reasons.append("POST_CLOSE_TRADES_PRESENT")
    return {"verified": False, "signal_day": signal_day, "close_at": close.isoformat(),
            "post_close_trade_count": count, "trade_request": request, "reasons": reasons}
