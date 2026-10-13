import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("launcher", ["game_launcher.py", "studio_launcher.py"])
@pytest.mark.parametrize("smoke", [True, False])
def test_launch_failure_exits_and_reports_without_unattended_dialog(monkeypatch, tmp_path, launcher, smoke):
    root = Path("prototypes/windows_p0").resolve()
    report = tmp_path / "report.json"
    dialogs = []

    def fail(**kwargs):
        raise OSError("Backend denied by operating system")

    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setitem(sys.modules, "desktop_host", SimpleNamespace(main=fail))
    monkeypatch.setitem(
        sys.modules,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(user32=SimpleNamespace(MessageBoxW=lambda *args: dialogs.append(args)))
        ),
    )
    monkeypatch.setattr(sys, "argv", [launcher, *(["--smoke-report", str(report)] if smoke else [])])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(root / launcher), run_name="__main__")
    assert result.value.code == 1
    if smoke:
        assert not dialogs
        assert json.loads(report.read_text(encoding="utf-8")) == {
            "status": "launch_failed",
            "error": "Backend denied by operating system",
        }
    else:
        assert len(dialogs) == 1
        assert not report.exists()


@pytest.mark.parametrize("launcher", ["game_launcher.py", "studio_launcher.py"])
def test_gui_dependency_import_failure_is_reported_in_smoke_mode(monkeypatch, tmp_path, launcher):
    root = Path("prototypes/windows_p0").resolve()
    report = tmp_path / "import-failed.json"
    dialogs = []
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setitem(sys.modules, "desktop_host", None)
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(user32=SimpleNamespace(MessageBoxW=lambda *args: dialogs.append(args)))
    ))
    monkeypatch.setattr(sys, "argv", [launcher, "--smoke-report", str(report)])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(root / launcher), run_name="__main__")
    assert result.value.code == 1
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["status"] == "launch_failed" and "desktop_host" in value["error"]
    assert dialogs == []
