"""service/screenshot.py のテスト(tools.md 5章 capture_screenshot)。

形式決定(format引数 > 拡張子 > png)、PNGの無再エンコード保存、
Pillowでの形式変換、保存先の許可ルート検証との連携を確認する。
保存は全て tmp_path 配下に閉じる。
"""

import io
from pathlib import Path

import pytest
from PIL import Image

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.service.screenshot import (
    SUPPORTED_FORMATS,
    capture_screenshot,
)
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport
from rigol_oscilloscope_mcp.testing.fake_scope import SCREENSHOT_SIZE

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_SOI = b"\xff\xd8"


@pytest.fixture
def driver() -> ScopeDriver:
    transport = FakeTransport(FakeScope())
    transport.open()
    return ScopeDriver(ScpiSession(transport), load_profile("mho98"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def config(root: Path) -> Config:
    return Config(screenshot_dir=root, allowed_dirs=(root,))


# --------------------------------------------------------------------------
# PNG(無再エンコード)
# --------------------------------------------------------------------------


def test_png_is_saved_without_reencoding(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    device_bytes = driver.capture_screenshot_bytes()

    result = capture_screenshot(driver, config)

    saved = Path(result.saved_path)
    assert saved.is_absolute()
    assert saved.parent == root
    assert saved.read_bytes() == device_bytes  # 機器の生バイトそのまま
    assert result.image_bytes == device_bytes
    assert device_bytes.startswith(PNG_MAGIC)
    assert result.format == "png"
    assert result.mime == "image/png"
    assert result.size_bytes == len(device_bytes)


def test_default_name_uses_scope_prefix_and_extension(
    driver: ScopeDriver, config: Config
) -> None:
    result = capture_screenshot(driver, config)

    name = Path(result.saved_path).name
    assert name.startswith("scope_")
    assert name.endswith(".png")


def test_default_name_extension_follows_format(
    driver: ScopeDriver, config: Config
) -> None:
    result = capture_screenshot(driver, config, format="jpg")

    assert Path(result.saved_path).name.startswith("scope_")
    assert Path(result.saved_path).name.endswith(".jpg")


def test_no_extension_and_no_format_defaults_to_png(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    result = capture_screenshot(driver, config, path=str(root / "capture"))

    assert result.saved_path == str(root / "capture.png")
    assert result.format == "png"
    assert Path(result.saved_path).read_bytes().startswith(PNG_MAGIC)


# --------------------------------------------------------------------------
# 形式変換
# --------------------------------------------------------------------------


def test_jpg_is_converted(driver: ScopeDriver, config: Config, root: Path) -> None:
    result = capture_screenshot(driver, config, path=str(root / "capture.jpg"))

    data = Path(result.saved_path).read_bytes()
    assert data.startswith(JPEG_SOI)
    assert data == result.image_bytes
    assert result.format == "jpeg"
    assert result.mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert image.size == SCREENSHOT_SIZE


@pytest.mark.parametrize(
    ("extension", "pillow_format", "mime"),
    [
        ("bmp", "BMP", "image/bmp"),
        ("webp", "WEBP", "image/webp"),
    ],
)
def test_other_formats_are_converted(
    driver: ScopeDriver,
    config: Config,
    root: Path,
    extension: str,
    pillow_format: str,
    mime: str,
) -> None:
    result = capture_screenshot(driver, config, path=str(root / f"capture.{extension}"))

    assert result.format == extension
    assert result.mime == mime
    data = Path(result.saved_path).read_bytes()
    assert data == result.image_bytes
    assert result.size_bytes == len(data)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == pillow_format
        assert image.size == SCREENSHOT_SIZE


def test_jpeg_extension_is_accepted(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    result = capture_screenshot(driver, config, path=str(root / "capture.jpeg"))

    assert result.format == "jpeg"
    assert Path(result.saved_path).read_bytes().startswith(JPEG_SOI)


def test_extension_case_is_ignored(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    result = capture_screenshot(driver, config, path=str(root / "capture.JPG"))

    assert result.format == "jpeg"
    assert Path(result.saved_path).read_bytes().startswith(JPEG_SOI)


# --------------------------------------------------------------------------
# 形式の優先順位と検証
# --------------------------------------------------------------------------


def test_format_argument_overrides_path_extension(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    result = capture_screenshot(
        driver, config, path=str(root / "capture.png"), format="jpg"
    )

    assert result.format == "jpeg"
    assert Path(result.saved_path).read_bytes().startswith(JPEG_SOI)


def test_unsupported_format_is_rejected(driver: ScopeDriver, config: Config) -> None:
    with pytest.raises(ScopeError) as excinfo:
        capture_screenshot(driver, config, format="gif")

    error = excinfo.value
    assert error.code == ErrorCode.INVALID_PARAMETER
    assert error.detail["format"] == "gif"
    assert list(SUPPORTED_FORMATS) == error.detail["supported"]


def test_unsupported_extension_is_rejected(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        capture_screenshot(driver, config, path=str(root / "capture.gif"))

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_path_outside_allowed_roots_is_rejected(
    driver: ScopeDriver, config: Config
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        capture_screenshot(driver, config, path="/etc/rigol-capture.png")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_missing_parent_directory_is_created(
    driver: ScopeDriver, config: Config, root: Path
) -> None:
    result = capture_screenshot(driver, config, path=str(root / "a" / "b" / "shot.png"))

    assert Path(result.saved_path) == root / "a" / "b" / "shot.png"
    assert Path(result.saved_path).is_file()
