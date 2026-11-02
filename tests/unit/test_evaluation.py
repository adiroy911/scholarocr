import json

from scholarocr.evaluation.metrics import cer, evaluate_path, levenshtein, normalize_latex, wer
from scholarocr.service import ScholarOCR


def test_basic_metrics():
    assert levenshtein("kitten", "sitting") == 3
    assert cer("abcd", "abcd") == 0 and cer("abcd", "abxd") == 0.25
    assert wer("the cat sat", "the cat sat down") == 1 / 3
    assert cer("", "x") is None
    assert normalize_latex(r"\left( \dfrac{a}{b} \right)") == normalize_latex(r"(\frac{a}{b})")


def test_evaluate_ground_truth_on_digital_pdf(settings, corpus, tmp_path):
    gt = {
        "source": str(corpus["tables.pdf"]),
        "pages": [{"page_number": 1, "text": "Table 1: Periodic properties Element Symbol Atomic number Hydrogen H 1 "
                   "Helium He 2 Lithium Li 3",
                   "tables": [[["Element", "Symbol", "Atomic number"], ["Hydrogen", "H", "1"], ["Helium", "He", "2"],
                               ["Lithium", "Li", "3"]]],
                   "elements": [{"type": "caption", "text": "Table 1: Periodic properties"}]}],
    }
    f = tmp_path / "gt.json"
    f.write_text(json.dumps(gt))
    report = evaluate_path(ScholarOCR(settings), f)
    m = report["documents"][0]["metrics"]
    assert m["table_shape_accuracy"] == 1.0 and m["table_cell_accuracy"] == 1.0
    assert m["page_completeness"] == 1.0 and m["traceability"] == 1.0
    assert m["cer"] < 0.05 and m["element_classification_accuracy"] == 1.0
