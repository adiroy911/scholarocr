"""Convert Pydantic JSON Schemas into the subset Gemini structured output handles reliably.

- ``$ref``/``$defs`` are inlined (Gemini documents only limited ``$ref`` support).
- ``anyOf: [X, {"type": "null"}]`` becomes X with a type array ``[t, "null"]``.
- ``title`` and ``default`` keys are dropped (noise for the model; Pydantic re-applies defaults).
Full validation always happens client-side with Pydantic afterwards.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


def to_gemini_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    return _clean(_inline(schema, defs, depth=0))


def _inline(node: Any, defs: dict, depth: int) -> Any:
    if depth > 20:
        raise ValueError("schema nesting too deep / recursive")
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            merged = copy.deepcopy(defs[name])
            extra = {k: v for k, v in node.items() if k != "$ref"}
            merged.update(extra)
            return _inline(merged, defs, depth + 1)
        return {k: _inline(v, defs, depth) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs, depth) for v in node]
    return node


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k not in ("title", "default")}
    if "anyOf" in node:
        variants = node["anyOf"]
        non_null = [v for v in variants if v.get("type") != "null"]
        has_null = len(non_null) != len(variants)
        if len(non_null) == 1:
            base = _clean(non_null[0])
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            base.update({k: _clean(v) for k, v in rest.items()})
            if has_null:
                t = base.get("type")
                if isinstance(t, str):
                    base["type"] = [t, "null"]
                elif isinstance(t, list) and "null" not in t:
                    base["type"] = t + ["null"]
            return base
        node["anyOf"] = [_clean(v) for v in variants]
        return {k: (_clean(v) if k != "anyOf" else v) for k, v in node.items()}
    if "const" in node and "enum" not in node:
        node["enum"] = [node.pop("const")]
    return {k: _clean(v) for k, v in node.items()}
