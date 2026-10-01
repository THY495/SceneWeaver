"""Distribution entry: a missing game manifest never falls back to Studio."""

import ctypes
import sys

from desktop_host import main
from launch_failure import record_smoke_failure

if __name__ == "__main__":
    try:
        main(require_game=True)
    except Exception as exc:
        if record_smoke_failure(sys.argv[1:], exc):
            raise SystemExit(1) from None
        ctypes.windll.user32.MessageBoxW(None, str(exc), "游戏启动失败", 0x10)
        raise SystemExit(1)
