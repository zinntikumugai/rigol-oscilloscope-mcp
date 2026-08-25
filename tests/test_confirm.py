"""safety/confirm.py のテスト(Requirements.md 6.2 確認フロー)。"""

import threading

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.safety.confirm import ConfirmRequest, ConfirmTokenStore


class FakeClock:
    """monotonic互換の手動クロック。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(clock: FakeClock | None = None, ttl_s: float = 300.0) -> ConfirmTokenStore:
    if clock is None:
        return ConfirmTokenStore(ttl_s=ttl_s)
    return ConfirmTokenStore(ttl_s=ttl_s, clock=clock)


def _issue(store: ConfirmTokenStore, **kwargs) -> ConfirmRequest:
    params = {
        "tool": "autoset",
        "args": {"timeout_s": 10.0},
        # 本番(service/control.py)がLLMへ流す文言と同じく英語で揃える
        "description": "Run Auto Setup and automatically adjust all channel settings",
        "risk": "The current scale and trigger settings will be lost",
    }
    params.update(kwargs)
    return store.issue(**params)


def _reason(exc_info: pytest.ExceptionInfo[ScopeError]) -> str:
    err = exc_info.value
    assert err.code == ErrorCode.USER_CONFIRMATION_REQUIRED
    return err.detail["reason"]


# --- 発行 -----------------------------------------------------------------


def test_issue_returns_confirm_request_fields() -> None:
    store = _store()
    req = _issue(store)
    assert isinstance(req, ConfirmRequest)
    assert req.tool == "autoset"
    assert req.description.startswith("Run Auto Setup")
    assert req.risk
    assert req.expires_in_s == 300.0
    assert isinstance(req.token, str) and len(req.token) >= 16


def test_confirm_request_is_frozen() -> None:
    req = _issue(_store())
    with pytest.raises(Exception):
        req.token = "tampered"  # type: ignore[misc]


def test_instruction_requires_human_confirmation() -> None:
    """文言退行ガード: LLMへ人間への確認を必須と指示していること(6.2)。"""
    req = _issue(_store())
    assert "human" in req.instruction
    assert "ask" in req.instruction
    assert "consent" in req.instruction


def test_instruction_is_constant_across_issues() -> None:
    store = _store()
    assert _issue(store).instruction == _issue(store, tool="raw_scpi").instruction


def test_tokens_are_unique() -> None:
    store = _store()
    tokens = {_issue(store).token for _ in range(20)}
    assert len(tokens) == 20


def test_expires_in_s_reflects_ttl() -> None:
    assert _issue(_store(ttl_s=42.0)).expires_in_s == 42.0


# --- 正常系 ---------------------------------------------------------------


def test_issue_then_consume_succeeds() -> None:
    store = _store()
    req = _issue(store)
    assert store.consume(req.token, "autoset", {"timeout_s": 10.0}) is None


def test_consume_with_confirm_token_in_args_succeeds() -> None:
    """2回目の呼び出しは 同一引数 + confirm_token。トークン自身はバインド対象外。"""
    store = _store()
    req = _issue(store)
    store.consume(req.token, "autoset", {"timeout_s": 10.0, "confirm_token": req.token})


def test_issue_ignores_confirm_token_key_in_args() -> None:
    store = _store()
    req = _issue(store, args={"timeout_s": 10.0, "confirm_token": "stale"})
    store.consume(req.token, "autoset", {"timeout_s": 10.0})


def test_key_order_does_not_affect_binding() -> None:
    store = _store()
    req = _issue(store, args={"a": 1, "b": {"x": 1, "y": 2}})
    store.consume(req.token, "autoset", {"b": {"y": 2, "x": 1}, "a": 1})


def test_non_ascii_args_bind() -> None:
    store = _store()
    req = _issue(store, tool="raw_scpi", args={"command": ":系統:ERR?"})
    store.consume(req.token, "raw_scpi", {"command": ":系統:ERR?"})


def test_generation_match_succeeds() -> None:
    store = _store()
    req = _issue(store, generation=7)
    store.consume(req.token, "autoset", {"timeout_s": 10.0}, generation=7)


def test_multiple_tokens_are_independent() -> None:
    store = _store()
    first = _issue(store)
    second = _issue(store, tool="raw_scpi", args={"command": "*IDN?"})
    store.consume(second.token, "raw_scpi", {"command": "*IDN?"})
    store.consume(first.token, "autoset", {"timeout_s": 10.0})


def test_consume_just_before_expiry_succeeds() -> None:
    clock = FakeClock()
    store = _store(clock)
    req = _issue(store)
    clock.advance(299.0)
    store.consume(req.token, "autoset", {"timeout_s": 10.0})


# --- 異常系 ---------------------------------------------------------------


def test_unknown_token() -> None:
    store = _store()
    with pytest.raises(ScopeError) as exc_info:
        store.consume("not-a-token", "autoset", {"timeout_s": 10.0})
    assert _reason(exc_info) == "unknown_token"


def test_expired_token() -> None:
    clock = FakeClock()
    store = _store(clock)
    req = _issue(store)
    clock.advance(301.0)
    with pytest.raises(ScopeError) as exc_info:
        store.consume(req.token, "autoset", {"timeout_s": 10.0})
    assert _reason(exc_info) == "expired"


def test_args_mismatch() -> None:
    store = _store()
    req = _issue(store)
    with pytest.raises(ScopeError) as exc_info:
        store.consume(req.token, "autoset", {"timeout_s": 20.0})
    assert _reason(exc_info) == "args_mismatch"


def test_tool_mismatch() -> None:
    store = _store()
    req = _issue(store)
    with pytest.raises(ScopeError) as exc_info:
        store.consume(req.token, "raw_scpi", {"timeout_s": 10.0})
    assert _reason(exc_info) == "tool_mismatch"


def test_generation_mismatch() -> None:
    store = _store()
    req = _issue(store, generation=1)
    with pytest.raises(ScopeError) as exc_info:
        store.consume(req.token, "autoset", {"timeout_s": 10.0}, generation=2)
    assert _reason(exc_info) == "generation_mismatch"


def test_token_is_single_use() -> None:
    store = _store()
    req = _issue(store)
    store.consume(req.token, "autoset", {"timeout_s": 10.0})
    with pytest.raises(ScopeError) as exc_info:
        store.consume(req.token, "autoset", {"timeout_s": 10.0})
    assert _reason(exc_info) == "unknown_token"


@pytest.mark.parametrize(
    ("bad_tool", "bad_args", "generation"),
    [
        ("raw_scpi", {"timeout_s": 10.0}, 0),
        ("autoset", {"timeout_s": 99.0}, 0),
        ("autoset", {"timeout_s": 10.0}, 5),
    ],
)
def test_failed_consume_invalidates_token(
    bad_tool: str, bad_args: dict, generation: int
) -> None:
    """検証失敗でもトークンは消費(無効化)されること(総当たり防止)。"""
    store = _store()
    req = _issue(store)
    with pytest.raises(ScopeError):
        store.consume(req.token, bad_tool, bad_args, generation=generation)
    with pytest.raises(ScopeError) as exc_info:
        store.consume(req.token, "autoset", {"timeout_s": 10.0})
    assert _reason(exc_info) == "unknown_token"


def test_expired_entries_are_purged() -> None:
    """期限切れは掃除され、以後 unknown_token になること。"""
    clock = FakeClock()
    store = _store(clock)
    stale = _issue(store)
    clock.advance(301.0)
    _issue(store, args={"timeout_s": 1.0})  # 発行時に掃除が走る
    with pytest.raises(ScopeError) as exc_info:
        store.consume(stale.token, "autoset", {"timeout_s": 10.0})
    assert _reason(exc_info) == "unknown_token"


def test_error_detail_contains_tool() -> None:
    store = _store()
    with pytest.raises(ScopeError) as exc_info:
        store.consume("nope", "autoset", {})
    assert exc_info.value.detail.get("tool") == "autoset"


# --- 並行性 ---------------------------------------------------------------


def test_only_one_thread_consumes_a_token() -> None:
    """同一トークンへ複数スレッドが殺到しても成功は1回だけ。"""
    store = _store()
    req = _issue(store)
    successes: list[int] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            store.consume(req.token, "autoset", {"timeout_s": 10.0})
        except ScopeError:
            return
        with lock:
            successes.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1


def test_concurrent_issue_produces_unique_tokens() -> None:
    store = _store()
    tokens: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        token = _issue(store).token
        with lock:
            tokens.append(token)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(tokens) == 8
    assert len(set(tokens)) == 8
