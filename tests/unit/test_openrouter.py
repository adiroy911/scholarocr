"""OpenRouter provider: settings mapping and transport request/response handling (no network)."""

import json

import httpx
import pytest
from pydantic import BaseModel

from scholarocr.config import DEFAULT_OPENROUTER_MODEL, Settings
from scholarocr.exceptions import GeminiError, GeminiUnavailableError
from scholarocr.intelligence.gemini_client import (
    GeminiClient,
    ImagePart,
    OpenRouterTransport,
    TextPart,
    classify_exception,
    to_strict_schema,
)
from scholarocr.intelligence.responses import PageExtractionResponse
from scholarocr.intelligence.schema_utils import to_gemini_schema


def test_strict_schema_requires_every_property():
    def check(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                assert set(node["required"]) == set(node["properties"]) and node["additionalProperties"] is False
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    strict = to_strict_schema(to_gemini_schema(PageExtractionResponse))
    check(strict)
    assert "text" in strict["properties"]["elements"]["items"]["required"]


class Answer(BaseModel):
    value: str


def _transport(handler) -> OpenRouterTransport:
    t = OpenRouterTransport("sk-test", 30)
    t._client = httpx.Client(base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler),
                             headers={"Authorization": "Bearer sk-test"})
    return t


def test_settings_provider_mapping():
    env = {"SCHOLAROCR_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "k", "GEMINI_MODEL": "gemini-x",
           "GEMINI_REVIEW_MODEL": "gemini-y", "OPENROUTER_MODEL": "z-ai/glm-5.3-flash"}
    s = Settings.from_env(env)
    assert s.provider == "openrouter" and s.api_key == "k"
    assert s.gemini_model == "z-ai/glm-5.3-flash" and s.gemini_review_model == "z-ai/glm-5.3-flash"
    assert s.math_model == "z-ai/glm-5.3-flash"
    assert Settings.from_env({"SCHOLAROCR_PROVIDER": "openrouter"}).gemini_model == DEFAULT_OPENROUTER_MODEL
    g = Settings.from_env({"GEMINI_MODEL": "gemini-x", "OPENROUTER_API_KEY": "k"})
    assert g.provider == "gemini" and g.gemini_model == "gemini-x" and g.api_key is None
    red = s.redacted()
    assert red["openrouter_api_key"] == "set" and "'k'" not in str(red)
    with pytest.raises(ValueError):
        Settings(provider="nope")


def test_missing_openrouter_key_names_the_right_variable():
    c = GeminiClient(Settings(provider="openrouter", openrouter_api_key=None, gemini_api_key="g"))
    assert not c.available
    with pytest.raises(GeminiUnavailableError, match="OPENROUTER_API_KEY"):
        c._get_transport()


def test_request_shape_and_response_parsing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={
            "model": "nvidia/x", "choices": [{"message": {"content": '{"value": "ok"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                      "completion_tokens_details": {"reasoning_tokens": 1}},
        })

    t = _transport(handler)
    raw = t.generate(model="nvidia/x", system="sys", parts=[TextPart("hi"), ImagePart(b"\x89PNG", "image/png")],
                     response_schema={"type": "object"}, temperature=0.0, media_resolution="HIGH", timeout_s=5)
    body = seen["body"]
    assert seen["auth"] == "Bearer sk-test"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    user = body["messages"][1]["content"]
    assert user[0] == {"type": "text", "text": "hi"}
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["response_format"]["type"] == "json_schema" and body["provider"]["require_parameters"] is True
    assert body["response_format"]["json_schema"]["strict"] is True
    assert raw.text == '{"value": "ok"}' and raw.model_version == "nvidia/x"
    assert raw.usage == {"prompt_tokens": 10, "output_tokens": 3, "thought_tokens": 1, "total_tokens": 13}


def test_http_errors_map_to_categories():
    t = _transport(lambda r: httpx.Response(429, json={"error": {"code": 429, "message": "slow down"}}))
    with pytest.raises(Exception) as ei:
        t.generate(model="m", system="s", parts=[TextPart("x")], response_schema={}, temperature=0,
                   media_resolution=None, timeout_s=5)
    assert classify_exception(ei.value) == ("rate_limited", True)
    # an error object inside a 200 body is still an error
    t2 = _transport(lambda r: httpx.Response(200, json={"error": {"code": 404, "message": "no such model"}}))
    with pytest.raises(Exception) as ei2:
        t2.generate(model="m", system="s", parts=[TextPart("x")], response_schema={}, temperature=0,
                    media_resolution=None, timeout_s=5)
    assert classify_exception(ei2.value) == ("model_not_found", False)


def test_client_end_to_end_with_openrouter_transport():
    t = _transport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": '{"value": "42"}'}, "finish_reason": "stop"}], "usage": {}}))
    c = GeminiClient(Settings(provider="openrouter", openrouter_api_key="k", gemini_rpm_limit=0), transport=t)
    parsed, info = c.generate_structured(purpose="t", model="m", system="s", parts=[TextPart("x")], schema=Answer,
                                         prompt_version="v1")
    assert parsed.value == "42" and info.attempts == 1
    t_bad = _transport(lambda r: httpx.Response(401, json={"error": {"code": 401, "message": "bad key"}}))
    c2 = GeminiClient(Settings(provider="openrouter", openrouter_api_key="k", gemini_rpm_limit=0), transport=t_bad)
    with pytest.raises(GeminiError) as e:
        c2.generate_structured(purpose="t", model="m", system="s", parts=[TextPart("x")], schema=Answer,
                               prompt_version="v1")
    assert e.value.category == "auth_error"
