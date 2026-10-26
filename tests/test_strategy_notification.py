"""通知行为：可读点位、真实资金口径、阶段条件和完整推送。"""

from copy import deepcopy
import unittest

from cloud_runner.runtime import message_for


def report(phase="MIXED", reason="HOLD"):
    return {
        "signal_day": "2026-10-23", "computed_at": "2026-10-25T17:07:40+00:00",
        "collected_at": "2026-10-25T17:07:40+00:00", "strategy_version": "PRIVATE_VERSION",
        "phase": phase, "decision": {"phase": phase, "reason": reason},
        "indicators": {"close": "704.4", "sma200": "677.29525", "peak20": "759.66"},
        "thresholds": {"ma_entry": "684.0682025", "ma_defense": "670.5222975", "rolling_exit": "698.8872"},
        "positions": {"QQQ": "8.1234", "TQQQ": "14.5678", "SGOV": "0"},
        "open_order_count": 1, "blockers": ["STRATEGY_CASH_SCOPE_UNCONFIRMED", "CLOSE_HOLDINGS_NOT_RECONCILED"],
    }


class NotificationTests(unittest.TestCase):
    def test_approved_example_prioritizes_levels_and_hides_internal_values(self):
        r = report()
        before = deepcopy(r)
        text = message_for(r, "deployment-notification-test")
        for expected in ("【今日动作】继续持有", "无新增买入", "2026-10-23", "704.40", "698.89", "670.52", "0.78%", "4.81%", "60%TQQQ", "100%TQQQ"):
            self.assertIn(expected, text)
        self.assertLess(text.index("698.89"), text.index("670.52"))
        for hidden in ("8.1234", "14.5678", "deployment-notification-test", "PRIVATE_VERSION", "MIXED", "17:07:40", "采集：", "版本："):
            self.assertNotIn(hidden, text)
        self.assertEqual(r, before)

    def test_unconfirmed_cash_does_not_invent_relative_etf_weights(self):
        text = message_for(report(), "x")
        self.assertIn("QQQ 待确认", text)
        self.assertIn("TQQQ 待确认", text)
        self.assertIn("SGOV 0%", text)
        self.assertIn("美元现金 待确认", text)
        self.assertNotIn("85.7%", text)
        self.assertNotIn("14.3%", text)

    def test_verified_allocation_includes_cash_and_excludes_other_assets(self):
        r = report()
        r["allocation"] = {"confirmed": True, "weights": {"QQQ": ".4", "TQQQ": ".3", "SGOV": ".2", "USD": ".1"}}
        text = message_for(r, "x")
        for expected in ("QQQ 40%", "TQQQ 30%", "SGOV 20%", "美元现金 10%"):
            self.assertIn(expected, text)
        self.assertIn("已排除IREN", text)
        self.assertNotIn("IREN ", text)

    def test_invalid_or_unconfirmed_allocation_remains_pending(self):
        weights = {"QQQ": ".4", "TQQQ": ".3", "SGOV": ".2", "USD": ".1"}
        for change in ({"QQQ": "NaN"}, {"USD": "-.1"}, {"USD": ".9"}, {"IREN": "0"}):
            r = report()
            r["allocation"] = {"confirmed": True, "weights": dict(weights, **change)}
            self.assertIn("QQQ 待确认", message_for(r, "x"))
        r = report()
        r["allocation"] = {"confirmed": False, "weights": weights}
        self.assertIn("QQQ 待确认", message_for(r, "x"))
        r["allocation"] = "invalid"
        self.assertIn("QQQ 待确认", message_for(r, "x"))

    def test_qqq_only_has_only_applicable_sale_and_normal_buyback(self):
        r = report("QQQ_ONLY")
        r["positions"]["TQQQ"] = "0"
        text = message_for(r, "x")
        self.assertNotIn("20日8%退出线：", text)
        self.assertIn("均线防守", text)
        self.assertIn("60%TQQQ＋40%QQQ", text)

    def test_defensive_has_no_spurious_sale_and_recovery_allocation(self):
        r = report("DEFENSIVE")
        r["positions"] = {"QQQ": "0", "TQQQ": "0", "SGOV": "100"}
        text = message_for(r, "x")
        self.assertIn("持有SGOV", text)
        self.assertIn("可投资资产100%TQQQ", text)
        self.assertIn("恢复资格", text)
        self.assertNotIn("20日8%退出线：", text)

    def test_exit_and_reentry_not_on_same_signal_and_ma_priority(self):
        r = report(reason="MA_DEFENSE")
        r["indicators"]["close"] = "650"
        text = message_for(r, "x")
        self.assertIn("转入SGOV", text)
        self.assertIn("已触发", text)
        self.assertIn("后续新收盘", text)
        self.assertIn("同一信号不买回", text)
        self.assertLess(text.index("均线防守线："), text.index("20日8%退出线："))

    def test_pending_reconciliation_is_not_a_hold_recommendation(self):
        r = report()
        r["decision"] = None
        text = message_for(r, "x")
        self.assertIn("先核对", text)
        self.assertNotIn("【今日动作】继续持有", text)

    def test_buyback_threshold_is_max_and_strictly_greater(self):
        r = report("QQQ_ONLY", "MIXED_ENTRY")
        r["thresholds"]["ma_entry"] = "720"
        text = message_for(r, "x")
        self.assertIn("高于约720.00", text)
        self.assertIn("60%TQQQ＋40%QQQ", text)

    def test_normal_and_boost_actions_and_absence_of_old_orders(self):
        for reason in ("ROLLING20_EXIT", "MIXED_ENTRY", "MA_RECOVERY", "DRIFT_CAP"):
            r = report(reason=reason)
            r["open_order_count"] = 0
            text = message_for(r, "x")
            self.assertNotIn("旧单", text)
            self.assertIn("下一交易日", text)

    def test_all_phases_fit_wecom_without_losing_close_reminder(self):
        for phase in ("MIXED", "BOOST", "QQQ_ONLY", "DEFENSIVE"):
            for reason in ("HOLD", "MA_DEFENSE", "ROLLING20_EXIT", "MIXED_ENTRY", "MA_RECOVERY", "DRIFT_CAP"):
                r = report(phase, reason)
                text = message_for(r, "x")
                self.assertLessEqual(len(text.encode("utf-8")), 1900, (phase, reason, len(text.encode("utf-8"))))
                self.assertIn("盘中触及不执行", text)
                self.assertIn("每日重算", text)
                self.assertIn("原始精度", text)

    def test_approved_sections_and_top_allocation_have_visual_separation(self):
        text = message_for(report(), "x")
        for section in ("【今日动作】", "【信号依据】", "【当前配置】", "【优先关注：卖出参考】", "【买回观察】", "【执行提醒】"):
            self.assertIn(section, text)
        self.assertEqual(text.count("━━━━━━━━━━━━━━━━━━"), 3)
        self.assertLess(text.index("【当前配置】"), text.index("━━━━━━━━━━━━━━━━━━"))
        self.assertIn("\n\n【当前配置】", text)
        self.assertIn("当前无有效挂单", message_for(dict(report(), open_order_count=0), "x"))

    def test_existing_instructions_are_actionable_and_exact_boundary_uses_original_price(self):
        r = report(reason="ROLLING20_EXIT")
        r["indicators"]["close"] = r["thresholds"]["rolling_exit"]
        r["blockers"].append("EXISTING_INSTRUCTIONS_REQUIRE_REVIEW")
        text = message_for(r, "x")
        self.assertIn("已触发", text)
        self.assertIn("已有指令需核对", text)
        self.assertIn("同一信号不买回", text)


if __name__ == "__main__":
    unittest.main()
