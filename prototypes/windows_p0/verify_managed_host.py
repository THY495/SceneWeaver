"""Integration checks for isolated managed PostgreSQL hosts; no model calls."""

import argparse
import ctypes as c
import json
import os
import socket
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pgroot", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--host-exe", type=Path)
    args = parser.parse_args()
    run = args.work.resolve() / ("managed-" + uuid4().hex[:8])
    run.mkdir(parents=True)
    children = []
    checks = []

    def launch(story, port=0, root=None):
        control = run / ("control-" + uuid4().hex[:8])
        control.mkdir()
        log = (control / "output.log").open("wb")
        entry = (
            [str(args.host_exe.resolve())]
            if args.host_exe
            else [sys.executable, str(Path(__file__).resolve().with_name("managed_host.py"))]
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
        }
        env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
        child = subprocess.Popen(
            entry
            + [
                str(root or args.pgroot.resolve()),
                str(run / story),
                str(run / "aliases"),
                str(control),
                "--preferred-port",
                str(port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=env,
            cwd=control,
        )
        children.append((child, control, log))
        return child, control

    def ready(child, control):
        deadline = time.monotonic() + 20
        while not (control / "ready.json").exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Host did not become ready; inspect {control / 'output.log'}")
            time.sleep(0.05)
        return json.loads((control / "ready.json").read_text(encoding="utf-8"))

    def stop(child, control):
        (control / "stop").touch()
        assert child.wait(timeout=20) == 0
        assert json.loads((control / "closed.json").read_text())["normal_stop"]

    def kill_host(state):
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handles = []
        try:
            for pid in state["pids"]:
                handle = kernel.OpenProcess(0x100001 if pid == state["host_pid"] else 0x100000, False, pid)
                if not handle:
                    # Short-lived helper processes recorded at readiness may
                    # already have exited while the second story was tested.
                    if c.get_last_error() == 87 and pid != state["host_pid"]:
                        continue
                    raise c.WinError(c.get_last_error())
                handles.append((pid, handle))
            owner = next(handle for pid, handle in handles if pid == state["host_pid"])
            assert kernel.TerminateProcess(owner, 75)
            assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for _, handle in handles)
        finally:
            for _, handle in handles:
                kernel.CloseHandle(handle)

    try:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            a, ac = launch("作品 A", occupied.getsockname()[1])
            first = ready(a, ac)
            assert first["attempts"] == 2 and first["visits"] == 1
            checks.append("occupied preferred port is skipped; independent port starts successfully")
        duplicate, dc = launch("作品 A")
        assert duplicate.wait(timeout=10) == 23
        assert json.loads((dc / "rejected.json").read_text())["reason"] == "already_running"
        checks.append("second host for same canonical data directory is rejected before database access")
        b, bc = launch("作品 B")
        second = ready(b, bc)
        assert second["port"] != first["port"] and second["visits"] == 1
        assert a.poll() is None
        stop(b, bc)
        assert a.poll() is None
        checks.append("two stories coexist; stopping B does not stop A or share its persisted counter")
        kill_host(first)
        a.wait(timeout=10)
        restarted, rc = launch("作品 A")
        restored = ready(restarted, rc)
        assert restored["visits"] == 2
        stop(restarted, rc)
        checks.append(
            "forced host exit releases lock and children; restart decrypts credential and preserves committed data"
        )
        bad, badc = launch("失败恢复", root=run / "missing-runtime")
        assert bad.wait(timeout=15) != 0
        assert not (run / "失败恢复" / "init-password.txt").exists()
        fixed, fc = launch("失败恢复")
        assert ready(fixed, fc)["visits"] == 1
        stop(fixed, fc)
        checks.append(
            "missing runtime fails without a ready marker; password temp removed; next valid launch succeeds"
        )
        result = {
            "status": "passed_on_developer_host",
            "checks": checks,
            "limitations": [
                "frozen database host, not full player"
                if args.host_exe
                else "development Python, no frozen player",
                "ASCII alias root supplied by verifier",
                "DPAPI protects local DB credential, not yet player model keys",
                "no cross-user or clean-Windows validation",
            ],
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        for child, control, log in children:
            if child.poll() is None:
                (control / "stop").touch()
                try:
                    child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    marker = control / "ready.json"
                    if marker.exists():
                        kill_host(json.loads(marker.read_text(encoding="utf-8")))
                    child.kill()
            log.close()


if __name__ == "__main__":
    main()
