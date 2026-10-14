"""Model-provider abstraction for the AI cog.

The cog talks to `chat()` and never to a vendor SDK, so the backing AI is an
.env decision, not a code change:

    AI_PROVIDER=openai            # "openai" = any OpenAI-compatible API
    AI_BASE_URL=https://openrouter.ai/api/v1
    AI_API_KEY=sk-or-...
    AI_MODEL_SMART=deepseek/deepseek-chat
    AI_MODEL_QUICK=google/gemini-2.5-flash-lite

or, once the Anthropic org is back:

    AI_PROVIDER=anthropic
    ANTHROPIC_API_KEY=sk-ant-...
    # models default to claude-sonnet-5 / claude-haiku-4-5

Images: `chat(..., images=[...])` attaches pictures to the user turn — the
asker's own attachments, never anything fetched from the archive. An entry is
either a URL string (the provider fetches it) or {"media_type": "image/png",
"data": "<base64>"} (bytes we fetched ourselves — the cog does this, because
provider-side fetching 400s on hosts that block bots, Wikimedia included, and
Discord's CDN links are signed and short-lived). Nothing is stored either way.

Known OpenAI-compatible base URLs (one backend covers them all):
    OpenRouter  https://openrouter.ai/api/v1
    Groq        https://api.groq.com/openai/v1
    Gemini      https://generativelanguage.googleapis.com/v1beta/openai
    DeepSeek    https://api.deepseek.com/v1
    Mistral     https://api.mistral.ai/v1

Per-model prices for the meter are configured in utils/ai_meter.py (AI_PRICES).
"""
import os
import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("ai.provider")

REQUEST_TIMEOUT_S = 90


@dataclass
class ChatResult:
    text: str
    tokens_in: int
    tokens_out: int
    refusal: bool = False


def _image_url(img) -> str:
    """URL form of one image entry: a plain URL passes through, fetched bytes
    become a data: URL (what the OpenAI dialect takes for inline images)."""
    if isinstance(img, dict):
        return f"data:{img['media_type']};base64,{img['data']}"
    return img


def _anthropic_source(img) -> dict:
    if isinstance(img, dict):
        return {"type": "base64", "media_type": img["media_type"], "data": img["data"]}
    return {"type": "url", "url": img}


def openai_user_message(text: str, images=None):
    """The user turn for an OpenAI-style request: a plain string when there
    are no images (what every existing test and provider log expects), else
    the parts form — text first, then one image_url part per picture."""
    if not images:
        return text
    parts = [{"type": "text", "text": text}]
    parts += [{"type": "image_url", "image_url": {"url": _image_url(i)}} for i in images]
    return parts


def anthropic_user_content(text: str, images=None):
    """The user turn for the Messages API: plain string without images, else
    image blocks FIRST then the text (the order the vision docs recommend)."""
    if not images:
        return text
    blocks = [{"type": "image", "source": _anthropic_source(i)} for i in images]
    blocks.append({"type": "text", "text": text})
    return blocks


# Models whose thinking must be switched off explicitly. Sonnet 5 runs adaptive
# thinking when the param is omitted and accepts {"type": "disabled"}; the 5.5
# generation REJECTS "disabled" with a 400 (effort is the knob there), so this
# is an exact-id list, not a prefix match — `claude-sonnet-5` must never catch
# `claude-sonnet-5-5`.
_THINKING_OFF_MODELS = ("claude-sonnet-5",)


def anthropic_thinking_kwargs(model: str) -> dict:
    """Extra Messages-API kwargs for a model id, or {} to leave the default."""
    return {"thinking": {"type": "disabled"}} if model in _THINKING_OFF_MODELS else {}


def parse_openai_response(data: dict) -> ChatResult:
    """Pure parser for an OpenAI-style /chat/completions response body."""
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    choices = data.get("choices") or []
    if not choices:
        return ChatResult("", tokens_in, tokens_out)
    choice = choices[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    refusal = choice.get("finish_reason") == "content_filter" or bool(
        (choice.get("message") or {}).get("refusal")
    )
    return ChatResult(text, tokens_in, tokens_out, refusal)


# Reasoning effort on the OpenAI dialect (OpenRouter). Measured on prod 10/13
# with Sonnet 5.5: the provider default is ADAPTIVE reasoning — 45 tokens of
# thinking on "how many e's in seventeen", 313 on a line of banter — all
# billed as output at $10/M. `{"enabled": false}` is a 400 there ("Reasoning
# is mandatory for this endpoint"); `{"effort": "low"}` ran with 0 reasoning
# tokens and the same correct answers. Unset = provider default; the operator
# picks the trade with AI_REASONING_EFFORT (minimal / low / medium / high).
_EFFORTS = ("minimal", "low", "medium", "high")


def reasoning_kwargs(effort=None) -> dict:
    """The extra request fields for the chosen effort; {} for unset/invalid."""
    e = (os.getenv("AI_REASONING_EFFORT", "") if effort is None else effort).strip().lower()
    return {"reasoning": {"effort": e}} if e in _EFFORTS else {}


class OpenAICompatProvider:
    """Any API speaking the OpenAI /chat/completions dialect."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def chat(self, model: str, system: str, user_content: str,
                   max_tokens: int, images=None) -> ChatResult:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": openai_user_message(user_content, images)},
            ],
            **reasoning_kwargs(),
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.base_url}/chat/completions",
                                    json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    err = (body.get("error") or {}).get("message", str(body))[:300]
                    raise RuntimeError(f"provider HTTP {resp.status}: {err}")
        return parse_openai_response(body)


class AnthropicProvider:
    """Anthropic Messages API via the official SDK."""

    def __init__(self):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY

    async def chat(self, model: str, system: str, user_content: str,
                   max_tokens: int, images=None) -> ChatResult:
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user",
                       "content": anthropic_user_content(user_content, images)}],
        )
        # Chat answers don't need thinking and it bills as output — off where
        # the API lets us say so (see _THINKING_OFF_MODELS for why not 5.5).
        kwargs.update(anthropic_thinking_kwargs(model))
        response = await self.client.messages.create(**kwargs)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return ChatResult(
            text=text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            refusal=response.stop_reason == "refusal",
        )


def build_provider(key_env: str = "AI_API_KEY"):
    """Build the provider chosen by .env, or None (cog answers 'not configured').

    `key_env` names the env var holding the API key, so one account can run
    two keys with separate spend limits: AI_API_KEY for the home community and
    AI_API_KEY_PAID for servers on prepaid credit. The OpenRouter key limits
    are the fence between the two pools; the cog picks the provider per guild.
    (Anthropic-direct ignores key_env — the SDK reads ANTHROPIC_API_KEY.)"""
    kind = os.getenv("AI_PROVIDER", "anthropic").strip().lower()
    if kind == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.warning("AI_PROVIDER=anthropic but ANTHROPIC_API_KEY not set — AI disabled")
            return None
        return AnthropicProvider()
    if kind == "openai":
        base_url = os.getenv("AI_BASE_URL", "").strip()
        api_key = os.getenv(key_env, "").strip()
        if not base_url or not api_key:
            log.warning("AI_PROVIDER=openai needs AI_BASE_URL + %s — provider not built", key_env)
            return None
        return OpenAICompatProvider(base_url, api_key)
    log.warning("Unknown AI_PROVIDER=%r — AI disabled", kind)
    return None
