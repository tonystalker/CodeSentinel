"""
sentinel_review/secrets.py
==========================
Credential resolution for sentinel-review. All API keys live here and
here only, sourced from environment variables (or a local .env file).
No key ever touches .sentinel.yml or config.py — see skill.md section 0.

BYOK model: users supply their own keys. The package never bundles or
transmits the maintainer's credentials.

Required env var:
    GROQ_API_KEY        — Groq inference (Llama 3.3 70B). Only required key.

Optional env vars (missing = graceful degradation):
    PINECONE_API_KEY    — Pinecone vector store. Missing → falls back to local FAISS.
    E2B_API_KEY         — E2B sandbox. Missing → fixes reported as unvalidated suggestions.
    LANGCHAIN_API_KEY   — LangSmith tracing. Missing → tracing is a no-op.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

# Load .env if present (gitignored, user-local). Safe to call repeatedly.
load_dotenv(override=False)


class SentinelSecrets(BaseModel):
    """Validated credential bundle resolved from environment variables.

    Constructed only by ``load_secrets()``. Never instantiate directly
    from user-supplied data — that would defeat the purpose.
    """

    groq_api_key: str = Field(
        description="Groq API key for Llama 3.3 70B inference. Required."
    )
    pinecone_api_key: str | None = Field(
        default=None,
        description="Pinecone API key. None → local FAISS fallback.",
    )
    e2b_api_key: str | None = Field(
        default=None,
        description="E2B API key for sandbox execution. None → fixes are unvalidated suggestions.",
    )
    langchain_api_key: str | None = Field(
        default=None,
        description="LangSmith API key for tracing. None → tracing disabled (no-op).",
    )

    @field_validator("groq_api_key")
    @classmethod
    def groq_key_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("GROQ_API_KEY is set but empty. Provide a valid key.")
        return v.strip()

    @model_validator(mode="after")
    def configure_langsmith(self) -> "SentinelSecrets":
        """Set LANGCHAIN_TRACING_V2 env var so LangSmith auto-activates."""
        if self.langchain_api_key:
            os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
            os.environ.setdefault("LANGCHAIN_API_KEY", self.langchain_api_key)
        return self

    model_config = {"extra": "forbid"}

    # Convenience properties
    @property
    def has_pinecone(self) -> bool:
        return self.pinecone_api_key is not None

    @property
    def has_e2b(self) -> bool:
        return self.e2b_api_key is not None

    @property
    def has_langsmith(self) -> bool:
        return self.langchain_api_key is not None


def load_secrets() -> SentinelSecrets:
    """Resolve all credentials from environment variables.

    Only ``GROQ_API_KEY`` is required. Missing optional keys degrade
    gracefully as documented in skill.md section 0:
    - No PINECONE_API_KEY  → local FAISS (factory.py handles this)
    - No E2B_API_KEY       → fixes are unvalidated suggestions only
    - No LANGCHAIN_API_KEY → tracing is a no-op

    Returns:
        Validated SentinelSecrets.

    Raises:
        ValueError: If GROQ_API_KEY is absent or empty, with a clear
            message naming the missing variable and where to get the key.
    """
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise ValueError(
            "GROQ_API_KEY environment variable is not set.\n"
            "sentinel-review requires a Groq API key to run.\n"
            "Get one free at https://console.groq.com → API Keys.\n"
            "Then: export GROQ_API_KEY=gsk_...\n"
            "Or add it to a .env file in your repo root (it will be gitignored)."
        )

    return SentinelSecrets(
        groq_api_key=groq_key,
        pinecone_api_key=os.environ.get("PINECONE_API_KEY") or None,
        e2b_api_key=os.environ.get("E2B_API_KEY") or None,
        langchain_api_key=os.environ.get("LANGCHAIN_API_KEY") or None,
    )
