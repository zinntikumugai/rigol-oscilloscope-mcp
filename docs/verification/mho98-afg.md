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

## 5. 未実施・今後の予定

- **ループバックFFT検証**: BNCケーブル入手後に実施(`RIGOL_TEST_AFG_LOOPBACK=1` を追加指定。AFG出力→CH1接続、1 kHz正弦を `analyze_waveform` のFFTで突合。テストは timebase 5 ms/div で分解能≈20 Hzを確保し、分解能アサート付き)。**現在ケーブル不足のため未実施**。実施後は本章へ結果を追記する
