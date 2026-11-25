"""機器時刻とホスト時刻の突き合わせ(issue #54)。

スクリーンショットはAIが人間へ渡す唯一の視覚証拠で、画面には機器の時計が写る。
撮影と同時にホストの時計と比べ、ずれていれば返却に警告を添える。**時刻の設定は
しない**(機器のグローバル状態の書き換えになる。#10)。

機器の時計はタイムゾーンを持たないので、ホストの**ローカル時刻**と比べる。
ずれが時差の単位(15分)にほぼ一致するなら、時計ではなくタイムゾーン設定の
違いだと示唆する(実機で観測: 機器は JST、ホストは UTC で +9 時間)。
"""

from __future__ import annotations

from datetime import datetime

from ..driver.scope import ScopeDriver

#: この秒数を超えるずれで警告する。機器の時計は秒単位で、クエリにも数十msかかる
CLOCK_TOLERANCE_S = 10.0
#: タイムゾーンのオフセットの最小単位(秒)
_TIMEZONE_STEP_S = 900
#: 時差と見なす残差の上限(秒)。時差に加えて時計のドリフトも乗る(実機実測:
#: +9時間 + 29秒)ので、許容誤差より広く取る
_TIMEZONE_SLACK_S = 120.0


def _timezone_hint(skew_s: float) -> tuple[str, float] | None:
    """ずれが時差(15分単位)に近ければ (`+09:00` 形式, 残りのずれ秒) を返す。"""
    steps = round(skew_s / _TIMEZONE_STEP_S)
    residual = round(skew_s - steps * _TIMEZONE_STEP_S, 1)
    if steps == 0 or abs(residual) > _TIMEZONE_SLACK_S:
        return None
    minutes = abs(steps) * _TIMEZONE_STEP_S // 60
    sign = "+" if steps > 0 else "-"
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}", residual


def clock_check(driver: ScopeDriver, now: datetime | None = None) -> dict | None:
    """機器時刻・ホスト時刻・ずれ(秒、機器 − ホスト)・警告。非対応機種は None。

    `now` はテスト用(タイムゾーン付き)。省略時はホストのローカル時刻。
    """
    device = driver.get_device_time()
    if device is None:
        return None
    host = (now or datetime.now()).astimezone()
    skew = round((device - host.replace(tzinfo=None)).total_seconds(), 1)

    warning = None
    if abs(skew) > CLOCK_TOLERANCE_S:
        warning = (
            f"The device clock is {skew:+.1f} s off the host clock; the time "
            "shown on the screen (and in screenshots) may be wrong."
        )
        hint = _timezone_hint(skew)
        if hint is not None:
            offset, residual = hint
            warning += (
                f" The difference is close to {offset}, so the device is probably "
                "set to a different timezone than the host"
            )
            warning += (
                f" (plus {residual:+.1f} s of clock drift)."
                if abs(residual) > CLOCK_TOLERANCE_S
                else "."
            )
    return {
        "device_time": device.isoformat(),
        "host_time": host.isoformat(timespec="seconds"),
        "time_skew_s": skew,
        "warning": warning,
    }
