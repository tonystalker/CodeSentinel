"""
sentinel_review/config.py
=========================
Behavior configuration for sentinel-review, loaded from `.sentinel.yml`
in the target repo root. No credential fields live here — credentials are
in `secrets.py` (environment variables only). See skill.md section 0.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class SentinelConfig(BaseModel):
    """All behavior knobs for a sentinel-review run.

    Loaded from `.sentinel.yml` in the target repo root; missing file
    falls back to these defaults so users can try the tool with zero config.

    IMPORTANT: This model intentionally has NO field that could hold an
    API key or credential. Credentials live exclusively in SentinelSecrets
    (secrets.py), populated from environment variables. If you add a field
    here that accepts a key string, you are creating a path for accidental
    credential commits.
    """

    # --- Vector store ---
    vector_store: Literal["pinecone", "local"] = Field(
        default="local",
        description="Backend for the embedding store. 'local' uses FAISS (no external account needed).",
    )

    # --- Auto-fix gating (skill.md §5) ---
    auto_fix: bool = Field(
        default=False,
        description="Enable confidence-gated automatic application of generated fixes.",
    )
    auto_fix_confidence_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Minimum fix.confidence to auto-apply. Below this, fix is a suggestion only.",
    )
    auto_fix_allowed_severities: list[str] = Field(
        default=["high", "medium", "low"],
        description=(
            "Severities eligible for auto-apply. 'critical' is intentionally absent "
            "from the default — those get flagged for human review even if the model "
            "is confident."
        ),
    )

    # --- Sandbox (skill.md §6) ---
    max_fix_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max E2B sandbox attempts per finding before reporting as unvalidated.",
    )
    sandbox_timeout_seconds: int = Field(
        default=90,
        ge=10,
        le=600,
        description="Hard timeout per sandbox invocation. E2B bills per second — keep this bounded.",
    )

    # --- Scan scope ---
    ignore_paths: list[str] = Field(
        default_factory=list,
        description="Glob patterns of paths to exclude from analysis (e.g. 'tests/*', 'vendor/*').",
    )
    enabled_agents: list[str] = Field(
        default=["bug", "security", "docs"],
        description="Which review agents to run. Valid values: 'bug', 'security', 'docs'.",
    )

    # --- Pinecone (only used when vector_store == 'pinecone') ---
    pinecone_index_name: str = Field(
        default="sentinel-review",
        description="Pinecone index name to use. Must exist before running.",
    )
    pinecone_environment: str = Field(
        default="us-east-1-aws",
        description="Pinecone environment/region string.",
    )
    pinecone_namespace_ttl_days: int = Field(
        default=30,
        description="Delete namespaces for repos not analyzed within this many days.",
    )

    # --- Embedder ---
    embedding_model: str = Field(
        default="jinaai/jina-embeddings-v2-base-code",
        description="HuggingFace model identifier for the code embedding model.",
    )
    embedding_batch_size: int = Field(
        default=32,
        ge=1,
        le=256,
        description="Number of chunks to embed per batch.",
    )

    # --- Cache directory ---
    cache_dir: str = Field(
        default=".sentinel_cache",
        description="Directory (relative to repo root) for local FAISS index and manifests.",
    )

    @field_validator("enabled_agents")
    @classmethod
    def validate_agents(cls, v: list[str]) -> list[str]:
        valid = {"bug", "security", "docs"}
        invalid = set(v) - valid
        if invalid:
            raise ValueError(f"Unknown agent(s): {invalid!r}. Valid: {valid!r}")
        return v

    @field_validator("auto_fix_allowed_severities")
    @classmethod
    def validate_severities(cls, v: list[str]) -> list[str]:
        valid = {"critical", "high", "medium", "low"}
        invalid = set(v) - valid
        if invalid:
            raise ValueError(f"Unknown severity value(s): {invalid!r}. Valid: {valid!r}")
        return v

    model_config = {"extra": "forbid"}  # catch typos in .sentinel.yml


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(repo_path: Path) -> SentinelConfig:
    """Load SentinelConfig from ``<repo_path>/.sentinel.yml``.

    If the file is absent, returns the default config without error —
    this is the zero-config path for first-time users.

    Args:
        repo_path: Root directory of the repo being reviewed.

    Returns:
        A validated SentinelConfig instance.

    Raises:
        ValueError: If .sentinel.yml exists but contains invalid values
            (e.g. unknown agent names, out-of-range thresholds).
        yaml.YAMLError: If .sentinel.yml is malformed YAML.
    """
    config_file = Path(repo_path) / ".sentinel.yml"
    if not config_file.exists():
        return SentinelConfig()

    with config_file.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # Reject any key that looks like a credential — belt-and-suspenders
    # check so that even if someone tries to add one, we catch it early.
    _CREDENTIAL_KEYWORDS = {"key", "secret", "token", "password", "passwd", "credential"}
    for field_name in raw:
        if any(kw in field_name.lower() for kw in _CREDENTIAL_KEYWORDS):
            raise ValueError(
                f"'.sentinel.yml' field '{field_name}' looks like a credential. "
                "Credentials must be set via environment variables, never in config files. "
                "See skill.md section 0."
            )

    return SentinelConfig(**raw)
