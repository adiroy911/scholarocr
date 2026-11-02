import pymupdf
from PIL import Image, ImageDraw

from scholarocr.ingestion.loader import build_manifest
from scholarocr.ingestion.pdf import render_page
from scholarocr.intelligence.extractor import VisualPageContext, box_to_bbox
from scholarocr.preprocessing.cropping import detect_content_region
from scholarocr.preprocessing.enhancement import preprocess
from scholarocr.preprocessing.quality import assess
from scholarocr.security import PathPolicy
from scholarocr.storage import ArtifactStore
from tests.fixtures.corpus import _font, screenshot_note_png, text_page_image


def test_crop_finds_note_inside_nested_frames(tmp_path):
    img = Image.open(screenshot_note_png(tmp_path / "shot.png")).convert("RGB")
    box = detect_content_region(img)
    assert box is not None
    x0, y0, x1, y1 = box
    # The dark note page (700,120)-(1300,880) with ~1% padding; app chrome and letterbox excluded.
    assert 670 <= x0 <= 700 and 100 <= y0 <= 120 and 1300 <= x1 <= 1330 and 880 <= y1 <= 900


def test_crop_never_triggers_on_normal_pages(corpus):
    for name in ("clean_digital.pdf", "tables.pdf", "scanned_poor.pdf", "math_paper.pdf", "physics_diagram.pdf",
                 "research_paper.pdf"):
        assert detect_content_region(render_page(pymupdf.open(corpus[name])[0], 120)) is None, name
    for name in ("chemistry.png", "rotated.png", "skewed.png", "handwritten.png"):
        assert detect_content_region(Image.open(corpus[name]).convert("RGB")) is None, name


def test_crop_ignores_a_ruled_box_on_white_paper():
    img = text_page_image(["Some text above the box"] * 3)
    d = ImageDraw.Draw(img)
    d.rectangle((150, 600, 1100, 1300), outline="black", width=4)
    d.text((200, 650), "Table inside a box", fill="black", font=_font(34))
    assert detect_content_region(img) is None


def test_light_on_dark_polarity():
    img = Image.new("RGB", (1200, 1600), (35, 35, 35))
    d = ImageDraw.Draw(img)
    for i in range(12):
        d.text((80, 100 + i * 110), "Light handwriting on a dark page", fill=(245, 230, 140), font=_font(40))
    q = assess(img, None)
    assert q.light_on_dark and q.ink_ratio < 0.2 and not q.is_blank and "dark" not in q.issues


def test_resolution_judged_from_glyph_height():
    small = text_page_image(["tiny text line for resolution check"] * 20, size=(900, 1200), font_size=9)
    big = text_page_image(["normal text line"] * 12, size=(1240, 1754), font_size=34)
    qs, qb = assess(small, 300), assess(big, 72)  # metadata deliberately misleading
    assert "low_resolution" in qs.issues and "low_resolution" not in qb.issues
    _, transforms, _ = preprocess(small, qs)
    assert any(t.step == "upscale" for t in transforms)
    _, transforms, _ = preprocess(big, qb)
    assert not any(t.step == "upscale" for t in transforms)


def test_crop_transform_maps_boxes_back_to_original(tmp_path):
    img = Image.open(screenshot_note_png(tmp_path / "shot.png")).convert("RGB")
    box = detect_content_region(img)
    q = assess(img.crop(box), None)
    processed, transforms, matrix = preprocess(img, q, crop_box=box)
    assert transforms[0].step == "crop" and processed.size == (box[2] - box[0], box[3] - box[1])
    ctx = VisualPageContext(document_id="d", page_id="d-p0001", page_number=1, source_file="s", source_page_index=0,
                            page_width_pt=img.width * 72 / 300, page_height_pt=img.height * 72 / 300,
                            original_dpi=300, original_size=img.size, processed_size=processed.size, matrix=matrix,
                            page_image_rel=None, quality=q)
    bb = box_to_bbox([0, 0, 1000, 1000], ctx)  # whole processed image -> the crop box on the original page
    k = 72 / 300
    assert abs(bb.x0 - box[0] * k) < 1 and abs(bb.y0 - box[1] * k) < 1
    assert abs(bb.x1 - box[2] * k) < 1 and abs(bb.y1 - box[3] * k) < 1


def test_placeholder_dpi_metadata_ignored(tmp_path):
    shot = screenshot_note_png(tmp_path / "shot.png")  # saved with 72 dpi
    m = build_manifest([shot], policy=PathPolicy([tmp_path], 50), store=ArtifactStore(tmp_path / "docs"), max_pages=5)
    e = m.pages[0]
    assert e.image_dpi == 300 and any("no_dpi_metadata" in n for n in e.notes)
