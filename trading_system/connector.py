"""实测字段边界。未知的即时报价结构不能猜测为已认证报价。"""

import json
from .market import InvalidInput, number


def decode_result(raw):
    if raw.get("isError"):
        raise InvalidInput("CONNECTOR_ERROR")
    if raw.get("structuredContent") is not None:
        return raw["structuredContent"]
    if "content" in raw:
        try:
            blocks = [x["text"] for x in raw["content"] if x.get("type") == "text"]
            if len(blocks) != 1:
                raise ValueError
            return json.loads(blocks[0])
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidInput("INVALID_CONNECTOR_RESULT") from exc
    return raw


def inspect_quote(raw):
    data = decode_result(raw)
    if data.get("top-status", {}).get("status") == "FROZEN":
        reason = "FROZEN_QUOTE"
    elif "bid-ask" not in data:
        reason = "MISSING_BID_ASK"
    else:
        reason = "UNVERIFIED_BID_ASK_SCHEMA"
    return dict(status="BLOCKED", reason=reason)


def tool_payload(payload):
    """仅转换已批准计划的数字表示，不改方向、价格、数量。"""
    result = dict(payload)
    for field in ("quantity", "limit_price"):
        value = number(payload[field])
        result[field] = float(value)
        if number(result[field]) != value:
            raise InvalidInput("LOSSY_TOOL_SERIALIZATION")
    return result
