# 今後の対応予定(MVP対象外)

**対象文書:** [Requirements.md](Requirements.md) 3.3 / 10.3 / 12章 の詳細
**位置づけ:** 本文書は規範(要件)ではなく予定・検討事項の記録。着手時に要件へ昇格させる

MVP(Phase 1 + 2 = Read Only + Basic Control)完了後に対応する機能と、判断を保留している検討事項をここに残す。旧要件定義 v0.1 に含まれていた将来機能の詳細も本文書へ移管した。

---

## 1. Phase 3 — Measurement Assistant

測定目的(「UARTを見たい」)から設定を導く支援層。

- **第一候補: 配布スキルでの実現。** 推奨ロジック(信号種別→設定の対応)はLLM自身の知識+スキルのワークフロー記述で賄えれば、サーバー側実装は不要
- サーバー側Tool `recommend_setup`([tools.md](tools.md) 6章)は、スキルで精度が不足する場合のフォールバックとして実装を検討する(機器設定は変更しない読み取り専用Tool)
- 旧v0.1の推奨プリセット案(スキル/実装の素材として保持):
  `digital / uart / i2c / spi / pwm / clock / power_ripple / switching_power_supply / audio / unknown_signal`

## 2. Phase 4 — 機器の高度機能

### 2.1 シリアルプロトコルデコード

MHO98は標準で複数のシリアルデコードを搭載する。対象候補: UART/RS232、I²C、SPI、CAN、CAN-FD、LIN。

- Tool案: `configure_decode` / `get_decode_result`(プロトコル別引数はスキーマで分岐)
- デコード結果の取得SCPIとイベントテーブル形式は実機検証が必要
- capabilitiesの `protocol_decode` で機種ごとの対応プロトコルを表現する

### 2.2 Logic Analyzer

- D0〜D15のON/OFF、Threshold設定、Logic Capture、プロトコルデコード連携
- ロジックプローブの物理接続はMCPから確認できないため、`requires_physical_confirmation` の対象とする

### 2.3 Function / Arbitrary Waveform Generator (AFG)

MHO98は2ch・100 MHz・1 GSa/s のAFGを搭載する。

- Tool案: `configure_afg` / `get_afg_state` / `enable_afg` / `disable_afg`
- **出力ONは DANGEROUS_WRITE**(confirmトークン必須)。DUTへ信号を注入する操作であり、物理確認の促しも必須
- capabilitiesの `afg_channels` で機種差を表現する

### 2.4 ホスト側高度解析

オシロ本体でなくMCPホスト側(Python)で波形データを解析する構成。

- 候補: FFT、Jitter、Overshoot / Undershoot、Ringing、Noise、Signal Integrity、統計測定
- `capture_waveform` の生データ(または一時ファイル)を入力とする解析Tool群として設計する
- NumPy/SciPy依存が増えるため、optional dependency(extras)化を検討する

## 3. Claudeプラグイン化

MCPサーバー本体+スキルを同梱するプラグインとして配布する([Requirements.md](Requirements.md) 10.3)。

同梱するスキルの素材(旧v0.1の操作例より):

- **UART測定ワークフロー:** get_state → (推奨設定の決定) → configure_channel(DC, 1V/div程度) → configure_timebase → configure_trigger(rising, 閾値の半分) → single → measure → capture_screenshot → 解析
- **Unknown Signal探索ワークフロー:** 一度に大きく設定を変えず、状態確認 → 粗いTimebase → 波形取得 → 振幅確認 → Timebase調整 → Trigger設定 → 再取得 → Frequency/Vpp測定、と段階的に絞り込む
- **安全プロンプト:** 物理接続確認の促し、商用電源(AC mains)測定の拒否と絶縁プローブ案内
- **反復上限ガイダンス:** 設定変更→再測定のフィードバックループは目安5回まで(旧 `max_iterations` はサーバー強制でなくスキル側ガイダンスとする)

Codex対応はMCPサーバーの `config.toml` 設定([Requirements.md](Requirements.md) 10.2)で完結する。プロンプト相当を配る仕組みはCodex側の機能(AGENTS.md等)を踏まえて別途検討する。

## 4. 機種プロファイルの拡充

- MHO98で未検証の項目の実機確認([device-profiles.md](device-profiles.md) 3.1、[verification/mho98-mvp.md](verification/mho98-mvp.md) 4章): 50Ωニモニック、autoset書き込み、RAWモード波形、limits境界値(RUN/STOP/SINGleはMVPで実機確認済み)
- **USB(USBTMC)接続の実機検証** — ユニットテスト(`tests/test_usb_transport.py`、PyVISAのフェイク)は通っているが実機未検証。VISAリソース文字列の推奨形式もここで確定させる
- **表示OFFチャンネルへの書き込みが無視される件への対策検討**([verification/mho98-mvp.md](verification/mho98-mvp.md) 3.3): 表示OFFのCHへ `:SCALe` / `:OFFSet` を送るとエラーなく無視される。`configure_channel` で自動的に `enabled=True` にするか、requested/applied の不一致を警告として返すに留めるか、要検討(暗黙に表示をONにするのは利用者の画面を勝手に変える副作用でもある)
- MHO98以外の対応機種の追加(DHO800/900系などを候補に、実機が用意でき次第)
- ファミリプロファイルの括り出し(同系2機種以上の検証が揃った段階で)
- **`raw_scpi` Tool は未実装**(configの `RIGOL_MCP_RAW_SCPI` は将来用の予約。[tools.md](tools.md) 7章の仕様で実装する際に使用する)

## 5. 検討事項(方針未定)

| 項目 | 現状の判断 | 再検討の条件 |
|---|---|---|
| PyPI公開 | 当面しない(GitHubからuvx起動) | 利用者が増え、バージョン固定・供給の信頼性が必要になったら |
| 複数台同時接続 | 非対象(単一アクティブ接続) | 複数台運用の実ニーズが出たら。全Toolへの `device_id` 波及が必要 |
| 機器自動探索(mDNS / VXI-11 discovery) | 非対象 | 接続先入力の手間が問題になったら |
| ネットワークMCP(HTTP/SSE、認証、TLS) | 非対象(stdioローカルのみ) | リモート利用の実ニーズが出たら。認証・ACL設計を伴う |
| MCP Resource(`rigol://state` 等) | 見送り(ホスト側サポートが不均一) | 主要ホストのResource対応が安定したら |
| READ_ONLY操作の並列化 | 見送り(全SCPIを直列化) | 複数クライアントからの読み取り需要が出たら |
| Windows対応 | 対象外(macOS / Linux) | 利用者からの要望が出たら。実装は pathlib 等でOS非依存に書いてあるが、動作確認とパス周り(許可ルート検証・ドライブレター・パス区切り)の検証が必要 |
