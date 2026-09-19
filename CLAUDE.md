# CLAUDE.md: Instructions for Claude Code

## プロジェクト目的

USB 接続の「ポート / ケーブル / デバイス」それぞれの能力を明らかにし、ボトルネックを特定して改善提案を行うクロスプラットフォーム CLI ツール `usb-link-check` の構築。

詳細設計は **`SPEC.md` を唯一の正とする**。本ファイルと SPEC.md が矛盾する場合は SPEC.md に従い、矛盾を人間に報告すること。

---

## 最重要ルール（違反した場合、成果物は無価値になる）

### 1. 実機出力を推測で捏造しない

`system_profiler` や Windows の IOCTL 構造体の出力形式を**推測して `tests/fixtures/` に置くことを固く禁じる。**

理由: 推測した出力形式 → それに合わせたパーサー → 同じ推測で作ったフィクスチャでテスト、という循環になり、**テストが全て通るのに実機では全く動かない**成果物ができる。

守るべき手順:

- `tests/fixtures/raw/` に実機ダンプが存在する対象のみ、パーサーを実装してよい
- ダンプが無い対象は実装を進めず、`TODO: 実機ダンプ待ち（<ファイル名>）` を残して**作業を止め、人間に採取を依頼する**
- SPEC.md 4 章のキー名・構造体名は**仮の設計方針**である。実機ダンプと食い違ったら**常に実機ダンプが正**。SPEC.md 側を修正し、修正した旨を報告すること
- デバイス名からの能力推測（"Extreme SSD" だから 10Gbps 等）を実装しない

### 2. 「測れないもの」を数値で埋めない

ケーブルの能力は測定不可能である（SPEC.md 1.1）。推論できない値は `UNKNOWN` のまま出力する。それらしい数値で穴埋めするコードを書かないこと。同様に、確度が `AT_LEAST` の値を `EXACT` として表示してはならない。

### 3. シリアル番号を公開リポジトリに出さない

実機ダンプにはデバイスのシリアル番号・固有 ID が含まれる。パブリックリポジトリに機器の資産情報を出さないため:

- `tools/sanitize_dump.py` を実装し、`serial_num` / `SerialNumber` / iSerial 相当の値を `REDACTED` に置換する
- サニタイズ済みでないダンプをコミットしない
- サニタイズ漏れを検出するテストを用意する（SPEC.md 10章）

---

## 開発ガイドライン

- 言語: Python 3.10+
- パッケージ管理: `uv`（無ければ標準 `venv`）
- 依存: **OS 標準コマンドと Python 標準ライブラリのみで動作させる。** 追加パッケージは表示用の `rich` と開発用の `pytest` / `ruff` / `pytest-cov` に限る
  - Windows の Win32 API 呼び出しは `ctypes`（標準ライブラリ）で行い、`pywin32` 等の外部依存を追加しないこと
- Lint / Format: `ruff check` と `ruff format` が通ること
- 型: 全公開関数に型註釈を付けること（`mypy` は導入するが厳格モードにはしない）
- コミットメッセージ: 日本語で可。論理的な単位に分割すること
- **Git 操作を行うときは、実行前に「いま何をするコマンドか」「何が起きるか」を日本語で 1〜2 行説明してから実行すること**（利用者は Git 初心者である）

## ディレクトリ構成

リポジトリ名・ルートディレクトリ名は `usb-link-check`（ハイフン）、Python パッケージ名は `usb_link_check`（アンダースコア）とする。

```text
usb-link-check/
├── src/
│   └── usb_link_check/
│       ├── __init__.py
│       ├── cli.py              # エントリーポイント (argparse + rich)
│       ├── models.py           # LinkSpeed, Confidence, Capability, ChainElement, Diagnosis
│       ├── diagnosis.py        # SPEC.md 5章の判定ロジック（OS非依存・純関数）
│       ├── report.py           # テキスト/JSON 出力
│       └── platforms/
│           ├── base.py         # 抽象基底クラス（OSコマンド実行を抽象化）
│           ├── macos.py        # system_profiler / ioreg パーサー
│           └── windows.py      # ctypes による USB IOCTL 呼び出しとパーサー
├── tools/
│   ├── collect_dump.py         # 実機ダンプ採取スクリプト（標準ライブラリのみ・依存インストール不要）
│   └── sanitize_dump.py        # シリアル番号等のマスキング
├── tests/
│   ├── fixtures/raw/           # 実機から採取した生ダンプ（サニタイズ済み）
│   ├── test_macos_parser.py
│   ├── test_windows_parser.py
│   ├── test_diagnosis.py       # SPEC.md 5章 判定表 A1-A4 / B1-B3 と1:1対応
│   └── test_report.py
├── .github/workflows/test.yml
├── SPEC.md
├── CLAUDE.md
├── README.md
├── LICENSE
├── .gitignore
└── pyproject.toml
```

`pyproject.toml` にエントリーポイントを定義すること:

```toml
[project.scripts]
usb-link-check = "usb_link_check.cli:main"
```

---

## 必須ルール

1. **自己完結テスト**: 外部コマンド・IOCTL の実行部はスタブに差し替え可能にし、USB 機器の無い環境で `pytest` 単体で全テストが通ること。
2. **カバレッジの扱い**: 全体カバレッジ率を目標にしないこと。パーサーと判定ロジックのテストを充実させ、実行層は `# pragma: no cover` で除外する。
3. **自己修復**: `pytest` と `ruff` を自ら実行し、失敗があれば解消するまで自律的に修正すること。ただし**テストを削除・無効化して通すことは禁止**。
4. **判定表との対応**: SPEC.md 5章の判定表の各行に対し、対応するテスト関数名にその行番号（A1, B2 等）を含めること。
5. **ドキュメント**: 日本語と英語の両方で README.md を作成すること。**「ケーブル能力は測定ではなく推論である」**という本ツールの前提を必ず冒頭に明記すること。
6. **Phase 1 のスコープを守る**: SPEC.md 2.2 の対象外項目（ベンチマーク、Thunderbolt 対応、ケーブルプロファイル、`--watch`）を実装しないこと。良かれと思って追加しない。
7. **Git / 公開**: リポジトリの作成・公開は**人間の明示的な指示があるまで行わない**。`gh repo create` を勝手に実行しないこと。コミットまでは自律的に進めてよい。

---

## 進め方

作業は 2 つのゲートに分かれる。ゲートを越える判断を自分で行わないこと。

- **ゲート A（実機ダンプ待ち）**: `tools/collect_dump.py` と `tools/sanitize_dump.py` を実装したら、そこで**一度停止して人間に採取を依頼する**。パーサー実装に進まない。
- **ゲート B（公開待ち）**: 実装・テスト・README・コミットまで完了したら、そこで**停止して人間に報告する**。GitHub への公開は指示を待つ。

各ステップ完了時に進捗を簡潔に報告すること。