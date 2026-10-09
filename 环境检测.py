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
OCR_REQUIREMENTS = APP_DIR / "requirements-ocr.txt"
INSTALLER = APP_DIR / "安装依赖.py"
VERSION_FILE = APP_DIR / "version.json"


def read_version() -> dict:
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "unknown", "python": ["3.10", "3.11", "3.12"]}


def required_packages(requirements: Path) -> list[tuple[str, str, str | None]]:
    packages: list[tuple[str, str, str | None]] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        name, separator, expected_version = line.partition("==")
        name = name.strip()
        expected_version = expected_version.strip() if separator else None
        import_name = {
            "opencv-python-headless": "cv2",
            "Pillow": "PIL",
            "pywin32": "win32api",
            "scikit-image": "skimage",
        }.get(name, name.replace("-", "_"))
        packages.append((name, import_name, expected_version))
    return packages


def check_python(version_info: dict) -> bool:
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    allowed = set(version_info.get("python", []))
    if current not in allowed:
        print(f"[失败] Python {current}，需要 Python {', '.join(sorted(allowed))}。")
        return False
    print(f"[通过] Python {current} ({sys.executable})")
    return True


def check_packages(requirements: Path, label: str = "") -> bool:
    success = True
    for distribution, import_name, expected_version in required_packages(requirements):
        try:
            installed = metadata.version(distribution)
            if expected_version and installed != expected_version:
                raise RuntimeError(f"版本为 {installed}，需要 {expected_version}")
            importlib.import_module(import_name)
            print(f"[通过] {label}{distribution} {installed}")
        except Exception as exc:
            print(f"[失败] {label}{distribution}: {exc}")
            success = False
    return success


def check_dependency_health() -> bool:
    """Use pip's metadata checker to catch transitive dependency conflicts."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check", "--disable-pip-version-check"],
            cwd=APP_DIR,
            check=False,
        )
    except OSError as exc:
        print(f"[失败] 无法运行 pip 依赖检查：{exc}")
        return False
    if result.returncode:
        print("[失败] pip 检测到不兼容的依赖。")
        return False
    print("[通过] pip 依赖关系")
    return True


def install_requirements() -> bool:
    print("[开始] 检查到依赖缺失，正在安装纯视觉版（含 EasyOCR）依赖……")
    try:
        result = subprocess.run([sys.executable, str(INSTALLER)], cwd=APP_DIR, check=False)
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
        "assets/inventory_panel_title.png",
        "assets/portal.png",
        "assets/dark_shark_boss.png",
        "assets/weapon_variant_a_icon.png",
        "assets/weapon_variant_b_icon.png",
    ]
    missing = [path for path in required if not (APP_DIR / path).is_file()]
    if missing:
        print("[失败] 缺少项目文件：" + ", ".join(missing))
        return False
    print(f"[通过] 项目文件和纯视觉模板资源 ({len(required)} 项)")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Curious Beast 纯视觉版环境预检")
    parser.add_argument("--download-model", action="store_true", help="提前下载并验证 EasyOCR 模型")
    parser.add_argument(
        "--install-missing",
        action="store_true",
        help="发现依赖缺失时自动安装 requirements.txt 与 requirements-ocr.txt",
    )
    args = parser.parse_args()

    version_info = read_version()
    print(f"Curious Beast 纯视觉环境检测 v{version_info.get('version', 'unknown')}")
    print("=" * 58)
    python_ok = check_python(version_info)
    packages_ok = check_packages(REQUIREMENTS)
    ocr_packages_ok = check_packages(OCR_REQUIREMENTS, "OCR ")
    dependencies_ok = check_dependency_health()
    if args.install_missing and python_ok and (
        not packages_ok or not ocr_packages_ok or not dependencies_ok
    ):
        installed = install_requirements()
        packages_ok = installed and check_packages(REQUIREMENTS)
        ocr_packages_ok = installed and check_packages(OCR_REQUIREMENTS, "OCR ")
        dependencies_ok = installed and check_dependency_health()
    checks = [
        python_ok,
        packages_ok,
        ocr_packages_ok,
        dependencies_ok,
        check_project_files(),
        prepare_ocr_model(args.download_model),
    ]
    print("=" * 58)
    if all(checks):
        print("环境检测通过，可以开始纯视觉完整链路冒烟测试。")
        return 0
    print("环境检测未通过，请根据上面的失败项修复后再运行冒烟测试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
