import json
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .models import fingerprint


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, source_path TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run_id TEXT, at TEXT, stage TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS receipts (identity TEXT PRIMARY KEY, invoice_hash TEXT, receipt TEXT);
            """)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def start(self, path):
        run_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute("INSERT INTO runs VALUES (?, ?, NULL)", (run_id, str(path)))
        return run_id

    def event(self, run_id, stage, data):
        event = {"stage": stage, "at": datetime.now(UTC).isoformat(), "data": data}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(run_id,at,stage,data) VALUES (?,?,?,?)",
                (run_id, event["at"], stage, json.dumps(data)),
            )
        return event

    def finish(self, run_id, result):
        with self.connect() as conn:
            conn.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), run_id))

    def get_run(self, run_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT source_path,result FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row or not row[1]:
            raise ValueError("Completed run not found")
        return row[0], json.loads(row[1])

    def latest_runs(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT source_path,result FROM runs WHERE result IS NOT NULL ORDER BY rowid"
            ).fetchall()
        return {str(Path(path).resolve()): json.loads(result) for path, result in rows}

    def claim_review(self, run_id, expected):
        """Only one reviewer may act on this exact pending result."""
        claimed = {
            **expected,
            "status": "reviewing",
            "reason": "Review interrupted; reprocess if it does not finish",
        }
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET result=? WHERE id=? AND result=?",
                (json.dumps(claimed), run_id, json.dumps(expected)),
            )
        if cursor.rowcount != 1:
            raise ValueError("This invoice changed while reviewing; refresh its current state")

    def payment(self, invoice, currency, run_id):
        identity = json.dumps(
            [" ".join(invoice.vendor.casefold().split()), invoice.number.strip().casefold()]
        )

        def canonical(value):
            if isinstance(value, Decimal):
                return format(value.normalize(), "f")
            if isinstance(value, dict):
                return {k: canonical(v) for k, v in value.items()}
            if isinstance(value, list):
                return [canonical(v) for v in value]
            return value

        values = invoice.model_dump()
        values["currency"] = currency
        digest = fingerprint(canonical(values))
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT invoice_hash,receipt FROM receipts WHERE identity=?", (identity,)
            ).fetchone()
            if row:
                conn.rollback()
                return (
                    ("duplicate", json.loads(row[1]))
                    if row[0] == digest
                    else (
                        "hold",
                        {"reason": "Invoice identity was already paid with different contents"},
                    )
                )
            receipt = {
                "id": str(uuid.uuid4()),
                "status": "success",
                "mock": True,
                "vendor": invoice.vendor,
                "invoice_number": invoice.number,
                "amount": str(invoice.total),
                "currency": currency,
                "run_id": run_id,
            }
            conn.execute(
                "INSERT INTO receipts VALUES (?,?,?)", (identity, digest, json.dumps(receipt))
            )
            conn.commit()
            return "paid", receipt
        finally:
            conn.close()


def load_inventory(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return dict(conn.execute("SELECT item,stock FROM inventory"))
