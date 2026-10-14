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


# ── images + thinking kwargs (10/13) ────────────────────────────────────────
from utils.ai_provider import (  # noqa: E402
    openai_user_message, anthropic_user_content, anthropic_thinking_kwargs)


def test_openai_user_message_plain_without_images():
    assert openai_user_message("hi", None) == "hi"
    assert openai_user_message("hi", []) == "hi"


def test_openai_user_message_parts_with_images():
    parts = openai_user_message("what is this", ["https://cdn/a.png", "https://cdn/b.jpg"])
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "https://cdn/a.png"}}
    assert parts[2]["image_url"]["url"] == "https://cdn/b.jpg"
    assert len(parts) == 3


def test_anthropic_user_content_images_first_then_text():
    blocks = anthropic_user_content("q", ["https://cdn/a.png"])
    assert blocks[0] == {"type": "image", "source": {"type": "url", "url": "https://cdn/a.png"}}
    assert blocks[-1] == {"type": "text", "text": "q"}
    assert anthropic_user_content("q", None) == "q"


def test_thinking_disabled_only_on_sonnet_5_not_5_5():
    assert anthropic_thinking_kwargs("claude-sonnet-5") == {"thinking": {"type": "disabled"}}
    assert anthropic_thinking_kwargs("claude-sonnet-5-5") == {}   # "disabled" is a 400 there
    assert anthropic_thinking_kwargs("claude-haiku-4-5") == {}


def test_fetched_bytes_become_inline_images():
    img = {"media_type": "image/png", "data": "AAAA"}
    parts = openai_user_message("q", [img, "https://cdn/x.png"])
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert parts[2]["image_url"]["url"] == "https://cdn/x.png"
    blocks = anthropic_user_content("q", [img])
    assert blocks[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}


def test_reasoning_kwargs(monkeypatch):
    from utils.ai_provider import reasoning_kwargs
    assert reasoning_kwargs("low") == {"reasoning": {"effort": "low"}}
    assert reasoning_kwargs(" High ") == {"reasoning": {"effort": "high"}}
    assert reasoning_kwargs("off") == {}          # not a thing: provider default
    assert reasoning_kwargs("") == {}
    monkeypatch.delenv("AI_REASONING_EFFORT", raising=False)
    assert reasoning_kwargs() == {}
    monkeypatch.setenv("AI_REASONING_EFFORT", "minimal")
    assert reasoning_kwargs() == {"reasoning": {"effort": "minimal"}}
