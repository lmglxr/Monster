from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import threading
from importlib import metadata
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REQUIREMENTS = APP_DIR / "requirements.txt"
OCR_REQUIREMENTS = APP_DIR / "requirements-ocr.txt"
INSTALLER = APP_DIR / "安装依赖.py"
VERSION_FILE = APP_DIR / "version.json"


def read_version() -> dict:
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "unknown", "python": ["3.10", "3.11", "3.12"]}


def required_packages(requirements: Path = REQUIREMENTS) -> list[tuple[str, str]]:
    packages: list[tuple[str, str]] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==", 1)[0].strip()
        import_name = {
            "opencv-python-headless": "cv2",
            "Pillow": "PIL",
            "pywin32": "win32api",
        }.get(name, name.replace("-", "_"))
        packages.append((name, import_name))
    return packages


def check_python(version_info: dict) -> bool:
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    allowed = set(version_info.get("python", []))
    if current not in allowed:
        print(f"[失败] Python {current}，需要 Python {', '.join(sorted(allowed))}。")
        return False
    print(f"[通过] Python {current} ({sys.executable})")
    return True


def check_packages(requirements: Path = REQUIREMENTS, label: str = "") -> bool:
    success = True
    for distribution, import_name in required_packages(requirements):
        try:
            installed = metadata.version(distribution)
            importlib.import_module(import_name)
            print(f"[通过] {label}{distribution} {installed}")
        except Exception as exc:
            print(f"[失败] {distribution}: {exc}")
            success = False
    return success


def install_requirements(ocr_enabled: bool) -> bool:
    print("[开始] 检查到依赖缺失，正在自动下载/修复 requirements.txt 中的模块……")
    command = [sys.executable, str(INSTALLER)]
    if ocr_enabled:
        command.append("--ocr")
    try:
        result = subprocess.run(command, cwd=APP_DIR, check=False)
    except OSError as exc:
        print(f"[失败] 无法启动 pip：{exc}")
        return False
    if result.returncode != 0:
        print(f"[失败] 依赖下载/安装失败，退出码 {result.returncode}。")
        return False
    print("[通过] 依赖下载/安装完成")
    return True


def check_project_files() -> bool:
    required = [
        "mvp_bot.py",
        "config.json",
        "smoke_test_config.json",
        "mono_runtime_probe.py",
        "assets/inventory_panel_title.png",
        "assets/portal.png",
        "assets/weapon_variant_a_icon.png",
        "assets/weapon_variant_b_icon.png",
    ]
    missing = [path for path in required if not (APP_DIR / path).is_file()]
    if missing:
        print("[失败] 缺少项目文件：" + ", ".join(missing))
        return False
    print(f"[通过] 项目文件和模板资源 ({len(required)} 项)")
    return True


def prepare_ocr_model(download_model: bool) -> bool:
    if not download_model:
        print("[跳过] EasyOCR 模型预下载（可用 --download-model 开启）")
        return True
    try:
        import easyocr

        print("[开始] 检查 EasyOCR 模型；首次运行可能需要下载约几分钟……")
        easyocr.Reader(["en"], gpu=False, verbose=False)
        print("[通过] EasyOCR 模型已准备好")
        return True
    except Exception as exc:
        print(f"[失败] EasyOCR 模型准备失败：{exc}")
        return False


def check_frida_attach(check_game: bool) -> bool:
    if not check_game:
        print("[跳过] Frida 游戏进程附加测试（可用 --check-game-attach 开启）")
        return True
    try:
        import frida

        device = frida.get_local_device()
        process = next(
            (
                item
                for item in device.enumerate_processes()
                if item.name.lower() in {"creaturecurios", "creaturecurios.exe"}
            ),
            None,
        )
        if process is None:
            print("[失败] 未找到 CreatureCurios.exe，请先启动游戏并进入角色画面。")
            return False

        session = device.attach(process.pid)
        try:
            result = {"mono_found": False}
            ready = threading.Event()

            def on_message(message: dict, _data) -> None:
                if message.get("type") == "send":
                    result["mono_found"] = bool(message.get("payload"))
                    ready.set()

            script = session.create_script(
                "send(Process.enumerateModules().some(" \
                "m => m.name.toLowerCase() === 'mono-2.0-bdwgc.dll'));"
            )
            script.on("message", on_message)
            script.load()
            ready.wait(timeout=3.0)
            script.unload()
            mono_found = result["mono_found"]
        finally:
            session.detach()

        if not mono_found:
            print("[失败] 已附加游戏，但没有找到 mono-2.0-bdwgc.dll。")
            return False
        print(f"[通过] Frida 可附加游戏进程 PID={process.pid}，Mono 模块已找到")
        return True
    except Exception as exc:
        print(f"[失败] Frida 游戏进程附加测试失败：{exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Curious Beast 冒烟测试环境预检")
    parser.add_argument("--download-model", action="store_true", help="提前下载并验证 EasyOCR 模型")
    parser.add_argument(
        "--check-game-attach",
        action="store_true",
        help="附加并立即分离游戏进程，验证 Frida 和 Mono 环境",
    )
    parser.add_argument(
        "--install-missing",
        action="store_true",
        help="发现依赖缺失时自动安装 requirements.txt",
    )
    args = parser.parse_args()

    version_info = read_version()
    print(f"Curious Beast 冒烟测试环境检测 v{version_info.get('version', 'unknown')}")
    print("=" * 58)
    python_ok = check_python(version_info)
    try:
        config = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}
    combat_cfg = config.get("combat", {})
    ocr_enabled = bool(combat_cfg.get("easyocr_enabled", False))
    print(f"EasyOCR 模式：{'启用' if ocr_enabled else '关闭（仅使用 Frida/日志）'}")
    packages_ok = check_packages()
    ocr_packages_ok = True
    if ocr_enabled:
        ocr_packages_ok = check_packages(OCR_REQUIREMENTS, "OCR ")
    if args.install_missing and python_ok and (not packages_ok or not ocr_packages_ok):
        packages_ok = install_requirements(ocr_enabled) and check_packages()
        ocr_packages_ok = True
        if ocr_enabled:
            ocr_packages_ok = check_packages(OCR_REQUIREMENTS, "OCR ")
    checks = [
        python_ok,
        packages_ok,
        check_project_files(),
        prepare_ocr_model(args.download_model and ocr_enabled),
        check_frida_attach(args.check_game_attach),
        ocr_packages_ok,
    ]
    print("=" * 58)
    if all(checks):
        print("环境检测通过，可以开始完整链路冒烟测试。")
        return 0
    print("环境检测未通过，请根据上面的失败项修复后再运行冒烟测试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
