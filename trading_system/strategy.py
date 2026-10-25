"""研究与执行共用的纯信号核心；所有窗口包含信号日。"""

from .market import InvalidInput, number, positive

VERSION = "sma200-rolling20-guard-sgov-v1"
SYMBOLS = ("QQQ", "TQQQ", "SGOV")
PHASES = ("QQQ_ONLY", "MIXED", "BOOST", "DEFENSIVE")
TARGETS = {
    "QQQ_ONLY": ("1", "0", "0"),
    "MIXED": (".4", ".6", "0"),
    "BOOST": ("0", "1", "0"),
    "DEFENSIVE": ("0", "0", "1"),
}


def indicators(closes):
    if len(closes) < 200:
        raise InvalidInput("INSUFFICIENT_HISTORY")
    values = [positive(v) for v in closes]
    return dict(
        close=str(values[-1]),
        sma200=str(sum(values[-200:]) / 200),
        peak20=str(max(values[-20:])),
    )


def decide(phase, close, sma200, peak20, tqqq_weight="0", recovery_eligible=False):
    if phase not in PHASES:
        raise InvalidInput("UNINITIALIZED_PHASE")
    close, sma, peak = map(positive, (close, sma200, peak20))
    weight = number(tqqq_weight)
    if peak < close or not 0 <= weight <= 1:
        raise InvalidInput("INVALID_INDICATOR_OR_WEIGHT")
    target, reason = phase, "HOLD"
    if close <= sma * number(".99"):
        if phase != "DEFENSIVE":
            target, reason = "DEFENSIVE", "MA_DEFENSE"
    elif phase in ("MIXED", "BOOST") and close <= peak * number(".92"):
        target, reason = "QQQ_ONLY", "ROLLING20_EXIT"
    elif (
        phase in ("QQQ_ONLY", "DEFENSIVE")
        and close > sma * number("1.01")
        and close > peak * number(".92")
    ):
        if phase == "DEFENSIVE":
            if not recovery_eligible:
                raise InvalidInput("RECOVERY_ELIGIBILITY_UNCONFIRMED")
            target, reason = "BOOST", "MA_RECOVERY"
        else:
            target, reason = "MIXED", "MIXED_ENTRY"
    elif phase == "MIXED" and weight > number(".7"):
        reason = "DRIFT_CAP"
    return dict(
        phase=target,
        reason=reason,
        weights=dict(zip(SYMBOLS, TARGETS[target])) if reason != "HOLD" else None,
    )
