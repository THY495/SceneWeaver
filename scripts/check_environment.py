"""Reject leaked developer configuration or an unrelated editable installation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def inspect_environment(root: Path, package: Path, environ: dict[str, str]) -> dict:
    root, package = root.resolve(), package.resolve()
    errors = []
    if not package.is_relative_to(root / "src" / "chatbot"):
        errors.append("chatbot import comes from another checkout")
    if (root / ".env").exists():
        errors.append("local .env must be absent for source CI")
    configured = sorted(name for name, value in environ.items() if name.startswith("CHAT_") and value)
    if configured:
        errors.append("application environment overrides must be absent for source CI")
    if environ.get("PYTHONPATH"):
        errors.append("PYTHONPATH must be absent for source CI")
    return {
        "python": sys.version.split()[0],
        "package_inside_checkout": package.is_relative_to(root / "src" / "chatbot"),
        "application_override_names": configured,
        "errors": errors,
    }


def main() -> int:
    import chatbot

    report = inspect_environment(
        Path(__file__).resolve().parents[1], Path(chatbot.__file__), dict(os.environ)
    )
    print(json.dumps(report, ensure_ascii=False))
    return bool(report["errors"])


if __name__ == "__main__":
    raise SystemExit(main())
