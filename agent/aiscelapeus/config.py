"""Central configuration. Every knob is an env var; nothing is hard-coded.

12-Factor: config lives in the environment, never in the image.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# Credentials the process cannot do its job without. Validated together, at
# startup, so a first run reports every missing key in one pass instead of one
# per re-run. Three of these were previously never checked at all, which made a
# missing key surface mid-incident as the agent silently failing to speak.
REQUIRED_KEYS = (
    "MOSS_PROJECT_ID",
    "MOSS_PROJECT_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "OPENAI_API_KEY",
)


class ConfigError(RuntimeError):
    """Configuration is unusable. Carries every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(
            f"Configuration is incomplete:\n  - {joined}\n"
            "Copy .env.example to .env.local at the repository root and fill it in."
        )


def repo_root() -> Path:
    """The repository root, resolved from this file rather than the cwd.

    `load_dotenv(".env.local")` resolves against the working directory, so the
    README's own instructions - write .env.local at the repo root, then `cd
    agent` - produced a crash for anyone but the author.
    """
    return Path(__file__).resolve().parent.parent.parent


def read_env(
    *,
    env_file: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve the effective environment. Explicit, so tests can be hermetic.

    Precedence: real environment wins over the dotenv file, matching
    `load_dotenv(override=False)`. Passing `environ` bypasses `os.environ`
    entirely.
    """
    base = dict(os.environ if environ is None else environ)

    if env_file is None:
        root = repo_root()
        candidates = [root / ".env.local", root / ".env"]
    else:
        candidates = [Path(env_file)]

    resolved: dict[str, str] = {}
    for candidate in candidates:
        if candidate.is_file():
            for key, value in dotenv_values(candidate).items():
                if value is not None:
                    resolved.setdefault(key, value)

    resolved.update({k: v for k, v in base.items() if v is not None})
    return resolved


class _Env:
    """Typed reads over a resolved environment mapping, collecting problems."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values
        self.problems: list[str] = []

    def req(self, name: str) -> str:
        value = self._values.get(name, "").strip()
        if not value:
            self.problems.append(f"missing required environment variable {name}")
        return value

    def opt(self, name: str, default: str = "") -> str:
        return self._values.get(name, default).strip()

    def flag(self, name: str, default: bool) -> bool:
        raw = self.opt(name)
        return raw.lower() == "true" if raw else default

    def number(self, name: str, default: float) -> float:
        raw = self.opt(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            self.problems.append(f"{name}={raw!r} is not a number")
            return default

    def integer(self, name: str, default: int) -> int:
        raw = self.opt(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            self.problems.append(f"{name}={raw!r} is not an integer")
            return default


@dataclass(frozen=True)
class MossConfig:
    project_id: str
    project_key: str
    protocols_index: str = "aiscelapeus-protocols"
    session_index_prefix: str = "aiscelapeus-session"
    # Judged criterion: Moss retrieval must stay under this. We trace every query
    # and emit a warning span attribute when it does not.
    latency_budget_ms: float = 10.0
    protocol_top_k: int = 3
    state_top_k: int = 5

    @staticmethod
    def from_env(env: _Env) -> "MossConfig":
        return MossConfig(
            project_id=env.req("MOSS_PROJECT_ID"),
            project_key=env.req("MOSS_PROJECT_KEY"),
            protocols_index=env.opt("MOSS_PROTOCOLS_INDEX", "aiscelapeus-protocols"),
            session_index_prefix=env.opt("MOSS_SESSION_INDEX_PREFIX", "aiscelapeus-session"),
            latency_budget_ms=env.number("MOSS_LATENCY_BUDGET_MS", 10.0),
            protocol_top_k=env.integer("MOSS_PROTOCOL_TOP_K", 3),
            state_top_k=env.integer("MOSS_STATE_TOP_K", 5),
        )


@dataclass(frozen=True)
class ModelConfig:
    stt_model: str = "nova-3-medical"
    stt_language: str = "en-US"
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.2
    tts_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    tts_model: str = "eleven_turbo_v2_5"

    @staticmethod
    def from_env(env: _Env) -> "ModelConfig":
        return ModelConfig(
            stt_model=env.opt("DEEPGRAM_MODEL", "nova-3-medical"),
            stt_language=env.opt("DEEPGRAM_LANGUAGE", "en-US"),
            llm_model=env.opt("LLM_MODEL", "gpt-4o"),
            llm_temperature=env.number("LLM_TEMPERATURE", 0.2),
            tts_voice_id=env.opt("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"),
            tts_model=env.opt("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        )


@dataclass(frozen=True)
class TelemetryConfig:
    service_name: str = "aiscelapeus-agent"
    otlp_endpoint: str = ""
    console_export: bool = False
    # PHI handling fails CLOSED. `APP_ENV` previously defaulted to
    # "development", and development was itself the condition that enabled PII
    # in spans - so the default was the opt-in. Production is the default now,
    # and carrying PHI requires saying so explicitly.
    environment: str = "production"
    allow_pii: bool = False

    @staticmethod
    def from_env(env: _Env) -> "TelemetryConfig":
        environment = env.opt("APP_ENV", "production")
        return TelemetryConfig(
            service_name=env.opt("OTEL_SERVICE_NAME", "aiscelapeus-agent"),
            otlp_endpoint=env.opt("OTEL_EXPORTER_OTLP_ENDPOINT"),
            console_export=env.flag("OTEL_CONSOLE_EXPORT", False),
            environment=environment,
            # Two independent conditions, both required: a non-production
            # environment AND a deliberate opt-in. Neither alone is enough.
            allow_pii=environment == "development"
            and env.flag("OTEL_ALLOW_PII", False),
        )


@dataclass(frozen=True)
class TriageConfig:
    # Criticality 1-5 (PRD 5.3). At or above this level the agent bridges a doctor.
    escalation_threshold: int = 4
    # How long a clinician request may go unanswered before the escalation is
    # recorded as FAILED_NO_RESPONSE rather than left indefinitely pending.
    dispatch_timeout_s: float = 30.0

    @staticmethod
    def from_env(env: _Env) -> "TriageConfig":
        return TriageConfig(
            escalation_threshold=env.integer("ESCALATION_THRESHOLD", 4),
            dispatch_timeout_s=env.number("DISPATCH_TIMEOUT_S", 30.0),
        )


@dataclass(frozen=True)
class Settings:
    moss: MossConfig
    models: ModelConfig
    telemetry: TelemetryConfig
    triage: TriageConfig

    @staticmethod
    def load(
        *,
        env_file: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        """Resolve every setting once, reporting all problems together.

        Reading the environment is an explicit argument rather than an
        import-time side effect, so a test can construct settings without
        touching the developer's own .env.local.
        """
        env = _Env(read_env(env_file=env_file, environ=environ))
        settings = Settings(
            moss=MossConfig.from_env(env),
            models=ModelConfig.from_env(env),
            telemetry=TelemetryConfig.from_env(env),
            triage=TriageConfig.from_env(env),
        )
        # Credentials consumed by the LiveKit plugins rather than by a config
        # class of ours. Unchecked, a missing ElevenLabs key surfaced only as
        # the agent silently failing to speak - after the responder had already
        # started talking.
        for key in REQUIRED_KEYS:
            env.req(key)

        if env.problems:
            raise ConfigError(sorted(set(env.problems)))
        return settings
