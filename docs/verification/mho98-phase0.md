# Phase 0 real-device verification results

- Date: 2026-08-22
- Device: RIGOL MHO98, firmware 00.01.00 (serial withheld), reached over LAN SCPI :5555
- Probe: `scpi-probe -suite all` (read-only, write probes not yet run)

## Confirmed working as implemented

- `*IDN?` → `RIGOL TECHNOLOGIES,MHO98,<serial>,00.01.00`
- Channel queries: `:CHANnel1:DISPlay?/SCALe?/OFFSet?/COUPling?/PROBe?/BWLimit?/IMPedance?`
  - Response formats seen: `2.000000E+00`, `1.000000E+1` (single-digit exponent — NR3 parser handles it), `DC`, `OFF`, `OMEG`
- Timebase: `:TIMebase:MAIN:SCALe?/OFFSet?`, `:ACQuire:SRATe?`, `:ACQuire:MDEPth?` (returns a number, `1.0000E+04`)
- Trigger: `:TRIGger:MODE?`→`EDGE`, `:TRIGger:EDGE:SOURce?`→`CHAN1`, `LEVel?`, `SLOPe?`→`POS` (short-form response, parsed OK), `:TRIGger:SWEep?`→`AUTO`, `:TRIGger:STATus?`→`TD`
- Measurements OK: `FREQuency`, `PERiod`, `VPP`, `VMAX`, `VMIN` (1 kHz / 3.268 Vpp probe-comp signal)
- Waveform chain: SOURce/MODE/FORMat BYTE/STARt/STOP/`:WAVeform:DATA?` (definite-length block, 1000 bytes)
  - Preamble: `0,0,1000,1,2.000000E-6,-1.000000E-3,0.000000,6.8267E-02,0,128`
    → **yorigin=0, yreference=128** (mock had yref=127); conversion `volts=(raw-yorigin-yref)*yinc` verified: vmin −0.068 V, vmax 3.140 V
  - Screen mode xinc 2e-6 (500 kSa/s) while `:ACQuire:SRATe?` reports 5e6 — screen data is decimated, as expected
- Screenshot: `:DISPlay:DATA?` (no argument) → PNG (`\x89PNG`), 97 098 bytes
- Error queue: `:SYSTem:ERRor?` format `-100,"Command err"` / `0,"No error"`
- Query latency ≈ 30–40 ms each (composite get_state ≈ 38 queries ⇒ ~1.3 s, slightly above the 1 s target)

## Deviations found (fixes required)

1. **`:MEASure:ITEM? VAVerage,...` is not accepted**: no response (5 s timeout) and `-222 "Data out of range"` queued.
   → Use `VAVG` (DS1000Z-style item name). Remaining items (`VRMS`, `PDUTy`, `RTIMe`, `FTIMe`) were not reached in this run — re-verify after the fix.
2. **Unknown/invalid query behavior**: device stays **silent** (client times out) and queues an error (`-100,"Command err"` for undefined header — not `-113`).
   → A wrong query mnemonic costs a full timeout; the mock should queue `-100,"Command err"` to match.
3. **Stale error queue**: `-222` was already queued before the run (leaked from a previous session) and polluted the idn suite's report.
   → scpi-probe should drain the error queue once at connect, before the first suite.
4. Mock waveform preamble should use yreference=128, yorigin=0 to match.

## Second run (same day, after fixes + `-allow-write`)

All previously failing/unreached items now verified:

- Measurement items all valid: `VAVG` (fix confirmed), `VRMS`, `PDUTy` (**unit = ratio**, 0.5002 — matches implementation), `RTIMe`, `FTIMe`
- Write commands verified with set→readback→restore, all with empty error queue:
  - `:CHANnel1:SCALe 3.0` → applied exactly 3 V/div
  - `:TIMebase:MAIN:SCALe 3e-4` → applied exactly 0.3 ms/div (sample rate followed: 5 MSa/s → 2 MSa/s)
  - `:TRIGger:MODE EDGE`, `:TRIGger:EDGE:SOURce CHANnel1`, `:TRIGger:EDGE:SLOPe POSitive`, `:TRIGger:SWEep AUTO`, `:TRIGger:EDGE:LEVel 2.0` → all applied
- **Device does NOT snap to 1-2-5**: both 3 V/div and 0.3 ms/div were accepted verbatim (MHO98 supports fine steps). The mock's 1-2-5 snapping diverges from instrument truth — kept deliberately as a stress case for the requested-vs-applied path (driver reads back after set, so behavior is correct either way), but do not treat mock snapping as device behavior.
- Latency variability observed under load: individual queries occasionally 0.9–3.0 s (VAVG 2.97 s, screenshot 1.88 s, `:SYSTem:ERRor?` 2.7 s). The 5 s default timeout holds, but composite operations (get_state) can exceed the 1 s response target on a busy device.

## Still unverified

- 50Ω impedance set mnemonic (`FIFT`) — deliberately not probed (risky write)
- `RUN`/`STOP`/`SINGle`/`AUToset` writes (probe avoids disturbing acquisition state)
- RAW-mode waveform download, chunk limits, larger memory depths
- Full parameter ranges (capabilities table boundaries)
- Bandwidth-limit set values, offset/position set commands
