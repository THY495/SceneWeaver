import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("game_manifest", Path("prototypes/windows_p0/game_manifest.py"))
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


def test_release_identity_is_strict_and_package_integrity_is_checked(tmp_path):
    assert manifest.load_release(tmp_path) is None
    raw = b'{"recipe":{}}'
    (tmp_path / "story.json").write_bytes(raw)
    data = {
        "format": "ai-novel-game.v1",
        "id": "2b96f902-cf28-454d-8ae7-5774982f72a9",
        "title": "雨灯",
        "package_sha256": hashlib.sha256(raw).hexdigest(),
    }
    path = tmp_path / "game.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert manifest.load_release(tmp_path)["title"] == "雨灯"
    (tmp_path / "story.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="不一致"):
        manifest.load_release(tmp_path)
    (tmp_path / "story.json").write_bytes(raw)
    for key, value in [("id", "../another-game"), ("title", "bad\nname"), ("backend", "arbitrary.exe")]:
        path.write_text(json.dumps({**data, key: value}), encoding="utf-8")
        with pytest.raises(ValueError):
            manifest.load_release(tmp_path)


def test_assembly_rejects_overlap_and_invalid_title_before_copying(tmp_path):
    # Placeholder executables exercise layout only, never native launch.
    desktop, backend, postgres = [tmp_path / name for name in ("desktop", "backend", "postgres")]
    for directory, entry in (
        (desktop, "Game.exe"),
        (backend, "GameBackend.exe"),
        (postgres, "bin/postgres.exe"),
    ):
        file = directory / entry
        file.parent.mkdir(parents=True)
        file.write_bytes(b"layout-only")
    package = tmp_path / "input.json"
    package.write_text(json.dumps({"recipe": {"title": "bad\nname"}}), encoding="utf-8")
    output = tmp_path / "release"

    def assemble(destination):
        return subprocess.run(
            [
                sys.executable,
                "prototypes/windows_p0/assemble_game.py",
                str(desktop),
                str(backend),
                str(postgres),
                str(package),
                str(destination),
                "--game-id",
                "2b96f902-cf28-454d-8ae7-5774982f72a9",
            ],
            capture_output=True,
            check=False,
        )

    assert assemble(output).returncode != 0
    assert not output.exists()
    assert assemble(desktop / "nested").returncode != 0
    assert not (desktop / "nested").exists()
    package.write_text(json.dumps({"recipe": {"title": "雨灯"}}), encoding="utf-8")
    result = assemble(output)
    assert result.returncode == 0, result.stderr
    assert manifest.load_release(output)["title"] == "雨灯"
    assert (output / "GameBackend/GameBackend.exe").is_file()
    assert (output / "pgsql/bin/postgres.exe").is_file()
    assert assemble(output).returncode != 0
