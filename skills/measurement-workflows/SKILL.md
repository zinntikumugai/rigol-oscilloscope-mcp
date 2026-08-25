---
name: measurement-workflows
description: Measurement workflows and safety rules for the rigol-oscilloscope MCP tools. Use whenever the user wants to measure, view, capture, or debug an electrical signal (UART, I2C, SPI, PWM, clock, power ripple, audio, unknown signals) with a Rigol oscilloscope.
---

# Oscilloscope Measurement Workflows

## Safety first — before ANY measurement

1. **Confirm the physical setup with the user.** The MCP server cannot see probes. Ask the user to confirm: the probe tip is on the intended test point, the ground clip is on a ground reference, and the probe's attenuation switch matches the `probe_ratio` you configure.
2. **Never measure AC mains (line voltage) with a standard probe.** If the user asks to probe mains, a primary side of a power supply, or anything referenced to line voltage: refuse and explain that this requires an isolated differential probe. Do not proceed without one, even if asked again — a grounded scope probe on mains can destroy equipment and injure people.

## Choosing settings from the signal type

Pick the row matching the signal, then adjust with the user's known values. All values map 1:1 onto `configure_channel` / `configure_timebase` / `configure_trigger` arguments.

| signal | coupling | assume Vpp | assume freq | trigger | sweep_mode | acquire | probe_ratio | notes |
|---|---|---|---|---|---|---|---|---|
| digital logic | DC | 3.3 V | 1 MHz | rising, Vpp/2 | normal | run | 10 | ask the logic level if unknown |
| uart | DC | 3.3 V | 115200 baud | **falling**, Vpp/2 | normal | single | 10 | idle-high: the start bit is a falling edge. timebase = 1/baud s/div shows one ~10-bit frame |
| i2c | DC | 3.3 V | 100 kHz (SCL) | falling, Vpp/2 | normal | single | 10 | two lines (SDA/SCL) — probe both, trigger on SDA |
| spi | DC | 3.3 V | 1 MHz (SCLK) | falling (CS assert), Vpp/2 | normal | single | 10 | multiple lines: CS/SCLK/MOSI |
| pwm | DC | 3.3 V | 10 kHz | rising, Vpp/2 | auto | run | 10 | then `measure` duty and frequency |
| clock | DC | 3.3 V | 10 MHz | rising, Vpp/2 | auto | run | 10 | |
| power ripple | **AC** | 50 mV | 100 kHz (switching) | rising, 0 V | auto | run | **1** | set `bandwidth_limit=true`; use a 1x probe with a short ground spring |
| switching PSU (secondary) | DC | 12 V | 100 kHz | rising, Vpp/2 | auto | run | 10 | secondary side only — the primary side is mains-referenced (see safety rule 2) |
| audio | AC | 1 V | 1 kHz | rising, 0 V | auto | run | 10 | |
| unknown signal | DC | 5 V (coarse) | — | none yet; sweep_mode auto, level 0 V | auto | run | 10 | start coarse: 1 ms/div, then follow the exploration workflow below |

Scaling rules (the instrument snaps values itself — trust the `applied` field it returns, no need to pre-round to 1-2-5 steps):

- `scale_v_per_div = Vpp / 4` (fills ~4 of 8 vertical divisions)
- `scale_s_per_div = periods_to_show / (frequency * 10)` (10 horizontal divisions; show ~4 periods of a periodic signal)
- trigger `level_v = Vpp / 2` for logic-type signals, `0` for AC-coupled ones

## UART workflow

1. `get_state` (use `sections` to read only what you need — a full read is ~39 SCPI queries)
2. Decide settings from the table, substituting the actual baud rate and logic level
3. `configure_channel` → `configure_timebase` → `configure_trigger`
4. `single`, wait for the trigger, then `measure` (frequency, vpp) and `capture_screenshot`
5. Interpret: bit time should be 1/baud; if the trace shows no edges, re-check the physical connection with the user

## Unknown-signal exploration workflow

Never change everything at once. Narrow down step by step:

1. `get_state`, then set a coarse timebase (1 ms/div) and vertical scale (per the table)
2. `capture_screenshot` or `capture_waveform` and inspect
3. Adjust `scale_v_per_div` to the observed amplitude
4. Narrow `scale_s_per_div` until individual periods are visible
5. Set the trigger (edge, half the observed amplitude), re-acquire
6. `measure` frequency and vpp to confirm

## Iteration limit

Keep configure → re-measure feedback loops to about **5 iterations**. If the signal still is not usable, report what you observed and ask the user before continuing.

## Operational notes

- Always prefer the `applied` values in tool responses over what you requested — the instrument snaps and clamps.
- Writes to a channel whose display is off are silently ignored by the instrument; enable the channel first (`configure_channel` with `enabled=true`).
- If a tool responds with `USER_CONFIRMATION_REQUIRED` and a `confirm_token`, ask the human user for explicit consent and only then repeat the call with the token. Never auto-confirm.
