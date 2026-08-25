"""IPガード: 実機検証環境の情報をリポジトリに残さないため。

git追跡ファイルの中に検証用LANのアドレスプレフィックスが混入していないことを検査する。
実機のIPやVISAリソース文字列をコミットしてしまう事故を機械的に防ぐのが目的。
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 実機検証環境のアドレスプレフィックス(完全なIPではないためテストコードに直書きしてよい)
FORBIDDEN_PATTERN = re.compile(r"172\.16\.")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPO_ROOT / name
        for name in result.stdout.decode("utf-8").split("\0")
        if name
    ]


def test_git_ls_files_returns_something() -> None:
    """ガード自体が空振りしていないことの確認。"""
    assert tracked_files()


def test_no_verification_lan_address_in_tracked_files() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data:  # バイナリは対象外
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN_PATTERN.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")

    assert not offenders, (
        "実機検証環境のアドレスがコミット対象に含まれています: " + ", ".join(offenders)
    )


def test_guard_detects_a_planted_address(tmp_path: Path) -> None:
    """パターン自体が機能していることの自己検査。"""
    planted = "172" + ".16" + ".0.1"
    assert FORBIDDEN_PATTERN.search(planted)


def test_guard_does_not_match_similar_addresses() -> None:
    assert not FORBIDDEN_PATTERN.search("192.168.1.120")
    assert not FORBIDDEN_PATTERN.search("17216.0.1")


def test_markers_are_registered(pytestconfig: pytest.Config) -> None:
    """conftestのskip機構が依存するマーカー登録の確認。

    マーカー名をパラメータ化するとテストIDがキーワードに載り、conftestの
    skip判定に巻き込まれるため、直接列挙する。
    """
    registered = {line.split(":")[0] for line in pytestconfig.getini("markers")}
    assert {"device", "device_write"} <= registered
