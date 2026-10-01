"""Build with DLL search paths belonging to the selected Python interpreter."""

import os
import sys
from pathlib import Path

import PyInstaller.__main__


def main():
    arguments = sys.argv[1:]
    # A venv made from Conda still loads extension modules from base_prefix.
    # PyInstaller otherwise misses their dependencies under Library/bin.
    library = Path(sys.base_prefix) / "Library" / "bin"
    dll_handle = None
    original_path = os.environ.get("PATH", "")
    try:
        if library.is_dir():
            os.environ["PATH"] = str(library) + os.pathsep + original_path
            dll_handle = os.add_dll_directory(str(library))
            for name in ("ffi.dll", "libmpdec-4.dll"):
                candidate = library / name
                if candidate.is_file():
                    arguments = ["--add-binary", f"{candidate};.", *arguments]
        PyInstaller.__main__.run(arguments)
    finally:
        os.environ["PATH"] = original_path
        if dll_handle:
            dll_handle.close()


if __name__ == "__main__":
    main()
