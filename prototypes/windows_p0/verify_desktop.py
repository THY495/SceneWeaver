"""Hidden-window component check, not a human UI usability test."""

import argparse
import json
import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        verify(args)
    except Exception as exc:
        # Launch failures happen before a child can create backend.log. Preserve
        # an explicit failed receipt instead of leaving an old passing report.
        report = {
            "status": "failed",
            "phase": "desktop_component_verification",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "winerror": getattr(exc, "winerror", None),
            "executable": str(args.executable.resolve()),
            "window_verified": False,
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


def verify(args):
    run = args.work.resolve() / ("desktop-" + uuid4().hex[:8])
    run.mkdir(parents=True)
    smoke = run / "window.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
    }
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    result = subprocess.run(
        [str(args.executable.resolve()), "--data", str(run / "data"), "--smoke-report", str(smoke)],
        env=env,
        cwd=run,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, "Frozen desktop failed; inspect this run's backend.log"
    assert json.loads(smoke.read_text())["bootstrap_redirected"]
    controls = list((run / "data").glob("control-*"))
    assert len(controls) == 1
    closed = json.loads((controls[0] / "backend-closed.json").read_text())
    assert closed["fake_calls"] == 0
    ready = json.loads((controls[0] / "ready.json").read_text())
    with socket.socket() as client:
        client.settimeout(1)
        assert client.connect_ex(("127.0.0.1", ready["port"])) != 0
    report = {
        "status": "passed_on_developer_host",
        "window": json.loads(smoke.read_text()),
        "backend_normal_close": True,
        "http_port_closed": True,
        "limitations": [
            "hidden window load/redirect only, no clicks or visual review",
            "existing host WebView2/.NET, no clean-machine validation",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
