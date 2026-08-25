# 今後の対応予定(MVP対象外)

**対象文書:** [Requirements.md](Requirements.md) 3.3 / 10.3 / 12章 の詳細
**位置づけ:** 本文書は規範(要件)ではなく予定・検討事項の記録。着手時に要件へ昇格させる

MVP(Phase 1 + 2 = Read Only + Basic Control)完了後に対応する機能と、判断を保留している検討事項をここに残す。旧要件定義 v0.1 に含まれていた将来機能の詳細も本文書へ移管した。

---

## 1. Phase 3 — Measurement Assistant(完了・要件へ昇格)

**同梱スキルで実現し完了**(2026-08-25)。信号種別10種の推奨設定表・ワークフロー・安全プロンプトは `skills/measurement-workflows/SKILL.md`、プラグイン構成は [Requirements.md](Requirements.md) 10.3、受入基準は同 11.3 を参照。

- サーバー側Tool `recommend_setup`([tools.md](tools.md) 7章)は**実装せず据え置き**。スキルで精度不足が実証された場合のフォールバックとして仕様のみ残す

## 2. Phase 4 — 機器の高度機能

### 2.1 シリアルプロトコルデコード(設定は完了・要件へ昇格)

**標準搭載6種(UART/RS232、I²C、SPI、CAN、LIN、パラレル)の設定Tool `configure_decode` を実装済み**([tools.md](tools.md) 6章、要件は [Requirements.md](Requirements.md) 3.2)。プロトコル別引数は `settings` オブジェクトで受け、対応表は機種プロファイルの `decode_protocols` と `driver/decode.py` が持つ。

残件:

- **`get_decode_result`(未実装・PR C予定)**: デコード結果の取得SCPIとイベントテーブルの応答形式は実機検証が必要
- **オプション必須プロトコルは延期**: I2S、FlexRay、MIL-STD-1553、CAN-FD。未ライセンス機では検証できないため、ライセンス適用後に着手する
- **将来ゲートは送信前に不要**: オプション必須ニモニックは沈黙せず「値を返しつつエラーキューに `-222` を積む」と実測済みのため、既存の「set → エラーキュー確認 → read-back」で機器自身のエラーを検出できる(実測根拠: [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) 4章)。ただしquery系でもエラーキュー確認を省略しないこと
- `:BUS` コアはDHO/MHO共通と見られる(ガイド比較)。DHO実機を検証できたらファミリプロファイルへ引き上げる([device-profiles.md](device-profiles.md) 2.2)

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

## 3. プラグイン化(完了・要件へ昇格)

**完了**(2026-08-25)。Claude(`.claude-plugin/plugin.json`)・Codex(`.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json`)の両プラグインとして実装し、[Requirements.md](Requirements.md) 10.3 へ昇格した。スキル(`skills/measurement-workflows/SKILL.md`、Agent Skillsオープン標準)とMCP起動定義は両ホストで共有。旧v0.1のスキル素材(UART測定・Unknown Signal探索・安全プロンプト・反復上限)はすべてスキル本文へ吸収済み。

**残タスク: Codex CLIでの実動作確認**(公式ドキュメント準拠で作成、実CLI未確認)。確認対象は Requirements.md 10.3 の未検証事項2点(`mcpServers` の相対パス指定、マーケットプレイスsource `path: "./"`)と、`codex plugin marketplace add zinntikumugai/rigol-oscilloscope-mcp` → install → スキル発見・MCPサーバー起動の通し。なおCodexはMCPサーバー単体なら `config.toml`([Requirements.md](Requirements.md) 10.2)、スキル単体なら `~/.agents/skills/` へのコピーでもプラグインなしで利用できる。

## 4. 機種プロファイルの拡充

- MHO98で未検証の項目の実機確認([device-profiles.md](device-profiles.md) 3.1、[verification/mho98-mvp.md](verification/mho98-mvp.md) 4章): 50Ωニモニック、autoset書き込み、RAWモード波形、limits境界値(RUN/STOP/SINGleはMVPで実機確認済み)
- **USB(USBTMC)接続の実機検証** — ユニットテスト(`tests/test_usb_transport.py`、PyVISAのフェイク)は通っているが実機未検証。VISAリソース文字列の推奨形式もここで確定させる
- **表示OFFチャンネルへの書き込みが無視される件への対策検討**([verification/mho98-mvp.md](verification/mho98-mvp.md) 3.3): 表示OFFのCHへ `:SCALe` / `:OFFSet` を送るとエラーなく無視される。`configure_channel` で自動的に `enabled=True` にするか、requested/applied の不一致を警告として返すに留めるか、要検討(暗黙に表示をONにするのは利用者の画面を勝手に変える副作用でもある)
- MHO98以外の対応機種の追加(DHO800/900系などを候補に、実機が用意でき次第)
- ファミリプロファイルの括り出し(同系2機種以上の検証が揃った段階で)
- **`raw_scpi` Tool は未実装**(configの `RIGOL_MCP_RAW_SCPI` は将来用の予約。[tools.md](tools.md) 8章の仕様で実装する際に使用する)

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
