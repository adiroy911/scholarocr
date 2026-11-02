"""Retention cleanup for remote mode.

* Documents (preserved source copy, extraction, crops, exports, record) are deleted once they have
  not been updated for ``SCHOLAROCR_REMOTE_RETENTION_HOURS`` (default 24). Documents with a running
  job are skipped until the next sweep.
* Staging batches older than one hour are removed (normally they are deleted right after ingestion).
* Users idle for ``SCHOLAROCR_REMOTE_KEY_RETENTION_DAYS`` (default 30) lose their stored key, tokens
  and sandbox entirely.
* Expired OAuth requests, codes and tokens are purged.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..logging_config import get_logger
from .services import ServicePool
from .store import AuthStore

log = get_logger("remote.retention")


def force_rmtree(path) -> None:
    """rmtree that also removes read-only files (preserved sources are chmod 0444)."""

    def onerror(fn, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            fn(p)
        except OSError:
            pass

    if os.path.exists(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=onerror)
        else:  # pragma: no cover
            shutil.rmtree(path, onerror=onerror)


class Janitor:
    def __init__(self, pool: ServicePool, store: AuthStore, *, clock: Callable[[], float] = time.time):
        self.pool, self.store, self.clock = pool, store, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sweep(self) -> dict:
        cfg = self.pool.cfg
        now = self.clock()
        cutoff_iso = (datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=cfg.retention_hours)
                      ).isoformat(timespec="seconds")
        report = {"documents_deleted": 0, "batches_removed": 0, "users_removed": 0, "auth_rows_purged": 0,
                  "skipped_running": 0}
        idle = set(self.store.idle_users(now - cfg.key_retention_days * 86400))
        for user_id in self.pool.known_users():
            try:
                sandbox = self.pool.sandbox(user_id)
            except Exception:
                continue
            if user_id in idle:
                self.remove_user(user_id)
                report["users_removed"] += 1
                continue
            report["batches_removed"] += sandbox.sweep_inbox(3600)
            if not (sandbox.data_dir / "scholarocr.db").exists():
                continue
            svc = self.pool.get_readonly(user_id)
            rows, _ = svc.repo.list_documents(limit=1_000_000)
            for r in rows:
                if (r.get("updated_at") or r.get("created_at") or "") > cutoff_iso:
                    continue
                if any(j.status.value in ("running", "queued") for j in svc.repo.jobs_for_document(r["document_id"])):
                    report["skipped_running"] += 1
                    continue
                try:
                    svc.delete(r["document_id"], mode="all", confirm=True)
                    report["documents_deleted"] += 1
                except Exception as e:  # keep sweeping other documents
                    log.warning("retention: could not delete a document (%s)", e.__class__.__name__)
        for user_id in idle:  # keys without a sandbox directory
            self.store.delete_user(user_id)
        report["auth_rows_purged"] = self.store.purge_expired(now)
        if any(report.values()):
            log.info("retention sweep: %s", report)
        return report

    def remove_user(self, user_id: str) -> None:
        self.pool.drop(user_id)
        self.store.delete_user(user_id)
        root = self.pool.cfg.users_dir / user_id
        if root.resolve().is_relative_to(self.pool.cfg.users_dir.resolve()):
            force_rmtree(root)

    def start(self, interval_s: float) -> None:
        def loop():
            while not self._stop.wait(interval_s):
                try:
                    self.sweep()
                except Exception:  # pragma: no cover - logged, never kills the server
                    log.exception("retention sweep failed")

        self._thread = threading.Thread(target=loop, name="scholarocr-janitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
