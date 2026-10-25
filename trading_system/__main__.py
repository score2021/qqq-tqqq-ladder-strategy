"""JSON文件接口。没有submit、place_order或券商凭证参数。"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .market import InvalidInput, stamp
from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--config")
    parser.add_argument("--input")
    parser.add_argument("--output", help="backup命令的私有目标文件，必须不存在")
    parser.add_argument("--now", help="仅SHADOW可使用的离线重放时间")
    parser.add_argument(
        "command",
        choices=[
            "initialize",
            "evaluate",
            "prepare",
            "record-external",
            "reconcile",
            "status",
            "backup",
        ],
    )
    args = parser.parse_args(argv)
    service = None
    try:
        config = json.loads(Path(args.config).read_text()) if args.config else {}
        if args.now and config.get("mode", "SHADOW") != "SHADOW":
            raise InvalidInput("CLOCK_OVERRIDE_SHADOW_ONLY")
        now = stamp(args.now) if args.now else datetime.now(timezone.utc)
        data = json.loads(Path(args.input).read_text()) if args.input else {}
        service = Service(args.db, config)
        if args.command == "status":
            result = service.status()
        elif args.command == "backup":
            if not args.output:
                raise InvalidInput("BACKUP_OUTPUT_REQUIRED")
            result = service.backup(args.output)
        elif args.command == "initialize":
            result = service.initialize(
                data["snapshot"],
                data.get("phase", "UNINITIALIZED"),
                data.get("confirmed", False),
                data.get("recovery_eligible", False),
            )
        elif args.command == "evaluate":
            result = service.evaluate(data, now)
        elif args.command == "prepare":
            result = service.prepare(data["snapshot"], data["quotes"], now)
        elif args.command == "record-external":
            result = service.record(data, now)
        else:
            result = service.reconcile(data, now)
        code = (
            4
            if result["status"] == "UNKNOWN_PUBLICATION"
            else 3
            if result["status"] in ("BLOCKED", "RECONCILE_REQUIRED")
            else 0
        )
    except (
        InvalidInput,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        sqlite3.Error,
    ) as exc:
        result = dict(status="INVALID_INPUT", reason=str(exc))
        code = 2
    finally:
        if service:
            service.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
