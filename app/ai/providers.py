"""
Free AI providers behind one small interface.

Gemini (Google AI Studio free tier), Groq (free plan) and Ollama (runs
on your own PC) all expose an OpenAI-compatible /chat/completions
endpoint, so a single httpx-based client covers all three — no extra
SDK needed.

`OPTIMIND_AI_PROVIDERS` sets the order to try them in. If one fails
(free quota used up, no internet, invalid answer), the next one is
tried. "local" is the built-in rule-based brain: it never fails and
costs nothing, so it is always the last resort.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Callable, TypeVar

from app import i18n
from app.config import settings
from app.i18n import tx

if TYPE_CHECKING:
    import httpx

T = TypeVar("T")

_TIMEOUT_SECONDS = 60.0
LOCAL = "local"

# Today's usage per provider, so the dashboard can show how much of the
# free quotas is being used (resets at midnight / on restart).
_stats: dict[str, dict] = {}
_stats_day: str | None = None
_stats_lock = threading.Lock()


def _record(provider: str, ok: bool, error: str | None = None) -> None:
    """(`error` may be a text: the AI page shows it in OptiMind's language)"""
    global _stats_day
    with _stats_lock:
        today = date.today().isoformat()
        if _stats_day != today:
            _stats.clear()
            _stats_day = today
        s = _stats.setdefault(provider, {"answered": 0, "failed": 0, "last_error": None, "last_used": None})
        if ok:
            s["answered"] += 1
            s["last_used"] = datetime.now(timezone.utc).isoformat()
        else:
            s["failed"] += 1
            s["last_error"] = error


def provider_stats() -> dict:
    with _stats_lock:
        return {"day": _stats_day, "chain": provider_chain(), "providers": {k: dict(v) for k, v in _stats.items()}}


class ProviderError(Exception):
    """A provider could not answer (quota, network, invalid output...)."""


class ProviderNotConfigured(ProviderError):
    """The provider has no API key/model set — skipped silently."""


def provider_chain() -> list[str]:
    names = [p.strip().lower() for p in settings.optimind_ai_providers.split(",") if p.strip()]
    if LOCAL not in names:
        names.append(LOCAL)  # the free rule-based brain is always the last resort
    return names


def _endpoint(provider: str) -> tuple[str, str, str, dict]:
    """Returns (base_url, api_key, model, extra request fields)."""
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ProviderNotConfigured(tx("{setting} is not set", setting="GEMINI_API_KEY"))
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            settings.gemini_api_key,
            settings.optimind_gemini_model,
            {"reasoning_effort": "low"},
        )
    if provider == "groq":
        if not settings.groq_api_key:
            raise ProviderNotConfigured(tx("{setting} is not set", setting="GROQ_API_KEY"))
        return (
            "https://api.groq.com/openai/v1",
            settings.groq_api_key,
            settings.optimind_groq_model,
            {"reasoning_effort": "low"},
        )
    if provider == "ollama":
        if not settings.optimind_ollama_model:
            raise ProviderNotConfigured(tx("{setting} is not set", setting="OPTIMIND_OLLAMA_MODEL"))
        return settings.optimind_ollama_url.rstrip("/"), "ollama", settings.optimind_ollama_model, {}
    if provider == "custom":
        if not settings.custom_ai_api_key or not settings.custom_ai_base_url:
            raise ProviderNotConfigured(tx("{setting} is not set", setting="CUSTOM_AI_API_KEY / CUSTOM_AI_BASE_URL"))
        return settings.custom_ai_base_url.rstrip("/"), settings.custom_ai_api_key, settings.custom_ai_model, {}
    raise ProviderNotConfigured(tx("unknown provider '{provider}'", provider=provider))


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, list):  # Gemini sometimes wraps the error in a list
            data = data[0]
        return data["error"]["message"]
    except Exception:
        return resp.text[:200]


def _post_chat(
    base_url: str,
    api_key: str,
    body: dict,
    timeout: float,
) -> httpx.Response:
    """One chat/completions POST. Returns the response (caller checks status)."""
    import httpx

    return httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=timeout,
    )


def chat_completion(
    provider: str,
    messages: list[dict],
    max_tokens: int,
    json_schema: dict | None = None,
) -> str:
    """Sends one chat request and returns the answer text.
    With `json_schema`, the provider is asked to answer with JSON that
    matches it (structured output). Many OpenAI-compatible endpoints
    (NVIDIA Nemotron, local LLMs, etc.) don't support `json_schema` — if the
    provider rejects it, we retry with plain free text so the answer can still
    be parsed as JSON. This keeps every custom provider working."""
    import httpx  # loaded on the first AI call only, so OptiMind starts lighter

    base_url, api_key, model, extra = _endpoint(provider)
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, **extra}

    def _build_body(with_schema: bool) -> dict:
        b = dict(body)
        if with_schema and json_schema:
            b["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": json_schema},
            }
        return b

    try:
        resp = _post_chat(base_url, api_key, _build_body(bool(json_schema)), _TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise ProviderError(tx("{provider}: network error ({error})", provider=provider, error=str(e))) from e

    # Provider doesn't support structured output: retry with free text.
    if resp.status_code == 400 and json_schema:
        try:
            resp = _post_chat(base_url, api_key, _build_body(False), _TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise ProviderError(tx("{provider}: network error ({error})", provider=provider, error=str(e))) from e

    if resp.status_code != 200:
        raise ProviderError(f"{provider}: HTTP {resp.status_code} - {_error_message(resp)}")  # the provider's own words

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ProviderError(tx("{provider}: unexpected response format", provider=provider)) from e
    if content is None:
        # Some providers (thinking models, refusals) return content: null.
        # Show what the provider actually sent so it can be diagnosed.
        raise ProviderError(
            tx("{provider}: empty answer — the model returned no content. "
               "Response: {body}", provider=provider, body=resp.text[:300])
        )
    if not content:
        raise ProviderError(
            tx("{provider}: empty answer — the model returned blank content. "
               "Response: {body}", provider=provider, body=resp.text[:300])
        )
    return content


def check(provider: str) -> str:
    """The Settings page's Test button: one tiny request with the saved key and model.
    Any answer counts (thinking models may spend these few tokens on thinking).
    Raises ProviderError (or ProviderNotConfigured) with the reason it fails."""
    import httpx

    base_url, api_key, model, extra = _endpoint(provider)
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 64, **extra}
    try:
        resp = httpx.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                          json=body, timeout=30.0)
    except httpx.HTTPError as e:
        raise ProviderError(tx("network error ({error})", error=str(e))) from e
    if resp.status_code != 200:
        raise ProviderError(f"HTTP {resp.status_code} - {_error_message(resp)}")
    return tx("{model} answered", model=model)


def run_with_fallback(task: Callable[[str], T]) -> tuple[T, str, list[str]]:
    """Calls task(provider) for each provider in order until one succeeds.
    Returns (result, provider that answered, reasons other providers failed)."""
    failures: list[str] = []
    for provider in provider_chain():
        try:
            result = task(provider)
        except ProviderNotConfigured:
            continue
        except ProviderError as e:
            failures.append(i18n.message(e))
            _record(provider, ok=False, error=i18n.message(e))
            continue
        _record(provider, ok=True)
        return result, provider, failures
    raise ProviderError(tx("no provider could answer: {reasons}", reasons=i18n.joined(failures, "; ")))
