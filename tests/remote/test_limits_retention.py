"""Size limits over MCP, per-user rate limits, and retention cleanup."""

import base64
import time

import pytest
from mcp import Client

from scholarocr.remote.ratelimit import RateLimited, SlidingWindow
from scholarocr.remote.retention import Janitor


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_sliding_window():
    clock = Clock()
    w = SlidingWindow(3, 60, clock)
    for _ in range(3):
        w.hit("u", "calls")
    with pytest.raises(RateLimited) as e:
        w.hit("u", "calls")
    assert 0 < e.value.retry_after_s <= 60
    w.hit("other", "calls")  # per user
    clock.t += 61
    w.hit("u", "calls")
    with pytest.raises(RateLimited):
        w.check("u", "pages", cost=3)
    assert SlidingWindow(0, 60, clock).check("u", "x", cost=10**6) is None  # 0 disables


async def test_size_limits_over_mcp(remote, corpus):
    ns = remote(max_file_mb=0.01, max_files_per_call=2, max_base64_mb=0.001)
    ns.login()
    big = ns.fetcher.add("https://files.example/big.pdf", corpus["handwritten.png"].read_bytes())
    small = ns.fetcher.add("https://files.example/s.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [big]})
        assert r.is_error and "limit_exceeded" in r.content[0].text
        r = await c.call_tool("ocr_upload", {"files": [small, small, small]})
        assert r.is_error and "at most 2 files" in r.content[0].text
        r = await c.call_tool("ocr_upload", {"files": []})
        assert r.is_error and "at least one file" in r.content[0].text
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000).decode()
        r = await c.call_tool("ocr_upload_base64", {"files": [{"file_name": "a.png", "data_base64": b64}]})
        assert r.is_error and "limit_exceeded" in r.content[0].text
    assert not list(ns.pool.sandbox(ns.identity.user).inbox.iterdir())  # failed calls leave nothing staged


async def test_per_user_call_rate_limit(remote):
    ns = remote(calls_per_minute=3)
    ns.login()
    async with Client(ns.mcp) as c:
        results = [await c.call_tool("list_documents", {}) for _ in range(4)]
        assert [r.is_error for r in results] == [False, False, False, True]
        assert "rate_limited" in results[-1].content[0].text
        ns.login("AIzaUSER-B-own-key-000000000000000002")  # a different user is not affected
        assert not (await c.call_tool("list_documents", {})).is_error


async def test_daily_page_budget(remote, corpus):
    ns = remote(pages_per_day=1)
    ns.login()
    a = ns.fetcher.add("https://files.example/a.pdf", corpus["handwritten.png"].read_bytes())
    b = ns.fetcher.add("https://files.example/b.png", corpus["rotated.png"].read_bytes())
    async with Client(ns.mcp) as c:
        first = await c.call_tool("ocr_upload", {"files": [a]})
        assert not first.is_error
        second = await c.call_tool("ocr_upload", {"files": [b]})
    assert second.is_error and "pages per day" in second.content[0].text


async def test_retention_sweep_deletes_old_documents_and_idle_users(remote, corpus):
    ns = remote(retention_hours=24, key_retention_days=30)
    uid = ns.login()
    f = ns.fetcher.add("https://files.example/a.pdf", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        doc_id = (await c.call_tool("ocr_upload", {"files": [f]})).structured_content["document_id"]
    svc = ns.pool.get(uid)
    assert svc.list_documents()["total"] == 1
    stale_batch = ns.pool.sandbox(uid).new_batch()
    old = time.time() - 7200
    import os

    os.utime(stale_batch, (old, old))

    now = time.time()
    first = Janitor(ns.pool, ns.store, clock=lambda: now).sweep()
    assert first["documents_deleted"] == 0 and first["batches_removed"] == 1  # stale staging batch
    report = Janitor(ns.pool, ns.store, clock=lambda: now + 25 * 3600).sweep()
    assert report["documents_deleted"] == 1
    assert svc.list_documents()["total"] == 0
    assert not (svc.store.doc_dir(doc_id)).exists()
    assert ns.store.get_key(uid) is not None  # key kept: user not idle yet

    report = Janitor(ns.pool, ns.store, clock=lambda: now + 31 * 86400).sweep()
    assert report["users_removed"] == 1
    assert ns.store.get_key(uid) is None and not (ns.cfg.users_dir / uid).exists()


async def test_delete_my_data(remote, corpus):
    ns = remote()
    uid = ns.login()
    f = ns.fetcher.add("https://files.example/a.pdf", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        await c.call_tool("ocr_upload", {"files": [f]})
        refused = await c.call_tool("delete_my_data", {})
        assert refused.is_error and ns.store.get_key(uid) is not None
        done = await c.call_tool("delete_my_data", {"confirm": True})
    assert not done.is_error
    assert ns.store.get_key(uid) is None and not (ns.cfg.users_dir / uid).exists()
