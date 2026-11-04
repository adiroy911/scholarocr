"""HTML/CSS page builder and headless-Chromium driver for the layout PDF export.

Each page becomes a fixed-size ``<section>`` (CSS ``@page`` sized in points); every element is an absolutely
positioned box at its bbox. Math is typeset by the vendored KaTeX (with mhchem); fonts are the vendored
Source Serif 4, Noto Serif Devanagari and Anek, inlined as data URIs so no file or network access is needed
for them. Text auto-fit runs in the browser, where the real fonts and KaTeX metrics are known.

The browser runs twice: ``--dump-dom`` lays the pages out (KaTeX, auto-fit, per-element status written to
``data-*`` attributes) and returns the finished DOM; the scripts are then stripped and ``--print-to-pdf``
prints that static DOM. Nothing is fetched from the network and the browser uses a throwaway profile.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..exceptions import ExportError

VENDOR = Path(__file__).resolve().parents[1] / "webapp" / "static" / "vendor"
BROWSER_ENV = "SCHOLAROCR_BROWSER"
BROWSER_TIMEOUT_S = 180
VIRTUAL_TIME_MS = 30000
DEVANAGARI = re.compile(r"[ऀ-ॿ꣠-ꣿ]")

# -------------------------------------------------------------------------------------------- browser discovery
_WIN_BROWSERS = (
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
    r"Chromium\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
)
_MAC_BROWSERS = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_PATH_BROWSERS = ("msedge", "microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                  "chromium", "chromium-browser", "chrome", "brave-browser")


@dataclass(frozen=True)
class Browser:
    path: Path
    name: str
    headless_shell: bool  # chrome-headless-shell takes plain --headless

    def describe(self) -> str:
        return f"{self.name} ({self.path})"


def _playwright_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        dirs.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    dirs.append(Path.home() / ".cache" / "ms-playwright")
    dirs.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    return dirs


def _playwright_browsers() -> list[Path]:
    """Chromium builds downloaded by Playwright, newest first (full Chromium before the headless shell)."""
    found: list[tuple[int, int, Path]] = []
    names = ("chrome.exe", "chrome", "chrome-headless-shell.exe", "chrome-headless-shell", "Chromium")
    for root in _playwright_dirs():
        if not root.is_dir():
            continue
        for d in root.glob("chromium*-*"):
            m = re.search(r"-(\d+)$", d.name)
            if not m:
                continue
            shell = "headless_shell" in d.name
            for exe in d.rglob("*"):
                if exe.name in names and exe.is_file():
                    found.append((int(m.group(1)), 0 if shell else 1, exe))
                    break
    return [p for _, _, p in sorted(found, reverse=True)]


def _browser(path: Path) -> Browser:
    shell = "headless-shell" in path.name or "headless_shell" in str(path)
    stem = path.stem.lower()
    name = {"msedge": "Microsoft Edge", "chrome": "Chrome/Chromium", "brave": "Brave",
            "chrome-headless-shell": "Chromium headless shell"}.get(stem, path.name)
    return Browser(path=path, name=name, headless_shell=shell)


def find_browser(explicit: str | Path | None = None) -> Browser | None:
    """A Chromium-family browser able to print PDFs: an explicit path or ``$SCHOLAROCR_BROWSER`` first, then
    Edge, Chrome, Chromium and Brave in their standard install places or on PATH, then Playwright's Chromium."""
    for cand in (explicit, os.environ.get(BROWSER_ENV)):
        if cand:
            p = Path(str(cand)).expanduser()
            if p.is_file():
                return _browser(p)
            hit = shutil.which(str(cand))
            if hit:
                return _browser(Path(hit))
    paths: list[Path] = []
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        paths += [Path(r) / rel for rel in _WIN_BROWSERS for r in roots if r]
    elif sys.platform == "darwin":
        paths += [Path(p) for p in _MAC_BROWSERS]
    for p in paths:
        if p.is_file():
            return _browser(p)
    for name in _PATH_BROWSERS:
        hit = shutil.which(name)
        if hit:
            return _browser(Path(hit))
    for p in _playwright_browsers():
        return _browser(p)
    return None


def _browser_args(browser: Browser, profile: Path) -> list[str]:
    args = [str(browser.path), "--headless" if browser.headless_shell else "--headless=new",
            "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-sync", "--disable-component-update",
            "--disable-default-apps", "--mute-audio", "--hide-scrollbars", "--force-color-profile=srgb",
            "--allow-file-access-from-files", f"--user-data-dir={profile}",
            f"--virtual-time-budget={VIRTUAL_TIME_MS}", "--run-all-compositor-stages-before-draw"]
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chromium refuses to start as root (e.g. in containers) otherwise
    return args


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=BROWSER_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as e:
        raise ExportError(f"Layout PDF: the browser timed out while {what} ({BROWSER_TIMEOUT_S} s)") from e
    except OSError as e:
        raise ExportError(f"Layout PDF: cannot start the browser {cmd[0]} ({e})") from e
    return proc


def lay_out(browser: Browser, page_html: Path, work: Path) -> str:
    """Run the layout pass and return the finished DOM (with KaTeX output and fitted sizes)."""
    cmd = _browser_args(browser, work / "profile") + ["--dump-dom", page_html.as_uri()]
    proc = _run(cmd, "laying out the pages")
    dom = proc.stdout.decode("utf-8", errors="replace")
    err = re.search(r'data-socr-error="([^"]*)"', dom)
    if err:
        raise ExportError(f"Layout PDF: laying out the pages failed in the browser: {html.unescape(err.group(1))[:300]}")
    if 'data-socr-done="1"' not in dom:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise ExportError("Layout PDF: the browser did not finish laying out the pages "
                          f"(exit {proc.returncode}): {' | '.join(tail)[:400]}")
    return dom


def print_pdf(browser: Browser, page_html: Path, pdf_path: Path, work: Path) -> None:
    cmd = _browser_args(browser, work / "profile") + [
        "--no-pdf-header-footer", "--print-to-pdf-no-header", f"--print-to-pdf={pdf_path}", page_html.as_uri()]
    proc = _run(cmd, "printing the PDF")
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise ExportError(f"Layout PDF: the browser produced no PDF (exit {proc.returncode}): "
                          f"{' | '.join(tail)[:400]}")


_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_TAG = re.compile(r"<div\b[^>]*\bdata-eid=\"[^\"]*\"[^>]*>")
_ATTR = re.compile(r"([\w:-]+)=\"([^\"]*)\"")


def static_dom(dom: str) -> str:
    """The laid-out DOM without scripts, ready to print (layout is already baked into inline styles)."""
    body = _SCRIPT.sub("", dom)
    return body if body.lstrip().lower().startswith("<!doctype") else "<!doctype html>\n" + body


def fit_results(dom: str) -> dict[str, dict[str, str]]:
    """``{element_id: {status, size, fit, target, texerr}}`` read back from the laid-out DOM."""
    out: dict[str, dict[str, str]] = {}
    for tag in _TAG.findall(dom):
        attrs = {k: html.unescape(v) for k, v in _ATTR.findall(tag)}
        eid = attrs.get("data-eid")
        if eid:
            out[eid] = {k[5:]: v for k, v in attrs.items() if k.startswith("data-")}
    return out


_SECTION = re.compile(r"<section\b[^>]*\bclass=\"pg\"[^>]*>")
_BODY = re.compile(r"<body\b[^>]*>")


def body_sizes(dom: str) -> dict:
    """``{"pages": [body size per page], "scopes": ["document" | "page" per page], "document": size,
    "scope": "document" | "mixed"}`` from the laid-out DOM."""
    pages, scopes = [], []
    for tag in _SECTION.findall(dom):
        attrs = dict(_ATTR.findall(tag))
        scopes.append(attrs.get("data-body-scope", "page"))
        try:
            pages.append(float(attrs.get("data-body", "")))
        except ValueError:
            pages.append(None)
    body = dict(_ATTR.findall((_BODY.findall(dom) or [""])[0]))
    try:
        doc = float(body.get("data-body", ""))
    except ValueError:
        doc = None
    return {"pages": pages, "scopes": scopes, "document": doc, "scope": body.get("data-body-scope", "page")}


# -------------------------------------------------------------------------------------------- page HTML
def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


@lru_cache(maxsize=1)
def _katex_css() -> str:
    css = (VENDOR / "katex" / "katex.min.css").read_text(encoding="utf-8")

    def inline(m: re.Match) -> str:
        font = VENDOR / "katex" / "fonts" / m.group(1)
        return f'url(data:font/woff2;base64,{_b64(font)}) format("woff2")' if font.is_file() else m.group(0)

    # keep only the woff2 source of each @font-face and inline it
    css = re.sub(r'url\(fonts/([^)]+?\.woff2)\) format\("woff2"\)(?:,url\([^)]*\) format\("[a-z]+"\))*', inline, css)
    return css


@lru_cache(maxsize=1)
def _katex_js() -> str:
    js = ((VENDOR / "katex" / "katex.min.js").read_text(encoding="utf-8") + "\n"
          + (VENDOR / "katex" / "mhchem.min.js").read_text(encoding="utf-8"))
    return js.replace("</script", "<\\/script")


_FACES = (  # family, file, weight, style, unicode-range (None = all)
    ("Sheet Serif", "source-serif-4-latin-400-normal.woff2", "400", "normal", None),
    ("Sheet Serif", "source-serif-4-latin-400-italic.woff2", "400", "italic", None),
    ("Sheet Serif", "source-serif-4-latin-600-normal.woff2", "600", "normal", None),
    ("Sheet Serif", "noto-serif-devanagari-devanagari-400-normal.woff2", "400", "normal", "dev"),
    ("Sheet Serif", "noto-serif-devanagari-devanagari-600-normal.woff2", "600", "normal", "dev"),
    ("Anek", "anek-latin-wdth.woff2", "100 800", "normal", None),
    ("Anek", "anek-devanagari-wdth.woff2", "100 800", "normal", "dev"),
)
_DEV_RANGE = "U+0900-097F, U+1CD0-1CF9, U+200C-200D, U+20A8, U+20B9, U+25CC, U+A830-A839, U+A8E0-A8FF"


def font_css(devanagari: bool) -> tuple[str, list[str]]:
    """@font-face rules for the vendored fonts (Devanagari faces only when the text needs them)."""
    rules, used = [], []
    for family, file, weight, style, rng in _FACES:
        if rng == "dev" and not devanagari:
            continue
        path = VENDOR / "fonts" / file
        if not path.is_file():
            continue
        urange = f"unicode-range:{_DEV_RANGE};" if rng == "dev" else ""
        rules.append(f'@font-face{{font-family:"{family}";src:url(data:font/woff2;base64,{_b64(path)}) '
                     f'format("woff2");font-weight:{weight};font-style:{style};{urange}}}')
        used.append(file)
    return "\n".join(rules), used


BASE_CSS = """
html,body{margin:0;padding:0}
body{-webkit-print-color-adjust:exact;print-color-adjust:exact;color:#1c1c1c}
.pg{position:relative;overflow:hidden;break-after:page;background:#fff}
.pg:last-of-type{break-after:auto}
body.paper .pg{background:#fbf8ef}
.el{position:absolute;box-sizing:border-box;display:flex;flex-direction:column;justify-content:center;overflow:visible}
.el.top{justify-content:flex-start}
.el>.c{flex:none;width:100%;font-family:"Sheet Serif","Noto Serif Devanagari",Cambria,"Times New Roman",serif;
  line-height:1.22;overflow-wrap:normal;word-break:normal;hyphens:manual;font-kerning:normal;
  font-variant-ligatures:common-ligatures;text-rendering:geometricPrecision}
.k-title>.c,.k-heading>.c{font-weight:600;letter-spacing:.005em}
.one-line>.c{white-space:nowrap}
body.measuring .el>.c{width:calc(100% - 2px)}
.k-caption>.c{font-style:italic}
.k-furniture>.c{font-family:Anek,"Sheet Serif",sans-serif;color:#3a3a3a;font-stretch:100%}
.k-code>.c{font-family:Consolas,"DejaVu Sans Mono","Courier New",monospace;white-space:pre}
.k-unclear>.c{color:#6b6b6b;font-style:italic}
.k-math>.c{text-align:left;white-space:nowrap}
.k-math .katex-display{margin:0;text-align:left}
.k-math .katex-display>.katex{text-align:left}
.k-math .katex-display.fleqn>.katex{padding-left:0}
.c .katex{font-size:1.08em;white-space:nowrap}
.c .nw{white-space:nowrap}
.k-math .katex{font-size:1em}
.k-math .dmw{display:flex;flex-wrap:wrap;align-items:center;column-gap:.28em;row-gap:.25em}
.k-math .dmp{flex:none}
.c .tex-err{font-style:italic}
.c s{text-decoration-thickness:.06em}
.c .sm{position:relative;display:inline-block}
.c .sm::after{content:"";position:absolute;left:-.1em;right:-.1em;top:50%;border-top:.07em solid currentColor}
.c .sgap{width:2.4em;height:1em;vertical-align:baseline}
.c .qn{font-weight:600;margin-right:.3em}
.c .marks{float:right;margin-left:.6em;font-size:.85em;color:#333}
.c ul,.c ol{margin:0;padding-left:1.15em}
.c li{margin:0}
.k-figure{justify-content:center;align-items:center}
.k-figure img{display:block;object-fit:contain;mix-blend-mode:multiply}
.k-placeholder{border:.75pt dashed #8a8a8a;border-radius:2pt;padding:3pt;background:rgba(255,255,255,.4)}
.k-placeholder>.c{color:#4a4a4a;font-family:Anek,"Sheet Serif",sans-serif;text-align:center}
.k-placeholder .lbl{font-weight:600}
.k-table table{border-collapse:collapse;width:100%;table-layout:auto}
.k-table td,.k-table th{border:.5pt solid #3a3a3a;padding:.12em .3em;vertical-align:middle;text-align:left;font-weight:400}
.k-table th,.k-table td.h{font-weight:600}
.k-table td.u{background:#fff3cd}
.k-table .cap{font-style:italic;font-size:.9em;margin-bottom:.2em}
.over-figure{background:rgba(255,255,255,.82)}
body.boxes .el{outline:.4pt dashed rgba(200,40,40,.75)}
body.boxes .el[data-status=shrunk]{outline-color:rgba(220,130,0,.9)}
body.boxes .el[data-status=overflow]{outline:.8pt solid rgba(220,0,0,.9)}
body.boxes .el::after{content:attr(data-tag);position:absolute;top:-5.5pt;left:0;font:4.5pt/1 monospace;
  color:#b22;white-space:nowrap}
"""

FIT_JS = r"""
(function(){
  // One body size for the document (or per page when the pages differ): every paragraph is set at it, headings,
  // captions and equations at fixed ratios of it. A box too small for its text first grows to the right (one-line
  // boxes and equations) and down into free space; only then is the text shrunk, down to a floor.
  var P = __PARAMS__;
  var PX = 96 / 72;
  function renderMath(){
    var nodes = document.querySelectorAll('[data-tex]');
    for (var i = 0; i < nodes.length; i++){
      var n = nodes[i], tex = n.getAttribute('data-tex');
      try { katex.render(tex, n, {displayMode: n.classList.contains('dm'), throwOnError: true, strict: 'ignore',
                                  trust: false, fleqn: true, output: 'html', maxExpand: 500}); }
      catch (e) {
        n.textContent = n.getAttribute('data-alt') || tex; n.classList.add('tex-err');
        var el = n.closest('.el'); if (el) el.setAttribute('data-texerr', String(+(el.getAttribute('data-texerr') || 0) + 1));
      }
    }
  }
  function inkPx(c){
    // what is really printed: an element's own box says nothing about the formula overflowing it
    var q = null, range = document.createRange(), walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT), n;
    function add(r){ if (!r.width && !r.height) return;
      if (!q) q = {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
      else { q.left = Math.min(q.left, r.left); q.top = Math.min(q.top, r.top);
             q.right = Math.max(q.right, r.right); q.bottom = Math.max(q.bottom, r.bottom); } }
    while ((n = walker.nextNode())) { if (!n.nodeValue.trim() && n.nodeValue.indexOf('\u00a0') < 0) continue;
      range.selectNodeContents(n); add(range.getBoundingClientRect()); }
    Array.prototype.forEach.call(c.querySelectorAll('svg,img'), function(s){ add(s.getBoundingClientRect()); });
    return q || c.getBoundingClientRect(); }
  function widthFits(b){ var c = b.firstElementChild;
    if (c.scrollWidth > b.clientWidth + 0.75) return false;
    return !c.querySelector('.katex') || inkPx(c).right <= b.getBoundingClientRect().right + 0.75; }
  function fits(b){ return widthFits(b) && b.firstElementChild.offsetHeight <= b.clientHeight + 0.75; }
  function setSize(b, s){ b.style.fontSize = s + 'pt'; }
  function search(b, lo, hi){
    if (hi < lo) hi = lo;
    setSize(b, lo); if (!fits(b)) return {fit: lo, ok: false};
    setSize(b, hi); if (fits(b)) return {fit: hi, ok: true};
    for (var i = 0; i < 16 && hi - lo > 0.05; i++){ var m = (lo + hi) / 2; setSize(b, m); if (fits(b)) lo = m; else hi = m; }
    return {fit: Math.floor(lo * 20) / 20, ok: true};
  }
  function quantile(a, q){ if (!a.length) return 0; a = a.slice().sort(function(x, y){ return x - y; });
    var i = (a.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i); return a[lo] + (a[hi] - a[lo]) * (i - lo); }
  function rectOf(b){ var x = parseFloat(b.style.left), y = parseFloat(b.style.top);
    return {x0: x, y0: y, x1: x + parseFloat(b.style.width), y1: y + parseFloat(b.style.height)}; }
  function setRect(b, r){ b.style.left = r.x0.toFixed(2) + 'pt'; b.style.top = r.y0.toFixed(2) + 'pt';
    b.style.width = (r.x1 - r.x0).toFixed(2) + 'pt'; b.style.height = (r.y1 - r.y0).toFixed(2) + 'pt'; }
  function meets(a, b){ return a.x0 < b.x1 - 0.5 && b.x0 < a.x1 - 0.5 && a.y0 < b.y1 - 0.5 && b.y0 < a.y1 - 0.5; }
  function freeBelow(r, others, H){ var lim = H - P.margin;
    others.forEach(function(o){ if (o.x1 > r.x0 + 1 && o.x0 < r.x1 - 1 && o.y0 >= r.y1 - 0.5) lim = Math.min(lim, o.y0 - P.gap); });
    return Math.max(lim, r.y1); }
  function freeRight(r, others, W){ var lim = W - P.margin;
    others.forEach(function(o){ if (o.y1 > r.y0 + 1 && o.y0 < r.y1 - 1 && o.x0 >= r.x1 - 0.5) lim = Math.min(lim, o.x0 - P.gap); });
    return Math.max(lim, r.x1); }
  function freeLeft(r, others){ var lim = P.margin;
    others.forEach(function(o){ if (o.y1 > r.y0 + 1 && o.y0 < r.y1 - 1 && o.x1 <= r.x0 + 0.5) lim = Math.max(lim, o.x1 + P.gap); });
    return Math.min(lim, r.x0); }
  function inkRect(b){ var pg = b.closest('.pg').getBoundingClientRect(), q = inkPx(b.firstElementChild);
    return {x0: (q.left - pg.left) / PX, y0: (q.top - pg.top) / PX, x1: (q.right - pg.left) / PX, y1: (q.bottom - pg.top) / PX}; }
  function wrapMath(b){
    // a long equation breaks before its top-level relations; each part stays whole
    var dm = b.querySelector('.dm[data-parts]'); if (!dm || dm.classList.contains('dmw')) return false;
    var parts = JSON.parse(dm.getAttribute('data-parts')), holder = document.createElement('div');
    try { parts.forEach(function(p){ var s = document.createElement('span'); s.className = 'dmp';
        katex.render(p, s, {displayMode: true, throwOnError: true, strict: 'ignore', trust: false, fleqn: true,
                            output: 'html', maxExpand: 500}); holder.appendChild(s); }); }
    catch (e) { return false; }
    dm.innerHTML = ''; while (holder.firstChild) dm.appendChild(holder.firstChild);
    dm.classList.add('dmw'); return true; }
  function keepOnPage(b, others, W, H){
    // nothing is cut off by the page edge: slide into free space, then reduce the size as far as it takes
    var r = rectOf(b), q = inkRect(b), over = q.x1 - (W - 1), moved = false;
    if (over > 0) { var dx = Math.min(over, r.x0 - freeLeft(r, others));
      if (dx > 0.25) { r = {x0: r.x0 - dx, y0: r.y0, x1: r.x1 - dx, y1: r.y1}; setRect(b, r); moved = true; } }
    var size = parseFloat(b.style.fontSize), shrunk = false;
    for (var i = 0; i < 40 && size > P.absoluteMin; i++){ q = inkRect(b);
      if (q.x1 <= W - 1 && q.y1 <= H - 1) break;
      size = Math.max(P.absoluteMin, size * 0.97); setSize(b, size); shrunk = true; }
    if (moved) b.setAttribute('data-box', [r.x0, r.y0, r.x1, r.y1].map(function(v){ return v.toFixed(2); }).join(','));
    if (shrunk) { b.setAttribute('data-size', size.toFixed(2)); b.setAttribute('data-status', 'shrunk'); }
    return r; }
  function naturalWidth(b){ var c = b.firstElementChild, ws = c.style.whiteSpace; c.style.whiteSpace = 'nowrap';
    var w = c.scrollWidth;
    if (c.querySelector('.katex')) w = Math.max(w, inkPx(c).right - b.getBoundingClientRect().left);
    c.style.whiteSpace = ws; return w / PX; }
  function spread(b, size){
    // multi-line text in a taller box: open the leading so the lines span the box like the original lines
    var c = b.firstElementChild, px = size * PX, h0 = c.offsetHeight, lines = Math.round(h0 / (px * P.leading));
    if (lines < 2 || b.clientHeight < h0 * 1.12) return;
    var lh = Math.min(b.clientHeight / lines, px * P.maxLeading);
    c.style.lineHeight = lh + 'px'; b.classList.add('top');
    if (!fits(b)) { c.style.lineHeight = ''; b.classList.remove('top'); }
  }
  function mark(b, status, size, target){
    if (status === 'overflow' || status === 'moved') b.classList.add('top');
    b.setAttribute('data-fit', (b._fit || size).toFixed(2)); b.setAttribute('data-size', size.toFixed(2));
    b.setAttribute('data-target', target.toFixed(2)); b.setAttribute('data-status', status);
    b.setAttribute('data-tag', (b.getAttribute('data-short') || '') + ' ' + status + ' ' + size.toFixed(1));
  }
  function place(b, body, rects, all, W, H){
    var g = b.getAttribute('data-group'), mn = +b.getAttribute('data-min'), mx = +b.getAttribute('data-max');
    var target = Math.max(mn, Math.min(body * +(b.getAttribute('data-scale') || 1), mx));
    var floor = Math.max(mn, Math.min(target, body * +(b.getAttribute('data-floor') || P.floor)));
    var size, status;
    if (g === 'own' || b.classList.contains('over-figure')) {
      // tables, figure placeholders, labels over a figure: the largest size that fits, never above the target
      var s0 = search(b, mn, Math.max(mn, target));
      size = s0.ok ? s0.fit : mn; setSize(b, size);
      status = !s0.ok ? 'overflow' : (size < target - 0.05 ? 'shrunk' : 'fit');
      mark(b, status, size, target); finishOnPage(b, all, rects, W, H); return;
    }
    setSize(b, target);
    var r0 = rects.get(b), r = {x0: r0.x0, y0: r0.y0, x1: r0.x1, y1: r0.y1}, grew = [];
    var others = [];
    all.forEach(function(o){ if (o !== b && !meets(rects.get(o), r0)) others.push(rects.get(o)); });
    if (fits(b)) {
      // a short text that wraps only because its box is narrow ("• T = T(x, t)" over two lines) stays on one line
      // when the free space to its right allows it
      var nat = naturalWidth(b), lines = Math.round(b.firstElementChild.offsetHeight / (target * PX * P.leading));
      if (lines >= 2 && nat <= (r.x1 - r.x0) * P.unwrapRatio && b.firstElementChild.innerHTML.indexOf('<br') < 0
          && r.x0 + nat + 1 <= freeRight(r, others, W)) {
        r.x1 = r.x0 + nat + 1; setRect(b, r); rects.set(b, r); b.setAttribute('data-grew', 'right');
        b.setAttribute('data-box', [r.x0, r.y0, r.x1, r.y1].map(function(v){ return v.toFixed(2); }).join(','));
        mark(b, 'moved', target, target); finishOnPage(b, all, rects, W, H); return;
      }
      if (b.hasAttribute('data-spread')) spread(b, target);
      mark(b, 'fit', target, target); finishOnPage(b, all, rects, W, H); return;
    }
    var oneLine = g === 'math' || b.classList.contains('one-line') || (r.y1 - r.y0) < target * P.leading * 1.8;
    if (oneLine) {  // keep it on one line: extend to the right into free space
      var right = Math.min(freeRight(r, others, W), r.x0 + naturalWidth(b) + 1);
      if (right > r.x1 + 0.5) { r.x1 = right; setRect(b, r); grew.push('right'); }
    }
    if (g === 'math' && !widthFits(b)) wrapMath(b);
    if (!fits(b)) {  // then down into free space, as far as the text needs
      var c = b.firstElementChild, need = r.y0 + c.offsetHeight / PX + 1, below = freeBelow(r, others, H);
      var bottom = Math.min(below, Math.max(need, r.y1));
      if (bottom > r.y1 + 0.5) { r.y1 = bottom; setRect(b, r); grew.push('down'); }
    }
    if (grew.length) { rects.set(b, r); b.setAttribute('data-grew', grew.join('+'));
      b.setAttribute('data-box', [r.x0, r.y0, r.x1, r.y1].map(function(v){ return v.toFixed(2); }).join(',')); }
    if (fits(b)) { status = grew.length ? 'moved' : 'fit'; size = target; }
    else {  // last resort: smaller, down to the floor
      var s = search(b, floor, target);
      size = s.ok ? s.fit : floor; setSize(b, size);
      // within 2 % of the target nobody sees a different size: still the shared size
      status = !s.ok ? 'overflow' : size >= target * P.sameSize ? (grew.length ? 'moved' : 'fit') : 'shrunk';
    }
    mark(b, status, size, target);
    finishOnPage(b, all, rects, W, H);
  }
  function finishOnPage(b, all, rects, W, H){
    var r0 = rects.get(b), others = [];
    all.forEach(function(o){ if (o !== b && !meets(rects.get(o), r0)) others.push(rects.get(o)); });
    rects.set(b, keepOnPage(b, others, W, H));
  }
  function layout(){
    // measure with 2 px less width than the printed box: the print pass lays the text out again and a line that
    // filled its box to the last fraction of a pixel could wrap there
    document.body.classList.add('measuring');
    renderMath();
    var pages = Array.prototype.slice.call(document.querySelectorAll('.pg'));
    var info = pages.map(function(pg){
      var items = Array.prototype.slice.call(pg.querySelectorAll('.el[data-min]'));
      var fitsBody = [];
      items.forEach(function(b){
        if (b.getAttribute('data-group') !== 'body' || b.classList.contains('over-figure')) return;
        var r = search(b, +b.getAttribute('data-min'), +b.getAttribute('data-max')); b._fit = r.fit;
        if (r.ok) fitsBody.push(r.fit);
      });
      return {pg: pg, items: items, fits: fitsBody, body: fitsBody.length ? quantile(fitsBody, P.bodyQuantile) : 0};
    });
    // The document's size: per page shape, the median of the pages' own sizes. Larger writing on a page of the same
    // shape does not make its text larger, or the size jumps from page to page. A page much denser than the rest
    // (more than pageTolerance under the median) and a page of another shape (landscape) keep their own.
    var shapes = {}, main = null;
    info.forEach(function(i){ var key = i.pg.style.width + 'x' + i.pg.style.height; i.shape = key;
      var s = shapes[key] = shapes[key] || {own: [], pages: 0}; s.pages++; if (i.body) s.own.push(i.body);
      if (!main || s.pages > shapes[main].pages) main = key; });
    Object.keys(shapes).forEach(function(k){ var s = shapes[k];
      s.body = Math.max(P.bodyMin, Math.min(s.own.length ? quantile(s.own, 0.5) : P.defaultBody, P.bodyMax)); });
    var docBody = shapes[main].body, shared = true;
    info.forEach(function(i){
      var near = !i.body || i.body >= shapes[i.shape].body * (1 - P.pageTolerance);
      var inMain = near && i.shape === main;
      if (!inMain) shared = false;
      var body = near ? shapes[i.shape].body : Math.max(P.bodyMin, Math.min(i.body, P.bodyMax));
      i.pg.setAttribute('data-body', body.toFixed(2));
      i.pg.setAttribute('data-body-scope', inMain ? 'document' : 'page');
      var W = parseFloat(i.pg.style.width), H = parseFloat(i.pg.style.height);
      var els = Array.prototype.slice.call(i.pg.querySelectorAll('.el'));
      var rects = new Map(); els.forEach(function(b){ rects.set(b, rectOf(b)); });
      i.items.slice().sort(function(a, b){ var ra = rects.get(a), rb = rects.get(b); return ra.y0 - rb.y0 || ra.x0 - rb.x0; })
        .forEach(function(b){ place(b, body, rects, els, W, H); });
    });
    // measured with the printed width, once every size is final
    document.body.classList.remove('measuring');
    Array.prototype.forEach.call(document.querySelectorAll('.el[data-min]'), function(b){ var q = inkRect(b);
      b.setAttribute('data-ink', [q.x0, q.y0, q.x1, q.y1].map(function(v){ return v.toFixed(2); }).join(',')); });
    document.body.setAttribute('data-body', docBody.toFixed(2));
    document.body.setAttribute('data-body-scope', shared ? 'document' : 'mixed');
    document.body.setAttribute('data-socr-done', '1');
  }
  function start(){ try { layout(); } catch (e) { document.body.setAttribute('data-socr-error', String(e)); document.body.setAttribute('data-socr-done', '1'); } }
  // Every face before measuring: a browser loads a font only when something uses it, so KaTeX's fonts would still
  // be missing when the formulas are measured, and they print wider than the fallback they are measured in.
  if (document.fonts && document.fonts.ready) {
    var faces = []; document.fonts.forEach(function(f){ faces.push(f.load()); });
    Promise.all(faces).then(function(){ return document.fonts.ready; }).then(start, start);
  } else start();
})();
"""

# bodyQuantile: the body size is the one at which this share of the paragraph boxes are already at their
# largest fitting size (0.25: three in four paragraphs fit their box at it). pageTolerance: pages whose own size is
# within this share of the document's size (the median of the pages' sizes) share it. floor: smallest size as a share of the body (an item's
# data-floor overrides it). margin / gap: free space kept at the page edge and between grown boxes, in points.
FIT_PARAMS = {"leading": 1.22, "maxLeading": 1.6, "absoluteMin": 5.0, "defaultBody": 11.0, "bodyMin": 7.0, "bodyMax": 20.0,
              "bodyQuantile": 0.25, "pageTolerance": 0.2, "floor": 0.75, "margin": 6.0, "gap": 2.0,
              "unwrapRatio": 1.6, "sameSize": 0.98}


def page_rule(name: str, width: float, height: float) -> str:
    return f"@page {name}{{size:{width:.2f}pt {height:.2f}pt;margin:0}}"


def build_document_html(pages_html: list[tuple[str, float, float]], *, title: str, lang: str | None,
                        paper: bool, show_boxes: bool, devanagari: bool,
                        params: dict | None = None) -> tuple[str, list[str]]:
    """Full self-contained HTML for ``[(inner html, width_pt, height_pt)]``. Returns (html, fonts used)."""
    fonts, used = font_css(devanagari)
    rules, sections = [], []
    sizes: dict[tuple[float, float], str] = {}
    for inner, w, h in pages_html:
        key = (round(w, 2), round(h, 2))
        if key not in sizes:
            sizes[key] = f"s{len(sizes)}"
            rules.append(page_rule(sizes[key], w, h))
        sections.append(f'<section class="pg" style="page:{sizes[key]};width:{w:.2f}pt;height:{h:.2f}pt">'
                        f"{inner}</section>")
    body_cls = " ".join(c for c in ("paper" if paper else "", "boxes" if show_boxes else "") if c)
    csp = ("default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; font-src data:; "
           "script-src 'unsafe-inline'")
    js = FIT_JS.replace("__PARAMS__", json.dumps({**FIT_PARAMS, **(params or {})}))
    doc = (f'<!doctype html>\n<html lang="{html.escape(lang or "en")}"><head><meta charset="utf-8">'
           f'<meta http-equiv="Content-Security-Policy" content="{csp}"><title>{html.escape(title)}</title>'
           f"<style>{_katex_css()}\n{fonts}\n@page{{margin:0}}\n" + "\n".join(rules) + BASE_CSS + "</style></head>"
           f'<body class="{body_cls}">' + "".join(sections)
           + f"<script>{_katex_js()}</script><script>{js}</script></body></html>")
    return doc, used
