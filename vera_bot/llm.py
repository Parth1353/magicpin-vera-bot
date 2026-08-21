"""Optional LLM editorial pass.

The deterministic composer is the product; this is polish, and it is treated as untrusted
polish. A rewrite is accepted only if it says nothing the fact sheet cannot back: no new
numbers, no dropped citation, no new URL, no taboo vocabulary, and the same single ask.
Anything else and the deterministic body ships unchanged.

Determinism is preserved by pinning temperature to 0 and caching on a hash of the exact
prompt, so the same contexts produce the same message on every run.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import guard
from .config import settings
from .facts import FactSheet
from .utils import squeeze

_SYSTEM = """You are an editor, not an author.

You will be given a WhatsApp message that a merchant-growth assistant is about to send, plus
the exact list of facts that are allowed to appear in it. Your only job is to improve how it
reads: rhythm, natural phrasing, and cutting anything limp.

Hard rules:
- Do not introduce any number, price, date, name, source or claim that is not already in the
  message. You may remove, never add.
- Keep every number that is already there, unchanged.
- Keep the source citation if one is present, at the end, after an em dash.
- Keep exactly one question or one explicit instruction. Never two.
- Keep the same language mix (if the message code-mixes Hindi and English, keep that).
- No URLs. No emoji beyond what is already there.
- Keep it under the original length.

Reply with the edited message only. No preamble, no quotes, no explanation."""


@dataclass
class PolishResult:
    body: str
    used_llm: bool
    reason: str = ""


class _Cache:
    def __init__(self, limit: int = 512) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._limit = limit

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        with self._lock:
            if len(self._data) >= self._limit:
                self._data.pop(next(iter(self._data)))
            self._data[key] = value


_cache = _Cache()


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #

def _post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _anthropic(prompt: str, timeout: float) -> str:
    data = _post("https://api.anthropic.com/v1/messages", {
        "model": settings.model_label(), "max_tokens": 600, "temperature": 0,
        "system": _SYSTEM, "messages": [{"role": "user", "content": prompt}],
    }, {"x-api-key": settings.llm_api_key, "anthropic-version": "2023-06-01"}, timeout)
    return data["content"][0]["text"]


def _openai_style(url: str, prompt: str, timeout: float, extra: dict | None = None) -> str:
    data = _post(url, {
        "model": settings.model_label(), "temperature": 0, "max_tokens": 600,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt}],
    }, {"Authorization": f"Bearer {settings.llm_api_key}", **(extra or {})}, timeout)
    return data["choices"][0]["message"]["content"]


def _gemini(prompt: str, timeout: float) -> str:
    model = settings.model_label()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={settings.llm_api_key}")
    data = _post(url, {
        "contents": [{"parts": [{"text": f"{_SYSTEM}\n\n{prompt}"}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 600},
    }, {}, timeout)
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _ollama(prompt: str, timeout: float) -> str:
    data = _post(f"{settings.ollama_url}/api/generate", {
        "model": settings.model_label(), "prompt": f"{_SYSTEM}\n\n{prompt}",
        "stream": False, "options": {"temperature": 0},
    }, {}, timeout)
    return data["response"]


_PROVIDERS: dict[str, Callable[[str, float], str]] = {
    "anthropic": _anthropic,
    "openai": lambda p, t: _openai_style("https://api.openai.com/v1/chat/completions", p, t),
    "deepseek": lambda p, t: _openai_style("https://api.deepseek.com/v1/chat/completions", p, t),
    "groq": lambda p, t: _openai_style("https://api.groq.com/openai/v1/chat/completions", p, t),
    "openrouter": lambda p, t: _openai_style(
        "https://openrouter.ai/api/v1/chat/completions", p, t,
        {"HTTP-Referer": "https://magicpin.com"}),
    "gemini": _gemini,
    "ollama": _ollama,
}


# --------------------------------------------------------------------------- #

def available() -> bool:
    return settings.llm_active and settings.llm_provider in _PROVIDERS


def polish(body: str, sheet: FactSheet, *, seen_bodies: tuple[str, ...] = (),
           timeout: float | None = None) -> PolishResult:
    """Return an edited body, or the original if the edit cannot be trusted."""
    if not available() or not squeeze(body):
        return PolishResult(body, False, "llm not active")

    prompt = _build_prompt(body, sheet)
    key = hashlib.sha256(f"{settings.model_label()}|{prompt}".encode("utf-8")).hexdigest()

    cached = _cache.get(key)
    if cached is not None:
        return PolishResult(cached, True, "cache hit")

    try:
        raw = _PROVIDERS[settings.llm_provider](prompt, timeout or settings.llm_timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError,
            TimeoutError, OSError, json.JSONDecodeError) as exc:
        return PolishResult(body, False, f"provider error: {type(exc).__name__}")

    candidate = _clean(raw)
    ok, reason = _acceptable(candidate, body, sheet, seen_bodies)
    if not ok:
        _cache.put(key, body)
        return PolishResult(body, False, f"edit rejected: {reason}")

    _cache.put(key, candidate)
    return PolishResult(candidate, True, "edit accepted")


def _build_prompt(body: str, sheet: FactSheet) -> str:
    facts = {
        "business": sheet.business_name,
        "owner": sheet.salutation,
        "locality": sheet.place,
        "category": sheet.category_slug,
        "tone": sheet.voice.get("tone"),
        "never_use": sheet.taboos,
        "domain_vocabulary": sheet.vocab[:10],
        "numbers_allowed": "only the ones already in the message",
    }
    return (f"FACTS THE MESSAGE MAY RELY ON:\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
            f"MESSAGE TO EDIT:\n{body}")


def _clean(raw: str) -> str:
    text = squeeze(raw or "")
    for prefix in ("Edited message:", "Here is the edited message:", "Message:"):
        if text.lower().startswith(prefix.lower()):
            text = squeeze(text[len(prefix):])
    return text.strip('"').strip()


def _acceptable(candidate: str, original: str, sheet: FactSheet,
                seen_bodies: tuple[str, ...]) -> tuple[bool, str]:
    if not candidate or len(candidate) < 40:
        return False, "empty or truncated"
    if len(candidate) > len(original) * 1.15 + 20:
        return False, "longer than the original"

    from .facts import _NUM_IN_TEXT
    before = sorted(_NUM_IN_TEXT.findall(original))
    after = sorted(_NUM_IN_TEXT.findall(candidate))
    if before != after:
        return False, f"numbers changed ({before} -> {after})"

    report = guard.check(candidate, sheet, seen_bodies=seen_bodies)
    if not report.ok:
        return False, "; ".join(report.blocking)
    if guard.count_asks(candidate) > 1:
        return False, "more than one ask"
    return True, "ok"
