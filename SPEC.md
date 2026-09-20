# SPEC.md: USB 接続診断ツール `usb-link-check`

版数: 2.1 (Phase 1)
最終更新: 2026-09-20

---

## 1. 目的

PC と USB デバイスの間の接続について、**「ポート」「ケーブル」「デバイス」の 3 要素それぞれの能力**を可能な範囲で明らかにし、

1. 現在のリンク速度がこの構成で出せる最高速かどうかを判定する
2. 最高速でない場合、**どの要素がボトルネックか**を特定する
3. 何を変えればどこまで速くなるかを、**断定できる範囲を明示して**提案する

ことを目的とする CLI ツール。

### 1.1 設計の中核となる物理的事実

USB のリンク速度は接続経路上の最も遅い要素で決まる。

```
L = min(P, C, D)

L : 実際にネゴシエートされたリンク速度   … 測定可能
P : PC 側ポート（およびハブ）の能力      … 測定可能
D : デバイスの能力                       … 環境により測定可能
C : ケーブルの能力                       … 測定不可能（後述）
```

**ケーブルは自身の仕様を申告しない。** USB ケーブルには（USB-C の eMarker を除き）識別情報が無く、eMarker も USB Power Delivery コントローラの管轄で OS の USB スタックからは読めない。

したがって本ツールは **C を測定しない。C は L・P・D から推論する。**
「ケーブル仕様を取得する」実装を書いてはならない。

---

## 2. スコープ

### 2.1 Phase 1 で実装する（本仕様書の対象）

- L / P / D の取得
- C の推論とボトルネック特定
- 改善提案の生成（**プロトコルリンク速度 = Gbps のみ**）
- 確度（確定 / 下限 / 不明）の明示
- テキスト出力と JSON 出力、終了コード

### 2.2 Phase 1 では実装しない（明示的に対象外）

| 項目 | 扱い |
|---|---|
| 実効スループット測定（MB/s） | Phase 3。`--benchmark` は実装しない |
| デバイスのハードウェア上限評価（中の SSD/HDD の実力） | Phase 3 |
| ケーブルプロファイル記録・what-if の確度向上 | Phase 2 |
| 挿抜監視（`--watch`） | Phase 2 |
| **Thunderbolt / USB4 接続のデバイス** | **対象外。検出したら 2.3 の通り明示して終了する** |
| Linux | 非対応。実行されたら明示してエラー終了 |
| USB Power Delivery / 給電能力 | 対象外 |

### 2.3 Thunderbolt / USB4 の扱い（重要）

Thunderbolt / USB4 で接続されたデバイスは macOS の `SPUSBDataType` に現れない（`SPThunderboltDataType` 側に現れる）。これを知らずに実装すると「デバイス未検出 = 断線ケーブル」と誤判定する。

Phase 1 の要件:

- 対象デバイスが USB ツリーに見つからない場合、**「断線」と断定してはならない**
- macOS では `system_profiler SPThunderboltDataType -json` を**存在確認のためだけに**実行し、そこに該当機器があれば
  `NOT SUPPORTED: Thunderbolt/USB4 接続のデバイスは Phase 1 の対象外です` と表示して終了コード 3 で終了する
- Windows でも同等の判別ができない場合は、「未検出」ではなく「判定不能」として扱う

---

## 3. 用語とデータモデル

### 3.1 LinkSpeed

**内部表現は必ず Mbps の数値とする。** 規格名（USB 3.0 / 3.1 Gen1 / 3.2 Gen1 はすべて 5Gbps）は表示時にのみ変換する。

| 識別子 | Mbps | 表示名 |
|---|---:|---|
| `LOW_SPEED` | 1.5 | USB 1.0 Low-Speed (1.5 Mbps) |
| `FULL_SPEED` | 12 | USB 1.1 Full-Speed (12 Mbps) |
| `HIGH_SPEED` | 480 | USB 2.0 High-Speed (480 Mbps) |
| `SUPER_SPEED` | 5000 | USB 3.2 Gen1 / SuperSpeed (5 Gbps) |
| `SUPER_SPEED_PLUS` | 10000 | USB 3.2 Gen2 / SuperSpeed+ (10 Gbps) |
| `SUPER_SPEED_PLUS_2X2` | 20000 | USB 3.2 Gen2x2 (20 Gbps) |

- 比較演算（`<`, `min`）が可能なこと
- Full-Speed / Low-Speed への低下も正常に検出・表示すること（ケーブル不良で 12Mbps まで落ちる事例は実在する）

### 3.2 Confidence（確度）

本ツールの価値は「断定できることとできないことを分けること」にある。すべての能力値は確度を伴う。

| 値 | 意味 | 表示 |
|---|---|---|
| `EXACT` | その速度であると確定 | `10 Gbps` |
| `AT_LEAST` | その速度以上であることのみ判明（上限不明） | `5 Gbps 以上（上限不明）` |
| `UNKNOWN` | 全く不明 | `不明` |

### 3.3 Capability

```python
@dataclass(frozen=True)
class Capability:
    speed: LinkSpeed | None  # UNKNOWN のとき None
    confidence: Confidence
```

### 3.4 接続チェーン

経路は要素の連鎖としてモデル化する。ハブが挟まる場合は要素が増える。

```
HostController → [ExternalHub …] → Port → Cable → Device
```

```python
@dataclass
class ChainElement:
    kind: Literal["controller", "hub", "port", "cable", "device"]
    name: str  # 表示名（例: "USB 3.1 Bus", "Generic USB Hub"）
    capability: Capability
    is_bottleneck: bool = False


@dataclass
class Diagnosis:
    link_speed: LinkSpeed  # L（測定値）
    chain: list[ChainElement]
    achievable_max: Capability  # 現構成で到達しうる最高速
    verdict: Verdict  # OPTIMAL / IMPROVABLE / UNDETERMINED / NOT_DETECTED
    suggestions: list[Suggestion]


@dataclass
class Suggestion:
    target: str  # "cable" / "port" / "hub"
    action: str  # 人間向けの日本語文
    expected: Capability  # 変更後に期待できる速度と確度
    certainty: Literal["confirmed", "likely", "unknown"]
```

---

## 4. 情報取得仕様

> **警告（実装者向け）**
> 本章に書かれたコマンド名・キー名・構造体名は**設計方針**であり、実機出力による確定が済むまでは仮である。
> 実機ダンプ（`tests/fixtures/raw/`）と食い違う場合は、**常に実機ダンプが正**。
> 実機ダンプが存在しない対象については実装を進めず、`TODO: 実機ダンプ待ち` として停止し人間に報告すること。
> 推測でフィクスチャを作成することを固く禁じる（CLAUDE.md 必須ルール 1）。

### 4.1 macOS

#### L（リンク速度）
- 一次手段: `/usr/sbin/system_profiler SPUSBDataType -json`
  - `_items` が**ハブ配下に再帰的にネストする**。必ず再帰探索すること
  - 速度は `device_speed` キー。**`-json` 出力の値はテキスト出力（`Up to 10 Gb/sec`）とは表記が異なり、`high_speed` / `super_speed` / `super_speed_plus` のようなトークン形式である可能性が高い**。実機ダンプで確定させること
  - macOS のバージョンによりキー名が異なりうる。未知の値に遭遇したら黙って落とさず、`UNKNOWN` として値を保持し `--debug` で原寸表示すること
- 二次手段（フォールバック）: `ioreg -p IOUSB -l -w 0` の `Speed` 属性

#### P（ポート能力）
- 対象デバイスの**親要素**（ルートハブ／バス）を JSON ツリー上で辿り、その名称・速度表記から判定する（例: `USB 3.1 Bus` → 10 Gbps）
- 親が外部ハブであれば `ChainElement(kind="hub")` として別要素で記録する
- 判定不能な場合は `UNKNOWN` とし、推測で埋めないこと
- **P は Windows と同じく「物理コネクタの能力」と定義する（4.2「P の算出定義」参照）。**
- **要確認（未確認・実機ダンプ待ち）:** USB3 コネクタに USB2 で接続したとき、親バスが USB2 側に見えるのか USB3 側に見えるのか。
  - USB2 側に見える場合は、Windows と同じ誤判定（A2 / B2 と取り違える）が起きうる。
  - `macos_ssd_fast` と `macos_ssd_usb2` の親バス（経路）を比べて確認し、結果をここに記録する。
  - **確認が済むまで macOS の P の実装に進まない。**
  - 確認結果: （未記録）

#### D（デバイス能力）
- **Phase 1 では、macOS では原則 `UNKNOWN` として扱ってよい。**
  デバイスの SuperSpeed 対応可否は BOS ディスクリプタで判別するが、macOS の標準コマンドからは安定して取得できない。
- `ioreg` から取得できる経路が実機ダンプで確認できた場合のみ実装する。確認できなければ `UNKNOWN` のまま進める（§5.2 の D 不明ロジックが機能する）。
- **デバイス名からの推測（"Extreme SSD" だから 10Gbps 等）を実装してはならない。**

### 4.2 Windows

PowerShell の `Get-PnpDevice` ではリンク速度は取得できない。`MSUsb_DeviceInformation`（root\\WMI）はドライバ依存で空を返すことが多く、管理者権限も要る。いずれも一次手段にしない。

#### 一次手段: Win32 API を `ctypes` で直接呼ぶ（外部依存なし）

Microsoft の USBView が使っているのと同じ経路を用いる。

1. `SetupDiGetClassDevs`（GUID_DEVINTERFACE_USB_HUB）でハブを列挙
2. 各ハブを `CreateFile` で開く
3. `IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX`
   → `USB_NODE_CONNECTION_INFORMATION_EX.Speed`（0=Low, 1=Full, 2=High）。**SuperSpeed は表現されない**（下記「L の判定順序」）。
4. `IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2`
   → `USB_NODE_CONNECTION_INFORMATION_EX_V2.Flags` から **L と D の両方**を取得
   - `DeviceIsOperatingAtSuperSpeedPlusOrHigher` → 現在 10Gbps 以上で動作中
   - `DeviceIsSuperSpeedPlusCapableOrHigher` → **デバイスは 10Gbps 対応（D）**
   - `DeviceIsSuperSpeedCapableOrHigher` → **デバイスは 5Gbps 対応（D）**
   - ※「対応しているのに SuperSpeed で動いていない」がまさにフォールバック状態であり、この API はそれを直接表現できる。**Windows では D が取得できる**
5. `IOCTL_USB_GET_HUB_INFORMATION_EX` / `IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES`
   → ハブ種別（Root / USB2.0 / USB3.0）とポート属性から **P** を取得

#### L（リンク速度）の判定順序（確定・2026-09-20 実機ダンプ）

**`EX.Speed` は SuperSpeed を表現しない。** 5 Gbps で動作中のデバイスでも `EX.Speed = 2 (UsbHighSpeed)` を返す（`windows_fast` の Port24 で実測）。

L は必ず次の順序で判定すること。

1. `V2.Flags` に `DeviceIsOperatingAtSuperSpeedPlusOrHigher` → 10 Gbps 以上
2. `V2.Flags` に `DeviceIsOperatingAtSuperSpeedOrHigher` → 5 Gbps
3. それ以外は `EX.Speed` を使う（0 = 1.5 Mbps、1 = 12 Mbps、2 = 480 Mbps）

**`EX.Speed` を単独で信用してはならない。** 守らないと、すべての USB 3.x デバイスを「USB 2.0 に落ちている」と誤判定する。

`V2.Flags` のビット割り当て:

| ビット | 名前 | 状態 |
|---|---|---|
| bit0 (0x01) | `DeviceIsOperatingAtSuperSpeedOrHigher` | **実測で確定** |
| bit1 (0x02) | `DeviceIsSuperSpeedCapableOrHigher` | **実測で確定** |
| bit2 (0x04) | `DeviceIsOperatingAtSuperSpeedPlusOrHigher` | 要実機検証（10Gbps デバイス未入手） |
| bit3 (0x08) | `DeviceIsSuperSpeedPlusCapableOrHigher` | 要実機検証（10Gbps デバイス未入手） |

実測値（2026-09-20、対象 346D:5678 の USB メモリ）:

| ダンプ | 構成 | 論理ポート | `EX.Speed` | `V2.Flags` | `bcdUSB` |
|---|---|---|---|---|---|
| `windows_fast` | USB3 コネクタ直挿し、5 Gbps | 24 (`Usb300`) | 2 | `0x03` | `0x0320` |
| `windows_usb2` | USB2.0 延長ケーブル経由、480 Mbps | 7 (`Usb110\|Usb200`) | 2 | `0x02` | `0x0210` |
| `windows_port11_ss_fail` | 直挿しなのに 480 Mbps | 11 (`Usb110\|Usb200`) | 2 | `0x02` | `0x0210` |

- **`bcdUSB` はリンク速度によって変わる**（同じデバイスが 5 Gbps では `0x0320`、480 Mbps では `0x0210` を返した）。D の根拠に使ってはならない。

#### D（デバイス能力）の判定ルール（確定・2026-09-20）

| `V2.Flags` | D | 確度 |
|---|---|---|
| `DeviceIsSuperSpeedPlusCapableOrHigher` あり | 10 Gbps | `AT_LEAST` |
| `DeviceIsSuperSpeedCapableOrHigher` のみ | 5 Gbps | `EXACT` |
| どちらも無し、かつ L = 480 Mbps | 480 Mbps | `EXACT` |
| どちらも無し、かつ L < 480 Mbps | L | `AT_LEAST`（上限は 480 Mbps） |

- 最後の行を `EXACT` にしない理由: SuperSpeed 非対応であることは分かるが、High-Speed デバイスが不良ケーブルで Full-Speed に落ちている可能性を排除できないため（3.1、CLAUDE.md ルール 2）。

#### コンパニオンポートの挙動（確定・2026-09-20 実機ダンプ）

同一デバイスが、リンク速度によって別の論理ポートに現れる。

- 5 Gbps でリンクしたとき: ポート 24（`Usb300`）に現れ、コンパニオンはポート 7
- 480 Mbps でリンクしたとき: ポート 7（`Usb110|Usb200`）に現れ、コンパニオンはポート 24

P は両方の `SupportedUsbProtocols` の和集合で求める（下記「P の算出定義」）。

#### P（ポート能力）の算出定義（確定・2026-09-20）

**P は「デバイスが接続されている論理ポートの能力」ではなく、「物理コネクタの能力」と定義する。** Windows では次の式で求める。

```
P = そのポートの SupportedUsbProtocols
    ∪ コンパニオンポートの SupportedUsbProtocols
```

- コンパニオンポートは `IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES` の `CompanionPortNumber` と `CompanionHubSymbolicLinkName` で特定する。
  - コンパニオンが別のハブにある可能性がある。外部 USB3 ハブは USB2 側と USB3 側が別のハブとして列挙される見込み（要実機検証）。そのためハブを跨いで参照すること。
- 和集合から P への対応は次のとおり。

| 和集合 | P | 確度 |
|---|---|---|
| `Usb300` を含む | 5 Gbps | `AT_LEAST`（`Usb300` は 5 / 10 / 20 Gbps を区別しないため） |
| `Usb300` を含まず `Usb200` を含む | 480 Mbps | `EXACT` |
| 上記以外（空、取得失敗など） | 不明 | `UNKNOWN` |

- コンパニオンを持たないポートでは、和集合がそのポート自身の値になる。自身が `Usb110|Usb200` であれば、真の USB2 専用コネクタとして **P = 480 Mbps（EXACT）** とする。

**理由（実機で確認した挙動）:**

- USB3 コネクタに USB2 ケーブルで挿すと、デバイスは USB2 側の論理ポートに現れる。
  - 例: ポート 5 = `Usb110|Usb200`、そのコンパニオンはポート 25 = `Usb300`。
- ここで P をポート 5 の値だけで求めると、P = 480 Mbps、L = 480 Mbps となり、判定表 A2（ポート律速）と**誤判定する**。
- 実際のコネクタは USB3 対応なので、P は 5 Gbps 以上である。正しい判定は **A1（ケーブル律速）**。
- 実測（2026-09-20、Intel xHCI ルートハブ）: 同一コネクタのペアは 5↔25、6↔21、7↔24、11↔23、12↔22。

#### 既知の限界: P の過大評価（2026-09-20 実機ダンプ `windows_port11_ss_fail`）

**「コンパニオンが存在する」ことは「物理的に SuperSpeed 配線が来ている」ことを保証しない。** そのため P は過大評価されうる。

- USB メモリを Port11 に**直挿し**したにもかかわらず、480 Mbps でリンクした。
- Port11 はコンパニオン Port23（`Usb300`）を持つので、和集合ルールでは P = 5 Gbps 以上と算出される。
- しかし実際には、この物理コネクタの SuperSpeed 配線が機能していない（フロントパネルの USB3 ヘッダ未接続など）。
- その結果、ケーブルを使っていないのに A1（ケーブル律速）と判定される。

ツールからは「直挿しかどうか」を判別できない。そのため、この状況では両方の可能性を提示し、**断定しない**（5.1 A1 の提案文）。

#### 所見: P の取得手段（2026-09-20 実機試運転。確定ではなく見込み）

Intel xHCI（`USB ルート ハブ (USB 3.0)`、26 ポート）1 台での試運転結果。採取ダンプでの再確認を要する。

- **`IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2` の出力 `SupportedUsbProtocols` がポート単位の P に使える見込み。**
  - 実測値: ポート 1〜16 = `0x3`（Usb110 + Usb200 = USB2）、ポート 17〜26 = `0x4`（Usb300 = USB3）
  - 入力は USBView と同じく `Usb300` ビットのみを立てた。
- **ポート番号とコネクタの対応は 1 対 1 ではない。** USB3 コネクタは物理的には 1 つでも、USB2 の論理ポートと USB3 の論理ポートの 2 つとして現れる。この 2 つは `IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES` の `CompanionPortNumber` で互いに相手を指す。
  - 実測値: 5↔25、6↔21、7↔24、11↔23、12↔22
  - 例えば USB3 コネクタに USB2 ケーブルで挿すと、デバイスは USB2 論理ポート（1〜16 のどれか）に現れる。このため、そのポートの `SupportedUsbProtocols` だけを見ると P を USB2 と誤判定する。**P は、コンパニオンの `SupportedUsbProtocols` まで含めて判定する必要がある**（判定表 A1 と A2 を分けるための要点）。
  - コンパニオンを持たない論理ポートは、USB2 専用コネクタである見込み（ポート 1, 3, 4, 8 など。`PortIsUserConnectable` = 1）。
- ルートハブの `IOCTL_USB_GET_HUB_INFORMATION_EX` は `HubType = UsbRootHub(1)` を返し、後続のハブディスクリプタは全バイト 0 だった。**ルートハブについては、ハブ種別から P を得られない。**
- 構造体サイズは 1 バイト境界パックの前提と一致した。
  - `USB_NODE_INFORMATION` 76 バイト、`USB_HUB_INFORMATION_EX` 77 バイト、`USB_NODE_CONNECTION_INFORMATION_EX` 35 + 11 × パイプ数、`_EX_V2` 16 バイト

#### 補助（表示用のみ）
- デバイス名・VID/PID は `Get-PnpDevice` / SetupAPI から取得してよい
- **デバイス名は次の順に採用する。** 先のものが取得できないときだけ次へフォールバックする。
  1. SetupAPI の `SPDRP_FRIENDLYNAME`
  2. マスストレージのときのみ **子デバイスの `FriendlyName`**（`CM_Get_Child` で辿る）。製品名が子側に入る
     - 実測（2026-09-20）: USB デバイス側は空で、子の USBSTOR 側が `Acer USB Flash Drive USB Device`
     - マスストレージ以外で子を優先すると機能名になってしまう（実測: Bluetooth アダプタが `Bluetooth Device (Personal Area Network)`）。そのためマスストレージ以外では 3 の後に回す
  3. 文字列ディスクリプタの `iManufacturer` + `iProduct`（実測: `Acer` + `USB Device`、`Chicony` + `USB Keyboard`）
  4. `DEVPKEY_Device_BusReportedDeviceDesc`
  5. SetupAPI の `SPDRP_DEVICEDESC`（例: `USB 大容量記憶装置`）
  6. `VID:PID`
- 採用した名前の末尾が汎用的な語（`USB Device` など）で重複している場合は落として表示する
  - 例: `Acer USB Flash Drive USB Device` → `Acer USB Flash Drive`
  - 落とした結果が空になる場合や、名前そのものが汎用語だけの場合は落とさない
- 文字列ディスクリプタは `IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION` で取得する。
  - **`iSerialNumber` の文字列は要求しないこと**（機器の資産情報をダンプに残さないため。CLAUDE.md 必須ルール 3）。
  - 取得した文字列は解釈済みの値だけを記録する（生バイト列の hex に文字列が入るとサニタイズで検出できないため）。
- PowerShell を呼ぶ場合は必ず `powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ...` とし、**出力は UTF-8 を明示**（日本語環境の CP932 で文字化けする）。JSON 化は `ConvertTo-Json -Depth 5`

#### 権限
- 上記 IOCTL 経路は**管理者権限を必要としない**方式を第一候補とする
- **確定事項: Windows の全 IOCTL が管理者権限なしで成功することを実機で確認済み（2026-09-20）。**
  - 対象: `NODE_INFORMATION` / `HUB_INFORMATION_EX` / `HUB_CAPABILITIES_EX` / `NODE_CONNECTION_INFORMATION_EX` / `_EX_V2` / `PORT_CONNECTOR_PROPERTIES` / `NODE_CONNECTION_DRIVERKEY_NAME`
  - 条件: Windows 11 Pro 26200。ハブは `CreateFile(GENERIC_WRITE, FILE_SHARE_WRITE)` で開いた。
- もし管理者権限が必須と判明した場合は、起動時に明確なメッセージを出して終了コード 4 とする（黙って空の結果を返してはならない）

---

## 5. 判定ロジック（本ツールの中核）

`L = min(P, C, D)` を逆算して C を推論する。

**本章の P は「物理コネクタの能力」である**（4.2「P の算出定義」）。デバイスが現れた論理ポートの能力ではない。

論理ポートの値だけで P を求めると、次の誤判定が起きる。
- 構成: USB3 コネクタ + USB2 ケーブル + USB3 デバイス
- 論理ポートの値だけで求めた場合: P = L = 480 Mbps となり、A2（ポート律速）と判定してしまう。
- 正しくは: P は 5 Gbps 以上なので L < min(P, D) となり、A1（ケーブル律速）である。

### 5.1 D が判明している場合（主に Windows）

| # | 条件 | ボトルネック | C について言えること | 提案 | 確度 |
|---|---|---|---|---|---|
| A1 | `L < min(P, D)` | **ケーブル（確定）** | `C = L`（EXACT） | 「ケーブルを USB 3.x 対応品に交換してください。ケーブルを使わず直挿ししている場合は、そのポートの SuperSpeed 配線に問題がある可能性があります（フロントパネル配線の未接続など）。別のポートで試してください。」 | `confirmed` |
| A2 | `L = P < D` | **ポート** | `C ≥ L`（AT_LEAST） | より高速なポート/PC に変更すれば L より速くなる。ただし**上限はケーブル次第で不明**（最大 D） | `likely` |
| A3 | `L = D ≤ P` | デバイス | `C ≥ L` | 改善余地なし。デバイスが天井 | — |
| A4 | `L = P = D` | なし | `C ≥ L` | **現構成の最高速を達成済み** | — |
| A5 | `L = P の下限値 < D` かつ P が `AT_LEAST` | **ポートまたはケーブル（切り分け不能）** | `C ≥ L`（AT_LEAST） | 「既知の高速ケーブルに差し替えて再測定してください。速度が上がればケーブル、変わらなければポート側が原因です」 | `unknown` |

**A5 を設けた理由。** Windows から得られる P は `Usb300` ビットの有無しか分からず、「5 / 10 / 20 Gbps のどれか」を区別できない（4.2）。そのため P = 5 Gbps 以上（上限不明）となり、次の 2 つを区別できない構成が生じる。

- 例: L = 5 Gbps、P ≥ 5 Gbps、D ≥ 10 Gbps
  - ポートが 5 Gbps までなら A2（ポート律速）
  - ポートが 10 Gbps まで出せるならケーブルが律速で A1

A1 と A2 では提案する対処（ケーブル交換 / ポート変更）が逆になるため、どちらかに寄せると機材を買い替えた後に効果が出ないという最悪の結果になりうる。よって A5 は断定せず `UNDETERMINED`（終了コード 3）とし、B1 と同じ「実験で切り分ける手順」を渡す。

#### P が `AT_LEAST` のときの比較規則（確定・2026-09-20）

`Usb300` から求めた P は「5 Gbps 以上・上限不明」である（4.2）。上表の比較は次の規則で行う。

| 条件 | 判定 |
|---|---|
| `L < P の下限値` | 表のとおり（`L < min(P, D)` なら A1） |
| `L = P の下限値` かつ `L = D` | **A4**。`min(P, D) = D = L` なので、P の上限が不明でも最高速を達成している |
| `L = P の下限値` かつ `L < D` かつ P が `AT_LEAST` | **A5**（A1 と A2 を区別できない） |
| `L = P の下限値` かつ `L < D` かつ P が `EXACT` | A2 |

#### 能力値の min の確度（確定・2026-09-20）

`achievable_max = min(P, D)` のように能力値同士の最小値を取るときの確度は、次の一般則で求める。

| 条件 | 結果 |
|---|---|
| 片方が `EXACT` な `v`、もう片方が `AT_LEAST` な `w` で `w ≥ v` | `v`（`EXACT`） |
| 片方が `EXACT` な `v`、もう片方が `AT_LEAST` な `w` で `w < v` | `w`（`AT_LEAST`） |
| 両方 `EXACT` | 小さい方（`EXACT`） |
| 両方 `AT_LEAST` | 小さい方（`AT_LEAST`） |
| どちらかが `UNKNOWN` | `UNKNOWN` |

1 行目が重要。`w ≥ v` なら `min` は `v` に決まるので、確度を `AT_LEAST` に落としてはならない（例: P ≥ 5 Gbps、D = 5 Gbps（EXACT）なら天井は 5 Gbps で確定）。逆に 2 行目では、真の値は `w` 以上 `v` 以下のどこかなので `AT_LEAST` に落とす。

**A1 の文言に注意。** 直挿しでも P の過大評価（4.2「既知の限界」）により A1 になりうる。「ケーブルが原因です」と断定せず、ケーブル交換とポート変更の両方を提示すること。

**A2 の文言に注意。** 「ポートを変えれば 10Gbps 出ます」と断定してはならない。ケーブル能力は `L 以上`としか分かっていない。断定するとユーザーが機材を買い替えた後に「ケーブルのせいで速くならない」という最悪の結果になる。

### 5.2 D が不明な場合（主に macOS）

| # | 条件 | 結論 | 提案 |
|---|---|---|---|
| B1 | `L < P` | **ケーブルまたはデバイスが律速（切り分け不能）** | 「既知の高速ケーブルに差し替えて再測定してください。速度が上がればケーブル、変わらなければデバイス側が原因です」 |
| B2 | `L = P` | **ポートが律速の候補**（C ≥ L, D ≥ L） | 「より高速なポートで再測定してください」 |
| B3 | `P` も不明 | **判定不能** | 現在値のみ表示し、終了コード 3 |

B1 の提案文は「実験によって切り分ける手順」を提示している点が重要。測れないものを推測で埋めず、ユーザーに次の一手を渡す。

### 5.3 ハブが挟まる場合

チェーン上に外部ハブ H があるとき、上流側の実効能力は `min(P, H)` とする。
`L = H < min(P, D)` の場合はハブがボトルネックであり、提案は「デバイスを PC に直結してください」とする。

### 5.4 eMarker の推定（確定・2026-09-20 / Type-C 側は要実機検証）

ポートのコネクタ形状は `USB_PORT_PROPERTIES` の `PortConnectorIsTypeC`（bit3 = 0x08）で判別する。これを使って**ケーブルの eMarker 搭載の有無を推定**する。

USB Type-C 仕様では、次のケーブルに eMarker 搭載が義務付けられている。

| ケーブル | eMarker |
|---|---|
| Full-Featured Type-C（C-to-C で SuperSpeed 対応） | 必須 |
| 5A 対応 | 必須 |
| USB4 / Thunderbolt | 必須 |
| USB 2.0 のみの C-to-C | 任意 |
| A-to-C、A-to-A | 搭載しない |

推定ルール:

| ポートのコネクタ形状 | L | eMarker | 確度 |
|---|---|---|---|
| Type-A | 問わず | **なし** | `likely` |
| Type-C | 5 Gbps 以上 | **あると推定** | `likely` |
| Type-C | 480 Mbps 以下 | **不明** | `unknown` |
| 不明 | 問わず | **不明** | `unknown` |

表示例:

```
eMarker     : なし（ポートが Type-A のため）確度: likely
eMarker     : あると推定（Type-C ポートで 5Gbps 以上でリンクしているため、
              USB Type-C 仕様上 eMarker 搭載が必須のケーブルに該当）確度: likely
```

**断定してはならない。** `EXACT` を使わない。Type-C ポートでも、相手側が Micro-B などの変換ケーブルである可能性を排除できない。必ず「推定」であることを明示する。

- **要実機検証（Type-C ポートを持つ機体で確認）。** 開発機（Intel xHCI）のポートはすべて Type-A で `PortConnectorIsTypeC` が未設定のため、Type-C 側の分岐は実機で検証できていない。
- **eMarker を直接読む手段について。** eMarker の読み取りには USB Power Delivery の物理層が必要で、PC の USB スタックからは取得できない。Windows には UCSI（USB Type-C Connector System Software Interface）が存在するが、ユーザーモードからの公開 API は確認できていない（**Phase 2 の調査項目**）。確実な読み取りは PD コントローラを搭載した別デバイス側の役割とする。

### 5.5 achievable_max（現構成で到達しうる最高速）

- D 判明時: `min(P, D)`（ケーブルを理想としたときの上限）
- D 不明時: `UNKNOWN`（`P` を上限値として `AT_LEAST` 表示はしない。P より速い D があるとは限らないため、`最大 P`と注記するに留める）

---

## 6. 出力仕様

### 6.1 テキスト出力（既定 / `rich` 使用）

```
現在の接続
  ポート      : USB-C (USB 3.2 Gen2x2)          20 Gbps
  ケーブル    : 推定 5 Gbps                      ← ボトルネック
  デバイス    : SanDisk Extreme SSD              10 Gbps
  ─────────────────────────────────────────────
  リンク速度  : 5 Gbps (USB 3.2 Gen1)

判定  [FAIL] 本構成の最高速 10 Gbps に対し 5 Gbps で接続中

改善提案
  1. ケーブルを 10Gbps 対応品に交換  → 10 Gbps  （確実・約2倍）
  2. ポート変更                      → 効果なし（ポートは既に十分）
  構成変更後の理論上限               : 10 Gbps（デバイス能力が天井）
```

- 確度が `AT_LEAST` の値には必ず「以上（上限不明）」を付記すること
- 確度が `UNKNOWN` の値は「不明」と表示し、**数値を推測で埋めない**
- 色: OPTIMAL=緑 / IMPROVABLE=黄 / UNDETERMINED=灰 / NOT_DETECTED=赤
- 到達しうる最高速のラベルは「**現構成で到達しうる最高速**」とする（改善提案と矛盾して見えないようにするため）
- **ポートのコネクタ形状（Type-C / Type-A）と eMarker の推定を表示する**（5.4）
- ポートは**人間が読める形と OS の内部表記の両方**を表示する。内部表記は USBView / UsbTreeView との照合に使う。
  - 例: `ポート24（USB3コネクタ / コンパニオン: ポート7） [Port_#0024.Hub_#0001]`
- **日本語の折り返しは単語境界ではなく表示幅で行う**（`rich` の既定の折り返しでは「より 高速な ポート」のように不自然に切れる）。行頭に句読点・閉じ括弧を置かないこと。

### 6.1.1 `--list` の表示

一覧を見るだけで問題のあるデバイスが見つかることを目的とする。

- 各行に L（実効リンク速度）、P（ポート能力）、D（デバイス能力）を表示する
- **各行の状態は 3 値で示す。** 2 値（警告 / 無印）にすると、判別できない行が「問題なし」と読めてしまうため。

**判定基準は D（デバイス能力）とする。** `min(P, D)` ではない。`--list` が答えるべき問いは「どのデバイスに改善の余地があるか」であり、ポートが律速しているデバイス（判定表 A2）も挿す穴を変えれば速くなるため、改善の余地があるものとして拾う必要がある。

| マーク | 条件 | 意味 |
|---|---|---|
| `⚠` | `L < D` が確定している | 改善の余地がある |
| `?` | `D` が `AT_LEAST` / `UNKNOWN` で、`L < D` かどうか判別できない | デバイスが天井なのか落ちているのか分からない |
| （無印） | `D` が `EXACT` で `L = D` | デバイス自身の上限で動作している（これ以上速くならない） |

  - 例: キーボードが `L = 1.5 Mbps`、`D = 1.5 Mbps 以上` のとき、本当に 1.5 Mbps が上限なのか落ちているのかは判別できない。この行は `?` とする。
  - 凡例を出力に含めること。「測れないものは測れないと言う」方針を `--list` でも一貫させる。
- **`--list` の印と `--device` の判定が構造的に食い違わないこと。** 次の対応をテストで固定する。
  - 無印 ⟹ `OPTIMAL`（`L = D` なので判定表 A3 / A4。改善の余地なし）
  - `⚠` ⟹ `OPTIMAL` にはならない（`L < D` が確定しているため）
  - 例外として `⚠` かつ `UNDETERMINED` になる構成がある（判定表 A5: `L < D` は確定だが、ポートとケーブルのどちらが律速かを区別できない）。改善の余地があることは確定しているので `⚠` でよい。
- `--all` を併用すると、**デバイスが接続されていないポートも一覧する**（その穴が USB3 コネクタか USB2 専用かが分かるようにする）

### 6.2 JSON 出力（`--json`）

```json
{
  "schema_version": 1,
  "timestamp": "2026-09-20T12:00:00+09:00",
  "os": "darwin",
  "device": { "name": "SanDisk Extreme SSD", "vid": "0x0781", "pid": "0x5591" },
  "link_speed_mbps": 5000,
  "chain": [
    { "kind": "port",   "name": "USB 3.2 Gen2x2 port", "speed_mbps": 20000, "confidence": "exact",    "is_bottleneck": false },
    { "kind": "cable",  "name": "(unidentifiable)",    "speed_mbps": 5000,  "confidence": "exact",    "is_bottleneck": true  },
    { "kind": "device", "name": "SanDisk Extreme SSD", "speed_mbps": 10000, "confidence": "exact",    "is_bottleneck": false }
  ],
  "achievable_max_mbps": 10000,
  "achievable_max_confidence": "exact",
  "connector_type": "type_c",
  "emarker": { "state": "likely_present", "certainty": "likely", "reason": "…" },
  "verdict": "IMPROVABLE",
  "suggestions": [
    { "target": "cable", "action": "10Gbps対応ケーブルへ交換", "expected_mbps": 10000, "certainty": "confirmed" }
  ]
}
```

- `speed_mbps` が不明のときは `null`、`confidence` は `"unknown"`
- `connector_type` は `"type_c"` / `"type_a"` / `"unknown"`、`emarker.state` は `"likely_present"` / `"absent"` / `"unknown"`（5.4）。**`emarker` に `exact` を使わない**
- `--json` 指定時は `rich` による装飾出力を一切行わないこと（標準出力は JSON のみ）

### 6.3 終了コード

| コード | 意味 |
|---:|---|
| 0 | `OPTIMAL` — 現構成の最高速を達成 |
| 1 | `IMPROVABLE` — ボトルネックあり（改善余地あり） |
| 2 | `NOT_DETECTED` — 対象デバイスが USB ツリーに存在しない |
| 3 | `UNDETERMINED` — 情報不足で判定不能 / 対象外構成（Thunderbolt 等） |
| 4 | 実行エラー（非対応 OS、権限不足、コマンド失敗） |

---

## 7. CLI 仕様

```
usb-link-check [OPTIONS]

  --device <SPEC>   対象デバイスの指定。"VID:PID"（例: 0781:5591）または
                    デバイス名の部分一致文字列。
                    未指定時: USB マスストレージクラスのデバイスを対象とし、
                    複数該当した場合は一覧を表示して終了コード 3 で終了する
                    （「直近接続」のような曖昧な自動選択は実装しない）
  --list            検出した USB デバイスを全件一覧表示して終了（終了コード 0）
                    各行に L / P / D を表示し、L < min(P,D) の行に ⚠ を付ける（6.1.1）
  --all             --list と併用したときのみ有効。デバイスが接続されていないポートも
                    一覧し、そのポートの正体（USB3 コネクタ / USB2 専用 / コンパニオン）を表示する
  --json            結果を JSON で出力
  --debug           取得した生のコマンド出力／構造体値をそのまま標準エラーへ出力
  --version         バージョンを表示
  -h, --help        ヘルプ
```

- `--device` 未指定で候補が 1 つに定まらない場合、**勝手に選ばない**こと。曖昧な自動選択は誤診断の温床になる。

---

## 8. エラー処理

- 非対応 OS（Linux 等）: `ERROR: このツールは macOS と Windows のみ対応しています` → 終了コード 4
- 外部コマンドの失敗・タイムアウト（既定 10 秒）: 原因を明示して終了コード 4
- 未知の速度値に遭遇: 例外で落とさず `UNKNOWN` として扱い、`--debug` で原寸を出せること

---

## 9. テスト仕様

### 9.1 フィクスチャ

- `tests/fixtures/raw/` に**実機から採取した生ダンプ**のみを置く。**AI が推測で作成したダンプを置いてはならない。**
- 採取は `tools/collect_dump.py --label <label> --device <VID:PID>` で行う。
  - ファイル名は `<label>.<種別>.<拡張子>` の形で、`--label` から機械的に決まる。主データだけは `<label>.json` とする。
  - 対象デバイスの接続先（ハブ番号・ポート番号・経路・コンパニオン）は、`<label>.meta.json` の `target` に記録される。Windows では `<label>.json` の `target` にも記録される。これが P の判定根拠になる。

最低限そろえる実機ダンプ:

| label | 構成 | 期待する判定 |
|---|---|---|
| `macos_ssd_fast` | 高速ケーブル | ケーブルを律速と判定しない（§10） |
| `macos_ssd_usb2` | USB 2.0 ケーブル | B1 |
| `windows_fast` | USB3 コネクタ直挿し、5 Gbps（採取済み） | A4（OPTIMAL） |
| `windows_usb2` | USB3 コネクタ + USB2.0 延長ケーブル、480 Mbps（採取済み） | ケーブル律速（A1） |
| `windows_port11_ss_fail` | USB3 コネクタ（Port11）直挿しなのに 480 Mbps（採取済み） | A1。ただし提案文にポート側 SS 配線の可能性を含むこと（4.2「既知の限界」） |

- `windows_fast` / `windows_usb2` / `windows_port2` は、文字列ディスクリプタと子デバイス名を含む形で再採取済み（2026-09-20）。
- **`windows_port11_ss_fail` は意図的に旧形式（文字列ディスクリプタ・子デバイス名なし）のまま残す。** デバイス名の解決が末尾までフォールバックする経路（`USB Device` に落ちる経路）を実データで検証するために使う。再採取しないこと。
| `windows_port2` | USB2 専用コネクタ（Port8、コンパニオンなし）に直挿し、480 Mbps（採取済み） | ポート律速（A2） |
| `windows_hub` | 外部 USB ハブ経由 | ハブ律速（5.3）。**TODO: 実機ダンプ待ち**。現在のフィクスチャ 4 件はいずれもルートハブ直結のため、5.3 のハブ経路は実機データで未検証 |

`windows_port2` に使うポートの注意: デバイスが USB2 論理ポート（1〜16 側）に現れても、そのポートが USB2 専用だとは限らない。4.2「所見」のとおり、USB3 コネクタに USB2 ケーブルで挿した場合（`windows_usb2`）も 1〜16 側に現れるため、A1 と A2 の区別はコンパニオンの有無で行う。

label ごとに生成されるファイル:

| ファイル | 内容 | OS |
|---|---|---|
| `<label>.json` | `system_profiler SPUSBDataType -json` の標準出力そのまま | macOS |
| `<label>.thunderbolt.json` | `system_profiler SPThunderboltDataType -json`（Thunderbolt 判別用） | macOS |
| `<label>.ioreg.txt` | `ioreg -p IOUSB -l -w 0` | macOS |
| `<label>.usbhost.json` | `system_profiler SPUSBHostDataType -json`（`-listDataTypes` に存在する macOS のみ） | macOS |
| `<label>.json` | SetupAPI + USB IOCTL の採取結果。解釈した値と生バイト列 (hex) を併記する | Windows |
| `<label>.meta.json` | 採取環境、各項目の成否、対象デバイスの接続先 (`target`) | 共通 |

- **サニタイズ必須**: 採取ダンプに含まれるシリアル番号・固有 ID は `REDACTED` に置換してからコミットすること（パブリックリポジトリに機器の資産情報を出さない）。サニタイズは `tools/sanitize_dump.py` として実装し、自動化すること。

### 9.2 テスト方針

- **パーサー層（`platforms/*.py` の解析関数）: 用意した実機フィクスチャ全件に対するテストを必須とし、分岐網羅 100% を目標とする**
- **判定ロジック（`diagnosis.py`）: §5 の判定表 A1〜A5 / B1〜B3 の各行に対応するテストを 1 件以上持つこと**（表とテストが 1:1 対応していること）
- **P の算出定義（4.2）: 実機フィクスチャを使ったテストを必ず持つこと。**
  - `windows_fast`（USB3 コネクタ直挿し、5 Gbps）→ A4 と判定されること。**L が 480 Mbps と誤判定されないこと**（4.2「L の判定順序」の検証）
  - `windows_usb2`（USB3 コネクタ + USB2 ケーブル）→ A1 と判定されること
  - `windows_port11_ss_fail`（直挿しで SS 配線不良）→ A1 と判定され、提案文にポート側の可能性が含まれること
  - `windows_port2`（USB2 専用コネクタ）→ A2 と判定されること
- **`windows_usb2`（A1）と `windows_port2`（A2）が別の判定になることを検証するテストを必ず持つこと。**
  - どちらも L = 480 Mbps、D = 5 Gbps で、P が 5 Gbps か 480 Mbps かという一点だけで区別される。
  - ここが本ツールの中核であり、退行を即座に検出できるようにする。
  - テスト関数名に行番号（A1 / A2）を含めること
  - 合成データではなく、必ず実機フィクスチャで検証すること。コンパニオンを誤って扱っても合成データのテストは通ってしまうため
- **subprocess / ctypes の実行層は `# pragma: no cover` で除外する。** 全体カバレッジ率を目標値にしない（グルーコードの水増しテストを誘発するため）
- OS コマンド実行部は抽象基底クラス（`platforms/base.py`）で抽象化し、テストではフィクスチャを返すスタブに差し替えること。USB 機器が無い CI 上で全テストが通ること

### 9.3 CI

- `.github/workflows/test.yml` を作成し、`ubuntu-latest` / `macos-latest` / `windows-latest` × Python 3.10 / 3.12 のマトリクスで `pytest` と `ruff` を実行すること
- `gh` の認証スコープに `workflow` が必要

---

## 10. 受け入れ基準（Definition of Done）

すべて自動またはコマンド一発で検証できる形であること。

- [ ] `pytest` が全 OS の CI でパスする（USB 機器なしで完走する）
- [ ] `usb-link-check --version` がバージョンを出力し終了コード 0
- [ ] `usb-link-check --help` が §7 の全オプションを表示する
- [ ] フィクスチャ `macos_ssd_usb2.json` を入力したとき、判定が `IMPROVABLE`、ボトルネックが `cable` または「ケーブルまたはデバイス」、終了コード 1 になる
- [ ] フィクスチャ `macos_ssd_fast.json` を入力したとき、判定が `OPTIMAL` または `IMPROVABLE`（ポート律速）となり、**ケーブルをボトルネックと判定しない**
- [ ] `--json` 出力が §6.2 のスキーマに適合する（スキーマ検証テストがある）
- [ ] D が `UNKNOWN` のフィクスチャで、出力に「不明」が表示され、**推測値が入らない**
- [ ] 判定表 §5 の A1〜A5 / B1〜B3 すべてに対応するユニットテストが存在する
- [ ] フィクスチャ `windows_usb2.json` を入力すると A1（ケーブル律速）、`windows_port2.json` を入力すると A2（ポート律速）と判定される（P の算出定義 4.2 の検証）
- [ ] フィクスチャ `windows_fast.json` を入力すると L = 5 Gbps、A4（OPTIMAL）と判定される（`EX.Speed` を単独で信用しないことの検証）
- [ ] フィクスチャ `windows_port11_ss_fail.json` を入力すると A1 と判定され、提案文にケーブル交換とポートの SS 配線の両方の可能性が含まれる
- [ ] `tests/fixtures/raw/` 内にシリアル番号が残っていないことを検査するテストがある
- [ ] Linux 上で実行すると終了コード 4 とメッセージが出る
- [ ] README.md（日英）に、**「ケーブル能力は測定ではなく推論である」**という前提が明記されている
