"""A refreshed Studio must embed the newly frozen author and player backends."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "prototypes/windows_p0/assemble_studio.py"


def entry(root: Path, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_fresh_backend_overrides_replace_old_backends(tmp_path):
    dist = tmp_path / "old-studio-dist"
    runtime = tmp_path / "old-game-runtime"
    refreshed_studio = tmp_path / "new-studio-backend"
    refreshed_game = tmp_path / "new-game-backend"
    output = tmp_path / "studio"
    entry(dist, "Sceneweaver/Sceneweaver.exe", b"desktop")
    entry(dist, "SceneweaverBackend/SceneweaverBackend.exe", b"old author")
    entry(runtime, "Game/Game.exe", b"player desktop")
    entry(runtime, "GameBackend/GameBackend.exe", b"old player")
    entry(runtime, "pgsql/bin/postgres.exe", b"database")
    entry(refreshed_studio, "SceneweaverBackend.exe", b"new author")
    entry(refreshed_game, "GameBackend.exe", b"new player")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(dist),
            str(runtime),
            str(output),
            "--studio-backend",
            str(refreshed_studio),
            "--game-backend",
            str(refreshed_game),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (output / "SceneweaverBackend/SceneweaverBackend.exe").read_bytes() == b"new author"
    assert (output / "game-runtime/GameBackend/GameBackend.exe").read_bytes() == b"new player"
    assert (output / "game-runtime/Game/Game.exe").read_bytes() == b"player desktop"
    assert (output / "pgsql/bin/postgres.exe").read_bytes() == b"database"
