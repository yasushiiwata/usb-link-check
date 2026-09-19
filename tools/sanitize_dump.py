#!/usr/bin/env python3
"""実機ダンプからシリアル番号・デバイス固有 ID を REDACTED に置換する。

使い方:
    python tools/sanitize_dump.py              # tests/fixtures/raw/ 配下を一括処理
    python tools/sanitize_dump.py FILE [...]   # 指定ファイル / ディレクトリのみ
    python tools/sanitize_dump.py --check      # 書き換えず、漏れがあれば終了コード 1

- Python 標準ライブラリのみで動作する。
- 冪等: 既に REDACTED の値は数えず、何度実行しても結果は変わらない。
- 値だけを置換し、それ以外のバイト列（改行コード・空白・キー順）は保持する。
  JSON を読み込んで書き直すと system_profiler の生の書式が失われるため、
  JSON もテキストとして正規表現で処理する。

置換対象（要実機検証: 実機ダンプを見て不足があれば追加すること）:
  1. キー名に "serial" / "uuid" を含むキーの値
     (serial_num, SerialNumber, iSerialNumber, "USB Serial Number",
      kUSBSerialNumberString, volume_uuid など)
  2. 固有 ID を含むことが分かっている特定キーの値
     (UsbDeviceSignature: VID/PID/シリアル文字列を連結したバイト列)
  3. Windows のデバイスインスタンス ID / シンボリックリンク中のシリアル部分
     USB\\VID_xxxx&PID_xxxx\\<ここ>、usb#vid_xxxx&pid_xxxx#<ここ>#{guid}
     USBSTOR\\<種別>\\<ここ>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REDACTED = "REDACTED"

DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "raw"
TARGET_SUFFIXES = {".json", ".txt"}

# 部分一致でキー名を判定する語（小文字）
_SENSITIVE_KEY_SUBSTRINGS = ("serial", "uuid")
# 完全一致で判定するキー名（小文字・空白除去後）。要実機検証。
_SENSITIVE_KEYS_EXACT = {
    "usbdevicesignature",  # ioreg: シリアル文字列を含むバイト列
    "switch_uid_key",  # SPThunderboltDataType: Thunderbolt スイッチ固有 ID（要実機検証）
}


def _normalize_key(key: str) -> str:
    return key.lower().replace(" ", "").replace("_", "")


_SENSITIVE_KEYS_NORMALIZED = {_normalize_key(k) for k in _SENSITIVE_KEYS_EXACT}

# JSON 形式:  "key" : value   (system_profiler は ` : ` のように空白を入れる)
_JSON_KV = re.compile(
    r'"(?P<key>[^"\\\n]*)"(?P<sep>[ \t]*:[ \t]*)'
    r'(?P<val>"(?:[^"\\\n]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)'
)
# ioreg 形式:  "key" = value   (ネストした辞書では {"key"=value,...} と空白なし)
_IOREG_KV = re.compile(
    r'"(?P<key>[^"\n]*)"(?P<sep>[ \t]*=[ \t]*)'
    r'(?P<val>"[^"\n]*"|<[0-9A-Fa-f ]*>|0x[0-9A-Fa-f]+|-?\d+|Yes|No)'
)
# Windows インスタンス ID / デバイスパス。区切りは \ （JSON では \\）または #。
_SEP = r"(?:\\\\|\\|#)"
_WIN_INSTANCE_IDS = (
    re.compile(
        r"(?P<prefix>USB"
        + _SEP
        + r"VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:&MI_[0-9A-F]{2})?"
        + _SEP
        + r")"
        r"(?P<val>[^\\#\"\s{}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<prefix>USBSTOR" + _SEP + r"[^\\#\"\s]+" + _SEP + r")(?P<val>[^\\#\"\s{}]+)",
        re.IGNORECASE,
    ),
)


def is_sensitive_key(key: str) -> bool:
    """キー名がシリアル番号・固有 ID を表すかどうか。"""
    k = key.lower()
    if any(s in k for s in _SENSITIVE_KEY_SUBSTRINGS):
        return True
    return _normalize_key(key) in _SENSITIVE_KEYS_NORMALIZED


def _redact_kv(pattern: re.Pattern[str], text: str) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        if not is_sensitive_key(m.group("key")):
            return m.group(0)
        if m.group("val") == f'"{REDACTED}"':
            return m.group(0)
        count += 1
        return f'"{m.group("key")}"{m.group("sep")}"{REDACTED}"'

    return pattern.sub(repl, text), count


def _redact_instance_ids(pattern: re.Pattern[str], text: str) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        if m.group("val") == REDACTED:
            return m.group(0)
        count += 1
        return m.group("prefix") + REDACTED

    return pattern.sub(repl, text), count


def sanitize_text(text: str) -> tuple[str, int]:
    """テキストをサニタイズし、(置換後テキスト, 置換件数) を返す。"""
    total = 0
    for kv in (_JSON_KV, _IOREG_KV):
        text, n = _redact_kv(kv, text)
        total += n
    for pat in _WIN_INSTANCE_IDS:
        text, n = _redact_instance_ids(pat, text)
        total += n
    return text, total


def _read(path: Path) -> str:
    # 改行コードを保持し、UTF-8 でない不正バイトも失わずに往復させる
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def sanitize_file(path: Path, *, write: bool = True) -> int:
    """ファイルをサニタイズして置換件数を返す。write=False なら書き換えない。"""
    original = _read(path)
    sanitized, count = sanitize_text(original)
    if write and count:
        path.write_bytes(sanitized.encode("utf-8", errors="surrogateescape"))
    return count


def iter_target_files(paths: list[Path]) -> list[Path]:
    """対象ファイル（.json / .txt）を列挙する。"""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(
                sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in TARGET_SUFFIXES)
            )
        elif p.is_file():
            files.append(p)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="実機ダンプのシリアル番号・固有 ID を REDACTED に置換する"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=f"対象ファイル/ディレクトリ（既定: {DEFAULT_TARGET}）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="書き換えずに検査のみ行い、未サニタイズの値があれば終了コード 1",
    )
    args = parser.parse_args(argv)

    targets = args.paths or [DEFAULT_TARGET]
    files = iter_target_files(targets)
    if not files:
        print("対象ファイルがありません:", ", ".join(str(t) for t in targets))
        return 0

    total = 0
    for f in files:
        n = sanitize_file(f, write=not args.check)
        total += n
        mark = "要置換" if args.check else "置換"
        print(f"  {f}: {mark} {n} 件")

    if args.check:
        if total:
            print(f"NG: 未サニタイズの値が {total} 件あります。--check なしで実行してください。")
            return 1
        print("OK: 未サニタイズの値はありません。")
        return 0

    print(f"合計 {total} 件を {REDACTED} に置換しました（対象 {len(files)} ファイル）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
