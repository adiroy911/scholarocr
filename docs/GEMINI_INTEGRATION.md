# Gemini integration

**SDK:** official `google-genai` (tested with 2.34.0). All calls go through
`intelligence/gemini_client.py`; no other module imports the SDK.

## Request shape

`client.models.generate_content(model=…, contents=[Content(role="user", parts=[image, text])],
config=GenerateContentConfig(system_instruction=…, temperature=0, response_mime_type="application/json",
response_json_schema=<flattened schema>))`. Images are sent inline as PNG (JPEG above ~7 MB) with
`media_resolution=MEDIA_RESOLUTION_HIGH` for page OCR and `MEDIUM` for figure descriptions and
classification. SDK-level retries are disabled; the client owns the retry policy.

## Where Gemini is (and is not) called

| Situation | Calls |
|---|---|
| Page with reliable embedded text, no figures | **none** |
| Text-layer page with figures/diagrams | 1 per figure (crop, max 4 per page), primary model |
| Scanned / math-font / image-heavy page | 1 extraction (primary, or math model on `visual_math`) |
| Model reports the page is rotated | +1 re-extraction after correcting rotation |
| Flagged elements (medium/high/unresolved; or all equations+tables with `detail="high"`) | 1 per element, review model, max 8 per page |
| Invalid structured output | +1 repair request; a second failure fails the page (never accepted) |
| `classify_document` | 1 (text sample if available, else ≤3 low-res page images) |
| Export, retrieval, verification without `review` | **none** |
| Unchanged pages on re-run | **none** (cache) |

## Prompts

`intelligence/prompts.py`, versioned by `PROMPT_VERSION` (part of every cache key). One system prompt
with strict transcription rules (no inventing, ordinal uncertainty, reading order, LaTeX for math,
mhchem for chemistry, table/figure/structure rules) plus route profiles: math, handwriting, tables.
Caller hints are passed explicitly marked "may be wrong; trust the image". When a PDF page has a text
layer but goes to visual OCR, up to 6,000 characters of it are included to disambiguate characters.

## Structured output and validation

`intelligence/responses.py` defines the response models (`PageExtractionResponse`, `ReviewResponse`,
`ClassificationResponse`, figure schema). `schema_utils.to_gemini_schema()` inlines `$ref`s, turns
nullable `anyOf` into type arrays and drops `title`/`default`. The response text is parsed and
validated with Pydantic; on failure one repair request carries the validation errors; a second failure
raises `ResponseValidationError` and the page is marked failed with the reason.

## Errors, retries, rate limiting

| Condition | Category | Retried |
|---|---|---|
| HTTP 429 | `rate_limited` | yes |
| HTTP 500/502/503/504, 408, timeouts, connection errors | `server_error`/`timeout`/`network_error` | yes |
| HTTP 400 / 401 / 403 / 404 | `invalid_request` / `auth_error` / `model_not_found` | no |
| Empty response | `empty_response` | yes |
| Finish reason SAFETY/RECITATION/… | `blocked` | no |

Backoff: `min(60, 2·2^(n-1)) × U(0.5, 1.5)` seconds, up to `GEMINI_MAX_RETRIES`. A shared sliding
window limits requests per minute (`GEMINI_RPM_LIMIT`).

## Usage tracking

Every call (and failure) is recorded in SQLite table `gemini_calls`: time, document, page, purpose,
model, prompt version, status, attempts, latency, prompt/output/thinking/total tokens, error category.
No prompt text, document content or key is stored or logged. `scholarocr usage [DOC]` aggregates it;
cost is estimated only from a user-supplied pricing file.

## Model selection

Defaults (`gemini-3.8-flash` for both primary and review) were taken from the official models
page on 2026-11-01. Check that page and set `GEMINI_MODEL` / `GEMINI_REVIEW_MODEL` /
`GEMINI_MATH_MODEL` for your account. `model_not_found` errors mean the ID is unavailable to your key.

## Verification status

- Unit/integration tests drive the full Gemini path with a scripted fake transport (`tests/fixtures/fake_gemini.py`): routing, prompts, schema validation, repair, retries, review, rotation re-extraction, usage logging.
- **Live API calls were not executed during development (no API key was available).** The live
  suite is ready: `SCHOLAROCR_RUN_LIVE_TESTS=1 GEMINI_API_KEY=… pytest tests/live -m live`. Run it
  first after configuring a key; it checks real extraction of math, chemistry and a poor scan.
- The SDK request construction (`SdkTransport`) uses only constructors/fields verified in the
  installed SDK source (`Part.from_bytes(media_resolution=…)`, `GenerateContentConfig.response_json_schema`,
  `HttpOptions.timeout` in ms, `HttpRetryOptions(attempts=1)`), but it has not been exercised against the live service.

## Switching to the Interactions API

Implement another `Transport` with the same `generate(...) -> RawResponse` signature and pass it to
`GeminiClient`. Nothing else changes.
