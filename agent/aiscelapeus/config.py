"""Central configuration. Every knob is an env var; nothing is hard-coded.

12-Factor: config lives in the environment, never in the image.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import dotenv_values

# Credentials the process cannot do its job without. Validated together, at
# startup, so a first run reports every missing key in one pass instead of one
# per re-run. Three of these were previously never checked at all, which made a
# missing key surface mid-incident as the agent silently failing to speak.
#
# One key per surviving vendor, and the stack is four vendors: LiveKit
# (transport, whose credentials the worker CLI validates itself), Deepgram
# (STT *and* TTS on a single key), Gemini (LLM), Moss (retrieval).
# ELEVENLABS_API_KEY and OPENAI_API_KEY were removed here: both vendors were
# dropped, yet both were still demanded, so `Settings.load()` raised on an
# environment that was in fact complete and blocked scripts/seed_moss.py
# outright. Spike 0 is why that was worse than it looked - `openai` installs as
# a hard transitive dependency of livekit-agents, so the stale requirement
# failed at runtime on a missing credential rather than at import on a missing
# module: the shape that looks correctly wired and breaks mid-call.
REQUIRED_KEYS = (
    "MOSS_PROJECT_ID",
    "MOSS_PROJECT_KEY",
    "DEEPGRAM_API_KEY",
    "GEMINI_API_KEY",
)


class LlmProvider(Enum):
    """The LLM vendors this build can actually talk to.

    An enum rather than the free string `LLM_PROVIDER` used to be, because a
    free string has no wrong value: `LLM_PROVIDER=gemni` would resolve to the
    default provider and the operator would believe they had switched vendors
    when they had not. That is the defect class this repo keeps finding - a bad
    value coerced into a plausible one - and CLAUDE.md answers it with "a raise
    over a silent clamp".

    Membership is also the migration's enforcement point: `openai` is absent,
    so the move to Gemini cannot be undone by an env var alone into a vendor
    with no credits, no entry in REQUIRED_KEYS, and therefore no startup check.
    """

    GEMINI = "gemini"

    @staticmethod
    def parse(raw: str) -> "LlmProvider":
        """Case-insensitive, whitespace-tolerant lookup. Raises on anything else.

        Hand-edited .env files carry stray spaces and capitals; `Gemini ` is
        correct in intent and obvious in meaning, so rejecting it would fail
        startup over formatting. A genuinely unknown value still raises.
        """
        candidate = raw.strip().lower()
        for provider in LlmProvider:
            if provider.value == candidate:
                return provider
        supported = ", ".join(provider.value for provider in LlmProvider)
        raise ValueError(
            f"unsupported LLM provider {raw.strip()!r}; supported: {supported}"
        )


@dataclass(frozen=True)
class ModelFallbackChain:
    """An ordered pair of models to try, primary first.

    A chain rather than two loose strings because the pair *is* the unit of
    meaning: split into `llm_model` and `llm_fallback_model`, a caller can reach
    for one without the other and nothing in the type system can require that
    they differ. Session 2 made this a real requirement rather than a
    precaution - `gemini-2.5-flash` is retired and 404s for new users, and a
    candidate model returned 503.

    A primary equal to its fallback is rejected rather than tolerated. It is
    never a deliberate configuration: retrying the exact model that just
    returned 503 is not a fallback, and the pair reads as configured resilience
    while providing none. Tolerating it would defer the discovery to the outage
    the chain exists to survive.
    """

    primary: str
    fallback: str

    def __post_init__(self) -> None:
        if not self.primary.strip():
            raise ValueError("the primary model must not be blank")
        if not self.fallback.strip():
            raise ValueError("the fallback model must not be blank")
        if self.primary.strip() == self.fallback.strip():
            raise ValueError(
                f"the fallback model must differ from the primary "
                f"({self.primary!r}); retrying the model that just failed is "
                f"not a fallback"
            )

    @property
    def attempts(self) -> tuple[str, ...]:
        """The models to try, in order. The retry path iterates this, not fields."""
        return (self.primary, self.fallback)


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

    def provider(self, name: str, default: LlmProvider) -> LlmProvider:
        """Parse a validated provider, recording a bad value as a problem.

        The rejection is appended rather than raised so it travels in the same
        one-pass report as every missing credential. A provider typo that
        aborted validation early would hide the missing keys behind it and
        restore the one-crash-per-re-run behaviour REQUIRED_KEYS exists to
        avoid. The offending value is echoed, because the operator fixing it
        needs to see what was actually read - a trailing space is invisible in
        a .env file.
        """
        raw = self.opt(name)
        if not raw:
            return default
        try:
            return LlmProvider.parse(raw)
        except ValueError as error:
            self.problems.append(f"{name}: {error}")
            return default

    def chain(self, primary_name: str, fallback_name: str, *, primary: str, fallback: str) -> "ModelFallbackChain":
        """Build a validated fallback chain, recording a bad pair as a problem.

        Same reasoning as `provider`: the invariant lives on
        `ModelFallbackChain`, but a `ValueError` escaping `Settings.load` would
        be a different exception type than every other config fault and would
        skip the remaining checks. Naming the fallback variable in the message
        is deliberate - it is the one the operator most likely mis-pasted.
        """
        try:
            return ModelFallbackChain(primary=primary, fallback=fallback)
        except ValueError as error:
            self.problems.append(
                f"{primary_name}={primary!r} / {fallback_name}={fallback!r}: {error}"
            )
            # A placeholder so construction can continue and collect the rest of
            # the problems. `Settings.load` raises before any caller sees it.
            return ModelFallbackChain(primary="invalid-primary", fallback="invalid-fallback")


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


# The post-migration stack, in one place so the prompt, .env.example and the
# tests all derive from it rather than restating it (CLAUDE.md: DRY means one
# source of truth for a rule).
#
# `gemini-2.5-flash` is deliberately absent: it is retired and returns 404 for
# new users, so any default naming it would be dead on a fresh project.
DEFAULT_LLM_MODEL = "gemini-3.6-flash"
DEFAULT_LLM_FALLBACK_MODEL = "gemini-3.7-flash"
DEFAULT_STT_MODEL = "nova-3-medical"
# Deepgram Aura-2. TTS rides the STT key, which is the whole reason ElevenLabs
# could be dropped for a 40-50 ms TTFB difference against a budget the LLM
# overruns by ~2 s.
DEFAULT_TTS_MODEL = "aura-2-thalia-en"


@dataclass(frozen=True)
class ModelConfig:
    """Which models each leg of the pipeline runs.

    Field names are unchanged from the pre-migration version on purpose:
    `main.py` reads `stt_model`, `stt_language`, `llm_model`,
    `llm_temperature`, `tts_voice_id` and `tts_model`, and that file belongs to
    another subsystem in flight. Renaming here would be a config-layer change
    breaking a file this change is not allowed to touch. Only the *values* and
    the *variables they are read from* moved vendor.

    `llm_model` and `llm_fallback_model` are therefore preserved as *names* -
    but as read-only views over `llm_chain`, not as stored fields. Storing them
    alongside the chain was the first version of this class, and an independent
    review showed it let `ModelConfig(llm_model="a", llm_chain=chain("c", "d"))`
    construct cleanly: two sources of truth for one fact, free to drift, with
    the invariant upheld only by `from_env` happening to pass matching values.
    That is the illegal state CLAUDE.md says to make unrepresentable rather than
    to avoid by discipline at one call site. The chain owns the models; these
    two properties are the spelling `main.py` already uses.
    """

    stt_model: str = DEFAULT_STT_MODEL
    stt_language: str = "en-US"
    llm_provider: LlmProvider = LlmProvider.GEMINI
    llm_temperature: float = 0.2
    tts_model: str = DEFAULT_TTS_MODEL
    # Aura-2 selects the speaker through the model id itself
    # (`aura-2-<voice>-en`), so there is no separate voice id to carry. Kept
    # only because main.py still passes it; it is an ElevenLabs-shaped remnant
    # for that subsystem to remove. Deliberately NOT read from an env var: an
    # earlier draft read `DEEPGRAM_TTS_VOICE`, a name that appears in neither
    # `.env` nor `.env.example`, which advertised a knob that does not exist.
    tts_voice_id: str = ""
    # The single source of truth for which LLMs run, and in what order.
    # `default_factory` rather than `default`: the value is immutable today, so
    # a shared instance is safe, but a factory stays correct if a mutable field
    # is ever added to the chain.
    llm_chain: ModelFallbackChain = field(
        default_factory=lambda: ModelFallbackChain(
            primary=DEFAULT_LLM_MODEL, fallback=DEFAULT_LLM_FALLBACK_MODEL
        )
    )

    @property
    def llm_model(self) -> str:
        """The model tried first. A view over the chain, so it cannot disagree."""
        return self.llm_chain.primary

    @property
    def llm_fallback_model(self) -> str:
        """The model tried when the primary fails. A view over the chain."""
        return self.llm_chain.fallback

    @staticmethod
    def from_env(env: _Env) -> "ModelConfig":
        return ModelConfig(
            stt_model=env.opt("DEEPGRAM_MODEL", DEFAULT_STT_MODEL),
            stt_language=env.opt("DEEPGRAM_LANGUAGE", "en-US"),
            llm_provider=env.provider("LLM_PROVIDER", LlmProvider.GEMINI),
            llm_temperature=env.number("LLM_TEMPERATURE", 0.2),
            tts_model=env.opt("DEEPGRAM_TTS_MODEL", DEFAULT_TTS_MODEL),
            llm_chain=env.chain(
                "LLM_MODEL",
                "LLM_FALLBACK_MODEL",
                primary=env.opt("LLM_MODEL", DEFAULT_LLM_MODEL),
                fallback=env.opt("LLM_FALLBACK_MODEL", DEFAULT_LLM_FALLBACK_MODEL),
            ),
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
        # class of ours. Unchecked, a missing TTS key surfaced only as the agent
        # silently failing to speak - after the responder had already started
        # talking. Deepgram now covers both STT and TTS, so that one key failing
        # to validate takes out hearing and speaking together, which is a
        # stronger reason to check it at startup rather than a weaker one.
        for key in REQUIRED_KEYS:
            env.req(key)

        if env.problems:
            raise ConfigError(sorted(set(env.problems)))
        return settings
