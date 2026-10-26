"""已授权资金范围的账户快照估值；不代表可用购买力或收盘对账。"""

from trading_system.market import InvalidInput, number

CONTRACTS = {"QQQ": 320227571, "TQQQ": 72539702, "SGOV": 424099317}
IREN = 526906130
TERMINAL_ORDERS = {"CANCELLED", "CANCELED", "FILLED", "REJECTED", "EXPIRED"}


def scope_confirmed(config):
    return (config.get("cash_scope_confirmed") is True
            and config.get("portfolio_scope") == "ALL_EXCEPT_IREN"
            and config.get("excluded_contract_ids") == [IREN])


def working_orders(orders):
    return [o for o in orders if str(o.get("order_status", "")).upper() not in TERMINAL_ORDERS]


def allocation_for(positions, balances, config):
    if not scope_confirmed(config):
        return {"confirmed": False, "scope_confirmed": False, "reason": "STRATEGY_SCOPE_UNCONFIRMED"}
    try:
        values = {s: number("0") for s in (*CONTRACTS, "USD")}
        seen = set()
        for p in positions:
            conid = p["contract_id"]
            if conid == IREN:
                continue
            if not isinstance(conid, int) or isinstance(conid, bool) or conid <= 0 or conid in seen:
                raise InvalidInput("UNVERIFIED_STRATEGY_CONTRACT")
            seen.add(conid)
            if p["currency"] != "USD":
                raise InvalidInput("UNVERIFIED_POSITION_FX")
            qty, value = number(p["position"]), number(p["market_value"])
            if qty < 0 or value < 0 or (qty == 0 and value != 0) or (qty > 0 and value <= 0):
                raise InvalidInput("INVALID_STRATEGY_VALUATION")
            symbol = next((s for s, c in CONTRACTS.items() if c == conid), "OTHER")
            values[symbol] = values.get(symbol, number("0")) + value
        seen_currencies = set()
        settled = None
        for b in balances:
            currency = b["currency"]
            if currency == "BASE":
                continue
            if not isinstance(currency, str) or currency in seen_currencies:
                raise InvalidInput("AMBIGUOUS_CASH_BALANCES")
            seen_currencies.add(currency)
            cash = number(b["cash_balance"])
            if currency != "USD" and cash != 0:
                raise InvalidInput("UNVERIFIED_NON_USD_CASH")
            if currency == "USD":
                if cash < 0:
                    raise InvalidInput("NEGATIVE_STRATEGY_CASH")
                values["USD"] = cash
                # 缺失或非法结算字段只阻断现金预算，不破坏已取得的快照估值。
                try:
                    settled = str(number(b["settled_cash"]))
                except (InvalidInput, KeyError, TypeError):
                    settled = None
        if "USD" not in seen_currencies:
            raise InvalidInput("MISSING_USD_CASH")
        total = sum(values.values())
        if total <= 0:
            raise InvalidInput("EMPTY_STRATEGY_ASSETS")
        return {"confirmed": True, "scope_confirmed": True,
                "weights": {s: str(v / total) for s, v in values.items()},
                "values_usd": {s: str(v) for s, v in values.items()}, "total_usd": str(total),
                "settled_cash_usd": settled,
                "excluded_contract_ids": [IREN], "basis": "IBKR_ACCOUNT_SNAPSHOT_MARKET_VALUE_AND_CASH"}
    except (InvalidInput, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, InvalidInput) else "MISSING_VALUATION_FIELD"
        return {"confirmed": False, "scope_confirmed": True, "reason": reason}
