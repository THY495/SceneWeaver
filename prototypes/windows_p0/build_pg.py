"""Build a fresh, conservative P0 PostgreSQL runtime from explicit components.

Requires pefile in the build environment. Never copies host System32 DLLs.
This is an internal prototype, not a public redistribution approval.
"""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pefile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("crt", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("vector_license", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source, crt, output = args.source.resolve(), args.crt.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError("Use a fresh output directory; existing packages are never overwritten")
    selected = {}

    def select(path, destination):
        if not path.is_file():
            raise FileNotFoundError(path)
        selected[destination] = path

    for name in ("postgres", "initdb", "pg_ctl", "pg_isready", "pg_dump", "pg_restore"):
        select(source / "bin" / f"{name}.exe", f"bin/{name}.exe")
    # initdb loads encoding conversions and the built-in PL/pgSQL handler.
    for path in (source / "lib").glob("*.dll"):
        if path.stem in {"plpgsql", "vector", "dict_snowball"} or "_and_" in path.stem:
            select(path, f"lib/{path.name}")
    for path in (source / "share").rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative.startswith("share/extension/") and not path.name.startswith(("vector", "plpgsql")):
            continue
        select(path, relative)
    # Keep the complete redistributable CRT directory contents together in bin.
    for path in crt.glob("*.dll"):
        select(path, f"bin/{path.name}")
    candidates = {p.name.lower(): p for p in (source / "bin").glob("*.dll")}
    checked = set()
    external = set()
    while True:
        pending = [
            (dest, path)
            for dest, path in selected.items()
            if dest not in checked and path.suffix.lower() in {".dll", ".exe"}
        ]
        if not pending:
            break
        for dest, path in pending:
            checked.add(dest)
            with pefile.PE(str(path), fast_load=True) as binary:
                binary.parse_data_directories(directories=[1, 13])
                for entry in [
                    *getattr(binary, "DIRECTORY_ENTRY_IMPORT", []),
                    *getattr(binary, "DIRECTORY_ENTRY_DELAY_IMPORT", []),
                ]:
                    name = entry.dll.decode("ascii").lower()
                    if any(Path(key).name.lower() == name for key in selected):
                        continue
                    if name in candidates:
                        select(candidates[name], "bin/" + candidates[name].name)
                    else:
                        external.add(name)
    output.mkdir(parents=True)
    manifest = []
    for destination, path in sorted(selected.items()):
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        manifest.append(
            {
                "path": destination,
                "bytes": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    notices = output / "notices"
    notices.mkdir()
    with zipfile.ZipFile(args.archive) as archive:
        for name in ("server_license.txt", "commandlinetools_3rd_party_licenses.txt"):
            (notices / name).write_bytes(archive.read("pgsql/" + name))
    shutil.copyfile(args.vector_license, notices / "pgvector-LICENSE")
    report = {
        "status": "internal_P0_candidate_not_release_approved",
        "crt_source_directory": crt.name,
        "policy": "explicit executables, conversion libraries, plpgsql, vector, snowball; PE closure",
        "external_imports": sorted(external),
        "files": manifest,
        "runtime_bytes": sum(item["bytes"] for item in manifest),
        "limitations": [
            "Static closure cannot see dynamic imports",
            "Full redistribution/license review remains pending",
            "Windows UCRT/system DLLs and OS compatibility require clean-host validation",
        ],
    }
    (output / "build-manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "files": len(manifest),
                "runtime_bytes": report["runtime_bytes"],
                "external_imports": report["external_imports"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
