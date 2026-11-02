"""SQLite repository for metadata, page checkpoints, jobs, Gemini usage, verifications and exports.

Large artifacts (images, page IR JSON, exports) live on disk via ArtifactStore; this DB holds
state and stable references. A fresh connection is opened per operation so the repository is
safe to share between threads and between processes (WAL mode + busy timeout).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..schemas import Job, JobStatus, PageStatus
from ..utils import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  title TEXT,
  filenames TEXT NOT NULL,
  document_type TEXT,
  page_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pages (
  document_id TEXT NOT NULL,
  page_number INTEGER NOT NULL,
  status TEXT NOT NULL,
  route TEXT,
  cache_key TEXT,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (document_id, page_number)
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  total_pages INTEGER NOT NULL DEFAULT 0,
  completed_pages INTEGER NOT NULL DEFAULT 0,
  failed_pages INTEGER NOT NULL DEFAULT 0,
  skipped_pages INTEGER NOT NULL DEFAULT 0,
  cached_pages INTEGER NOT NULL DEFAULT 0,
  options TEXT NOT NULL DEFAULT '{}',
  errors TEXT NOT NULL DEFAULT '[]',
  pid INTEGER,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_doc ON jobs(document_id);
CREATE TABLE IF NOT EXISTS gemini_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  document_id TEXT,
  page_number INTEGER,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  latency_ms INTEGER,
  prompt_tokens INTEGER,
  output_tokens INTEGER,
  thought_tokens INTEGER,
  total_tokens INTEGER,
  error_category TEXT
);
CREATE INDEX IF NOT EXISTS calls_doc ON gemini_calls(document_id);
CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  passed INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id TEXT NOT NULL,
  format TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS locks (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  heartbeat REAL NOT NULL
);
"""


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ---------------- documents ----------------
    def upsert_document(self, document_id: str, *, title: str | None, filenames: list[str], document_type: str | None,
                        page_count: int, status: str, data: dict[str, Any] | None = None) -> None:
        ts = now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT INTO documents(document_id,title,filenames,document_type,page_count,status,created_at,updated_at,data)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET title=COALESCE(excluded.title,title),
                     document_type=COALESCE(excluded.document_type,document_type), status=excluded.status,
                     updated_at=excluded.updated_at, data=excluded.data""",
                (document_id, title, json.dumps(filenames), document_type, page_count, status, ts, ts,
                 json.dumps(data or {})),
            )

    def update_document(self, document_id: str, **fields: Any) -> None:
        allowed = {"title", "document_type", "status", "data"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if k == "data" else v)
        sets.append("updated_at=?")
        vals.append(now_iso())
        with self._conn() as c:
            c.execute(f"UPDATE documents SET {', '.join(sets)} WHERE document_id=?", (*vals, document_id))

    def get_document(self, document_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        return self._doc_row(row) if row else None

    def list_documents(self, *, status: str | None = None, document_type: str | None = None, query: str | None = None,
                       limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where, vals = [], []
        if status:
            where.append("status=?")
            vals.append(status)
        if document_type:
            where.append("document_type=?")
            vals.append(document_type)
        if query:
            where.append("(filenames LIKE ? OR title LIKE ?)")
            vals += [f"%{query}%", f"%{query}%"]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM documents {clause}", vals).fetchone()[0]
            rows = c.execute(f"SELECT * FROM documents {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                             (*vals, limit, offset)).fetchall()
        return [self._doc_row(r) for r in rows], total

    def delete_document(self, document_id: str, *, keep_record: bool) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pages WHERE document_id=?", (document_id,))
            c.execute("DELETE FROM verifications WHERE document_id=?", (document_id,))
            c.execute("DELETE FROM exports WHERE document_id=?", (document_id,))
            if not keep_record:
                c.execute("DELETE FROM jobs WHERE document_id=?", (document_id,))
                c.execute("DELETE FROM documents WHERE document_id=?", (document_id,))

    @staticmethod
    def _doc_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["filenames"] = json.loads(d["filenames"])
        d["data"] = json.loads(d["data"])
        return d

    # ---------------- pages ----------------
    def set_page(self, document_id: str, page_number: int, status: PageStatus | str, *, route: str | None = None,
                 cache_key: str | None = None, error: str | None = None) -> None:
        st = status.value if isinstance(status, PageStatus) else status
        with self._conn() as c:
            c.execute(
                """INSERT INTO pages(document_id,page_number,status,route,cache_key,error,updated_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(document_id,page_number) DO UPDATE SET status=excluded.status, route=excluded.route,
                   cache_key=excluded.cache_key, error=excluded.error, updated_at=excluded.updated_at""",
                (document_id, page_number, st, route, cache_key, error, now_iso()),
            )

    def get_pages(self, document_id: str) -> dict[int, dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM pages WHERE document_id=? ORDER BY page_number", (document_id,)).fetchall()
        return {r["page_number"]: dict(r) for r in rows}

    # ---------------- jobs ----------------
    def create_job(self, job_id: str, document_id: str, total_pages: int, options: dict) -> Job:
        ts = now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT INTO jobs(job_id,document_id,status,stage,total_pages,options,created_at,updated_at,pid)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id, document_id, JobStatus.QUEUED.value, "queued", total_pages, json.dumps(options), ts, ts, os.getpid()),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "stage", "total_pages", "completed_pages", "failed_pages", "skipped_pages",
                   "cached_pages", "errors", "pid", "cancel_requested", "options"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            if isinstance(v, JobStatus):
                v = v.value
            if k in ("errors", "options"):
                v = json.dumps(v)
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(now_iso())
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", (*vals, job_id))

    def get_job(self, job_id: str) -> Job | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def cancel_requested(self, job_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def jobs_for_document(self, document_id: str) -> list[Job]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE document_id=? ORDER BY created_at DESC", (document_id,)).fetchall()
        return [self._job(r) for r in rows]

    def running_jobs(self) -> list[Job]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE status IN ('queued','running')").fetchall()
        return [self._job(r) for r in rows]

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        d = dict(row)
        d["options"] = json.loads(d["options"])
        d["errors"] = json.loads(d["errors"])
        d.pop("cancel_requested", None)
        return Job(**d)

    # ---------------- locks (one active processor per document across processes) ----------------
    def acquire_lock(self, name: str, owner: str, stale_after: float = 120.0) -> bool:
        import time

        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT owner, heartbeat FROM locks WHERE name=?", (name,)).fetchone()
            if row and row["owner"] != owner and now - row["heartbeat"] < stale_after:
                c.execute("ROLLBACK")
                return False
            c.execute("INSERT OR REPLACE INTO locks(name, owner, heartbeat) VALUES(?,?,?)", (name, owner, now))
            c.execute("COMMIT")
        return True

    def heartbeat(self, name: str, owner: str) -> None:
        import time

        with self._conn() as c:
            c.execute("UPDATE locks SET heartbeat=? WHERE name=? AND owner=?", (time.time(), name, owner))

    def release_lock(self, name: str, owner: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    # ---------------- gemini usage ----------------
    def record_call(self, **fields: Any) -> None:
        cols = ["at", "document_id", "page_number", "purpose", "model", "prompt_version", "status", "attempts",
                "latency_ms", "prompt_tokens", "output_tokens", "thought_tokens", "total_tokens", "error_category"]
        fields.setdefault("at", now_iso())
        fields.setdefault("attempts", 1)
        with self._conn() as c:
            c.execute(f"INSERT INTO gemini_calls({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                      [fields.get(k) for k in cols])

    def usage(self, document_id: str | None = None) -> list[dict]:
        clause, vals = ("WHERE document_id=?", [document_id]) if document_id else ("", [])
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT model, purpose, status, COUNT(*) AS calls, SUM(attempts) AS attempts,
                    SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens, SUM(COALESCE(output_tokens,0)) AS output_tokens,
                    SUM(COALESCE(thought_tokens,0)) AS thought_tokens, SUM(COALESCE(total_tokens,0)) AS total_tokens
                    FROM gemini_calls {clause} GROUP BY model, purpose, status""",
                vals,
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- verifications / exports ----------------
    def add_verification(self, document_id: str, passed: bool, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO verifications(document_id,created_at,passed,data) VALUES(?,?,?,?)",
                      (document_id, now_iso(), int(passed), json.dumps(data)))

    def add_export(self, document_id: str, fmt: str, path: str, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO exports(document_id,format,path,created_at,data) VALUES(?,?,?,?,?)",
                      (document_id, fmt, path, now_iso(), json.dumps(data)))

    def list_exports(self, document_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT format, path, created_at, data FROM exports WHERE document_id=? ORDER BY id DESC",
                             (document_id,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]
