"""只渲染已计算报告，不改变交易信号、仓位或资金口径。"""

from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from trading_system.market import InvalidInput, number, positive, stamp

ASSETS = ("QQQ", "TQQQ", "SGOV", "USD")
SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def display(value, places=2):
    return format(number(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")


def allocation_text(report):
    """只有已核实且完整的四资产权重才能展示策略总资产比例。"""
    allocation = report.get("allocation") or {}
    weights = allocation.get("weights") if isinstance(allocation, dict) else None
    try:
        if allocation.get("confirmed") is not True or not isinstance(weights, dict) or set(weights) not in (set(ASSETS), set(ASSETS) | {"OTHER"}):
            raise InvalidInput("ALLOCATION_UNCONFIRMED")
        values = {s: number(v) for s, v in weights.items()}
        if any(v < 0 or v > 1 for v in values.values()) or abs(sum(values.values()) - 1) > Decimal("1e-24"):
            raise InvalidInput("INVALID_ALLOCATION")
    except (InvalidInput, AttributeError):
        positions = report.get("positions", {})
        labels = [f"{s} " + ("0%" if s in positions and number(positions[s]) == 0 else "待确认") for s in ASSETS[:-1]]
        note = "资金范围已确认，配置估值待核对。" if isinstance(allocation, dict) and allocation.get("scope_confirmed") else "策略总资产含现金，资金范围待确认。"
        return "【当前配置】\n" + "｜".join(labels[:2]) + "\n" + "｜".join(labels[2:] + ["美元现金 待确认"]) + "\n" + note
    labels = {}
    for symbol in values:
        percentage = display(values[symbol] * 100, 1).rstrip("0").rstrip(".")
        label = "美元现金" if symbol == "USD" else "其他策略资产" if symbol == "OTHER" else symbol
        labels[symbol] = f"{label} {percentage}%"
    lines = ["【当前配置】", labels["QQQ"] + "｜" + labels["TQQQ"], labels["SGOV"] + "｜" + labels["USD"]]
    if "OTHER" in labels:
        lines.append(labels["OTHER"])
    if allocation.get("total_usd") is not None:
        total = Decimal(display(positive(allocation["total_usd"])))
        lines.append(f"策略资产估值：约{total:,.2f}美元")
    lines.append("已排除IREN，包含全部账户现金。")
    return "\n".join(lines)


def sale_lines(report):
    phase, thresholds = report["phase"], report["thresholds"]
    close = positive(report["indicators"]["close"])
    if phase == "DEFENSIVE":
        return ["当前防守阶段，无新增风险资产卖出参考线。"]
    levels = [("均线防守线", positive(thresholds["ma_defense"]), "清掉QQQ和TQQQ，转入SGOV。", 0)]
    if phase in ("MIXED", "BOOST"):
        levels.append(("20日8%退出线", positive(thresholds["rolling_exit"]), "清掉TQQQ，转回QQQ。", 1))
    # 已触发均线时优先展示；未触发的参考线按距离由近到远展示。
    levels.sort(key=lambda x: (0, x[3]) if close <= x[1] else (1, abs(close - x[1])))
    result = []
    for index, (label, price, action, _) in enumerate(levels):
        distance = "收盘条件已触发" if close <= price else f"距信号收盘下跌约{display((close - price) / close * 100)}%"
        result.append(f"{'①' if index == 0 else '②'} {label}：约{display(price)}美元\n{distance}\n触发后：{action}")
    if len(levels) > 1:
        result.append("两种退出同时成立，均线防守优先。")
    return result


def render(report):
    decision = report["decision"]
    phase = report["phase"]
    reason = decision["reason"] if decision else None
    drift_pending = "DRIFT_REQUIRES_CONFIRMED_CLOSE_NAV" in report.get("blockers", [])
    if decision is None and drift_pending and "PHASE_POSITION_MISMATCH" not in report.get("blockers", []):
        reason = report.get("price_decision", {}).get("reason")
    cash = report.get("cash_review", {})
    actions = {
        "MA_DEFENSE": "清QQQ/TQQQ，转入SGOV（均线优先）",
        "ROLLING20_EXIT": "清TQQQ，转回QQQ",
        "MIXED_ENTRY": "普通入场：60%TQQQ＋40%QQQ",
        "MA_RECOVERY": "首次恢复：可投资资产100%TQQQ",
        "DRIFT_CAP": "TQQQ调回60%目标，非减半",
    }
    if reason == "HOLD":
        action = "持有SGOV，等待恢复" if phase == "DEFENSIVE" else "继续持有QQQ，等待入场" if phase == "QQQ_ONLY" else "继续持有，无新增买入"
    else:
        action = actions.get(reason, "先核对账户与状态，暂不确定动作")
    if cash.get("status") == "REVIEW":
        action = "闲置现金补QQQ预算；保留现有TQQQ"
    elif drift_pending and reason == "HOLD":
        action = "价格条件继续持有；70%漂移待核对"
    day = stamp(report["computed_at"]).astimezone(ZoneInfo("Asia/Singapore"))
    lines = [f"QQQ/TQQQ 策略日报｜{day.month}月{day.day}日", "", f"【今日动作】{action}",
             f"【信号依据】{report['signal_day']}收盘｜QQQ {display(report['indicators']['close'])}美元"]
    if cash.get("status") == "REVIEW":
        budget = Decimal(display(cash["budget_before_fees_usd"]))
        reserve = Decimal(display(cash["reserve_usd"]))
        lines.append(f"【现金预算】QQQ约{budget:,.2f}美元（未扣佣金）；预留约{reserve:,.2f}美元。")
    lines.extend(["", allocation_text(report), "", SEPARATOR, "【优先关注：卖出参考】", ""])
    lines.extend(sale_lines(report))
    buy = max(positive(report["thresholds"]["ma_entry"]), positive(report["thresholds"]["rolling_exit"]))
    lines.extend(["", SEPARATOR, "【买回观察】", ""])
    if phase in ("MIXED", "BOOST"):
        lines.append("当前不因满足入场条件重复加仓。")
    lines.append(f"QQQ后续新收盘须严格高于约{display(buy)}美元，届时按最新均线及20日最高收盘重算。")
    if phase in ("MIXED", "BOOST"):
        lines.extend(["普通重新入场：60%TQQQ＋40%QQQ。", "均线防守后首次恢复：可投资资产100%TQQQ。"])
    elif phase == "DEFENSIVE":
        lines.append("恢复资格确认后，满足双入场条件买回可投资资产100%TQQQ。")
    else:
        lines.append("满足双入场条件后，普通买回60%TQQQ＋40%QQQ；需后续新收盘。")
    if reason in ("MA_DEFENSE", "ROLLING20_EXIT"):
        lines.append("本次退出，同一信号不买回。")
    else:
        lines.append("同一信号不退出后立即买回。")
    lines.extend(["", SEPARATOR, "【执行提醒】", ""])
    if report.get("open_order_count", 0):
        lines.append(f"有效或待核对挂单{report['open_order_count']}张，可能独立成交，请先核对。")
    else:
        lines.append("当前无有效挂单。")
    if "EXISTING_INSTRUCTIONS_REQUIRE_REVIEW" in report.get("blockers", []):
        lines.append("待处理：已有指令需核对。")
    if drift_pending:
        lines.append("70%漂移待核对：缺完整收盘持仓/现金流水，不用当前配置替代。")
    if cash.get("status") == "REVIEW":
        lines.append("补QQQ仅为预算，先核对漂移及实际费用，再确认执行。")
    elif cash.get("status") == "WAIT":
        labels = {"SETTLED_CASH_UNVERIFIED": "已结算美元待核实", "NO_SETTLED_SURPLUS": "没有超过预留的已结算美元",
                  "ORDER_OR_INSTRUCTION_CONFLICT": "先核对已有订单/指令", "ALLOCATION_UNVERIFIED": "先核对配置估值"}
        lines.append("现金分配：" + labels[cash["reason"]] + "。")
    lines.extend(["以上均为QQQ参考价，不是TQQQ委托价。", "盘中触及不执行；收盘确认后，下一交易日处理，参考线每日重算。", "精确边界按程序原始精度判断。", "", "配置按账户快照市值估算；本次仅通知，未提交订单。"])
    return "\n".join(lines)
