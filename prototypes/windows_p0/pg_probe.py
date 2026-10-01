"""Start a private native PostgreSQL cluster, test production paths, stop it.

Does not install a Windows service or touch the development Docker database.
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pgroot", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--host-crash-ready", type=Path)
    args = parser.parse_args()
    job = None
    if args.host_crash_ready:
        from windows_job import contain_current_host, process_ids

        job = contain_current_host()
    # Preserve a caller-supplied junction alias for the path compatibility probe.
    pgroot = args.pgroot.absolute()
    run = args.work.resolve() / ("native-" + uuid4().hex[:8])
    run.mkdir(parents=True)
    data = run / "data"
    password = secrets.token_hex(24)
    pwfile = run / "init-password.txt"
    pwfile.write_text(password, encoding="ascii")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
    }
    env["PATH"] = str(pgroot / "bin") + os.pathsep + str(Path(os.environ["SYSTEMROOT"]) / "System32")
    report = {"port": port, "run": str(run), "database_host": "127.0.0.1", "status": "running"}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def tool(name, *arguments, check=True):
        # A server child can inherit pipe handles on Windows and keep communicate()
        # waiting after pg_ctl exits. Files avoid tying startup to pipe EOF.
        output = run / (name + "-" + uuid4().hex[:8] + ".log")
        with output.open("wb") as log:
            result = subprocess.run(
                [str(pgroot / "bin" / (name + ".exe")), *map(str, arguments)],
                env=env,
                cwd=run,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        if check and result.returncode:
            raise RuntimeError(output.read_text(encoding="utf-8", errors="replace"))
        return result

    started = False
    try:
        before = time.perf_counter()
        tool(
            "initdb",
            "-D",
            data,
            "-U",
            "probe",
            "--pwfile",
            pwfile,
            "--auth=scram-sha-256",
            "--encoding=UTF8",
            "--locale=C",
        )
        report["initdb_ms"] = round((time.perf_counter() - before) * 1000, 2)
        pwfile.unlink()
        with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
            config.write(
                f"\nlisten_addresses='127.0.0.1'\nport={port}\nmax_connections=24\nshared_buffers='32MB'\n"
            )
        before = time.perf_counter()
        tool(
            "pg_ctl",
            "-D",
            data,
            "-l",
            run / "postgres.log",
            "-w",
            "start",
        )
        started = True
        report["start_ms"] = round((time.perf_counter() - before) * 1000, 2)
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="probe",
            password=password,
            dbname="postgres",
            autocommit=True,
            connect_timeout=5,
        ) as conn:
            report["postgres_version"] = conn.execute("SELECT version()").fetchone()[0]
            conn.execute("CREATE EXTENSION vector")
            report["vector_version"] = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()[0]
            assert conn.execute("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector").fetchone()[0] == 0
            conn.execute("CREATE TABLE restart_probe(id int primary key, payload jsonb)")
            conn.execute("INSERT INTO restart_probe VALUES (1, '{\"ok\":true}')")
        if args.host_crash_ready:
            ready_temp = args.host_crash_ready.with_suffix(".tmp")
            ready_temp.write_text(
                json.dumps(
                    {"host_pid": os.getpid(), "pids": process_ids(job), "port": port, "data": str(data)}
                ),
                encoding="utf-8",
            )
            ready_temp.replace(args.host_crash_ready)
            # Bounded fallback if the external verifier fails before killing us.
            time.sleep(30)
            raise RuntimeError("Crash verifier did not terminate the host within 30 seconds")
        # Test runner needs development Python; only native DB processes use the minimal environment.
        testenv = os.environ.copy()
        testenv["TEST_DATABASE_URL"] = (
            f"postgresql+psycopg://probe:{password}@127.0.0.1:{port}/sceneweaver_native_test"
        )
        testenv["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        tests = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_item_transfers.py",
                "tests/test_runtime_completion_race.py",
                "tests/test_player_reading.py",
                "tests/test_player_snapshot.py",
                "tests/test_agents_worker.py::test_graph_pg_checkpoint_and_business_effect",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "pytest_asyncio.plugin",
                "--basetemp=" + str(run / "pytest"),
                "--junitxml=" + str(args.report.parent / "native-pg-tests.xml"),
            ],
            env=testenv,
            capture_output=True,
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output = (tests.stdout + tests.stderr).decode(errors="replace").replace(password, "[REDACTED]")
        (args.report.parent / "native-pg-tests.txt").write_text(output, encoding="utf-8")
        report["pytest_exit"] = tests.returncode
        assert tests.returncode == 0, "Production test slice failed; see native-pg-tests.txt"
        before = time.perf_counter()
        tool("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
        started = False
        report["stop_ms"] = round((time.perf_counter() - before) * 1000, 2)
        before = time.perf_counter()
        tool("pg_ctl", "-D", data, "-l", run / "postgres.log", "-w", "start")
        started = True
        report["restart_ms"] = round((time.perf_counter() - before) * 1000, 2)
        with psycopg.connect(
            host="127.0.0.1", port=port, user="probe", password=password, dbname="postgres", connect_timeout=5
        ) as conn:
            assert conn.execute("SELECT payload FROM restart_probe WHERE id=1").fetchone()[0] == {"ok": True}
        # Immediate shutdown requires WAL recovery. Keep a transaction open so
        # recovery must distinguish committed data from an unfinished update.
        pending = psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="probe",
            password=password,
            dbname="postgres",
            connect_timeout=5,
        )
        try:
            pending.execute("UPDATE restart_probe SET payload = '{\"ok\":false}' WHERE id=1")
            tool("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")
            started = False
        finally:
            pending.close()
        before = time.perf_counter()
        tool("pg_ctl", "-D", data, "-l", run / "postgres.log", "-w", "start")
        started = True
        report["recovery_start_ms"] = round((time.perf_counter() - before) * 1000, 2)
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="probe",
            password=password,
            dbname="postgres",
            connect_timeout=5,
        ) as conn:
            assert conn.execute("SELECT payload FROM restart_probe WHERE id=1").fetchone()[0] == {"ok": True}
        report["immediate_shutdown_recovery"] = "committed row retained; unfinished update rolled back"
        report["runtime_bytes"] = sum(
            p.stat().st_size
            for name in ["bin", "lib", "share"]
            for p in (pgroot / name).rglob("*")
            if p.is_file()
        )
        report["cluster_bytes_after_tests"] = sum(p.stat().st_size for p in data.rglob("*") if p.is_file())
        report["status"] = "passed_on_developer_host"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc).replace(password, "[REDACTED]")
        raise
    finally:
        if started or tool("pg_ctl", "-D", data, "status", check=False).returncode == 0:
            result = tool("pg_ctl", "-D", data, "-m", "fast", "-w", "stop", check=False)
            report["final_stop_exit"] = result.returncode
        report["server_running_after_probe"] = (
            tool("pg_ctl", "-D", data, "status", check=False).returncode == 0
        )
        if pwfile.exists():
            pwfile.unlink()
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
