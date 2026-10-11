"""SQLite-Speicher: Cache, Kosten, Alerts, Empfehlungen, Signale."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    agent TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'actual',   -- actual | reserved
    ref TEXT                                -- z. B. Batch-ID für Reservierungen
);
CREATE INDEX IF NOT EXISTS idx_llm_costs_day ON llm_costs(day);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    subject TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    text TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts(dedupe_key, ts);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    subject TEXT NOT NULL,
    type TEXT NOT NULL,
    score REAL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    agent_scores TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rec_evaluations (
    rec_id INTEGER NOT NULL,
    horizon_months INTEGER NOT NULL,
    ts TEXT NOT NULL,
    return_pct REAL NOT NULL,
    benchmark_pct REAL NOT NULL,
    PRIMARY KEY (rec_id, horizon_months)
);
CREATE TABLE IF NOT EXISTS agent_weights (
    agent TEXT PRIMARY KEY,
    factor REAL NOT NULL,
    hit_rate REAL,
    samples INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_items (
    key TEXT PRIMARY KEY,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    subject TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    facts_hash TEXT NOT NULL,
    score REAL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    purpose TEXT NOT NULL,
    meta TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    subject TEXT NOT NULL,
    strength REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_subject ON signals(subject, ts);
CREATE TABLE IF NOT EXISTS superinvestor_holdings (
    cik TEXT NOT NULL,
    period TEXT NOT NULL,
    cusip TEXT NOT NULL,
    issuer TEXT NOT NULL,
    ticker TEXT,
    value_usd REAL NOT NULL,
    shares REAL NOT NULL,
    PRIMARY KEY (cik, period, cusip)
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Datenbank nicht verbunden")
        return self._conn

    async def execute(self, sql: str, params: tuple | list = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid or 0

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: tuple | list = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    # ------------------------------------------------------------ Cache
    async def cache_get(self, key: str) -> Any | None:
        row = await self.fetchone("SELECT value, expires_at FROM cache WHERE key = ?", (key,))
        if row is None or row["expires_at"] < time.time():
            return None
        return json.loads(row["value"])

    async def cache_set(self, key: str, value: Any, ttl_seconds: float) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, default=str), time.time() + ttl_seconds),
        )

    async def cache_purge(self) -> None:
        await self.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))

    # ------------------------------------------------------------ Gesehen (Dedupe)
    async def mark_seen(self, key: str) -> bool:
        """True, wenn neu (und jetzt markiert); False, wenn schon gesehen."""
        row = await self.fetchone("SELECT 1 FROM seen_items WHERE key = ?", (key,))
        if row is not None:
            return False
        await self.execute("INSERT INTO seen_items (key, ts) VALUES (?, ?)", (key, utcnow().isoformat()))
        return True

    # ------------------------------------------------------------ Kosten
    async def add_cost(
        self,
        *,
        model: str,
        agent: str,
        cost_usd: float,
        day: date,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        kind: str = "actual",
        ref: str | None = None,
    ) -> None:
        await self.execute(
            """INSERT INTO llm_costs (ts, day, model, agent, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, cost_usd, kind, ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                utcnow().isoformat(),
                day.isoformat(),
                model,
                agent,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                cost_usd,
                kind,
                ref,
            ),
        )

    async def cost_for_day(self, day: date) -> float:
        row = await self.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_costs WHERE day = ?", (day.isoformat(),)
        )
        return float(row["s"]) if row else 0.0

    async def release_reservation(self, ref: str) -> None:
        await self.execute("DELETE FROM llm_costs WHERE kind = 'reserved' AND ref = ?", (ref,))

    async def costs_by_day(self, since: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT day, SUM(cost_usd) AS cost FROM llm_costs WHERE day >= ? GROUP BY day ORDER BY day",
            (since.isoformat(),),
        )

    async def costs_by_agent(self, since: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT agent, model, SUM(cost_usd) AS cost, COUNT(*) AS calls FROM llm_costs "
            "WHERE day >= ? AND kind = 'actual' GROUP BY agent, model ORDER BY cost DESC",
            (since.isoformat(),),
        )

    # ------------------------------------------------------------ Alerts
    async def recently_alerted(self, dedupe_key: str, days: int) -> bool:
        since = utcnow().timestamp() - days * 86400
        since_iso = datetime.fromtimestamp(since, UTC).isoformat()
        row = await self.fetchone(
            "SELECT 1 FROM alerts WHERE dedupe_key = ? AND ts >= ? LIMIT 1", (dedupe_key, since_iso)
        )
        return row is not None

    async def add_alert(self, *, type_: str, subject: str, dedupe_key: str, text: str, sent: bool) -> int:
        return await self.execute(
            "INSERT INTO alerts (ts, type, subject, dedupe_key, text, sent) VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow().isoformat(), type_, subject, dedupe_key, text, int(sent)),
        )

    async def pending_alerts(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM alerts WHERE sent = 0 ORDER BY id")

    async def mark_alert_sent(self, alert_id: int) -> None:
        await self.execute("UPDATE alerts SET sent = 1 WHERE id = ?", (alert_id,))

    # ------------------------------------------------------------ Analysen
    async def save_analysis(self, subject: str, facts_hash: str, score: float | None, payload: dict) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO analyses (subject, ts, facts_hash, score, payload) VALUES (?, ?, ?, ?, ?)",
            (
                subject,
                utcnow().isoformat(),
                facts_hash,
                score,
                json.dumps(payload, default=str, ensure_ascii=False),
            ),
        )

    async def get_analysis(self, subject: str) -> dict | None:
        row = await self.fetchone("SELECT * FROM analyses WHERE subject = ?", (subject,))
        if row is None:
            return None
        return {
            "subject": row["subject"],
            "ts": row["ts"],
            "facts_hash": row["facts_hash"],
            "score": row["score"],
            "payload": json.loads(row["payload"]),
        }

    async def top_analyses(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT subject, ts, score, payload FROM analyses WHERE score IS NOT NULL ORDER BY score DESC LIMIT ?",
            (limit,),
        )

    # ------------------------------------------------------------ Signale
    async def add_signal(self, agent: str, subject: str, strength: float, payload: dict) -> None:
        await self.execute(
            "INSERT INTO signals (ts, agent, subject, strength, payload) VALUES (?, ?, ?, ?, ?)",
            (
                utcnow().isoformat(),
                agent,
                subject,
                strength,
                json.dumps(payload, default=str, ensure_ascii=False),
            ),
        )

    async def recent_signals(self, subject: str | None = None, days: int = 30) -> list[aiosqlite.Row]:
        since = datetime.fromtimestamp(utcnow().timestamp() - days * 86400, UTC).isoformat()
        if subject is None:
            return await self.fetchall("SELECT * FROM signals WHERE ts >= ? ORDER BY ts DESC", (since,))
        return await self.fetchall(
            "SELECT * FROM signals WHERE subject = ? AND ts >= ? ORDER BY ts DESC", (subject, since)
        )

    # ------------------------------------------------------------ Batches
    async def add_batch(self, batch_id: str, purpose: str, meta: dict) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO batches (id, created, status, purpose, meta) VALUES (?, ?, ?, ?, ?)",
            (batch_id, utcnow().isoformat(), "submitted", purpose, json.dumps(meta, default=str)),
        )

    async def open_batches(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM batches WHERE status = 'submitted' ORDER BY created")

    async def set_batch_status(self, batch_id: str, status: str) -> None:
        await self.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch_id))
