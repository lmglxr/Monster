from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REQUIREMENTS = APP_DIR / "requirements.txt"
OCR_REQUIREMENTS = APP_DIR / "requirements-ocr.txt"


def repair_pywin32() -> bool:
    postinstall = Path(sys.executable).parent / "pywin32_postinstall.py"
    if not postinstall.is_file():
        return True
    result = subprocess.run(
        [sys.executable, str(postinstall), "-install"],
        cwd=APP_DIR,
        check=False,
    )
    return result.returncode == 0


def sources() -> list[str]:
    configured = os.environ.get("CURIOUS_BEAST_PIP_INDEX", "").strip()
    candidates = [
        configured,
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple/",
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://pypi.org/simple",
    ]
    result: list[str] = []
    for source in candidates:
        if source and source not in result:
            result.append(source)
    return result


def run_pip(index_url: str, requirements: Path, *extra: str) -> bool:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        *extra,
        "-r",
        str(requirements),
        "--disable-pip-version-check",
        "--index-url",
        index_url,
        "--timeout",
        "30",
        "--retries",
        "1",
    ]
    completed = subprocess.run(command, cwd=APP_DIR, check=False)
    return completed.returncode == 0


def main() -> int:
    print("正在准备纯视觉版 Python 依赖（含 EasyOCR）；网络较慢时会自动切换下载源。")
    for index_url in sources():
        print(f"尝试 Python 源：{index_url}")
        if not run_pip(index_url, REQUIREMENTS):
            print(f"当前源安装失败，将切换下一个源：{index_url}")
            continue
        # run_pip() already adds the `pip install` subcommand. Passing another
        # literal "install" here makes pip try to download a package named
        # `install`, causing every OCR dependency attempt to fail.
        if not run_pip(index_url, OCR_REQUIREMENTS):
            print(f"当前源安装 OCR 依赖失败，将切换下一个源：{index_url}")
            continue
        if not repair_pywin32():
            print("pywin32 后处理失败，将切换下一个源重试。")
            continue
        print(f"依赖安装完成，使用源：{index_url}")
        return 0
    print("所有 Python 下载源均失败。请检查网络，或设置 CURIOUS_BEAST_PIP_INDEX 后重试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
