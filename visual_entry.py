from __future__ import annotations

import runpy
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent


def run_python_script(name: str, args: list[str]) -> int:
    script = APP_DIR / name
    previous_argv = sys.argv[:]
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        sys.argv = previous_argv
    return 0


def prepare_environment(download_model: bool) -> int:
    args = ["--install-missing"]
    if download_model:
        args.insert(0, "--download-model")
    return run_python_script("环境检测.py", args)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "help"
    commands = {
        "run": (False, ["--live"]),
        "run-laptop": (False, ["--config-overrides", "laptop_config.json", "--live"]),
        "smoke": (True, ["--full-smoke-test", "--live", "--smoke-cycles", "2"]),
        "smoke-laptop": (
            True,
            [
                "--config-overrides",
                "laptop_config.json",
                "--full-smoke-test",
                "--live",
                "--smoke-cycles",
                "2",
            ],
        ),
    }
    if command == "help" or command not in commands:
        print("Usage: visual_entry.py [run|run-laptop|smoke|smoke-laptop]")
        return 0 if command == "help" else 2

    download_model, bot_args = commands[command]
    result = prepare_environment(download_model)
    if result:
        print("Environment check failed. The game was not started.")
        return result
    return run_python_script("mvp_bot.py", bot_args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
