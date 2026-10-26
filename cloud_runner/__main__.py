"""systemd 入口：受限 Codex 只读取数，Python 计算并推送。"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from .runtime import analyze, deliver, due_jobs, message_for, private_json, send_wecom, extract_bundle, backup_state


def collect(root, job):
    """私密保存工具轨迹；不把凭据或异常 URL 写到公共输出。"""
    output = root / "reports" / (job.replace(":", "-") + "-collection.json")
    log = root / "logs" / (job.replace(":", "-") + "-codex.jsonl")
    prompt = (root / "app/deploy/collector-prompt.txt").read_text()
    environment = {k: v for k, v in os.environ.items() if k in
                   ("PATH", "LANG", "LC_ALL", "HOME", "SSL_CERT_FILE")}
    environment["CODEX_HOME"] = str(root / "codex-home")
    with log.open("w") as stdout, log.with_suffix(".stderr").open("w") as stderr:
        result = subprocess.run([str(root / "bin/codex"), "exec", "--skip-git-repo-check",
                                 "--sandbox", "read-only", "--json", "-o", str(output), "-"],
                                input=prompt, text=True, cwd=root / "app", env=environment,
                                stdout=stdout, stderr=stderr, timeout=600)
    if result.returncode:
        raise RuntimeError("CODEX_COLLECTION_FAILED")
    bundle = extract_bundle(log, datetime.now(timezone.utc))
    private_json(output, bundle)
    return bundle


def run_job(root, job, collector=collect, sender=None, now=None):
    now = now or datetime.now(timezone.utc)
    state_path = root / "state/jobs.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if job in state:
        return {"status": "ALREADY_ATTEMPTED", "job": job, "previous": state[job]}
    if (root / "state/STOP").exists():
        return {"status": "STOPPED", "job": job}
    state[job] = {"status": "RUNNING", "started_at": now.isoformat()}
    private_json(state_path, state)
    sender = sender or (lambda message: send_wecom(root / "config/wecom-webhook", message))
    try:
        bundle = collector(root, job)
        config = json.loads((root / "config/cloud.local.json").read_text())
        current = now if collector is not collect else datetime.now(timezone.utc)
        report = analyze(bundle, current, config)
        message = message_for(report, job)
        status = "READ_ONLY_REPORT"
    except Exception as exc:
        # 不输出可能包含密钥的 HTTP 异常文字，只保存类别及固定错误码。
        reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) and str(exc).replace("_", "").isalnum() else type(exc).__name__
        report = {"status": "BLOCKED", "reason": reason, "job": job,
                  "computed_at": datetime.now(timezone.utc).isoformat(), "executable_orders": []}
        message = (f"QQQ/TQQQ 云端任务异常\n任务：{job}\n原因：{reason}\n"
                   "本次没有可靠策略结果，没有生成指令或提交订单。请检查云端报告与认证。")
        status = "BLOCKED"
    private_json(root / "reports" / (job.replace(":", "-") + "-report.json"), report)
    delivery = "NOT_SENT"
    try:
        delivery = deliver(root / "state", job, message, sender)
    except RuntimeError as exc:
        delivery = str(exc)
    state[job].update(status=status, delivery=delivery,
                      completed_at=datetime.now(timezone.utc).isoformat())
    private_json(state_path, state)
    backup_state(root, job)
    return {"status": status, "delivery": delivery, "job": job}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("command", choices=("tick", "run", "notify-test", "status"))
    parser.add_argument("--job")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    os.umask(0o077)
    (root / "state").mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "state/cloud.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "LOCKED"}))
            return 0
        now = datetime.now(timezone.utc)
        if args.command == "status":
            p = root / "state/jobs.json"
            result = json.loads(p.read_text()) if p.exists() else {}
        elif args.command == "notify-test":
            result = {"delivery": deliver(root / "state", "deployment-notify-test", "QQQ/TQQQ 云主机部署验收测试：devsin2 已成功通过企业微信接口发送此消息。本消息不包含交易指令。", lambda message: send_wecom(root / "config/wecom-webhook", message))}
        else:
            jobs = [args.job] if args.command == "run" else due_jobs(now)
            if args.command == "run" and (not args.job or not all(c.isalnum() or c in "-_:" for c in args.job)):
                parser.error("--job requires a safe unique name")
            result = [run_job(root, job) for job in jobs]
        print(json.dumps(result, ensure_ascii=False))
        if isinstance(result, list) and any(
            item.get("status") == "BLOCKED" or item.get("delivery", "SENT") not in ("SENT", "ALREADY_SENT")
            for item in result
        ):
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
