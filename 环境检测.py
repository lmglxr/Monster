from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REQUIREMENTS = APP_DIR / "requirements.txt"
VERSION_FILE = APP_DIR / "version.json"


def read_version() -> dict:
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "unknown", "python": ["3.10", "3.11", "3.12"]}


def required_packages() -> list[tuple[str, str]]:
    packages: list[tuple[str, str]] = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
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


def check_packages() -> bool:
    success = True
    for distribution, import_name in required_packages():
        try:
            installed = metadata.version(distribution)
            importlib.import_module(import_name)
            print(f"[通过] {distribution} {installed}")
        except Exception as exc:
            print(f"[失败] {distribution}: {exc}")
            success = False
    return success


def install_requirements() -> bool:
    print("[开始] 检查到依赖缺失，正在自动下载/修复 requirements.txt 中的模块……")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--index-url",
        "https://pypi.org/simple",
        "-r",
        str(REQUIREMENTS),
    ]
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
            modules = session.enumerate_modules()
            mono_found = any(
                module.name.lower() == "mono-2.0-bdwgc.dll" for module in modules
            )
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
    packages_ok = check_packages()
    if not packages_ok and args.install_missing and python_ok:
        packages_ok = install_requirements() and check_packages()
    checks = [
        python_ok,
        packages_ok,
        check_project_files(),
        prepare_ocr_model(args.download_model),
        check_frida_attach(args.check_game_attach),
    ]
    print("=" * 58)
    if all(checks):
        print("环境检测通过，可以开始完整链路冒烟测试。")
        return 0
    print("环境检测未通过，请根据上面的失败项修复后再运行冒烟测试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
