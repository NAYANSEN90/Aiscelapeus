"""The configuration boundary: what the process refuses to start without.

Two classes of defect live here, and both have already happened in this repo.

The first is a *stale requirement*: `Settings.load()` demanded
`OPENAI_API_KEY` and `ELEVENLABS_API_KEY` long after Session 2 dropped both
vendors. That is not cosmetic. Spike 0 established that `openai` installs as a
hard transitive dependency of `livekit-agents`, so `import openai` succeeds
regardless - the stale key therefore failed at *runtime on a missing
credential* rather than at *import on a missing module*. The wiring looks
correct and breaks mid-call, with a responder on the line. It also blocked
`scripts/seed_moss.py` outright, which is the regression
`test_load_succeeds_with_only_the_required_keys` exists to pin.

The second is a *silent default*: a typo'd provider or a retired model name
resolving to something that merely looks plausible. `gemini-2.5-flash` is
retired and returns 404 for new users, and one candidate returned 503 - so the
provider and the fallback chain are both validated structures rather than free
strings.

Every test here builds its environment explicitly. Nothing reads the real
.env: `Settings.load(environ=...)` takes the mapping as an argument, and
`env_file` is pointed at a path that does not exist so the repo-root dotenv
files cannot leak in.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from aiscelapeus.config import (
    REQUIRED_KEYS,
    ConfigError,
    LlmProvider,
    ModelConfig,
    ModelFallbackChain,
    Settings,
    _Env,
)

#: A dotenv path that cannot exist, so `read_env` finds no file to merge. Every
#: test passes this: without it, `Settings.load` falls back to the repo root and
#: a developer's real .env decides whether the test passes.
NO_ENV_FILE = Path(__file__).parent / "does-not-exist.env"

#: Exactly the credentials the process actually needs after the Session 2
#: migration: Moss (retrieval), Deepgram (STT and TTS on one key), Gemini (LLM).
#: Deliberately spelled out here rather than derived from `REQUIRED_KEYS`, so a
#: key silently added to or removed from that constant is caught by
#: `test_required_keys_is_exactly_the_surviving_vendors` instead of being
#: absorbed by every test in the file.
LIVE_VENDOR_KEYS: dict[str, str] = {
    "MOSS_PROJECT_ID": "fake-project",
    "MOSS_PROJECT_KEY": "fake-moss-key",
    "DEEPGRAM_API_KEY": "fake-deepgram-key",
    "GEMINI_API_KEY": "fake-gemini-key",
}

#: The two vendors Session 2 dropped. ElevenLabs because Deepgram Aura-2 covers
#: TTS on the existing STT key; OpenAI because no credits are available.
DROPPED_VENDOR_KEYS = ("ELEVENLABS_API_KEY", "OPENAI_API_KEY")


def load(**overrides: str) -> Settings:
    """Load settings from a hermetic environment: live vendors plus overrides."""
    environ = {**LIVE_VENDOR_KEYS, **overrides}
    return Settings.load(env_file=NO_ENV_FILE, environ=environ)


# ------------------------------------------------------------- required keys


def test_load_succeeds_with_only_the_required_keys() -> None:
    # falsifier: this is the regression that broke `scripts/seed_moss.py`.
    # `Settings.load()` demanded credentials for two vendors the project no
    # longer uses, so a correctly-configured machine could not construct
    # settings at all and every entry point that loads config was dead. The
    # seeder had to be scoped to MossConfig to work around it.
    settings = load()

    assert settings.moss.project_id == "fake-project"
    assert settings.models.llm_provider is LlmProvider.GEMINI
    assert settings.telemetry.environment == "production"


def test_no_dropped_vendor_key_is_required() -> None:
    # falsifier: a dropped vendor's key is still demanded, so `Settings.load()`
    # raises on an environment that is in fact complete. Asserted over the
    # public constant *and* by loading, because a key could be absent from
    # REQUIRED_KEYS and still be read by a `env.req` call inside a config class.
    for key in DROPPED_VENDOR_KEYS:
        assert key not in REQUIRED_KEYS

    # And prove it by loading: an environment holding *only* the surviving
    # vendors' keys must construct. Asserting on the constant alone would miss
    # an `env.req("OPENAI_API_KEY")` call living inside a config class, which
    # would reject the same environment without appearing in REQUIRED_KEYS.
    settings = load()

    assert settings.models.llm_provider is LlmProvider.GEMINI
    assert settings.moss.project_key == "fake-moss-key"


def test_required_keys_is_exactly_the_surviving_vendors() -> None:
    # falsifier: a credential is quietly added to REQUIRED_KEYS without an
    # `.env.example` entry or a conftest entry beside it, so the suite stays
    # green on the author's machine and every other machine fails to start. This
    # pins the set so growing it is a deliberate, visible edit.
    assert set(REQUIRED_KEYS) == set(LIVE_VENDOR_KEYS)


def test_every_missing_key_is_reported_in_one_pass() -> None:
    # falsifier: validation returns on the first missing credential, so filling
    # in the environment becomes one crash per re-run. Worse, the comment above
    # REQUIRED_KEYS records why this matters: an unchecked key used to surface
    # mid-incident as the agent silently failing to speak, after the responder
    # had already started talking.
    with pytest.raises(ConfigError) as raised:
        Settings.load(env_file=NO_ENV_FILE, environ={})

    problems = "\n".join(raised.value.problems)
    for key in REQUIRED_KEYS:
        assert key in problems, f"{key} missing from the one-pass report"
    assert len(raised.value.problems) >= len(REQUIRED_KEYS)


def test_a_blank_credential_counts_as_missing() -> None:
    # falsifier: a key present but empty - the state `.env.example` ships in,
    # and the state a half-filled `.env` is in - passes validation, so the
    # process starts and fails on the first real API call instead of at startup.
    with pytest.raises(ConfigError) as raised:
        load(GEMINI_API_KEY="   ")

    assert any("GEMINI_API_KEY" in problem for problem in raised.value.problems)


def test_the_error_names_every_problem_not_just_credentials() -> None:
    # falsifier: a malformed number and a missing credential are reported by
    # separate mechanisms, so fixing the credential reveals the number problem
    # only on the next run - the one-crash-per-re-run behaviour this design
    # exists to avoid, reintroduced through the back door.
    with pytest.raises(ConfigError) as raised:
        Settings.load(
            env_file=NO_ENV_FILE,
            environ={**LIVE_VENDOR_KEYS, "MOSS_PROJECT_ID": "", "LLM_TEMPERATURE": "hot"},
        )

    joined = "\n".join(raised.value.problems)
    assert "MOSS_PROJECT_ID" in joined
    assert "LLM_TEMPERATURE" in joined


# ----------------------------------------------------------- Gemini defaults


def test_model_defaults_are_the_post_migration_stack() -> None:
    # falsifier: the defaults still name `gpt-4o` and an ElevenLabs voice id, so
    # a deployment that does not set every model variable silently runs against
    # vendors the project dropped - and, because `openai` installs as a hard
    # transitive dep of livekit-agents, it fails at runtime on a missing key
    # rather than at import on a missing module.
    models = ModelConfig.from_env(_Env({}))

    assert models.llm_provider is LlmProvider.GEMINI
    assert models.llm_model == "gemini-3.6-flash"
    assert models.llm_fallback_model == "gemini-3.7-flash"
    assert models.stt_model == "nova-3-medical"
    assert models.tts_model == "aura-2-thalia-en"


def test_no_default_names_a_dropped_vendor() -> None:
    # falsifier: one stale default survives the migration - an `eleven_*` model
    # or a `gpt-*` model left in a field nobody re-read - and that single field
    # routes a clinical call to a vendor with no credential behind it.
    models = ModelConfig.from_env(_Env({}))
    values = [
        value for value in dataclasses.asdict(models).values() if isinstance(value, str)
    ]

    for value in values:
        lowered = value.lower()
        assert not lowered.startswith("eleven"), f"{value!r} is an ElevenLabs id"
        assert not lowered.startswith("gpt-"), f"{value!r} is an OpenAI model"


def test_the_retired_gemini_model_is_not_a_default() -> None:
    # falsifier: `gemini-2.5-flash` is retired and returns 404 for new users.
    # Shipping it as a default means the LLM leg is dead on any fresh project,
    # and the failure arrives as a 404 mid-call rather than as a config error.
    models = ModelConfig.from_env(_Env({}))

    assert models.llm_model != "gemini-2.5-flash"
    assert models.llm_fallback_model != "gemini-2.5-flash"


def test_tts_reads_the_deepgram_variable_not_the_elevenlabs_one() -> None:
    # falsifier: the TTS model is still read from `ELEVENLABS_MODEL`, so setting
    # `DEEPGRAM_TTS_MODEL` - the variable `.env` and `.env.example` actually
    # document - has no effect and the operator's override is silently ignored.
    models = ModelConfig.from_env(
        _Env({"DEEPGRAM_TTS_MODEL": "aura-2-orion-en", "ELEVENLABS_MODEL": "eleven_turbo_v2_5"})
    )

    assert models.tts_model == "aura-2-orion-en"


def test_model_overrides_are_read_from_the_environment() -> None:
    # falsifier: the defaults are correct but hard-coded past the env read, so a
    # model retirement cannot be worked around by configuration and requires a
    # code change and a redeploy during an outage.
    models = ModelConfig.from_env(
        _Env(
            {
                "LLM_MODEL": "gemini-3.8-flash",
                "LLM_FALLBACK_MODEL": "gemini-3.6-flash",
                "LLM_TEMPERATURE": "0.7",
                "DEEPGRAM_MODEL": "nova-3-general",
            }
        )
    )

    assert models.llm_model == "gemini-3.8-flash"
    assert models.llm_fallback_model == "gemini-3.6-flash"
    assert models.llm_temperature == 0.7
    assert models.stt_model == "nova-3-general"


# ------------------------------------------------------------ fallback chain


def test_the_fallback_chain_is_a_typed_structure_not_loose_strings() -> None:
    # falsifier: primary and fallback travel as two unrelated strings, so a
    # caller can use one without the other and nothing can enforce that they
    # differ. The chain is the unit of meaning; splitting it is what lets an
    # invalid pair exist.
    models = ModelConfig.from_env(_Env({}))
    chain = models.llm_chain

    assert isinstance(chain, ModelFallbackChain)
    assert chain.primary == models.llm_model
    assert chain.fallback == models.llm_fallback_model


def test_the_chain_orders_attempts_primary_first() -> None:
    # falsifier: the chain reverses or de-duplicates wrongly and the retry runs
    # the fallback first, so every call pays for the model that was chosen as
    # second-best - and the 503 the fallback exists for is never actually
    # exercised in the order that was designed.
    chain = ModelFallbackChain(primary="gemini-3.6-flash", fallback="gemini-3.7-flash")

    assert chain.attempts == ("gemini-3.6-flash", "gemini-3.7-flash")


def test_a_primary_equal_to_its_fallback_is_a_config_error() -> None:
    # falsifier: a copy-paste sets both entries to the same model, so the
    # "fallback" retries the exact model that just returned 503. The chain reads
    # as configured resilience and provides none - the class of defect that only
    # shows up during the outage it was meant to survive.
    with pytest.raises(ValueError, match="fallback"):
        ModelFallbackChain(primary="gemini-3.6-flash", fallback="gemini-3.6-flash")


def test_an_identical_pair_surfaces_through_settings_load_not_at_first_call() -> None:
    # falsifier: the equality rule exists on the type but `Settings.load` never
    # constructs the chain, so a misconfigured pair passes startup validation
    # and raises deep inside the first LLM turn instead - mid-call, with a
    # responder waiting.
    with pytest.raises(ConfigError) as raised:
        load(LLM_MODEL="gemini-3.6-flash", LLM_FALLBACK_MODEL="gemini-3.6-flash")

    assert any("LLM_FALLBACK_MODEL" in problem for problem in raised.value.problems)


def test_a_blank_fallback_is_a_config_error_not_a_silent_single_model() -> None:
    # falsifier: an empty `LLM_FALLBACK_MODEL` resolves to "no fallback" without
    # complaint, so the resilience Session 2 required because one model returned
    # 503 is absent while the config file still appears to declare it.
    with pytest.raises(ConfigError) as raised:
        load(LLM_FALLBACK_MODEL="   ")

    assert any("LLM_FALLBACK_MODEL" in problem for problem in raised.value.problems)


def test_the_bare_model_fields_cannot_disagree_with_the_chain() -> None:
    # falsifier: `llm_model` and `llm_fallback_model` are stored *alongside*
    # `llm_chain` instead of being views over it, so
    # `ModelConfig(llm_model="a", llm_chain=chain("c", "d"))` constructs cleanly
    # and two sources of truth for one fact drift apart - main.py calls the
    # model in the field while the retry path walks a different chain. An
    # independent review found exactly this hole in the first version of this
    # class.
    chain = ModelFallbackChain(primary="gemini-3.8-flash", fallback="gemini-3.6-flash")
    models = ModelConfig(llm_chain=chain)

    assert models.llm_model == "gemini-3.8-flash"
    assert models.llm_fallback_model == "gemini-3.6-flash"

    # And the disagreement is not merely avoided, it is unconstructible: the
    # names are read-only properties, so there is no keyword to pass.
    with pytest.raises(TypeError):
        ModelConfig(llm_model="a", llm_chain=chain)  # type: ignore[call-arg]


def test_a_model_override_moves_the_chain_not_just_a_field() -> None:
    # falsifier: `LLM_MODEL` sets the bare field but never reaches the chain, so
    # the retry path keeps attempting the default model that the operator
    # explicitly overrode - the override appears to work and silently does not.
    models = ModelConfig.from_env(_Env({"LLM_MODEL": "gemini-3.8-flash"}))

    assert models.llm_chain.primary == "gemini-3.8-flash"
    assert models.llm_chain.attempts[0] == "gemini-3.8-flash"


def test_every_variable_config_reads_is_documented_in_the_example_file() -> None:
    # falsifier: config.py reads a variable that `.env.example` does not
    # document - an earlier draft read `DEEPGRAM_TTS_VOICE`, a name in neither
    # .env nor .env.example - so the code advertises a knob a new contributor
    # cannot find, and the name they *can* find has no effect. .env.example is
    # the file a contributor copies, so a read with no entry there is a setting
    # that effectively does not exist.
    #
    # Read off the AST rather than the raw text: an earlier version of this test
    # grepped the source and failed on the word appearing in a *comment*
    # explaining its own removal. A comment is not a read.
    config_path = Path(__file__).resolve().parents[2] / "aiscelapeus" / "config.py"
    tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))

    readers = {"opt", "req", "flag", "number", "integer", "provider"}
    read_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in readers
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            read_names.add(node.args[0].value)

    assert "DEEPGRAM_TTS_VOICE" not in read_names
    assert read_names, "the AST walk found no env reads at all - the walk is broken"

    example = (
        Path(__file__).resolve().parents[3] / ".env.example"
    ).read_text(encoding="utf-8")
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    undocumented = sorted(read_names - documented)
    assert not undocumented, (
        f"config.py reads variables absent from .env.example: {undocumented}"
    )


def test_no_dropped_vendor_variable_is_read_any_more() -> None:
    # falsifier: one stale read survives the migration - `ELEVENLABS_MODEL` or
    # `OPENAI_API_KEY` left in a branch nobody re-read - so a dropped vendor's
    # variable still steers a clinical call. Checked against the read names, not
    # the file text, so the comments recording *why* these were removed do not
    # themselves trip the assertion.
    config_path = Path(__file__).resolve().parents[2] / "aiscelapeus" / "config.py"
    tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))

    read_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            read_names.add(node.args[0].value)

    offenders = sorted(
        name
        for name in read_names
        if name.startswith("ELEVENLABS") or name.startswith("OPENAI")
    )
    assert not offenders, f"dropped-vendor variables still read: {offenders}"


def test_the_chain_is_frozen() -> None:
    # falsifier: the chain is mutable, so a retry path rewrites `primary` on the
    # shared settings object and every later session in the process inherits the
    # degraded model with nothing in the config recording why.
    chain = ModelFallbackChain(primary="gemini-3.6-flash", fallback="gemini-3.7-flash")

    with pytest.raises(dataclasses.FrozenInstanceError):
        chain.primary = "gemini-3.8-flash"  # type: ignore[misc]


# -------------------------------------------------------- provider validation


def test_the_provider_is_a_validated_enum_not_a_free_string() -> None:
    # falsifier: the provider is a bare `str`, so `llm_provider == "gemini"`
    # comparisons scattered across the wiring each get to be wrong
    # independently, and no single place can enumerate what is supported.
    models = ModelConfig.from_env(_Env({"LLM_PROVIDER": "gemini"}))

    assert isinstance(models.llm_provider, LlmProvider)
    assert models.llm_provider is LlmProvider.GEMINI


def test_a_typod_provider_is_an_error_not_a_silent_default() -> None:
    # falsifier: `LLM_PROVIDER=gemni` falls through to the default provider, so
    # the operator believes they switched vendors and did not. This repo keeps
    # finding this exact class of defect - a bad value coerced into a plausible
    # one - and CLAUDE.md answers it with "a raise over a silent clamp".
    with pytest.raises(ConfigError) as raised:
        load(LLM_PROVIDER="gemni")

    joined = "\n".join(raised.value.problems)
    assert "LLM_PROVIDER" in joined
    assert "gemni" in joined, "the rejected value must be echoed so it can be fixed"


def test_the_error_lists_the_supported_providers() -> None:
    # falsifier: the rejection says only "invalid provider", so the operator has
    # to read the source to discover the accepted spelling - during the incident
    # in which they are trying to change provider.
    with pytest.raises(ConfigError) as raised:
        load(LLM_PROVIDER="openai")

    joined = "\n".join(raised.value.problems)
    assert LlmProvider.GEMINI.value in joined


def test_a_dropped_vendor_is_not_a_supported_provider() -> None:
    # falsifier: `openai` is still an accepted provider value, so the migration
    # is reversible by an env var alone - into a vendor with no credits, no
    # required key, and therefore no startup check to catch it.
    values = {provider.value for provider in LlmProvider}

    assert "openai" not in values
    assert "elevenlabs" not in values


def test_the_provider_is_case_insensitive_and_trimmed() -> None:
    # falsifier: `LLM_PROVIDER=Gemini ` with a stray space or capital - what a
    # hand-edited .env actually contains - is rejected as a typo, so startup
    # fails on a value that was correct in intent and obvious in meaning.
    models = ModelConfig.from_env(_Env({"LLM_PROVIDER": "  Gemini  "}))

    assert models.llm_provider is LlmProvider.GEMINI


# --------------------------------------------------------------- hermeticity


def test_an_explicit_environ_shuts_out_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # falsifier: `environ=` is merged with `os.environ` rather than replacing
    # it, so a developer's exported credential decides a test's outcome and the
    # suite passes or fails per machine. That is the exact defect conftest.py's
    # docstring was written about.
    monkeypatch.setenv("LLM_MODEL", "leaked-from-the-shell")
    settings = load()

    assert settings.models.llm_model == "gemini-3.6-flash"
