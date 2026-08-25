"""safety/audit.py のテスト(Requirements.md 7.6 監査ログ)。"""

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.safety.audit import AuditLogger, token_digest

RECORD_KEYS = {
    "timestamp",
    "tool",
    "requested",
    "before",
    "after",
    "result",
    "detail",
}


def _lines(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


# --- record ---------------------------------------------------------------


def test_record_writes_single_parsable_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    logger.record(
        tool="configure_channel",
        requested={"channel": "CH1", "scale_v_per_div": 1.0},
        before={"scale_v_per_div": 0.5},
        after={"scale_v_per_div": 1.0},
        result="success",
    )
    records = _lines(path)
    assert len(records) == 1
    entry = records[0]
    assert set(entry) == RECORD_KEYS
    assert entry["tool"] == "configure_channel"
    assert entry["requested"] == {"channel": "CH1", "scale_v_per_div": 1.0}
    assert entry["before"] == {"scale_v_per_div": 0.5}
    assert entry["after"] == {"scale_v_per_div": 1.0}
    assert entry["result"] == "success"
    assert entry["detail"] is None


def test_timestamp_is_iso8601_utc(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLogger(path).record("run", {}, None, None, "success")
    stamp = _lines(path)[0]["timestamp"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_record_appends(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    for index in range(3):
        logger.record("run", {"index": index}, None, None, "success")
    records = _lines(path)
    assert [entry["requested"]["index"] for entry in records] == [0, 1, 2]


def test_record_with_detail(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLogger(path).record(
        "autoset", {}, None, None, "error", detail={"code": "TIMEOUT"}
    )
    assert _lines(path)[0]["detail"] == {"code": "TIMEOUT"}


def test_non_ascii_is_not_escaped(tmp_path: Path) -> None:
    """非ASCIIがそのまま保存される(ensure_ascii=False)ことの検証用に意図的な日本語。"""
    path = tmp_path / "audit.jsonl"
    AuditLogger(path).record("raw_scpi", {"note": "確認済み"}, None, None, "success")
    text = path.read_text(encoding="utf-8")
    assert "確認済み" in text
    assert "\\u" not in text


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "audit.jsonl"
    AuditLogger(path).record("run", {}, None, None, "success")
    assert path.is_file()
    assert len(_lines(path)) == 1


def test_existing_file_is_not_truncated(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLogger(path).record("run", {}, None, None, "success")
    AuditLogger(path).record("stop", {}, None, None, "success")
    assert [entry["tool"] for entry in _lines(path)] == ["run", "stop"]


def test_non_serializable_values_do_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLogger(path).record("connect", {"path": tmp_path}, None, None, "success")
    records = _lines(path)
    assert len(records) == 1
    assert isinstance(records[0]["requested"]["path"], str)


# --- 無効化(path=None) ---------------------------------------------------


def test_disabled_logger_writes_nothing(tmp_path: Path, capsys) -> None:
    logger = AuditLogger(None)
    logger.record("run", {}, None, None, "success")
    logger.record_confirm("issued", "autoset", "0123456789abcdef")
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err == ""


def test_disabled_logger_reports_enabled_false() -> None:
    assert AuditLogger(None).enabled is False


def test_enabled_logger_reports_enabled_true(tmp_path: Path) -> None:
    assert AuditLogger(tmp_path / "audit.jsonl").enabled is True


# --- 障害耐性 -------------------------------------------------------------


def test_write_failure_warns_and_does_not_raise(tmp_path: Path, capsys) -> None:
    """監査ログの失敗で操作自体を止めない(例外を伝播させない)。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")
    logger = AuditLogger(blocked / "audit.jsonl")
    logger.record("run", {}, None, None, "success")
    assert "audit" in capsys.readouterr().err.lower()


# --- record_confirm -------------------------------------------------------


@pytest.mark.parametrize("event", ["issued", "consumed", "rejected"])
def test_record_confirm_writes_event(tmp_path: Path, event: str) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLogger(path).record_confirm(event, "raw_scpi", "0123456789abcdef")
    entry = _lines(path)[0]
    assert entry["event"] == event
    assert entry["tool"] == "raw_scpi"
    assert entry["token_digest"] == "0123456789abcdef"
    assert "timestamp" in entry


def test_record_confirm_never_contains_raw_token(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    token = "s3cret-token-value"
    AuditLogger(path).record_confirm("issued", "autoset", token_digest(token))
    text = path.read_text(encoding="utf-8")
    assert token not in text


def test_token_digest_is_sha256_prefix() -> None:
    token = "s3cret-token-value"
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    digest = token_digest(token)
    assert digest == expected
    assert len(digest) == 16


def test_token_digest_differs_per_token() -> None:
    assert token_digest("a") != token_digest("b")


# --- 並行性 ---------------------------------------------------------------


def test_concurrent_records_do_not_interleave(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    per_thread = 100
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        for index in range(per_thread):
            logger.record(
                name, {"index": index, "pad": "確認" * 40}, None, None, "success"
            )

    threads = [
        threading.Thread(target=worker, args=("run",)),
        threading.Thread(target=worker, args=("stop",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = _lines(path)
    assert len(records) == 2 * per_thread
    assert all(set(entry) == RECORD_KEYS for entry in records)
    counts = {"run": 0, "stop": 0}
    for entry in records:
        counts[entry["tool"]] += 1
    assert counts == {"run": per_thread, "stop": per_thread}
