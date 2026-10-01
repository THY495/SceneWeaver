"""Run the frozen artifact out of tree, with development paths removed.

This is isolation on a developer machine, NOT a clean Windows VM acceptance.
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    work = args.work.resolve() / ("独立验证 " + uuid4().hex[:8])
    bundle = work / "发行 文件夹"
    shutil.copytree(args.bundle.resolve(), bundle)
    data, cwd = work / "玩家存档", work / "empty-cwd"
    cwd.mkdir()
    exe = bundle / "SceneweaverP0.exe"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
    }
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    checks, timings = [], []

    def run(command, *extra, code=0):
        result = work / (uuid4().hex + ".json")
        started = time.perf_counter()
        process = subprocess.run(
            [str(exe), "--data-dir", str(data), "--command", command, "--result", str(result), *extra],
            env=env,
            cwd=cwd,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
        )
        elapsed = (time.perf_counter() - started) * 1000
        assert process.returncode == code, (
            command,
            process.returncode,
            result.read_text(encoding="utf-8") if result.exists() else "no result",
        )
        if code in (71, 72):
            assert not result.exists()
            return None
        value = json.loads(result.read_text(encoding="utf-8"))
        if code == 0:
            assert value["frozen"]
        return value, elapsed

    initial, elapsed = run("snapshot")
    assert initial["revision"] == 1 and len(initial["events"]) == 1
    checks.append("standalone frozen executable initializes fixed opening")
    for _ in range(5):
        _, duration = run("snapshot")
        timings.append(duration)
    assert run("gui-smoke")[0]["gui_initialized"]
    checks.append("bundled Tcl/Tk initializes hidden GUI without developer PATH")
    assert run("knowledge", "--actor", "player", "--text", "蓝鹭")[0]["matches"] == []
    assert len(run("knowledge", "--actor", "captain", "--text", "蓝鹭")[0]["matches"]) == 1
    assert len(run("knowledge", "--actor", "player", "--text", "末班船")[0]["matches"]) == 1
    checks.append("fixture actor-scoped text lookup denies another actor's private record")
    run("drop", "--request-id", "drop-1", "--crash", "before", code=71)
    state = run("snapshot")[0]
    assert state["revision"] == 1 and state["receipts"] == 0 and len(state["events"]) == 1
    checks.append("process loss before commit rolls back state, event and receipt")
    run("drop", "--request-id", "drop-1", "--crash", "after", code=72)
    state = run("snapshot")[0]
    assert state["revision"] == 2 and state["receipts"] == 1 and len(state["events"]) == 2
    assert state["placement"] == {"kind": "location", "target": "pier"}
    run("drop", "--request-id", "drop-1")
    repeated = run("snapshot")[0]
    assert repeated["revision"] == 2 and repeated["receipts"] == 1 and len(repeated["events"]) == 2
    assert "同一请求" in run("take", "--request-id", "drop-1", code=1)[0]["error"]
    checks.append(
        "response loss after commit replays same request without duplicate effects; changed payload rejected"
    )
    run("take", "--request-id", "take-1", "--revision", "2")
    run("talk", "--request-id", "talk-1", "--text", "船还在吗？")
    run("talk", "--request-id", "talk-1", "--text", "船还在吗？")
    state = run("snapshot")[0]
    assert state["revision"] == 3 and state["receipts"] == 3 and len(state["events"]) == 5
    assert "假模型" in state["events"][-1]["text"]
    checks.append("production pure item rules and persisted fake reply survive separate process restarts")
    forbidden = {".env", "openrouter.txt", "openrouter。txt", "psycopg", "node.exe", "postgres.exe"}
    assert not [p for p in bundle.rglob("*") if p.name.lower() in forbidden]
    report = {
        "status": "passed_developer_host_isolation_not_clean_vm",
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "bundle": str(bundle),
        "data": str(data),
        "checks": checks,
        "bundle_bytes": sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()),
        "first_launch_ms": round(elapsed, 2),
        "subsequent_process_launch_ms": [round(v, 2) for v in timings],
        "median_subsequent_process_launch_ms": round(statistics.median(timings), 2),
        "network": "no network code, no real model calls; OS network access was not sandbox-disabled",
        "limits": [
            "not a clean VM",
            "not the existing full engine",
            "GUI initialization only, no visual interaction test",
            "no vector retrieval, real provider or existing worker migration",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
