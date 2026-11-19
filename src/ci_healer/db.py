"""SQLite persistence: idempotency, audit log, and the history that powers flaky detection.

Decisions:
- stdlib sqlite3, no ORM. Five tables, a dozen queries — an ORM would hide the SQL you're here
  to learn from.
- One short-lived connection per operation + WAL mode. Webhook processing runs in a thread
  pool; per-call connections sidestep sqlite3's "objects created in a thread can only be used
  in that same thread" rule without a global lock.
- Everything the bot decides is written down (`analyses`, `actions`), including dry-run
  decisions. That table *is* the trust story: you can audit what it would have done before
  letting it do anything.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .models import JobAnalysis, RunContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event       TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    repo          TEXT NOT NULL,
    run_id        INTEGER NOT NULL,
    run_attempt   INTEGER NOT NULL,
    head_sha      TEXT NOT NULL,
    head_branch   TEXT,
    workflow_name TEXT,
    event         TEXT,
    conclusion    TEXT,
    html_url      TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (repo, run_id, run_attempt)
);

CREATE TABLE IF NOT EXISTS analyses (
    id               INTEGER PRIMARY KEY,
    run_pk           INTEGER NOT NULL REFERENCES runs(id),
    job_id           INTEGER,
    job_name         TEXT,
    failed_step      TEXT,
    category         TEXT NOT NULL,
    confidence       REAL,
    source           TEXT,
    summary          TEXT,
    evidence         TEXT,   -- JSON list
    failing_tests    TEXT,   -- JSON list
    implicated_files TEXT,   -- JSON list
    signature        TEXT,
    llm              TEXT,    -- "provider/model" that classified it (NULL/"" = rule)
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analyses_signature ON analyses(signature);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY,
    run_pk     INTEGER NOT NULL REFERENCES runs(id),
    kind       TEXT NOT NULL,     -- rerun | fix_pr | notify | ignore | fix_verified
    status     TEXT NOT NULL,     -- planned | done | skipped | failed
    dry_run    INTEGER NOT NULL,
    reason     TEXT,
    detail     TEXT,              -- JSON
    created_at TEXT NOT NULL
);

-- Per-test outcomes. We only ever learn about tests from failures (their names are in the
-- log) and from the *rerun that passed* (same SHA, so those tests passed this time).
CREATE TABLE IF NOT EXISTS test_outcomes (
    repo        TEXT NOT NULL,
    test_id     TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    run_id      INTEGER NOT NULL,
    run_attempt INTEGER NOT NULL,
    outcome     TEXT NOT NULL,    -- failed | passed
    created_at  TEXT NOT NULL,
    PRIMARY KEY (repo, test_id, run_id, run_attempt)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            # Minimal migration for DBs created before the column existed. (A real project
            # would use numbered migrations; one ALTER doesn't justify a framework yet.)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(analyses)")}
            if "llm" not in cols:
                c.execute("ALTER TABLE analyses ADD COLUMN llm TEXT")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- idempotency ---------------------------------------------------------------------
    def claim_delivery(self, delivery_id: str, event: str) -> bool:
        """Return True the first time we see this delivery GUID, False on redelivery."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)", (delivery_id, event, _now())
            )
            return cur.rowcount == 1

    # --- runs ----------------------------------------------------------------------------
    def upsert_run(self, ctx: RunContext) -> int:
        with self._conn() as c:
            c.execute(
                """INSERT INTO runs (repo, run_id, run_attempt, head_sha, head_branch,
                                     workflow_name, event, conclusion, html_url, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(repo, run_id, run_attempt) DO UPDATE SET conclusion=excluded.conclusion""",
                (ctx.repo, ctx.run_id, ctx.run_attempt, ctx.head_sha, ctx.head_branch,
                 ctx.workflow_name, ctx.event, ctx.conclusion, ctx.html_url, _now()),
            )
            row = c.execute(
                "SELECT id FROM runs WHERE repo=? AND run_id=? AND run_attempt=?",
                (ctx.repo, ctx.run_id, ctx.run_attempt),
            ).fetchone()
            return int(row["id"])

    # --- analyses & actions --------------------------------------------------------------
    def insert_analysis(self, run_pk: int, a: JobAnalysis) -> int:
        cl = a.classification
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO analyses (run_pk, job_id, job_name, failed_step, category, confidence,
                       source, summary, evidence, failing_tests, implicated_files, signature,
                       llm, input_tokens, output_tokens, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_pk, a.job_id, a.job_name, a.failed_step, cl.category.value, cl.confidence,
                 a.source, cl.summary, json.dumps(cl.evidence), json.dumps(cl.failing_tests),
                 json.dumps(cl.implicated_files), a.signature, a.llm, a.input_tokens,
                 a.output_tokens, _now()),
            )
            return int(cur.lastrowid)

    def insert_action(self, run_pk: int, kind: str, status: str, dry_run: bool,
                      reason: str, detail: dict | None = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO actions (run_pk, kind, status, dry_run, reason, detail, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_pk, kind, status, int(dry_run), reason, json.dumps(detail or {}), _now()),
            )
            return int(cur.lastrowid)

    def count_actions(self, repo: str, run_id: int, kind: str, include_dry_run: bool = True) -> int:
        q = """SELECT COUNT(*) FROM actions a JOIN runs r ON a.run_pk = r.id
               WHERE r.repo=? AND r.run_id=? AND a.kind=? AND a.status IN ('done','planned')"""
        if not include_dry_run:
            q += " AND a.dry_run = 0"
        with self._conn() as c:
            return int(c.execute(q, (repo, run_id, kind)).fetchone()[0])

    def find_fix_action(self, repo: str, branch: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """SELECT a.* FROM actions a JOIN runs r ON a.run_pk = r.id
                   WHERE r.repo=? AND a.kind='fix_pr' AND json_extract(a.detail,'$.branch')=?
                   ORDER BY a.id DESC LIMIT 1""",
                (repo, branch),
            ).fetchone()

    # --- test history (the flaky-detection memory) ---------------------------------------
    def record_test_outcomes(self, ctx: RunContext, tests: list[str], outcome: str) -> None:
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO test_outcomes VALUES (?,?,?,?,?,?,?)",
                [(ctx.repo, t, ctx.head_sha, ctx.run_id, ctx.run_attempt, outcome, _now())
                 for t in tests],
            )

    def failing_tests_of_earlier_attempts(self, repo: str, run_id: int, before_attempt: int) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT DISTINCT test_id FROM test_outcomes
                   WHERE repo=? AND run_id=? AND run_attempt<? AND outcome='failed'""",
                (repo, run_id, before_attempt),
            ).fetchall()
            return [r["test_id"] for r in rows]

    def test_history(self, repo: str, test_id: str) -> dict:
        """Summarise one test's history.

        `flip_shas` is the number of commits on which this test BOTH failed and passed —
        direct evidence of non-determinism. `same_sha_failures` is how many times it failed
        on a commit where it never passed (consistent with a real break).
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT head_sha,
                          SUM(outcome='failed') AS fails,
                          SUM(outcome='passed') AS passes
                   FROM test_outcomes WHERE repo=? AND test_id=? GROUP BY head_sha""",
                (repo, test_id),
            ).fetchall()
        flips = sum(1 for r in rows if r["fails"] and r["passes"])
        return {
            "test_id": test_id,
            "shas_seen": len(rows),
            "flip_shas": flips,
            "total_failures": sum(r["fails"] for r in rows),
        }

    def signature_history(self, repo: str, signature: str) -> dict:
        """How often have we seen this exact failure signature, and how often did a later
        attempt of the same run then pass (i.e. it was transient)?"""
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS seen,
                          SUM(EXISTS (SELECT 1 FROM runs r2
                                      WHERE r2.repo=r.repo AND r2.run_id=r.run_id
                                        AND r2.run_attempt>r.run_attempt
                                        AND r2.conclusion='success')) AS later_passed
                   FROM analyses a JOIN runs r ON a.run_pk=r.id
                   WHERE r.repo=? AND a.signature=?""",
                (repo, signature),
            ).fetchone()
        return {"seen": int(row["seen"] or 0), "later_passed": int(row["later_passed"] or 0)}

    # --- reporting -----------------------------------------------------------------------
    def recent_analyses(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT r.repo, r.run_id, r.run_attempt, r.head_branch, r.html_url,
                          a.job_name, a.failed_step, a.category, a.confidence, a.source,
                          a.llm, a.input_tokens, a.output_tokens, a.summary, a.created_at
                   FROM analyses a JOIN runs r ON a.run_pk=r.id
                   ORDER BY a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_actions(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT r.repo, r.run_id, r.run_attempt, a.kind, a.status, a.dry_run,
                          a.reason, a.detail, a.created_at
                   FROM actions a JOIN runs r ON a.run_pk=r.id
                   ORDER BY a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
