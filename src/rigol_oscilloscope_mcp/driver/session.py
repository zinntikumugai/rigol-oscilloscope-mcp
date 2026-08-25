"""SCPIセッション(エラーキュー管理と設定検証)。

Requirements.md 7.1 の2規範をここに閉じ込める:

- **接続時drain**: エラーキューは前セッションの残留で汚染されうる
- **set後検証**: 送信成功≠処理成功。設定系は send → エラーキュー確認 → read-back

上位のドライバは生のSCPI文字列を組み立て、本層に「検証付きで送る」ことだけを
委ねる。どのコマンドで失敗したかを追跡できるよう、エラーには常に送信した
コマンドを添える。
"""

from __future__ import annotations

import logging

from ..errors import ErrorCode, ScopeError
from ..transport import Transport

ERROR_QUERY = ":SYSTem:ERRor?"

# SCPI送受信のDEBUGログ(Requirements.md 8.3)。ここが唯一の送受信経路なので、
# 上位のドライバ・サービス層にログ呼び出しをばらまかずに済む。
logger = logging.getLogger(__name__)


def _is_no_error(response: str) -> bool:
    """エラー番号が0か判定する(`+0,"No error"` と符号付きで返す機種がある)。

    番号として解釈できない応答は、保守的に「エラーあり」として扱う。
    """
    try:
        return int(response.split(",", 1)[0].strip()) == 0
    except ValueError:
        return False


class ScpiSession:
    """1台のオシロスコープに対するSCPIの送受信セッション。"""

    def __init__(self, transport: Transport, timeout_s: float = 5.0) -> None:
        self.transport = transport
        self.timeout_s = timeout_s

    # -- 素通し -----------------------------------------------------------

    def write(self, command: str) -> None:
        """書き込む(エラーキューは確認しない)。"""
        logger.debug("-> %s", command)
        self.transport.write(command)

    def query(self, command: str) -> str:
        """問い合わせて応答文字列を返す(エラーキューは確認しない)。"""
        logger.debug("-> %s", command)
        response = self.transport.query(command, self.timeout_s)
        logger.debug("<- %s", response.strip())
        return response

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes:
        """バイナリブロック応答を返す。スクリーンショット等は長めの猶予を渡す。"""
        logger.debug("-> %s", command)
        payload = self.transport.query_binary(
            command, self.timeout_s if timeout_s is None else timeout_s
        )
        # 波形・画面キャプチャは数十KB〜。ログには載せず長さだけ残す。
        logger.debug("<- <binary %d bytes>", len(payload))
        return payload

    # -- エラーキュー -----------------------------------------------------

    def drain_error_queue(self, max_iter: int = 20) -> list[str]:
        """エラーキューを空になるまで読み捨て、捨てたエラーを返す。

        `0,"No error"` を読むまで繰り返す。max_iter を超えても空にならない場合は
        機器が異常(あるいはエラーが増え続けている)とみなして SCPI_ERROR。
        """
        drained: list[str] = []
        for _ in range(max_iter):
            response = self.query(ERROR_QUERY)
            if _is_no_error(response):
                return drained
            drained.append(response.strip())
        raise ScopeError(
            ErrorCode.SCPI_ERROR,
            f"error queue is still not empty after {max_iter} reads",
            {"max_iter": max_iter, "drained": drained},
        )

    def check_error(self, command: str) -> None:
        """直前のコマンドがエラーを積んでいないか1回だけ確認する。"""
        response = self.query(ERROR_QUERY)
        if _is_no_error(response):
            return
        raise ScopeError(
            ErrorCode.SCPI_ERROR,
            f"the device did not accept the command: {command} ({response.strip()})",
            {"command": command, "scpi_error": response.strip()},
        )

    # -- 検証付き操作 -----------------------------------------------------

    def query_checked(self, command: str) -> str:
        """問い合わせ、その後エラーキューを確認して応答を返す。"""
        response = self.query(command)
        self.check_error(command)
        return response

    def write_checked(self, command: str) -> None:
        """書き込み、その後エラーキューを確認する。

        実機は不正な書き込みに無応答で応じる(タイムアウトしない)ため、
        受理されたかどうかはエラーキューでしか分からない。
        """
        self.write(command)
        self.check_error(command)

    def set_and_verify(self, set_cmd: str, readback_query: str) -> str:
        """設定 → エラーキュー確認 → read-back(Requirements.md 7.1 / 7.3)。

        read-back の生応答を返す。値の解釈は呼び出し側(ドライバ)の責務。
        """
        self.write_checked(set_cmd)
        return self.query(readback_query)
