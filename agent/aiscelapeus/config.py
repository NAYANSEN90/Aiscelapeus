"""Central configuration. Every knob is an env var; nothing is hard-coded.

12-Factor: config lives in the environment, never in the image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load .env.local first (developer overrides), then .env.
load_dotenv(".env.local", override=False)
load_dotenv(".env", override=False)


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Copy .env.example to .env.local and fill it in."
        )
    return value


def _opt(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _opt(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _opt(name)
    return int(raw) if raw else default


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
    def from_env() -> "MossConfig":
        return MossConfig(
            project_id=_req("MOSS_PROJECT_ID"),
            project_key=_req("MOSS_PROJECT_KEY"),
            protocols_index=_opt("MOSS_PROTOCOLS_INDEX", "aiscelapeus-protocols"),
            session_index_prefix=_opt("MOSS_SESSION_INDEX_PREFIX", "aiscelapeus-session"),
            latency_budget_ms=_float("MOSS_LATENCY_BUDGET_MS", 10.0),
            protocol_top_k=_int("MOSS_PROTOCOL_TOP_K", 3),
            state_top_k=_int("MOSS_STATE_TOP_K", 5),
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
    def from_env() -> "ModelConfig":
        return ModelConfig(
            stt_model=_opt("DEEPGRAM_MODEL", "nova-3-medical"),
            stt_language=_opt("DEEPGRAM_LANGUAGE", "en-US"),
            llm_model=_opt("LLM_MODEL", "gpt-4o"),
            llm_temperature=_float("LLM_TEMPERATURE", 0.2),
            tts_voice_id=_opt("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"),
            tts_model=_opt("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        )


@dataclass(frozen=True)
class TelemetryConfig:
    service_name: str = "aiscelapeus-agent"
    otlp_endpoint: str = ""
    console_export: bool = True
    environment: str = "development"

    @staticmethod
    def from_env() -> "TelemetryConfig":
        return TelemetryConfig(
            service_name=_opt("OTEL_SERVICE_NAME", "aiscelapeus-agent"),
            otlp_endpoint=_opt("OTEL_EXPORTER_OTLP_ENDPOINT"),
            console_export=_opt("OTEL_CONSOLE_EXPORT", "true").lower() == "true",
            environment=_opt("APP_ENV", "development"),
        )


@dataclass(frozen=True)
class TriageConfig:
    # Criticality 1-5 (PRD 5.3). At or above this level the agent bridges a doctor.
    escalation_threshold: int = 4
    # Prosody: sustained distress above this score contributes to escalation.
    distress_threshold: float = 0.75

    @staticmethod
    def from_env() -> "TriageConfig":
        return TriageConfig(
            escalation_threshold=_int("ESCALATION_THRESHOLD", 4),
            distress_threshold=_float("DISTRESS_THRESHOLD", 0.75),
        )


@dataclass(frozen=True)
class Settings:
    moss: MossConfig = field(default_factory=MossConfig.from_env)
    models: ModelConfig = field(default_factory=ModelConfig.from_env)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig.from_env)
    triage: TriageConfig = field(default_factory=TriageConfig.from_env)

    @staticmethod
    def load() -> "Settings":
        return Settings(
            moss=MossConfig.from_env(),
            models=ModelConfig.from_env(),
            telemetry=TelemetryConfig.from_env(),
            triage=TriageConfig.from_env(),
        )
