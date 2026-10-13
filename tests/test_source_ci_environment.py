"""CI must test its own installation and cannot inherit developer credentials."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def inspect_environment():
    root = Path(__file__).resolve().parents[1]
    public = root / "scripts/check_environment.py"
    template = root / "scripts/release_alpha/check_environment.py"
    spec = importlib.util.spec_from_file_location("source_ci_environment", public if public.exists() else template)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.inspect_environment


def test_own_checkout_without_configuration_passes(tmp_path, inspect_environment):
    result = inspect_environment(tmp_path, tmp_path / "src/chatbot/__init__.py", {})
    assert result["errors"] == []


def test_development_editable_package_is_rejected(tmp_path, inspect_environment):
    result = inspect_environment(tmp_path / "release", tmp_path / "src/chatbot/__init__.py", {})
    assert "chatbot import comes from another checkout" in result["errors"]


def test_application_credentials_rejected_without_printing_values(tmp_path, inspect_environment):
    result = inspect_environment(
        tmp_path,
        tmp_path / "src/chatbot/__init__.py",
        {"CHAT_API_KEY": "synthetic-private-value", "CHAT_API_KEY_FILE": "private-filename"},
    )
    assert result["errors"]
    assert "synthetic-private-value" not in json.dumps(result)
    assert "private-filename" not in json.dumps(result)


def test_local_env_file_is_rejected_without_reading_it(tmp_path, inspect_environment):
    (tmp_path / ".env").write_text("private-fixture", encoding="utf-8")
    result = inspect_environment(tmp_path, tmp_path / "src/chatbot/__init__.py", {})
    assert "local .env must be absent for source CI" in result["errors"]


def test_injected_pythonpath_is_rejected(tmp_path, inspect_environment):
    result = inspect_environment(tmp_path, tmp_path / "src/chatbot/__init__.py", {"PYTHONPATH": "other"})
    assert "PYTHONPATH must be absent for source CI" in result["errors"]
