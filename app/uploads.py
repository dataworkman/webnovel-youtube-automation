from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile


def upload_suffix(filename: str | None, allowed: set[str], default: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not suffix:
        return default
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported file type {suffix}. Allowed: {', '.join(sorted(allowed))}")
    return suffix


def save_upload(upload: UploadFile, destination: Path, max_bytes: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with destination.open("wb") as fh:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(413, f"파일이 너무 큽니다 (최대 {max_bytes // (1024 * 1024)}MB).")
                fh.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
