"""
Tests for pipeline config CLI helpers.

All tests are pure — they use tmp_path and never touch the real .env.
No CLI invocation; the pure functions _set_env_key and _read_env_keys
are tested directly.

Tests cover:
  - _read_env_keys: standard parsing, comments, blank lines, quoted values
  - _set_env_key: write new key, update existing, preserve other keys,
                  create file if missing, handle file with no trailing newline
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.cli import _read_env_keys, _set_env_key


# ── _read_env_keys ─────────────────────────────────────────────────────────────


def test_read_env_keys_parses_standard_format(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-test\nCV_PATH=/home/user/cv.pdf\n")
    result = _read_env_keys(env)
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert result["CV_PATH"] == "/home/user/cv.pdf"


def test_read_env_keys_skips_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# This is a comment\nANTHROPIC_API_KEY=sk-ant-test\n")
    result = _read_env_keys(env)
    assert "# This is a comment" not in result
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test"


def test_read_env_keys_skips_blank_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("\nANTHROPIC_API_KEY=sk-ant-test\n\nCV_PATH=/cv.pdf\n")
    result = _read_env_keys(env)
    assert len(result) == 2


def test_read_env_keys_strips_quoted_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-ant-test"\nCV_PATH=\'/cv.pdf\'\n')
    result = _read_env_keys(env)
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert result["CV_PATH"] == "/cv.pdf"


def test_read_env_keys_returns_empty_for_missing_file(tmp_path):
    result = _read_env_keys(tmp_path / ".env")
    assert result == {}


# ── _set_env_key ───────────────────────────────────────────────────────────────


def test_set_env_key_writes_new_key(tmp_path):
    env = tmp_path / ".env"
    _set_env_key("ANTHROPIC_API_KEY", "sk-ant-test", env)
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env.read_text()


def test_set_env_key_creates_file_if_missing(tmp_path):
    env = tmp_path / ".env"
    assert not env.exists()
    _set_env_key("ANTHROPIC_API_KEY", "sk-ant-test", env)
    assert env.exists()
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env.read_text()


def test_set_env_key_updates_existing_key_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=old-value\nCV_PATH=/cv.pdf\n")
    _set_env_key("ANTHROPIC_API_KEY", "new-value", env)
    content = env.read_text()
    assert "ANTHROPIC_API_KEY=new-value" in content
    assert "ANTHROPIC_API_KEY=old-value" not in content


def test_set_env_key_preserves_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-test\nCV_PATH=/cv.pdf\nOUTPUT_DIR=./output\n")
    _set_env_key("CV_PATH", "/new/path/cv.pdf", env)
    result = _read_env_keys(env)
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert result["CV_PATH"] == "/new/path/cv.pdf"
    assert result["OUTPUT_DIR"] == "./output"


def test_set_env_key_preserves_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Anthropic config\nANTHROPIC_API_KEY=old\n")
    _set_env_key("ANTHROPIC_API_KEY", "new", env)
    content = env.read_text()
    assert "# Anthropic config" in content


def test_set_env_key_appends_when_key_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CV_PATH=/cv.pdf\n")
    _set_env_key("ANTHROPIC_API_KEY", "sk-ant-new", env)
    result = _read_env_keys(env)
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-new"
    assert result["CV_PATH"] == "/cv.pdf"


def test_set_env_key_handles_no_trailing_newline(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CV_PATH=/cv.pdf")  # no trailing newline
    _set_env_key("ANTHROPIC_API_KEY", "sk-ant-test", env)
    result = _read_env_keys(env)
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert result["CV_PATH"] == "/cv.pdf"


def test_set_env_key_case_insensitive_match(tmp_path):
    """Key comparison should be case-insensitive (env vars are conventionally upper)."""
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=old\n")
    _set_env_key("anthropic_api_key", "new", env)
    content = env.read_text()
    # Should replace, not duplicate
    assert content.count("anthropic_api_key") + content.count("ANTHROPIC_API_KEY") == 1
