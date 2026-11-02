"""Evaluation against manually verified ground truth.

Ground-truth file format (JSON)::

    {
      "source": "relative/or/absolute/path.pdf",       # or a list of paths (one document)
      "options": {"context_hint": "...", "document_type": "exam_paper"},   # optional
      "pages": [
        {"page_number": 1,
         "text": "full page text in reading order (furniture excluded)",
         "equations": ["x^2 + 5x + 6 = 0"],
         "tables": [[["Element", "Symbol"], ["Hydrogen", "H"]]],
         "elements": [{"type": "heading", "text": "Chapter 1"}]}
      ]
    }

Metrics: character/word error rate, math expression accuracy (normalised LaTeX exact match and
mean similarity), table structure accuracy (shape and cell accuracy), page completeness, element
classification accuracy and source traceability. Nothing here claims an accuracy figure beyond
what the supplied ground truth supports.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..schemas import Document, ElementType, PageStatus, ProcessingOptions
from ..schemas.elements import FURNITURE_TYPES


def levenshtein(a: list | str, b: list | str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return " ".join(s.split())


def cer(reference: str, hypothesis: str) -> float | None:
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        return None
    return levenshtein(ref, hyp) / len(ref)


def wer(reference: str, hypothesis: str) -> float | None:
    ref, hyp = normalize_text(reference).split(), normalize_text(hypothesis).split()
    if not ref:
        return None
    return levenshtein(ref, hyp) / len(ref)


def normalize_latex(s: str) -> str:
    s = re.sub(r"\\(left|right|displaystyle|,|;|!|:|quad|qquad)(?![a-zA-Z])", "", s)
    s = re.sub(r"\\(mathrm|text|operatorname)\{([^{}]*)\}", r"\2", s)
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    return re.sub(r"\s+", "", s)


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return 1 - levenshtein(a, b) / max(len(a), len(b))


def page_text(doc: Document, page_number: int) -> str:
    page = next((p for p in doc.pages if p.page_number == page_number), None)
    if page is None:
        return ""
    parts = []
    for el in sorted(page.elements, key=lambda e: e.reading_order):
        if el.type in FURNITURE_TYPES or el.type == ElementType.UNCERTAIN:
            continue
        parts.append(el.text_for_search() if el.type == ElementType.TABLE else (el.content or el.latex or ""))
    return "\n".join(p for p in parts if p)


def evaluate_document(doc: Document, gt: dict, doc_dir: Path | None = None) -> dict[str, Any]:
    pages_out = []
    cer_num = cer_den = wer_num = wer_den = 0
    eq_total = eq_exact = 0
    eq_sims: list[float] = []
    tb_total = tb_shape = 0
    cell_total = cell_ok = 0
    cls_total = cls_ok = 0
    gt_pages = gt.get("pages", [])
    processed = {p.page_number for p in doc.pages if p.status in (PageStatus.DONE, PageStatus.BLANK)}
    for gp in gt_pages:
        n = gp["page_number"]
        row: dict[str, Any] = {"page_number": n, "processed": n in processed}
        hyp = page_text(doc, n)
        if "text" in gp:
            ref = normalize_text(gp["text"])
            c_err = levenshtein(ref, normalize_text(hyp))
            w_ref = ref.split()
            w_err = levenshtein(w_ref, normalize_text(hyp).split())
            cer_num += c_err
            cer_den += len(ref)
            wer_num += w_err
            wer_den += len(w_ref)
            row["cer"] = round(c_err / len(ref), 4) if ref else None
            row["wer"] = round(w_err / len(w_ref), 4) if w_ref else None
        page = next((p for p in doc.pages if p.page_number == n), None)
        elements = page.elements if page else []
        preds_math = [normalize_latex(e.latex or e.content) for e in elements
                      if e.type in (ElementType.EQUATION, ElementType.CHEMICAL_EQUATION)]
        for eq in gp.get("equations", []):
            eq_total += 1
            target = normalize_latex(eq)
            best = max((similarity(target, p) for p in preds_math), default=0.0)
            eq_sims.append(best)
            eq_exact += best == 1.0
        pred_tables = [e.table.as_grid() for e in elements if e.type == ElementType.TABLE and e.table]
        for gt_table in gp.get("tables", []):
            tb_total += 1
            cells = [normalize_text(c) for r in gt_table for c in r]
            cell_total += len(cells)
            best_ok, best_shape = 0, False
            for pt in pred_tables:
                shape = len(pt) == len(gt_table) and all(len(a) == len(b) for a, b in zip(pt, gt_table))
                ok = sum(1 for r, gr in zip(pt, gt_table) for c, gc in zip(r, gr) if normalize_text(c) == normalize_text(gc))
                if (ok, shape) > (best_ok, best_shape):
                    best_ok, best_shape = ok, shape
            cell_ok += best_ok
            tb_shape += best_shape
        for ge in gp.get("elements", []):
            cls_total += 1
            target = normalize_text(ge["text"])
            best, best_sim = None, 0.0
            for e in elements:
                sim = similarity(target, normalize_text(e.content))
                if sim > best_sim:
                    best, best_sim = e, sim
            if best is not None and best_sim >= 0.6 and best.type.value == ge["type"]:
                cls_ok += 1
        pages_out.append(row)

    els = list(doc.iter_elements())
    traceable = 0
    for e in els:
        ok = e.source.document_id == doc.document_id and e.source.page_number == e.page_number
        if doc_dir is not None and e.source.page_image:
            ok = ok and (doc_dir / e.source.page_image).exists()
        if e.source.region_image and doc_dir is not None:
            ok = ok and (doc_dir / e.source.region_image).exists()
        traceable += ok

    def ratio(a, b):
        return round(a / b, 4) if b else None

    return {
        "document_id": doc.document_id,
        "pages": pages_out,
        "metrics": {
            "cer": ratio(cer_num, cer_den),
            "wer": ratio(wer_num, wer_den),
            "math_exact_match": ratio(eq_exact, eq_total),
            "math_mean_similarity": round(sum(eq_sims) / len(eq_sims), 4) if eq_sims else None,
            "table_shape_accuracy": ratio(tb_shape, tb_total),
            "table_cell_accuracy": ratio(cell_ok, cell_total),
            "page_completeness": ratio(sum(1 for gp in gt_pages if gp["page_number"] in processed), len(gt_pages)),
            "element_classification_accuracy": ratio(cls_ok, cls_total),
            "traceability": ratio(traceable, len(els)),
        },
        "counts": {"pages": len(gt_pages), "equations": eq_total, "tables": tb_total, "cells": cell_total,
                   "classified_elements": cls_total, "elements": len(els)},
    }


def evaluate_path(svc, path: Path) -> dict[str, Any]:
    """Process (cached) and evaluate each ground-truth file under ``path``."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    results = []
    for f in files:
        gt = json.loads(f.read_text(encoding="utf-8"))
        sources = gt["source"] if isinstance(gt["source"], list) else [gt["source"]]
        resolved = [str((f.parent / s).resolve()) if not Path(s).is_absolute() else s for s in sources]
        opts = ProcessingOptions(**gt.get("options", {}))
        started = svc.process(resolved, opts, wait=True)
        doc = svc.get_document(started["document_id"])
        res = evaluate_document(doc, gt, svc.store.doc_dir(doc.document_id))
        res["ground_truth"] = f.name
        results.append(res)
    keys = ["cer", "wer", "math_exact_match", "math_mean_similarity", "table_shape_accuracy", "table_cell_accuracy",
            "page_completeness", "element_classification_accuracy", "traceability"]
    aggregate = {}
    for k in keys:
        vals = [r["metrics"][k] for r in results if r["metrics"][k] is not None]
        aggregate[k] = round(sum(vals) / len(vals), 4) if vals else None
    return {"documents": results, "aggregate_mean": aggregate, "n_documents": len(results)}
