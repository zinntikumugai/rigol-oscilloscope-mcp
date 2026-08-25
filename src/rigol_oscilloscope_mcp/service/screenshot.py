"""画面キャプチャの取得・形式変換・保存(tools.md 5章 capture_screenshot)。

機器はPNGを返す(MHO98: `:DISPlay:DATA?`)。保存形式がPNGならバイト列を
そのまま書き出し、再エンコードによる劣化と処理時間を避ける。それ以外の形式は
Pillowで変換する(JPEG/BMPはRGBA・パレットを扱えないためRGBへ変換する)。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from ..config import Config
from ..driver.scope import ScopeDriver
from ..errors import ErrorCode, ScopeError
from .paths import resolve_write_path

SUPPORTED_FORMATS = ("png", "jpg", "jpeg", "bmp", "webp")

DEFAULT_FORMAT = "png"
DEFAULT_STEM_PREFIX = "scope_"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

# "jpg" は "jpeg" に正規化する(返却する format / mime を一意にするため)
_CANONICAL = {
    "png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "bmp": "bmp",
    "webp": "webp",
}
_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "bmp": "image/bmp",
    "webp": "image/webp",
}
_PILLOW_FORMAT = {"jpeg": "JPEG", "bmp": "BMP", "webp": "WEBP"}


@dataclass(frozen=True)
class ScreenshotResult:
    """保存済みスクリーンショット。`image_bytes` は保存ファイルと同一内容。"""

    saved_path: str
    format: str
    size_bytes: int
    mime: str
    image_bytes: bytes


def _requested_format(path: str | None, format: str | None) -> str:
    """形式決定: format引数 > pathの拡張子 > png(未対応形式はエラー)。"""
    if format is not None:
        requested = format
    elif path is not None and (suffix := Path(path).suffix):
        requested = suffix[1:]
    else:
        return DEFAULT_FORMAT

    normalized = requested.strip().lower().lstrip(".")
    if normalized not in SUPPORTED_FORMATS:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"未対応の画像形式です: {requested}",
            {"format": requested, "supported": list(SUPPORTED_FORMATS)},
        )
    return normalized


def _convert(png_bytes: bytes, canonical: str) -> bytes:
    """機器のPNGを指定形式へ変換する(アルファ・パレットはRGBへ落とす)。"""
    buffer = io.BytesIO()
    with Image.open(io.BytesIO(png_bytes)) as image:
        image.convert("RGB").save(buffer, format=_PILLOW_FORMAT[canonical])
    return buffer.getvalue()


def capture_screenshot(
    driver: ScopeDriver,
    config: Config,
    path: str | None = None,
    format: str | None = None,
) -> ScreenshotResult:
    """現在の画面を取得し、許可ルート内の保存先へ書き出す。

    `path` はディレクトリでもファイルでもよく、省略時はデフォルト保存先の
    `scope_YYYYmmdd_HHMMSS.{形式}` になる。保存先の検証は
    `service.paths.resolve_write_path`(許可ルート外は INVALID_PARAMETER)。
    """
    requested = _requested_format(path, format)
    canonical = _CANONICAL[requested]

    stem = DEFAULT_STEM_PREFIX + datetime.now().strftime(TIMESTAMP_FORMAT)
    target = resolve_write_path(path, config, stem, requested)

    png_bytes = driver.capture_screenshot_bytes()
    data = png_bytes if canonical == "png" else _convert(png_bytes, canonical)

    target.write_bytes(data)

    return ScreenshotResult(
        saved_path=str(target),
        format=canonical,
        size_bytes=len(data),
        mime=_MIME[canonical],
        image_bytes=data,
    )
