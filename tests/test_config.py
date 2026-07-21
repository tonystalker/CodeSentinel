"""
tests/test_config.py
====================
Task 1 acceptance tests for config.py and secrets.py.

Done-when criteria (build.md Task 1):
  ✓ Load a sample .sentinel.yml and confirm overrides apply
  ✓ Loading a repo with no config file returns defaults without error
  ✓ .sentinel.yml schema has no field that accepts a credential
  ✓ load_secrets() with only GROQ_API_KEY set returns optional fields as None
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from sentinel_review.config import SentinelConfig, load_config
from sentinel_review.secrets import SentinelSecrets, load_secrets


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Return a temporary directory representing a repo root."""
    return tmp_path


@pytest.fixture()
def repo_with_config(tmp_repo: Path) -> Path:
    """Write a sample .sentinel.yml and return the repo path."""
    config_data = {
        "vector_store": "local",
        "auto_fix": True,
        "auto_fix_confidence_threshold": 0.90,
        "auto_fix_allowed_severities": ["high", "medium"],
        "max_fix_attempts": 2,
        "sandbox_timeout_seconds": 60,
        "ignore_paths": ["tests/*", "vendor/*"],
        "enabled_agents": ["bug", "security"],
        "embedding_model": "jinaai/jina-embeddings-v2-base-code",
        "cache_dir": ".my_sentinel_cache",
    }
    (tmp_repo / ".sentinel.yml").write_text(yaml.dump(config_data), encoding="utf-8")
    return tmp_repo


# ---------------------------------------------------------------------------
# Task 1 acceptance tests
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_returns_defaults_when_no_config_file(self, tmp_repo: Path) -> None:
        """Loading a repo with no .sentinel.yml returns defaults without error."""
        cfg = load_config(tmp_repo)
        assert isinstance(cfg, SentinelConfig)
        assert cfg.vector_store == "local"
        assert cfg.auto_fix is False
        assert cfg.auto_fix_confidence_threshold == 0.85
        assert cfg.max_fix_attempts == 3
        assert cfg.sandbox_timeout_seconds == 90
        assert cfg.ignore_paths == []
        assert set(cfg.enabled_agents) == {"bug", "security", "docs"}

    def test_overrides_apply_from_sentinel_yml(self, repo_with_config: Path) -> None:
        """Values in .sentinel.yml override all defaults."""
        cfg = load_config(repo_with_config)
        assert cfg.auto_fix is True
        assert cfg.auto_fix_confidence_threshold == 0.90
        assert cfg.auto_fix_allowed_severities == ["high", "medium"]
        assert cfg.max_fix_attempts == 2
        assert cfg.sandbox_timeout_seconds == 60
        assert cfg.ignore_paths == ["tests/*", "vendor/*"]
        assert set(cfg.enabled_agents) == {"bug", "security"}
        assert cfg.cache_dir == ".my_sentinel_cache"

    def test_schema_has_no_credential_field(self) -> None:
        """.sentinel.yml schema must not define any field that accepts a credential.

        This test enumerates all field names on SentinelConfig and asserts
        that none of them look like they could hold an API key.
        """
        credential_keywords = {"key", "secret", "token", "password", "passwd", "credential"}
        for field_name in SentinelConfig.model_fields:
            for kw in credential_keywords:
                assert kw not in field_name.lower(), (
                    f"SentinelConfig has field '{field_name}' which contains '{kw}'. "
                    "Credential fields must never appear in config.py — see skill.md §0."
                )

    def test_invalid_agent_name_raises(self, tmp_repo: Path) -> None:
        """Unknown agent name in .sentinel.yml raises a clear error."""
        (tmp_repo / ".sentinel.yml").write_text(
            yaml.dump({"enabled_agents": ["bug", "turbo_secret_agent"]}),
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="Unknown agent"):
            load_config(tmp_repo)

    def test_credential_key_in_yml_raises(self, tmp_repo: Path) -> None:
        """A field containing 'key' in .sentinel.yml raises a clear error."""
        (tmp_repo / ".sentinel.yml").write_text(
            "groq_api_key: gsk_fake123\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="credential"):
            load_config(tmp_repo)

    def test_extra_fields_in_yml_raise(self, tmp_repo: Path) -> None:
        """Extra (unknown) fields in .sentinel.yml raise a validation error."""
        (tmp_repo / ".sentinel.yml").write_text(
            yaml.dump({"this_field_does_not_exist": True}),
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            load_config(tmp_repo)

    def test_invalid_vector_store_raises(self, tmp_repo: Path) -> None:
        (tmp_repo / ".sentinel.yml").write_text(
            yaml.dump({"vector_store": "redis"}),
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            load_config(tmp_repo)


class TestLoadSecrets:
    def test_only_groq_key_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_secrets() with only GROQ_API_KEY set returns optional fields as None."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123")
        monkeypatch.delenv("PINECONE_API_KEY", raising=False)
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

        secrets = load_secrets()
        assert secrets.groq_api_key == "gsk_test_key_123"
        assert secrets.pinecone_api_key is None
        assert secrets.e2b_api_key is None
        assert secrets.langchain_api_key is None

    def test_missing_groq_key_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing GROQ_API_KEY raises ValueError naming the missing var."""
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            load_secrets()

    def test_all_optional_keys_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All four keys populated → all four fields non-None."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123")
        monkeypatch.setenv("PINECONE_API_KEY", "pc_fake_456")
        monkeypatch.setenv("E2B_API_KEY", "e2b_fake_789")
        monkeypatch.setenv("LANGCHAIN_API_KEY", "lsm_fake_abc")

        secrets = load_secrets()
        assert secrets.has_pinecone
        assert secrets.has_e2b
        assert secrets.has_langsmith

    def test_secrets_model_has_no_config_mixing(self) -> None:
        """SentinelSecrets must only hold credential fields, not behavior config."""
        behavior_config_keywords = {
            "threshold", "timeout", "attempts", "vector_store",
            "ignore", "enabled", "cache", "batch"
        }
        for field_name in SentinelSecrets.model_fields:
            for kw in behavior_config_keywords:
                assert kw not in field_name.lower(), (
                    f"SentinelSecrets has '{field_name}' which looks like behavior config. "
                    "Behavior config belongs in config.py, not secrets.py."
                )

    def test_empty_groq_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GROQ_API_KEY set to whitespace raises ValueError."""
        monkeypatch.setenv("GROQ_API_KEY", "   ")
        with pytest.raises(ValueError, match="empty"):
            load_secrets()
