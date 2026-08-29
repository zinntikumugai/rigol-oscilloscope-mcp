"""models.py の共有dataclassのテスト。"""

import dataclasses

import pytest

from rigol_oscilloscope_mcp.models import (
    ChannelState,
    IdnInfo,
    MeasurementResult,
    TimebaseState,
    TriggerState,
)

ALL_MODELS = [IdnInfo, ChannelState, TimebaseState, TriggerState, MeasurementResult]


@pytest.mark.parametrize("model", ALL_MODELS)
def test_models_are_frozen_dataclasses(model: type) -> None:
    assert dataclasses.is_dataclass(model)
    assert model.__dataclass_params__.frozen  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (IdnInfo, ["manufacturer", "model", "serial", "firmware"]),
        (
            ChannelState,
            [
                "channel",
                "enabled",
                "scale_v_per_div",
                "offset_v",
                "coupling",
                "impedance",
                "probe_ratio",
                "bandwidth_limit",
            ],
        ),
        (
            TimebaseState,
            ["scale_s_per_div", "position_s", "sample_rate_sa_per_s", "memory_depth"],
        ),
        (
            TriggerState,
            ["type", "source", "level_v", "slope", "sweep_mode", "status", "settings"],
        ),
        (MeasurementResult, ["name", "key", "value", "quality"]),
    ],
)
def test_model_field_names_and_order(model: type, fields: list[str]) -> None:
    assert [f.name for f in dataclasses.fields(model)] == fields


def test_idn_info_roundtrip() -> None:
    idn = IdnInfo(
        manufacturer="RIGOL TECHNOLOGIES",
        model="MHO98",
        serial="SN123",
        firmware="00.01.00",
    )
    assert dataclasses.asdict(idn)["model"] == "MHO98"


def test_channel_state_construction() -> None:
    ch = ChannelState(
        channel="CH1",
        enabled=True,
        scale_v_per_div=1.0,
        offset_v=0.0,
        coupling="DC",
        impedance="1M",
        probe_ratio=10.0,
        bandwidth_limit=False,
    )
    assert ch.channel == "CH1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ch.enabled = False  # type: ignore[misc]


def test_timebase_state_allows_none_for_optional_fields() -> None:
    tb = TimebaseState(
        scale_s_per_div=1e-3,
        position_s=0.0,
        sample_rate_sa_per_s=None,
        memory_depth=None,
    )
    assert tb.sample_rate_sa_per_s is None
    assert tb.memory_depth is None


def test_trigger_state_construction() -> None:
    trig = TriggerState(
        type="edge",
        source="CH1",
        level_v=1.5,
        slope="rising",
        sweep_mode="auto",
        status="TD",
        settings={"source": "CH1", "level_v": 1.5, "slope": "rising"},
    )
    assert trig.status == "TD"
    # 2ソース・2レベルの種別では最上位が None になり、値は settings にある
    delay = TriggerState(
        type="delay",
        source=None,
        level_v=None,
        slope=None,
        sweep_mode="auto",
        status="TD",
        settings={"source_a": "CH1", "source_b": "CH2"},
    )
    assert delay.settings["source_b"] == "CH2"


def test_measurement_result_allows_none_value() -> None:
    m = MeasurementResult(
        name="frequency", key="frequency_hz", value=None, quality="no_signal"
    )
    assert m.value is None
    assert m.key == "frequency_hz"


def test_models_are_equal_by_value() -> None:
    a = MeasurementResult("frequency", "frequency_hz", 1000.0, "valid")
    b = MeasurementResult("frequency", "frequency_hz", 1000.0, "valid")
    assert a == b
