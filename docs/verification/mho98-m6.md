# MHO98 M6(取り込み制御と同期)/ M7(自動監視)/ M8(時刻とメタ情報)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**手順規律:** [mho98-unlicensed.md](mho98-unlicensed.md) と同じ(1コマンド → 応答/エラーキュー確認 → 記録)
**参照ガイド:** MHO98 Programming Guide PGA45102-1110(2026-02-26版、対象ソフトウェア 00.01.00)

## 1. 取得モード(issue #52、2026-11-25)

### 1.1 読み取りプローブ

| コマンド | 応答 | エラーキュー |
|---|---|---|
| `:ACQuire:TYPE?` | `NORM` | No error |
| `:ACQuire:AVERages?` | `2` | No error |
| `:ACQuire:BITS?` | `14` | No error |
| `:ACQuire:MDEPth?` | `1.0000E+04` | No error |

応答は全て短形。どのモードでも `:BITS?` は読める。

### 1.2 書き込みプローブ(最後に元の値へ戻した)

| 状態 | コマンド | エラーキュー | 読み戻し |
|---|---|---|---|
| NORM | `:ACQuire:BITS 14`(現在値と同じ) | **`-200,"Command execute failed"`** | — |
| NORM | `:ACQuire:AVERages 4` | No error | `4` |
| AVER | `:ACQuire:AVERages 100` | **`-200,"Command execute failed"`** | (変化なし) |
| AVER | `:ACQuire:AVERages 128` | No error | `128` |
| AVER | `:ACQuire:AVERages 65536` | No error | `65536` |
| AVER | `:ACQuire:BITS 14` | **`-200`** | — |
| HRES | `:ACQuire:BITS 16` | No error | `16` |
| HRES | `:ACQuire:BITS 14` | No error | `14` |
| HRES | `:ACQuire:AVERages 4` | No error | `4` |
| PEAK | `:ACQuire:BITS 14` | **`-200`** | — |

### 1.3 ガイドとの食い違い(実装へ反映済み)

1. **平均回数の非2のべき乗は切り下げではなく `-200` で拒否される。** ガイド3.3.1 の Remarks「2のべき乗でない値は、それより小さい最も近い2のべき乗が自動入力される(9 → 8)」は前面パネルのテンキー入力の話で、SCPIには当てはまらない。→ `configure_acquisition` は送信前に `INVALID_PARAMETER` で弾く
2. **`:ACQuire:BITS` は high_resolution モードでしか書けない**(現在値と同じ値でも `-200`)。ガイド3.3.5 は「高分解能モードの分解能」と書くだけで、他モードで書き込みを拒否するとは書いていない。→ 送信前に有効モードを判定して弾く(`mode` 未指定なら `:ACQuire:TYPE?` を1回だけ読む)。送信順は `TYPE → AVERages → BITS` で固定
3. 平均回数は **average 以外のモードでも書き込める**(NORM / HRES で受理)。設定は保持され、average へ切り替えたときに効く

### 1.4 スイート実行

- read-only(`-m device tests/device/test_readonly.py`): **20 passed, 1 skipped**(`test_acquisition_config_answers` を含む)
- write(`-m device_write -k acquisition`): **7 passed**。4モードの往復、平均回数65536の往復、high_resolution + 16bit の往復。全テストで復元漏れゼロ(復元は「high_resolution で分解能を戻す → 元のモードと平均回数を戻す」の2段)

## 2. 強制トリガと取り込み完了の同期(issue #53、2026-11-25)

### 2.1 `*OPC?` は取り込み完了を待たない

| 手順 | 応答 | 所見 |
|---|---|---|
| `:SINGle` | (write)No error | |
| `*OPC?` | **`1`**(25ms) | トリガ待ちのままでも即答する |
| `:TRIGger:STATus?` | `WAIT` | 取り込みは終わっていない |

→ `*OPC?` は「コマンドの受理が終わった」ことしか示さず、取り込みの完了同期には使えない。**ロードマップ1.3の前提(`*OPC?` で完了を申告させる)は成り立たない。** 代替として `single` / `force_trigger` に `wait_s`(`:TRIGger:STATus?` を STOP までポーリング)を実装した。

### 2.2 `:TFORce` の効き方(0.25秒間隔で `:TRIGger:STATus?` を観測)

| 状態 | 操作 | 観測された状態の推移 |
|---|---|---|
| 掃引 SINGle | `:SINGle` | `STOP` → `WAIT` → `WAIT` …(トリガ待ちが続く) |
| 同上 | `:TFORce` | `WAIT` → **`STOP`**(2回目の観測=約0.25秒後) |
| 掃引 NORMal | `:SINGle` → `:TFORce` | `WAIT` … → 即 `STOP` |
| 連続取り込み(WAIT) | `:TFORce` | `TD`(1フレーム取り込む。停止はしない) |

いずれもエラーキューは No error。

### 2.3 付随して判明したこと

- **`:SINGle` は掃引モードを `SINGle` に書き換える**(`:TRIGger:SWEep?` → `SING`)。プローブ後に `AUTO` + `:RUN` へ戻した。device_write の fixture も掃引モードと取り込み状態を復元する
- **トリガレベルは画面範囲へクランプされる**: CH1 50mV/div で `:TRIGger:EDGE:LEVel 10` → 読み戻し `2.250000E-1`。「レベルを信号の外へ置いてトリガさせない」手は使えないため、テストでは成立し得ないパルス幅条件(5〜10秒)で WAIT を作った

### 2.4 待機中の割り込み(PR #59 レビュー対応の実機確認)

サーバーと同じく Tool 全体をロックで囲んだ状態で、成立しないトリガ条件の `single(wait_s=5)` を待機させ、0.5秒後に別スレッドから `force_trigger(wait_s=2)` を打った:

- `force_trigger` → `completed: true` / `STOP`
- 待機していた `single` → **`interrupted: true`** / `trigger_status: WAIT` / `completed: false`。全体の所要 **0.89秒**(5秒を待たずに返った = 待機側はロックを手放していた)
- 割り込み直後の `single` 側の観測は **まだ WAIT**(STOP への遷移に約0.25秒かかる)。呼び出し側は `completed` ではなく `interrupted` で「別の操作が割り込んだ」と判断する必要がある

### 2.5 スイート実行

- write(`-m device_write -k force_trigger`): **2 passed**(割り込みテストを含む)。`single(wait_s=0.5)` → `completed: false` / `WAIT`、`force_trigger(wait_s=2.0)` → `completed: true` / `STOP`。トリガ種別・掃引モード・取り込み状態の復元漏れゼロ(パルス種別配下の幅設定は既存のトリガテストと同じく復元対象外)

## 3. 機器時刻(issue #54、2026-11-25)

| コマンド | 応答 | エラーキュー |
|---|---|---|
| `:SYSTem:DATE?` | `2026,11,26` | No error |
| `:SYSTem:TIME?` | **`02:18:54`**(コロン区切り) | No error |

- **V-3 解決**: `:SYSTem:TIME?` はコロン区切り。ガイド3.24.5 の set 形(`16,10,17`)はカンマ区切りなので、パーサは両方を受理する
- 観測時のホスト時刻は `2026-11-25 17:18`(UTC)。read-onlyスイートでの実測は **`time_skew_s: 32428.6` = +9時間 + 約29秒**(JSTに設定された機器と、UTCのホスト。残り29秒は時計のドリフト)。機器の時計はタイムゾーンを持たないため、`clock_check` はずれが15分単位の倍数から120秒以内ならタイムゾーン違い(+ドリフト秒数)を示唆する。当初の許容10秒ではこの実機の値を取りこぼしたため広げた
- 時計の**設定はしない**(機器のグローバル状態の書き換え。#10)
- read-only(`-m device tests/device/test_readonly.py`): **21 passed, 1 skipped**(`test_device_clock_answers` を含む)

## 4. 機器メタ情報(issue #55、2026-11-25)

| コマンド | 応答 | エラーキュー | 所見 |
|---|---|---|---|
| `:SYSTem:RAMount?` | `4` | No error | 実アナログch数。プロファイル宣言値と一致 |
| `:AUToset:ENAble?` | `1` | No error | AUTO機能 有効 |
| `:AUToset:LOCK?` | `0` | No error | ロックなし |
| `:SYSTem:MODules?` | `1,0,0,0,0` | No error | ガイド3.24.15: 1桁目=LA、2桁目=DG。**実態と合わない**(下記) |

- **`:SYSTem:MODules?` は使わない。** LAプローブ未接続の状態で LA=`1`、AFGを内蔵し実機で出力も確認済みなのに DG=`0`。プローブの有無でもAFGの有無でもない何かを示しており、判定材料にならない(V-2 の後半は「判定できない」で決着)
- autoset の可否は、**承認後・実行前**に読む。autoset 自体は実機での実行が禁止なので、無効時に `:AUToset` を送ったときの機器の反応は観測していない
- read-only(`-m device tests/device/test_readonly.py`): **22 passed, 1 skipped**(`test_hardware_info_answers` を含む)

## 5. イベント検索(issue #56、2026-11-25)

### 5.1 読み取りプローブ(初期状態)

`:SEARch:STATe?` → `0` / `:MODE?` → `EDGE` / `:COUNt?` → `0` / `:EVENt?` → `0` / `:EDGE:SLOPe?` → `POS` / `:EDGE:SOURce?` → `CHAN1` / `:EDGE:THReshold?` → `0.000000` / `:PULSe:POLarity?` → `POS` / `:PULSe:QUALifier?` → `GRE` / `:PULSe:UWIDth?` → `2.000000E-6` / `:PULSe:LWIDth?` → `1.000000E-6` / `:PULSe:THReshold?` → `0.000000`。いずれも No error。

### 5.2 **沈黙を1回踏んだ**(V-4 の核心)

検索有効・イベント0件の状態で **`:SEARch:VALue? 0` を送るとSCPIサーバーが沈黙**した(5秒タイムアウト)。再接続(open時の空行)で `*IDN?` が応答し復旧。エラーキューには **`-200,"Command execute failed"` が1件**残っていたのでdrainした。変更していた設定(CH1スケール・時間軸・検索のしきい値と有効状態)はすべて元へ戻した。

→ **範囲外の `VALue?` は沈黙を招く。** `get_search_results` は `1..COUNt?` の範囲しか読まず、件数が読み出し中に変わりうる取り込み中は読まない(`SAFETY_POLICY_DENIED`)。FakeScope も範囲外で沈黙するようにした。

### 5.3 応答形式(V-4 解決)

CH1 = 校正信号(1kHz、約0.3Vpp)、CH1 0.1V/div、200µs/div、しきい値0.2V:

| 条件 | `:SEARch:COUNt?` | `:SEARch:VALue? n` |
|---|---|---|
| edge / rising | `2` | `1` → `-9.324000E-4`、`2` → `6.760000E-5`(1ms間隔) |
| edge / either | `4` | `-8.542000E-4` / `-3.546000E-4` / `1.458000E-4` / `6.454000E-4`(0.5ms間隔) |
| edge / rising、5ms/div | `50` | 50件を連続で読んで **1301ms(約26ms/件)** |
| pulse / greater 100µs | `50` | `-2.430500E-2`、`-2.330500E-2` … |

- `VALue? n` は**1始まりのイベント番号**で、**トリガ点基準の時刻(秒、科学表記)**を返す
- `:SEARch:EVENt 2` の後の `:EVENt?` が `1` のままだった(移動が読み戻しに反映されない)。使わない
- 停止(`:STOP`)後も `COUNt?` は同じ値を返す
- 1V/div・しきい値1V(信号振幅0.4Vの外)では当然0件

### 5.4 スイート実行

- write(`-m device_write -k search`): **2 passed**。edge検索で2件を取得し間隔が1ms(±50µs)であることを確認、取り込み中の読み出しが `SAFETY_POLICY_DENIED` になることを確認。検索設定(両種別)・CH1スケール・時間軸・掃引モード・取り込み状態の復元漏れゼロ

## 6. Pass/Fail(マスク)試験(issue #57、2026-11-25)

### 6.1 読み取りプローブ(初期状態)

`:MASK:ENABle?` → `0` / `:SOURce?` → `CHAN1` / `:OPERate?` → `STOP` / **`:X?` → `2.400000E0`** / **`:Y?` → `4.800000E0`** / `:FAILed?` `:PASSed?` `:TOTal?` → `0` / `:OUTPut:ENABle?` → `0`。いずれも No error。

### 6.2 書き込みプローブ(最後に元の値へ戻した)

| コマンド | エラーキュー | 読み戻し | 所見 |
|---|---|---|---|
| `:MASK:X 0.28` | No error | `2.800000E-1` | |
| `:MASK:X 2.8` | No error | `2.800000E0` | ガイドの上限 2 div を超えても受理 |
| `:MASK:X 0.005` | No error | `1.000000E-1` | 最小付近は丸められる |
| `:MASK:Y 0.36` | No error | `4.000000E-1` | 同上 |
| `:MASK:CREate`(試験 無効) | **`-200`** | — | 有効化が前提。沈黙はしない |
| `:MASK:ENABle 1` → `:X 0.5` / `:Y 0.5` → `:CREate` → `:OPERate RUN` | No error | | |
| 1秒ごとに `TOTal?` / `PASSed?` / `FAILed?` / `:TRIGger:STATus?` | | `4,3,1,STOP` のまま | **不合格が出た時点で取り込みが止まった** |
| `:MASK:OPERate STOP` | No error | カウンタは `4,3,1` のまま | 停止ではカウンタは消えない |
| `:MASK:RESet` | No error | `0,0,0` | |

- 余白の値: **ガイドの既定値(0.24 / 0.48 div)の10倍が初期値として読める**一方、0.28 を書けば 0.28 が読み戻る。単位がガイドの記述(div)どおりかは判断できなかった。ホスト側で値域を決め打ちせず、read-back を返す
- 不合格による停止: マスク試験そのもの(`:MASK:OPERate?` → `STOP`)と取り込み(`:TRIGger:STATus?` → `STOP`)の両方が止まる。本体の Stop on Fail 相当と思われるが、ガイドに対応するSCPIは無い
- 停止中に `:RUN` を送ると、直後の `:TRIGger:STATus?` はまだ `STOP` のことがある(device_write の復元fixtureは最大1秒待つ)

### 6.3 スイート実行

- write(`-m device_write -k "mask or force_trigger or search"`): **5 passed**。マスク試験は CH1 の校正信号で `total=1, failed=1`(不合格で停止)を観測し、`total == passed + failed` を確認。無効時の `:MASK:CREate` が `-200` になることを再確認。マスク設定・掃引モード・取り込み状態の復元漏れゼロ


## 7. 実機テスト全体の通し実行(2026-11-26)

#52〜#57 のマージ後、既存分も含めた実機テスト(`-m "device or device_write"`)を初めて通しで流した。

- 1回目: **74 passed / 2 failed / 2 errors / 5 skipped**
  - `test_configure_cursor_round_trip` / `test_configure_histogram_round_trip`: 固定の位置(±200µs・±0.5V など)を書いていたが、機器は**画面範囲外の位置を黙って丸める**(2µs/div で -200µs → -50µs、50mV/div で -0.5V → -0.2V)。**作業開始前のコミット(54a1061)でも再現**し、今回の変更とは無関係
  - ヒストグラムの復元判定: 元の値 -10ns を書き戻すと **-100ns に丸められる**(機器の分解能)。厳密一致の判定で復元漏れとみなしていた
  - `test_configure_trigger_every_type_round_trips[spi]`: 単独では通る。ヒストグラムの後片付けが崩れた影響とみられる
- 対応(issue #64): 検証位置を今の画面範囲(中心±2〜3div)から決め、範囲の復元判定は 1/10div の誤差を許容した
- 2回目: **76 passed / 5 skipped / 失敗0**

## 8. メモリ深さ(issue #66、2026-11-26)

| コマンド | エラーキュー | 読み戻し | 所見 |
|---|---|---|---|
| `:ACQuire:MDEPth 1000` | No error | `1.0000E+03` | |
| `:ACQuire:MDEPth 1M` | No error | `1.0000E+06` | 短縮表記も受理 |
| `:ACQuire:MDEPth 250000000` | No error | **`1.2500E+08`** | ch数で決まる上限へ**無言で丸め** |
| `:ACQuire:MDEPth 500000000` | No error | **`1.2500E+08`** | 同上 |
| `:ACQuire:MDEPth AUTO` | No error | **`2.0000E+04`** | **AUTO の読み戻しは実際の深さ**。AUTO かどうかは区別できない |
| `:ACQuire:MDEPth 12345` | **`-222`** | (変化なし) | 一覧外。沈黙はしない |

- 上限は表示中のch数で決まる(ガイド3.3.2)。4ch表示で 125M、**2ch表示(CH1+CH2)で 250M** を確認した
- write(`-k acquisition`): **8 passed**(1M の往復、500M → 上限への丸めを表示ch数から期待値を計算して確認)。復元漏れゼロ(AUTO だったかは読めないため数値で戻す)

## 9. チャンネルの追加属性(issue #67、2026-11-26)

CH2 で1項目ずつ書き込み → 読み戻し → 元へ戻した(いずれも No error):

| 項目 | 初期値 | 書き込み → 読み戻し |
|---|---|---|
| `:INVert` | `0` | `1` → `1` |
| `:VERNier` | `0` | `1` → `1` |
| `:UNITs` | `VOLT` | `AMPere` → **`AMP`**(短形) |
| `:LABel:SHOW` / `:LABel:CONTent` | `0` / `CH2` | `TEST2` → `TEST2` |
| `:POSition`(バイアス) | `0.000000E+00` | `0.1` → `1.000000E-01`(`:OFFSet?` は `0` のまま。オフセットとは別の値) |
| `:TCALibrate`(遅延補正) | `0.000000` | `2e-8` → `2.000000E-8` |

- read-only: `test_channel_extras_answer` で CH1 の追加属性を読めることを確認
- write(`-k extras`): **1 passed**。7項目をまとめて往復し、復元漏れゼロ

## 10. 実機テストの戻し忘れ点検(issue #76、2026-11-26)

各テストの前後で主要な状態(ch表示・トリガ種別/ソース/レベル/掃引・時間軸・XY・メモリ深さ・取得モード・測定統計表示・CH1/CH2 スケール)を比べる一時プラグインを差し込んで、実機テストを通しで流した。

- 戻し忘れは **`test_configure_decode_parallel_user_bus_set_and_readback` の1本だけ**: CH3 の表示を OFF → ON のまま残していた。パラレルのビットソースにchを指定すると、機器がそのchの表示を自動でONにするため
- 対応: デコードの fixture で、走査より前に全アナログchの表示状態を控え、ビットソースを読み直す走査の**後**で戻す
- 対応後の通し実行: **76 passed / 5 skipped、戻し忘れ0件**

## 11. 水平軸の拡張(issue #68、2026-11-26)

### 11.1 プローブ(最後に元の値へ戻した)

| コマンド | 結果 |
|---|---|
| `:TIMebase:DELay:ENABle 1` → `:SCALe 5e-7` → `:OFFSet 2e-6` | 読み戻し `5.000000E-7` / `2.000000E-6` |
| `:TIMebase:DELay:ENABle 0` | 直後の `:ENABle?` は**約1秒**かかる |
| `:TIMebase:HREFerence:MODE USER` → `:POSition 60` | `USER` / `60` |
| `:TIMebase:VERNier 1` | `1` |
| **`:TIMebase:MODE XY`** | **No error だが `:MODE?` は `MAIN` のまま**。代わりに `:TIMebase:XY:ENABle?` が `1` になり、画面に XY ウィンドウが開いた(`:XY:ENABle 0` で閉じた)。XY は扱わない |
| **`:TIMebase:MODE ROLL`** | `ROLL`。**時間軸が 50ms/div に書き換わり、メモリ深さも変わる**。`:MODE MAIN` に戻しても時間軸は 50ms/div のまま |
| `:TIMebase:ROLL 0` / `1` | `0` / `1`(自動ROLL。{0\|1} の整数) |

遅延掃引が**無効のままでも** `:DELay:SCALe 1e-6` / `:OFFSet 1e-6` は受理され、読み戻しにも反映される(無視されない。PR #79 レビュー時に確認)。一方、無効時の遅延掃引のスケール・オフセットは前回の値が残っているだけで(例: 2µs/div のメインに対して 20µs/div)、今の時間軸では範囲外のことがある。

### 11.2 V-6(遅延掃引中の `:WAVeform:DATA?`)

CH1 0.1V/div・メイン 200µs/div で停止し、遅延掃引を 20µs/div・オフセット 200µs にした:

| 状態 | 点数 | 時間間隔 | 取り込み開始位置 |
|---|---|---|---|
| ズームOFF(NORMal) | 1000 | 2.0E-6 | -1.0E-3 |
| ズームON(NORMal) | 1000 | 2.0E-6 | **-1.0E-3**(変わらない) |
| ズームON(RAW) | 10000 | 2.0E-7 | -1.0E-3 |

→ **ズーム中もメイン時間軸の全体を返す。** ズーム区間だけの再取得には使えない。

### 11.3 スイート実行

- read-only: `test_timebase_extras_answer`
- write(`-k "timebase_extras or roll_mode"`): **2 passed**。微調整・水平基準・遅延掃引の往復と、ROLL + スケール指定(モードを先に送るのでスケールが最終値になる)。前後比較の戻し忘れ0件

## 12. CAN-FD デコード(issue #69、2026-11-26)

`:SYSTem:OPTion:STATus? CAN-FD` → `1`(導入済み)。

| コマンド | エラーキュー | 読み戻し | 所見 |
|---|---|---|---|
| `:BUS1:CAN:FDBaud?` / `:FDSPoint?` | No error | `1000000` / `80` | 初期値 |
| `:BUS1:CAN:FDBaud 2000000` | No error | `2000000` | |
| `:BUS1:CAN:FDSPoint 70` | No error | `70` | |
| **`:BUS1:CAN:FDBaud 12345678`** | **No error** | **`12345678`** | ガイドの上限 10Mbps を超えても**受理される**。ホスト側で 1〜10Mbps を検証する |

プローブで変えた値は元に戻した。

- 未ライセンスの実機は FD 項目の読み取りに値を返しつつ `-222` を積む([mho98-unlicensed.md](mho98-unlicensed.md))。そのため FD 項目は、プロファイルの `decode_can_fd` 宣言と CAN-FD オプションが揃ったときだけ読み書きする
- write(`-k can_fd`): **1 passed**。2Mbps / 70% を往復し、CAN 配下の値を元に戻した。前後比較の戻し忘れ0件

## 13. オートセットの挙動設定(issue #70、2026-11-26)

初期値: `:AUToset:PEAK?` → `1` / `:OPENch?` → `0` / `:OVERlap?` → `1` / `:KEEPcoup?` → `0`。4項目とも反転して書き込み → 読み戻し → 元へ戻した(すべて No error)。autoset の実行は行っていない。

- write(`-k autoset_options`): **1 passed**。4項目を反転して往復し、元へ戻した。前後比較の戻し忘れ0件

## 14. :MEASure 系統の周波数カウンタと測定ヒストグラム(issue #71、2026-11-26)

CH1 0.1V/div・200µs/div(校正信号 1kHz、`:MEASure:ITEM? VPP` → `3.2499E-01`)で確かめ、最後に元へ戻した:

| コマンド | 応答 | 所見 |
|---|---|---|
| `:MEASure:COUNter:ENABle 1` → `:VALue?`(1秒ごとに4回) | **`9.90E+37`** のまま | 有効な信号でも値が出ない。**見送る** |
| `:MEASure:HISTogram:ENABle 1` → `:STATistics:RESult?`(1秒ごと) | `[["92","1","0","Vpp","0","9hits","1hits","325.9mV",...]]` → `"25hits"` → `"43hits"` | ヒット数が増えていく。直前の測定項目(Vpp)が対象 |

応答の各位置の意味はガイドに無い。6〜15番目を `:HISTogram`(ラベル付き)の13ラベルと照合した:

| 位置 | 値の例 | 対応するラベル |
|---|---|---|
| 3 | `Vpp` | 測定項目 |
| 5 / 6 | `43hits` / `8hits` | Sum / Peaks |
| 7 / 8 / 9 | `327.3mV` / `323mV` / `4.296mV` | Max / Min / Pk_Pk(327.3 − 323 = 4.3 で整合) |
| 10 / 11 / 12 | `324.7mV` / `324.5mV` / `324.2mV` | Mean / Median / Mode |
| 13 / 14 | `32.3uV` / `1.11mV` | Bin width / Sigma |
| 0 / 1 / 2 / 4 / 15 | `92` / `1` / `0` / `0`〜`4` / `3.23mV` | **不明**(`unlabeled` に生で返す) |

- **対象の項目**: 測定結果ビューに既に Frequency などが並んでいると、Vpp を測った後でも対象は Frequency のままだった(他の測定テストと続けて流して判明)。`clear_measurements` で全消去してから目的の項目だけを測ると、その項目が対象になる
- **ヒットが溜まる条件**: 対象の測定値が有効であること。CH1 50mV/div では校正信号(約0.3V)がはみ出して Vpp が測定不能になり、ヒットは0のままだった
- 実機テストでは**利用者の測定項目を消さない**(結果ビューの項目一覧を読む手段が無く、消すと元へ戻せないため。PR #82 のレビュー指摘)。そのため対象の項目は機器次第になり、テストは応答の構造と、ヒットがあるときの Max − Min = Pk_Pk だけを確かめる。ヒットが溜まること自体は、全消去したうえでの確認(上の表)で裏取りした
- write(`-k measurement`): **4 passed**。前後比較の戻し忘れ0件

## 15. 設定の一括保存・復元(issue #72、2026-11-26)

| 操作 | 結果 |
|---|---|
| `:SYSTem:SETup?` | **3044バイトのバイナリ**(54ms)。ガイドの「ASCII」ではない。中身は zlib 圧縮らしい区画が並ぶ |
| 時間軸を 1e-5 に変えてから、読んだブロックを `:SYSTem:SETup #43044...` で書き戻す | No error。約1.7秒後に時間軸が **2e-6(元の値)に戻った** |

ヘッダの構造(保存したファイルで確認):

| バイト | 中身 |
|---|---|
| 0〜3 | **9バイト目以降の CRC32**(リトルエンディアン)。設定が変わると変わる(`b95392df` → `c4258c1e` を観測)ので、固定の識別子ではない |
| 4〜7 | **全体長 − 8**(リトルエンディアン。3044 → `0x0bdc` = 3036) |

→ 復元前に CRC32 と長さを照合し、合わないデータ(別のファイル・壊れたファイル)は送らない。当初は先頭4バイトを固定の識別子とみなす実装だったが、実機の2回目の保存で値が変わり、CRC32 だと判明した。

- write(`-k restore_setup`): **1 passed**。保存 → 時間軸を5倍 → 承認つきで復元 → 時間軸が元の値。前後比較の戻し忘れ0件

## 16. ホスト側解析の拡充(issue #73、2026-11-26)

CH1 の校正信号(1kHz 矩形波)を 0.1V/div で解析した(設定は最後に元へ戻した)。

| 時間軸 | 結果 | 所見 |
|---|---|---|
| 200µs/div(2周期) | THD **183%**、`jitter` は計算不可 | **THD の不具合**: 基本波のビンが2で、高調波の探索範囲が基本波と重なり、基本波を2次として数えた → 約12周期未満では計算しないよう修正 |
| 2ms/div(約20周期) | 基本波 **999.99Hz**、THD **44.3%**、周期 **1.0000ms**、周期ジッタ 0.11µs、トップ 0.311V | 理想の矩形波の THD(10次まで)は約45%で妥当 |
| 同上 | ノイズ 44.7mV(標準偏差の推定) → **7.2mV**(中央絶対偏差の推定) | **ノイズの不具合**: エッジの段差が隣接差に入り、標準偏差では過大だった → 中央絶対偏差に変更 |

- write(`-k host_analyses`): **1 passed**。前後比較の戻し忘れ0件
- セルフレビュー後の再確認(2026-11-26。THD の0詰め補正、ヒストグラムの2つの平坦部によるトップ・ベース、ジッタのヒステリシス): 画面データ1000点(2ms/div)で 基本波 **0.196V**(振幅 0.317V の矩形波の理論値 0.202V)、THD **43.6%**、周期 1.0000ms、周期ジッタ 0.11µs。**1 passed**、戻し忘れ0件。途中で試した「2のべき乗への切り詰め」は 1000点が 512点(約10周期)になって THD を計算できなくなったため採らなかった。Copilot の指摘(幅1%のパルス+0.5%の3Vスパイク)を受けて境界を大津の方法から「最頻ビンと、5%以上離れた次点のビン」に変えた後も同じ値(トップ 0.311V / ベース −0.007V、周期 1.0000ms)で **1 passed**

## 17. オプション必須のデコード・トリガ(issue #74、2026-11-26)

`:SYSTem:OPTion:STATus?` → `AUDio` / `FLEX` / `AERO` いずれも `1`(導入済み)。信号源が無いので、検証は設定の往復まで。

### 17.1 **沈黙を1回踏んだ**(デコードのしきい値)

BUS2 を IIS モードにして **`:BUS2:THReshold? I2SClk`**(ガイド3.4.9 に載っているトークン)を送ると、**`-222` を積んで沈黙**した。再接続で復旧し、エラーキューを drain して BUS2 をパラレルへ戻した。残りのトークンは1つずつ、毎回復旧つきで確かめた:

| モード | トークン | 結果 |
|---|---|---|
| FLEXray | `FLEX` | `0.000000`(応答) |
| M1553 | `1553` | `0.000000` |
| IIS | `DATA` / `WS` | `0.000000` |
| IIS | **`I2SClk`** | **沈黙**(ガイドの誤り) |
| IIS | `CLK`(SPI と共通) | `0.000000` → **I2S のクロックしきい値はこれを使う** |

### 17.2 読み取りプローブ

- デコード: IIS / FLEXray / M1553 の配下はガイドどおりの値を返した(`:BUS2:MODE?` は `IIS` / `FLEX` / `M1553`)
- トリガ: 扱う全項目(FlexRay 9 / I2S 13 / 1553 9)を1項目ずつ読み、すべて No error。`:TRIGger:MODE?` は `FLEX` / `IIS` / `M1553`

### 17.3 I2S トリガのユーザー幅

`data_bits=16` で `data=1000` を書くと **15** に丸められた。比較に使う幅は `:TRIGger:IIS:UWIDth`(4〜WIDTh、既定4)で、これが4のままだと 2^4−1 に丸められる。→ `user_bits` として扱い、`data` より前に送る。

### 17.4 スイート実行

- write(`-k "option_decode or option_trigger"`): **6 passed**(3プロトコル × デコード/トリガ)。各配下を控えて元へ戻し、前後比較の戻し忘れ0件

- セルフレビュー後の再確認(2026-11-26): FlexRay トリガの `frame_type="null"`(YAML の素の `null` キーが None になっていた不具合を修正)を書き込み → 読み戻し `null` → 元へ戻した(No error)。`-k "decode or option or i2s or flexray or m1553"` **12 passed**、戻し忘れ0件
- 機器の不具合の記録: この確認の前に、BUS1 だけ「データソース USER → `:BITX` 書き込み → `:SOURce?`」で `-232` を積んで沈黙する状態になった(BUS2 は同じ手順で正常、BUS2 でオプション3種の設定往復をしても再現せず、`:SYSTem:SETup` の復元でも直らなかった)。**機器の再起動で解消**し、その後は同じテストを続けて流しても再発しない。原因は不明

## 18. ロジックアナライザ(issue #86、2026-11-26)

検証用 ESP32 ボード(rigol-oscilloscope-mcp_check-esp32)を D0〜D15 に接続した状態で確かめた(ボードは `idle`。信号を使う検証は issue #87)。

| 操作 | 結果 | 所見 |
|---|---|---|
| `:LA:ENABle?` / `:ACTive?` / `:AUTosort?` / `:SIZE?` / `:POD<n>:DISPlay?` / `:POD<n>:THReshold?` / `:DIGital:ENABle? D<n>` / `:DIGital:LABel? D<n>` | `1` / `D0` / `D15D0` / `MED` / `1` / `0.000000` / `1` / `D0` | 11本すべて No error |
| `:LA:POD1:THReshold 1.65` | 読み戻し `1.650000E0` | |
| `:LA:DIGital:LABel D0,TX` | 読み戻し `TX` | 引数の区切りは空白なしの `,` で通る |
| 16ch 有効のまま `:LA:SIZE LARGe` | **`-222`** | ガイドどおり8ch以下のときだけ。POD2 を消すと通る |
| D15 を無効にして `:LA:ACTive D15` | **受理**(読み戻し `D15`) | ガイドの「有効なchだけ」と違う |
| `:LA:POD2:DISPlay OFF` → `ON` | D8〜D15 が揃って 0 → 1 | **ポッドの表示はその8chをまとめて切り替える**。ch1本を ON にしてもポッドの表示は 0 のまま |
| `:LA:ENABle OFF` → `ON` | 無効のあいだポッド・chは全て 0、有効に戻すと元の状態 | 無効のあいだは元の状態を読めない |
| `:WAVeform:SOURce D0` / `D1`(RUN・STOP、NORMal・RAW) | **`-200`** | デジタル波形の点列は取り出せない。`LA` は `-222` |

- write(`-k logic_analyzer`): **1 passed**。しきい値 1.65V・ラベル・ch無効化・アクティブch・並び順を書いて読み戻し、元の状態へ戻して一致を確認

## 19. 検証ボードでロジックアナライザ経由の観測(issue #87、2026-11-26)

検証用 ESP32 ボード(rigol-oscilloscope-mcp_check-esp32、ファームウェア 0.1.0)を D0〜D15 に接続。ボードの HTTP API でモードを切り替え、返ってくる期待値・`scope_hint`(`configure_decode` の引数)と、MCP で読んだデコード結果を突き合わせた。LA のしきい値は 1.65V(テスト後に元の値へ戻した)。

| モード | ソース | 時間軸 / トリガ | 結果 |
|---|---|---|---|
| SPI 1MHz mode0 | CLK D4 / MOSI D5 / CS D6 | 10µs/div、D6 立ち下がり | ✅ `A5 5A 00 FF`(期待どおり) |
| I2C 100kHz | SCL D2 / SDA D3 | 200µs/div、D3 立ち下がり | ✅ アドレス `50`(Write・NACK)、データ `DE AD BE EF`。**50µs/div では窓が足りず後半のバイトが切れた**(`DE` だけ / 100µs/div で `DE AD BE`) |
| CAN 500k | D7(`signal_type: tx`) | 50µs/div、D7 立ち下がり | ✅ ID `123`・DLC 8・`01 02 03 04 05 06 07 08`、`Ack Error`(相手ノードが無いので正常) |
| パラレル 1kHz(8bit カウンタ) | CLK D0、データ D8〜D15(D8=bit0) | 1ms/div、D0 立ち上がり | ✅ ただし下記のビット順 |
| PWM 1kHz | D0 | — | ✅ 周波数カウンタ(ソース D0)で 1kHz |
| UART / LIN | D1 | — | ⏭ **ボードが出力できない**(`last_error: ESP_FAIL`、`running: false`。何度設定し直しても同じ) |

見つかった不具合・所見:

- **MCP の不具合**: I2C のイベントテーブルの種別トークンは **`I2C`**(モード名は `IIC`)で、「unknown decoding type」と警告していた → 対応表を追加して修正。SPI / CAN / PARALLEL / RS232 はモード名のまま解釈できる
- **パラレルのビットの重みは `bus` と `endian` の組み合わせ**: ボードの `scope_hint`(`bus: d15_d8`、`endian: msb`)ではビット反転した値(`CF 2F AF 6F …` = `F3 F4 F5 F6 …` の反転)になった。`d15_d8` + `lsb` と `d8_d15` + `msb` で正しい値(`BC BD BE …`)。つまり **`msb` なら後に書かれたch、`lsb` なら先に書かれたchがMSB**。従来の説明(「先に書かれた側がMSB」)を直した。ボード側の `scope_hint` も同じ誤りを含む
- ボードの `scope_hint.note` は「MCP は LA を設定できないので前面パネルで」と書いているが、issue #86 で `configure_logic_analyzer` が使えるようになった
- write(`-k board`): **5 passed, 2 skipped**(UART / LIN はボードの出力不可で skip)。LA・デコード・トリガ・時間軸・カウンタは各フィクスチャで元へ戻し、前後比較の戻し忘れ0件

