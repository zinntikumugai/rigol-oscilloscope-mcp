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
