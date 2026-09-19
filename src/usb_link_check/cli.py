"""エントリーポイント (argparse + rich)。SPEC.md 7章。"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from typing import Any

from . import __version__
from .models import EXIT_ERROR, Capability, Diagnosis
from .platforms.base import UsbPlatform
from .report import print_report, render_device_list, to_json

_LINUX_MESSAGE = "ERROR: このツールは macOS と Windows のみ対応しています"
_MACOS_MESSAGE = (
    "ERROR: macOS 版は未実装です（実機ダンプ待ち）。"
    "tools/collect_dump.py でダンプを採取してください。"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usb-link-check",
        description="USB 接続のポート / ケーブル / デバイスの能力からボトルネックを特定する",
    )
    parser.add_argument(
        "--device",
        metavar="SPEC",
        help='対象デバイス。"VID:PID"（例: 0781:5591）またはデバイス名の部分一致。'
        "未指定時は USB マスストレージクラスのデバイスを対象とする",
    )
    parser.add_argument(
        "--list", action="store_true", help="検出した USB デバイスを全件一覧表示して終了"
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出力")
    parser.add_argument(
        "--debug", action="store_true", help="取得した生データをそのまま標準エラーへ出力"
    )
    parser.add_argument("--version", action="version", version=f"usb-link-check {__version__}")
    return parser


def _make_platform(system: str) -> UsbPlatform | None:  # pragma: no cover - OS 依存
    if system == "Windows":
        from .platforms.windows import WindowsPlatform

        return WindowsPlatform()
    return None


def run(
    args: argparse.Namespace,
    usb: UsbPlatform,
    *,
    out: Any = None,
    err: Any = None,
) -> int:
    """解析済みの引数と情報取得層から結果を出力する（OS 非依存・テスト可能）。"""
    out = out or sys.stdout
    err = err or sys.stderr

    if args.debug:
        print(json.dumps(usb.load(), ensure_ascii=False, indent=2, default=str), file=err)

    if args.list:
        print(render_device_list(usb.devices()), file=out)
        return 0

    candidates = usb.select(args.device)
    if not candidates:
        return _not_detected(args, usb, out)
    if len(candidates) > 1:
        # 曖昧な自動選択はしない（SPEC.md 7章）
        print("対象デバイスが 1 つに定まりません。--device で指定してください:", file=out)
        print(render_device_list(candidates), file=out)
        return 3

    diagnosis = usb.diagnose(candidates[0])
    if args.json:
        print(to_json(diagnosis, usb.os_name), file=out)
    else:
        print_report(diagnosis, file=out)
    return diagnosis.exit_code


def _not_detected(args: argparse.Namespace, usb: UsbPlatform, out: Any) -> int:
    """対象が見つからない場合。「断線」と断定しない（SPEC.md 2.3）。"""
    diagnosis = Diagnosis(
        link_speed=None,
        chain=[],
        achievable_max=Capability.unknown(),
        verdict="NOT_DETECTED",
    )
    if args.json:
        print(to_json(diagnosis, usb.os_name), file=out)
    else:
        print("対象デバイスが USB ツリーに見つかりませんでした。", file=out)
        print(
            "ケーブル断線と断定はできません。"
            "Thunderbolt / USB4 接続のデバイスは Phase 1 の対象外です。",
            file=out,
        )
    return diagnosis.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    system = platform.system()
    if system == "Darwin":
        print(_MACOS_MESSAGE, file=sys.stderr)
        return EXIT_ERROR
    if system != "Windows":
        print(_LINUX_MESSAGE, file=sys.stderr)
        return EXIT_ERROR

    usb = _make_platform(system)
    if usb is None:  # pragma: no cover - 上の分岐で到達しない
        print(_LINUX_MESSAGE, file=sys.stderr)
        return EXIT_ERROR
    try:
        return run(args, usb)
    except OSError as e:  # pragma: no cover - 実機でのみ発生
        print(f"ERROR: USB 情報の取得に失敗しました: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
