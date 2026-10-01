"""Filesystem paths suitable for Windows APIs without changing machine policy."""

import os
from pathlib import Path


def filesystem_path(path: Path):
    absolute = str(path.resolve())
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)
