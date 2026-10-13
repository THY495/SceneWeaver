"""Public documentation must be approved independently of snapshot hashes."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


def candidate(tmp_path, extra=None, readme="# Sceneweaver\n", version="0.1.0-alpha.3"):
    root = Path(__file__).resolve().parents[1]
    verifier = root / "scripts/verify_release.py"
    if not verifier.exists():
        verifier = root / "scripts/release_alpha/verify_release.py"
    files = {
        "README.md": readme.encode(),
        "scripts/verify_release.py": verifier.read_bytes(),
        "src/chatbot/config.py": b'product: Literal["roleplay"] = "roleplay"\n',
        "src/chatbot/version.py": b'PUBLIC_VERSION = "0.1.0-alpha.3"\n',
        "pyproject.toml": b'[project]\nversion = "0.1.0a3"\n',
        "apps/web/index.html": b'<script src="/src/studio.tsx"></script>\n',
    }
    if extra:
        files.update(extra)
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "version": version,
        "scope": "roleplay-source-only",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    (tmp_path / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "scripts/verify_release.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result, json.loads(result.stdout)


def test_approved_public_documentation_passes(tmp_path):
    result, report = candidate(tmp_path)
    assert result.returncode == 0
    assert report["errors"] == []


def test_previous_release_identity_is_rejected(tmp_path):
    result, report = candidate(tmp_path, version="0.1.0-alpha.2")
    assert result.returncode == 1
    assert "release identity mismatch" in report["errors"]


@pytest.mark.parametrize(
    "name, data, error",
    [
        (
            "src/chatbot/version.py",
            b'PUBLIC_VERSION = "0.1.0-alpha.2"\n',
            "runtime version differs from release manifest",
        ),
        (
            "pyproject.toml",
            b'[project]\nversion = "0.1.0a2"\n',
            "package version differs from release manifest",
        ),
    ],
)
def test_inconsistent_version_sources_are_rejected(tmp_path, name, data, error):
    result, report = candidate(tmp_path, {name: data})
    assert result.returncode == 1
    assert error in report["errors"]


@pytest.mark.parametrize(
    "name",
    [
        "PUSH_TO_GITHUB.md",
        "docs/001-development-diary.md",
        "docs/AGENTS.md",
        "evals/directional-v1/roleplay/private/character.md",
        "scripts/update_release_checkout.py",
    ],
)
def test_declaring_an_internal_file_in_the_manifest_does_not_publish_it(tmp_path, name):
    result, report = candidate(tmp_path, {name: b"Private maintenance material\n"})
    assert result.returncode == 1
    assert any(name in error for error in report["errors"])
    assert not any("file set changed" in error for error in report["errors"])


def test_public_readme_rejects_machine_specific_workspace_paths(tmp_path):
    result, report = candidate(tmp_path, readme="# Sceneweaver\nZ:\\WorkSpace\\private\\run\n")
    assert result.returncode == 1
    assert "local workspace path in public documentation: README.md" in report["errors"]
