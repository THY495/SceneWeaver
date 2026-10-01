"""Inventory native imports without treating host DLL availability as portability."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import pefile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    files = sorted(p for folder in ("bin", "lib", "share") for p in (root / folder).rglob("*") if p.is_file())
    bundled = {p.name.lower() for p in files if p.suffix.lower() in {".dll", ".exe"}}
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    imports = {}
    inventory = []
    for path in files:
        inventory.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        if path.suffix.lower() not in {".dll", ".exe"}:
            continue
        with pefile.PE(str(path), fast_load=True) as binary:
            binary.parse_data_directories(directories=[1, 13])
            for entry in [
                *getattr(binary, "DIRECTORY_ENTRY_IMPORT", []),
                *getattr(binary, "DIRECTORY_ENTRY_DELAY_IMPORT", []),
            ]:
                name = entry.dll.decode("ascii").lower()
                imports.setdefault(name, []).append(path.relative_to(root).as_posix())
    dependencies = {}
    for name, consumers in sorted(imports.items()):
        dependencies[name] = {
            "consumers": sorted(set(consumers)),
            "bundled_filename_present": name in bundled,
            "host_system32_present": (system / name).is_file(),
            "windows_api_contract": name.startswith(("api-ms-", "ext-ms-")),
        }
    report = {
        "scope": "Static PE imports for bin/lib/share; not a loader or clean-machine test",
        "limitations": [
            "Filename presence does not prove DLL search-path resolution",
            "Dynamic LoadLibrary imports and license obligations require separate review",
            "System32 presence on developer host does not prove redistributability",
        ],
        "total_bytes": sum(entry["bytes"] for entry in inventory),
        "files": inventory,
        "dependencies": dependencies,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    external = [
        name
        for name, info in dependencies.items()
        if not info["bundled_filename_present"] and not info["windows_api_contract"]
    ]
    print(
        json.dumps(
            {"files": len(files), "total_bytes": report["total_bytes"], "external_imports": external},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
