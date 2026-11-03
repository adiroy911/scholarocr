"""A scripted stand-in for the Gemini API used by default in tests (no network, no key)."""

from __future__ import annotations

import json
from typing import Any, Callable

from scholarocr.intelligence.gemini_client import ImagePart, RawResponse, TextPart


class APIErrorLike(Exception):
    def __init__(self, code: int, status: str = ""):
        super().__init__(f"{code} {status}")
        self.code = code
        self.status = status


def default_page(text: str = "Q1. Solve x^2 + 5x + 6 = 0. [2]") -> dict:
    return {
        "page_language": "en",
        "page_label": "1",
        "page_doubt": "low",
        "elements": [
            {"type": "heading", "text": "Mathematics — Paper I", "level": 1, "box_2d": [60, 80, 110, 700]},
            {"type": "paragraph", "role": "question", "text": text, "question": {"number": "1", "marks": "2"},
             "box_2d": [130, 80, 170, 900]},
            {"type": "equation", "text": "x^2 + 5x + 6 = 0", "latex": "x^2 + 5x + 6 = 0", "box_2d": [180, 300, 220, 700],
             "uncertainty": "low"},
            {"type": "equation", "text": "int_0^1 x^2 dx", "latex": "\\int_0^1 x^2 \\, dx = \\frac{1}{3",
             "box_2d": [230, 300, 280, 700], "uncertainty": "low"},
            {"type": "table", "text": "", "box_2d": [300, 80, 450, 900], "table": {
                "header_rows": 1, "caption": "Values",
                "rows": [[{"text": "x", "is_header": True}, {"text": "f(x)", "is_header": True}],
                         [{"text": "1"}, {"text": "2"}]]}},
            {"type": "figure", "text": "", "box_2d": [470, 200, 700, 800],
             "figure": {"description": "A parabola opening upwards", "labels": ["y", "x"], "kind": "graph"}},
            {"type": "caption", "text": "Fig. 1: Graph of f", "caption_of": 5, "box_2d": [705, 300, 730, 700]},
            {"type": "uncertain", "text": "[illegible: 3 words]", "uncertainty": "unresolved", "box_2d": [750, 80, 780, 500]},
            {"type": "page_number", "text": "1", "box_2d": [950, 480, 970, 520]},
        ],
    }


class FakeTransport:
    """``responder(purpose_hint, parts, call_index) -> dict | str | Exception``.

    Default responder: page extraction → ``default_page()``, review → confirmed, classify → exam paper.
    """

    def __init__(self, responder: Callable[[str, list, int], Any] | None = None):
        self.calls: list[dict] = []
        self.responder = responder or self.default_responder

    @staticmethod
    def kind(system: str) -> str:
        if system.startswith("You are a second-pass verifier"):
            return "review"
        if system.startswith("You classify"):
            return "classify"
        return "extract"

    @staticmethod
    def default_responder(kind: str, parts: list, idx: int):
        if kind == "review":
            return {"verdict": "confirmed", "text": "", "uncertainty": "low"}
        if kind == "classify":
            return {"document_type": "exam_paper", "subjects": ["mathematics"], "certainty": "medium",
                    "rationale": "numbered questions with marks", "hint_consistent": True, "evidence_pages": [1]}
        return default_page()

    def generate(self, *, model, system, parts, response_schema, temperature, media_resolution, timeout_s):
        kind = self.kind(system)
        self.calls.append({"model": model, "kind": kind, "n_images": sum(isinstance(p, ImagePart) for p in parts),
                           "text": "\n".join(p.text for p in parts if isinstance(p, TextPart)),
                           "schema": response_schema, "media_resolution": media_resolution})
        out = self.responder(kind, parts, len(self.calls) - 1)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, RawResponse):
            return out
        text = out if isinstance(out, str) else json.dumps(out)
        return RawResponse(text=text, finish_reason="STOP",
                           usage={"prompt_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}, model_version=model)
