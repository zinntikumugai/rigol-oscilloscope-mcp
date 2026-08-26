# LLMによる総合動作確認プロンプト

**用途:** 本MCPサーバーを接続した**別のLLMセッション**(Claude Code / Codex等)に下記プロンプトを貼り、接続→設定→測定→解析→デコード→復元の通し動作を確認させる。Requirements 11.2「AI連携」の確認手段。

**前提:**

- MCPサーバーが設定済みのセッションであること(README「インストール・起動」)
- 実機を使う場合: 接続先IPを手元に用意し、**CH1にプローブ補償信号(本体の校正端子、1 kHz方形波)を接続しておく**と測定・デコードまで確認できる
- 実機なしの場合: `RIGOL_MCP_FAKE=1` で起動(FakeScopeは1 kHz/3.27 Vpp相当の決定的な応答を返す。デコードイベントは固定データ)

**安全上の注意(プロンプト本体にも同じ制約を埋め込んである):**

- 50Ω設定・autosetの**実行**はしない(autosetはconfirmトークンの**発行確認のみ**で中止する)
- AFGの出力ONは行わない(状態取得のみ)。出力ONまで確認したい場合は、AFG出力端子が**解放(未接続)**であることを人間が確認した上で明示的に指示すること
- プロンプトは最後に全設定を復元させるが、大切な測定セットアップ中の実機では実行しないこと

---

## プロンプト本体(コピーして別セッションへ)

```text
You are testing an MCP server ("rigol-oscilloscope") that controls a RIGOL
oscilloscope over SCPI. Run the following end-to-end operational check using
ONLY the MCP tools, in order. Record each step's outcome. Safety constraints,
no exceptions: never set channel impedance to "50", never complete the autoset
confirmation, never enable the AFG output; when a tool returns an error dict,
record its code/message and continue with the remaining steps.

1.  connect — ask me for the device address first (do not guess). Report the
    model, profile name and confidence from the result.
2.  get_capabilities — report analog_channels, confidence, and options (may be
    null on models without option queries).
3.  get_state with sections=["channels","timebase","trigger"] — save this as
    the RESTORE SNAPSHOT. Also call get_state with sections=["trigger"] to
    confirm section filtering works.
4.  configure_channel on CH1: enabled=true, coupling="DC", probe_ratio=10,
    scale_v_per_div=1.0, offset_v=0. Verify each applied value; note any
    value the instrument snapped.
5.  configure_timebase scale_s_per_div=0.0002, then configure_trigger
    type="edge", source="CH1", level_v=1.5, slope="rising", sweep_mode="auto".
    Verify applied values.
6.  run, then measure CH1 with ["frequency","vpp"]. With the probe-comp signal
    the frequency should be ~1 kHz; report the values and their quality flags.
7.  analyze_waveform CH1 with analyses=["stats","fft"] — report vpp_v,
    dominant_frequency_hz and frequency_resolution_hz; sanity-check the
    dominant frequency against step 6.
8.  capture_waveform CH1 — report points and effective sample rate (do not
    print the samples). capture_screenshot — report the saved path and confirm
    an image was returned.
9.  clear_measurements — expect {"result": "ok"} (this clears the on-screen
    Result view items added by step 6).
10. configure_decode protocol="uart", bus=1, enabled=true, event_table=true,
    data_format="hex", settings={"rx_source":"CH1","tx_source":"off",
    "baud_bps":9600,"rx_threshold_v":1.5}. Then stop, then get_decode_result
    bus=1 — report columns and event_count (events may be empty without a real
    UART signal; the probe-comp square wave usually decodes as one byte).
11. Confirmation-flow check WITHOUT executing: call autoset once with no
    confirm_token. Expect an error dict with code USER_CONFIRMATION_REQUIRED
    containing confirm_token/risk/instruction. DO NOT call it again — abort
    here and record that the gate works.
12. get_afg_state (if supported on this model) — report output state per
    channel. Do NOT enable the output.
13. RESTORE: using the snapshot from step 3, restore CH1/timebase/trigger to
    their original values (and run/stop state). Re-read the same sections and
    report any field that differs from the snapshot (volatile fields like
    trigger_status/sample_rate may differ — note but don't count them).
14. Final report as a table: step | tool(s) | PASS/FAIL/SKIP | notes. Include
    every error dict (code + message) you saw. State explicitly whether the
    restore left any non-volatile difference.
```

---

## 期待される結果の目安

| 環境 | 目安 |
|---|---|
| 実機(MHO98、プローブ補償接続) | 全ステップPASS。手順6は ~1 kHz / ~3.3 Vpp、手順10はイベント1件以上(0xF0等) |
| 実機(guideプロファイル機種) | 手順1でconfidence=guideが報告される。デコード(手順10)とAFG(手順12)は `UNSUPPORTED_FEATURE` → SKIP扱いで正常 |
| FakeScope(`RIGOL_MCP_FAKE=1`) | 全ステップPASS。手順6は 1.0001 kHz / 3.268 Vpp、手順10のイベントは固定データ |

FAILが出た場合は、最終レポートのエラーdict(code/message)と該当ステップを添えてissueへ。
