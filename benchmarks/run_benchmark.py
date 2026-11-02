"""Benchmark ScholarOCR against ground truth built from the synthetic corpus.

    python benchmarks/run_benchmark.py            # deterministic (text-layer) documents, no API key needed
    python benchmarks/run_benchmark.py --live     # also scanned/visual documents (real Gemini calls)

The corpus is synthetic, so these numbers describe behaviour on clean generated documents only.
They are NOT an accuracy claim for real-world scans; use `scholarocr evaluate` with your own
manually verified ground truth for that.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scholarocr.config import Settings  # noqa: E402
from scholarocr.evaluation.metrics import evaluate_path  # noqa: E402
from scholarocr.service import ScholarOCR  # noqa: E402
from tests.fixtures import corpus as C  # noqa: E402

DETERMINISTIC = {
    "clean_digital.pdf": {"pages": [
        {"page_number": 1, "text": "Introduction to Algebra Chapter 1: Quadratic Equations A quadratic equation is an "
         "equation of the second degree, meaning it contains at least one term that is squared. The standard form is "
         "written with real coefficients. 1. Solve the equation number 11 completely. [2] 2. Solve the equation "
         "number 12 completely. [3] 3. Solve the equation number 13 completely. [5]",
         "elements": [{"type": "title", "text": "Introduction to Algebra"},
                      {"type": "heading", "text": "Chapter 1: Quadratic Equations"}]},
        {"page_number": 2, "text": "Chapter 2: Quadratic Equations A quadratic equation is an equation of the second "
         "degree, meaning it contains at least one term that is squared. The standard form is written with real "
         "coefficients. 1. Solve the equation number 21 completely. [2] 2. Solve the equation number 22 completely. "
         "[3] 3. Solve the equation number 23 completely. [5]"}]},
    "tables.pdf": {"pages": [{"page_number": 1, "text": "Table 1: Periodic properties Element Symbol Atomic number "
                              "Hydrogen H 1 Helium He 2 Lithium Li 3",
                              "tables": [[["Element", "Symbol", "Atomic number"], ["Hydrogen", "H", "1"],
                                          ["Helium", "He", "2"], ["Lithium", "Li", "3"]]],
                              "elements": [{"type": "caption", "text": "Table 1: Periodic properties"}]}]},
    "multilingual.pdf": {"pages": [{"page_number": 1, "text": "Théorème de Pythagore: le carré de l'hypoténuse. "
                                    "Θεώρημα: άθροισμα γωνιών τριγώνου. Теорема: сумма углов треугольника равна 180°."}]},
    "research_paper.pdf": {"pages": [{"page_number": 1, "elements": [
        {"type": "title", "text": "Spectral Methods for Sparse Graphs"},
        {"type": "heading", "text": "1 Introduction"}, {"type": "heading", "text": "2 Results"},
        {"type": "bibliography_entry", "text": "[1] F. Chung. Spectral Graph Theory. AMS, 1997."}]}]},
}

VISUAL = {
    "math_paper.pdf": {"options": {"context_hint": "mathematics exam paper"}, "pages": [
        {"page_number": 1, "text": "Mathematics — Paper I " + " ".join(C.MATH_LINES),
         "equations": ["x^2 + 5x + 6 = 0", r"\int_0^1 x^2 \, dx"]}]},
    "chemistry.png": {"pages": [{"page_number": 1, "text": "Chemistry — Class 11 " + " ".join(C.CHEM_LINES)}]},
    "scanned_poor.pdf": {"pages": [{"page_number": 1, "text": "Biology Notes The mitochondrion is the powerhouse of "
                                    "the cell. It produces ATP through respiration. Glycolysis occurs in the cytoplasm."}]},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="include visual (Gemini) documents")
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "results"))
    ap.add_argument("--only", nargs="*", help="limit to these corpus documents (e.g. math_paper.pdf)")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="scholarocr-bench-"))
    files = C.build_corpus(work / "corpus")
    gt_dir = work / "gt"
    gt_dir.mkdir()
    sets = dict(DETERMINISTIC)
    if args.live:
        sets.update(VISUAL)
    if args.only:
        sets = {k: v for k, v in sets.items() if k in set(args.only)}
    for name, gt in sets.items():
        (gt_dir / f"{name}.json").write_text(json.dumps({"source": str(files[name]), **gt}), encoding="utf-8")
    settings = Settings.from_env()
    settings.data_dir = work / "data"
    settings.allowed_dirs = [work]
    if args.live and not settings.api_key:
        print(f"--live requires {settings.api_key_env_name} (provider={settings.provider})", file=sys.stderr)
        return 2
    t0 = time.time()
    report = evaluate_path(ScholarOCR(settings), gt_dir)
    report["elapsed_s"] = round(time.time() - t0, 2)
    report["mode"] = "live" if args.live else "deterministic"
    report["models"] = {"provider": settings.provider, "primary": settings.gemini_model,
                        "review": settings.gemini_review_model} if args.live else None
    report["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"benchmark-{report['mode']}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{'document':28s} {'CER':>6s} {'WER':>6s} {'tbl':>5s} {'cls':>5s} {'trace':>5s}")
    for d in report["documents"]:
        m = d["metrics"]
        f = lambda v: "  -  " if v is None else f"{v:5.3f}"  # noqa: E731
        print(f"{d['ground_truth']:28s} {f(m['cer'])} {f(m['wer'])} {f(m['table_cell_accuracy'])} "
              f"{f(m['element_classification_accuracy'])} {f(m['traceability'])}")
    print(f"\naggregate: {report['aggregate_mean']}\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
