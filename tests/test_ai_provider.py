"""Tests for utils/ai_provider.py — response parsing + provider selection."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.ai_provider import parse_openai_response, build_provider, OpenAICompatProvider


def test_parse_openai_response_happy_path():
    r = parse_openai_response({
        "choices": [{"message": {"content": " hello there \n"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
    })
    assert r.text == "hello there"
    assert (r.tokens_in, r.tokens_out) == (120, 45)
    assert not r.refusal


def test_parse_openai_response_content_filter_is_refusal():
    r = parse_openai_response({
        "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 0},
    })
    assert r.refusal


def test_parse_openai_response_refusal_field():
    # OpenAI-style explicit refusal message field
    r = parse_openai_response({
        "choices": [{"message": {"content": None, "refusal": "I can't help with that."},
                     "finish_reason": "stop"}],
        "usage": {},
    })
    assert r.refusal
    assert r.text == ""


def test_parse_openai_response_empty_and_malformed():
    r = parse_openai_response({})
    assert r.text == "" and r.tokens_in == 0 and r.tokens_out == 0
    r = parse_openai_response({"choices": [], "usage": None})
    assert r.text == ""


def test_build_provider_unconfigured_returns_none(monkeypatch):
    for var in ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert build_provider() is None                      # anthropic default, no key
    monkeypatch.setenv("AI_PROVIDER", "openai")
    assert build_provider() is None                      # openai without url/key
    monkeypatch.setenv("AI_PROVIDER", "abacus")
    assert build_provider() is None                      # unknown provider


def test_build_provider_openai_compat(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_BASE_URL", "https://openrouter.ai/api/v1/")
    monkeypatch.setenv("AI_API_KEY", "sk-or-test")
    p = build_provider()
    assert isinstance(p, OpenAICompatProvider)
    assert p.base_url == "https://openrouter.ai/api/v1"  # trailing slash stripped
    assert p.api_key == "sk-or-test"
