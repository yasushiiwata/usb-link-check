# usb-link-check

USB 接続の「ポート」「ケーブル」「デバイス」それぞれの能力を可能な範囲で明らかにし、ボトルネックを特定するクロスプラットフォーム CLI ツールです。

[English](README.en.md)

## 最初に読んでください: ケーブル能力は測定ではなく推論です

**USB ケーブルの能力は測定できません。** USB ケーブルには（USB-C の eMarker を除き）識別情報がなく、eMarker も USB Power Delivery コントローラの管轄で、OS の USB スタックからは読めません。

そこで本ツールは、次の物理的事実から**ケーブル能力を推論**します。

```
L = min(P, C, D)

L : 実際にネゴシエートされたリンク速度   … 測定できる
P : PC 側ポート（およびハブ）の能力      … 測定できる
D : デバイスの能力                       … Windows では測定できる
C : ケーブルの能力                       … 測定できない → L・P・D から推論する
```

このため本ツールは、**断定できることと、できないことを必ず区別して表示します。**

- `10 Gbps` … その速度で確定
- `5 Gbps 以上（上限不明）` … それ以上であることしか分からない
- `不明` … 全く分からない

推測した数値で穴埋めすることはしません。「不明」と表示されたら、それが本当に分からないという意味です。

## 現在の状態

- **Windows: 実装済み**
- **macOS: 未実装**（実機ダンプ待ち。推測で実装しないという方針のため）
- Linux: 非対応（実行するとエラー終了します）

## インストール

Python 3.10 以上が必要です。

```sh
pip install -e .
```

## 使い方

```sh
usb-link-check --list                    # 検出した USB デバイスを一覧表示
usb-link-check --list --all              # 空きポートも含めて全ポートの正体を表示
usb-link-check                           # USB マスストレージのデバイスを診断
usb-link-check --device 0781:5591        # VID:PID を指定して診断
usb-link-check --device "Extreme SSD"    # デバイス名の部分一致で指定
usb-link-check --json                    # JSON で出力
usb-link-check --debug                   # 取得した生データを標準エラーへ出力
```

### 一覧表示

`--list` は各デバイスの L（実効リンク速度）、P（ポート能力）、D（デバイス能力）を並べ、各行の状態を 3 値で示します。判別できない行を「問題なし」と読ませないためです。

```
    VID:PID    L(実効)     P(ポート)   D(デバイス)  位置                デバイス名
    32E6:9221  480 Mbps    5 Gbps+     480 Mbps    ハブ0/ポート6       Web Camera
⚠   346D:5678  480 Mbps    5 Gbps+     5 Gbps      ハブ0/ポート7       Acer USB Flash Drive
?   04F2:0400  1.5 Mbps    5 Gbps+     1.5 Mbps+   ハブ0/ポート12      Chicony USB Keyboard

⚠ = 能力より遅くリンクしていることが確定（L < min(P, D)）
? = 判別できません（P または D の上限が不明で、落ちているのか天井なのか分からない）
無印 = 現構成で出せる最高速で動作（L = min(P, D)）
```

`+` は「以上（上限不明）」です。`--all` を付けると、どの穴が USB3 コネクタなのかも分かります。

```
位置              種別          コネクタ  P(ポート)   コンパニオン    状態
ハブ0/ポート7     USB3コネクタ  Type-A    5 Gbps+     ポート24        DeviceConnected
ハブ0/ポート8     USB2専用      Type-A    480 Mbps    なし            NoDeviceConnected
```

`--device` を指定せず候補が 1 つに定まらない場合、**勝手に選ばず**一覧を表示して終了します。誤診断を避けるためです。

### 出力例

USB3 コネクタに USB 2.0 ケーブルでつないだ場合です。

```
現在の接続
  ポート      : ポート7（USB3コネクタ / Type-A / コンパニオン: ポート24）
                5 Gbps 以上（上限不明）
                [Port_#0007.Hub_#0001]
  ケーブル    : (識別不能)  推定 480 Mbps  ← ボトルネック
  eMarker     : なし（ポートが Type-A のため）確度: likely
  デバイス    : Acer USB Flash Drive USB Device  5 Gbps
  ──────────────────────────────────────────────────────────
  リンク速度  : USB 2.0 High-Speed (480 Mbps)

判定  [FAIL] 改善の余地があります
      （判定表 A1）
      現構成で到達しうる最高速: 5 Gbps

改善提案
  1. ケーブルを USB 3.x 対応品に交換してください。ケーブルを使わず直挿し
     している場合は、そのポートの SuperSpeed 配線に問題がある可能性があり
     ます（フロントパネル配線の未接続など）。別のポートで試してください。
     期待できる速度: 5 Gbps（確度: confirmed）
```

角括弧内は OS の内部表記です。USBView / UsbTreeView の表示と照合するために残しています。

### 終了コード

| コード | 意味 |
|---:|---|
| 0 | `OPTIMAL` — 現構成の最高速を達成している |
| 1 | `IMPROVABLE` — ボトルネックがある |
| 2 | `NOT_DETECTED` — 対象デバイスが USB ツリーに無い |
| 3 | `UNDETERMINED` — 情報不足で判定できない / 対象外の構成 |
| 4 | 実行エラー（非対応 OS、権限不足、コマンド失敗） |

## 仕組み

Windows では、Microsoft の USBView と同じ経路（`SetupDiGetClassDevs` + USB の IOCTL）を `ctypes` で直接呼びます。外部パッケージは使いません。管理者権限は不要です。

実機で確認した重要な点を 2 つ挙げます。どちらも知らないと誤診断します。

1. **`EX.Speed` は SuperSpeed を表現しません。** 5 Gbps で動作中でも `UsbHighSpeed` を返します。そのため L は `V2.Flags` を先に見て判定します。
2. **ポート番号と物理コネクタは 1 対 1 ではありません。** USB3 コネクタは USB2 と USB3 の 2 つの論理ポートとして現れます。P は両者（コンパニオンポート）の対応プロトコルの和集合で求めます。これを忘れると、ケーブル律速（A1）をポート律速（A2）と取り違えます。

詳細は [SPEC.md](SPEC.md) を参照してください。

### eMarker の推定

ポートが Type-C か Type-A かは `PortConnectorIsTypeC` から分かります。これを使って、ケーブルの eMarker 搭載の有無を**推定**します（USB Type-C 仕様では、SuperSpeed 対応の C-to-C ケーブルなどに eMarker 搭載が義務付けられています）。

| ポート | リンク速度 | eMarker | 確度 |
|---|---|---|---|
| Type-A | 問わず | なし | likely |
| Type-C | 5 Gbps 以上 | あると推定 | likely |
| Type-C | 480 Mbps 以下 | 不明 | unknown |

Type-C ポートでも相手側が変換ケーブルである可能性は排除できないため、**断定はしません**。eMarker を直接読むには USB Power Delivery の物理層が必要で、PC の USB スタックからは取得できません（Windows の UCSI については Phase 2 の調査項目です）。

### 既知の限界

コンパニオンポートが存在しても、その物理コネクタに SuperSpeed の配線が来ているとは限りません（フロントパネルの USB3 ヘッダ未接続など）。この場合 P は過大評価され、直挿しなのにケーブル律速と判定されます。ツールからは直挿しかどうかを判別できないため、提案文ではケーブルとポート配線の両方の可能性を示します。

## 実装しないもの

Phase 1 では次を実装しません。

- 実効スループット測定（MB/s）
- Thunderbolt / USB4 接続のデバイス
- ケーブルプロファイルの記録
- 挿抜監視

## 開発

```sh
pip install -e ".[dev]"
pytest          # USB 機器が無い環境でも全テストが通ります
ruff check .
ruff format --check .
```

テストは `tests/fixtures/raw/` の**実機ダンプ**を入力にしています。推測で作ったダンプは置きません。実機ダンプの採取は次のコマンドで行います。

```sh
python tools/collect_dump.py --list                                  # VID:PID を調べる
python tools/collect_dump.py --label windows_fast --device 0781:5591 # 採取する
python tools/sanitize_dump.py                                        # シリアル番号を REDACTED に置換
```

採取したダンプをコミットする前に、必ず `tools/sanitize_dump.py` を実行してください（`collect_dump.py` は採取後に自動で実行します）。

## ライセンス

MIT
