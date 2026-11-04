"""Resuming a stopped, failed, interrupted or partly read document from the page, with agy mocked."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scholarocr.webapp.agy import AgyError, AgyRateLimited
from scholarocr.webapp.server import create_app
from tests.fixtures.corpus import degrade, images_to_pdf, text_page_image
from tests.webapp.conftest import ORIGIN, FakeAgy, FakeRegistry, events, make_client, page_payload, upload
from tests.webapp.test_concurrency import SlowAgy


@pytest.fixture
def four_pages(tmp_path):
    pages = [degrade(text_page_image([f"Q{i}. Solve x + {i} = {i + 3}. [2]", "Show your working."], size=(620, 877),
                                     font_size=22), seed=i) for i in range(1, 5)]
    return images_to_pdf(pages, tmp_path / "four.pdf", dpi=100)


def _threaded_client(cfg, keys, agy, token="t"):
    asgi = create_app(cfg, keys=keys, registry=FakeRegistry(cfg, keys), agy_factory=lambda: agy, token=token,
                      threaded_runs=True)
    c = TestClient(asgi, base_url=ORIGIN)
    c.headers.update({"X-ScholarOCR-Token": token, "Origin": ORIGIN})
    return c, asgi.state.app


def _start_read(client, path):
    up = upload(client, path).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]})
    assert r.status_code == 202, r.text
    return cid, r.json()["run_id"], r.json()["assistant_message"]["id"]


def _read_message(client, cid):
    return next(m for m in client.get(f"/api/conversations/{cid}").json()["messages"] if m["kind"] == "read")


def _document_id(client, cid):
    return _read_message(client, cid)["data"]["documents"][0]["document_id"]


def _resume(client, doc_id, message_id):
    return client.post(f"/api/documents/{doc_id}/resume", json={"message_id": message_id})


def _status(client, doc_id):
    view = client.get(f"/api/documents/{doc_id}").json()
    return {p["page_number"]: p["status"] for p in view["pages"]}, view


def _prompts_for_pages(fake):
    """Which pages the mock was asked to read, in order (each call names its own run folder)."""
    out = []
    for c in fake.calls:
        if c.get("schema") is not None:
            out.append(int(str(c["run_dir"]).rsplit("-p", 1)[1]))
    return out


def _page_text(client, doc_id, page):
    _, view = _status(client, doc_id)
    return [e["text"] for p in view["pages"] if p["page_number"] == page for e in p["elements"]]


def _source_files(cfg, doc_id) -> list[Path]:
    return list((cfg.engine_data_dir / "documents" / doc_id / "source").iterdir())


def _remove_source(cfg, doc_id) -> None:
    for f in _source_files(cfg, doc_id):
        os.chmod(f, stat.S_IWRITE)  # preserved sources are read-only
        f.unlink()


def _stop_after_some_pages(cfg, keys, path):
    """A read stopped while pages are in progress: page 1 and 2 accepted (concurrency 1 keeps it exact)."""
    gate = threading.Event()

    class StopsAtThirdRead(FakeAgy):
        def run(self, prompt, **kw):
            if len(self.calls) == 2 and kw.get("schema") is not None:
                gate.set()
                while not kw["cancel"].is_set():
                    time.sleep(0.01)
                with self.lock:
                    self.calls.append({"prompt": prompt, **kw})
                from scholarocr.webapp.agy import AgyCancelled

                raise AgyCancelled("stopped")
            return super().run(prompt, **kw)

    fake = StopsAtThirdRead()
    client, app = _threaded_client(cfg, keys, fake)
    client.put("/api/prefs", json={"read_concurrency": 1})
    cid, run_id, mid = _start_read(client, path)
    assert gate.wait(30)
    assert client.post(f"/api/runs/{run_id}/cancel").json() == {"cancelling": True}
    evs = events(client, run_id)
    assert evs[-1]["state"] == "cancelled"
    return client, app, fake, cid, mid


def test_resume_after_stop_reads_only_unaccepted_pages(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    status, view = _status(client, doc_id)
    assert [status[n] for n in (1, 2)] == ["done", "done"]
    assert view["resume"] == {"unread": [3, 4], "source_missing": []}
    kept = (_page_text(client, doc_id, 1), _page_text(client, doc_id, 2))
    first = _read_message(client, cid)
    assert first["state"] == "cancelled" and first["data"]["documents"][0]["unread_pages"] == [3, 4]

    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    assert r.status_code == 202, r.text
    assert r.json()["already_running"] is False and r.json()["message_id"] == mid
    evs = events(client, r.json()["run_id"])
    assert evs[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fake)) == [3, 4]  # accepted pages were not read again
    # same throttled reader, progress counting from the pages already accepted
    started = next(e for e in evs if e["type"] == "document_started")
    assert started["resumed"] is True and started["already_read"] == 2 and started["page_count"] == 4
    done_events = [e for e in evs if e["type"] == "page" and e["status"] == "done"]
    assert [e["read"] for e in done_events] == [3, 4]
    assert all(e["total"] == 4 and e["concurrency"] == 1 for e in done_events)
    status, view = _status(client, doc_id)
    assert set(status.values()) == {"done"} and view["resume"] is None
    assert (_page_text(client, doc_id, 1), _page_text(client, doc_id, 2)) == kept
    # it stays one message with one document and adds the usage of this run
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["kind"] for m in msgs].count("read") == 1
    m = _read_message(client, cid)
    assert m["state"] == "done" and m["id"] == mid and len(m["data"]["documents"]) == 1
    assert m["data"]["documents"][0]["resumed"] == 1 and "unread_pages" not in m["data"]["documents"][0]
    assert m["data"]["usage"]["total_tokens"] == 22300 * 4
    assert sorted(m["data"]["pages"]) == [f"{doc_id}:{n}" for n in (1, 2, 3, 4)]


def test_resume_uses_the_engine_model_and_effort_of_the_read(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    stored = _read_message(client, cid)["data"]
    client.put("/api/prefs", json={"read_effort": "medium", "agy_model": "gemini-3.8-flash-low"})
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    events(client, r.json()["run_id"])
    m = _read_message(client, cid)["data"]
    assert m["engine"] == stored["engine"] and m["model"] == stored["model"] and m["effort"] == stored["effort"]
    assert {c["model"] for c in fake.calls} == {fake_first_model(stored)}


def fake_first_model(stored):
    from tests.webapp.conftest import MODELS
    from scholarocr.webapp.agy import model_for_effort

    return model_for_effort(stored["model"], stored["effort"], [m["id"] for m in MODELS])[0]


def test_stop_during_resume_then_resume_again(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    slow = SlowAgy(delay=30)
    app.runs.agy_factory = lambda: slow
    r = _resume(client, doc_id, mid)
    run_id = r.json()["run_id"]
    deadline = time.monotonic() + 20
    while slow.active < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.post(f"/api/runs/{run_id}/cancel").json() == {"cancelling": True}
    assert events(client, run_id)[-1]["state"] == "cancelled"  # the same Stop as a first read
    assert slow.cancelled == 1
    assert _status(client, doc_id)[1]["resume"]["unread"] == [3, 4]
    app.runs.agy_factory = lambda: fake
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fake)) == [3, 4]


def test_double_start_keeps_one_run(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    slow = SlowAgy(delay=1.0)
    app.runs.agy_factory = lambda: slow
    answers: list = []
    barrier = threading.Barrier(4)

    def click():
        barrier.wait()
        answers.append(_resume(client, doc_id, mid))

    threads = [threading.Thread(target=click) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.status_code for r in answers) == [200, 200, 200, 202]
    assert len({r.json()["run_id"] for r in answers}) == 1
    run_id = answers[0].json()["run_id"]
    # resuming while it is reading answers with that run too
    again = _resume(client, doc_id, mid)
    assert again.status_code == 200 and again.json() == {
        "run_id": run_id, "message_id": mid, "conversation_id": cid, "already_running": True}
    assert events(client, run_id)[-1]["state"] == "done"
    assert sorted(int(str(c["run_dir"]).rsplit("-p", 1)[1]) for c in slow.calls) == [3, 4]  # each page read once
    # and when nothing is running or left, nothing starts
    r = _resume(client, doc_id, mid)
    assert r.status_code == 400 and "read" in r.json()["message"]
    assert len(slow.calls) == 2


def test_resume_refused_while_the_conversation_is_answering(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    release = threading.Event()

    class SlowChat(FakeAgy):
        def run(self, prompt, **kw):
            release.wait(20)
            return super().run(prompt, **kw)

    app.runs.agy_factory = lambda: SlowChat()
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": "What is Q1?"}).status_code == 202
    r = _resume(client, doc_id, mid)
    assert r.status_code == 400 and "still working" in r.json()["message"]
    release.set()


def test_same_document_is_not_read_twice_across_conversations(cfg, keys, four_pages):
    slow = SlowAgy(delay=30)
    client, app = _threaded_client(cfg, keys, slow)
    cid, run_id, _ = _start_read(client, four_pages)
    deadline = time.monotonic() + 20
    while not slow.active and time.monotonic() < deadline:
        time.sleep(0.02)
    cid2, run2, _ = _start_read(client, four_pages)
    evs = events(client, run2)
    assert evs[-1]["state"] == "error" and "another conversation" in evs[-1]["data"]["error"]["message"]
    client.post(f"/api/runs/{run_id}/cancel")
    events(client, run_id)
    assert all(run_id in str(c["run_dir"]) for c in slow.calls)  # the second read started no page


def test_interrupted_job_is_resumable_after_a_restart(cfg, keys, four_pages):
    """The process dies during a read: message and job say running, the document lock is still held."""
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    svc = app.pool.reader(app.prefs.load())
    stale_job = svc.repo.create_job("job_dead", doc_id, 4, {})
    svc.repo.update_job(stale_job.job_id, status="running", pid=2 ** 22 + 1)  # no such process
    assert svc.repo.acquire_lock(f"doc:{doc_id}", stale_job.job_id)
    data = _read_message(client, cid)["data"]
    data["documents"][0]["state"] = "running"
    data["documents"][0].pop("unread_pages", None)
    app.store.update_message(mid, data=data, state="running")
    app.store.close()

    fresh = FakeAgy()
    client2, app2 = _threaded_client(cfg, keys, fresh, token="t2")
    m = _read_message(client2, cid)
    assert m["state"] == "interrupted"  # not stuck "reading"
    assert client2.get(f"/api/conversations/{cid}").json()["active_run"] is None
    status, view = _status(client2, doc_id)
    assert view["resume"]["unread"] == [3, 4]
    r = _resume(client2, doc_id, mid)
    assert r.status_code == 202, r.text
    evs = events(client2, r.json()["run_id"])
    assert evs[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fresh)) == [3, 4]
    svc2 = app2.pool.reader(app2.prefs.load())
    assert svc2.repo.get_job("job_dead").status.value == "interrupted"
    # the page that a dying app stored but never recorded counts as accepted
    assert _read_message(client2, cid)["state"] == "done"


def test_page_stored_but_not_recorded_is_not_read_again(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    svc = app.pool.reader(app.prefs.load())
    svc.repo.set_page(doc_id, 2, "awaiting_transcription")  # the app died between the two writes
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fake)) == [3, 4]


def test_interrupted_while_preparing_finishes_preparing(cfg, keys, four_pages):
    """Pages that were never prepared go through the pipeline, for those pages only."""
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    svc = app.pool.reader(app.prefs.load())
    (cfg.engine_data_dir / "documents" / doc_id / "pages" / "0004" / "page.json").unlink()
    assert svc.resume_plan(doc_id)["to_prepare"] == [4]
    assert svc.resume_plan(doc_id)["prepared"] == [3]
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    evs = events(client, r.json()["run_id"])
    assert evs[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fake)) == [3, 4]
    job = svc.repo.jobs_for_document(doc_id)[0]
    assert job.total_pages == 1 and job.options["pages"] == "4"


def test_stop_while_preparing_then_resume_at_once(cfg, keys, four_pages):
    """The stopped job may still hold the document's lock: a resume waits for it instead of failing on it."""
    slow = SlowAgy(delay=0.2)
    client, app = _threaded_client(cfg, keys, slow)
    cid, run_id, mid = _start_read(client, four_pages)
    deadline = time.monotonic() + 20
    while not _read_message(client, cid)["data"].get("documents") and time.monotonic() < deadline:
        time.sleep(0.01)  # the document exists and its job is preparing pages
    client.post(f"/api/runs/{run_id}/cancel")
    assert events(client, run_id)[-1]["state"] == "cancelled"
    doc_id = _document_id(client, cid)
    unread = _status(client, doc_id)[1]["resume"]["unread"]
    assert unread
    slow.calls.clear()
    r = _resume(client, doc_id, mid)
    assert r.status_code == 202, r.text
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    read = sorted(int(str(c["run_dir"]).rsplit("-p", 1)[1]) for c in slow.calls)
    assert read == unread and set(_status(client, doc_id)[0].values()) == {"done"}


def test_source_gone_says_so_and_attaching_again_restores_it(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    (cfg.engine_data_dir / "documents" / doc_id / "pages" / "0004" / "page.json").unlink()
    _remove_source(cfg, doc_id)
    _, view = _status(client, doc_id)
    assert view["resume"] == {"unread": [3, 4], "source_missing": ["four.pdf"]}
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    assert r.status_code == 400
    assert "is gone" in r.json()["message"] and "Attach the file again" in r.json()["message"]
    assert r.json()["details"]["source_missing"] == ["four.pdf"]
    assert not fake.calls and app.runs.active_for(cid) is None
    assert _read_message(client, cid)["state"] == "cancelled"  # a refused resume changes nothing

    up = upload(client, four_pages).json()  # attach the same file again
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]})
    assert r.status_code == 202, r.text
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fake)) == [3, 4]  # the cache keeps what was read
    assert len(_source_files(cfg, doc_id)) == 1
    assert _status(client, doc_id)[1]["resume"] is None


def test_source_gone_does_not_block_prepared_pages(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    _remove_source(cfg, doc_id)
    assert _status(client, doc_id)[1]["resume"] == {"unread": [3, 4], "source_missing": []}
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    assert r.status_code == 202, r.text
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    assert sorted(_prompts_for_pages(fake)) == [3, 4]


def test_repeated_failures_are_retried_and_reported(cfg, keys, four_pages):
    """Rejected and failed pages stay unread; a resume tries them again and says so when they fail again."""
    fails = FakeAgy([AgyError("agy exited with code 1")] * 2)
    client, fake, app = make_client(cfg, keys, agy=fails)
    app.cfg.rate_limit_backoff_s = 0.01
    client.put("/api/prefs", json={"read_concurrency": 1})
    fails.replies = [page_payload(), page_payload(), "not json", "not json", "not json", "not json", "not json",
                     "not json"]
    cid, run_id, mid = _start_read(client, four_pages)
    assert events(client, run_id)[-1]["state"] == "partial"
    doc_id = _document_id(client, cid)
    assert _status(client, doc_id)[1]["resume"]["unread"] == [3, 4]
    first = _read_message(client, cid)["data"]
    assert first["pages"][f"{doc_id}:3"]["status"] == "rejected"

    # resume: page 3 fails again with a rate limit until the retries run out, page 4 succeeds
    fails.calls.clear()
    fails.replies = [AgyRateLimited("429 quota") for _ in range(4)] + [page_payload()]
    r = _resume(client, doc_id, mid)
    evs = events(client, r.json()["run_id"])
    assert evs[-1]["state"] == "partial"
    assert sorted(set(_prompts_for_pages(fails))) == [3, 4]
    m = _read_message(client, cid)
    entry = m["data"]["documents"][0]
    assert m["state"] == "partial" and entry["unread_pages"] == [3] and entry["resumed"] == 1
    assert m["data"]["pages"][f"{doc_id}:3"]["status"] == "error"
    assert "429" in m["data"]["pages"][f"{doc_id}:3"]["error"]
    assert m["data"]["pages"][f"{doc_id}:4"]["status"] == "done"
    status, view = _status(client, doc_id)
    assert status == {1: "done", 2: "done", 3: "awaiting_transcription", 4: "done"}

    # and once more, successfully
    fails.calls.clear()
    fails.replies = []
    r = _resume(client, doc_id, mid)
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    assert _prompts_for_pages(fails) == [3]
    assert _read_message(client, cid)["data"]["documents"][0]["resumed"] == 2


def test_resume_of_one_document_leaves_the_others(cfg, keys, tmp_path, four_pages):
    """Two files in one message, both stopped by a kill: resuming one keeps the message partial."""
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    data = _read_message(client, cid)["data"]
    data["documents"].append({"document_id": doc_id + "x", "files": ["other.pdf"], "state": "interrupted",
                              "unread_pages": [1], "page_count": 1})
    data["files"] = [["four.pdf"], ["other.pdf"], ["later.pdf"]]
    app.store.update_message(mid, data=data)
    fake.calls.clear()
    r = _resume(client, doc_id, mid)
    assert events(client, r.json()["run_id"])[-1]["state"] == "partial"
    docs = _read_message(client, cid)["data"]["documents"]
    assert [d["state"] for d in docs] == ["done", "interrupted"]
    assert _read_message(client, cid)["data"]["files"][2] == ["later.pdf"]


def test_stopped_read_lists_files_that_never_started(cfg, keys, four_pages, tmp_path):
    second = images_to_pdf([text_page_image(["Other file"], size=(620, 877), font_size=22)], tmp_path / "b.pdf", dpi=100)
    gate = threading.Event()

    class StopAtFirst(SlowAgy):
        def run(self, prompt, **kw):
            gate.set()
            return super().run(prompt, **kw)

    slow = StopAtFirst(delay=30)
    client, app = _threaded_client(cfg, keys, slow)
    ids = [upload(client, p).json()["id"] for p in (four_pages, second)]
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": ids})
    assert gate.wait(20)
    client.post(f"/api/runs/{r.json()['run_id']}/cancel")
    assert events(client, r.json()["run_id"])[-1]["state"] == "cancelled"
    data = _read_message(client, cid)["data"]
    assert data["files"] == [["four.pdf"], ["b.pdf"]]
    assert [d["files"] for d in data["documents"]] == [["four.pdf"]]


def test_resume_rejects_a_foreign_message_or_document(cfg, keys, four_pages):
    client, app, fake, cid, mid = _stop_after_some_pages(cfg, keys, four_pages)
    doc_id = _document_id(client, cid)
    assert _resume(client, doc_id, "m_missing").status_code == 404
    assert _resume(client, "nope", mid).status_code == 404
    assert client.post(f"/api/documents/{doc_id}/resume", json={}).status_code == 404
    # the token check covers this route like every other
    assert client.post(f"/api/documents/{doc_id}/resume", json={"message_id": mid},
                       headers={"X-ScholarOCR-Token": "wrong"}).status_code == 403


def test_resume_with_a_key_engine_reads_only_unaccepted_pages(cfg, keys, four_pages):
    """Gemini engine: the job gets the unread pages only, so accepted ones keep their content."""
    from tests.fixtures.fake_gemini import FakeTransport

    keys.set("gemini", "fake-key-0123456789")
    transports: list = []

    def factory(engine, key):
        t = FakeTransport()
        transports.append(t)
        return t

    client, fake, app = make_client(cfg, keys, transport_factory=factory)
    up = upload(client, four_pages).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]], "engine": "gemini"})
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    doc_id = _document_id(client, cid)
    mid = r.json()["assistant_message"]["id"]
    svc = app.pool.get("gemini", app.prefs.load(), _read_message(client, cid)["data"]["model"])
    # page 3 failed in that read
    from scholarocr.pipeline.checkpoints import load_page, save_page
    from scholarocr.schemas import PageStatus

    page = load_page(svc.store, doc_id, 3)
    page.status, page.elements = PageStatus.FAILED, []
    save_page(svc.store, page)
    keep = load_page(svc.store, doc_id, 1).model_dump_json()
    calls_before = len(transports[0].calls) if hasattr(transports[0], "calls") else None
    r = _resume(client, doc_id, mid)
    assert r.status_code == 202, r.text
    assert events(client, r.json()["run_id"])[-1]["state"] == "done"
    assert load_page(svc.store, doc_id, 3).status == PageStatus.DONE
    assert load_page(svc.store, doc_id, 1).model_dump_json() == keep
    assert svc.repo.jobs_for_document(doc_id)[0].options["pages"] == "3"
    assert not fake.calls  # agy has no part in it
    if calls_before is not None:
        assert len(transports[0].calls) > calls_before


def test_page_shows_the_resume_action_and_its_failure_states():
    """The script carries the action and no longer tells people to attach the file for a resumable read."""
    js = (Path(__file__).parents[2] / "src/scholarocr/webapp/static/app.js").read_text(encoding="utf-8")
    assert "Resume reading" in js and "/resume`" in js and "Attach file" in js
    assert "attach the file again to read the rest" not in js
