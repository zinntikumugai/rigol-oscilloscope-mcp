"""操作クラス分類(Requirements.md 6.1 / tools.md 10章)。

Tool名 → 基本操作クラスの静的表のみを扱う。引数に依存する昇格
(例: `configure_channel` の impedance="50" は RESTRICTED_WRITE)は
呼び出し側(control service)の責務とする。

本表は静的分類に加え、server.py のTool登録時検証(表に無いTool名は登録拒否、
承認必須クラスは `confirm_token` 引数必須)にも使う。
"""

from __future__ import annotations

from enum import StrEnum

from ..errors import ErrorCode, ScopeError


class OperationClass(StrEnum):
    """操作クラス(Requirements.md 6.1)。

    - READ_ONLY: 自動実行可
    - SAFE_WRITE: 原則自動実行可(パラメータ範囲検証必須)
    - RESTRICTED_WRITE: ユーザー承認(confirmトークン)必須
    - DANGEROUS_WRITE: ユーザーの明示確認なしで実行禁止
    """

    READ_ONLY = "READ_ONLY"
    SAFE_WRITE = "SAFE_WRITE"
    RESTRICTED_WRITE = "RESTRICTED_WRITE"
    DANGEROUS_WRITE = "DANGEROUS_WRITE"


#: Tool名 → 基本操作クラス(tools.md 10章のサマリ表)
TOOL_CLASSES: dict[str, OperationClass] = {
    # 1. 接続管理
    "connect": OperationClass.SAFE_WRITE,
    "disconnect": OperationClass.SAFE_WRITE,
    "scope_identify": OperationClass.READ_ONLY,
    "get_capabilities": OperationClass.READ_ONLY,
    # 2. 状態取得
    "get_state": OperationClass.READ_ONLY,
    "get_channel": OperationClass.READ_ONLY,
    "get_timebase": OperationClass.READ_ONLY,
    "get_trigger": OperationClass.READ_ONLY,
    "get_acquisition_state": OperationClass.READ_ONLY,
    # 3. 設定変更(configure_channel の impedance="50" のみ呼び出し側で昇格)
    "configure_channel": OperationClass.SAFE_WRITE,
    "configure_timebase": OperationClass.SAFE_WRITE,
    "configure_trigger": OperationClass.SAFE_WRITE,
    # 4. Acquisition
    "run": OperationClass.SAFE_WRITE,
    "stop": OperationClass.SAFE_WRITE,
    "single": OperationClass.SAFE_WRITE,
    "autoset": OperationClass.RESTRICTED_WRITE,
    # 5. 測定・データ取得
    "measure": OperationClass.READ_ONLY,
    # Resultビュー表示のみの変更で再測定により可逆(issue #16)
    "clear_measurements": OperationClass.SAFE_WRITE,
    # 測定の前提設定(Phase M4)。取り込み条件にも出力にも触れず、測定値の
    # 解釈のしかた(しきい値・測定区間・振幅算出方式・統計)だけを変えるため
    # configure_histogram / configure_cursor と同じ SAFE_WRITE。
    "configure_measurement": OperationClass.SAFE_WRITE,
    "get_measurement_statistics": OperationClass.READ_ONLY,
    "capture_waveform": OperationClass.READ_ONLY,
    "analyze_waveform": OperationClass.READ_ONLY,
    "capture_screenshot": OperationClass.READ_ONLY,
    # 6. プロトコルデコード(tools.md、Phase 4)
    # 表示・解析層のみを変える(取り込み設定も出力も変えない)完全に可逆な操作で、
    # configure_channel より侵襲性が低いため SAFE_WRITE。
    "configure_decode": OperationClass.SAFE_WRITE,
    "get_decode_result": OperationClass.READ_ONLY,
    # 7. 信号発生(tools.md、Phase 4)
    # configure_afg は設定のみで**出力状態には触れない**(信号は外へ出ない)ため
    # SAFE_WRITE。実際に信号を外へ出すのは enable_afg だけで、被測定回路への注入に
    # なるため DANGEROUS_WRITE(confirmトークン必須)。出力OFF(disable_afg)は
    # 常に安全側への操作で、緊急停止を承認でブロックしないため SAFE_WRITE。
    "configure_afg": OperationClass.SAFE_WRITE,
    "get_afg_state": OperationClass.READ_ONLY,
    "enable_afg": OperationClass.DANGEROUS_WRITE,
    "disable_afg": OperationClass.SAFE_WRITE,
    # プリセットの周波数・位相を再適用して両チャンネルの位相を揃えるだけの操作で、
    # 振幅・出力状態(信号が出るかどうか)には一切触れないため SAFE_WRITE。
    "sync_afg_phase": OperationClass.SAFE_WRITE,
    # 8. MATH演算(tools.md、Phase M1)
    # configure_math はオシロ内蔵の演算トレース(表示・解析層)だけを変える完全に
    # 可逆な操作で、取り込み設定にも出力にも触れないため configure_decode と同じ
    # SAFE_WRITE。50Ω・信号出力のような引数依存の昇格も無い(演算子と引数の
    # 結合制約は機器のエラーキューが返す)。
    "configure_math": OperationClass.SAFE_WRITE,
    "get_math_state": OperationClass.READ_ONLY,
    # 9. カーソル・周波数カウンタ・電圧計・ヒストグラム(tools.md、Phase M2)
    # いずれも configure_decode / configure_math と同じ根拠で SAFE_WRITE: 画面の
    # カーソルと測定表示・統計だけを変える完全に可逆な操作で、取り込み設定にも
    # 出力にも触れない(信号は1mVも外へ出ない)。50Ω・信号出力のような引数依存の
    # 昇格も無く、モードとの結合制約(Totalize時の NDIGits 等)は機器のエラー
    # キューが返す。統計のクリア/リセット(configure_meter の clear_totalize、
    # configure_histogram の reset)も消えるのは機器が数えた統計だけで、利用者の
    # 設定は失われないため昇格させない。
    "configure_cursor": OperationClass.SAFE_WRITE,
    "get_cursor_measurement": OperationClass.READ_ONLY,
    "configure_meter": OperationClass.SAFE_WRITE,
    "get_meter_value": OperationClass.READ_ONLY,
    "configure_histogram": OperationClass.SAFE_WRITE,
    "get_histogram_result": OperationClass.READ_ONLY,
    # 10. リファレンス波形(tools.md、Phase M3)
    # configure_reference も configure_math / configure_cursor と同じ根拠で
    # SAFE_WRITE: 画面のリファレンストレース(表示・解析層)だけを変える操作で、
    # 取り込み設定にも出力にも触れない(信号は1mVも外へ出ない)。
    # **ただし `save=True` は不可逆**で、その枠に入っていた波形は失われる
    # (枠にデータがあるかを問い合わせるコマンドが機器に無く、undoも無い)。
    # それでも昇格させないのは、失われるのが機器内の表示用データだけで、
    # 被測定回路にも利用者の取り込み設定にも影響しないため —— 同じく利用者に
    # 見えている状態を壊す clear_measurements と同じ扱いとする。不可逆である
    # ことはTool説明文で呼び出し側へ明示する(server.py)。
    "configure_reference": OperationClass.SAFE_WRITE,
    "get_reference_state": OperationClass.READ_ONLY,
    # 11. Raw SCPI(開発用・デフォルト無効)
    "raw_scpi": OperationClass.DANGEROUS_WRITE,
}


def classify(tool: str) -> OperationClass:
    """Tool名の基本操作クラスを返す。

    未知のTool名は ScopeError(INVALID_PARAMETER)。未知の操作を
    「安全側」と誤認しないための fail-closed 動作。
    """
    try:
        return TOOL_CLASSES[tool]
    except KeyError:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"Unknown tool name: {tool}",
            {"tool": tool},
        ) from None
