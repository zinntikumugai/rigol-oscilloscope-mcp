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
# ミリ秒まで含める(同一秒内の連続撮影でデフォルト名が衝突しないように)
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S_%f"
_MISMATCH_HINT = (
    "formatとpathの拡張子を一致させるか、どちらか一方のみ指定してください"
)

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


def _normalized(requested: str) -> str:
    """`SUPPORTED_FORMATS` のいずれかへ正規化する(未対応はエラー)。"""
    normalized = requested.strip().lower().lstrip(".")
    if normalized not in SUPPORTED_FORMATS:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"未対応の画像形式です: {requested}",
            {"format": requested, "supported": list(SUPPORTED_FORMATS)},
        )
    return normalized


def _requested_format(path: str | None, format: str | None) -> str:
    """形式決定: format引数 > pathの拡張子 > png。

    未対応形式はエラー。format と path の拡張子が両方あって食い違う場合も
    エラーにする(拡張子と中身が一致しないファイルを作らないため)。
    区切り文字終わりのディレクトリ指定は拡張子を持たないものとして扱う。
    """
    suffix = "" if path is None or path.endswith(("/", "\\")) else Path(path).suffix
    if format is None:
        return _normalized(suffix) if suffix else DEFAULT_FORMAT

    normalized = _normalized(format)
    if suffix and _CANONICAL[_normalized(suffix)] != _CANONICAL[normalized]:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"format と path の拡張子が一致しません: {format} / {suffix}",
            {
                "format": format,
                "path_extension": suffix.lstrip("."),
                "hint": _MISMATCH_HINT,
            },
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
    `scope_YYYYmmdd_HHMMSS_mmm.{形式}`(mmm はミリ秒)になる。保存先の検証は
    `service.paths.resolve_write_path`(許可ルート外は INVALID_PARAMETER)。
    """
    requested = _requested_format(path, format)
    canonical = _CANONICAL[requested]

    # %f はマイクロ秒6桁なので末尾3桁を落としてミリ秒にする
    stem = DEFAULT_STEM_PREFIX + datetime.now().strftime(TIMESTAMP_FORMAT)[:-3]
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
