"""Build one image-only PDF from chosen pages of other PDFs, so a reader cannot use their text layers.

    python -I benchmarks/rasterize_pages.py OUT.pdf --dpi 200 a.pdf:5,6 b.pdf:2

Each page is rendered to a lossless image and placed on a page of the same size. The script fails if the result
still has extractable text. It only reads the input files; it takes no other action on their content. Prints a
JSON list mapping output page numbers to their source file and page, which belongs in the benchmark's README.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pymupdf


def rasterize(specs: list[str], out: Path, dpi: int = 200) -> list[dict]:
    result = pymupdf.open()
    mapping: list[dict] = []
    for spec in specs:
        path, _, pages = spec.rpartition(":")
        with pymupdf.open(path) as src:
            for n in (int(x) for x in pages.split(",")):
                page = src[n - 1]
                new = result.new_page(width=page.rect.width, height=page.rect.height)
                new.insert_image(new.rect, pixmap=page.get_pixmap(dpi=dpi, alpha=False))
                mapping.append({"page": result.page_count, "source": Path(path).name, "source_page": n})
    leftover = sum(len(p.get_text().strip()) for p in result)
    if leftover:
        raise SystemExit(f"the output still has {leftover} characters of text")
    out.parent.mkdir(parents=True, exist_ok=True)
    result.save(out, deflate=True)
    return mapping


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path)
    ap.add_argument("specs", nargs="+", help="file.pdf:page,page")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()
    print(json.dumps(rasterize(args.specs, args.out, args.dpi), indent=1))


if __name__ == "__main__":
    main()
