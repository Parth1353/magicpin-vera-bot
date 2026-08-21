"""Runtime configuration, all environment-driven with safe defaults.

The bot is fully functional with no configuration at all: the composer is deterministic
and needs no API key. Every LLM setting below is opt-in polish on top of that, and if a
provider is slow, rate-limited or absent the deterministic output is what ships.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] or default


@dataclass
class Settings:
    # --- identity, surfaced on /v1/metadata ---------------------------------
    team_name: str = os.getenv("VERA_TEAM_NAME", "Parth Saini")
    team_members: list[str] = field(
        default_factory=lambda: _list("VERA_TEAM_MEMBERS", ["Parth Saini"]))
    contact_email: str = os.getenv("VERA_CONTACT_EMAIL", "parthsaini13@gmail.com")
    version: str = os.getenv("VERA_VERSION", "1.0.0")
    submitted_at: str = os.getenv("VERA_SUBMITTED_AT", "2026-08-21T00:00:00Z")

    # --- latency budgets -----------------------------------------------------
    # api-call-examples.md budgets tick/reply at 10s and judge_simulator times out at 15s.
    tick_budget_seconds: float = _num("VERA_TICK_BUDGET", 8.0)
    reply_budget_seconds: float = _num("VERA_REPLY_BUDGET", 8.0)

    # --- optional LLM polish -------------------------------------------------
    llm_enabled: bool = _flag("VERA_LLM_ENABLED", False)
    llm_provider: str = os.getenv("VERA_LLM_PROVIDER", "anthropic").strip().lower()
    llm_model: str = os.getenv("VERA_LLM_MODEL", "").strip()
    llm_api_key: str = (os.getenv("VERA_LLM_API_KEY")
                        or os.getenv("ANTHROPIC_API_KEY")
                        or os.getenv("OPENAI_API_KEY") or "").strip()
    llm_timeout: float = _num("VERA_LLM_TIMEOUT", 5.0)
    llm_max_calls_per_tick: int = int(_num("VERA_LLM_MAX_CALLS_PER_TICK", 4))
    llm_polish_replies: bool = _flag("VERA_LLM_POLISH_REPLIES", True)
    ollama_url: str = os.getenv("VERA_OLLAMA_URL", "http://localhost:11434")

    # --- behaviour -----------------------------------------------------------
    max_actions_per_tick: int = int(_num("VERA_MAX_ACTIONS_PER_TICK", 20))
    debug_endpoints: bool = _flag("VERA_DEBUG_ENDPOINTS", True)

    @property
    def llm_active(self) -> bool:
        if not self.llm_enabled:
            return False
        if self.llm_provider == "ollama":
            return True
        return bool(self.llm_api_key)

    def describe_approach(self) -> str:
        base = ("deterministic grounded composer: 4-context fact sheet with per-number "
                "provenance, derived-insight ranking, per-trigger-kind composition, and an "
                "output guard that blocks ungrounded numbers, URLs, category taboos, internal "
                "jargon and repetition")
        if self.llm_active:
            return (f"{base}; optional LLM editorial pass ({self.llm_provider}) accepted only "
                    f"when it introduces no new facts")
        return f"{base}; no LLM in the request path, so output is reproducible and sub-10ms"

    def model_label(self) -> str:
        if not self.llm_active:
            # Accurate rather than coy: there is no model in the request path, and that is
            # the design, not a gap.
            return "deterministic composer v1.0 (LLM editor optional, disabled)"
        return self.llm_model or _DEFAULT_MODELS.get(self.llm_provider, self.llm_provider)


_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-1.5-flash",
    "deepseek": "deepseek-chat",
    "groq": "llama-3.1-70b-versatile",
    "openrouter": "anthropic/claude-3.5-sonnet",
    "ollama": "llama3",
}

settings = Settings()
