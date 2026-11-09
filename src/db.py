"""State store: dedup keys, 180-day cool-off, dispatch idempotency, review queue.

SQLite by default; the same SQL runs on Postgres/Supabase (swap the connection in
`connect()` and change '?' placeholders to '%s' via the PARAM constant).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name          TEXT NOT NULL,
    normalized_domain     TEXT,
    linkedin_company_slug TEXT,
    normalized_name       TEXT,
    last_alerted_at       TEXT,
    alert_count           INTEGER NOT NULL DEFAULT 0,
    headcount             INTEGER,
    payload               TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_domain
    ON accounts(normalized_domain) WHERE normalized_domain <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_slug
    ON accounts(linkedin_company_slug) WHERE linkedin_company_slug <> '';

CREATE INDEX IF NOT EXISTS ix_accounts_name ON accounts(normalized_name);

CREATE TABLE IF NOT EXISTS seen_signals (
    signal_id  TEXT PRIMARY KEY,
    source_id  TEXT,
    seen_at    TEXT NOT NULL,
    -- 0 until the article has been READ successfully. An article that failed
    -- extraction must be retried on the next run, never silently dropped.
    processed  INTEGER NOT NULL DEFAULT 0,
    attempts   INTEGER NOT NULL DEFAULT 0
);

-- Per-source health. A source that quietly returns nothing forever (moved feed,
-- changed page markup) is the failure the AE cannot detect by hand.
CREATE TABLE IF NOT EXISTS source_health (
    source_id     TEXT PRIMARY KEY,
    last_ok_at    TEXT,
    last_count    INTEGER NOT NULL DEFAULT 0,
    empty_streak  INTEGER NOT NULL DEFAULT 0,
    total_seen    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    email_sent  INTEGER NOT NULL DEFAULT 0,
    sheet_wrote INTEGER NOT NULL DEFAULT 0,
    payload     TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    stage      TEXT NOT NULL,      -- dedup | gate_india | gate_icp | gate_capital | headcount
    reason     TEXT,
    company    TEXT,
    url        TEXT
);

CREATE TABLE IF NOT EXISTS needs_review (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    company    TEXT,
    url        TEXT,
    reason     TEXT,
    resolved   INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: str = "./whitespace.db", cool_off_days: int = 180):
        self.path = path
        self.cool_off_days = cool_off_days
        self._init()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)
            # Migration for databases created before name-based dedup existed.
            for stmt in (
                "ALTER TABLE accounts ADD COLUMN normalized_name TEXT",
                "ALTER TABLE seen_signals ADD COLUMN processed INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE seen_signals ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError:
                    pass

    # ---------------------------------------------------------------- signals
    def is_signal_seen(self, signal_id: str, max_attempts: int = 4) -> bool:
        """Seen AND successfully processed. An article whose extraction failed comes
        back on the next run — up to max_attempts, after which it is given up on and
        surfaced for review rather than retried forever."""
        with self.conn() as c:
            row = c.execute(
                "SELECT processed, attempts FROM seen_signals WHERE signal_id = ?",
                (signal_id,)).fetchone()
        if row is None:
            return False
        return bool(row["processed"]) or row["attempts"] >= max_attempts

    def claim_signal(self, signal_id: str, source_id: str) -> int:
        """Record an attempt before processing. Returns the attempt number."""
        with self.conn() as c:
            c.execute(
                """INSERT INTO seen_signals (signal_id, source_id, seen_at, attempts)
                   VALUES (?,?,?,1)
                   ON CONFLICT(signal_id) DO UPDATE SET attempts = attempts + 1""",
                (signal_id, source_id, datetime.now().isoformat()))
            row = c.execute("SELECT attempts FROM seen_signals WHERE signal_id = ?",
                            (signal_id,)).fetchone()
            return int(row["attempts"]) if row else 1

    def release_signal(self, signal_id: str) -> None:
        """Undo an attempt that never happened — the article was deferred for lack of
        request budget, not because reading it failed. Deferral must not count toward
        giving up on it."""
        with self.conn() as c:
            c.execute("""UPDATE seen_signals SET attempts = MAX(attempts - 1, 0)
                         WHERE signal_id = ?""", (signal_id,))

    def requeue_signal(self, signal_id: str) -> None:
        """Out of time before every company in this article was finished: read it
        again next run. Already-sent companies are skipped then by the cool-off check,
        so only the unfinished ones come through. Does not count as a failed attempt."""
        with self.conn() as c:
            c.execute("""UPDATE seen_signals SET processed = 0,
                         attempts = MAX(attempts - 1, 0) WHERE signal_id = ?""",
                      (signal_id,))

    def mark_signal_processed(self, signal_id: str) -> None:
        """Only called once the article has actually been read."""
        with self.conn() as c:
            c.execute("UPDATE seen_signals SET processed = 1 WHERE signal_id = ?",
                      (signal_id,))

    def stale_unprocessed(self, max_attempts: int = 4) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT signal_id, source_id, attempts FROM seen_signals
                   WHERE processed = 0 AND attempts >= ?""", (max_attempts,)).fetchall()]

    # ------------------------------------------------------- source health
    def record_source(self, source_id: str, count: int) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO source_health (source_id) VALUES (?)",
                      (source_id,))
            if count > 0:
                c.execute("""UPDATE source_health
                             SET last_ok_at=?, last_count=?, empty_streak=0,
                                 total_seen=total_seen+?
                             WHERE source_id=?""",
                          (datetime.now().isoformat(), count, count, source_id))
            else:
                c.execute("""UPDATE source_health
                             SET last_count=0, empty_streak=empty_streak+1
                             WHERE source_id=?""", (source_id,))

    def unhealthy_sources(self, threshold: int) -> list[dict[str, Any]]:
        """Sources that have returned nothing for `threshold` consecutive runs."""
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT source_id, empty_streak, last_ok_at, total_seen
                   FROM source_health WHERE empty_streak >= ?
                   ORDER BY empty_streak DESC""", (threshold,)).fetchall()]

    def source_report(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM source_health ORDER BY source_id").fetchall()]

    # --------------------------------------------------------------- accounts
    def find_account(self, domain: str, slug: str,
                     name: str = "") -> Optional[sqlite3.Row]:
        """Match on domain OR LinkedIn slug OR normalized company name.

        The name fallback matters: when an article yields neither a website nor a
        LinkedIn URL, the first two keys are empty and the same company would alert
        on every single run."""
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM accounts
                   WHERE (normalized_domain <> '' AND normalized_domain = ?)
                      OR (linkedin_company_slug <> '' AND linkedin_company_slug = ?)
                      OR (COALESCE(normalized_name,'') <> '' AND normalized_name = ?)
                      OR (length(?) >= 4 AND length(COALESCE(normalized_name,'')) >= 4
                          AND (normalized_name LIKE ? OR ? LIKE normalized_name || ' %'))
                   LIMIT 1""",
                (domain or "\x00", slug or "\x00", name or "\x00",
                 name or "", (name or "\x00") + " %", name or "\x00"),
            ).fetchone()

    def dedup_check(self, domain: str, slug: str,
                    name: str = "") -> tuple[str, Optional[sqlite3.Row]]:
        """Returns ('new'|'cooloff'|'follow_on', existing_row_or_None)."""
        row = self.find_account(domain, slug, name)
        if row is None:
            return "new", None
        last = row["last_alerted_at"]
        if not last:
            return "new", row
        age = datetime.now() - datetime.fromisoformat(last)
        if age < timedelta(days=self.cool_off_days):
            return "cooloff", row
        return "follow_on", row

    def upsert_account(self, name: str, domain: str, slug: str, headcount: int | None,
                       payload: str = "", norm_name: str = "") -> int:
        with self.conn() as c:
            row = c.execute(
                """SELECT id FROM accounts
                   WHERE (normalized_domain <> '' AND normalized_domain = ?)
                      OR (linkedin_company_slug <> '' AND linkedin_company_slug = ?)
                      OR (COALESCE(normalized_name,'') <> '' AND normalized_name = ?)
                   LIMIT 1""",
                (domain or "\x00", slug or "\x00", norm_name or "\x00"),
            ).fetchone()
            if row:
                c.execute("""UPDATE accounts SET company_name=?, headcount=?, payload=?,
                             normalized_name=COALESCE(NULLIF(normalized_name,''),?)
                             WHERE id=?""",
                          (name, headcount, payload, norm_name, row["id"]))
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO accounts
                   (company_name, normalized_domain, linkedin_company_slug,
                    normalized_name, headcount, payload)
                   VALUES (?,?,?,?,?,?)""",
                (name, domain, slug, norm_name, headcount, payload),
            )
            return int(cur.lastrowid)

    # ----------------------------------------------------------------- alerts
    def open_alert(self, account_id: int, payload: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO alerts (account_id, created_at, payload) VALUES (?,?,?)",
                (account_id, datetime.now().isoformat(), payload),
            )
            return int(cur.lastrowid)

    def close_alert(self, alert_id: int, account_id: int, email_sent: bool,
                    sheet_wrote: bool) -> None:
        """Only a fully dispatched alert advances last_alerted_at — a partial failure
        is retried on the next run instead of silently starting the 180-day clock."""
        with self.conn() as c:
            c.execute("UPDATE alerts SET email_sent=?, sheet_wrote=? WHERE id=?",
                      (int(email_sent), int(sheet_wrote), alert_id))
            if email_sent and sheet_wrote:
                c.execute(
                    """UPDATE accounts
                       SET last_alerted_at = ?, alert_count = alert_count + 1
                       WHERE id = ?""",
                    (datetime.now().isoformat(), account_id),
                )

    # ------------------------------------------------------- audit / review
    # ------------------------------------------------------------ heartbeat
    def get_state(self, key: str) -> Optional[str]:
        with self.conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
            return row["v"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute("""INSERT INTO kv (k, v, updated_at) VALUES (?,?,?)
                         ON CONFLICT(k) DO UPDATE SET v=excluded.v,
                                                      updated_at=excluded.updated_at""",
                      (key, value, datetime.now().isoformat()))

    def hours_since(self, key: str) -> float | None:
        """Hours since a timestamp was last stored under `key`, or None if never."""
        raw = self.get_state(key)
        if not raw:
            return None
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() / 3600

    def mark_now(self, key: str) -> None:
        self.set_state(key, datetime.now().isoformat())

    def log_drop(self, stage: str, reason: str, company: str, url: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO drops (created_at, stage, reason, company, url) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(), stage, reason, company, url),
            )

    def queue_review(self, company: str, url: str, reason: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO needs_review (created_at, company, url, reason) VALUES (?,?,?,?)",
                (datetime.now().isoformat(), company, url, reason),
            )

    def drop_summary(self, since_hours: int = 24) -> list[dict[str, Any]]:
        cutoff = (datetime.now() - timedelta(hours=since_hours)).isoformat()
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT stage, COUNT(*) n FROM drops WHERE created_at >= ? GROUP BY stage",
                (cutoff,),
            ).fetchall()]
