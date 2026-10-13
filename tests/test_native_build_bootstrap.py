"""Build gates must reject stale locks and avoid the developer environment."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("native_build", Path("prototypes/windows_p0/native_build.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_combined_lock_matches_application_inputs():
    assert build.validate_lock()["python"] == "3.13.9"


@pytest.mark.parametrize("changed", ["application_constraints", "lock_sha256"])
def test_stale_lock_refused_before_environment_install(tmp_path, monkeypatch, changed):
    metadata = dict(build.validate_lock())
    metadata[changed] = "stale"
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(metadata), "utf-8")
    monkeypatch.setattr(build, "INPUTS", path)
    with pytest.raises(ValueError, match="Native lock inputs changed"):
        build.validate_lock()


def test_text_input_hash_tolerates_windows_checkout_newlines(tmp_path):
    path = tmp_path / "input.lock"
    path.write_bytes(b"a\nb\n")
    digest = build.text_sha(path)
    path.write_bytes(b"a\r\nb\r\n")
    assert build.text_sha(path) == digest


@pytest.mark.parametrize("relative", [".", ".runtime", ".runtime/../src", "../outside"])
def test_output_cannot_adopt_source_or_parent(relative):
    with pytest.raises(ValueError, match="subdirectory"):
        build.checked_output(build.ROOT / relative)


def test_child_environment_has_no_model_or_python_overrides(tmp_path, monkeypatch):
    for key in ("CHAT_API_KEY", "CHAT_PRODUCT", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT", "NODE_OPTIONS"):
        monkeypatch.setenv(key, "synthetic-test-override")
    env = build.build_environment(tmp_path)
    assert not any(key.startswith("CHAT_") for key in env)
    assert all(key not in env for key in ("PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT", "NODE_OPTIONS"))
    assert env["UV_CACHE_DIR"] == str(tmp_path / "uv-cache")
