"""The review pass in the web app: doubtful elements get a second look through agy, with agy mocked."""

from __future__ import annotations

import json
import threading
import pytest

from scholarocr.intelligence.responses import ReviewResponse
from scholarocr.intelligence.reviewer import apply_review, select_for_review
from scholarocr.pipeline.checkpoints import load_page, save_page
from scholarocr.schemas import ElementType, Uncertainty
from scholarocr.webapp import review as rv
from scholarocr.webapp.agy import AgyCancelled, AgyError, AgyRateLimited, AgyResult, AgyTimeout
from scholarocr.webapp.config import Prefs, PrefsStore
from scholarocr.webapp.view import document_view
from tests.webapp.conftest import events, make_client, upload

PARA_OK = "Force equals mass times acceleration."
PARA_DOUBT = "The velocty of the bal is constant in this region."
EQ_LATEX = r"v = u + at"


def flagged_page() -> dict:
    """A valid page: one clear paragraph, one doubtful, one very doubtful equation, one unreadable region."""
    return {
        "elements": [
            {"type": "paragraph", "text": PARA_OK, "uncertainty": "low", "box_2d": [50, 100, 90, 900]},
            {"type": "paragraph", "text": PARA_DOUBT, "uncertainty": "medium",
             "uncertainty_reasons": ["a word could differ"], "box_2d": [100, 100, 140, 900]},
            {"type": "equation", "text": "v = u + at", "latex": EQ_LATEX, "uncertainty": "high",
             "uncertainty_reasons": ["a guess among symbols"], "box_2d": [150, 300, 190, 700]},
            {"type": "uncertain", "text": "[illegible: 2 words]", "uncertainty": "unresolved", "box_2d": [200, 100, 240, 900]},
        ],
        "page_language": "en", "page_doubt": "low", "rotation_detected": 0, "notes": [],
    }


class ReviewAgy:
    """Page calls return ``page``; review calls return what ``verdicts(crops)`` builds. Records the review calls."""

    def __init__(self, verdicts=None, *, page=None, raises=None, open_crops=True):
        self.verdicts = verdicts
        self.page = page or flagged_page()
        self.raises = raises
        self.open_crops = open_crops
        self.review_calls: list[dict] = []
        self.page_calls: list[dict] = []
        self.lock = threading.Lock()

    def run(self, prompt, **kw):
        if "second-pass checker" not in prompt:
            with self.lock:
                self.page_calls.append({"prompt": prompt, **kw})
            return AgyResult("{}", json.loads(json.dumps(self.page)),
                             {"input_tokens": 20000, "output_tokens": 1500, "total_tokens": 21500}, 30.0, 31.0, "conv-page")
        with self.lock:
            self.review_calls.append({"prompt": prompt, "crops": sorted(p.name for p in kw["read_dirs"][0].iterdir()), **kw})
        if self.raises is not None:
            raise self.raises
        crops = sorted(p.name for p in kw["read_dirs"][0].iterdir())
        if self.open_crops and kw.get("on_event"):
            for name in crops:
                kw["on_event"]({"step_type": "tool", "tool_name": "read_file", "state": "ACTIVE",
                                "tool_info": {"name": "read_file", "parameters": {"Path": f"{kw['read_dirs'][0]}/{name}"}}})
        lines = [json.loads(ln) for ln in prompt.split("Candidates (JSON, one per line):\n", 1)[1].splitlines()]
        reply = self.verdicts(lines) if self.verdicts else {"reviews": [{"crop": c["crop"], "verdict": "confirmed"} for c in lines]}
        return AgyResult(json.dumps(reply), reply, {"input_tokens": 9000, "output_tokens": 200, "total_tokens": 9200}, 6.0, 7.0, "conv-review")


def verdicts_by_type(**by_type):
    def build(cands):
        out = []
        for c in cands:
            item = by_type.get(c["type"], {"verdict": "confirmed"})
            out.append({"crop": c["crop"], **item})
        return {"reviews": out}

    return build


@pytest.fixture
def scan(tmp_path):
    from tests.fixtures.corpus import images_to_pdf, text_page_image

    page = text_page_image(["Force equals mass times acceleration.", "The velocity of the ball is constant."],
                           size=(620, 877), font_size=22)
    return images_to_pdf([page], tmp_path / "one.pdf", dpi=100)


def read_doc(client, path):
    client.put("/api/prefs", json={"review_flagged": True})
    up = upload(client, path).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]})
    assert r.status_code == 202, r.text
    evs = events(client, r.json()["run_id"])
    doc_id = next(e for e in evs if e["type"] == "document_started")["document_id"]
    return doc_id, evs


def elements_of(client, doc_id, page=0):
    return client.get(f"/api/documents/{doc_id}").json()["pages"][page]["elements"]


def by_text(els, fragment):
    return next(e for e in els if fragment in (e["text"] or "") or fragment in (e.get("latex") or ""))


def test_nothing_is_reviewed_when_the_switch_is_off(cfg, keys, scan):
    agy = ReviewAgy()
    client, _, _ = make_client(cfg, keys, agy=agy)
    up = upload(client, scan).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]})
    evs = events(client, r.json()["run_id"])
    assert evs[-1]["state"] == "done"
    assert agy.review_calls == []
    assert not any(e.get("status") == "reviewing" for e in evs)


def test_the_setting_persists(cfg, keys):
    store = PrefsStore(cfg.prefs_path)
    assert Prefs().review_flagged is False
    assert store.update({"review_flagged": True}).review_flagged is True
    assert PrefsStore(cfg.prefs_path).load().review_flagged is True


def test_review_uses_the_existing_selection_and_only_the_crop_folder(cfg, keys, scan):
    agy = ReviewAgy()
    client, _, app = make_client(cfg, keys, agy=agy)
    doc_id, evs = read_doc(client, scan)
    assert evs[-1]["state"] == "done"
    assert len(agy.page_calls) == 1 and len(agy.review_calls) == 1
    call = agy.review_calls[0]
    svc = app.pool.reader(Prefs())
    page = load_page(svc.store, doc_id, 1)
    # same elements as the selection logic picks (the clear paragraph is not among them)
    assert PARA_OK not in call["prompt"] and PARA_DOUBT in call["prompt"] and EQ_LATEX in call["prompt"]
    assert len(call["crops"]) == 3 and call["crops"] == ["crop-1.png", "crop-2.png", "crop-3.png"]
    # the only readable folder is the crops; the page image folder of the read is not in it
    assert [p.name for p in call["read_dirs"]] == ["crops"]
    assert "-review" in str(call["run_dir"]) and call["read_dirs"][0].parent == call["run_dir"]
    # schema-validated output is asked for; stopping it uses the run's cancel event
    assert set(call["schema"]["properties"]) == {"reviews"} and call["cancel"] is not None
    assert "table" not in call["schema"]["properties"]["reviews"]["items"]["properties"]
    assert not call["run_dir"].exists()  # the crops are removed afterwards
    assert page.status.value == "done"
    # the stats are stored apart from the reading's, and the page total includes them
    msg = app.store.messages(client.get("/api/conversations").json()["conversations"][0]["id"])[-1]
    stats = msg["data"]["pages"][f"{doc_id}:1"]
    assert stats["review"]["flagged"] == 3 and stats["review"]["usage"]["total_tokens"] == 9200
    assert stats["usage"]["total_tokens"] == 21500 + 9200
    assert msg["data"]["usage"]["total_tokens"] == 21500 + 9200
    assert any(e.get("status") == "reviewing" and e.get("flagged") == 3 for e in evs)
    assert any(e.get("status") == "reviewed" for e in evs)
    assert any(e["type"] == "usage" and e.get("review") for e in evs)


def test_the_real_runner_sandboxes_the_review_and_reports_opened_crops(tmp_path, monkeypatch):
    """Through the real AgyRunner with a fake process, with agy's stream events as agy 1.4.12 writes them."""
    from tests.webapp.test_agy_runner import FakeProc, ev, result_line, runner

    monkeypatch.setenv("GEMINI_API_KEY", "should-not-pass")
    crops = tmp_path / "run" / "crops"
    crops.mkdir(parents=True)
    read = {"step_type": "tool", "tool_name": "read_file", "state": "ACTIVE",
            "tool_info": {"name": "read_file", "parameters": {"Path": f"{crops.as_posix()}/crop-2.png"}}}
    proc = FakeProc([ev("step_update", read), result_line(structured_output={"reviews": []})])
    r, popen = runner(tmp_path, proc)
    seen: set[str] = set()
    out = r.run("check", run_dir=tmp_path / "run", model="m", read_dirs=[crops],
                schema=rv.review_schema(with_table=False), on_event=rv._opened(seen))
    assert seen == {"crop-2.png"} and out.structured == {"reviews": []}
    cmd, kwargs = popen.calls[0]
    settings = json.loads((tmp_path / "run" / "agy-settings.json").read_text())
    assert settings == {"permissions": {"allow": [f"read_file({crops.resolve().as_posix()})"]}}
    assert "GEMINI_API_KEY" not in kwargs["env"]
    assert "--agent" in cmd and "--json-schema" in cmd
    assert "image file or files" in (tmp_path / "run" / ".agents" / "agents" / "scholarocr-reader.md").read_text()


def test_confirmed_clears_the_doubt_mark_even_from_high(cfg, keys, scan):
    """A confirmation lowers the stored label one step (high -> medium); the sheet shows no doubt either way."""
    agy = ReviewAgy()
    client, _, app = make_client(cfg, keys, agy=agy)
    doc_id, _ = read_doc(client, scan)
    view = client.get(f"/api/documents/{doc_id}").json()
    els = view["pages"][0]["elements"]
    para, eq = by_text(els, PARA_DOUBT), by_text(els, EQ_LATEX)
    assert para["uncertainty"] == "low" and para["mark"] == "low" and para["review"]["state"] == "confirmed"
    assert eq["uncertainty"] == "medium" and eq["mark"] == "low" and eq["review"]["state"] == "confirmed"
    assert "reasons" in para and not any(r.startswith("reviewed:") for r in para["reasons"])
    # the summary follows the marks; only the unreadable region is left
    assert view["uncertainty"]["medium"] == 0 and view["uncertainty"]["high"] == 0 and view["uncertainty"]["unresolved"] == 1
    assert view["reviews"]["confirmed"] == 3
    # it survives the verification that follows, and a fresh load (no rule raises it again)
    svc = app.pool.reader(Prefs())
    svc.verify(doc_id)
    again = client.get(f"/api/documents/{doc_id}").json()["pages"][0]["elements"]
    assert by_text(again, EQ_LATEX)["mark"] == "low"
    # the same rule as the issue that was flagged does not re-raise it either: nothing re-ran the rules
    assert load_page(svc.store, doc_id, 1).elements[2].uncertainty == Uncertainty.MEDIUM


def test_a_confirmation_the_model_doubts_settles_nothing(cfg, keys, scan):
    item = {"verdict": "confirmed", "uncertainty": "medium"}
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(paragraph=item)))
    doc_id, _ = read_doc(client, scan)
    para = by_text(elements_of(client, doc_id), PARA_DOUBT)
    assert para["uncertainty"] == "medium" and para["mark"] == "medium"
    assert para["review"]["state"] == "rejected" and "own doubt" in para["review"]["reason"]


def test_review_effort_is_the_questions_setting_not_the_reading_one(cfg, keys, scan):
    agy = ReviewAgy()
    client, _, _ = make_client(cfg, keys, agy=agy)
    client.put("/api/prefs", json={"read_effort": "low", "chat_effort": "medium"})
    read_doc(client, scan)
    assert agy.page_calls[0]["effort"] == "low"
    assert agy.review_calls[0]["effort"] == "medium" and agy.review_calls[0]["model"] == "gemini-3.8-flash"


def test_confirming_an_unreadable_region_keeps_its_mark(cfg, keys, scan):
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy())
    doc_id, _ = read_doc(client, scan)
    region = by_text(elements_of(client, doc_id), "illegible")
    assert region["mark"] == "unresolved" and region["review"]["state"] == "confirmed"


def test_correction_keeps_the_original_and_can_be_undone(cfg, keys, scan):
    fixed = "The velocity of the ball is constant in this region."
    agy = ReviewAgy(verdicts_by_type(paragraph={"verdict": "corrected", "text": fixed, "uncertainty": "low",
                                                "notes": ["velocity, ball"]},
                                     equation={"verdict": "corrected", "text": "v = u - at", "latex": "v = u - at",
                                               "uncertainty": "low"}))
    client, _, app = make_client(cfg, keys, agy=agy)
    doc_id, _ = read_doc(client, scan)
    els = elements_of(client, doc_id)
    para = by_text(els, fixed)
    assert para["review"]["state"] == "corrected" and para["review"]["original"]["text"] == PARA_DOUBT
    assert para["review"]["can_undo"] is True and para["review"]["notes"] == "velocity, ball"
    assert para["mark"] == "medium"  # two passes disagreed: the mark stays, with "changed" next to it
    eq = by_text(els, "v = u - at")
    assert eq["latex"] == "v = u - at" and eq["review"]["original"]["latex"] == EQ_LATEX
    # the corrected text reaches the exports' source of truth
    svc = app.pool.reader(Prefs())
    assert fixed in svc.get_content(doc_id, pages="1", fmt="markdown", include_ids=False, max_chars=9000)["content"]

    r = client.post(f"/api/documents/{doc_id}/elements/{para['id']}/undo-review")
    assert r.status_code == 200, r.text
    els = r.json()["view"]["pages"][0]["elements"]
    para = by_text(els, PARA_DOUBT)
    assert para["review"]["state"] == "undone" and para["review"]["undone"]["text"] == fixed
    assert para["uncertainty"] == "medium" and "can_undo" not in para["review"]
    assert para.get("reasons") == ["a word could differ"]
    # undoing twice, or an element with nothing to undo, is refused
    assert client.post(f"/api/documents/{doc_id}/elements/{para['id']}/undo-review").status_code == 400
    clear = by_text(els, PARA_OK)
    assert client.post(f"/api/documents/{doc_id}/elements/{clear['id']}/undo-review").status_code == 400
    # the equation's undo restores LaTeX and label
    r = client.post(f"/api/documents/{doc_id}/elements/{eq['id']}/undo-review").json()
    eq = by_text(r["view"]["pages"][0]["elements"], EQ_LATEX)
    assert eq["latex"] == EQ_LATEX and eq["uncertainty"] == "high" and eq["mark"] == "high"


def test_undo_needs_the_token_and_an_owned_document(cfg, keys, scan):
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy())
    assert client.post("/api/documents/doc_nothing/elements/x-p1-e1/undo-review").status_code == 404
    client.headers.pop("X-ScholarOCR-Token")
    assert client.post("/api/documents/doc_nothing/elements/x-p1-e1/undo-review").status_code in (401, 403)


@pytest.mark.parametrize("item,why", [
    ({"verdict": "corrected", "text": "v = u + at", "latex": r"v = \frac{u + at", "uncertainty": "low"}, "LaTeX problem"),
    ({"verdict": "corrected", "text": "v = u - at", "uncertainty": "low"}, "not the LaTeX"),
    ({"verdict": "corrected", "text": "v = u - at", "latex": "v = u - at", "uncertainty": "high"}, "own doubt"),
])
def test_bad_equation_corrections_are_rejected_and_the_original_stays(cfg, keys, scan, item, why):
    client, _, app = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(equation=item)))
    doc_id, evs = read_doc(client, scan)
    assert evs[-1]["state"] == "done"
    eq = by_text(elements_of(client, doc_id), EQ_LATEX)
    assert eq["latex"] == EQ_LATEX and eq["text"] == "v = u + at"
    assert eq["uncertainty"] == "high" and eq["mark"] == "high"  # still marked: nothing was settled
    assert eq["review"]["state"] == "rejected" and why in eq["review"]["reason"]
    assert client.get(f"/api/documents/{doc_id}").json()["reviews"]["rejected"] == 1


@pytest.mark.parametrize("text,why", [
    ("", "missing correction"),  # verdict without any correction: refused by validate_review
    ("The velocity is $x^{2$ in this region here today.", "unbalanced braces"),
    ("The velocity is $x in this region of the plane.", "unbalanced $"),
    ("Something else was written on this line entirely, unrelated to anything before.", "replaces most"),
])
def test_bad_text_corrections_are_rejected(cfg, keys, scan, text, why):
    item = {"verdict": "corrected", "text": text, "uncertainty": "low"}
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(paragraph=item)))
    doc_id, _ = read_doc(client, scan)
    para = by_text(elements_of(client, doc_id), PARA_DOUBT)
    assert para["text"] == PARA_DOUBT and para["uncertainty"] == "medium"
    assert para["review"]["state"] == "rejected" and why in para["review"]["reason"]


def test_a_reply_that_fails_the_schema_rejects_only_that_element(cfg, keys, scan):
    def build(cands):
        return {"reviews": [{"crop": c["crop"], "verdict": "maybe" if c["type"] == "paragraph" else "confirmed"} for c in cands]}

    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(build))
    doc_id, evs = read_doc(client, scan)
    els = elements_of(client, doc_id)
    assert by_text(els, PARA_DOUBT)["review"]["state"] == "rejected"
    assert "schema" in by_text(els, PARA_DOUBT)["review"]["reason"]
    assert by_text(els, EQ_LATEX)["review"]["state"] == "confirmed"
    assert evs[-1]["state"] == "done"


def test_a_correction_equal_to_the_reading_counts_as_confirmed(cfg, keys, scan):
    item = {"verdict": "corrected", "text": PARA_DOUBT + "  ", "uncertainty": "low"}
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(paragraph=item)))
    doc_id, _ = read_doc(client, scan)
    para = by_text(elements_of(client, doc_id), PARA_DOUBT)
    assert para["text"] == PARA_DOUBT and para["review"]["state"] == "confirmed" and para["mark"] == "low"


def test_unreadable_verdict_marks_it_and_can_be_undone(cfg, keys, scan):
    item = {"verdict": "unreadable", "uncertainty": "unresolved"}
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(paragraph=item)))
    doc_id, _ = read_doc(client, scan)
    para = by_text(elements_of(client, doc_id), PARA_DOUBT)
    assert para["text"] == PARA_DOUBT and para["mark"] == "unresolved" and para["review"]["state"] == "unreadable"
    r = client.post(f"/api/documents/{doc_id}/elements/{para['id']}/undo-review").json()
    para = by_text(r["view"]["pages"][0]["elements"], PARA_DOUBT)
    assert para["mark"] == "medium" and para["review"]["state"] == "undone"


def test_a_region_read_on_the_second_look_is_no_longer_unreadable_and_undo_restores_it(cfg, keys, scan):
    item = {"verdict": "corrected", "text": "two words", "uncertainty": "low"}
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(uncertain=item)))
    doc_id, _ = read_doc(client, scan)
    region = by_text(elements_of(client, doc_id), "two words")
    assert region["type"] == "paragraph" and region["mark"] == "medium" and region["review"]["state"] == "corrected"
    r = client.post(f"/api/documents/{doc_id}/elements/{region['id']}/undo-review").json()
    region = by_text(r["view"]["pages"][0]["elements"], "illegible")
    assert region["type"] == "uncertain" and region["mark"] == "unresolved"


def test_a_verdict_without_opening_the_crop_does_not_count(cfg, keys, scan):
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(open_crops=False))
    doc_id, evs = read_doc(client, scan)
    assert evs[-1]["state"] == "done"
    page = client.get(f"/api/documents/{doc_id}").json()["pages"][0]
    para = by_text(page["elements"], PARA_DOUBT)
    assert "review" not in para and para["mark"] == "medium"
    assert "no usable answer" in page["review_note"]


@pytest.mark.parametrize("error,state", [
    (AgyError("agy exited without a result"), "failed"),
    (AgyTimeout("agy did not finish within 180 s"), "failed"),
    (RuntimeError("boom"), "failed"),
])
def test_a_failed_review_never_fails_the_page(cfg, keys, scan, error, state):
    agy = ReviewAgy(raises=error)
    client, _, app = make_client(cfg, keys, agy=agy)
    doc_id, evs = read_doc(client, scan)
    assert evs[-1]["state"] == "done"
    assert not any(e["type"] == "error" for e in evs)
    assert len(agy.page_calls) == 1  # the page is not read again
    view = client.get(f"/api/documents/{doc_id}").json()
    page = view["pages"][0]
    assert page["status"] == "done" and "review_note" in page
    assert not any("review not finished" in w for w in page.get("warnings", []))
    para = by_text(page["elements"], PARA_DOUBT)
    assert para["mark"] == "medium" and "review" not in para
    assert not any(c["run_dir"].exists() for c in agy.review_calls)
    msg = app.store.messages(client.get("/api/conversations").json()["conversations"][0]["id"])[-1]
    assert msg["data"]["pages"][f"{doc_id}:1"]["review"]["state"] == state
    assert msg["data"]["pages"][f"{doc_id}:1"]["status"] == "done"


def test_a_reply_in_the_wrong_format_is_a_failed_review(cfg, keys, scan):
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(lambda cands: {"something": "else"}))
    doc_id, evs = read_doc(client, scan)
    assert evs[-1]["state"] == "done"
    assert "review format" in client.get(f"/api/documents/{doc_id}").json()["pages"][0]["review_note"]


def test_quota_on_a_review_throttles_the_reader_and_skips_later_reviews(cfg, keys, tmp_path):
    from tests.fixtures.corpus import images_to_pdf, text_page_image

    cfg.rate_limit_backoff_s = 0.2
    pages = [text_page_image([f"Page {i} text here."], size=(620, 877), font_size=22) for i in range(1, 4)]
    pdf = images_to_pdf(pages, tmp_path / "three.pdf", dpi=100)
    agy = ReviewAgy(raises=AgyRateLimited("agy reported FAILED: RESOURCE_EXHAUSTED"))
    client, _, app = make_client(cfg, keys, agy=agy)
    client.put("/api/prefs", json={"read_concurrency": 1})
    doc_id, evs = read_doc(client, pdf)
    assert evs[-1]["state"] == "done"  # every page accepted
    assert len(agy.page_calls) == 3
    assert len(agy.review_calls) == 1  # no more reviews after the quota message
    throttled = [e for e in evs if e["type"] == "stage" and e["stage"] == "throttled"]
    assert len(throttled) == 1 and "review" in throttled[0]["message"]
    view = client.get(f"/api/documents/{doc_id}").json()
    assert [p["status"] for p in view["pages"]] == ["done"] * 3
    assert all("review_note" in p for p in view["pages"])
    assert "exhausted quota" in view["pages"][0]["review_note"] and "earlier in this run" in view["pages"][1]["review_note"]


def test_stop_during_a_review_keeps_the_page_and_stops_the_run(cfg, keys, scan):
    client, _, app = make_client(cfg, keys, agy=None)
    agy = ReviewAgy()

    def run(prompt, **kw):
        if "second-pass checker" in prompt:
            kw["cancel"].set()  # the user pressed Stop while the review's agy runs
            agy.review_calls.append(kw)
            raise AgyCancelled("cancelled")
        return ReviewAgy.run(agy, prompt, **kw)

    agy.run = run
    app.runs.agy_factory = lambda: agy
    doc_id, evs = read_doc(client, scan)
    assert evs[-1]["state"] == "cancelled"
    assert len(agy.review_calls) == 1
    page = client.get(f"/api/documents/{doc_id}").json()["pages"][0]
    assert page["status"] == "done" and page["review_note"] == "stopped"
    assert by_text(page["elements"], PARA_DOUBT)["mark"] == "medium"


def test_reviews_share_the_page_limit(cfg, keys, tmp_path):
    """Reviews run in the page's worker, under its slot: never more agy processes than the setting."""
    import time

    from tests.fixtures.corpus import images_to_pdf, text_page_image

    pages = [text_page_image([f"Page {i} text here."], size=(620, 877), font_size=22) for i in range(1, 5)]
    pdf = images_to_pdf(pages, tmp_path / "four.pdf", dpi=100)

    class Counting(ReviewAgy):
        active = peak = 0

        def run(self, prompt, **kw):
            with self.lock:
                Counting.active += 1
                Counting.peak = max(Counting.peak, Counting.active)
            try:
                time.sleep(0.15)
                return super().run(prompt, **kw)
            finally:
                with self.lock:
                    Counting.active -= 1

    agy = Counting()
    client, _, app = make_client(cfg, keys, agy=agy)
    client.put("/api/prefs", json={"read_concurrency": 2})
    _, evs = read_doc(client, pdf)
    assert evs[-1]["state"] == "done"
    assert Counting.peak <= 2 and len(agy.review_calls) == 4
    assert app.runs.limiter.in_use == 0


def test_more_flagged_than_the_cap_only_reviews_the_selection(cfg, keys, scan):
    page = flagged_page()
    page["elements"] = [{"type": "paragraph", "text": f"Doubtful line number {i} of this page.", "uncertainty": "medium",
                         "box_2d": [20 + i * 40, 100, 50 + i * 40, 900]} for i in range(12)]
    agy = ReviewAgy(page=page)
    client, _, app = make_client(cfg, keys, agy=agy)
    doc_id, _ = read_doc(client, scan)
    assert len(agy.review_calls[0]["crops"]) == 8
    svc = app.pool.reader(Prefs())
    stored = load_page(svc.store, doc_id, 1)
    assert sum(1 for e in stored.elements if rv.review_info(e)) == 8
    # the rest stay unreviewed and marked
    assert sum(1 for e in client.get(f"/api/documents/{doc_id}").json()["pages"][0]["elements"] if e["mark"] == "medium") == 4


def test_list_corrections_replace_the_items_and_undo_restores_them(cfg, keys, scan):
    page = flagged_page()
    page["elements"] = [{"type": "list", "items": ["first itm", "second item"], "text": "", "uncertainty": "medium",
                         "box_2d": [100, 100, 200, 900]}]
    item = {"verdict": "corrected", "text": "first item\nsecond item", "uncertainty": "low"}
    agy = ReviewAgy(verdicts_by_type(list=item), page=page)
    client, _, _ = make_client(cfg, keys, agy=agy)
    doc_id, _ = read_doc(client, scan)
    assert "first itm" in agy.review_calls[0]["prompt"]
    el = elements_of(client, doc_id)[0]
    assert el["items"] == ["first item", "second item"] and el["review"]["original"]["text"] == "first itm\nsecond item"
    r = client.post(f"/api/documents/{doc_id}/elements/{el['id']}/undo-review").json()
    assert r["view"]["pages"][0]["elements"][0]["items"] == ["first itm", "second item"]


def test_table_corrections_are_checked_and_shown(cfg, keys, scan):
    page = flagged_page()
    cell = lambda s: {"text": s}  # noqa: E731
    page["elements"] = [{"type": "table", "text": "a b", "uncertainty": "medium", "box_2d": [100, 100, 300, 900],
                         "table": {"rows": [[cell("a"), cell("b")], [cell("1"), cell("2")]], "header_rows": 1}}]
    good = {"verdict": "corrected", "uncertainty": "low",
            "table": {"rows": [[cell("a"), cell("b")], [cell("1"), cell("7")]], "header_rows": 1}}
    agy = ReviewAgy(verdicts_by_type(table=good), page=page)
    client, _, _ = make_client(cfg, keys, agy=agy)
    doc_id, _ = read_doc(client, scan)
    assert "table" in agy.review_calls[0]["schema"]["properties"]["reviews"]["items"]["properties"]
    el = elements_of(client, doc_id)[0]
    assert el["table"]["rows"][1][1]["text"] == "7" and el["review"]["state"] == "corrected"
    assert "1 | 2" in el["review"]["original"]["table"]
    r = client.post(f"/api/documents/{doc_id}/elements/{el['id']}/undo-review").json()
    assert r["view"]["pages"][0]["elements"][0]["table"]["rows"][1][1]["text"] == "2"


def test_ragged_table_correction_is_rejected(cfg, keys, scan):
    page = flagged_page()
    cell = lambda s: {"text": s}  # noqa: E731
    page["elements"] = [{"type": "table", "text": "a b", "uncertainty": "medium", "box_2d": [100, 100, 300, 900],
                         "table": {"rows": [[cell("a"), cell("b")], [cell("1"), cell("2")]], "header_rows": 1}}]
    bad = {"verdict": "corrected", "uncertainty": "low", "table": {"rows": [[cell("a"), cell("b")], [cell("1")]], "header_rows": 1}}
    client, _, _ = make_client(cfg, keys, agy=ReviewAgy(verdicts_by_type(table=bad), page=page))
    doc_id, _ = read_doc(client, scan)
    el = elements_of(client, doc_id)[0]
    assert el["review"]["state"] == "rejected" and el["table"]["rows"][1][1]["text"] == "2"


def test_key_engine_reviews_show_and_undo_from_the_revisions(cfg, keys, scan):
    """The key engines' pipeline reviews by itself; those corrections have no snapshot but can still be undone."""
    client, _, app = make_client(cfg, keys, agy=ReviewAgy())
    doc_id, _ = read_doc(client, scan)
    svc = app.pool.reader(Prefs())
    page = load_page(svc.store, doc_id, 1)
    el = next(e for e in page.elements if e.content == PARA_OK)
    el.uncertainty = Uncertainty.HIGH
    apply_review(el, ReviewResponse(verdict="corrected", text="Force is mass times acceleration.", uncertainty="low"), by="review:gemini-x")
    save_page(svc.store, page)
    shown = by_text(elements_of(client, doc_id), "Force is mass")
    assert shown["review"]["state"] == "corrected" and shown["review"]["original"]["text"] == PARA_OK
    r = client.post(f"/api/documents/{doc_id}/elements/{el.element_id}/undo-review").json()
    back = by_text(r["view"]["pages"][0]["elements"], PARA_OK)
    assert back["uncertainty"] == "high" and back["review"]["state"] == "undone"


def test_reviewed_elements_are_not_selected_again_by_host_candidates(cfg, keys, scan):
    client, _, app = make_client(cfg, keys, agy=ReviewAgy())
    doc_id, _ = read_doc(client, scan)
    svc = app.pool.reader(Prefs())
    assert svc.host_review_candidates(doc_id) == []
    page = load_page(svc.store, doc_id, 1)
    assert select_for_review([e for e in page.elements if e.type == ElementType.PARAGRAPH], detail="standard") == []


def test_prompt_lists_every_crop_and_the_rules():
    from scholarocr.webapp.prompts import review_prompt

    p = review_prompt("C:/x/crops", [{"crop": "crop-1.png", "type": "equation", "text": "a", "latex": "a"},
                                     {"crop": "crop-2.png", "type": "paragraph", "handwritten": True, "text": "b"}])
    assert "C:/x/crops/crop-1.png" in p and "C:/x/crops/crop-2.png" in p
    assert "writer's own spelling" in p and "handwritten word" in p and "LaTeX correctness" in p
    assert "\\ce{...}" in p


def test_document_view_without_reviews_is_unchanged(cfg, keys, scan):
    client, _, app = make_client(cfg, keys, agy=ReviewAgy())
    up = upload(client, scan).json()
    cid = client.post("/api/conversations").json()["id"]
    run_id = client.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]}).json()["run_id"]
    evs = events(client, run_id)
    doc_id = next(e for e in evs if e["type"] == "document_started")["document_id"]
    view = document_view(app.pool.reader(Prefs()), doc_id)
    para = by_text(view["pages"][0]["elements"], PARA_DOUBT)
    assert "review" not in para and para["mark"] == "medium" and view["uncertainty"]["medium"] == 1
    assert view["reviews"]["confirmed"] == 0
