/* ScholarOCR app. Plain JS, no build step.
   Safety rule: model output is only ever placed with textContent / text nodes, or rendered by KaTeX with
   trust off. Nothing from the server is ever parsed as HTML. */
"use strict";
(() => {
  const TOKEN = document.querySelector('meta[name="scholarocr-token"]').content;
  const VERSION = document.querySelector('meta[name="scholarocr-version"]').content;
  const SPRITE = "/static/icons.svg#";
  const $ = (id) => document.getElementById(id);
  const EVENT_TYPES = ["stage", "job", "document_started", "page", "usage", "verification", "document", "delta", "error", "done"];
  const UNC_LABEL = { low: "Clear", medium: "Some doubt", high: "Doubtful", unresolved: "Unreadable" };
  const TYPE_LABEL = {
    title: "Title", heading: "Heading", subheading: "Subheading", paragraph: "Paragraph", text: "Text", handwriting: "Handwriting",
    equation: "Equation", chemical_equation: "Chemical equation", table: "Table", figure: "Figure", diagram: "Diagram", image: "Image",
    caption: "Caption", footnote: "Footnote", header: "Header", footer: "Footer", page_number: "Page number", list: "List", code: "Code",
    bibliography_entry: "Reference", reference: "Reference", uncertain: "Unreadable region",
  };
  // Native Word equations need pandoc on PATH; without it the export still works (see docs/INSTALLATION.md).
  const DOCX_NOTE = {
    native: "Word document with native, editable equations",
    fallback: "Word document. pandoc is not installed, so equations are inserted as LaTeX source and inline math as plain text. Install pandoc for native Word equations.",
  };
  const EXPORT_LABEL = { markdown: "Markdown", docx: "Word", latex: "LaTeX", "searchable-pdf": "Searchable PDF", json: "JSON", csv: "CSV", xlsx: "Excel" };

  const S = {
    prefs: null, engines: [], keys: {}, keyBackend: "file", setupNeeded: false, limits: { max_file_mb: 50, max_files: 30 },
    convs: [], convId: null, conv: null, messages: [], docViews: {}, attachments: [], run: null, engine: null, model: null,
    selectedEl: null, lastFocus: null,
  };

  // ------------------------------------------------------------------ DOM helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    append(el, kids);
    return el;
  }
  function append(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k === null || k === undefined || k === false) continue;
      el.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return el;
  }
  function setKids(el, ...kids) { el.replaceChildren(); return append(el, kids); }
  function icon(name, cls) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    if (cls) svg.setAttribute("class", cls);
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", SPRITE + name);
    svg.append(use);
    return svg;
  }
  function setIcon(btn, name) { const use = btn.querySelector("use"); if (use) use.setAttribute("href", SPRITE + name); }
  const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  const fmtTokens = (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k tokens` : `${n} tokens`;
  const fmtSecs = (s) => s >= 90 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const tokenUrl = (path) => `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };

  let toastTimer = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
  }

  // ------------------------------------------------------------------ API
  async function api(method, path, body) {
    const opts = { method, headers: { "X-ScholarOCR-Token": TOKEN } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opts); } catch { throw new Error("The app is not responding. Is scholarocr-app still running?"); }
    let data = null;
    try { data = await r.json(); } catch { /* not JSON */ }
    if (!r.ok) {
      const e = new Error((data && data.message) || `Request failed (${r.status})`);
      e.status = r.status; e.code = data && data.error;
      throw e;
    }
    return data;
  }

  function uploadFile(att) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/uploads?name=${encodeURIComponent(att.name)}`);
      xhr.setRequestHeader("X-ScholarOCR-Token", TOKEN);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) { att.progress = e.loaded / e.total; renderAttachments(); } };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error((data && data.message) || `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Upload failed: the app did not answer"));
      att.xhr = xhr;
      xhr.send(att.file);
    });
  }

  // ------------------------------------------------------------------ theme
  function effectiveTheme() {
    const t = document.documentElement.dataset.theme;
    if (t === "light" || t === "dark") return t;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    $("theme-btn").setAttribute("aria-label", `Use ${next} theme`);
    setIcon($("theme-btn"), next === "dark" ? "moon" : "sun");
  }
  applyTheme(store.get("scholarocr.theme") || "system");
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(document.documentElement.dataset.theme || "system"));

  // ------------------------------------------------------------------ math & text
  function mathNode(tex, display) {
    const el = document.createElement(display ? "div" : "span");
    if (window.katex) {
      try {
        window.katex.render(tex, el, { displayMode: !!display, throwOnError: false, trust: false, strict: "ignore", maxSize: 40, maxExpand: 400 });
        return el;
      } catch { /* fall through to raw text */ }
    }
    el.className = "math-raw";
    el.textContent = display ? tex : `$${tex}$`;
    return el;
  }

  // Inline content of a transcription: plain text with $...$ math.
  function inlineMath(text) {
    const out = [];
    const re = /\$\$([^$]+?)\$\$|\$((?:\\\$|[^$\n])+?)\$|\\\((.+?)\\\)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
      out.push(mathNode(m[1] || m[2] || m[3], !!m[1]));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(document.createTextNode(text.slice(last)));
    return out;
  }

  // Inline Markdown for chat answers: **bold**, *italic*, `code`, $math$, [p. N] citations.
  function inlineMd(text) {
    const out = [];
    const re = /\*\*(.+?)\*\*|`([^`]+)`|\$\$([^$]+?)\$\$|\$((?:\\\$|[^$\n])+?)\$|\[(?:pp?|pages?)\.?\s*(\d[\d,\s–-]*)\]|(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])|(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) out.push(h("strong", null, inlineMd(m[1])));
      else if (m[2] !== undefined) out.push(h("code", { text: m[2] }));
      else if (m[3] !== undefined) out.push(mathNode(m[3], true));
      else if (m[4] !== undefined) out.push(mathNode(m[4], false));
      else if (m[5] !== undefined) out.push(citation(m[5]));
      else out.push(h("em", null, inlineMd(m[6] || m[7])));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(document.createTextNode(text.slice(last)));
    return out;
  }

  function citation(spec) {
    const pages = [];
    for (const part of spec.split(",")) {
      const r = part.trim().split(/[–-]/).map((x) => parseInt(x, 10)).filter((x) => !isNaN(x));
      if (r.length === 1) pages.push(r[0]);
      else if (r.length === 2 && r[1] >= r[0] && r[1] - r[0] < 50) for (let i = r[0]; i <= r[1]; i++) pages.push(i);
    }
    const label = `p. ${spec.trim().replace(/\s+/g, " ")}`;
    return h("button", { type: "button", class: "cite", text: label, "aria-label": `Go to page ${spec.trim()}`,
      onclick: () => jumpToPage(pages[0]) });
  }

  function renderMarkdown(src) {
    const root = h("div", { class: "answer" });
    const lines = src.replace(/\r\n?/g, "\n").split("\n");
    const isBlockStart = (l) => /^(#{1,4})\s|^```|^\s*\$\$|^\s*([-*+]|\d+[.)])\s+|^\s*>/.test(l);
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if (/^```/.test(line)) {
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        root.append(h("pre", null, h("code", { text: buf.join("\n") })));
        continue;
      }
      if (/^\s*\$\$/.test(line)) {
        let body = line.trim().slice(2);
        if (body.trim().endsWith("$$") && body.trim().length > 2) { body = body.trim().slice(0, -2); i++; }
        else {
          const buf = [body]; i++;
          while (i < lines.length && !lines[i].trim().endsWith("$$")) buf.push(lines[i++]);
          if (i < lines.length) { buf.push(lines[i].trim().slice(0, -2)); i++; }
          body = buf.join("\n");
        }
        root.append(mathNode(body.trim(), true));
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { root.append(h("hr")); i++; continue; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        root.append(h(`h${Math.min(5, m[1].length + 2)}`, null, inlineMd(m[2])));
        i++;
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
        const head = cells(line); i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        const table = h("table", { class: "tbl" },
          h("thead", null, h("tr", null, head.map((c) => h("th", null, inlineMd(c))))),
          h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, inlineMd(c)))))));
        root.append(h("div", { class: "tbl-wrap" }, table));
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const list = h(ordered ? "ol" : "ul");
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          let item = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""); i++;
          while (i < lines.length && lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) item += " " + lines[i++].trim();
          list.append(h("li", null, inlineMd(item)));
        }
        root.append(list);
        continue;
      }
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        root.append(h("blockquote", null, inlineMd(buf.join(" "))));
        continue;
      }
      const buf = [line]; i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) buf.push(lines[i++]);
      const p = h("p");
      buf.forEach((l, k) => { if (k) p.append(h("br")); append(p, inlineMd(l)); });
      root.append(p);
    }
    return root;
  }

  // ------------------------------------------------------------------ document rendering
  const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  function stripQuestion(text, q) {
    let t = text;
    if (q && q.number) t = t.replace(new RegExp(`^\\s*(?:Q(?:uestion)?\\.?\\s*)?\\(?${escRe(q.number)}\\)?[.):]?\\s+`, "i"), "");
    if (q && q.marks) t = t.replace(new RegExp(`\\s*[\\[(]\\s*${escRe(q.marks)}\\s*(?:marks?|M)?\\s*[\\])]\\s*$`, "i"), "");
    return t.trim() ? t : text;
  }
  function qLabel(num, sub) {
    if (/^\d+$/.test(num)) return sub ? `(${num})` : `${num}.`;
    if (sub && /^([a-z]|[ivxl]+)$/i.test(num)) return `(${num})`;
    return num;
  }
  function marksLabel(m) { return /^[\[(]/.test(m) ? m : `[${m}]`; }

  function elementBody(el) {
    const t = el.type;
    const text = el.text || "";
    if (t === "equation" || t === "chemical_equation") {
      if (el.latex) return mathNode(el.latex, true);
      return h("p", null, h("span", { class: "u-text" }, inlineMath(text)));
    }
    if (t === "table" && el.table) return tableNode(el.table);
    if (t === "list" && el.items && el.items.length) return h("ul", null, el.items.map((it) => h("li", null, inlineMath(it))));
    if (t === "figure" || t === "diagram" || t === "image") {
      const f = el.figure || {};
      const kind = [f.figure_label, f.kind ? f.kind.replace(/_/g, " ") : TYPE_LABEL[t]].filter(Boolean).join(", ");
      return h("div", { class: "figure" }, icon("image"),
        h("div", null,
          h("div", { class: "figure-kind", text: kind || TYPE_LABEL[t] }),
          h("p", { class: "figure-desc", text: f.description || text || "No description." }),
          f.labels && f.labels.length ? h("div", { class: "figure-labels" }, f.labels.slice(0, 24).map((l) => h("span", { text: l }))) : null));
    }
    if (t === "code") return h("pre", null, h("code", { text }));
    return h("p", null, h("span", { class: "u-text" }, inlineMath(text)));
  }

  function tableNode(tbl) {
    const table = h("table", { class: "tbl" });
    if (tbl.caption || tbl.label) table.append(h("caption", { text: [tbl.label, tbl.caption].filter(Boolean).join(": ") }));
    const body = h("tbody");
    (tbl.rows || []).forEach((row, ri) => {
      const tr = h("tr");
      for (const c of row) {
        const cell = h(c.is_header || ri < (tbl.header_rows || 0) ? "th" : "td", {
          rowspan: c.row_span > 1 ? c.row_span : null, colspan: c.col_span > 1 ? c.col_span : null, class: c.uncertain ? "uc" : null });
        if (c.latex) cell.append(mathNode(c.latex, false)); else append(cell, inlineMath(c.text || ""));
        tr.append(cell);
      }
      body.append(tr);
    });
    table.append(body);
    return h("div", { class: "tbl-wrap" }, table);
  }

  function typeClass(t) {
    if (t === "title") return "el-title";
    if (t === "heading") return "el-heading";
    if (t === "subheading") return "el-subheading";
    if (t === "header" || t === "footer" || t === "page_number") return "el-chrome";
    if (t === "caption") return "el-caption";
    if (t === "footnote") return "el-footnote";
    if (t === "bibliography_entry" || t === "reference") return "el-ref";
    if (t === "equation" || t === "chemical_equation") return "el-math";
    if (t === "code") return "el-code";
    if (t === "list") return "el-list";
    if (t === "handwriting") return "el-handwriting";
    return "el-text";
  }

  function elementRow(docId, el) {
    const q = el.question;
    const sub = el.role === "subquestion";
    const shown = q ? { ...el, text: stripQuestion(el.text || "", q) } : el;
    const node = h("div", {
      class: `el ${typeClass(el.type)} u-${el.uncertainty}${sub ? " sub" : ""}`, tabindex: "0", role: "button",
      "aria-label": `${TYPE_LABEL[el.type] || el.type}${el.uncertainty !== "low" ? `, ${UNC_LABEL[el.uncertainty].toLowerCase()}` : ""}. Show source`,
      dataset: { eid: el.id },
    }, elementBody(shown));
    if (el.uncertainty === "unresolved" && el.type !== "uncertain") node.append(h("span", { class: "u-flag", text: "unreadable" }));
    const open = () => openSource(docId, el, node);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return h("div", { class: "row" },
      h("div", { class: `qnum${sub ? " sub" : ""}`, text: q && q.number ? qLabel(q.number, sub) : "" }),
      node,
      h("div", { class: "marks", text: q && q.marks ? marksLabel(q.marks) : "" }));
  }

  function pageStatusText(p) {
    if (p.status === "awaiting_transcription") return "Not read yet";
    if (p.status === "failed") return "Failed";
    if (p.status === "blank") return "Blank page";
    if (p.status === "pending") return "Not processed";
    if (p.route === "embedded_text" || p.route === "docx_text") return "From the file's text layer";
    return "";
  }

  function renderDoc(view, entry, pageStats) {
    const docId = view.document_id;
    const card = h("article", { class: "doc", dataset: { doc: docId } });
    const name = view.title || (view.filenames || [])[0] || "Document";
    const facts = h("div", { class: "doc-facts" });
    const ver = entry && entry.verification;
    let findings = null;
    if (ver && !ver.error) {
      const errors = (ver.counts && ver.counts.error) || 0, warns = (ver.counts && ver.counts.warning) || 0;
      const label = errors ? `Checks: ${plural(errors, "error", "errors")}` : warns ? `Checks passed, ${plural(warns, "warning", "warnings")}` : "Checks passed";
      if (ver.findings && ver.findings.length) {
        findings = h("ul", { class: "findings", hidden: true }, ver.findings.slice(0, 30).map((f) =>
          h("li", null, h("span", { class: "sev", text: f.severity }), f.page ? `p. ${f.page}: ` : "", f.message)));
        const btn = h("button", { type: "button", class: `fact ${errors ? "bad" : "ok"}`, "aria-expanded": "false" },
          icon(errors ? "triangle-alert" : "check"), label);
        btn.addEventListener("click", () => { findings.hidden = !findings.hidden; btn.setAttribute("aria-expanded", String(!findings.hidden)); });
        facts.append(btn);
      } else facts.append(h("span", { class: `fact ${errors ? "bad" : "ok"}` }, icon(errors ? "triangle-alert" : "check"), label));
    }
    if (view.pages_done < view.page_count) facts.append(h("span", { class: "fact bad" }, icon("info"), `${view.pages_done} of ${plural(view.page_count, "page", "pages")} read`));
    for (const u of ["medium", "high", "unresolved"]) {
      const n = (view.uncertainty || {})[u] || 0;
      if (n) facts.append(h("span", { class: "fact" }, h("span", { class: `swatch sw-${u}` }), `${n} ${UNC_LABEL[u].toLowerCase()}`));
    }
    const head = h("div", { class: "doc-head" },
      h("h3", { class: "doc-title", text: name }),
      h("p", { class: "doc-files", text: `${(view.filenames || []).join(", ")}, ${plural(view.page_count, "page", "pages")}` }),
      facts, findings);
    card.append(head);
    if (view.export_formats && view.export_formats.length) {
      card.append(h("div", { class: "exports" }, h("span", { class: "exports-label", text: "Export" }),
        view.export_formats.map((f) => {
          const a = h("a", { class: "export-link", href: tokenUrl(`/api/documents/${encodeURIComponent(docId)}/export/${f}`), download: "",
            title: f === "docx" ? DOCX_NOTE[view.native_equations ? "native" : "fallback"] : null },
            icon("download"), EXPORT_LABEL[f] || f);
          a.addEventListener("click", () => { a.classList.add("busy"); setTimeout(() => a.classList.remove("busy"), 2500); });
          return a;
        })));
    }
    for (const p of view.pages) {
      const stats = pageStats && pageStats[`${docId}:${p.page_number}`];
      const sub = [pageStatusText(p), p.label ? `printed ${p.label}` : "",
        stats && stats.usage && stats.usage.total_tokens ? fmtTokens(stats.usage.total_tokens) : "",
        stats && stats.seconds ? fmtSecs(stats.seconds) : ""].filter(Boolean);
      const section = h("section", { class: "sheet-page", id: `pg-${docId}-${p.page_number}`, dataset: { page: p.page_number } },
        h("div", { class: "page-head" },
          h("span", { class: "page-name", text: `Page ${p.page_number}` }),
          h("span", { class: "page-sub" }, sub.map((s, k) => [k ? h("span", { "aria-hidden": "true", text: "  /  " }) : null, s])),
          h("button", { type: "button", class: "btn btn-quiet btn-small", onclick: (e) => openPage(docId, p.page_number, e.currentTarget) },
            icon("eye"), "Page image")));
      const sheet = h("div", { class: "sheet" });
      if (p.elements.length) p.elements.forEach((el) => sheet.append(elementRow(docId, el)));
      else if (p.status === "failed") sheet.append(h("p", { class: "page-error", text: p.error || "This page could not be read." }));
      else if (p.status === "awaiting_transcription") sheet.append(h("p", { class: "page-empty", text: "This page has not been read yet." }));
      else if (p.status === "blank") sheet.append(h("p", { class: "page-empty", text: "Blank page." }));
      else sheet.append(h("p", { class: "page-empty", text: "No content found on this page." }));
      if (p.truncated) sheet.append(h("p", { class: "page-notes", text: `${p.truncated} more elements are in the exports.` }));
      if (p.notes && p.notes.length) sheet.append(h("p", { class: "page-notes", text: `Reader notes: ${p.notes.join("; ")}` }));
      section.append(sheet);
      card.append(section);
    }
    return card;
  }

  function jumpToPage(n) {
    const all = document.querySelectorAll(`.sheet-page[data-page="${n}"]`);
    const target = all[all.length - 1];
    if (!target) { toast(`Page ${n} is not shown in this conversation`); return; }
    target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    target.classList.remove("page-flash"); void target.offsetWidth; target.classList.add("page-flash");
  }

  // ------------------------------------------------------------------ source panel
  function openPanel(trigger) {
    S.lastFocus = trigger || document.activeElement;
    $("source-panel").hidden = false;
    $("sp-close").focus();
  }
  function closePanel() {
    $("source-panel").hidden = true;
    if (S.selectedEl) { S.selectedEl.classList.remove("selected"); S.selectedEl = null; }
    if (S.lastFocus && document.contains(S.lastFocus)) S.lastFocus.focus();
  }
  function cropImg(src, alt) {
    const img = h("img", { class: "sp-crop loading", alt, src });
    img.addEventListener("load", () => img.classList.remove("loading"));
    img.addEventListener("error", () => { img.replaceWith(h("p", { class: "page-error", text: "The source image could not be loaded." })); });
    return img;
  }
  function openSource(docId, el, node) {
    if (S.selectedEl) S.selectedEl.classList.remove("selected");
    S.selectedEl = node; node.classList.add("selected");
    const page = parseInt((el.id.match(/-p(\d+)-e/) || [])[1] || "0", 10);
    $("sp-title").textContent = `${TYPE_LABEL[el.type] || el.type}${page ? `, page ${page}` : ""}`;
    const body = $("sp-body");
    const facts = h("div", { class: "sp-facts" },
      h("span", { class: "fact" }, h("span", { class: `swatch sw-${el.uncertainty === "low" ? "none" : el.uncertainty}` }), UNC_LABEL[el.uncertainty]),
      el.handwritten ? h("span", { class: "fact", text: "Handwritten" }) : null,
      el.question && el.question.number ? h("span", { class: "fact", text: `Question ${el.question.number}` }) : null,
      el.question && el.question.marks ? h("span", { class: "fact", text: `Marks ${el.question.marks}` }) : null);
    const raw = el.type === "table" && el.table ? el.table.rows.map((r) => r.map((c) => c.text).join(" | ")).join("\n") : (el.text || "");
    const copy = h("button", { type: "button", class: "btn btn-small" }, icon("copy"), "Copy text");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(el.latex || raw); toast("Copied"); } catch { toast("Copy is blocked in this browser"); }
    });
    setKids(body, 
      cropImg(tokenUrl(`/api/documents/${encodeURIComponent(docId)}/elements/${encodeURIComponent(el.id)}/source.png`), "Source region of this element"),
      facts,
      el.reasons && el.reasons.length ? h("ul", { class: "sp-reasons" }, el.reasons.map((r) => h("li", { text: r }))) : null,
      h("p", { class: "sp-label", text: "Text" }), h("pre", { class: "sp-raw", text: raw || "(empty)" }),
      el.latex ? [h("p", { class: "sp-label", text: "LaTeX" }), h("pre", { class: "sp-raw", text: el.latex })] : null,
      h("div", { class: "sp-actions" }, copy,
        page ? h("button", { type: "button", class: "btn btn-small btn-quiet", onclick: () => openPage(docId, page, node) }, icon("eye"), "Whole page") : null));
    openPanel(node);
  }
  function openPage(docId, n, trigger) {
    $("sp-title").textContent = `Page ${n}`;
    $("sp-body").replaceChildren(cropImg(tokenUrl(`/api/documents/${encodeURIComponent(docId)}/pages/${n}/source.png`), `Page ${n} as uploaded`));
    openPanel(trigger);
  }

  // ------------------------------------------------------------------ messages
  function engineText(data) {
    const eng = S.engines.find((e) => e.id === data.engine);
    const model = eng && eng.models.find((m) => m.id === data.model);
    return { engine: (eng && eng.label) || data.engine_label || data.engine || "", model: (model && model.label) || data.model || "" };
  }

  function userMessage(m) {
    const atts = (m.data && m.data.attachments) || [];
    return h("li", { class: "msg-user", dataset: { mid: m.id } },
      atts.length ? h("div", { class: "msg-files" }, atts.map((a) => fileChip(a.name, `${a.kind === "image" ? "Image" : a.kind.toUpperCase()}, ${fmtBytes(a.bytes)}`, a.kind))) : null,
      m.text ? h("div", { class: "bubble", text: m.text }) : null);
  }
  function fileChip(name, meta, kind, extra) {
    return h("div", { class: "chip" }, h("span", { class: "chip-thumb" }, icon(kind === "image" ? "image" : "file-text")),
      h("span", { class: "chip-text" }, h("span", { class: "chip-name", text: name, title: name }), h("span", { class: "chip-meta", text: meta })), extra);
  }

  function assistantShell(m) {
    const body = h("div", { class: "asst-body", id: `body-${m.id}` });
    return h("li", { class: "msg-asst", dataset: { mid: m.id } }, h("img", { class: "avatar mark", src: "/static/mark.svg", alt: "ScholarOCR" }), body);
  }

  function banner(kind, text, actions) {
    return h("div", { class: `banner banner-${kind}`, role: kind === "error" ? "alert" : null },
      icon(kind === "error" ? "circle-x" : kind === "warn" ? "triangle-alert" : "info"),
      h("div", null, h("p", { text }), actions && actions.length ? h("div", { class: "banner-actions" }, actions) : null));
  }

  function metaLine(data, extra) {
    const et = engineText(data);
    const usage = data.usage || {};
    return h("p", { class: "asst-meta" },
      et.model ? h("span", { text: et.model, title: et.engine }) : h("span", { text: et.engine }),
      data.seconds_total ? h("span", { text: fmtSecs(data.seconds_total) }) : null,
      usage.total_tokens ? h("span", { text: fmtTokens(usage.total_tokens) }) : null, extra);
  }

  async function docView(docId) {
    if (S.docViews[docId]) return S.docViews[docId];
    const v = await api("GET", `/api/documents/${encodeURIComponent(docId)}`);
    S.docViews[docId] = v;
    return v;
  }

  function stateBanner(state, data) {
    if (state === "cancelled") return banner("info", "Stopped. Pages read before stopping are kept; attach the file again to read the rest.");
    if (state === "interrupted") return banner("warn", "The app closed while this was running. Attach the file again to finish reading it.");
    if (state === "error") return banner("error", (data.error && data.error.message) || "Reading failed.");
    if (state === "partial") {
      const pages = (data.documents || []).flatMap((d) => d.unread_pages || []);
      return banner("warn", `Some pages were not read${pages.length ? ` (page ${pages.join(", ")})` : ""}. Read pages are shown below; attach the file again to retry the rest.`);
    }
    return null;
  }

  function renderReadDone(body, m) {
    const data = m.data || {};
    setKids(body, metaLine(data), stateBanner(m.state, data));
    for (const d of data.documents || []) {
      const slot = h("div", { class: "doc-slot" }, h("div", { class: "progress" }, h("div", { class: "progress-head" }, h("span", { class: "spinner" }), h("span", { class: "progress-title", text: "Loading document" }))));
      body.append(slot);
      docView(d.document_id).then((v) => slot.replaceChildren(renderDoc(v, d, data.pages)))
        .catch((e) => slot.replaceChildren(banner("error", e.message)));
    }
  }

  function renderChatDone(body, m) {
    const data = m.data || {};
    setKids(body, 
      m.text ? renderMarkdown(m.text) : null,
      stateBanner(m.state === "partial" ? "done" : m.state, data),
      h("p", { class: "usage" }, [engineText(data).model, data.seconds ? fmtSecs(data.seconds) : "", data.usage && data.usage.total_tokens ? fmtTokens(data.usage.total_tokens) : "",
        data.truncated ? "document trimmed to fit" : ""].filter(Boolean).join(", ")));
  }

  function renderMessage(m) {
    if (m.role === "user") return userMessage(m);
    const li = assistantShell(m);
    const body = li.querySelector(".asst-body");
    if (S.run && S.run.messageId === m.id) renderLive(body);
    else if (m.kind === "read") renderReadDone(body, m);
    else renderChatDone(body, m);
    return li;
  }

  function renderMessages() {
    const list = $("messages");
    list.replaceChildren(...S.messages.map(renderMessage));
    updateEmpty();
  }

  // ------------------------------------------------------------------ live runs
  function newLive(kind) { return { kind, stage: "starting", message: "", docs: [], error: null, answer: "", usage: null, cancelling: false, engine: null, model: null }; }

  function applyEvent(ev) {
    const run = S.run;
    if (!run) return;
    const L = run.live;
    const doc = (id) => L.docs.find((d) => d.id === id);
    switch (ev.type) {
      case "stage":
        L.stage = ev.stage; L.message = ev.message || "";
        if (ev.engine) { L.engine = ev.engine; L.model = ev.model; }
        if (ev.stage === "cancelling") L.cancelling = true;
        break;
      case "document_started":
        L.docs.push({ id: ev.document_id, files: ev.files, pageCount: ev.page_count, job: null, pages: {}, view: null, verification: null });
        break;
      case "job": { const d = doc(ev.document_id); if (d) d.job = ev; L.stage = "preparing"; break; }
      case "page": {
        const d = doc(ev.document_id);
        if (!d) break;
        const prev = d.pages[ev.page] || { started: Date.now(), usage: {} };
        d.pages[ev.page] = { ...prev, ...ev, usage: prev.usage, started: prev.started };
        if (ev.status !== "reading" && ev.status !== "retrying" && ev.status !== "rotated") d.pages[ev.page].finished = Date.now();
        L.stage = "reading";
        break;
      }
      case "usage":
        if (ev.document_id) {
          const d = doc(ev.document_id);
          const p = d && d.pages[ev.page];
          if (p) for (const [k, v] of Object.entries(ev.usage || {})) p.usage[k] = (p.usage[k] || 0) + v;
        } else L.usage = ev.usage;
        break;
      case "verification": { const d = doc(ev.document_id); if (d) d.verification = ev; L.stage = "verifying"; break; }
      case "document": { const d = doc(ev.document_id); if (d) { d.view = ev.view; S.docViews[ev.document_id] = ev.view; } break; }
      case "delta": L.answer += ev.text; break;
      case "error": L.error = ev; break;
      case "done": finishRun(ev); return;
      default: break;
    }
    scheduleLive();
  }

  let liveFrame = 0;
  function scheduleLive() {
    if (liveFrame) return;
    liveFrame = requestAnimationFrame(() => {
      liveFrame = 0;
      if (!S.run) return;
      const body = $(`body-${S.run.messageId}`);
      if (body) { renderLive(body); keepScrolled(); }
    });
  }

  function stopButton() {
    const L = S.run.live;
    return h("button", { type: "button", class: "btn btn-small", disabled: L.cancelling, onclick: cancelRun }, icon("stop"), L.cancelling ? "Stopping" : "Stop");
  }

  function pageRow(n, p, total) {
    const live = p.status === "reading" || p.status === "retrying" || p.status === "rotated";
    const statusText = {
      reading: "Reading", retrying: p.message || "Asking again", rotated: "Re-oriented, reading again", done: `${plural(p.elements || 0, "element", "elements")}`,
      rejected: p.message || "Rejected by the validator", timeout: p.message || "Timed out", error: p.message || "Failed",
    }[p.status] || p.status;
    const secs = ((p.finished || Date.now()) - p.started) / 1000;
    const meta = [p.usage && p.usage.total_tokens ? fmtTokens(p.usage.total_tokens) : "", fmtSecs(secs)].filter(Boolean).join(", ");
    const bad = ["rejected", "timeout", "error"].includes(p.status);
    return h("li", { class: `prow${p.status === "done" ? " done" : ""}${bad ? " bad" : ""}` },
      live ? h("span", { class: "spinner", role: "img", "aria-label": "working" }) : icon(p.status === "done" ? "check" : bad ? "triangle-alert" : "info"),
      h("span", null, h("strong", { text: `Page ${n}` }), total ? h("span", { class: "prow-status", text: ` of ${total}` }) : null, h("span", { class: "prow-status", text: `  ${statusText}` })),
      h("span", { class: "prow-meta", dataset: live ? { live: "1" } : {}, text: meta }),
      p.errors && p.errors.length && p.status !== "done" ? h("ul", { class: "prow-errors" }, p.errors.slice(0, 4).map((e) => h("li", { text: `${e.path}: ${e.problem}` }))) : null);
  }

  function renderLive(body) {
    const L = S.run.live;
    const et = engineText({ engine: L.engine, model: L.model });
    if (L.kind === "chat") {
      setKids(body, 
        L.answer ? renderMarkdown(L.answer) : h("div", { class: "progress" }, h("div", { class: "progress-head" }, h("span", { class: "spinner" }),
          h("span", { class: "progress-title", text: L.cancelling ? "Stopping" : `Thinking with ${et.model || et.engine}` }), stopButton())),
        L.answer ? h("p", { class: "usage caret" }, h("span", { text: et.model })) : null);
      return;
    }
    const nodes = [];
    for (const d of L.docs) {
      if (d.view) { nodes.push(renderDoc(d.view, { verification: d.verification }, null)); continue; }
      const pages = Object.keys(d.pages).map(Number).sort((a, b) => a - b);
      const name = d.files.length === 1 ? d.files[0] : `${d.files[0]} and ${d.files.length - 1} more`;
      let title = "Preparing pages", sub = name;
      if (pages.length) {
        const cur = d.pages[pages[pages.length - 1]];
        title = cur.status === "reading" || cur.status === "retrying" ? `Reading page ${cur.index} of ${cur.total}` : `Read ${pages.filter((n) => d.pages[n].status === "done").length} of ${cur.total} pages`;
      } else if (L.stage === "verifying" || d.verification) title = "Checking the result";
      const job = d.job;
      const prog = h("div", { class: "progress" },
        h("div", { class: "progress-head" }, h("span", { class: "spinner" }), h("span", { class: "progress-title", text: L.cancelling ? "Stopping" : title }), stopButton()),
        h("p", { class: "progress-sub" }, h("span", { text: sub }), et.model ? h("span", { text: `, ${et.model}` }) : null),
        !pages.length && job && job.total ? h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(job.total), "aria-valuenow": String(job.completed) },
          h("i", { style: `width:${Math.round((100 * job.completed) / job.total)}%` })) : null,
        pages.length ? h("ul", { class: "pages-list" }, pages.map((n) => pageRow(n, d.pages[n], null))) : null);
      nodes.push(prog);
    }
    if (!L.docs.length) {
      nodes.push(h("div", { class: "progress" }, h("div", { class: "progress-head" }, h("span", { class: "spinner" }),
        h("span", { class: "progress-title", text: L.cancelling ? "Stopping" : (L.message || "Starting") }), stopButton()),
        h("p", { class: "progress-sub", text: et.model || et.engine })));
    }
    if (L.error) nodes.push(banner("error", L.error.message));
    setKids(body, ...nodes);
  }

  setInterval(() => {
    if (!S.run) return;
    for (const node of document.querySelectorAll('.prow-meta[data-live="1"]')) {
      const li = node.closest(".prow");
      if (!li) continue;
      const d = S.run.live.docs.find((x) => Object.values(x.pages).some((p) => !p.finished));
      if (!d) continue;
      const p = Object.values(d.pages).find((x) => !x.finished);
      if (p) node.textContent = [p.usage && p.usage.total_tokens ? fmtTokens(p.usage.total_tokens) : "", fmtSecs((Date.now() - p.started) / 1000)].filter(Boolean).join(", ");
    }
  }, 1000);

  function followRun(runId, messageId, kind) {
    if (S.run && S.run.es) S.run.es.close();
    const es = new EventSource(tokenUrl(`/api/runs/${encodeURIComponent(runId)}/events`));
    S.run = { id: runId, messageId, kind, es, live: newLive(kind) };
    const seen = new Set();
    const onEvent = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (seen.has(ev.seq)) return;
      seen.add(ev.seq);
      applyEvent(ev);
    };
    for (const t of EVENT_TYPES) es.addEventListener(t, onEvent);
    es.addEventListener("error", () => {
      if (es.readyState === EventSource.CLOSED && S.run && S.run.id === runId) {
        S.run = null;
        updateComposer();
        if (S.convId) loadConversation(S.convId, { keepScroll: true });
      }
    });
    updateComposer();
    scheduleLive();
  }

  async function finishRun(ev) {
    const run = S.run;
    if (!run) return;
    run.es.close();
    S.run = null;
    updateComposer();
    if (ev.state === "done" && run.kind === "read") toast("Document read");
    if (ev.state === "error") toast("Reading failed; the reason is shown in the conversation");
    await loadConversation(S.convId, { keepScroll: true });
    refreshHistory();
  }

  async function cancelRun() {
    if (!S.run) return;
    S.run.live.cancelling = true;
    scheduleLive();
    try { await api("POST", `/api/runs/${encodeURIComponent(S.run.id)}/cancel`); } catch (e) { toast(e.message); }
  }

  // ------------------------------------------------------------------ conversations & history
  function setRoute(cid) {
    const target = cid ? `#/c/${cid}` : "#/";
    if (location.hash !== target) history.pushState(null, "", target);
  }

  async function refreshHistory() {
    try {
      const data = await api("GET", "/api/conversations");
      S.convs = data.conversations;
    } catch { return; }
    const list = $("history-list");
    list.replaceChildren(...S.convs.map((c) => {
      const running = S.run && S.convId === c.id;
      const more = h("button", { type: "button", class: "icon-btn h-more", "aria-label": `Actions for ${c.title}`, "aria-haspopup": "menu", "aria-expanded": "false" }, icon("more"));
      more.addEventListener("click", (e) => { e.stopPropagation(); openRowMenu(more, c); });
      return h("li", { class: `history-item${c.id === S.convId ? " current" : ""}` },
        h("button", { type: "button", class: "history-link", "aria-current": c.id === S.convId ? "page" : null, onclick: () => openConversation(c.id) },
          running ? h("span", { class: "h-dot", "aria-label": "working" }) : null, h("span", { class: "h-title", text: c.title })),
        more);
    }));
    $("history-empty").hidden = S.convs.length > 0;
  }

  async function openConversation(cid) {
    closeNarrowSidebar();
    if (cid === S.convId) return;
    setRoute(cid);
    await loadConversation(cid);
  }

  async function loadConversation(cid, opts = {}) {
    if (!cid) { newConversation(); return; }
    let data;
    try { data = await api("GET", `/api/conversations/${encodeURIComponent(cid)}`); } catch (e) {
      toast(e.status === 404 ? "That conversation no longer exists" : e.message);
      newConversation();
      return;
    }
    const scroller = $("scroller");
    const prev = scroller.scrollTop;
    S.convId = cid; S.conv = data.conversation; S.messages = data.messages;
    $("topbar-title").textContent = data.conversation.title;
    document.title = `${data.conversation.title} | ScholarOCR`;
    if (data.active_run && (!S.run || S.run.id !== data.active_run.id)) followRun(data.active_run.id, data.active_run.message_id, data.active_run.kind);
    renderMessages();
    updateComposer();
    if (opts.keepScroll) scroller.scrollTop = prev; else scroller.scrollTop = scroller.scrollHeight;
    for (const li of $("history-list").children) li.classList.remove("current");
    refreshHistory();
  }

  function newConversation() {
    if (S.run && S.run.es) S.run.es.close();
    S.run = null; S.convId = null; S.conv = null; S.messages = [];
    $("topbar-title").textContent = "";
    document.title = "ScholarOCR";
    setRoute(null);
    renderMessages();
    updateComposer();
    refreshHistory();
    closeNarrowSidebar();
    $("composer-input").focus();
  }

  function updateEmpty() {
    const hasMsgs = S.messages.length > 0;
    $("setup").hidden = !(S.setupNeeded && !hasMsgs);
    $("empty").hidden = hasMsgs || S.setupNeeded;
    const saved = S.prefs && S.prefs.saved_instructions;
    $("saved-chip").hidden = !saved;
    if (saved) $("saved-chip-text").textContent = `Saved instructions: ${saved.replace(/\s+/g, " ")}`;
  }

  function keepScrolled() {
    const s = $("scroller");
    if (s.scrollHeight - s.scrollTop - s.clientHeight < 240) s.scrollTop = s.scrollHeight;
  }

  // row menu (rename / delete)
  function openRowMenu(btn, conv) {
    const menu = $("row-menu");
    menu.replaceChildren(
      h("button", { type: "button", class: "menu-item", role: "menuitem", onclick: () => { closeMenus(); renameConversation(conv); } }, icon("pencil"), h("span", { text: "Rename" })),
      h("button", { type: "button", class: "menu-item danger", role: "menuitem", onclick: () => { closeMenus(); deleteConversation(conv); } }, icon("trash-2"), h("span", { text: "Delete" })));
    showMenu(menu, btn);
  }
  async function renameConversation(conv) {
    const title = prompt("Name", conv.title);
    if (title === null) return;
    try {
      await api("PATCH", `/api/conversations/${encodeURIComponent(conv.id)}`, { title });
      if (conv.id === S.convId) $("topbar-title").textContent = title.trim() || "Untitled";
      refreshHistory();
    } catch (e) { toast(e.message); }
  }
  function deleteConversation(conv) {
    const dlg = $("confirm-dialog");
    $("confirm-title").textContent = `Delete "${conv.title}"?`;
    dlg.returnValue = "";
    dlg.showModal();
    dlg.addEventListener("close", async function onClose() {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue !== "ok") return;
      try {
        await api("DELETE", `/api/conversations/${encodeURIComponent(conv.id)}`);
        if (conv.id === S.convId) newConversation(); else refreshHistory();
        toast("Deleted");
      } catch (e) { toast(e.message); }
    });
  }

  // ------------------------------------------------------------------ menus
  let openMenu = null;
  function showMenu(menu, anchor) {
    closeMenus();
    menu.hidden = false;
    const r = anchor.getBoundingClientRect();
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    let left = Math.min(Math.max(12, r.right - mw), innerWidth - mw - 12);
    let top = r.top - mh - 8;
    if (top < 12) top = Math.min(r.bottom + 8, innerHeight - mh - 12);
    menu.style.left = `${left}px`; menu.style.top = `${top}px`;
    anchor.setAttribute("aria-expanded", "true");
    openMenu = { menu, anchor };
    const first = menu.querySelector('[aria-checked="true"]') || menu.querySelector(".menu-item:not([aria-disabled='true'])") || menu.querySelector(".menu-item");
    if (first) first.focus();
  }
  function closeMenus(returnFocus) {
    if (!openMenu) return;
    openMenu.menu.hidden = true;
    openMenu.anchor.setAttribute("aria-expanded", "false");
    if (returnFocus) openMenu.anchor.focus();
    openMenu = null;
  }
  for (const m of [$("engine-menu"), $("row-menu")]) {
    m.addEventListener("keydown", (e) => {
      const items = [...m.querySelectorAll(".menu-item")];
      const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
      else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); closeMenus(true); }
    });
  }
  document.addEventListener("mousedown", (e) => {
    if (openMenu && !openMenu.menu.contains(e.target) && !openMenu.anchor.contains(e.target)) closeMenus();
  });

  // ------------------------------------------------------------------ engine picker
  function currentEngine() { return S.engines.find((e) => e.id === S.engine); }
  function chooseInitialEngine() {
    const pref = S.prefs.engine;
    const avail = S.engines.filter((e) => e.available);
    let eng = pref !== "auto" ? S.engines.find((e) => e.id === pref && e.available) : null;
    eng = eng || avail[0] || null;
    S.engine = eng ? eng.id : null;
    S.model = eng ? eng.default_model : null;
    updateEngineButton();
  }
  function modelLabel(eng, id) { const m = eng && eng.models.find((x) => x.id === id); return m ? m.label : id; }
  function updateEngineButton() {
    const eng = currentEngine();
    const label = $("engine-btn-label");
    if (!eng) { label.textContent = "No engine"; $("engine-btn").title = "Set up an engine in Settings"; return; }
    label.textContent = eng.id === "antigravity" ? modelLabel(eng, S.model) : `${eng.id === "gemini" ? "Gemini key" : "OpenRouter key"}, ${S.model}`;
    $("engine-btn").title = `${eng.label}: ${modelLabel(eng, S.model)}`;
    $("engine-btn").setAttribute("aria-label", `Engine: ${eng.label}, model ${modelLabel(eng, S.model)}. Change`);
  }
  function buildEngineMenu() {
    const menu = $("engine-menu");
    const kids = [h("div", { class: "menu-heading", text: "Engine" })];
    for (const e of S.engines) {
      kids.push(h("button", { type: "button", class: "menu-item", role: "menuitemradio", "aria-checked": String(e.id === S.engine), "aria-disabled": e.available ? null : "true",
        onclick: () => { if (!e.available) { closeMenus(); openSettings("keys"); return; } selectEngine(e.id); } },
        icon("check", "mi-check"), h("span", { class: "mi-text" }, h("span", { text: e.label }), h("span", { class: "mi-sub", text: e.available ? (e.id === "antigravity" ? "Signed-in Google account, no key" : "Key stored on this computer") : (e.reason || "Not set up") }))));
    }
    const eng = currentEngine();
    if (eng && eng.models.length > 1) {
      kids.push(h("div", { class: "menu-sep" }), h("div", { class: "menu-heading", text: "Model" }));
      for (const m of eng.models) {
        kids.push(h("button", { type: "button", class: "menu-item", role: "menuitemradio", "aria-checked": String(m.id === S.model), onclick: () => selectModel(m.id) },
          icon("check", "mi-check"), h("span", { class: "mi-text" }, h("span", { text: m.label }), h("span", { class: "mi-sub", text: m.id }))));
      }
    }
    menu.replaceChildren(...kids);
  }
  async function savePrefs(changes) {
    try { const r = await api("PUT", "/api/prefs", changes); S.prefs = r.prefs; } catch (e) { toast(e.message); }
  }
  function selectEngine(id) {
    const e = S.engines.find((x) => x.id === id);
    S.engine = id; S.model = e.default_model;
    updateEngineButton(); buildEngineMenu(); closeMenus(true);
    savePrefs({ engine: id });
  }
  function selectModel(id) {
    S.model = id;
    updateEngineButton(); closeMenus(true);
    const key = { antigravity: "agy_model", gemini: "gemini_model", openrouter: "openrouter_model" }[S.engine];
    savePrefs({ [key]: id });
    const e = currentEngine(); if (e) e.default_model = id;
  }
  $("engine-btn").addEventListener("click", () => {
    if (openMenu && openMenu.menu === $("engine-menu")) { closeMenus(true); return; }
    buildEngineMenu(); showMenu($("engine-menu"), $("engine-btn"));
  });

  // ------------------------------------------------------------------ attachments
  function kindOf(file) {
    if (/^image\//.test(file.type) || /\.(png|jpe?g|webp|tiff?|gif|bmp)$/i.test(file.name)) return "image";
    if (/\.docx$/i.test(file.name)) return "docx";
    return "pdf";
  }
  function addFiles(files) {
    const list = [...files];
    if (!list.length) return;
    const room = (S.limits.max_files || 30) - S.attachments.length;
    if (list.length > room) toast(`At most ${S.limits.max_files} files per message`);
    for (const file of list.slice(0, Math.max(0, room))) {
      const att = { local: Math.random().toString(36).slice(2), file, name: file.name || "upload", size: file.size, kind: kindOf(file), status: "uploading", progress: 0 };
      if (file.size > S.limits.max_file_mb * 1048576) { att.status = "error"; att.error = `Larger than ${S.limits.max_file_mb} MB`; }
      if (att.kind === "image" && att.status !== "error") { try { att.url = URL.createObjectURL(file); } catch { /* no preview */ } }
      S.attachments.push(att);
      if (att.status === "uploading") {
        uploadFile(att).then((meta) => { att.status = "ready"; att.id = meta.id; att.name = meta.name; att.kind = meta.kind; })
          .catch((e) => { att.status = "error"; att.error = e.message; })
          .finally(() => { renderAttachments(); updateComposer(); });
      }
    }
    renderAttachments(); updateComposer();
  }
  function removeAttachment(att) {
    if (att.xhr && att.status === "uploading") att.xhr.abort();
    if (att.id) api("DELETE", `/api/uploads/${att.id}`).catch(() => {});
    if (att.url) URL.revokeObjectURL(att.url);
    S.attachments = S.attachments.filter((a) => a !== att);
    renderAttachments(); updateComposer();
    $("composer-input").focus();
  }
  function renderAttachments() {
    $("attachments").replaceChildren(...S.attachments.map((a) => {
      const meta = a.status === "uploading" ? `Uploading ${Math.round(a.progress * 100)}%` : a.status === "error" ? a.error : `${a.kind === "image" ? "Image" : a.kind.toUpperCase()}, ${fmtBytes(a.size)}`;
      const thumb = a.url ? h("img", { class: "chip-thumb", src: a.url, alt: "" }) : h("span", { class: "chip-thumb" }, icon(a.kind === "image" ? "image" : "file-text"));
      return h("li", { class: `chip${a.status === "error" ? " error" : ""}` }, thumb,
        h("span", { class: "chip-text" }, h("span", { class: "chip-name", text: a.name, title: a.name }), h("span", { class: "chip-meta", text: meta })),
        h("button", { type: "button", class: "icon-btn chip-remove", "aria-label": `Remove ${a.name}`, onclick: () => removeAttachment(a) }, icon("x")),
        a.status === "uploading" ? h("span", { class: "chip-bar", style: `width:${Math.round(a.progress * 100)}%` }) : null);
    }));
  }
  $("attach-btn").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });

  let dragDepth = 0;
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  window.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $("dropzone").hidden = false; });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("dropzone").hidden = true; });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; $("dropzone").hidden = true;
    if (S.run) { toast("Wait for the current run to finish"); return; }
    addFiles(e.dataTransfer.files);
  });
  window.addEventListener("paste", (e) => {
    const files = e.clipboardData && [...e.clipboardData.files];
    if (files && files.length) { e.preventDefault(); addFiles(files); }
  });

  // ------------------------------------------------------------------ composer
  const input = $("composer-input");
  function autosize() { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.4)}px`; }
  input.addEventListener("input", () => { autosize(); updateComposer(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!$("send-btn").disabled) send(); }
  });

  function updateComposer() {
    const btn = $("send-btn");
    const running = !!(S.run && S.convId && S.run.messageId && S.messages.some((m) => m.id === S.run.messageId));
    btn.classList.toggle("stop", running);
    setIcon(btn, running ? "stop" : "arrow-up");
    btn.setAttribute("aria-label", running ? "Stop" : "Send");
    const uploading = S.attachments.some((a) => a.status === "uploading");
    const ready = S.attachments.filter((a) => a.status === "ready").length;
    const text = input.value.trim();
    const hasDocs = S.messages.some((m) => m.kind === "upload" || m.kind === "read");
    btn.disabled = running ? false : (uploading || !S.engine || (!ready && !(text && hasDocs)));
    input.placeholder = hasDocs ? "Ask about this document" : "Attach a file, add instructions";
    $("attach-btn").disabled = running;
  }

  async function send() {
    if (S.run && S.messages.some((m) => m.id === S.run.messageId)) { cancelRun(); return; }
    const text = input.value.trim();
    const ready = S.attachments.filter((a) => a.status === "ready");
    if (S.attachments.some((a) => a.status === "error") && !ready.length) { toast("Remove the files that failed, then try again"); return; }
    if (!ready.length && !text) return;
    const btn = $("send-btn");
    btn.disabled = true;
    try {
      if (!S.convId) {
        const c = await api("POST", "/api/conversations");
        S.convId = c.id; S.conv = c; S.messages = [];
        setRoute(c.id);
      }
      const res = await api("POST", `/api/conversations/${encodeURIComponent(S.convId)}/messages`,
        { text, uploads: ready.map((a) => a.id), engine: S.engine, model: S.model });
      for (const a of S.attachments) if (a.url) URL.revokeObjectURL(a.url);
      S.attachments = S.attachments.filter((a) => a.status !== "ready");
      input.value = ""; autosize(); renderAttachments();
      S.messages.push(res.user_message, res.assistant_message);
      followRun(res.run_id, res.assistant_message.id, res.assistant_message.kind === "chat" ? "chat" : "read");
      renderMessages();
      $("scroller").scrollTop = $("scroller").scrollHeight;
      refreshHistory();
    } catch (e) {
      toast(e.message);
      if (e.code === "no_engine") loadState(true);
    } finally {
      updateComposer();
    }
  }
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });

  // ------------------------------------------------------------------ settings
  function renderEngineList() {
    const list = $("engine-list");
    const pref = S.prefs.engine;
    const opts = [{ id: "auto", label: "Automatic", available: true, reason: "Antigravity when it is set up, else a stored key" }, ...S.engines];
    list.replaceChildren(...opts.map((e) => h("label", { class: `engine-opt${e.available ? "" : " unavailable"}` },
      h("input", { type: "radio", name: "engine-pref", value: e.id, checked: e.id === pref }),
      h("span", null, h("span", { class: "eo-name", text: e.label }), h("br"),
        h("span", { class: "eo-sub", text: e.id === "auto" ? e.reason : e.available ? (e.id === "antigravity" ? `${plural(e.models.length, "model", "models")} available` : "Key stored") : (e.id === "antigravity" ? e.reason : "No key stored yet; add one below.") })))));
  }
  function renderKeyStatus() {
    $("key-backend-note").textContent = S.keyBackend === "keychain" ? "Keys are stored in the system keychain and never shown again." :
      "Keys are stored in your user-level scholarocr.env file and never shown again.";
    for (const p of ["gemini", "openrouter"]) {
      const set = S.keys[p] && S.keys[p].set;
      const el = $(`key-status-${p}`);
      el.textContent = set ? "A key is stored." : "No key stored.";
      el.classList.toggle("set", !!set);
      document.querySelector(`[data-key-remove="${p}"]`).hidden = !set;
    }
  }
  function openSettings(focus) {
    const p = S.prefs;
    $("saved-instructions").value = p.saved_instructions || "";
    $("max-file-mb").value = p.max_file_mb; $("max-files").value = p.max_files; $("max-pages").value = p.max_pages;
    for (const r of document.querySelectorAll('input[name="theme"]')) r.checked = r.value === (store.get("scholarocr.theme") || p.theme || "system");
    $("settings-error").hidden = true;
    $("dialog-version").textContent = `ScholarOCR ${VERSION}`;
    renderEngineList(); renderKeyStatus();
    $("settings-dialog").showModal();
    if (focus === "keys") $("key-gemini").focus();
    else if (focus === "instructions") $("saved-instructions").focus();
  }
  async function saveSettings() {
    const theme = (document.querySelector('input[name="theme"]:checked') || {}).value || "system";
    const engine = (document.querySelector('input[name="engine-pref"]:checked') || {}).value || "auto";
    const changes = { saved_instructions: $("saved-instructions").value, engine, theme,
      max_file_mb: Number($("max-file-mb").value), max_files: Number($("max-files").value), max_pages: Number($("max-pages").value) };
    try {
      const r = await api("PUT", "/api/prefs", changes);
      S.prefs = r.prefs;
      store.set("scholarocr.theme", theme); applyTheme(theme);
      $("settings-dialog").close();
      await loadState(false);
      toast("Settings saved");
    } catch (e) { const err = $("settings-error"); err.textContent = e.message; err.hidden = false; }
  }
  $("settings-btn").addEventListener("click", () => openSettings());
  $("saved-chip").addEventListener("click", () => openSettings("instructions"));
  $("settings-close").addEventListener("click", () => $("settings-dialog").close());
  $("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
  $("settings-save").addEventListener("click", saveSettings);
  $("engines-refresh").addEventListener("click", async () => { await loadState(true); renderEngineList(); toast("Engines checked"); });
  for (const b of document.querySelectorAll("[data-key-save]")) {
    b.addEventListener("click", async () => {
      const p = b.dataset.keySave, field = $(`key-${p}`);
      if (!field.value.trim()) { field.focus(); return; }
      try { await api("PUT", `/api/keys/${p}`, { key: field.value.trim() }); field.value = ""; await loadState(false); renderKeyStatus(); renderEngineList(); toast("Key saved"); }
      catch (e) { const err = $("settings-error"); err.textContent = e.message; err.hidden = false; }
    });
  }
  for (const b of document.querySelectorAll("[data-key-remove]")) {
    b.addEventListener("click", async () => {
      try { await api("DELETE", `/api/keys/${b.dataset.keyRemove}`); await loadState(false); renderKeyStatus(); renderEngineList(); toast("Key removed"); }
      catch (e) { toast(e.message); }
    });
  }
  $("setup-recheck").addEventListener("click", async () => { $("setup-recheck").disabled = true; await loadState(true); $("setup-recheck").disabled = false; toast(S.setupNeeded ? "Still no engine found" : "Engine found"); });
  $("setup-keys").addEventListener("click", () => openSettings("keys"));

  // ------------------------------------------------------------------ sidebar & global keys
  const shell = $("shell");
  const narrow = () => matchMedia("(max-width: 760px)").matches;
  function setCollapsed(c) {
    shell.classList.toggle("sb-collapsed", c);
    $("sb-toggle").setAttribute("aria-expanded", String(!c));
    $("sb-toggle").setAttribute("aria-label", c ? "Expand sidebar" : "Collapse sidebar");
    setIcon($("sb-toggle"), c ? "panel-left-open" : "panel-left-close");
    store.set("scholarocr.sidebar", c ? "collapsed" : "open");
  }
  function closeNarrowSidebar() { if (shell.classList.contains("sb-open")) { shell.classList.remove("sb-open"); $("scrim").hidden = true; } }
  $("sb-toggle").addEventListener("click", () => { if (narrow()) closeNarrowSidebar(); else setCollapsed(!shell.classList.contains("sb-collapsed")); });
  $("open-history").addEventListener("click", () => { shell.classList.add("sb-open"); $("scrim").hidden = false; $("new-btn").focus(); });
  $("scrim").addEventListener("click", closeNarrowSidebar);
  $("new-btn").addEventListener("click", newConversation);
  $("home-link").addEventListener("click", (e) => { e.preventDefault(); newConversation(); });
  $("sp-close").addEventListener("click", closePanel);
  $("theme-btn").addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    store.set("scholarocr.theme", next); applyTheme(next);
  });
  if (store.get("scholarocr.sidebar") === "collapsed") setCollapsed(true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (openMenu) { closeMenus(true); return; }
      if (!$("source-panel").hidden) { closePanel(); return; }
      if (shell.classList.contains("sb-open")) { closeNarrowSidebar(); $("open-history").focus(); }
    }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "O" || e.key === "o")) { e.preventDefault(); newConversation(); }
  });
  window.addEventListener("popstate", () => routeFromHash());

  function routeFromHash() {
    const m = location.hash.match(/^#\/c\/([A-Za-z0-9_]+)$/);
    if (m) { if (m[1] !== S.convId) loadConversation(m[1]); }
    else if (S.convId) newConversation();
  }

  // ------------------------------------------------------------------ boot
  async function loadState(refresh) {
    try {
      const st = await api("GET", `/api/state${refresh ? "?refresh=1" : ""}`);
      S.prefs = st.prefs; S.engines = st.engines; S.keys = st.keys; S.keyBackend = st.key_backend; S.setupNeeded = st.setup_needed; S.limits = st.limits;
      const reasons = st.engines.filter((e) => e.id === "antigravity" && !e.available).map((e) => e.reason);
      $("setup-reason").textContent = reasons[0] || "";
      $("setup-reason").hidden = !reasons[0];
      if (!S.engine || !currentEngine() || !currentEngine().available) chooseInitialEngine(); else updateEngineButton();
      if (!store.get("scholarocr.theme") && st.prefs.theme !== "system") applyTheme(st.prefs.theme);
      updateEmpty(); updateComposer();
    } catch (e) { toast(e.message); }
  }

  (async () => {
    await loadState(false);
    await refreshHistory();
    routeFromHash();
    updateEmpty();
    if (!S.convId) input.focus();
  })();
})();
