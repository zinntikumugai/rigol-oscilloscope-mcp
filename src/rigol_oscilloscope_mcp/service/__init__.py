"""サービス層(接続ライフサイクルと、Toolから呼ばれる操作の組み立て)。"""

from .analysis import VALID_ANALYSES, analyze_waveform, waveform_fft, waveform_stats
from .connection import ConnectionManager, ConnectionStatus
from .control import AUTOSET_NOTE, ControlService
from .decode import get_decode_result
from .measurement import get_measurement_statistics, get_meter_value, measure
from .paths import allowed_roots, resolve_write_path
from .screenshot import SUPPORTED_FORMATS, ScreenshotResult, capture_screenshot
from .state import (
    VALID_SECTIONS,
    get_acquisition_dict,
    get_channel_dict,
    get_state,
    get_timebase_dict,
    get_trigger_dict,
)
from .waveform import INLINE_POINTS_LIMIT, capture_waveform

__all__ = [
    "AUTOSET_NOTE",
    "INLINE_POINTS_LIMIT",
    "SUPPORTED_FORMATS",
    "VALID_ANALYSES",
    "VALID_SECTIONS",
    "ConnectionManager",
    "ConnectionStatus",
    "ControlService",
    "ScreenshotResult",
    "allowed_roots",
    "analyze_waveform",
    "capture_screenshot",
    "capture_waveform",
    "get_acquisition_dict",
    "get_channel_dict",
    "get_decode_result",
    "get_measurement_statistics",
    "get_meter_value",
    "get_state",
    "get_timebase_dict",
    "get_trigger_dict",
    "measure",
    "resolve_write_path",
    "waveform_fft",
    "waveform_stats",
]
