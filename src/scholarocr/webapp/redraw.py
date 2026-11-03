"""The "Redraw" action for one figure in the web app: opt-in, one model call, two when the first copy fails.

The redraw is a faithful vector copy, checked by the server (``pipeline/svg_fidelity``): a copy that is not
faithful gets one retry with the check's specific feedback; one that fails again is stored but marked "not
faithful", and the app keeps showing the cleaned crop.

Antigravity: ``agy -p`` reads the figure's cleaned crop (the only readable folder) and replies with an SVG.
Key engines: the service's ``redraw_figure`` (structured calls). Either way the SVG goes through
``ScholarOCR.submit_figure_svg``: strict sanitising, a render check, the fidelity check; the crop never changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from ..exceptions import InputError
from ..intelligence.redraw import MAX_REDRAW_ATTEMPTS, redraw_brief
from ..service import ScholarOCR
from ..svg_sanitize import extract_svg
from .agy import AgyError, AgyRunner, model_for_effort
from .engines import EngineStatus

REDRAW_PREAMBLE = """You are the drawing model for the ScholarOCR desktop app.
Step 1: read exactly one file with read_file:
{image_path}
That is the only tool call you may make. Shell commands, scripts, code execution, file edits, browsing, MCP
tools and every other tool are denied in this run and will fail, so do not try them.
Step 2: reply with ONLY the SVG document, from <svg to </svg>. No prose, no code fence."""


def agy_prompt(image_path: str, brief: str) -> str:
    return REDRAW_PREAMBLE.format(image_path=image_path) + "\n\n" + brief


def redraw(svc: ScholarOCR, document_id: str, element_id: str, *, engine: EngineStatus, model: str | None,
           key_service: Callable[[], ScholarOCR] | None, agy_factory: Callable[[], AgyRunner | None],
           runs_dir: Path, timeout_s: float, effort: str | None = None) -> dict[str, Any]:
    if engine.id != "antigravity":
        if key_service is None:
            raise InputError(f"{engine.label} cannot redraw figures")
        return key_service().redraw_figure(document_id, element_id, model=model)
    runner = agy_factory()
    if runner is None:
        raise AgyError("Antigravity CLI (agy) is not on PATH")
    data, info = svc.figure_redraw_request(document_id, element_id)
    _, el = svc.find_element(document_id, element_id)
    size = (int(info["image_size_px"][0]), int(info["image_size_px"][1]))
    run_dir = runs_dir / f"redraw-{element_id}"
    model_arg, effort_arg = model_for_effort(model, effort, [m["id"] for m in engine.models])
    img_dir = run_dir / "figure"
    img_dir.mkdir(parents=True, exist_ok=True)
    usage: dict[str, int] = {}
    feedback: list[str] | None = None
    history: list[dict[str, Any]] = []  # the check's verdict on every attempt (a refused first copy is not stored)
    last: dict[str, Any] | None = None
    try:
        image = img_dir / "figure.png"
        image.write_bytes(data)
        ref = str(image.resolve()).replace("\\", "/")
        for attempt in range(MAX_REDRAW_ATTEMPTS):
            try:
                res = runner.run(agy_prompt(ref, redraw_brief(el, size, feedback)), run_dir=run_dir,
                                 model=model_arg, read_dirs=[img_dir], schema=None, timeout_s=timeout_s,
                                 effort=effort_arg)
            except AgyError as e:
                if last is None:
                    raise
                # the retry failed (timeout, quota): report the first attempt's verdict instead of losing it
                return {**last, "usage": usage, "attempts": attempt + 1, "history": history,
                        "retry_error": e.message}
            for k, v in res.usage.items():
                usage[k] = usage.get(k, 0) + v
            svg = extract_svg(res.response)
            try:
                if svg is None:
                    raise InputError("the reply contained no <svg>...</svg>")
                out = svc.submit_figure_svg(document_id, element_id, svg, by=f"antigravity:{model or 'default'}")
            except InputError as e:  # unsafe, unrenderable or missing SVG: refused before any fidelity check
                if attempt == MAX_REDRAW_ATTEMPTS - 1:
                    raise InputError(f"the redrawn figure was refused twice: {e.message}") from None
                feedback = [f"the SVG was refused: {e.message}"]
                continue
            out["usage"], out["attempts"] = usage, attempt + 1
            fid = out.get("fidelity") or {}
            history.append({k: fid.get(k) for k in ("attempt", "score", "recall", "precision", "passed",
                                                     "labels_missing", "missing_regions", "extra_regions",
                                                     "tolerance_px", "size")})
            out["history"] = history
            if out["accepted"] or not out.get("retry_allowed"):
                return out
            last = out
            feedback = out["feedback"]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    raise InputError("redraw failed")  # pragma: no cover
