"""Local history: conversations, their messages and the documents attached to them (``app.db``)."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..exceptions import NotFoundError
from ..utils import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'done', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS conversation_documents (
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, document_id TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY (conversation_id, document_id));
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class AppStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            # A run that was in progress when the app stopped can never finish.
            self._db.execute("UPDATE messages SET state='interrupted', updated_at=? WHERE state='running'", (now_iso(),))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ conversations
    def create_conversation(self, title: str = "New document") -> dict[str, Any]:
        cid, now = new_id("c"), now_iso()
        with self._lock:
            self._db.execute("INSERT INTO conversations VALUES (?,?,?,?)", (cid, title[:120], now, now))
        return {"id": cid, "title": title[:120], "created_at": now, "updated_at": now}

    def get_conversation(self, cid: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise NotFoundError(f"No conversation {cid}")
        return dict(row)

    def list_conversations(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM conversation_documents d WHERE d.conversation_id=c.id) AS documents "
                "FROM conversations c ORDER BY c.updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rename(self, cid: str, title: str) -> dict[str, Any]:
        title = " ".join((title or "").split())[:120] or "Untitled"
        with self._lock:
            n = self._db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                                 (title, now_iso(), cid)).rowcount
        if not n:
            raise NotFoundError(f"No conversation {cid}")
        return self.get_conversation(cid)

    def touch(self, cid: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), cid))

    def delete_conversation(self, cid: str) -> None:
        with self._lock:
            n = self._db.execute("DELETE FROM conversations WHERE id=?", (cid,)).rowcount
        if not n:
            raise NotFoundError(f"No conversation {cid}")

    # ------------------------------------------------------------------ messages
    def add_message(self, cid: str, role: str, kind: str, text: str = "", data: dict | None = None,
                    state: str = "done") -> dict[str, Any]:
        mid, now = new_id("m"), now_iso()
        with self._lock:
            self._db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                             (mid, cid, role, kind, text, json.dumps(data or {}, ensure_ascii=False), state, now, now))
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
        return {"id": mid, "conversation_id": cid, "role": role, "kind": kind, "text": text, "data": data or {},
                "state": state, "created_at": now, "updated_at": now}

    def update_message(self, mid: str, *, text: str | None = None, data: dict | None = None,
                       state: str | None = None, kind: str | None = None) -> None:
        sets, args = ["updated_at=?"], [now_iso()]
        if text is not None:
            sets.append("text=?")
            args.append(text)
        if data is not None:
            sets.append("data=?")
            args.append(json.dumps(data, ensure_ascii=False))
        if state is not None:
            sets.append("state=?")
            args.append(state)
        if kind is not None:
            sets.append("kind=?")
            args.append(kind)
        with self._lock:
            self._db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", (*args, mid))

    def messages(self, cid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid",
                                    (cid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"] or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------ documents
    def attach_document(self, cid: str, document_id: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO conversation_documents VALUES (?,?,?)", (cid, document_id, now_iso()))

    def documents(self, cid: str) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT document_id FROM conversation_documents WHERE conversation_id=? "
                                    "ORDER BY created_at DESC", (cid,)).fetchall()
        return [r[0] for r in rows]

    def owns_document(self, document_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM conversation_documents WHERE document_id=? LIMIT 1",
                                    (document_id,)).fetchone() is not None
