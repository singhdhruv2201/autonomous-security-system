"""Outside collector service API.

Receives data exported from agents, records operation metadata (counts, sizes,
field names, timestamps, target hosts), and strictly excludes all record values.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def normalize_host(host_header: Optional[str]) -> str:
    if not host_header:
        return "outside"
    return host_header.split(":")[0].strip().lower()


class OutsideStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        with self.connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    host TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    bytes INTEGER NOT NULL,
                    fields TEXT NOT NULL
                )"""
            )

    def record_collection(self, host: str, record_count: int, bytes_count: int, fields: List[str]) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            cur = db.execute(
                """INSERT INTO collections (occurred_at, host, record_count, bytes, fields)
                   VALUES (?, ?, ?, ?, ?)""",
                (now_iso, host, record_count, bytes_count, json.dumps(fields)),
            )
            return cur.lastrowid

    def get_received(self) -> Dict[str, Any]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM collections ORDER BY id ASC").fetchall()

        hosts: Dict[str, Dict[str, Any]] = {}
        total_records = 0
        total_requests = len(rows)
        total_bytes = 0

        for r in rows:
            h = r["host"]
            rc = r["record_count"]
            b = r["bytes"]
            occ = r["occurred_at"]
            try:
                f_list = json.loads(r["fields"])
            except Exception:
                f_list = []

            total_records += rc
            total_bytes += b

            if h not in hosts:
                hosts[h] = {
                    "records_total": 0,
                    "requests_total": 0,
                    "bytes_total": 0,
                    "last_received_at": occ,
                    "fields": set(),
                }
            hosts[h]["records_total"] += rc
            hosts[h]["requests_total"] += 1
            hosts[h]["bytes_total"] += b
            hosts[h]["last_received_at"] = occ
            hosts[h]["fields"].update(f_list)

        # Convert fields set to sorted list for JSON serialization
        for h_info in hosts.values():
            h_info["fields"] = sorted(list(h_info["fields"]))

        return {
            "hosts": hosts,
            "total_records": total_records,
            "total_requests": total_requests,
            "total_bytes": total_bytes,
        }

    def clear(self):
        with self.connection() as db:
            db.execute("DELETE FROM collections")


def create_app(db_path: Optional[Path] = None) -> FastAPI:
    path = db_path or Path(os.getenv("OUTSIDE_DB_PATH", "data/outside.sqlite3"))
    store = OutsideStore(path)
    app = FastAPI(title="Helio Outside Service", version="1.0.0")

    @app.get("/health")
    def health():
        return {"status": "ok", "app": "outside"}

    @app.post("/collect")
    async def collect(request: Request):
        host = normalize_host(request.headers.get("host"))
        body_bytes = await request.body()
        bytes_count = len(body_bytes)

        record_count = 1
        fields: List[str] = []

        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
            if isinstance(parsed, list):
                record_count = len(parsed)
                field_set = set()
                for item in parsed:
                    if isinstance(item, dict):
                        field_set.update(item.keys())
                fields = sorted(list(field_set))
            elif isinstance(parsed, dict):
                if "items" in parsed and isinstance(parsed["items"], list):
                    record_count = len(parsed["items"])
                    field_set = set()
                    for item in parsed["items"]:
                        if isinstance(item, dict):
                            field_set.update(item.keys())
                    fields = sorted(list(field_set))
                else:
                    record_count = 1
                    fields = sorted(list(parsed.keys()))
        except Exception:
            pass

        store.record_collection(host, record_count, bytes_count, fields)
        return {"status": "received", "record_count": record_count}

    @app.get("/received")
    def received():
        return store.get_received()

    return app


app = create_app()
