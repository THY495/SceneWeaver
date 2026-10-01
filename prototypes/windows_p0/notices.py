"""Include dependency license files and provenance; not a complete release license audit."""

import json
import platform
import shutil
import sys
from importlib.metadata import distribution
from pathlib import Path

output = Path(sys.argv[1]) / "notices"
output.mkdir(parents=True, exist_ok=True)
manifest = {"python": platform.python_version(), "platform": platform.platform(), "dependencies": {}}
for name in [
    "pydantic",
    "pydantic-core",
    "annotated-types",
    "typing-extensions",
    "typing-inspection",
    "pyinstaller",
]:
    package = distribution(name)
    copied = []
    for file in package.files or []:
        if "license" in str(file).lower() or Path(file).name.lower().startswith("copying"):
            target = output / name / Path(file).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package.locate_file(file), target)
            copied.append(str(target.relative_to(output)))
    manifest["dependencies"][name] = {"version": package.version, "license_files": copied}
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copyfile(python_license, output / "Python-LICENSE.txt")
(output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
(output / "README.txt").write_text(
    "P0 packaging probe only. Uses bundled CPython/SQLite/Tcl/Tk and pydantic. "
    "PyInstaller is a build tool. Collected notices are preliminary; audit bundled "
    "DLLs, Tcl/Tk, Python distribution and application licensing before public distribution. "
    "This artifact is not the finished Sceneweaver engine. No API credentials are bundled.\n",
    encoding="utf-8",
)
