"""合成账户验证信号、资金、发布恢复和实际成交；不访问券商。"""

from copy import deepcopy
import contextlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from trading_system.strategy import decide, indicators
from trading_system.market import Calendar, InvalidInput
from trading_system.service import Service
from trading_system.connector import inspect_quote, decode_result, tool_payload
from trading_system.__main__ import main

NOW = datetime(2026, 10, 26, 13, 35, tzinfo=timezone.utc)


def config(**changes):
    result = dict(
        mode="REVIEW",
        binding_ref="SYNTHETIC",
        scope_confirmed=True,
        account_binding_confirmed=True,
        migration_confirmed=True,
        fee_budget_confirmed=True,
        fee_cap_per_order="1",
        instrument_rules_confirmed=True,
    )
    result.update(changes)
    return result


def snapshot(positions=None, cash="200", at=NOW):
    return dict(
        binding_ref="SYNTHETIC",
        at=at.isoformat(),
        positions=positions or {"QQQ": "98", "TQQQ": "0", "SGOV": "0"},
        cash=cash,
        settled_available_usd=cash,
        marks={"QQQ": "100", "TQQQ": "50", "SGOV": "100"},
        orders=[],
        instructions=[],
        orders_complete=True,
        instructions_complete=True,
    )


def quotes(at=NOW):
    return {
        s: dict(
            bid=p,
            ask=str(D(p) + D(".02")),
            at=at.isoformat(),
            live=True,
            timestamp_verified=True,
        )
        for s, p in [("QQQ", "100"), ("TQQQ", "50"), ("SGOV", "100")]
    }


def signal():
    days = Calendar().history("2026-10-23", 200)
    return dict(
        bars=[
            dict(date=d, close="110" if i == 199 else "100", complete=True)
            for i, d in enumerate(days)
        ],
        source="SYNTHETIC",
        adjustment="split_adjusted",
        close=dict(
            date="2026-10-23",
            positions={"QQQ": "98", "TQQQ": "0", "SGOV": "0"},
            marks={"QQQ": "110", "TQQQ": "50", "SGOV": "100"},
            cash="200",
            receivables="0",
            confirmed=True,
        ),
    )


class StrategyTests(unittest.TestCase):
    def test_strict_entry_and_guard(self):
        self.assertEqual(decide("QQQ_ONLY", "101", "100", "101")["reason"], "HOLD")
        self.assertEqual(decide("QQQ_ONLY", "102", "100", "120")["reason"], "HOLD")
        self.assertEqual(decide("QQQ_ONLY", "110.4", "100", "120")["reason"], "HOLD")
        self.assertEqual(decide("QQQ_ONLY", "111", "100", "120")["phase"], "MIXED")

    def test_exit_priority_equalities_and_boost(self):
        self.assertEqual(decide("BOOST", "99", "100", "120")["phase"], "DEFENSIVE")
        self.assertEqual(decide("BOOST", "110.4", "100", "120")["phase"], "QQQ_ONLY")
        self.assertEqual(decide("MIXED", "110", "100", "110", ".7")["reason"], "HOLD")
        self.assertEqual(
            decide("MIXED", "110", "100", "110", ".70001")["reason"], "DRIFT_CAP"
        )
        self.assertEqual(decide("BOOST", "110", "100", "110", ".99")["reason"], "HOLD")
        self.assertEqual(
            decide("DEFENSIVE", "110", "100", "110", recovery_eligible=True)["phase"],
            "BOOST",
        )
        with self.assertRaises(InvalidInput):
            decide("DEFENSIVE", "110", "100", "110", recovery_eligible=False)

    def test_indicator_rolling_window_and_bad_data(self):
        self.assertEqual(
            indicators(["100"] * 180 + ["200"] + ["100"] * 20)["peak20"], "100"
        )
        for values in (["100"] * 199, ["NaN"] * 200, ["0"] * 200):
            with self.assertRaises(InvalidInput):
                indicators(values)
        for args in [
            ("MIXED", "0", "100", "100"),
            ("OTHER", "100", "100", "100"),
            ("MIXED", "100", "100", "99"),
            ("MIXED", "100", "100", "100", "2"),
        ]:
            with self.assertRaises(InvalidInput):
                decide(*args)

    def test_calendar_holiday_early_close_and_dst(self):
        cal = Calendar()
        self.assertEqual(cal.next_session("2026-10-23"), "2026-10-26")
        self.assertEqual(cal.session("2026-11-27")[1].hour, 18)
        self.assertEqual(cal.session("2026-10-26")[0].hour, 13)
        self.assertEqual(cal.session("2026-11-02")[0].hour, 14)
        self.assertEqual(cal.latest_complete(NOW), "2026-10-23")
        self.assertEqual(cal.latest_complete(NOW.replace(hour=21)), "2026-10-26")

    def test_actual_connector_shapes_do_not_promote_last_to_quote(self):
        observed = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "last": {"price": 100, "ts": 1792799985},
                            "top-status": {"status": "FROZEN"},
                        }
                    ),
                }
            ],
            "structuredContent": None,
            "isError": False,
        }
        self.assertEqual(inspect_quote(observed)["reason"], "FROZEN_QUOTE")
        with self.assertRaises(InvalidInput):
            decode_result({"isError": True})
        self.assertEqual(
            inspect_quote({"last": {"ts": 1}})["reason"], "MISSING_BID_ASK"
        )
        self.assertEqual(
            inspect_quote({"bid-ask": {"bid": 1, "ask": 2}})["reason"],
            "UNVERIFIED_BID_ASK_SCHEMA",
        )


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.s = Service(self.path, config())
        self.s.initialize(snapshot(), "QQQ_ONLY", True)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def ready(self):
        self.assertEqual(self.s.evaluate(signal(), NOW)["status"], "DECISION_READY")
        return self.s.prepare(snapshot(), quotes(), NOW)

    def publish_bind(self, plan, order="O1", now=NOW):
        pid = plan["plan"]["id"]
        claim = self.s.record(dict(kind="publication_start", plan_id=pid), now)
        self.assertEqual(claim["status"], "PUBLISHING")
        payload = claim["payload"]
        self.s.record(
            dict(
                kind="instruction_receipt",
                plan_id=pid,
                instruction_id="I" + order,
                payload=payload,
                url="https://example.invalid/list",
            ),
            now,
        )
        self.s.record(
            dict(
                kind="order_binding",
                instruction_id="I" + order,
                order_id=order,
                user_confirmed=True,
                binding_ref="SYNTHETIC",
                symbol=plan["plan"]["symbol"],
                side=payload["side"],
                quantity=payload["quantity"],
            ),
            now,
        )
        return payload

    def test_default_shadow_keeps_positions_uninitialized(self):
        other = Service(Path(self.tmp.name) / "shadow.sqlite", {})
        try:
            other.initialize(snapshot())
            self.assertEqual(other.status()["phase"], "UNINITIALIZED")
            self.assertEqual(other.status()["positions"]["QQQ"], "98")
            self.assertEqual(other.evaluate(signal(), NOW)["status"], "SHADOW_ANALYSIS")
            self.assertEqual(other.status()["goals"], [])
        finally:
            other.close()

    def test_plan_is_not_a_fill_and_repeat_deduplicates(self):
        plan = self.ready()
        self.assertEqual(plan["status"], "PLAN_READY")
        self.assertEqual(plan["plan"]["payload"]["side"], "SELL")
        self.assertEqual(self.s.status()["positions"]["QQQ"], "98")
        self.assertEqual(self.s.evaluate(signal(), NOW)["status"], "EXISTING_DECISION")
        self.assertEqual(
            self.s.prepare(snapshot(), quotes(), NOW)["plan"]["id"], plan["plan"]["id"]
        )
        self.assertEqual(self.s.status()["phase"], "QQQ_ONLY")

    def test_account_orders_quote_and_cash_gates(self):
        self.s.evaluate(signal(), NOW)
        for key, value, reason in [
            ("binding_ref", "WRONG", "ACCOUNT_BINDING_MISMATCH"),
            ("orders_complete", False, "INCOMPLETE_SNAPSHOT"),
            ("orders", [{"order_id": "UNKNOWN"}], "UNCLASSIFIED_ORDER"),
        ]:
            snap = snapshot()
            snap[key] = value
            self.assertEqual(self.s.prepare(snap, quotes(), NOW)["reason"], reason)
        for key, value in [
            ("live", False),
            ("timestamp_verified", False),
            ("bid", "0"),
        ]:
            q = quotes()
            q["QQQ"][key] = value
            self.assertEqual(self.s.prepare(snapshot(), q, NOW)["status"], "BLOCKED")
        self.assertEqual(
            self.s.prepare(snapshot(at=NOW - timedelta(minutes=2)), quotes(), NOW)[
                "reason"
            ],
            "STALE_SNAPSHOT",
        )
        self.assertEqual(
            self.s.prepare(snapshot(), quotes(NOW - timedelta(minutes=2)), NOW)[
                "reason"
            ],
            "STALE_QUOTE",
        )

    def test_complete_sessions_and_close_holdings_are_required(self):
        for mutate in ("incomplete", "gap", "future", "duplicate", "close_unconfirmed"):
            data = signal()
            if mutate == "incomplete":
                data["bars"][-1]["complete"] = False
            if mutate == "gap":
                data["bars"].pop(1)
            if mutate == "future":
                data["bars"][-1]["date"] = "2026-10-26"
            if mutate == "duplicate":
                data["bars"][-2]["date"] = data["bars"][-1]["date"]
            if mutate == "close_unconfirmed":
                data["close"]["confirmed"] = False
            self.assertEqual(self.s.evaluate(data, NOW)["status"], "BLOCKED", mutate)

    def test_unknown_publication_survives_reopen_and_never_repeats(self):
        plan = self.ready()["plan"]
        self.s.record(dict(kind="publication_start", plan_id=plan["id"]), NOW)
        self.s.close()
        self.s = Service(self.path, config())
        self.assertEqual(
            self.s.prepare(snapshot(), quotes(), NOW)["status"], "UNKNOWN_PUBLICATION"
        )
        self.assertEqual(
            self.s.record(dict(kind="publication_start", plan_id=plan["id"]), NOW)[
                "status"
            ],
            "UNKNOWN_PUBLICATION",
        )

    def test_expiry_does_not_erase_submitted_or_unknown_work(self):
        plan = self.ready()["plan"]
        later = NOW + timedelta(minutes=6)
        self.assertEqual(
            self.s.record(dict(kind="publication_start", plan_id=plan["id"]), later)[
                "reason"
            ],
            "PLAN_EXPIRED",
        )
        new = self.s.prepare(snapshot(at=later), quotes(later), later)
        self.assertNotEqual(new["plan"]["id"], plan["id"])
        self.publish_bind(new, now=later)
        self.assertEqual(
            self.s.prepare(snapshot(at=later), quotes(later), later)["status"],
            "WAIT_FILL",
        )

    def test_partial_fill_duplicate_and_late_fee(self):
        plan = self.ready()
        self.publish_bind(plan)
        fill = dict(
            trade_id="F1",
            order_id="O1",
            symbol="QQQ",
            side="SELL",
            quantity="1.5",
            price="100",
            commission=".35",
        )
        snap = snapshot({"QQQ": "96.5", "TQQQ": "0", "SGOV": "0"}, "349.65")
        event = dict(
            snapshot=snap,
            fills=[fill],
            orders=[dict(order_id="O1", status="PARTIALLY_FILLED")],
        )
        self.assertEqual(self.s.reconcile(event, NOW)["status"], "WAIT_FILL")
        self.s.reconcile(event, NOW)
        self.assertEqual(self.s.status()["positions"]["QQQ"], "96.5")
        self.assertEqual(self.s.status()["cash"], "349.65")
        fill["commission"] = ".40"
        snap["cash"] = "349.60"
        snap["settled_available_usd"] = "349.60"
        self.s.reconcile(event, NOW)
        self.assertEqual(self.s.status()["cash"], "349.60")
        self.assertEqual(self.s.status()["fees"], "0.40")
        self.assertEqual(self.s.status()["phase"], "QQQ_ONLY")
        fill["quantity"] = "2"
        self.assertEqual(
            self.s.reconcile(event, NOW)["reason"], "TRADE_CORRECTION_REQUIRES_REVIEW"
        )

    def test_unknown_fill_and_snapshot_mismatch_block(self):
        self.assertEqual(
            self.s.reconcile(
                dict(
                    snapshot=snapshot(),
                    fills=[dict(trade_id="X", order_id="UNKNOWN")],
                    orders=[],
                ),
                NOW,
            )["reason"],
            "UNBOUND_ORDER",
        )
        snap = snapshot()
        snap["positions"]["TQQQ"] = "1"
        self.assertEqual(
            self.s.reconcile(dict(snapshot=snap, fills=[], orders=[]), NOW)["status"],
            "RECONCILE_REQUIRED",
        )
        self.assertEqual(self.s.prepare(snap, quotes(), NOW)["status"], "BLOCKED")

    def test_binding_and_receipt_must_match(self):
        p = self.ready()["plan"]
        self.s.record(dict(kind="publication_start", plan_id=p["id"]), NOW)
        bad = deepcopy(p["payload"])
        bad["quantity"] = "999"
        self.assertEqual(
            self.s.record(
                dict(
                    kind="instruction_receipt",
                    plan_id=p["id"],
                    instruction_id="I",
                    payload=bad,
                ),
                NOW,
            )["reason"],
            "INSTRUCTION_MISMATCH",
        )
        self.s.record(
            dict(
                kind="instruction_receipt",
                plan_id=p["id"],
                instruction_id="I",
                payload=p["payload"],
            ),
            NOW,
        )
        self.assertEqual(
            self.s.record(
                dict(
                    kind="order_binding",
                    instruction_id="I",
                    order_id="O",
                    user_confirmed=False,
                ),
                NOW,
            )["status"],
            "BLOCKED",
        )

    def test_cashflows_dividends_and_event_idempotency(self):
        ex = dict(kind="dividend_ex", event_id="EX", amount="10", source="synthetic")
        self.s.record(ex, NOW)
        self.s.record(ex, NOW)
        self.assertEqual(self.s.status()["cash"], "200")
        self.assertEqual(self.s.status()["receivables"], "10")
        self.s.record(
            dict(
                kind="dividend_pay",
                event_id="PAY",
                ex_event_id="EX",
                amount="8",
                source="synthetic",
            ),
            NOW,
        )
        self.assertEqual(self.s.status()["cash"], "208")
        self.assertEqual(self.s.status()["receivables"], "0")
        self.s.record(
            dict(
                kind="cashflow",
                event_id="IN",
                amount="100",
                source="synthetic",
                confirmed=True,
            ),
            NOW,
        )
        self.assertEqual(self.s.status()["cash"], "308")
        self.assertEqual(self.s.status()["phase"], "QQQ_ONLY")

    def test_only_actual_defense_and_recovery_fills_change_cycle(self):
        self.s.close()
        self.path.unlink()
        self.s = Service(self.path, config())
        self.s.initialize(
            snapshot({"QQQ": "0", "TQQQ": "0", "SGOV": "98"}),
            "DEFENSIVE",
            True,
            recovery_eligible=True,
        )
        self.s.evaluate(signal(), NOW)
        self.assertEqual(self.s.status()["phase"], "DEFENSIVE")
        self.assertTrue(self.s.status()["recovery_eligible"])

    def test_tool_payload_preserves_six_decimal_roundtrip(self):
        p = dict(
            contract_id_ex="1",
            side="BUY",
            quantity="0.123456",
            order_type="LIMIT",
            limit_price="1",
            time_in_force="DAY",
        )
        self.assertEqual(D(str(tool_payload(p)["quantity"])), D("0.123456"))

    def test_migration_confirmation_does_not_reset_facts_or_cashflows(self):
        other = Service(Path(self.tmp.name) / "adopt.sqlite", config())
        try:
            other.initialize(snapshot())
            other.record(
                dict(
                    kind="cashflow",
                    event_id="IN",
                    amount="10",
                    source="fixture",
                    confirmed=True,
                ),
                NOW,
            )
            event = dict(
                kind="migration_confirmation",
                confirmed=True,
                phase="QQQ_ONLY",
                snapshot=snapshot(cash="210"),
                recovery_eligible=False,
            )
            self.assertEqual(other.record(event, NOW)["status"], "MIGRATION_CONFIRMED")
            self.assertEqual(other.status()["cash"], "210")
            self.assertEqual(other.status()["phase"], "QQQ_ONLY")
        finally:
            other.close()

    def test_publication_checks_freshness_config_and_stop_again(self):
        plan = self.ready()["plan"]
        later = NOW + timedelta(seconds=31)
        self.assertEqual(
            self.s.record(dict(kind="publication_start", plan_id=plan["id"]), later)[
                "reason"
            ],
            "REPREPARE_FRESH_QUOTES",
        )
        fresh = self.s.prepare(snapshot(at=later), quotes(later), later)
        self.assertEqual(fresh["plan"]["id"], plan["id"])
        self.assertEqual(
            self.s.record(dict(kind="publication_start", plan_id=plan["id"]), later)[
                "status"
            ],
            "PUBLISHING",
        )

    def test_shadow_plan_never_claims_publication(self):
        self.s.config["mode"] = "SHADOW"
        plan = self.ready()
        self.assertEqual(plan["status"], "SHADOW_PLAN")
        self.assertEqual(
            self.s.record(
                dict(kind="publication_start", plan_id=plan["plan"]["id"]), NOW
            )["reason"],
            "SHADOW_CANNOT_PUBLISH",
        )

    def test_complete_sell_then_buy_and_actual_phase_transition(self):
        self.s.evaluate(signal(), NOW)
        snap = snapshot()
        for i in range(8):
            result = self.s.prepare(snap, quotes(), NOW)
            if result["status"] == "NO_ACTION":
                break
            self.assertEqual(result["status"], "PLAN_READY", result)
            payload = self.publish_bind(result, order=str(i))
            q = D(payload["quantity"])
            symbol = result["plan"]["symbol"]
            p = D(payload["limit_price"])
            delta = q if payload["side"] == "BUY" else -q
            snap["positions"][symbol] = str(D(snap["positions"][symbol]) + delta)
            snap["cash"] = str(D(snap["cash"]) - delta * p - D(".35"))
            snap["settled_available_usd"] = snap["cash"]
            fill = dict(
                trade_id="F" + str(i),
                order_id=str(i),
                symbol=symbol,
                side=payload["side"],
                quantity=str(q),
                price=str(p),
                commission=".35",
            )
            rec = self.s.reconcile(
                dict(
                    snapshot=snap,
                    fills=[fill],
                    orders=[dict(order_id=str(i), status="FILLED")],
                ),
                NOW,
            )
            self.assertEqual(rec["status"], "RECONCILED", rec)
        else:
            self.fail("目标不能陷入反复微调")
        self.assertEqual(self.s.status()["phase"], "MIXED")
        self.assertGreaterEqual(D(snap["cash"]), D("200"))
        self.assertGreater(D(snap["positions"]["TQQQ"]), 0)

    def test_buyer_waits_for_settled_funds_and_risk_goal_is_retained(self):
        self.s.evaluate(signal(), NOW)
        # 合成一组已经清掉QQQ、等待买入的事实，仍不能用购买力替代结算资金。
        st = self.s.state()
        st["positions"]["QQQ"] = "0"
        st["cash"] = "10000"
        self.s.save(st)
        snap = snapshot({"QQQ": "0", "TQQQ": "0", "SGOV": "0"}, "10000")
        snap["settled_available_usd"] = "0"
        self.assertEqual(self.s.prepare(snap, quotes(), NOW)["status"], "WAIT_FUNDS")

    def test_publication_absence_requires_explicit_no_order_evidence(self):
        p = self.ready()["plan"]
        self.s.record(dict(kind="publication_start", plan_id=p["id"]), NOW)
        self.assertEqual(
            self.s.record(dict(kind="instruction_absent", plan_id=p["id"]), NOW)[
                "status"
            ],
            "BLOCKED",
        )
        result = self.s.record(
            dict(
                kind="instruction_absent", plan_id=p["id"], user_confirmed_no_order=True
            ),
            NOW,
        )
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(
            self.s.prepare(snapshot(), quotes(), NOW)["status"], "PLAN_READY"
        )

    def test_cli_status_and_invalid_json_have_machine_results(self):
        run = subprocess.run(
            [sys.executable, "-m", "trading_system", "--db", str(self.path), "status"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["phase"], "QQQ_ONLY")
        source = Path(self.tmp.name) / "bad.json"
        source.write_text("{}")
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "trading_system",
                "--db",
                str(self.path),
                "--input",
                str(source),
                "initialize",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 2)
        self.assertEqual(json.loads(run.stdout)["status"], "INVALID_INPUT")

    def test_new_risk_signal_blocks_unknown_old_publication_then_replaces(self):
        p = self.ready()["plan"]
        self.s.record(dict(kind="publication_start", plan_id=p["id"]), NOW)
        nextday = NOW + timedelta(days=1)
        frame = signal()
        frame["bars"] = frame["bars"][1:] + [
            dict(date="2026-10-26", close="90", complete=True)
        ]
        frame["close"]["date"] = "2026-10-26"
        frame["close"]["marks"]["QQQ"] = "90"
        self.assertEqual(
            self.s.evaluate(frame, nextday)["reason"],
            "SIGNAL_CHANGED_REQUIRES_ORDER_RESOLUTION",
        )
        self.assertEqual(
            self.s.prepare(snapshot(at=nextday), quotes(nextday), nextday)["status"],
            "BLOCKED",
        )
        self.s.record(
            dict(
                kind="instruction_absent", plan_id=p["id"], user_confirmed_no_order=True
            ),
            nextday,
        )
        self.assertEqual(
            self.s.evaluate(frame, nextday)["decision"]["phase"], "DEFENSIVE"
        )
        self.assertEqual(
            D(
                self.s.prepare(snapshot(at=nextday), quotes(nextday), nextday)["plan"][
                    "payload"
                ]["quantity"]
            ),
            D("98"),
        )

    def test_defense_cycle_then_partial_recovery_uses_boost_not_mixed(self):
        self.s.close()
        self.path = Path(self.tmp.name) / "cycle.sqlite"
        self.s = Service(self.path, config())
        snap = snapshot({"QQQ": "40", "TQQQ": "120", "SGOV": "0"}, "200")
        self.s.initialize(snap, "MIXED", True)
        frame = signal()
        frame["bars"][-1]["close"] = "90"
        frame["close"]["marks"]["QQQ"] = "90"
        frame["close"]["positions"] = deepcopy(snap["positions"])
        self.s.evaluate(frame, NOW)
        for i in range(6):
            result = self.s.prepare(snap, quotes(), NOW)
            if result["status"] == "NO_ACTION":
                break
            payload = self.publish_bind(result, order="D" + str(i))
            q = D(payload["quantity"])
            p = D(payload["limit_price"])
            symbol = result["plan"]["symbol"]
            delta = q if payload["side"] == "BUY" else -q
            snap["positions"][symbol] = str(D(snap["positions"][symbol]) + delta)
            snap["cash"] = str(D(snap["cash"]) - delta * p - D(".35"))
            snap["settled_available_usd"] = snap["cash"]
            fill = dict(
                trade_id="D" + str(i),
                order_id="D" + str(i),
                symbol=symbol,
                side=payload["side"],
                quantity=str(q),
                price=str(p),
                commission=".35",
            )
            self.s.reconcile(
                dict(
                    snapshot=snap,
                    fills=[fill],
                    orders=[dict(order_id="D" + str(i), status="FILLED")],
                ),
                NOW,
            )
        self.assertEqual(self.s.status()["phase"], "DEFENSIVE")
        self.assertTrue(self.s.status()["recovery_eligible"])
        self.assertGreater(D(snap["positions"]["SGOV"]), 0)
        nextday = NOW + timedelta(days=1)
        snap["at"] = nextday.isoformat()
        frame = signal()
        frame["bars"] = frame["bars"][1:] + [
            dict(date="2026-10-26", close="111", complete=True)
        ]
        frame["close"].update(
            date="2026-10-26", positions=deepcopy(snap["positions"]), cash=snap["cash"]
        )
        frame["close"]["marks"]["QQQ"] = "111"
        self.assertEqual(self.s.evaluate(frame, nextday)["decision"]["phase"], "BOOST")
        result = self.s.prepare(snap, quotes(nextday), nextday)
        payload = self.publish_bind(result, order="RS", now=nextday)
        q = D(payload["quantity"])
        p = D(payload["limit_price"])
        snap["positions"]["SGOV"] = "0"
        snap["cash"] = str(D(snap["cash"]) + q * p - D(".35"))
        snap["settled_available_usd"] = snap["cash"]
        fill = dict(
            trade_id="RS",
            order_id="RS",
            symbol="SGOV",
            side="SELL",
            quantity=str(q),
            price=str(p),
            commission=".35",
        )
        self.s.reconcile(
            dict(
                snapshot=snap,
                fills=[fill],
                orders=[dict(order_id="RS", status="FILLED")],
            ),
            nextday,
        )
        result = self.s.prepare(snap, quotes(nextday), nextday)
        payload = self.publish_bind(result, order="RB", now=nextday)
        self.assertEqual(payload["side"], "BUY")
        self.assertEqual(result["plan"]["symbol"], "TQQQ")
        snap["positions"]["TQQQ"] = "1.5"
        snap["cash"] = str(
            D(snap["cash"]) - D("1.5") * D(payload["limit_price"]) - D(".35")
        )
        snap["settled_available_usd"] = snap["cash"]
        fill = dict(
            trade_id="RB",
            order_id="RB",
            symbol="TQQQ",
            side="BUY",
            quantity="1.5",
            price=payload["limit_price"],
            commission=".35",
        )
        self.s.reconcile(
            dict(
                snapshot=snap,
                fills=[fill],
                orders=[dict(order_id="RB", status="PARTIALLY_FILLED")],
            ),
            nextday,
        )
        self.assertEqual(self.s.status()["phase"], "BOOST")
        self.assertFalse(self.s.status()["recovery_eligible"])
        self.assertEqual(
            self.s.prepare(snap, quotes(nextday), nextday)["status"], "WAIT_FILL"
        )

    def test_cli_commands_use_json_and_review_clock_override_is_rejected(self):
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps(config()))
        source = Path(self.tmp.name) / "signal.json"
        source.write_text(json.dumps(signal()))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(
                [
                    "--db",
                    str(self.path),
                    "--config",
                    str(cfg),
                    "--now",
                    NOW.isoformat(),
                    "--input",
                    str(source),
                    "evaluate",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(out.getvalue())["reason"], "CLOCK_OVERRIDE_SHADOW_ONLY"
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["--db", str(self.path), "status"])
        self.assertEqual(code, 0)

    def test_duplicate_ids_bad_config_and_pending_risk_do_not_silently_reset(self):
        with self.assertRaises(InvalidInput):
            Service(Path(self.tmp.name) / "bad.sqlite", {"mode": "AUTO"})
        with self.assertRaises(InvalidInput):
            self.s.initialize(snapshot(), "QQQ_ONLY", True)
        frame = signal()
        frame["bars"][-1]["close"] = "90"
        frame["close"]["marks"]["QQQ"] = "90"
        self.s.evaluate(frame, NOW)
        nextday = NOW + timedelta(days=1)
        new = signal()
        new["bars"] = new["bars"][1:] + [
            dict(date="2026-10-26", close="111", complete=True)
        ]
        new["close"]["date"] = "2026-10-26"
        new["close"]["marks"]["QQQ"] = "111"
        self.assertEqual(self.s.evaluate(new, nextday)["reason"], "PENDING_RISK_GOAL")
        self.assertEqual(self.s.status()["goals"][0]["phase"], "DEFENSIVE")

    def test_backup_restoration_requires_fresh_reconciliation(self):
        dest = Path(self.tmp.name) / "backup.sqlite"
        self.assertEqual(self.s.backup(dest)["status"], "BACKUP_CREATED")
        restored = Service(dest, config())
        try:
            restored.evaluate(signal(), NOW)
            self.assertEqual(
                restored.prepare(snapshot(), quotes(), NOW)["status"], "BLOCKED"
            )
            restored.reconcile(dict(snapshot=snapshot(), fills=[], orders=[]), NOW)
            self.assertEqual(
                restored.prepare(snapshot(), quotes(), NOW)["status"], "PLAN_READY"
            )
        finally:
            restored.close()

    def test_replaying_terminal_binding_does_not_reopen_order(self):
        p = self.ready()
        payload = self.publish_bind(p)
        self.s.reconcile(
            dict(
                snapshot=snapshot(),
                fills=[],
                orders=[dict(order_id="O1", status="CANCELLED")],
            ),
            NOW,
        )
        event = dict(
            kind="order_binding",
            instruction_id="IO1",
            order_id="O1",
            user_confirmed=True,
            binding_ref="SYNTHETIC",
            symbol=p["plan"]["symbol"],
            side=payload["side"],
            quantity=payload["quantity"],
        )
        self.s.record(event, NOW)
        self.assertEqual(self.s.store.get("order", "O1")["status"], "CANCELLED")

    def test_publication_checks_actual_quote_time_not_validation_time(self):
        self.s.evaluate(signal(), NOW)
        plan = self.s.prepare(snapshot(), quotes(NOW - timedelta(seconds=29)), NOW)[
            "plan"
        ]
        result = self.s.record(
            dict(kind="publication_start", plan_id=plan["id"]),
            NOW + timedelta(seconds=2),
        )
        self.assertEqual(result["reason"], "REPREPARE_FRESH_QUOTES")

    def test_cli_synthetic_walkthrough_and_backup(self):
        root = Path("examples/assisted")
        db = str(Path(self.tmp.name) / "demo.sqlite")
        base = ["--db", db, "--config", str(root / "shadow-config.json")]
        for command, name, status in [
            ("initialize", "bootstrap", "INITIALIZED"),
            ("evaluate", "signal", "DECISION_READY"),
            ("prepare", "prepare", "SHADOW_PLAN"),
        ]:
            argv = base + [
                "--input",
                str(root / (name + ".json")),
                "--now",
                NOW.isoformat(),
                command,
            ]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(argv)
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(json.loads(out.getvalue())["status"], status)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(
                base
                + ["--output", str(Path(self.tmp.name) / "cli-backup.sqlite"), "backup"]
            )
        self.assertEqual(code, 0, out.getvalue())

    def test_drift_risk_reduction_does_not_require_new_entry_filter(self):
        self.s.close()
        self.path = Path(self.tmp.name) / "drift.sqlite"
        self.s = Service(self.path, config())
        snap = snapshot({"QQQ": "10", "TQQQ": "400", "SGOV": "0"}, "200")
        self.s.initialize(snap, "MIXED", True)
        frame = signal()
        for bar in frame["bars"]:
            bar["close"] = "100"
        frame["close"].update(positions=deepcopy(snap["positions"]), cash="200")
        frame["close"]["marks"]["QQQ"] = "100"
        result = self.s.evaluate(frame, NOW)
        self.assertEqual(result["decision"]["reason"], "DRIFT_CAP")
        result = self.s.prepare(snap, quotes(), NOW)
        self.assertEqual(result["status"], "PLAN_READY", result)
        self.assertEqual(result["plan"]["symbol"], "TQQQ")
        self.assertEqual(result["plan"]["payload"]["side"], "SELL")

    def test_missing_commission_is_pending_and_zero_later_confirms_it(self):
        plan = self.ready()
        self.publish_bind(plan)
        fill = dict(
            trade_id="P",
            order_id="O1",
            symbol="QQQ",
            side="SELL",
            quantity="1",
            price="100",
        )
        snap = snapshot({"QQQ": "97", "TQQQ": "0", "SGOV": "0"}, "300")
        event = dict(
            snapshot=snap,
            fills=[fill],
            orders=[dict(order_id="O1", status="PARTIALLY_FILLED")],
        )
        self.s.reconcile(event, NOW)
        self.assertEqual(self.s.status()["pending_commissions"], ["P"])
        fill["commission"] = "0"
        self.s.reconcile(event, NOW)
        self.assertEqual(self.s.status()["pending_commissions"], [])

    def test_submitted_unbound_order_is_reported_for_binding(self):
        p = self.ready()["plan"]
        self.s.record(dict(kind="publication_start", plan_id=p["id"]), NOW)
        self.s.record(
            dict(
                kind="instruction_receipt",
                plan_id=p["id"],
                instruction_id="I",
                payload=p["payload"],
            ),
            NOW,
        )
        snap = snapshot()
        snap["orders"] = [dict(order_id="UNBOUND")]
        self.assertEqual(self.s.prepare(snap, quotes(), NOW)["status"], "NEEDS_BINDING")

    def test_cash_below_reserve_does_not_create_zero_target_liquidation(self):
        self.s.evaluate(signal(), NOW)
        self.s.config["fee_cap_per_order"] = "100000"
        self.assertEqual(
            self.s.prepare(snapshot(), quotes(), NOW)["status"], "WAIT_FUNDS"
        )


if __name__ == "__main__":
    unittest.main()
