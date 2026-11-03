"""Reading several pages at once with Antigravity: concurrency cap, pipelining, throttling, Stop, page order."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from scholarocr.webapp.agy import AgyCancelled, AgyRateLimited, AgyResult
from scholarocr.webapp.config import Prefs
from scholarocr.webapp.engines import EngineStatus
from scholarocr.webapp.runs import Limiter, ReadProgress, Run, RunManager
from tests.fixtures.corpus import degrade, images_to_pdf, text_page_image
from tests.webapp.conftest import MODELS, events, make_client, page_payload, upload


@pytest.fixture
def five_pages(tmp_path):
    pages = [degrade(text_page_image([f"Q{i}. Solve x + {i} = {i + 3}. [2]", "Show your working."], size=(620, 877),
                                     font_size=22), seed=i) for i in range(1, 6)]
    return images_to_pdf(pages, tmp_path / "five.pdf", dpi=100)


class SlowAgy:
    """Records how many page reads run at once; each takes ``delay`` seconds."""

    def __init__(self, delay: float = 0.25, fail: dict[int, list[Exception]] | None = None):
        self.delay = delay
        self.fail = fail or {}
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: list[dict] = []
        self.cancelled = 0

    def run(self, prompt, **kw):
        with self.lock:
            self.calls.append({"prompt": prompt, **kw})
            idx = len(self.calls)
            self.active += 1
            self.peak = max(self.peak, self.active)
            todo = self.fail.get(idx)
        try:
            if kw.get("schema") is None:
                return AgyResult("ok", None, {"total_tokens": 1}, 0.1, 0.1, "c")
            deadline = time.monotonic() + self.delay
            while time.monotonic() < deadline:
                if kw.get("cancel") is not None and kw["cancel"].is_set():
                    with self.lock:
                        self.cancelled += 1
                    raise AgyCancelled("cancelled")
                time.sleep(0.01)
            if todo:
                raise todo.pop(0)
            p = page_payload()
            return AgyResult("{}", p, {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}, 0.2, 0.2,
                             f"conv-{idx}")
        finally:
            with self.lock:
                self.active -= 1


def _read(client, path):
    up = upload(client, path).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]})
    assert r.status_code == 202, r.text
    return cid, events(client, r.json()["run_id"])


def test_pages_are_read_several_at_once(cfg, keys, five_pages):
    slow = SlowAgy(delay=1.5)  # slower than preparing a page, so pages overlap
    client, _, app = make_client(cfg, keys, agy=slow)
    assert client.put("/api/prefs", json={"read_concurrency": 2}).status_code == 200
    cid, evs = _read(client, five_pages)
    assert evs[-1]["state"] == "done"
    assert slow.peak == 2 and len(slow.calls) == 5  # never more than the setting
    reading = [e for e in evs if e["type"] == "page" and e["status"] == "reading"]
    assert any(len(e["reading"]) == 2 for e in reading) and all(e["total"] == 5 for e in reading)
    done = [e["page"] for e in evs if e["type"] == "page" and e["status"] == "done"]
    assert sorted(done) == [1, 2, 3, 4, 5]
    data = client.get(f"/api/conversations/{cid}").json()["messages"][-1]["data"]
    assert [int(k.rsplit(":", 1)[1]) for k in data["pages"]] == [1, 2, 3, 4, 5]  # stored in page order
    assert data["concurrency"] == 2 and data["effort"] == "low"
    view = client.get(f"/api/documents/{data['documents'][0]['document_id']}").json()
    assert [p["page_number"] for p in view["pages"]] == [1, 2, 3, 4, 5]
    assert all(p["status"] == "done" for p in view["pages"])
    assert client.put("/api/prefs", json={"read_concurrency": 7}).status_code == 400
    assert client.put("/api/prefs", json={"read_effort": "turbo"}).status_code == 400


def test_rate_limit_pauses_halves_concurrency_and_retries(cfg, keys, five_pages):
    cfg = cfg.model_copy(update={"rate_limit_backoff_s": 0.05})
    slow = SlowAgy(delay=0.1, fail={1: [AgyRateLimited("agy reported ERROR: Error 429: RESOURCE_EXHAUSTED")]})
    client, _, _ = make_client(cfg, keys, agy=slow)
    client.put("/api/prefs", json={"read_concurrency": 4})
    _, evs = _read(client, five_pages)
    assert evs[-1]["state"] == "done"  # the throttled page was read on the second try
    throttled = [e for e in evs if e["type"] == "stage" and e.get("stage") == "throttled"]
    assert len(throttled) == 1 and throttled[0]["concurrency"] == 2 and "rate limiting" in throttled[0]["message"]
    after = [e for e in evs if e["type"] == "page" and e["status"] == "reading" and e["seq"] > throttled[0]["seq"]]
    assert after and all(len(e["reading"]) <= 2 for e in after)
    assert len(slow.calls) == 6


def test_repeated_failures_count_as_throttling(cfg, keys, five_pages):
    from scholarocr.webapp.agy import AgyError

    cfg = cfg.model_copy(update={"rate_limit_backoff_s": 0.05})
    slow = SlowAgy(delay=0.05, fail={1: [AgyError("agy exited without a result (boom)")],
                                     2: [AgyError("agy exited without a result (boom)")]})
    client, _, _ = make_client(cfg, keys, agy=slow)
    client.put("/api/prefs", json={"read_concurrency": 1})
    _, evs = _read(client, five_pages)
    assert [e for e in evs if e["type"] == "stage" and e.get("stage") == "throttled"]
    assert evs[-1]["state"] == "done"


def test_stop_kills_every_running_page(cfg, keys, five_pages):
    from starlette.testclient import TestClient

    from scholarocr.webapp.server import create_app
    from tests.webapp.conftest import ORIGIN, FakeRegistry

    slow = SlowAgy(delay=30)
    asgi = create_app(cfg, keys=keys, registry=FakeRegistry(cfg, keys), agy_factory=lambda: slow, token="t",
                      threaded_runs=True)
    c = TestClient(asgi, base_url=ORIGIN)
    c.headers.update({"X-ScholarOCR-Token": "t", "Origin": ORIGIN})
    c.put("/api/prefs", json={"read_concurrency": 3})
    up = upload(c, five_pages).json()
    cid = c.post("/api/conversations").json()["id"]
    run_id = c.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]}).json()["run_id"]
    deadline = time.monotonic() + 30
    while slow.active < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert slow.active == 3
    assert c.post(f"/api/runs/{run_id}/cancel").json() == {"cancelling": True}
    evs = events(c, run_id)
    assert evs[-1]["state"] == "cancelled"
    assert slow.cancelled == 3 and slow.active == 0 and len(slow.calls) == 3  # all three stopped, none started
    assert sorted(evs[-1]["data"]["documents"][0]["unread_pages"]) == [1, 2, 3, 4, 5]


def test_limiter_is_shared_and_resizable():
    lim = Limiter(2)
    cancel = threading.Event()
    assert lim.acquire(cancel) and lim.acquire(cancel)
    got = []
    t = threading.Thread(target=lambda: got.append(lim.acquire(cancel)))
    t.start()
    time.sleep(0.3)
    assert got == []  # the third waits
    lim.set_capacity(3)
    t.join(2)
    assert got == [True] and lim.in_use == 3
    cancel.set()
    assert lim.acquire(cancel) is False  # a stopped run never waits for a slot
    for _ in range(3):
        lim.release()
    assert lim.in_use == 0


class StubService:
    """Just enough of ScholarOCR for the page reader: the job prepares one page per tick."""

    def __init__(self, pages: int):
        self.pages = pages
        self.prepared = 0
        self.status = {}
        self.submitted: list[tuple[int, int]] = []  # (page, pages prepared when it was submitted)
        self.repo = SimpleNamespace(get_pages=lambda doc: {n: {"status": s} for n, s in self.status.items()})

    def tick(self):
        if self.prepared < self.pages:
            self.prepared += 1
            self.status[self.prepared] = "awaiting_transcription"

    def job_status(self, job_id):
        self.tick()
        done = self.prepared == self.pages
        return {"completed_pages": 0, "failed_pages": 0, "total_pages": self.pages,
                "stage": "done" if done else f"page {self.prepared}",
                "status": "awaiting_transcription" if done else "running"}

    def cancel_job(self, job_id):
        return {}

    def awaiting_pages(self, doc):
        return [n for n, s in sorted(self.status.items()) if s == "awaiting_transcription"]

    def host_page_image(self, doc, n, instructions="full"):
        return b"\x89PNG", "image/png", {"task": {"route": None, "hints": {}, "text_layer": None}}

    def submit_page_content(self, doc, n, payload, transcriber=None):
        self.submitted.append((n, self.prepared))
        self.status[n] = "done"
        return {"accepted": True, "elements": 3, "uncertainty": {"low": 3}}


def test_reading_starts_before_all_pages_are_prepared(cfg, tmp_path):
    svc = StubService(pages=8)
    fake = SlowAgy(delay=0.01)
    rm = RunManager(cfg, store=SimpleNamespace(update_message=lambda *a, **k: None), pool=None,
                    agy_factory=lambda: fake, threaded=False)
    run = Run("r1", "c1", "m1", "read")
    eng = EngineStatus("antigravity", "Antigravity", True, models=list(MODELS), default_model=MODELS[0]["id"])
    data = {"pages": {}, "usage": {}}
    rm._transcribe_with_agy(run, svc, "doc", "job", eng, "gemini-3.8-flash-medium", Prefs(read_concurrency=3), data,
                            page_count=8)
    assert sorted(n for n, _ in svc.submitted) == list(range(1, 9))
    assert svc.submitted[0][1] < 8  # page 1 was read while later pages were still being prepared
    assert list(data["pages"]) == [f"doc:{n}" for n in range(1, 9)]
    assert all(c["model"] == "gemini-3.8-flash" and c["effort"] == "low" for c in fake.calls)


def test_progress_snapshot():
    p = ReadProgress(total=12, concurrency=3)
    p.reading.update({6, 4, 5})
    assert p.snapshot() == {"reading": [4, 5, 6], "read": 0, "total": 12, "concurrency": 3}
