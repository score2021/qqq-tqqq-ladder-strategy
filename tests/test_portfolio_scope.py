"""已授权资金范围与账户真实状态的边界测试。"""

from copy import deepcopy
from decimal import Decimal
import unittest

from cloud_runner.runtime import analyze, message_for
from test_cloud_runner import bundle, NOW


def configured():
    return {"phase": "MIXED", "cash_scope_confirmed": True,
            "portfolio_scope": "ALL_EXCEPT_IREN", "excluded_contract_ids": [526906130]}


def account():
    b = bundle()
    b["positions"]["positions"] = [
        {"contract_id": 526906130, "contract_description": "IREN", "position": "5", "currency": "USD", "market_value": "99999.123"},
        {"contract_id": 320227571, "contract_description": "QQQ", "position": "50", "currency": "USD", "market_value": "5760.12345"},
        {"contract_id": 72539702, "contract_description": "TQQQ", "position": "20", "currency": "USD", "market_value": "960.0123"},
    ]
    b["balances"]["balances"] = [
        {"currency": "BASE", "cash_balance": "3280.0005"},
        {"currency": "USD", "cash_balance": "3280.0005", "settled_cash": "3280.0005"},
        {"currency": "CNH", "cash_balance": "0", "exchange_rate": ".15"},
    ]
    b["orders"]["orders"] = [{"order_id": 1, "order_status": "CANCELLED"}]
    return b


class PortfolioScopeTests(unittest.TestCase):
    def test_scope_uses_snapshot_values_and_usd_cash_once_excluding_iren(self):
        b = account()
        before = deepcopy(b)
        r = analyze(b, NOW, configured())
        a = r["allocation"]
        self.assertTrue(a["confirmed"])
        self.assertEqual(Decimal(a["total_usd"]), Decimal("10000.13625"))
        self.assertEqual(Decimal(a["values_usd"]["USD"]), Decimal("3280.0005"))
        self.assertEqual(Decimal(a["values_usd"]["QQQ"]), Decimal("5760.12345"))
        self.assertEqual(a["excluded_contract_ids"], [526906130])
        self.assertNotIn("STRATEGY_CASH_SCOPE_UNCONFIRMED", r["blockers"])
        self.assertIn("CLOSE_HOLDINGS_NOT_RECONCILED", r["blockers"])
        self.assertEqual(r["executable_orders"], [])
        self.assertEqual(b, before)

    def test_confirmed_notification_has_real_percentages_and_no_cash_scope_warning(self):
        text = message_for(analyze(account(), NOW, configured()), "hidden")
        for expected in ("QQQ 57.6%", "TQQQ 9.6%", "SGOV 0%", "美元现金 32.8%", "10,000.14", "当前无有效挂单"):
            self.assertIn(expected, text)
        self.assertNotIn("待确认", text)
        self.assertNotIn("资金归属待核对", text)
        self.assertNotIn("旧单保留", text)
        self.assertIn("按账户快照市值估算", text)

    def test_new_usd_asset_is_in_scope_not_silently_ignored(self):
        b = account()
        b["positions"]["positions"].append({"contract_id": 777, "contract_description": "ABC", "position": "10", "currency": "USD", "market_value": "1000"})
        r = analyze(b, NOW, configured())
        self.assertTrue(r["allocation"]["confirmed"])
        self.assertEqual(Decimal(r["allocation"]["values_usd"]["OTHER"]), Decimal("1000"))
        self.assertEqual(Decimal(r["allocation"]["total_usd"]), Decimal("11000.13625"))
        self.assertIn("其他策略资产", message_for(r, "x"))

    def test_cancelled_filled_rejected_are_not_working_pending_cancel_is_working(self):
        b = account()
        b["orders"]["orders"] = [{"order_status": s} for s in ("CANCELLED", "CANCELED", "FILLED", "REJECTED", "EXPIRED")]
        r = analyze(b, NOW, configured())
        self.assertEqual(r["open_order_count"], 0)
        self.assertNotIn("EXISTING_ORDERS_RETAINED", r["blockers"])
        b["orders"]["orders"].extend([{"order_status": "PendingCancel"}, {"order_status": "UNKNOWN"}, {}])
        r = analyze(b, NOW, configured())
        self.assertEqual(r["open_order_count"], 3)
        self.assertIn("EXISTING_ORDERS_RETAINED", r["blockers"])
        self.assertIn("有效或待核对挂单3张", message_for(r, "x"))

    def test_confirmation_requires_explicit_scope_and_exact_exclusion(self):
        for config in ({"phase": "MIXED"}, {"phase": "MIXED", "cash_scope_confirmed": True}, dict(configured(), excluded_contract_ids=[526906130, 72539702])):
            r = analyze(account(), NOW, config)
            self.assertFalse(r["allocation"]["confirmed"])
            self.assertIn("STRATEGY_CASH_SCOPE_UNCONFIRMED", r["blockers"])

    def test_nonzero_foreign_cash_missing_usd_and_negative_cash_are_not_fabricated(self):
        cases = []
        b = account(); b["balances"]["balances"][-1]["cash_balance"] = "100"; cases.append(b)
        b = account(); b["balances"]["balances"] = [{"currency": "BASE", "cash_balance": "100"}]; cases.append(b)
        b = account(); b["balances"]["balances"][1]["cash_balance"] = "-1"; cases.append(b)
        b = account(); b["balances"]["balances"].append(dict(b["balances"]["balances"][1])); cases.append(b)
        b = account(); b["balances"]["balances"][1]["cash_balance"] = "NaN"; cases.append(b)
        for b in cases:
            r = analyze(b, NOW, configured())
            self.assertFalse(r["allocation"]["confirmed"])
            self.assertIn("STRATEGY_ALLOCATION_UNVERIFIED", r["blockers"])
            self.assertIn("配置估值待核对", message_for(r, "x"))

    def test_missing_valuation_duplicate_or_unidentified_assets_fail_closed(self):
        cases = []
        b = account(); del b["positions"]["positions"][1]["market_value"]; cases.append(b)
        b = account(); b["positions"]["positions"].append({"position": "1", "currency": "USD", "market_value": "100"}); cases.append(b)
        b = account(); b["positions"]["positions"].append({"contract_id": 777, "position": "1", "currency": "EUR", "market_value": "100"}); cases.append(b)
        for b in cases:
            r = analyze(b, NOW, configured())
            self.assertFalse(r["allocation"]["confirmed"])


if __name__ == "__main__":
    unittest.main()
