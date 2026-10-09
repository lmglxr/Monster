from __future__ import annotations

import hashlib
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
MODEL_STORAGE_DIRECTORY = APP_DIR / "assets" / "easyocr_models"

# Values published by EasyOCR for the English detector and recognizer used by
# this project. Keeping verified copies in the game folder avoids relying on
# GitHub Releases during a first run on a new computer.
MODEL_CHECKSUMS = {
    "craft_mlt_25k.pth": "2f8227d2def4037cdb3b34389dcf9ec1",
    "english_g2.pth": "5864788e1821be9e454ec108d61b887d",
}


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_bundled_models() -> list[str]:
    """Return human-readable validation failures for the shipped OCR models."""
    failures: list[str] = []
    for filename, expected_checksum in MODEL_CHECKSUMS.items():
        path = MODEL_STORAGE_DIRECTORY / filename
        if not path.is_file():
            failures.append(f"缺少 {path.relative_to(APP_DIR)}")
            continue
        if _md5(path) != expected_checksum:
            failures.append(f"{path.relative_to(APP_DIR)} 校验失败，请重新获取完整项目文件")
    return failures


def create_english_reader(*, verbose: bool):
    """Create the project's reader without any runtime model download."""
    failures = validate_bundled_models()
    if failures:
        raise RuntimeError("；".join(failures))

    import easyocr

    return easyocr.Reader(
        ["en"],
        gpu=False,
        verbose=verbose,
        model_storage_directory=str(MODEL_STORAGE_DIRECTORY),
        download_enabled=False,
    )
