"""云端调度、原始数据验证和通知恢复的离线测试。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
import subprocess

from cloud_runner.runtime import due_jobs, analyze, deliver, private_json, send_wecom, extract_bundle, raw, backup_state
from trading_system.market import Calendar, InvalidInput
from cloud_runner.__main__ import run_job, main, collect


NOW = datetime(2026, 10, 26, 13, 36, tzinfo=timezone.utc)


def bundle():
    days = Calendar().history("2026-10-23", 201)
    history = {"source": "Last", "time": [d + "T13:30:00Z" for d in days],
               "close": ["100"] * len(days)}
    return {"status": "OK", "collected_at": NOW.isoformat(),
            "history": {"QQQ": history, "TQQQ": history, "SGOV": history},
            "positions": {"positions": [{"contract_id": 320227571, "position": "1"},
                                          {"contract_id": 72539702, "position": ".2"}]},
            "orders": {"orders": []}, "instructions": {"instructions": []},
            "balances": {"balances": [{"currency": "USD", "settled_cash": "50"}]},
            "trades": {"trades": []}, "quotes": {}}


class CloudTests(unittest.TestCase):
    def test_backups_restore_json_and_consistent_sqlite_without_credentials(self):
        import sqlite3
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_json(root / "state/jobs.json", {"job": "SENT"})
            private_json(root / "config/cloud.local.json", {"phase": "MIXED"})
            (root / "config/wecom-webhook").write_text("secret")
            db = sqlite3.connect(root / "state/test.sqlite")
            db.execute("create table facts(value integer)")
            db.execute("insert into facts values(7)")
            db.commit()
            path = backup_state(root, "test")
            with tarfile.open(path) as archive:
                self.assertNotIn("config/wecom-webhook", archive.getnames())
                self.assertEqual(json.load(archive.extractfile("state/jobs.json")), {"job": "SENT"})
                target = root / "restored.sqlite"
                target.write_bytes(archive.extractfile("state/test.sqlite").read())
            restored = sqlite3.connect(target)
            self.assertEqual(restored.execute("select value from facts").fetchone(), (7,))
            restored.close()
            db.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_job_persists_report_never_changes_phase_or_submits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_json(root / "config/cloud.local.json", {"phase": "MIXED"})
            r = run_job(root, "test-1", collector=lambda *_: bundle(), sender=lambda _: {"errcode": 0}, now=NOW)
            self.assertEqual(r["status"], "READ_ONLY_REPORT")
            self.assertEqual(r["delivery"], "SENT")
            report = json.loads((root / "reports/test-1-report.json").read_text())
            self.assertEqual(report["executable_orders"], [])
            self.assertEqual(json.loads((root / "config/cloud.local.json").read_text()), {"phase": "MIXED"})
            self.assertEqual(run_job(root, "test-1", collector=lambda *_: self.fail())["status"], "ALREADY_ATTEMPTED")
            (root / "state/STOP").touch()
            self.assertEqual(run_job(root, "test-2")["status"], "STOPPED")

    def test_job_failure_notification_and_unknown_delivery_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = run_job(root, "bad", collector=lambda *_: (_ for _ in ()).throw(RuntimeError("MISSING_AUTH")), sender=lambda _: {"errcode": 0}, now=NOW)
            self.assertEqual(r["status"], "BLOCKED")
            self.assertEqual(json.loads((root / "reports/bad-report.json").read_text())["reason"], "MISSING_AUTH")
            r = run_job(root, "unknown", collector=lambda *_: (_ for _ in ()).throw(OSError("https://secret")), sender=lambda _: (_ for _ in ()).throw(TimeoutError()), now=NOW)
            self.assertEqual(r["delivery"], "WECOM_DELIVERY_UNKNOWN")
            self.assertNotIn("https://secret", (root / "reports/unknown-report.json").read_text())

    def test_cli_status_notify_tick_invalid_job_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(tmp)
            self.assertEqual(main(["--root", tmp, "status"]), 0)
            with patch("cloud_runner.__main__.send_wecom", return_value={"errcode": 0}):
                self.assertEqual(main(["--root", tmp, "notify-test"]), 0)
                self.assertEqual(main(["--root", tmp, "notify-test"]), 0)
            with patch("cloud_runner.__main__.due_jobs", return_value=[]):
                self.assertEqual(main(["--root", tmp, "tick"]), 0)
            with patch("cloud_runner.__main__.run_job", return_value={"status": "TEST"}) as run:
                main(["--root", tmp, "run", "--job", "safe-1"])
                self.assertEqual(run.call_args.args[1], "safe-1")
            with patch("cloud_runner.__main__.run_job", return_value={"status": "BLOCKED", "delivery": "SENT"}):
                self.assertEqual(main(["--root", tmp, "run", "--job", "failed-1"]), 3)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(["--root", tmp, "run", "--job", "../invalid"])
            with patch("cloud_runner.__main__.fcntl.flock", side_effect=BlockingIOError):
                self.assertEqual(main(["--root", tmp, "status"]), 0)
            self.assertIn("LOCKED", output.getvalue())

    def test_collector_rejects_model_final_even_if_looks_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            (root / "reports").mkdir()
            (root / "app/deploy").mkdir(parents=True)
            (root / "app/deploy/collector-prompt.txt").write_text("read only")
            with patch("cloud_runner.__main__.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaisesRegex(RuntimeError, "CODEX_COLLECTION_FAILED"):
                    collect(root, "bad")
            with patch("cloud_runner.__main__.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
                with self.assertRaisesRegex(InvalidInput, "INCOMPLETE_BROKER_TOOL_TRACE"):
                    collect(root, "empty")
            with patch("cloud_runner.__main__.subprocess.run", return_value=subprocess.CompletedProcess([], 0)), patch("cloud_runner.__main__.extract_bundle", return_value={"status": "OK"}):
                self.assertEqual(collect(root, "ok"), {"status": "OK"})
                self.assertEqual((root / "reports/ok-collection.json").stat().st_mode & 0o777, 0o600)

    def test_raw_trace_not_ai_final_supplies_all_broker_facts(self):
        b = bundle()
        events = []
        names = {"positions": "get_account_positions", "balances": "get_account_balances",
                 "orders": "get_account_orders", "instructions": "get_order_instructions", "trades": "get_account_trades"}
        for key, name in names.items():
            events.append({"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed", "server": "codex_apps", "tool": "interactive_brokers_ibkr." + name, "arguments": {"period": "DAYS_7"} if key == "trades" else {}, "result": {"structured_content": b[key], "content": []}, "error": None}})
        for symbol, conid in (("QQQ", 320227571), ("TQQQ", 72539702), ("SGOV", 424099317)):
            events.append({"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed", "server": "codex_apps", "tool": "interactive_brokers_ibkr.get_price_history", "arguments": {"contract_id": conid, "outside_rth": False, "security_type": "STK", "step": "ONE_DAY", "period": "ONE_YEAR"}, "result": {"structured_content": b["history"][symbol], "content": []}, "error": None}})
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "trace.jsonl"
            p.write_text("\n".join(json.dumps(x) for x in events))
            self.assertEqual({s: raw(v) for s, v in extract_bundle(p, NOW)["history"].items()}, b["history"])
            self.assertEqual(extract_bundle(p, NOW)["trade_request"], {"period": "DAYS_7"})
            events[-1]["item"]["arguments"]["outside_rth"] = True
            p.write_text("\n".join(json.dumps(x) for x in events))
            with self.assertRaises(InvalidInput): extract_bundle(p, NOW)
            events[-1]["item"]["tool"] = "interactive_brokers_ibkr.create_order_instruction"
            p.write_text("\n".join(json.dumps(x) for x in events))
            with self.assertRaises(InvalidInput): extract_bundle(p, NOW)

    def test_calendar_exact_window_dst_early_close_and_holiday(self):
        at = NOW.replace(hour=13, minute=20)
        self.assertEqual(due_jobs(at), ["2026-10-26:preopen"])
        self.assertEqual(due_jobs(NOW), [])
        self.assertEqual(due_jobs(NOW.replace(hour=20)), [])
        self.assertEqual(due_jobs(datetime(2026, 11, 2, 13, 20, tzinfo=timezone.utc)), ["2026-11-02:preopen"])
        self.assertEqual(due_jobs(datetime(2026, 11, 27, 18, 20, tzinfo=timezone.utc)), [])
        self.assertEqual(due_jobs(datetime(2026, 11, 26, 13, 20, tzinfo=timezone.utc)), [])
        self.assertEqual(due_jobs(datetime(2026, 10, 25, 13, 20, tzinfo=timezone.utc)), [])

    def test_missing_or_stale_history_rejected_and_current_bar_excluded(self):
        b = bundle()
        report = analyze(b, NOW, {"phase": "MIXED"})
        self.assertEqual(report["signal_day"], "2026-10-23")
        self.assertIsNone(report["decision"])
        self.assertEqual(report["price_decision"]["reason"], "HOLD")
        self.assertEqual(report["executable_orders"], [])
        b["history"]["QQQ"]["time"].append("2026-10-26T13:30:00Z")
        b["history"]["QQQ"]["close"].append("1")
        self.assertEqual(analyze(b, NOW, {"phase": "MIXED"})["indicators"]["close"], "100")
        b["history"]["QQQ"]["time"].pop(1)
        b["history"]["QQQ"]["close"].pop(1)
        with self.assertRaises(InvalidInput):
            analyze(b, NOW, {"phase": "MIXED"})

    def test_failure_no_positions_currency_and_cash_fabrication(self):
        for key in ("positions", "orders", "instructions", "balances", "trades"):
            b = bundle()
            b.pop(key)
            with self.assertRaises(InvalidInput):
                analyze(b, NOW, {"phase": "MIXED"})
        b = bundle()
        b["collected_at"] = "2026-10-23T12:00:00Z"
        with self.assertRaises(InvalidInput):
            analyze(b, NOW, {"phase": "MIXED"})
        b = bundle()
        b["positions"]["positions"][0]["position"] = "-1"
        with self.assertRaises(InvalidInput):
            analyze(b, NOW, {"phase": "MIXED"})

    def test_signal_priority_orders_conflict_and_drift_uncertainty(self):
        b = bundle()
        b["history"]["QQQ"]["close"][-1] = "90"
        r = analyze(b, NOW, {"phase": "MIXED"})
        self.assertEqual(r["decision"]["reason"], "MA_DEFENSE")
        self.assertIn("ACCOUNT_BINDING_NOT_AUTOMATICALLY_VERIFIED", r["blockers"])
        b = bundle()
        b["orders"]["orders"] = [{"order_id": 1, "order_status": "NEW"}]
        r = analyze(b, NOW, {"phase": "MIXED"})
        self.assertIn("EXISTING_ORDERS_RETAINED", r["blockers"])
        b["positions"]["positions"][1]["position"] = "10"
        self.assertIsNone(analyze(b, NOW, {"phase": "MIXED"})["decision"])

    def test_delivery_success_dedup_unknown_and_retry_business_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deliver(root, "one", "hello", lambda _: {"errcode": 0})
            self.assertEqual(deliver(root, "one", "hello", lambda _: self.fail()), "ALREADY_SENT")
            with self.assertRaises(RuntimeError):
                deliver(root, "two", "hello", lambda _: (_ for _ in ()).throw(TimeoutError()))
            self.assertEqual(deliver(root, "two", "hello", lambda _: self.fail()), "UNKNOWN_DELIVERY")
            with self.assertRaises(RuntimeError):
                deliver(root, "three", "hello", lambda _: {"errcode": 93000})
            self.assertEqual(deliver(root, "three", "hello", lambda _: {"errcode": 0}), "SENT")
            self.assertEqual((root / "deliveries.json").stat().st_mode & 0o777, 0o600)

    def test_private_file_and_wecom_business_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.json"
            private_json(p, {"x": 1})
            self.assertEqual(json.loads(p.read_text()), {"x": 1})
            p.write_text("https://not-wecom.example/?key=secret")
            with self.assertRaises(InvalidInput):
                send_wecom(p, "test")
            p.write_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret")
            class Reply:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return b'{"errcode":0,"errmsg":"ok"}'
            with patch("cloud_runner.runtime.urlopen", return_value=Reply()) as call:
                self.assertEqual(send_wecom(p, "测试")["errcode"], 0)
                self.assertEqual(json.loads(call.call_args.args[0].data)["msgtype"], "text")


if __name__ == "__main__":
    unittest.main()
