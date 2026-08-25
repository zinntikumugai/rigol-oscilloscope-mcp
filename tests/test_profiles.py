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
    assert p.dialect["scpi_port"] == 5555
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
    assert p.dialect["screenshot_format"] == "png"
    assert p.dialect["waveform_preamble"] == {"yorigin": 0, "yreference": 128}
    assert p.dialect["nr3_single_digit_exponent"] is True
    assert p.dialect["snaps_to_125"] is False
    assert p.dialect["invalid_query_behavior"] == "silent_timeout"
    assert p.dialect["error_queue_stale_on_connect"] is True
    assert p.limits["probe_ratio"] == [0.001, 0.01, 0.1, 1, 10, 100, 1000, 10000]


def test_mho98_inherits_generic_and_overrides() -> None:
    generic = load_profile(GENERIC)
    mho98 = load_profile("mho98")

    # 継承: genericのdialectキーはすべて見える
    assert set(generic.dialect) <= set(mho98.dialect)
    # 深いマージ: measurement_items は共通5項目 + 固有5項目 = 10項目
    assert len(mho98.dialect["measurement_items"]) == 10
    for name, mnemonic in generic.dialect["measurement_items"].items():
        assert mho98.dialect["measurement_items"][name] == mnemonic
    # リストは置換(マージではない)
    assert len(mho98.capabilities["measurements"]) == 10
    assert mho98.capabilities["measurements"] == [
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
    r = resolve_profile(idn("DHO814"))
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
  scpi_port: 5555
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
    assert child.dialect["scpi_port"] == 5555
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
