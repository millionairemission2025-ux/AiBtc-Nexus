"""SQLite ledger of negotiations, payments and platform fees."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

from .errors import DuplicatePayment

SCHEMA = """
CREATE TABLE IF NOT EXISTS negotiations (
    negotiation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    counterparty TEXT NOT NULL,
    service_id TEXT NOT NULL,
    status TEXT NOT NULL,
    agreed_price_sats INTEGER,
    rounds INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (negotiation_id, role)
);
CREATE TABLE IF NOT EXISTS payments (
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    payment_hash TEXT NOT NULL,
    counterparty TEXT NOT NULL,
    service_id TEXT NOT NULL DEFAULT '',
    amount_sats INTEGER NOT NULL,
    fee_sats INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    preimage TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (direction, payment_hash)
);
CREATE TABLE IF NOT EXISTS platform_fees (
    payment_hash TEXT PRIMARY KEY,
    amount_sats INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accrued', 'settled')),
    settlement_hash TEXT,
    created_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class PaymentRecord:
    direction: str
    payment_hash: str
    counterparty: str
    service_id: str
    amount_sats: int
    fee_sats: int
    status: str
    preimage: str | None
    created_at: float


class Ledger:
    """Thread-safe SQLite ledger. Use ``":memory:"`` for tests."""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self._conn.close()

    # negotiations ------------------------------------------------------
    def record_negotiation(
        self,
        negotiation_id: str,
        role: str,
        counterparty: str,
        service_id: str,
        status: str,
        agreed_price_sats: int | None = None,
        rounds: int = 0,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO negotiations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (negotiation_id, role) DO UPDATE SET
                     status = excluded.status,
                     agreed_price_sats = excluded.agreed_price_sats,
                     rounds = excluded.rounds,
                     updated_at = excluded.updated_at""",
                (
                    negotiation_id,
                    role,
                    counterparty,
                    service_id,
                    status,
                    agreed_price_sats,
                    rounds,
                    now,
                    now,
                ),
            )

    def negotiation_stats(self, role: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM negotiations"
        args: tuple = ()
        if role:
            sql += " WHERE role = ?"
            args = (role,)
        with self._lock:
            rows = self._conn.execute(sql + " GROUP BY status", args).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # payments ----------------------------------------------------------
    def begin_payment(
        self,
        direction: str,
        payment_hash: str,
        counterparty: str,
        service_id: str,
        amount_sats: int,
        reserved_fee_sats: int = 0,
    ) -> None:
        """Insert a pending payment. Raises DuplicatePayment if it is pending or done.

        ``reserved_fee_sats`` counts against spending limits until the real fee is known.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM payments WHERE direction = ? AND payment_hash = ?",
                (direction, payment_hash),
            ).fetchone()
            if row and row["status"] in ("pending", "succeeded"):
                raise DuplicatePayment(f"payment {payment_hash[:16]}… already {row['status']}")
            self._conn.execute(
                """INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                   ON CONFLICT (direction, payment_hash) DO UPDATE SET
                     status = 'pending', amount_sats = excluded.amount_sats,
                     fee_sats = excluded.fee_sats, created_at = excluded.created_at,
                     updated_at = excluded.updated_at""",
                (
                    direction,
                    payment_hash,
                    counterparty,
                    service_id,
                    amount_sats,
                    reserved_fee_sats,
                    now,
                    now,
                ),
            )

    def complete_payment(
        self, direction: str, payment_hash: str, preimage: str, fee_sats: int = 0
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE payments SET status = 'succeeded', preimage = ?, fee_sats = ?,
                   updated_at = ? WHERE direction = ? AND payment_hash = ?""",
                (preimage, fee_sats, time.time(), direction, payment_hash),
            )

    def fail_payment(self, direction: str, payment_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE payments SET status = 'failed', updated_at = ?
                   WHERE direction = ? AND payment_hash = ? AND status = 'pending'""",
                (time.time(), direction, payment_hash),
            )

    def record_incoming(
        self, payment_hash: str, counterparty: str, service_id: str, amount_sats: int, preimage: str
    ) -> bool:
        """Record a settled incoming payment. Returns False if it was already recorded."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO payments
                   VALUES ('in', ?, ?, ?, ?, 0, 'succeeded', ?, ?, ?)""",
                (payment_hash, counterparty, service_id, amount_sats, preimage, now, now),
            )
            return cur.rowcount == 1

    def get_payment(self, direction: str, payment_hash: str) -> PaymentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM payments WHERE direction = ? AND payment_hash = ?",
                (direction, payment_hash),
            ).fetchone()
        if not row:
            return None
        return PaymentRecord(
            row["direction"],
            row["payment_hash"],
            row["counterparty"],
            row["service_id"],
            row["amount_sats"],
            row["fee_sats"],
            row["status"],
            row["preimage"],
            row["created_at"],
        )

    def spent_since(self, since: float, counterparty: str | None = None) -> int:
        """Sats committed to outgoing payments (pending or succeeded) since ``since``,
        including routing fees."""
        sql = """SELECT COALESCE(SUM(amount_sats + fee_sats), 0) AS s FROM payments
                 WHERE direction = 'out' AND status IN ('pending', 'succeeded')
                   AND created_at >= ?"""
        args: list = [since]
        if counterparty is not None:
            sql += " AND counterparty = ?"
            args.append(counterparty)
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()["s"])

    def totals(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT direction, COUNT(*) AS n, COALESCE(SUM(amount_sats), 0) AS amount,
                          COALESCE(SUM(fee_sats), 0) AS fees
                   FROM payments WHERE status = 'succeeded' GROUP BY direction"""
            ).fetchall()
        out = {"out_count": 0, "out_sats": 0, "out_fee_sats": 0, "in_count": 0, "in_sats": 0}
        for r in rows:
            out[f"{r['direction']}_count"] = r["n"]
            out[f"{r['direction']}_sats"] = r["amount"]
            if r["direction"] == "out":
                out["out_fee_sats"] = r["fees"]
        return out

    # platform fees -----------------------------------------------------
    def accrue_fee(self, payment_hash: str, amount_sats: int) -> None:
        if amount_sats <= 0:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO platform_fees VALUES (?, ?, 'accrued', NULL, ?)",
                (payment_hash, amount_sats, time.time()),
            )

    def accrued_fees(self) -> tuple[int, list[str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payment_hash, amount_sats FROM platform_fees WHERE status = 'accrued'"
            ).fetchall()
        return sum(r["amount_sats"] for r in rows), [r["payment_hash"] for r in rows]

    def mark_fees_settled(self, payment_hashes: list[str], settlement_hash: str) -> None:
        with self._lock:
            self._conn.executemany(
                """UPDATE platform_fees SET status = 'settled', settlement_hash = ?
                   WHERE payment_hash = ? AND status = 'accrued'""",
                [(settlement_hash, h) for h in payment_hashes],
            )

    def fee_totals(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, COALESCE(SUM(amount_sats), 0) AS s
                   FROM platform_fees GROUP BY status"""
            ).fetchall()
        out = {"accrued": 0, "settled": 0}
        out.update({r["status"]: r["s"] for r in rows})
        return out
