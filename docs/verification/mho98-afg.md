# MHO98 AFG(信号発生器)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**前提:** MHO900-BND適用済み(AFG50 / AFG100 両オプション有効)。手順規律は [mho98-unlicensed.md](mho98-unlicensed.md) と同じ(1コマンド → 応答5s timeout → `:SYSTem:ERRor?` → 記録。沈黙時は空行付き再接続 → `*IDN?` で復旧確認)
**安全条件:** 本記録の全プローブで**出力ONは一切行っていない**。各書き込みステップ後に `:SOURce1:OUTPut:STATe?` = `0` を確認済み

## 1. read-onlyプローブ(PR-AFG1、2026-08-26)

ガイド3.25章の `:SOURce<n>` 読み取り系は全て正常応答(エラーキュー全て `0,"No error"`、レイテンシ~0.03s)。

| コマンド | 応答 | 備考 |
|---|---|---|
| `:SYSTem:OPTion:STATus? AFG50` / `AFG100` | 1 / 1 | 両オプション有効 |
| `:SOURce1:OUTPut:STATe?` / `:SOURce2:...` | 0 / 0 | 両ch出力OFF(前提確認) |
| `:SOURce1:FUNCtion?` | SIN | 短形で返る |
| `:SOURce1:FREQuency?` | 1.000000E+3 | NR3 |
| `:SOURce1:VOLTage:AMPLitude?` | 5.000000E0 | Vpp、ガイド既定値 |
| `:SOURce1:VOLTage:OFFSet?` / `:PHASe?` | 0.000000 | |
| `:SOURce1:IMPedance?` | **OMEG** | HighZ。設定後の返却は **FIFTy**(下記) |
| `:SOURce1:FUNCtion:SQUare:DUTY?` / `:RAMP:SYMMetry?` | 5.000000E+1 | 現波形がSineでも保存値を返す |
| `:SOURce1:PERiod?` / `:VOLTage:HIGH?` / `:LOW?` | 1E-3 / 2.5 / -2.5 | Tool非公開だがサブツリー実在確認 |
| `:SOURce2:FUNCtion?` | SIN | ch2応答 |
| `:SOURce3:OUTPut:STATe?` | **沈黙** | チャンネル境界=2の実測。空行再接続で0.03s復旧 |

**注意(復旧後のエラーキュー残渣):** `:SOURce3` の沈黙はエラーキューに `-100,"Command err"` を残し、再接続では消えない。次のコマンドのエラーキュー確認に混入するため、**復旧後はドレインが必要**(実装の `connect` 時 `drain_error_queue` が該当。プローブでは直後の書き込み1件に-100が混入した形で観測)。

## 2. 出力OFFのままの書き込み検証(PR-AFG1)

各項目 set → エラーキュー → readback(→ 復元)。全ステップで output=0 を維持。

| 書き込み | エラーキュー | readback | 所見 |
|---|---|---|---|
| `FUNCtion SQUare` | (-100は前段の残渣) | SQU | 適用OK |
| `FREQuency 1000` / `AMPLitude 1` / `OFFSet 0` / `PHASe 0` | No error | 期待値 | |
| `FUNCtion:SQUare:DUTY 55` | No error | 55 | 復元済み |
| `FUNCtion:RAMP:SYMMetry 55`(**Square中**) | No error | 55 | 波形非依存で保存される(エラーにならない) |
| `IMPedance FIFTy` | No error | **FIFTy** | 返却トークンは長形。復元済み(OMEG) |
| `FUNCtion DC` → `FREQuency 2000` | **-200,"Command execute failed"** | 1kHzのまま | 「DC/NOISeは周波数なし」のガイド記載を実測確認。**沈黙せず明示拒否** |
| `AMPLitude 50`(範囲外、HighZ上限20Vpp) | **No error** | **2.000000E+1** | **サイレントにクランプ、エラーキューに何も積まれない** |
| `FUNCtion PULSe`(ガイド外トークン) | **-222,"Data out of range"** | SIN(不変) | **沈黙しない**(オプション`<type>`トークンと異なり安全に失敗) |

検証後、sine / 1 kHz / 5 Vpp(ガイド既定値)へ復元し、output=0 を最終確認。

### 実装への含意

1. **範囲外値はエラーキューに現れない場合がある**(振幅クランプ)→ requested / applied の**readback比較が唯一の信頼できる検出手段**。既存の applied 返却設計で対応済み
2. 波形依存パラメータ(DUTY/SYMMetry)は波形非依存に保存され、書き込みもエラーにならない → クライアント側での波形連動検証は不要
3. DC/NOISe中の周波数書き込みは機器が `-200` で明示拒否 → クライアント側ゲート不要(set→エラーキュー確認で検出可能)
4. 不正な波形トークンは `-222` で安全に失敗(沈黙はチャンネル番号範囲外のみ)

## 3. PR-AFG1 スイート実行(2026-08-26)

- read-onlyスイート(`-m device`): **12 passed**(`test_afg_state_answers` 含む)
- write(`-m device_write -k afg`): **PASS** — `test_configure_afg_set_and_readback`: `get_afg_config` でスナップショット → sine→square / 2 kHz / 1 Vpp / duty 60 を書き込み → applied 確認 → finally で全復元。全工程で output=false を維持

## 4. PR-AFG2 出力ON実機検証・解放状態(2026-08-26)

`RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 RIGOL_TEST_ALLOW_AFG_OUTPUT=1 uv run pytest -m device_write -k afg`

- `test_enable_afg_output_open_circuit`: **PASS** — **AFG出力に何も接続しない(解放)状態**で実施。事前 output=false 確認 → `configure_afg`(sine / 1 kHz / 1 Vpp / offset 0)→ `enable_afg` の**2段階confirmフローを実機で初実行**(1回目: `USER_CONFIRMATION_REQUIRED` + confirm_token発行、コマンド送信ゼロ / 2回目: トークン消費で `:SOURce1:OUTPut:STATe ON` → readback true)→ finally `disable_afg`(confirm不要で即OFF)+ スナップショット全復元 + output=false 最終確認
- `test_configure_afg_set_and_readback`: PASS(継続)
- `test_afg_loopback_fft`: **SKIP**(`RIGOL_TEST_AFG_LOOPBACK` 未設定 — 意図どおり)

## 5. ループバックFFT・フィードバック検査(2026-08-26、BNCプローブ結線)

**物理結線:** CH1=プローブ補償 / **CH2=G1(AFG1)/ CH3=G2(AFG2)**(10xプローブ経由)/ CH4=未接続。
出力ONはユーザー承認のもと実施し、終了時に両ch出力OFF・全設定復元を確認済み。

### 公式テスト

`RIGOL_TEST_ALLOW_WRITE=1 RIGOL_TEST_ALLOW_AFG_OUTPUT=1 RIGOL_TEST_AFG_LOOPBACK=1 uv run pytest -m device_write -k afg` → **3件PASS**(`test_afg_loopback_fft` 含む。受信chは物理結線に合わせCH2へ変更)

- 実行前、本体操作で両AFG出力がONのままだったため**スイートの安全ガード(出力ON時はSKIP)が正しく発動**することも副次確認できた。`disable_afg` でOFF化後にPASS

### フィードバック検査スイープ(G1→CH2 / G2→CH3、各8点)

周波数(カウンタ+FFT)・デューティは全点で良好:

| 項目 | 結果 |
|---|---|
| 周波数 | 設定1k/100k/1MHzの全点で±1%以内(square 1k/100kは誤差ゼロ)。FFTも分解能内一致 |
| duty | 50%設定→実測50.0%、60%設定→59.94%(両ch) |
| 波形 | sine / square / ramp とも復号・測定に問題なし |
| G1/G2差 | なし |

振幅は当初「約1/9」で観測されたが、**原因は結線が10xプローブ経由**だったこと(probe_ratio=1.0で測定していたため)。`probe_ratio=10` で再検査した結果:

| 設定Vpp | G1→CH2 実測 | G2→CH3 実測 |
|---|---|---|
| 0.2 | 0.216(+8%) | 0.210(+5%) |
| 1.0 | 1.002(+0.2%) | 0.996(−0.4%) |
| 5.0 | 4.996(−0.1%) | 4.985(−0.3%) |

- **HighZ(OMEG)設定時の振幅マッピングを実測確認**: 設定Vpp = 1MΩ入力での実測Vpp(換算不要)
- 0.2 Vppの+5〜8%はプローブの小信号誤差の範囲
- 教訓: ループバック検証時は**受け側チャンネルの probe_ratio を物理経路に一致させる**こと(発生器+取得+FFT+measureの三位一体検証はこれで完結)

### 全13波形の実機検査(2026-08-26 追補)

**① FUNCtion往復(出力OFF)**: 13種全トークン(sine/square/ramp/noise/dc/arb/exp_rise/exp_fall/ecg/gaussian/lorentz/haversine/sinc)が受理され、readbackが一致。プロファイル `afg_waveforms` の全宣言が実機裏取り済みとなった。

**② ループバック実測(G1→CH2、1 kHz / 1 Vpp設定)**:

| 波形 | FFT支配ピーク | 所見 |
|---|---|---|
| exp_rise / exp_fall / gaussian / lorentz / haversine | ≈1 kHz(分解能内) | 基本波が支配。周期性◎ |
| ecg | ≈2 kHz(第2高調波) | パルス的波形の正常なスペクトル形状 |
| sinc / arb(内蔵既定) | 高域(87〜96 kHz) | 広帯域波形の特性どおり(基本波が支配的でない) |
| noise | 意味のあるピークなし | std 0.31 V・広帯域。ランダム波形として妥当 |
| dc(offset 1.0 V) | — | mean 0.948 V で追従(プローブ誤差内) |

- 特殊波形ではエッジカウンタの `frequency` が無効値 → measureのquality設計どおり `null` になることを確認(FFTが代替手段)
- 特殊波形のVpp実測は 1.36〜1.58 V(設定1 Vpp)— 波形ごとの振幅定義差・リンギングを含む観測値として記録(sine系の±0.4%一致とは性質が異なる)

## 6. 変調・ARB選択・位相同期の実機検証(2026-08-27)

### 実機quirk: MOD:STATe OFF中のパラメータ書き込みは黙って無視される

`:SOURce1:MOD:AM:DEPTh 50` を `MOD:STATe` OFFの状態で送るとエラーキューは `No error` のまま readback は既定値100(無視)。STATe ONにしてから送ると正常適用。**表示OFFチャンネルへの書き込み無視(mho98-mvp.md 3.3)と同族のquirk**。また `MOD:STATe ON` にしても `OUTPut:STATe` はOFFのまま(=変調有効化だけでは信号は出ない)ことも確認。

→ 実装は送信順を状態依存に変更(有効化はパラメータより先/無効化は最後)、パラメータのみ指定+OFF時は送信前拒否。FakeScopeにも同quirkをモデル化。

### 変調(AM)の実測 — ループバックFFTでサイドバンド確認

G1: キャリア sine 100 kHz / 1 Vpp、AM depth 100% / 変調周波数 10 kHz → CH2で取得(分解能5 kHz):

- ピーク: キャリア 97.7 kHz(0.225 V)、**下側波帯 87.9 kHz(0.117 V)**
- **側波帯/キャリア比 0.52 ≒ 理論値 0.5(depth 100%)** — AM変調が定量的に機能

### 位相同期(`sync_afg_phase`)の実測 — 2ch相対位相

G1→CH2 / G2→CH3(sine 1 kHz)。停止後の同一レコードから両chの基本波位相を算出:

| 状態 | CH2-CH3 相対位相 |
|---|---|
| sync前 | 67.5°(不定) |
| **sync後(両ch phase 0°)** | **-0.4°** |
| **G2へ phase 90° 設定 + sync** | **-89.4°** |

→ ガイドの記述どおり「周波数が同一または整数倍のとき位相が整列」を定量確認。

### deviceスイート

- `test_configure_afg_modulation_set_and_readback` / `test_sync_afg_phase`: **PASS**(quirk対応後)
- read-onlyスイート: 12件PASS(get_afg_configのmodulationキー追加を反映)
- ARBファイルロードは**実機にファイルが無いため書き込み未検証**(パス検証・SCPI列生成はFakeScopeで担保。`:LOAD:ARBitrary?` クエリ形の実機応答は確認済み扱いとしない — 要実機検証のまま)

## 7. 未実施・今後の予定

- 変調(AM/FM/PM)・ARBファイル選択(`:LOAD:ARBitrary`)・位相同期(`:PHASe:SYNChronize`)はガイド3.25.15-25/3.25.3/3.25.7の逐語解読で実装済み(FakeScope + ユニットテストのみで担保。**実機未検証**)
- `arb_file` は機器内に既知のARBファイルが無いため実機検証を意図的に見送っている(要実機検証。実機に既存ファイルを用意できたら `docs/device/test_write.py` へ追加する)
- `enabled` の変調ON→出力ONの組み合わせ(実際に変調が掛かった信号が出ているか)はループバックFFTでの実測が望ましい(未実施)
- `:PERiod` / `:VOLTage:HIGH`・`:LOW` は roadmap 2.3 のとおり恒久スキップ(`frequency_hz` / `amplitude_vpp`+`offset_v` で表現可能なため)
