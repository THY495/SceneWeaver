"""Kill only a private probe host; verify its recorded job members terminate."""

import argparse
import ctypes as c
import json
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
    args = parser.parse_args()
    run = args.work.resolve() / ("host-" + uuid4().hex[:8])
    run.mkdir(parents=True)
    ready = run / "ready.json"
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    handles = []
    actual_host = None
    with (run / "host.log").open("wb") as log:
        host = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("pg_probe.py")),
                str(args.pgroot.absolute()),
                str(run),
                str(run / "probe.json"),
                "--host-crash-ready",
                str(ready),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not ready.exists():
                if host.poll() is not None:
                    raise RuntimeError("Probe exited before ready; inspect host.log")
                time.sleep(0.05)
            if not ready.exists():
                raise TimeoutError("Probe did not become ready")
            state = json.loads(ready.read_text(encoding="utf-8"))
            # Windows venv python.exe may be a launcher for a second Python
            # process. Terminate the actual host that owns the job handle.
            assert state["host_pid"] in state["pids"] and len(state["pids"]) > 1
            for pid in state["pids"]:
                access = 0x100001 if pid == state["host_pid"] else 0x100000
                handle = kernel.OpenProcess(access, False, pid)
                if not handle:
                    raise c.WinError(c.get_last_error())
                handles.append(handle)
                if pid == state["host_pid"]:
                    actual_host = handle
            before = time.perf_counter()
            assert kernel.TerminateProcess(actual_host, 73)  # No host finally executes.
            host.wait(timeout=5)
            assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
            elapsed = round((time.perf_counter() - before) * 1000, 2)
            with socket.socket() as client:
                client.settimeout(1)
                assert client.connect_ex(("127.0.0.1", state["port"])) != 0
            result = {
                "status": "passed_on_developer_host",
                "job_processes": len(handles),
                "host_kill_to_all_exited_ms": elapsed,
                "port_closed": True,
                "limitations": [
                    "Private PostgreSQL host only; not the full player shell",
                    "No assertion about clean-machine compatibility",
                ],
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
        finally:
            if actual_host and kernel.WaitForSingleObject(actual_host, 0) != 0:
                kernel.TerminateProcess(actual_host, 74)
            if host.poll() is None:
                host.kill()
                host.wait(timeout=5)
            for handle in handles:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
