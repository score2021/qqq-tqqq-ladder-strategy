"""交易日调度、原始证据校验和持久通知。"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import sqlite3
import tarfile
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from trading_system.market import Calendar, InvalidInput, number, positive, stamp
from trading_system.strategy import VERSION, decide, indicators
from trading_system.connector import decode_result
from .portfolio import CONTRACTS, allocation_for, working_orders
from .review import cash_review, close_review



def extract_bundle(log, now):
    """仅从 CLI 已完成工具事件取事实，完全忽略模型最终文字。"""
    bundle = {"status": "OK", "collected_at": stamp(now).isoformat(), "history": {}, "quotes": {}}
    names = {"get_account_positions": "positions", "get_account_balances": "balances",
             "get_account_orders": "orders", "get_order_instructions": "instructions",
             "get_account_trades": "trades"}
    for line in Path(log).read_text().splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        if item.get("type") != "mcp_tool_call" or item.get("server") != "codex_apps":
            continue
        tool = item.get("tool", "")
        if not tool.startswith("interactive_brokers_ibkr."):
            raise InvalidInput("UNAUTHORIZED_APP_TOOL")
        name = tool.split(".", 1)[1]
        if name not in names and name != "get_price_history":
            raise InvalidInput("UNAUTHORIZED_BROKER_TOOL")
        if event.get("type") != "item.completed" or item.get("status") != "completed":
            continue
        result = item.get("result") or {}
        result = {"content": result.get("content", []),
                  "structuredContent": result.get("structured_content"),
                  "isError": bool(item.get("error"))}
        if name in names:
            bundle[names[name]] = result
            if name == "get_account_trades":
                bundle["trade_request"] = item.get("arguments", {})
        else:
            args = item.get("arguments", {})
            if args.get("outside_rth") is not False or args.get("step") != "ONE_DAY" or args.get("security_type") != "STK" or ("period" in args and "step_count" in args):
                raise InvalidInput("UNVERIFIED_HISTORY_REQUEST")
            symbols = [s for s, c in CONTRACTS.items() if c == args.get("contract_id")]
            if len(symbols) != 1:
                raise InvalidInput("UNEXPECTED_CONTRACT")
            bundle["history"][symbols[0]] = result
    if not all(key in bundle for key in names.values()) or set(bundle["history"]) != set(CONTRACTS):
        raise InvalidInput("INCOMPLETE_BROKER_TOOL_TRACE")
    return bundle


def due_jobs(now):
    now = stamp(now)
    local = now.astimezone(ZoneInfo("Asia/Singapore"))
    day = str(local.date())
    try:
        opens, _ = Calendar().session(day)
    except InvalidInput:
        return []
    start = local.replace(hour=21, minute=20, second=0, microsecond=0)
    end = min(start + timedelta(minutes=10), opens)
    return [f"{day}:preopen"] if start <= now < end else []


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backup_state(root, name):
    """调度锁内备份；SQLite 走 backup API，凭据不进备份包。"""
    root = Path(root)
    target = root / "backups"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = target / f"state-{at}.tar.gz"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with tempfile.TemporaryDirectory(dir=target) as tmp, os.fdopen(fd, "wb") as file, tarfile.open(fileobj=file, mode="w:gz") as archive:
        for source in sorted((root / "state").glob("*.json")):
            archive.add(source, arcname="state/" + source.name, recursive=False)
        config = root / "config/cloud.local.json"
        if config.exists():
            archive.add(config, arcname="config/cloud.local.json", recursive=False)
        for source in sorted((root / "state").glob("*.sqlite")):
            copy = Path(tmp) / source.name
            with sqlite3.connect(source) as src, sqlite3.connect(copy) as dst:
                src.backup(dst)
            archive.add(copy, arcname="state/" + source.name, recursive=False)
    for old in sorted(target.glob("state-*.tar.gz"))[:-14]:
        old.unlink()
    return path


def raw(value):
    if not isinstance(value, dict):
        raise InvalidInput("RAW_OBJECT_REQUIRED")
    return decode_result(value) if "content" in value else value


def history_bars(value, now):
    data = raw(value)
    if data.get("source") != "Last" or len(data.get("time", [])) != len(data.get("close", [])):
        raise InvalidInput("UNVERIFIED_HISTORY_SCHEMA")
    latest = Calendar().latest_complete(now)
    bars = []
    for t, c in zip(data["time"], data["close"]):
        day = str(stamp(t).astimezone(ZoneInfo("America/New_York")).date())
        if day <= latest:
            bars.append({"date": day, "close": str(positive(c)), "complete": True})
    if not bars or bars[-1]["date"] != latest or [b["date"] for b in bars] != sorted(set(b["date"] for b in bars)):
        raise InvalidInput("STALE_OR_UNORDERED_HISTORY")
    return bars


def analyze(bundle, now, config):
    now = stamp(now)
    if bundle.get("status") != "OK":
        raise InvalidInput("IBKR_COLLECTION_NOT_OK")
    age = (now - stamp(bundle["collected_at"])).total_seconds()
    if not 0 <= age <= 900:
        raise InvalidInput("STALE_COLLECTION")
    facts = {}
    for key in ("positions", "orders", "instructions", "balances", "trades"):
        value = raw(bundle.get(key))
        if not isinstance(value.get(key), list):
            raise InvalidInput("UNVERIFIED_" + key.upper() + "_SCHEMA")
        facts[key] = value[key]
    all_bars = {s: history_bars(bundle["history"][s], now) for s in CONTRACTS}
    q = all_bars["QQQ"]
    signal_day = Calendar().validate_bars(q, now)
    values = indicators([b["close"] for b in q])
    positions = {s: "0" for s in CONTRACTS}
    seen = set()
    for position in facts["positions"]:
        for symbol, conid in CONTRACTS.items():
            if position.get("contract_id") == conid:
                if symbol in seen or number(position["position"]) < 0:
                    raise InvalidInput("AMBIGUOUS_OR_SHORT_POSITION")
                seen.add(symbol)
                positions[symbol] = str(number(position["position"]))
    marks = {s: number(bs[-1]["close"]) for s, bs in all_bars.items()}
    risky = sum(number(positions[s]) * marks[s] for s in ("QQQ", "TQQQ"))
    upper_weight = number(positions["TQQQ"]) * marks["TQQQ"] / risky if risky else number("0")
    phase = config["phase"]
    allocation = allocation_for(facts["positions"], facts["balances"], config)
    orders = working_orders(facts["orders"])
    blockers = ["READ_ONLY_NOTIFICATION",
                "ACCOUNT_BINDING_NOT_AUTOMATICALLY_VERIFIED", "EXECUTABLE_QUOTE_NOT_VERIFIED",
                "CLOSE_HOLDINGS_NOT_RECONCILED"]
    if not allocation["scope_confirmed"]:
        blockers.append("STRATEGY_CASH_SCOPE_UNCONFIRMED")
    elif not allocation["confirmed"]:
        blockers.append("STRATEGY_ALLOCATION_UNVERIFIED")
    if orders:
        blockers.append("EXISTING_ORDERS_RETAINED")
    if facts["instructions"]:
        blockers.append("EXISTING_INSTRUCTIONS_REQUIRE_REVIEW")
    if (phase in ("MIXED", "BOOST") and number(positions["TQQQ"]) == 0) or (phase in ("QQQ_ONLY", "DEFENSIVE") and number(positions["TQQQ"]) != 0):
        blockers.append("PHASE_POSITION_MISMATCH")
    args = (phase, values["close"], values["sma200"], values["peak20"])
    low = decide(*args, "0", config.get("recovery_eligible", False))
    high = decide(*args, "1", config.get("recovery_eligible", False))
    # 未确认收盘持仓与资金时，不能把当前持仓比例当作正式漂移信号。
    decision = low if low == high else None
    if decision is None:
        blockers.append("DRIFT_REQUIRES_CONFIRMED_CLOSE_NAV")
    if "PHASE_POSITION_MISMATCH" in blockers:
        decision = None
    closing = close_review(facts["trades"], signal_day, now, bundle.get("trade_request"))
    cash = cash_review(allocation, phase, low, config, bool(orders or facts["instructions"]),
                       "PHASE_POSITION_MISMATCH" in blockers)
    return {"strategy_version": VERSION, "signal_day": signal_day,
            "collected_at": bundle["collected_at"], "computed_at": now.isoformat(),
            "phase": phase, "indicators": values,
            "thresholds": {"ma_entry": str(number(values["sma200"]) * number("1.01")),
                           "ma_defense": str(number(values["sma200"]) * number(".99")),
                           "rolling_exit": str(number(values["peak20"]) * number(".92"))},
            "decision": decision, "price_decision": low, "positions": positions,
            "close_review": closing, "cash_review": cash,
            "observed_weight_upper_bound": str(upper_weight),
            "open_order_count": len(orders), "completed_order_count": len(facts["orders"]) - len(orders),
            "allocation": allocation, "blockers": blockers,
            "executable_orders": [], "price_basis": "IBKR RTH ONE_DAY Last.close; frozen snapshots excluded"}


def send_wecom(secret_path, message):
    url = Path(secret_path).read_text().strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "qyapi.weixin.qq.com" or parsed.path != "/cgi-bin/webhook/send" or not parse_qs(parsed.query).get("key"):
        raise InvalidInput("INVALID_WECOM_WEBHOOK")
    text = message.encode("utf-8")
    if len(text) > 1900:
        message = text[:1850].decode("utf-8", errors="ignore") + "\n完整报告已保存在云主机。"
    payload = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def deliver(state_dir, key, message, sender):
    path = Path(state_dir) / "deliveries.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    old = state.get(key, {}).get("status")
    if old in ("SENT", "SENDING", "UNKNOWN"):
        return "ALREADY_SENT" if old == "SENT" else "UNKNOWN_DELIVERY"
    state[key] = {"status": "SENDING", "at": datetime.now(timezone.utc).isoformat()}
    private_json(path, state)
    try:
        result = sender(message)
    except Exception as exc:
        state[key]["status"] = "UNKNOWN"
        private_json(path, state)
        raise RuntimeError("WECOM_DELIVERY_UNKNOWN") from None
    if result.get("errcode") != 0:
        state[key].update(status="FAILED", errcode=result.get("errcode"))
        private_json(path, state)
        raise RuntimeError("WECOM_BUSINESS_ERROR")
    state[key]["status"] = "SENT"
    private_json(path, state)
    return "SENT"


def message_for(report, job):
    from .notification import render

    return render(report)
