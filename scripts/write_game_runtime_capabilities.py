"""Record image limits compiled into a freshly built GameBackend runtime."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chatbot.presentation import MAX_SPRITE_BYTES
from chatbot.story_packages import MAX_ENCODED_PNG


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not (args.output.parent / "GameBackend.exe").is_file():
        raise FileNotFoundError("GameBackend.exe must be built before recording capabilities")
    value = {
        "format": "sceneweaver.game-runtime-capabilities.v1",
        "max_png_bytes": MAX_SPRITE_BYTES,
        "max_encoded_png_chars": MAX_ENCODED_PNG,
    }
    args.output.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
