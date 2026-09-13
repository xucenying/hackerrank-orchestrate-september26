"""Paths and environment configuration.

Only filesystem paths live here for now. Model/API settings are added in Phase 3 and are
read from environment variables (never hardcoded).
"""

from __future__ import annotations

from pathlib import Path

# <repo>/code/buyorwait/config.py -> parents[2] == <repo>
ROOT_DIR: Path = Path(__file__).resolve().parents[2]
CODE_DIR: Path = ROOT_DIR / "code"
DATASET_DIR: Path = ROOT_DIR / "dataset"
IMAGES_DIR: Path = DATASET_DIR / "media" / "images"
OUTPUT_CSV: Path = ROOT_DIR / "output.csv"

FORECAST_HORIZON_DAYS: int = 90  # challenge requirement (problem_statement.md, "90-Day Safety Check")


# --------------------------------------------------------------------------------------
# Environment (secrets and model settings come only from the environment / .env)
# --------------------------------------------------------------------------------------

import os


def load_env() -> None:
    """Load ``<repo>/.env`` into the process environment without overriding existing values."""
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=ROOT_DIR / ".env", override=False)


def openai_settings() -> dict[str, str | None]:
    """Model settings for the OpenAI-compatible client. Never logged with the key.

    * ``OPENAI_MODEL``                 default model (explanation agent)
    * ``EVIDENCE_MODEL``               model for the EvidenceAgent (defaults to OPENAI_MODEL)
    * ``EVIDENCE_REASONING_EFFORT``    reasoning effort for the EvidenceAgent (e.g. low|medium|high)
    * ``EXPLANATION_REASONING_EFFORT`` reasoning effort for the ExplanationAgent (optional)
    """
    load_env()
    default_model = os.environ.get("OPENAI_MODEL", "gpt-5.6")
    return {
        "api_key": os.environ.get("OPENAI_API_KEY"),
        "model": default_model,
        "evidence_model": os.environ.get("EVIDENCE_MODEL") or default_model,
        "evidence_reasoning_effort": os.environ.get("EVIDENCE_REASONING_EFFORT") or None,
        "explanation_reasoning_effort": os.environ.get("EXPLANATION_REASONING_EFFORT") or None,
        "organization": os.environ.get("OPENAI_ORGANIZATION") or None,
        "base_url": os.environ.get("OPENAI_BASE_URL") or None,
    }


def make_openai_client(model: str | None = None, reasoning_effort: str | None = None):
    """Construct the PicoAgents OpenAI client from the environment. Imported lazily so the
    deterministic pipeline and tests never require picoagents or a key.

    ``reasoning_effort`` is injected into every chat-completion request because PicoAgents'
    ``Agent`` does not forward per-call parameters to the client.
    """
    from picoagents import OpenAIChatCompletionClient

    s = openai_settings()
    if not s["api_key"]:
        raise EnvironmentError("OPENAI_API_KEY is not set (put it in .env or the environment)")

    class _Client(OpenAIChatCompletionClient):
        _extra: dict = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}

        async def create(self, messages, tools=None, output_format=None, **kwargs):
            return await super().create(messages, tools=tools, output_format=output_format, **{**self._extra, **kwargs})

        async def create_stream(self, messages, tools=None, output_format=None, **kwargs):
            async for chunk in super().create_stream(messages, tools=tools, output_format=output_format, **{**self._extra, **kwargs}):
                yield chunk

    client = _Client(model=model or s["model"], api_key=s["api_key"], base_url=s["base_url"], organization=s["organization"])
    client.reasoning_effort = reasoning_effort  # for logging
    return client


def make_evidence_client():
    s = openai_settings()
    return make_openai_client(s["evidence_model"], s["evidence_reasoning_effort"])


def make_explanation_client():
    s = openai_settings()
    return make_openai_client(s["model"], s["explanation_reasoning_effort"])
