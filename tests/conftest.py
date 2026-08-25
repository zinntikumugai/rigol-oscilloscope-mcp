"""pytest共通設定。

実機マーカー(device / device_write)の一括skip判定を行う。
"""

import sys

# mise外で実行された場合のバックストップ(mise.toml の PYTHONDONTWRITEBYTECODE と同目的)
sys.dont_write_bytecode = True

import os  # noqa: E402

import pytest  # noqa: E402


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """実機が使えない環境では device / device_write テストをskipする。"""
    has_address = bool(os.environ.get("RIGOL_TEST_ADDRESS"))
    allow_write = os.environ.get("RIGOL_TEST_ALLOW_WRITE") == "1"

    skip_device = pytest.mark.skip(
        reason="RIGOL_TEST_ADDRESS が未設定のため実機テストをスキップ"
    )
    skip_device_write = pytest.mark.skip(
        reason="RIGOL_TEST_ALLOW_WRITE=1 でないため実機書き込みテストをスキップ"
    )

    for item in items:
        if "device" in item.keywords and not has_address:
            item.add_marker(skip_device)
        if "device_write" in item.keywords and not allow_write:
            item.add_marker(skip_device_write)
