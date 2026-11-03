"""Strict sanitiser for model-drawn SVG figures (``redraw_figure`` / ``submit_figure_svg``).

The SVG is re-built from scratch from a parse of the input, keeping only an allowlist of drawing elements and
presentation attributes with values that pass per-attribute checks. Everything else is dropped and reported:
scripts, ``style`` elements and attributes, event handlers (``on*``), ``foreignObject``, ``image``, ``a``,
``use``, animation elements, any namespaced attribute (``xlink:href``), any reference that is not a local
``url(#id)``, ``javascript:``/``data:`` values, CSS expressions. Input with a DOCTYPE, an entity declaration or a
processing instruction is rejected outright (no entity expansion can happen), as is input over the size or
element limits, input that is not well-formed XML, and input whose root is not ``<svg>``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .exceptions import InputError

SVG_NS = "http://www.w3.org/2000/svg"
MAX_SVG_BYTES = 400_000
MAX_ELEMENTS = 4000
MAX_TEXT = 500

ALLOWED_ELEMENTS = {"svg", "g", "defs", "marker", "path", "line", "polyline", "polygon", "rect", "circle", "ellipse",
                    "text", "tspan", "title", "desc"}

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_LEN = re.compile(rf"^\s*{_NUM}\s*(?:px|pt|mm|cm|in|em|ex|%)?\s*$")
_NUMS = re.compile(r"^[0-9eE.,+\-\s]*$")
_PATH = re.compile(r"^[0-9eE.,+\-\sMmLlHhVvCcSsQqTtAaZz]*$")
_TRANSFORM = re.compile(r"^(?:\s*(?:matrix|translate|scale|rotate|skewX|skewY)\s*\([0-9eE.,+\-\s]*\)\s*,?)*\s*$")
_COLOR = re.compile(r"^\s*(?:none|currentColor|transparent|#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|"
                    r"rgba?\(\s*[0-9.%\s,]+\))\s*$")
_URL_REF = re.compile(r"^\s*url\(\s*#[A-Za-z_][\w\-.]{0,60}\s*\)\s*$")
_ID = re.compile(r"^[A-Za-z_][\w\-.]{0,60}$")
_FONT = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,80}$")
_KEYWORD = re.compile(r"^[A-Za-z\-]{1,30}$")


def _check_paint(v: str) -> bool:
    return bool(_COLOR.match(v) or _URL_REF.match(v))


ATTR_CHECKS = {
    "x": _LEN.match, "y": _LEN.match, "x1": _LEN.match, "y1": _LEN.match, "x2": _LEN.match, "y2": _LEN.match,
    "cx": _LEN.match, "cy": _LEN.match, "r": _LEN.match, "rx": _LEN.match, "ry": _LEN.match,
    "width": _LEN.match, "height": _LEN.match, "dx": _NUMS.match, "dy": _NUMS.match,
    "d": _PATH.match, "points": _NUMS.match, "viewBox": _NUMS.match, "transform": _TRANSFORM.match,
    "fill": _check_paint, "stroke": _check_paint, "stroke-width": _LEN.match, "stroke-linecap": _KEYWORD.match,
    "stroke-linejoin": _KEYWORD.match, "stroke-dasharray": _NUMS.match, "stroke-opacity": _LEN.match,
    "fill-opacity": _LEN.match, "opacity": _LEN.match, "font-size": _LEN.match, "font-family": _FONT.match,
    "font-style": _KEYWORD.match, "font-weight": lambda v: bool(re.match(r"^(?:normal|bold|bolder|lighter|\d{3})$", v)),
    "text-anchor": _KEYWORD.match, "dominant-baseline": _KEYWORD.match, "markerWidth": _LEN.match,
    "markerHeight": _LEN.match, "refX": _LEN.match, "refY": _LEN.match,
    "orient": lambda v: bool(re.match(rf"^(?:auto|auto-start-reverse|{_NUM})$", v.strip())),
    "markerUnits": _KEYWORD.match, "marker-start": _URL_REF.match, "marker-end": _URL_REF.match,
    "marker-mid": _URL_REF.match, "preserveAspectRatio": lambda v: bool(re.match(r"^[A-Za-z ]{1,40}$", v)),
    "id": _ID.match, "version": lambda v: bool(re.match(r"^\d(\.\d)?$", v)), "fill-rule": _KEYWORD.match,
    "vector-effect": _KEYWORD.match, "font-variant": _KEYWORD.match, "letter-spacing": _LEN.match,
}
FORBIDDEN_VALUE = re.compile(r"javascript:|vbscript:|data:|expression\s*\(|@import|<|>|\\", re.I)


@dataclass
class SanitizedSvg:
    svg: str
    removed: list[str] = field(default_factory=list)  # what was dropped, for the caller's report
    elements: int = 0
    texts: list[str] = field(default_factory=list)  # text content kept (the labels)


def _local(tag: str) -> tuple[str | None, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return None, tag


def extract_svg(text: str) -> str | None:
    """The first ``<svg ...>...</svg>`` in a model reply (which may wrap it in prose or a code fence)."""
    m = re.search(r"<svg\b.*?</svg\s*>", text or "", re.S | re.I)
    return m.group(0) if m else None


def sanitize_svg(raw: str) -> SanitizedSvg:
    """Return a safe, re-serialised SVG or raise ``InputError`` explaining why the input was refused."""
    if not isinstance(raw, str) or not raw.strip():
        raise InputError("svg is empty")
    if len(raw.encode("utf-8")) > MAX_SVG_BYTES:
        raise InputError(f"svg is larger than {MAX_SVG_BYTES // 1000} kB")
    lowered = raw.lower()
    if "<!doctype" in lowered or "<!entity" in lowered or "<?xml-stylesheet" in lowered:
        raise InputError("svg must not contain a DOCTYPE, entity declarations or stylesheets")
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw.strip())
    if "<?" in body:
        raise InputError("svg must not contain processing instructions")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise InputError(f"svg is not well-formed XML ({e})") from None
    ns, name = _local(root.tag)
    if name != "svg" or ns not in (None, SVG_NS):
        raise InputError("the root element must be <svg>")
    removed: list[str] = []
    out = SanitizedSvg(svg="")

    def copy(src: ET.Element, depth: int) -> ET.Element | None:
        ns_, tag = _local(src.tag)
        if ns_ not in (None, SVG_NS) or tag not in ALLOWED_ELEMENTS:
            removed.append(f"element <{tag}>")
            return None
        if depth > 40:
            removed.append("elements nested too deeply")
            return None
        out.elements += 1
        if out.elements > MAX_ELEMENTS:
            raise InputError(f"svg has more than {MAX_ELEMENTS} elements")
        dst = ET.Element(tag)
        for key, value in src.attrib.items():
            ans, aname = _local(key)
            check = ATTR_CHECKS.get(aname)
            if ans is not None or check is None:
                removed.append(f"attribute {aname!r} on <{tag}>")
                continue
            v = str(value)
            if len(v) > 20000 or FORBIDDEN_VALUE.search(v) or not check(v):
                removed.append(f"value of {aname!r} on <{tag}>")
                continue
            dst.set(aname, v)
        if tag in ("text", "tspan", "title", "desc") and src.text and src.text.strip():
            dst.text = src.text[:MAX_TEXT]
            if tag in ("text", "tspan"):
                out.texts.append(dst.text.strip())
        for child in src:
            c = copy(child, depth + 1)
            if c is not None:
                dst.append(c)
            if tag in ("text", "tspan") and child.tail and child.tail.strip():
                tail = child.tail[:MAX_TEXT]
                if c is not None:
                    c.tail = tail
                elif len(dst):  # the child was dropped: keep its trailing text inside this element
                    dst[-1].tail = (dst[-1].tail or "") + tail
                else:
                    dst.text = (dst.text or "") + tail
        return dst

    clean = copy(root, 0)
    assert clean is not None
    clean.set("xmlns", SVG_NS)
    if "viewBox" not in clean.attrib and not ("width" in clean.attrib and "height" in clean.attrib):
        raise InputError("svg needs a viewBox (or width and height)")
    out.svg = ET.tostring(clean, encoding="unicode", short_empty_elements=True)
    out.removed = sorted(set(removed))
    return out
