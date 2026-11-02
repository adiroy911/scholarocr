"""Programmatic test corpus. Generated at test time so no binaries live in git.

These are *synthetic* stand-ins for the 15 required categories. They exercise ingestion,
routing, preprocessing, deterministic extraction and export. They are not a substitute for
real scanned academic documents in accuracy benchmarks (see tests/evaluation/README.md).
"""

from __future__ import annotations

import io
import random
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIRS = [str(Path(__file__).parent / "fonts"), "/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts", "/Library/Fonts", "/System/Library/Fonts"]


def _font_path(name: str) -> str | None:
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return str(p)
    return None


SANS = _font_path("DejaVuSans.ttf")
SERIF_IT = _font_path("DejaVuSerif-Italic.ttf") or SANS


def _font(size: int, path: str | None = SANS):
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


# ---------------------------------------------------------------- digital PDFs
def clean_digital_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    for n in (1, 2):
        p = doc.new_page(width=595, height=842)
        if n == 1:
            p.insert_text((72, 90), "Introduction to Algebra", fontsize=22, fontname="hebo")
        p.insert_text((72, 140), f"Chapter {n}: Quadratic Equations", fontsize=16, fontname="hebo")
        y = 180
        para = ("A quadratic equation is an equation of the second degree, meaning it contains at least "
                "one term that is squared. The standard form is written with real coefficients.")
        p.insert_textbox(pymupdf.Rect(72, y, 523, y + 60), para, fontsize=11)
        y += 80
        for q, marks in ((1, 2), (2, 3), (3, 5)):
            p.insert_text((72, y), f"{q}. Solve the equation number {q + n * 10} completely. [{marks}]", fontsize=11)
            y += 22
        p.insert_text((290, 815), str(n), fontsize=9)
    doc.save(path)
    return path


def table_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 80), "Table 1: Periodic properties", fontsize=13, fontname="hebo")
    rows = [["Element", "Symbol", "Atomic number"], ["Hydrogen", "H", "1"], ["Helium", "He", "2"], ["Lithium", "Li", "3"]]
    y = 110
    for r in rows:
        for c, cell in enumerate(r):
            p.insert_text((72 + c * 150, y), cell, fontsize=11)
        y += 22
    for i in range(len(rows) + 1):
        p.draw_line((68, 96 + i * 22), (520, 96 + i * 22))
    doc.save(path)
    return path


def research_paper_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((120, 70), "Spectral Methods for Sparse Graphs", fontsize=18, fontname="hebo")
    p.insert_text((200, 95), "A. Author, B. Researcher", fontsize=11)
    p.insert_textbox(pymupdf.Rect(72, 110, 523, 170),
                     "Abstract. We study spectral partitioning on sparse random graphs and prove a bound on the "
                     "second eigenvalue of the normalized Laplacian.", fontsize=10)
    body = ("Sparse graphs arise in many applications. Spectral methods use eigenvectors of the Laplacian to "
            "find good partitions. ") * 4
    p.insert_text((72, 190), "1 Introduction", fontsize=13, fontname="hebo")
    p.insert_textbox(pymupdf.Rect(72, 200, 290, 520), body, fontsize=9)
    p.insert_text((305, 190), "2 Results", fontsize=13, fontname="hebo")
    p.insert_textbox(pymupdf.Rect(305, 200, 523, 520), body, fontsize=9)
    p.insert_text((72, 560), "References", fontsize=13, fontname="hebo")
    p.insert_text((72, 580), "[1] F. Chung. Spectral Graph Theory. AMS, 1997.", fontsize=9)
    doc.save(path)
    return path


def multilingual_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    lines = ["Théorème de Pythagore: le carré de l'hypoténuse.", "Θεώρημα: άθροισμα γωνιών τριγώνου.",
             "Теорема: сумма углов треугольника равна 180°."]
    y = 90
    for ln in lines:
        p.insert_text((72, y), ln, fontsize=12, fontname="dejavu", fontfile=SANS)
        y += 26
    doc.save(path)
    return path


# ---------------------------------------------------------------- raster pages
def text_page_image(lines: list[str], *, size=(1240, 1754), font_path=SANS, font_size=34, title=None) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 120
    if title:
        d.text((100, y), title, fill="black", font=_font(font_size + 14, font_path))
        y += 110
    for ln in lines:
        d.text((100, y), ln, fill="black", font=_font(font_size, font_path))
        y += int(font_size * 1.8)
    return img


def degrade(img: Image.Image, seed: int = 0) -> Image.Image:
    rng = np.random.default_rng(seed)
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    arr = 70 + arr * 0.55  # low contrast, grey paper
    arr += rng.normal(0, 18, arr.shape)
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    return out.convert("RGB")


def images_to_pdf(images: list[Image.Image], path: Path, dpi: int = 150) -> Path:
    doc = pymupdf.open()
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        w, h = im.width * 72 / dpi, im.height * 72 / dpi
        p = doc.new_page(width=w, height=h)
        p.insert_image(p.rect, stream=buf.getvalue())
    doc.save(path)
    return path


MATH_LINES = ["Q1. Solve x² + 5x + 6 = 0.  [2]", "Q2. Evaluate ∫₀¹ x² dx.  [3]", "Q3. Find lim (sin x)/x as x → 0.  [3]",
              "     (a) Show that √2 is irrational.  [2]"]
CHEM_LINES = ["1. Balance: H₂ + O₂ → H₂O  [2]", "2. N₂ + 3H₂ ⇌ 2NH₃ (Haber process)  [3]", "3. Write the charge on SO₄²⁻.  [1]"]


def scanned_math_pdf(path: Path) -> Path:
    return images_to_pdf([degrade(text_page_image(MATH_LINES, title="Mathematics — Paper I"), 1)], path)


def scanned_poor_pdf(path: Path) -> Path:
    lines = ["The mitochondrion is the powerhouse of the cell.", "It produces ATP through respiration.",
             "Glycolysis occurs in the cytoplasm."]
    return images_to_pdf([degrade(text_page_image(lines, title="Biology Notes"), 2)], path)


def chemistry_png(path: Path) -> Path:
    text_page_image(CHEM_LINES, title="Chemistry — Class 11").save(path, dpi=(150, 150))
    return path


def rotated_png(path: Path) -> Path:
    text_page_image(["Rotated page sample text line one.", "Second line of rotated text.",
                     "Third line, still rotated."] * 3, title="Rotated").rotate(90, expand=True).save(path, dpi=(150, 150))
    return path


def skewed_png(path: Path, angle: float = 4.0) -> Path:
    text_page_image(["Skewed scans are common with phone cameras.", "Deskewing improves OCR quality.",
                     "This page is rotated by a few degrees."] * 3, title="Skewed"
                    ).rotate(angle, expand=True, fillcolor="white").save(path, dpi=(150, 150))
    return path


def handwritten_png(path: Path) -> Path:
    # Italic serif is only a stand-in for handwriting; real handwriting needs real samples.
    text_page_image(["Ideal gas law: PV = nRT", "Boyle: P₁V₁ = P₂V₂ (const T)", "Remember units: atm, L, K"],
                    font_path=SERIF_IT, title="Lecture 4 — Gases").save(path, dpi=(150, 150))
    return path


def physics_diagram_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 80), "Ohm's law and simple circuits", fontsize=15, fontname="hebo")
    p.insert_text((72, 110), "The current through a resistor is V = IR.", fontsize=11)
    # Vector "circuit"
    p.draw_rect(pymupdf.Rect(150, 200, 400, 380))
    p.draw_line((250, 200), (300, 200), color=(1, 1, 1), width=3)
    p.draw_line((260, 190), (260, 210))
    p.draw_line((290, 180), (290, 220))
    p.insert_text((265, 175), "V", fontsize=10)
    p.insert_text((410, 290), "R", fontsize=10)
    # Raster graph
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    d.line((40, 260, 380, 260), fill="black", width=2)
    d.line((40, 260, 40, 20), fill="black", width=2)
    d.line((40, 260, 360, 40), fill="black", width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p.insert_image(pymupdf.Rect(150, 420, 450, 645), stream=buf.getvalue())
    p.insert_text((200, 665), "Fig. 1: I-V characteristic", fontsize=10)
    doc.save(path)
    return path


def mixed_pdf(path: Path) -> Path:
    doc = pymupdf.open(clean_digital_pdf(path.with_name("_tmp_clean.pdf")))
    scan = pymupdf.open(scanned_math_pdf(path.with_name("_tmp_scan.pdf")))
    doc.insert_pdf(scan)
    doc.save(path)
    doc.close()
    scan.close()
    path.with_name("_tmp_clean.pdf").unlink()
    path.with_name("_tmp_scan.pdf").unlink()
    return path


def duplicate_and_blank_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    for text in ("Page A unique content about vectors.", None, "Page C content about matrices.",
                 "Page A unique content about vectors."):
        p = doc.new_page(width=595, height=842)
        if text:
            p.insert_text((72, 100), text, fontsize=12)
    doc.save(path)
    return path


def encrypted_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret", fontsize=12)
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    return path


def corrupted_pdf(path: Path) -> Path:
    src = path.with_name("_tmp_c.pdf")
    clean_digital_pdf(src)
    data = src.read_bytes()
    src.unlink()
    path.write_bytes(data[:200])
    return path


def fake_pdf(path: Path) -> Path:
    path.write_text("this is not a pdf")
    return path


def docx_file(path: Path) -> Path:
    import docx

    d = docx.Document()
    d.add_heading("Lab Record: Titration", level=1)
    d.add_paragraph("Aim: to determine the concentration of HCl.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Trial", "Volume (mL)"
    t.cell(1, 0).text, t.cell(1, 1).text = "1", "12.4"
    d.save(path)
    return path


BUILDERS = {
    "clean_digital.pdf": clean_digital_pdf,
    "scanned_poor.pdf": scanned_poor_pdf,
    "rotated.png": rotated_png,
    "skewed.png": skewed_png,
    "math_paper.pdf": scanned_math_pdf,
    "chemistry.png": chemistry_png,
    "physics_diagram.pdf": physics_diagram_pdf,
    "handwritten.png": handwritten_png,
    "tables.pdf": table_pdf,
    "research_paper.pdf": research_paper_pdf,
    "mixed.pdf": mixed_pdf,
    "multilingual.pdf": multilingual_pdf,
    "corrupted.pdf": corrupted_pdf,
    "fake.pdf": fake_pdf,
    "encrypted.pdf": encrypted_pdf,
    "duplicates_blank.pdf": duplicate_and_blank_pdf,
    "lab_record.docx": docx_file,
}


def build_corpus(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    random.seed(0)
    return {name: fn(directory / name) for name, fn in BUILDERS.items()}


if __name__ == "__main__":  # python tests/fixtures/corpus.py out_dir
    import sys

    out = build_corpus(Path(sys.argv[1] if len(sys.argv) > 1 else "corpus"))
    for k, v in out.items():
        print(k, v)


def screenshot_note_png(path: Path) -> Path:
    """A screen-share screenshot: black letterbox bars, a white app canvas with toolbar text, and a
    dark note page with light handwriting-like text in the middle (like a tablet notes app)."""
    W, H = 2000, 900
    img = Image.new("RGB", (W, H), "black")
    d = ImageDraw.Draw(img)
    d.rectangle((260, 0, 1740, H), fill="white")  # app canvas
    d.text((300, 20), "8:38 PM  Practice  Untitled Notebook", fill="black", font=_font(28))
    d.text((300, 820), "Jyotirmoy is presenting", fill="black", font=_font(30))
    d.rectangle((700, 120, 1300, 880), fill=(40, 40, 42))  # dark note page
    y = 150
    for ln in ["Boyle's law: PV = k", "P1 V1 = P2 V2", "Charles' law: V/T = k", "Gay-Lussac: P/T = k"] * 3:
        d.text((730, y), ln, fill=(250, 220, 120), font=_font(30))
        y += 58
    img.save(path, dpi=(72, 72))
    return path
