"""機種プロファイル(docs/device-profiles.md)のテスト。"""

import dataclasses
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.models import IdnInfo
from rigol_oscilloscope_mcp.profiles import (
    Profile,
    ResolvedProfile,
    available_profiles,
    load_profile,
    resolve_profile,
)
from rigol_oscilloscope_mcp.profiles.loader import (
    _available_profiles_from,
    _load_profile_from,
    _resolve_profile_from,
)

GENERIC = "rigol-generic"

RIGOL = "RIGOL TECHNOLOGIES"


def idn(model: str, manufacturer: str = RIGOL) -> IdnInfo:
    return IdnInfo(
        manufacturer=manufacturer, model=model, serial="SN123", firmware="00.01.00"
    )


# --- dataclass契約 -----------------------------------------------------------


@pytest.mark.parametrize("model", [Profile, ResolvedProfile])
def test_profile_models_are_frozen_dataclasses(model: type) -> None:
    assert dataclasses.is_dataclass(model)
    assert model.__dataclass_params__.frozen  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (Profile, ["name", "confidence", "capabilities", "dialect", "limits"]),
        (ResolvedProfile, ["profile", "unsupported_vendor"]),
    ],
)
def test_profile_field_names_and_order(model: type, fields: list[str]) -> None:
    assert [f.name for f in dataclasses.fields(model)] == fields


# --- 同梱プロファイル --------------------------------------------------------


def test_available_profiles_contains_bundled_profiles() -> None:
    names = available_profiles()
    assert names == sorted(names)
    assert "mho98" in names
    assert GENERIC in names


def test_load_generic_profile() -> None:
    p = load_profile(GENERIC)
    assert p.name == GENERIC
    assert p.confidence == "generic"
    assert p.capabilities["analog_channels"] == 4
    assert p.dialect["screenshot_command"] == ":DISPlay:DATA?"
    assert p.limits == {}
    assert sorted(p.dialect["measurement_items"]) == [
        "frequency",
        "period",
        "vmax",
        "vmin",
        "vpp",
    ]


def test_load_mho98_profile_fields() -> None:
    p = load_profile("mho98")
    assert p.name == "mho98"
    assert p.confidence == "verified"
    assert p.capabilities["analog_channels"] == 4
    assert p.capabilities["digital_channels"] == 16
    assert p.capabilities["afg_channels"] == 2
    assert p.capabilities["protocol_decode"] is True
    assert p.capabilities["impedance_50ohm"] is True
    assert p.dialect["screenshot_command"] == ":DISPlay:DATA?"
    # yorigin は定数ではなく設定依存の動的値のためプロファイルには持たせない
    # (実測: offset -0.064 V で yorigin=-9.0)。ライブのプリアンブルを使うこと。
    assert p.dialect["waveform_preamble"] == {"yreference": 128}
    assert p.dialect["nr3_single_digit_exponent"] is True
    assert p.dialect["snaps_to_125"] is False
    assert p.dialect["invalid_query_behavior"] == "silent_timeout"
    assert p.dialect["error_queue_stale_on_connect"] is True
    assert p.limits["probe_ratio"] == [0.001, 0.01, 0.1, 1, 10, 100, 1000, 10000]


def test_mho98_declares_option_query() -> None:
    """オプション照会はMHO900専用(docs/verification/mho98-unlicensed.md)。"""
    p = load_profile("mho98")

    assert p.dialect["option_query"] == ":SYSTem:OPTion:STATus?"
    types = p.dialect["option_types"]
    assert types["bundle"] == "BND"
    assert types["afg_50mhz"] == "AFG50"
    assert types["memory_500mpts"] == "RLU-05"
    assert len(types) == 11


def test_generic_does_not_declare_option_query() -> None:
    """DHO800/900 には `:SYSTem:OPTion:*` が無い。宣言の不在がゲートになる。"""
    p = load_profile(GENERIC)

    assert "option_query" not in p.dialect
    assert "option_types" not in p.dialect


def test_mho98_declares_standard_decode_protocols() -> None:
    """標準搭載6種のみ(オプション必須プロトコルは不在=ゲート)。"""
    p = load_profile("mho98")

    assert p.capabilities["decode_buses"] == 4
    assert p.dialect["decode_protocols"] == {
        "uart": "RS232",
        "i2c": "IIC",
        "spi": "SPI",
        "can": "CAN",
        "lin": "LIN",
        "parallel": "PARallel",
    }
    assert p.dialect["decode_formats"] == {
        "hex": "HEX",
        "ascii": "ASCii",
        "dec": "DEC",
        "bin": "BIN",
    }


def test_generic_does_not_declare_decode() -> None:
    """`:BUS` はDHO/MHO共通だが未検証。宣言の不在がそのままゲートになる。"""
    p = load_profile(GENERIC)

    assert "decode_protocols" not in p.dialect
    assert "decode_buses" not in p.capabilities
    assert p.supports("protocol_decode") is False


def test_mho98_declares_afg_dialect() -> None:
    """信号発生(`:SOURce<n>`)。実機検証: docs/verification/mho98-afg.md。"""
    p = load_profile("mho98")

    assert p.capabilities["afg_channels"] == 2
    assert p.dialect["afg_prefix"] == ":SOURce{n}"
    waveforms = p.dialect["afg_waveforms"]
    assert len(waveforms) == 13
    assert waveforms["sine"] == "SINusoid"
    assert waveforms["exp_rise"] == "EXPRise"
    # 実機に PULSe は存在しない(送ると -222。mho98-afg.md 2章)
    assert "pulse" not in waveforms
    assert p.dialect["afg_impedances"] == {"highz": "OMEG", "50": "FIFTy"}


def test_mho98_declares_afg_modulation_dialect() -> None:
    """変調(ガイド3.25.15-25)。変調ソースは内蔵のみ(EXTernalは無い)。"""
    p = load_profile("mho98")

    assert p.dialect["afg_mod_types"] == {"am": "AM", "fm": "FM", "pm": "PM"}
    waveforms = p.dialect["afg_mod_waveforms"]
    assert waveforms == {
        "sine": "SINusoid",
        "square": "SQUare",
        "triangle": "TRIangle",
        "upramp": "UPRamp",
        "dnramp": "DNRamp",
        "noise": "NOISe",
    }


def test_mho98_declares_cursor_meter_and_histogram_dialect() -> None:
    """カーソル/カウンタ/電圧計/ヒストグラム(ガイド3.7・3.8・3.10・3.11)。"""
    p = load_profile("mho98")

    for capability in ("cursor", "frequency_counter", "dvm", "histogram"):
        assert p.supports(capability) is True
    assert p.dialect["cursor_modes"] == {
        "off": "OFF",
        "manual": "MANual",
        "track": "TRACk",
        "xy": "XY",
    }
    assert p.dialect["cursor_types"] == {"time": "TIME", "amplitude": "AMPLitude"}
    assert p.dialect["counter_modes"] == {
        "frequency": "FREQuency",
        "period": "PERiod",
        "totalize": "TOTalize",
    }
    assert p.dialect["dvm_modes"] == {"ac_rms": "ACRMs", "dc": "DC", "dc_rms": "DCRMs"}
    assert p.dialect["histogram_types"] == {
        "horizontal": "HORizontal",
        "vertical": "VERTical",
    }


def test_generic_does_not_declare_cursor_meter_or_histogram() -> None:
    """不在がそのまま UNSUPPORTED_FEATURE のゲート(device-profiles.md 4.2)。"""
    p = load_profile(GENERIC)

    for capability in ("cursor", "frequency_counter", "dvm", "histogram"):
        assert p.supports(capability) is False
    for key in (
        "cursor_modes",
        "cursor_types",
        "counter_modes",
        "dvm_modes",
        "histogram_types",
    ):
        assert key not in p.dialect


def test_generic_does_not_declare_afg() -> None:
    """DHO800/900は番号なし `:SOURce`(DGモジュール)で別方言。不在がゲート。"""
    p = load_profile(GENERIC)

    assert "afg_prefix" not in p.dialect
    assert "afg_waveforms" not in p.dialect
    assert "afg_impedances" not in p.dialect
    assert "afg_mod_types" not in p.dialect
    assert "afg_mod_waveforms" not in p.dialect
    assert "afg_channels" not in p.capabilities


def test_mho98_inherits_generic_and_overrides() -> None:
    generic = load_profile(GENERIC)
    mho98 = load_profile("mho98")

    # 継承: genericのdialectキーはすべて見える
    assert set(generic.dialect) <= set(mho98.dialect)
    # 深いマージ: genericの5項目はそのまま見え、mho98はガイド3.17.2 の全41項目
    assert len(mho98.dialect["measurement_items"]) == 41
    for name, mnemonic in generic.dialect["measurement_items"].items():
        assert mho98.dialect["measurement_items"][name] == mnemonic
    # リストは置換(マージではない)
    assert len(mho98.capabilities["measurements"]) == 41
    assert sorted(mho98.capabilities["measurements"]) == sorted(
        mho98.dialect["measurement_items"]
    )


# --- ガイドベースプロファイル: DHO800 / DHO900 --------------------------------


def test_available_profiles_contains_dho_profiles() -> None:
    names = available_profiles()
    assert "dho800" in names
    assert "dho900" in names


def test_load_dho800_profile_fields() -> None:
    """公式プログラミングガイドの逐語解読のみ(実機未検証 = confidence: guide)。"""
    p = load_profile("dho800")

    assert p.name == "dho800"
    assert p.confidence == "guide"
    # DHO802/812 は2chだが、機種内の差はスキーマ未対応(機器側が拒否する)
    assert p.capabilities["analog_channels"] == 4
    assert p.capabilities["digital_channels"] == 0
    assert p.capabilities["afg_channels"] == 0
    assert p.capabilities["protocol_decode"] is False
    assert p.capabilities["impedance_control"] is False
    assert p.capabilities["impedance_50ohm"] is False
    assert p.capabilities["waveform_download"] is True
    assert p.capabilities["screenshot"] is True
    # DHOは実機未検証なのでM4の41項目拡張の対象外。ガイド解読済みの10項目のみ
    assert p.capabilities["measurements"] == [
        "frequency",
        "period",
        "vpp",
        "vmax",
        "vmin",
        "vavg",
        "rms",
        "duty",
        "rise_time",
        "fall_time",
    ]
    assert p.dialect["waveform_preamble"] == {"yreference": 128}
    assert len(p.limits["probe_ratio"]) == 24
    assert p.limits["probe_ratio"][0] == 0.001
    assert p.limits["probe_ratio"][-1] == 50000


def test_dho900_inherits_dho800() -> None:
    dho800 = load_profile("dho800")
    dho900 = load_profile("dho900")

    assert dho900.confidence == "guide"
    # dho900はAFG方言(番号なし:SOURce+DGSTatusゲート)だけを追加宣言する
    assert set(dho900.dialect) - set(dho800.dialect) == {
        "afg_prefix", "afg_waveforms", "afg_presence_query"
    }
    assert set(dho800.dialect) <= set(dho900.dialect)
    assert dho900.dialect["measurement_items"] == dho800.dialect["measurement_items"]
    # LA は DHO900 シリーズ全体で D0-D15
    assert dho900.capabilities["digital_channels"] == 16
    assert dho900.capabilities["analog_channels"] == 4




def test_dho900_declares_numberless_afg_with_presence_gate() -> None:
    """DHO900のAFGは番号なし:SOURce・1ch・6波形・DGSTatusゲート(S型のみ搭載)。"""
    p = load_profile("dho900")

    assert p.dialect["afg_prefix"] == ":SOURce"
    assert p.capabilities["afg_channels"] == 1
    assert set(p.dialect["afg_waveforms"]) == {
        "sine", "square", "ramp", "dc", "noise", "arb"
    }
    assert p.dialect["afg_presence_query"] == ":SYSTem:DGSTatus?"
    # インピーダンス・変調はDHOガイドに存在しない/未検証 → 未宣言=ゲート
    assert "afg_impedances" not in p.dialect
    assert "afg_mod_types" not in p.dialect


def test_other_dho_profiles_do_not_declare_afg() -> None:
    for name in ("dho800", "dho1000", "dho4000"):
        p = load_profile(name)
        assert "afg_prefix" not in p.dialect, name


def test_load_dho1000_profile_fields() -> None:
    """DHO1000/4000ガイド(PGA34101-1110)の逐語解読のみ(confidence: guide)。"""
    p = load_profile("dho1000")

    assert p.confidence == "guide"
    assert p.capabilities["analog_channels"] == 4
    assert p.capabilities["afg_channels"] == 0  # ガイドに:SOURce章が無い
    assert p.capabilities["digital_channels"] == 0  # :LA章も無い
    # 50Ωは「not supported by the DHO1000 series」(ガイド3.9.8)
    assert p.supports("impedance_control") is False
    assert p.supports("impedance_50ohm") is False


def test_dho4000_inherits_dho1000_and_enables_50ohm() -> None:
    """DHO4000との差分は入力インピーダンスのみ(ガイド3.9.8)。"""
    dho1000 = load_profile("dho1000")
    dho4000 = load_profile("dho4000")

    assert dho4000.confidence == "guide"
    assert set(dho4000.dialect) == set(dho1000.dialect)
    assert dho4000.dialect["measurement_items"] == dho1000.dialect["measurement_items"]
    assert dho4000.supports("impedance_control") is True
    assert dho4000.supports("impedance_50ohm") is True
    assert dho4000.capabilities["analog_channels"] == 4



@pytest.mark.parametrize("name", ["dho800", "dho900", "dho1000", "dho4000"])
def test_dho_profiles_declare_in_scope_dialect(name: str) -> None:
    """ガイド解読で確定した範囲(コア読み書き+画面+測定消去+autoset)のみ。"""
    p = load_profile(name)

    # ガイド 3.9.7: [<type>]={BMP|PNG|JPG}、既定はBMP → PNG引数が必須
    assert p.dialect["screenshot_command"] == ":DISPlay:DATA? PNG"
    assert p.dialect["screenshot_timeout_s"] == 30
    assert p.dialect["autoset_command"] == ":AUToset"  # ガイド 3.2.1
    assert p.dialect["measurement_clear"] == ":MEASure:CLEar"  # ガイド 3.17.3
    assert p.dialect["bwlimit_on"] == "20M"  # ガイド 3.6.1(選択肢は OFF|20M のみ)
    assert p.measurement_mnemonic("vavg") == "VAVG"
    assert p.measurement_mnemonic("duty") == "PDUTy"


@pytest.mark.parametrize("name", ["dho800", "dho900", "dho1000", "dho4000"])
def test_dho_profiles_do_not_declare_unverified_features(name: str) -> None:
    """デコード/AFG/オプション照会は実機検証まで未宣言 — 不在がそのままゲート。"""
    p = load_profile(name)

    afg_core = ("afg_prefix", "afg_waveforms")
    for key in (
        "decode_protocols",
        "decode_formats",
        "afg_impedances",
        "afg_mod_types",
        "afg_mod_waveforms",
        "option_query",
        "option_types",
    ):
        assert key not in p.dialect
    if name != "dho900":  # dho900のみAFGコア方言を宣言(DGSTatusゲート付き)
        for key in afg_core:
            assert key not in p.dialect
    assert "decode_buses" not in p.capabilities
    assert p.supports("protocol_decode") is False


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("DHO802", "dho800"),
        ("DHO814", "dho800"),
        ("DHO924S", "dho900"),
        ("DHO1072", "dho1000"),
        ("DHO1104", "dho1000"),
        ("DHO4804", "dho4000"),
        ("DHO914", "dho900"),
        ("MHO98", "mho98"),
        ("DS1054Z", GENERIC),
    ],
)
def test_resolve_dho_models(model: str, expected: str) -> None:
    """DHO9xx と MHO9xx は先頭文字が異なるため衝突しない。"""
    assert resolve_profile(idn(model)).profile.name == expected


def test_load_profile_unknown_name_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_profile("no-such-scope")
    assert exc.value.code == ErrorCode.INVALID_PARAMETER


# --- Profile のヘルパ --------------------------------------------------------


def test_measurement_mnemonic() -> None:
    mho98 = load_profile("mho98")
    assert mho98.measurement_mnemonic("vavg") == "VAVG"
    assert mho98.measurement_mnemonic("duty") == "PDUTy"
    assert mho98.measurement_mnemonic("frequency") == "FREQuency"
    assert mho98.measurement_mnemonic("nosuch") is None
    # genericには vavg が無い(未確認ニモニックは送らない)
    assert load_profile(GENERIC).measurement_mnemonic("vavg") is None


def test_supports() -> None:
    mho98 = load_profile("mho98")
    assert mho98.supports("screenshot") is True
    assert mho98.supports("waveform_download") is True
    assert mho98.supports("no_such_capability") is False
    # 真偽値でないcapabilityの扱い(analog_channels: 4)も bool を返す
    assert isinstance(mho98.supports("analog_channels"), bool)


# --- 3層解決 ------------------------------------------------------------------


def test_resolve_verified_model() -> None:
    r = resolve_profile(idn("MHO98"))
    assert r.profile.name == "mho98"
    assert r.profile.confidence == "verified"
    assert r.unsupported_vendor is False


def test_resolve_matches_by_regex_family_of_model_string() -> None:
    assert resolve_profile(idn("MHO94")).profile.name == "mho98"


def test_resolve_unknown_rigol_model_falls_back_to_generic() -> None:
    r = resolve_profile(idn("DS1054Z"))
    assert r.profile.name == GENERIC
    assert r.profile.confidence == "generic"
    assert r.unsupported_vendor is False


def test_resolve_non_rigol_vendor() -> None:
    r = resolve_profile(idn("DSOX1204G", manufacturer="KEYSIGHT TECHNOLOGIES"))
    assert r.profile.name == GENERIC
    assert r.profile.confidence == "generic"
    assert r.unsupported_vendor is True


def test_vendor_check_is_case_insensitive() -> None:
    lower = resolve_profile(idn("MHO98", manufacturer="Rigol Technologies"))
    assert lower.unsupported_vendor is False
    assert resolve_profile(idn("MHO98", manufacturer="")).unsupported_vendor is True


def test_resolve_returns_resolved_profile() -> None:
    assert isinstance(resolve_profile(idn("MHO98")), ResolvedProfile)


# --- ディレクトリ注入(一時YAML) ---------------------------------------------


def write_yaml(directory: Path, name: str, body: str) -> None:
    (directory / f"{name}.yaml").write_text(body, encoding="utf-8")


def test_deep_merge_with_injected_directory(tmp_path: Path) -> None:
    write_yaml(
        tmp_path,
        "base",
        """
confidence: generic
capabilities:
  analog_channels: 2
  screenshot: true
dialect:
  screenshot_command: ":DISPlay:DATA?"
  measurement_items:
    frequency: FREQuency
limits:
  probe_ratio: [1, 10]
""",
    )
    write_yaml(
        tmp_path,
        "child",
        """
inherits: base
confidence: verified
match: "^CHILD"
capabilities:
  analog_channels: 4
dialect:
  measurement_items:
    vavg: VAVG
limits:
  probe_ratio: [10]
""",
    )

    child = _load_profile_from(tmp_path, "child")
    assert child.name == "child"
    assert child.confidence == "verified"
    # スカラは置換
    assert child.capabilities["analog_channels"] == 4
    # 親のキーは残る
    assert child.capabilities["screenshot"] is True
    assert child.dialect["screenshot_command"] == ":DISPlay:DATA?"
    # dictは再帰マージ
    assert child.dialect["measurement_items"] == {
        "frequency": "FREQuency",
        "vavg": "VAVG",
    }
    # listは置換
    assert child.limits["probe_ratio"] == [10]

    assert _available_profiles_from(tmp_path) == ["base", "child"]


def test_resolution_priority_verified_over_family(tmp_path: Path) -> None:
    write_yaml(tmp_path, "rigol-generic", "confidence: generic\n")
    write_yaml(
        tmp_path,
        "fam",
        'confidence: family\nmatch: "^XX"\ninherits: rigol-generic\n',
    )
    write_yaml(
        tmp_path,
        "exact",
        'confidence: verified\nmatch: "^XX10"\ninherits: rigol-generic\n',
    )

    assert _resolve_profile_from(tmp_path, idn("XX10")).profile.name == "exact"
    assert _resolve_profile_from(tmp_path, idn("XX20")).profile.name == "fam"
    assert _resolve_profile_from(tmp_path, idn("ZZ1")).profile.name == "rigol-generic"


def test_resolution_priority_guide_between_family_and_generic(tmp_path: Path) -> None:
    """guide(ガイド解読のみ・実機未検証)は family に負け、generic に勝つ。"""
    write_yaml(tmp_path, "rigol-generic", "confidence: generic\n")
    write_yaml(
        tmp_path,
        "guided",
        'confidence: guide\nmatch: "^XX"\ninherits: rigol-generic\n',
    )
    write_yaml(
        tmp_path,
        "fam",
        'confidence: family\nmatch: "^XX10"\ninherits: rigol-generic\n',
    )
    write_yaml(
        tmp_path,
        "loose",
        'confidence: generic\nmatch: "^XX"\ninherits: rigol-generic\n',
    )

    assert _resolve_profile_from(tmp_path, idn("XX10")).profile.name == "fam"
    assert _resolve_profile_from(tmp_path, idn("XX20")).profile.name == "guided"
    assert _resolve_profile_from(tmp_path, idn("ZZ1")).profile.name == "rigol-generic"


def test_circular_inheritance_raises(tmp_path: Path) -> None:
    write_yaml(tmp_path, "a", "inherits: b\nconfidence: verified\n")
    write_yaml(tmp_path, "b", "inherits: a\nconfidence: verified\n")

    with pytest.raises(ScopeError) as exc:
        _load_profile_from(tmp_path, "a")
    assert exc.value.code == ErrorCode.INVALID_PARAMETER


def test_self_inheritance_raises(tmp_path: Path) -> None:
    write_yaml(tmp_path, "loop", "inherits: loop\n")
    with pytest.raises(ScopeError) as exc:
        _load_profile_from(tmp_path, "loop")
    assert exc.value.code == ErrorCode.INVALID_PARAMETER


def test_missing_parent_raises(tmp_path: Path) -> None:
    write_yaml(tmp_path, "orphan", "inherits: ghost\n")
    with pytest.raises(ScopeError) as exc:
        _load_profile_from(tmp_path, "orphan")
    assert exc.value.code == ErrorCode.INVALID_PARAMETER


def test_missing_blocks_default_to_empty_dict(tmp_path: Path) -> None:
    write_yaml(tmp_path, "bare", "confidence: generic\n")
    p = _load_profile_from(tmp_path, "bare")
    assert p.capabilities == {}
    assert p.dialect == {}
    assert p.limits == {}
    assert p.measurement_mnemonic("vpp") is None
    assert p.supports("screenshot") is False


def test_m4_measurement_expansion_is_mho98_only() -> None:
    """41項目への拡張はMHO98(verified)限定。DHO系は実機未検証なので広げない。

    プロファイル宣言の不在がそのままゲートになる(AGENTS.md ルール2)。
    """
    from rigol_oscilloscope_mcp.driver.scope import MEASUREMENT_KEYS

    assert sorted(load_profile("mho98").dialect["measurement_items"]) == sorted(
        MEASUREMENT_KEYS
    )
    for name in ("rigol-generic", "dho800", "dho900", "dho1000", "dho4000"):
        items = load_profile(name).dialect["measurement_items"]
        assert len(items) < 41, f"{name} に未検証の測定項目が混入している"
        assert "delay_rise_rise" not in items
