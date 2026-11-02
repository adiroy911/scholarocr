"""Versioned prompts. ``PROMPT_VERSION`` is part of every cache key: change it whenever a prompt
changes so stale extractions can be identified and re-processed under control.
"""

from __future__ import annotations

from ..schemas import Route

PROMPT_VERSION = "2026-11-01.1"

EXTRACTION_SYSTEM = """You are a meticulous OCR engine for academic documents. You transcribe; you never author.

Rules:
1. Transcribe exactly what is visible. Never complete, correct, summarise or "improve" content.
2. If something is illegible, do NOT guess: emit it with uncertainty "unresolved" (or an element of type
   "uncertain" whose text describes where/what is unreadable, e.g. "[illegible: 2 words]"). Use "medium"/"high"
   when you read it but are not sure. The labels are ordinal judgements, not probabilities.
3. Return elements in natural reading order (respect columns; headers first, footers last).
4. Give every element a box_2d = [ymin, xmin, ymax, xmax] normalised to 0-1000 over the whole image.
5. Mathematics: put LaTeX in `latex` (amsmath syntax), and a readable linear form in `text`. Preserve fractions,
   roots, integrals, sums, limits, matrices, piecewise functions, sub/superscripts, Greek letters, vectors and
   equation numbers. Never flatten math into ambiguous plain text. Inline math inside a paragraph stays in the
   paragraph's text wrapped as $...$.
6. Chemistry: use type "chemical_equation" with mhchem LaTeX (\\ce{...}). Keep charges, states (s)(l)(g)(aq),
   reaction arrows (->), equilibrium arrows (<=>) and stoichiometry exactly. Distinguish look-alikes (O vs 0,
   l vs 1, x vs ×, - vs −).
7. Tables: only when content is genuinely tabular (rows x columns). Visually aligned prose is not a table.
   Give rows/cells, header rows, merged cells (row_span/col_span), caption and printed label.
8. Figures/diagrams/graphs: one element with box_2d tightly around the visual, a factual description, all text
   labels inside it, and relationships (arrows, connections). Captions are separate "caption" elements with
   caption_of pointing at the figure's index.
9. Academic structure: set `role` (question, subquestion, definition, theorem, example, proof, solution, ...)
   and `question` (number/marks exactly as printed). Subquestions use parent_index.
10. Headers, footers and page numbers get their own element types.
"""

PROFILE_ADDENDA: dict[Route, str] = {
    Route.VISUAL_GENERAL: "",
    Route.VISUAL_MATH: """This page is mathematics/science heavy. Prioritise exact LaTeX for every expression.
Check each symbol: subscripts vs superscripts, primes, bars/hats/vectors, limits of integrals and sums,
matrix dimensions, and operator precedence. Display equations are separate "equation" elements; keep equation
numbers in `text` (e.g. "(2.3)").""",
    Route.VISUAL_HANDWRITING: """This page is handwritten. Mark handwritten elements with handwritten=true and
type "handwriting" (or equation/chemical_equation/table/figure when appropriate). Handwriting is ambiguous:
mark every doubtful word or symbol as uncertain rather than choosing a plausible reading. Transcribe marginal
annotations as separate elements with role "marginal_note". Never fill in words that are not written.""",
    Route.VISUAL_TABLE: """This page contains complex tables. Reconstruct exact row/column structure including
merged cells and multi-row headers. Do not split one table into several or merge separate tables.""",
}

REVIEW_SYSTEM = """You are a second-pass verifier for OCR output from an academic document. You receive a crop
of the source image and a candidate transcription. Compare them character by character.
- verdict "confirmed": the candidate is exactly right.
- verdict "corrected": you can read the source and the candidate has errors; return the corrected text/latex/table.
- verdict "unreadable": the source cannot be read reliably; do not guess.
Never add content that is not visible in the crop. The uncertainty label is an ordinal judgement."""

REVIEW_FOCUS = {
    "equation": "Focus on LaTeX correctness: every symbol, sub/superscript, limit, fraction and bracket.",
    "chemical_equation": "Focus on formulas, charges, states, stoichiometric coefficients and arrow types (\\ce{}).",
    "table": "Focus on table structure: row/column counts, merged cells, header rows and each cell's text.",
    "handwriting": "Focus on each handwritten word/symbol; prefer 'unreadable' over guessing.",
    "default": "Focus on exact transcription.",
}

CLASSIFY_SYSTEM = """You classify academic documents. Base the decision only on the evidence provided
(page images and/or extracted text). A caller-provided hint may be wrong: use it as a prior, and report via
hint_consistent whether the evidence supports it. Report certainty as an ordinal label (not a probability)."""


def extraction_prompt(route: Route, *, document_type: str | None, language_hint: str | None,
                      context_hint: str | None, text_layer: str | None) -> str:
    parts = [PROFILE_ADDENDA.get(route, "")]
    hints = []
    if document_type:
        hints.append(f"document type: {document_type}")
    if language_hint:
        hints.append(f"language(s): {language_hint}")
    if context_hint:
        hints.append(f"context: {context_hint}")
    if hints:
        parts.append("Caller-provided hints (may be wrong; trust the image over the hint): " + "; ".join(hints))
    if text_layer:
        parts.append(
            "The PDF also has an embedded text layer for this page (may be incomplete, mis-ordered, or wrong for "
            "math symbols). Use it only to disambiguate characters; the image is authoritative:\n<<<\n"
            + text_layer + "\n>>>"
        )
    parts.append("Extract every element on this page image according to the rules. Return JSON only.")
    return "\n\n".join(p for p in parts if p)


def review_prompt(element_type: str, candidate: dict) -> str:
    import json

    focus = REVIEW_FOCUS.get(element_type, REVIEW_FOCUS["default"])
    return f"{focus}\nCandidate transcription (JSON):\n{json.dumps(candidate, ensure_ascii=False)}\nReturn JSON only."


def classify_prompt(context_hint: str | None, text_sample: str | None, page_count: int) -> str:
    parts = [f"The document has {page_count} page(s)."]
    if context_hint:
        parts.append(f"Caller hint (may be wrong): {context_hint}")
    if text_sample:
        parts.append("Extracted text sample:\n<<<\n" + text_sample + "\n>>>")
    parts.append("Classify the document. Return JSON only.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------------------------- host mode
# Instructions handed to the MCP client's own model (host mode). Versioned separately: part of the cache
# key of host-transcribed pages.
HOST_PROMPT_VERSION = "host-2026-11-03.1"

HOST_RULES = """Transcribe this page image for ScholarOCR. You transcribe; you never author.
1. Exactly what is visible. Never complete, correct, summarise or improve content.
2. Illegible: do NOT guess. Use an element of type "uncertain" (text like "[illegible: 2 words]",
   uncertainty "unresolved"), or keep the element and set uncertainty "medium"/"high" when you read it but
   are unsure. Labels are ordinal judgements, not probabilities.
3. Elements in natural reading order (respect columns; header first, footer/page number last).
4. box_2d = [ymin, xmin, ymax, xmax] as integers 0-1000, normalised to THIS image (origin top-left).
   Give one for every element; the server maps it back to the original page.
5. Math: LaTeX (amsmath) in "latex", a readable linear form in "text". Display equations are separate
   "equation" elements; inline math stays in the paragraph text as $...$. Keep equation numbers.
6. Chemistry: type "chemical_equation" with mhchem LaTeX (\\ce{...}); keep charges, states, arrows and
   coefficients exactly. Distinguish O/0, l/1, x/×, -/−.
7. Tables only for genuinely tabular content: rows of cells, header_rows, row_span/col_span for merged cells.
8. Figures/diagrams/graphs: one element with box_2d tight around it and "figure": {description, labels,
   relationships, kind}. Captions are separate "caption" elements with caption_of = the figure's index.
9. Structure: role (question, subquestion, definition, theorem, example, proof, solution, ...) and
   "question": {number, marks} exactly as printed; subquestions set parent_index (0-based index).
10. Headers, footers and page numbers get their own types."""

HOST_SHAPE = """Call submit_page_content(document_id, page_number, elements=[...], page_language="en",
page_label=<printed page number or null>, legibility="low|medium|high|unresolved" (overall doubt),
rotation_detected=0|90|180|270 (clockwise, only if the image itself is rotated), notes=[...]).
Element fields: type (required), text, latex, role, level, items, table, figure, question, box_2d,
uncertainty, uncertainty_reasons, handwritten, language, caption_of, parent_index. Examples:
{"type":"heading","level":1,"text":"Chapter 2","box_2d":[40,90,80,600]}
{"type":"paragraph","role":"question","text":"Q3. Solve $x^2=4$. [2]","question":{"number":"3","marks":"2"},"box_2d":[100,80,140,920]}
{"type":"equation","text":"E = mc^2","latex":"E = mc^2","box_2d":[150,300,190,700]}
{"type":"table","table":{"header_rows":1,"rows":[[{"text":"x","is_header":true},{"text":"y","is_header":true}],[{"text":"1"},{"text":"2"}]]},"box_2d":[200,80,330,900]}
{"type":"figure","figure":{"description":"Circuit with a battery and two resistors in series","labels":["R1","R2"],"kind":"circuit"},"box_2d":[340,150,600,850]}
{"type":"caption","text":"Fig. 2: Series circuit","caption_of":4,"box_2d":[605,300,630,700]}
{"type":"uncertain","text":"[illegible: 3 words]","uncertainty":"unresolved","box_2d":[650,80,680,500]}
Types: title heading subheading paragraph text handwriting equation chemical_equation table figure diagram
image caption footnote header footer page_number list code bibliography_entry reference uncertain."""

HOST_SHORT = """Same rules as before: transcribe exactly, never guess (use "uncertain"/unresolved), reading
order, box_2d [ymin,xmin,ymax,xmax] 0-1000 of THIS image, LaTeX for math, \\ce{} for chemistry. Submit with
submit_page_content. Ask for instructions="full" to see the full rules and element examples again."""


def host_page_brief(route: Route, *, full: bool, document_type: str | None, language_hint: str | None,
                    context_hint: str | None, text_layer: str | None) -> str:
    """Instructions returned with a page image in host mode."""
    parts = [HOST_RULES + "\n\n" + HOST_SHAPE if full else HOST_SHORT]
    addendum = PROFILE_ADDENDA.get(route, "")
    if addendum:
        parts.append("Page profile: " + addendum)
    hints = []
    if document_type:
        hints.append(f"document type: {document_type}")
    if language_hint:
        hints.append(f"language(s): {language_hint}")
    if context_hint:
        hints.append(f"context: {context_hint}")
    if hints:
        parts.append("Caller-provided hints (may be wrong; trust the image): " + "; ".join(hints))
    if text_layer:
        parts.append("The PDF has an embedded text layer for this page (may be incomplete or wrong for math). "
                     "Use it only to disambiguate characters; the image is authoritative:\n<<<\n" + text_layer
                     + "\n>>>")
    return "\n\n".join(parts)


HOST_REVIEW_BRIEF = """Re-check this element against the crop. Compare character by character.
Then call submit_review(document_id, element_id, verdict, ...):
- verdict "confirmed": the stored transcription is exactly right;
- verdict "corrected": you can read it and it is wrong; pass the corrected text / latex / table;
- verdict "unreadable": the source cannot be read reliably; do not guess.
For a figure/diagram/image you can instead pass figure={description, labels, relationships, kind}.
uncertainty = your ordinal judgement after the check (low/medium/high/unresolved)."""
