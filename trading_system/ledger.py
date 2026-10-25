"""单写入、事务性JSON账本；事实表的唯一键由业务层严格校验。"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .market import InvalidInput


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.lock = open(str(self.path) + ".lock", "a")
        os.chmod(self.lock.name, 0o600)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=1)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS objects (kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id))"
        )
        schema = self.get("meta", "schema")
        if schema is not None and schema != {"version": 1}:
            self.close()
            raise InvalidInput("SCHEMA_VERSION_MISMATCH")
        self.put("meta", "schema", {"version": 1})

    @contextmanager
    def transaction(self):
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InvalidInput("WRITER_LOCKED") from exc
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def get(self, kind, key):
        row = self.db.execute(
            "SELECT body FROM objects WHERE kind=? AND id=?", (kind, str(key))
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, body):
        self.db.execute(
            "INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
            (kind, str(key), encode(body)),
        )

    def all(self, kind):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT body FROM objects WHERE kind=? ORDER BY rowid", (kind,)
            )
        ]

    def backup(self, destination):
        destination = Path(destination)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
            row = target.execute(
                "SELECT body FROM objects WHERE kind='state' AND id='current'"
            ).fetchone()
            if row:
                state = json.loads(row[0])
                state["reconcile_required"] = True
                target.execute(
                    "UPDATE objects SET body=? WHERE kind='state' AND id='current'",
                    (encode(state),),
                )
            target.commit()
        finally:
            target.close()

    def close(self):
        self.db.close()
        self.lock.close()
