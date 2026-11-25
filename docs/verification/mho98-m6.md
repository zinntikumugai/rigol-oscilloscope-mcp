# MHO98 M6(取り込み制御と同期)実機検証記録

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
