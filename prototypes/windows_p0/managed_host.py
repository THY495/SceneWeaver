"""Private P0 host, not a public player entry point. Windows only."""

import argparse
import ctypes as c
import json
import msvcrt
import os
import secrets
import socket
import subprocess
import time
from ctypes import wintypes as w
from pathlib import Path
from uuid import uuid4

import psycopg
from windows_job import contain_current_host, process_ids


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, directory):
        self.path = directory.resolve() / "host.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.file.close()
            raise AlreadyRunning("This data directory is already open") from exc
        return self

    def __exit__(self, *unused):
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def protect(data, decrypt=False):
    class Blob(c.Structure):
        _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_ubyte))]

    crypt = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [c.c_void_p]
    buffer = (c.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(Blob)]
    if not fn(c.byref(source), None, None, None, None, 1, c.byref(target)):
        raise c.WinError(c.get_last_error())
    try:
        return c.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def atomic_json(path, value):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, indent=2), encoding="utf-8")
    pending.replace(path)


def runtime_path(source, alias_root):
    source = source.resolve()
    if str(source).isascii():
        return source
    alias_root = alias_root.resolve()
    if not str(alias_root).isascii():
        raise ValueError(
            "P0 requires an ASCII alias root; automatic location selection is not yet implemented"
        )
    alias_root.mkdir(parents=True, exist_ok=True)
    alias = alias_root / ("pg-" + uuid4().hex)
    env = os.environ.copy()
    env["SW_P0_ALIAS"] = str(alias)
    env["SW_P0_TARGET"] = str(source)
    # Fixed script; paths are data supplied through the environment, never code.
    result = subprocess.run(
        [
            str(Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:SW_P0_ALIAS -Target $env:SW_P0_TARGET | Out-Null",
        ],
        env=env,
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or alias.resolve() != source:
        raise RuntimeError("Unable to create verified runtime directory alias")
    return alias  # Keep the alias; resolving it again would undo the workaround.


class DatabaseHost:
    def __init__(self, pgroot, directory):
        self.root, self.directory = pgroot, directory.resolve()
        self.data = self.directory / "data"
        self.started = False
        self.attempted = False
        self.port = None
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA", "USERPROFILE"}
        }
        self.env["PATH"] = str(pgroot / "bin") + os.pathsep + str(Path(os.environ["SYSTEMROOT"]) / "System32")

    def tool(self, name, *args, check=True):
        logpath = self.directory / (name + "-" + uuid4().hex[:8] + ".log")
        with logpath.open("wb") as log:
            result = subprocess.run(
                [str(self.root / "bin" / (name + ".exe")), *map(str, args)],
                cwd=self.directory,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        if check and result.returncode:
            raise RuntimeError(f"{name} failed; inspect {logpath.name}")
        return result.returncode

    def initialize(self):
        credential = self.directory / "database.dpapi"
        if credential.exists():
            try:
                self.password = protect(credential.read_bytes(), decrypt=True).decode("ascii")
            except OSError as exc:
                raise RuntimeError(
                    "无法解密本地数据库凭据。请使用创建此存档的 Windows 用户启动；"
                    "存档与凭据已保留，不会自动重建。"
                ) from exc
        else:
            if self.data.exists():
                raise RuntimeError("Existing data without credentials; refusing to replace it")
            self.password = secrets.token_hex(24)
            pending = credential.with_suffix(".tmp")
            pending.write_bytes(protect(self.password.encode("ascii")))
            pending.replace(credential)
        if self.data.exists():
            if not (self.data / "PG_VERSION").is_file():
                raise RuntimeError("Incomplete database initialization; retained for diagnosis")
            return
        pwfile = self.directory / "init-password.txt"
        try:
            pwfile.write_text(self.password, encoding="ascii")
            self.tool(
                "initdb",
                "-D",
                self.data,
                "-U",
                "probe",
                "--pwfile",
                pwfile,
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            )
        finally:
            pwfile.unlink(missing_ok=True)

    def start(self, preferred_port=0):
        # Never adopt a service started outside this owned lifecycle.
        if self.tool("pg_ctl", "-D", self.data, "status", check=False) == 0:
            raise RuntimeError("Database already running outside this host; refusing to adopt or stop it")
        for attempt in range(3):
            with socket.socket() as reservation:
                try:
                    reservation.bind(("127.0.0.1", preferred_port if attempt == 0 else 0))
                except OSError:
                    continue
                self.port = reservation.getsockname()[1]
            # Re-check readiness/auth after release; retry a failed startup rather
            # than assuming the port reservation guarantees ownership.
            with (self.data / "postgresql.conf").open("a", encoding="utf-8") as config:
                config.write(
                    f"\nlisten_addresses='127.0.0.1'\nport={self.port}\nmax_connections=24\nshared_buffers='32MB'\n"
                )
            self.attempted = True
            code = self.tool(
                "pg_ctl",
                "-D",
                self.data,
                "-l",
                self.directory / "postgres.log",
                "-t",
                "5",
                "-w",
                "start",
                check=False,
            )
            if code == 0:
                self.started = True
                with self.connect() as conn:
                    # Windows server settings may contain ACP path bytes even
                    # with a UTF8 database. Read only this setting as raw bytes;
                    # every business connection still uses normal UTF8.
                    conn.execute("SET client_encoding TO 'SQL_ASCII'")
                    raw = conn.execute("SHOW data_directory").fetchone()[0]
                    paths = []
                    for encoding in ("utf-8", "mbcs"):
                        try:
                            paths.append(Path(raw.decode(encoding)).resolve())
                        except UnicodeDecodeError:
                            pass
                    if self.data not in paths:
                        raise RuntimeError("Started database does not match owned data directory")
                return attempt + 1
            # A timed-out pg_ctl may have left a live server; never race it with a retry.
            if self.tool("pg_ctl", "-D", self.data, "status", check=False) == 0:
                raise RuntimeError("Startup incomplete with live server; cleanup required")
        raise RuntimeError("Database startup failed after three port attempts")

    def connect(self):
        return psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            user="probe",
            password=self.password,
            dbname="postgres",
            connect_timeout=5,
        )

    def stop(self):
        if self.attempted and self.tool("pg_ctl", "-D", self.data, "status", check=False) == 0:
            self.tool("pg_ctl", "-D", self.data, "-m", "fast", "-t", "10", "-w", "stop")
        self.started = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pgroot", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("alias_root", type=Path)
    parser.add_argument("control", type=Path)
    parser.add_argument("--preferred-port", type=int, default=0)
    args = parser.parse_args()
    job = contain_current_host()
    args.directory.mkdir(parents=True, exist_ok=True)
    args.control.mkdir(parents=True, exist_ok=True)
    try:
        with InstanceLock(args.directory):
            root = runtime_path(args.pgroot, args.alias_root)
            host = DatabaseHost(root, args.directory)
            try:
                host.initialize()
                attempts = host.start(args.preferred_port)
                with host.connect() as conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS host_probe(id int primary key, visits int not null)"
                    )
                    visits = conn.execute(
                        "INSERT INTO host_probe VALUES (1, 1) ON CONFLICT(id) DO UPDATE SET visits=host_probe.visits+1 RETURNING visits"
                    ).fetchone()[0]
                atomic_json(
                    args.control / "ready.json",
                    {
                        "host_pid": os.getpid(),
                        "pids": process_ids(job),
                        "port": host.port,
                        "attempts": attempts,
                        "visits": visits,
                    },
                )
                deadline = time.monotonic() + 90
                while not (args.control / "stop").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                host.stop()
            atomic_json(args.control / "closed.json", {"normal_stop": True})
    except AlreadyRunning:
        atomic_json(args.control / "rejected.json", {"reason": "already_running"})
        raise SystemExit(23)


if __name__ == "__main__":
    main()
