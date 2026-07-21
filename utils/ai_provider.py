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


class OpenAICompatProvider:
    """Any API speaking the OpenAI /chat/completions dialect."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def chat(self, model: str, system: str, user_content: str,
                   max_tokens: int) -> ChatResult:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
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
                   max_tokens: int) -> ChatResult:
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        if model.startswith("claude-sonnet-5"):
            # Sonnet 5 runs adaptive thinking when the param is omitted — turn it
            # off explicitly: chat answers don't need it and thinking bills as output.
            kwargs["thinking"] = {"type": "disabled"}
        response = await self.client.messages.create(**kwargs)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return ChatResult(
            text=text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            refusal=response.stop_reason == "refusal",
        )


def build_provider():
    """Build the provider chosen by .env, or None (cog answers 'not configured')."""
    kind = os.getenv("AI_PROVIDER", "anthropic").strip().lower()
    if kind == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.warning("AI_PROVIDER=anthropic but ANTHROPIC_API_KEY not set — AI disabled")
            return None
        return AnthropicProvider()
    if kind == "openai":
        base_url = os.getenv("AI_BASE_URL", "").strip()
        api_key = os.getenv("AI_API_KEY", "").strip()
        if not base_url or not api_key:
            log.warning("AI_PROVIDER=openai needs AI_BASE_URL + AI_API_KEY — AI disabled")
            return None
        return OpenAICompatProvider(base_url, api_key)
    log.warning("Unknown AI_PROVIDER=%r — AI disabled", kind)
    return None
