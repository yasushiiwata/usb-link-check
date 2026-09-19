#!/usr/bin/env python3
"""実機ダンプ採取スクリプト（Python 標準ライブラリのみ・追加インストール不要）。

使い方:
    python tools/collect_dump.py --list        # 接続中の USB デバイスと VID:PID を一覧表示
    python tools/collect_dump.py --label macos_ssd_fast --device 0781:5591
    python tools/collect_dump.py --label windows_fast --device 0781:5591

保存先: tests/fixtures/raw/<label>.*（--out-dir で変更可）

--device（VID:PID）で指定した対象デバイスについて、接続先のハブ番号・ポート番号・
経路を必ず記録し（Windows は <label>.json と meta の "target"、macOS は meta の "target"）、
画面の要約の先頭に表示する。USB2 / USB3 どちらのポートに挿したかを後から判別するため。

  macOS
    <label>.json              system_profiler SPUSBDataType -json の標準出力そのまま
    <label>.thunderbolt.json  system_profiler SPThunderboltDataType -json（Thunderbolt 判別用）
    <label>.ioreg.txt         ioreg -p IOUSB -l -w 0 の標準出力そのまま
    <label>.usbhost.json      SPUSBHostDataType が存在する macOS のみ（要実機検証。下記参照）
    <label>.meta.json         採取環境・各項目の成否・エラー内容

  Windows
    <label>.json              SetupAPI + USB IOCTL の採取結果（解釈値と生バイト列 hex を併記）
    <label>.meta.json         採取環境・各項目の成否・エラー内容

採取後は自動で tools/sanitize_dump.py を適用し、シリアル番号等を REDACTED に置換する。
一部の項目が失敗しても中断せず、失敗内容を記録して続行する。

Windows の採取処理（ctypes / IOCTL）と解析処理は、パッケージ側の
src/usb_link_check/platforms/win32_raw.py と windows.py にある（二重実装を避けるため）。
本スクリプトはそれらを読み込んで使う。macOS の採取は本スクリプト内で完結する。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import traceback
import unicodedata
from pathlib import Path
from typing import Any

COLLECTOR_VERSION = "0.1.0"  # 採取スクリプト自体の版（パッケージ版数とは別）
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "raw"
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
COMMAND_TIMEOUT_SEC = 120  # system_profiler は機器が多いと遅いので長めに取る


# =====================================================================
# 共通
# =====================================================================


def validate_label(label: str) -> str:
    """ラベルをファイル名として安全な文字列に限定する。"""
    if not LABEL_RE.match(label):
        raise ValueError(f"--label には英数字と _ . - のみ使用できます（先頭は英数字）: {label!r}")
    return label


_DEVICE_SPEC_RE = re.compile(r"^(?P<vid>[0-9A-Fa-f]{1,4}):(?P<pid>[0-9A-Fa-f]{1,4})$")


def parse_device_spec(spec: str) -> tuple[int, int]:
    """ "VID:PID"（16 進）を (vid, pid) に変換する。"""
    m = _DEVICE_SPEC_RE.match(spec.strip())
    if not m:
        raise ValueError(f"--device は VID:PID（16 進、例: 0781:5591）で指定してください: {spec!r}")
    return int(m.group("vid"), 16), int(m.group("pid"), 16)


class Context:
    """採取 1 回分の状態（保存ファイル・成否・警告）を保持する。"""

    def __init__(
        self, label: str, out_dir: Path, vid: int, pid: int, *, quiet: bool = False
    ) -> None:
        self.label = label
        self.out_dir = out_dir
        self.quiet = quiet  # --list 用: 成功した項目の進捗表示を省く
        self.vid = vid
        self.pid = pid
        # 対象デバイスの接続位置（P の判定根拠）。meta と要約に必ず出す。
        self.target: dict[str, Any] = {"device": f"{vid:04X}:{pid:04X}", "matches": []}
        self.items: list[dict[str, Any]] = []
        self.saved: list[Path] = []
        self.warnings: list[str] = []
        self.summary: list[str] = []
        self.platform: dict[str, Any] = {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": sys.version,
        }
        self.is_admin: bool | None = None

    def path(self, suffix: str) -> Path:
        return self.out_dir / f"{self.label}{suffix}"

    def record(self, name: str, ok: bool, **info: Any) -> None:
        self.items.append({"name": name, "status": "ok" if ok else "failed", **info})
        if ok and self.quiet:
            return
        mark = "[OK]  " if ok else "[失敗]"
        detail = f" -> {info['file']}" if ok and "file" in info else ""
        if not ok and info.get("error"):
            detail = f": {info['error']}"
        print(f"  {mark} {name}{detail}")

    def info(self, message: str) -> None:
        if not self.quiet:
            print(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"  [注意] {message}")

    def write_bytes(self, suffix: str, data: bytes) -> Path:
        p = self.path(suffix)
        p.write_bytes(data)
        self.saved.append(p)
        return p

    def write_json(self, suffix: str, obj: Any) -> Path:
        text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
        return self.write_bytes(suffix, text.encode("utf-8"))

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["status"] != "ok"]

    def check_target(self) -> None:
        """対象デバイスが一意に見つかったかを判定し、見つからなければ警告する。"""
        n = len(self.target["matches"])
        if n == 0:
            self.warn(
                f"対象デバイス {self.target['device']} が見つかりませんでした。"
                " 接続と VID:PID を確認して再採取してください（ハブ/ポート番号が記録されません）。"
            )
        elif n > 1:
            self.warn(
                f"対象デバイス {self.target['device']} が {n} 台見つかりました。"
                " 採取対象以外の同型機は外してから再採取してください。"
            )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


# =====================================================================
# macOS
# =====================================================================

_SYSTEM_PROFILER = "/usr/sbin/system_profiler"
_IOREG = "/usr/sbin/ioreg"


def run_command(
    argv: list[str], timeout: int = COMMAND_TIMEOUT_SEC
) -> dict[str, Any]:  # pragma: no cover
    """外部コマンドを実行し、結果を辞書で返す（例外は投げない）。"""
    result: dict[str, Any] = {"argv": argv}
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        result.update(ok=False, error=f"コマンドが見つかりません: {e}")
        return result
    except subprocess.TimeoutExpired:
        result.update(ok=False, error=f"タイムアウト（{timeout} 秒）")
        return result
    except OSError as e:
        result.update(ok=False, error=f"{type(e).__name__}: {e}")
        return result
    result.update(
        ok=cp.returncode == 0,
        returncode=cp.returncode,
        stdout=cp.stdout,
        stderr=cp.stderr.decode("utf-8", errors="replace"),
    )
    if cp.returncode != 0:
        result["error"] = f"終了コード {cp.returncode}: {result['stderr'].strip()[:500]}"
    return result


def _looks_like_permission_error(stderr: str) -> bool:
    s = stderr.lower()
    return "permission denied" in s or "not permitted" in s or "requires root" in s


def _save_command(ctx: Context, name: str, argv: list[str], suffix: str) -> Any:  # pragma: no cover
    """コマンドを実行して標準出力をそのまま保存する。JSON なら読み込み結果を返す。"""
    r = run_command(argv)
    info: dict[str, Any] = {"command": " ".join(argv)}
    for k in ("returncode", "stderr", "error"):
        if r.get(k):
            info[k] = r[k]
    stdout: bytes = r.get("stdout") or b""
    if stdout:
        info["file"] = str(ctx.write_bytes(suffix, stdout))
        info["bytes"] = len(stdout)
    parsed: Any = None
    if stdout and suffix.endswith(".json"):
        try:
            parsed = json.loads(stdout.decode("utf-8"))
            info["valid_json"] = True
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            info["valid_json"] = False
            info["json_error"] = str(e)
    ok = bool(r.get("ok")) and bool(stdout)
    if r.get("ok") and not stdout:
        info["error"] = "標準出力が空でした"
    ctx.record(name, ok, **info)
    if _looks_like_permission_error(r.get("stderr", "")):
        ctx.warn(
            f"{name} が権限エラーを返しました。管理者権限が必要な可能性があります。"
            " `sudo python3 tools/collect_dump.py ...` で再実行してください。"
        )
    return parsed


def summarize_system_profiler(obj: Any) -> list[str]:
    """system_profiler -json の出力を木構造で要約する（目視照合用。パーサーではない）。

    キー名が未確定（要実機検証）のため、特定キーに依存せず
    `_name` を持つ要素と、キー名に "speed" を含む値を列挙する。
    """
    lines: list[str] = []
    id_keys = ("vendor_id", "product_id", "location_id")

    def walk(node: Any, depth: int) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, depth)
            return
        if not isinstance(node, dict):
            return
        name = node.get("_name")
        if name is not None:
            extras = [
                f"{k}={v}"
                for k, v in node.items()
                if isinstance(v, (str, int, float)) and ("speed" in k.lower() or k in id_keys)
            ]
            suffix = f"  [{', '.join(extras)}]" if extras else ""
            lines.append(f"{'  ' * depth}- {name}{suffix}")
            depth += 1
        for v in node.values():
            if isinstance(v, (list, dict)):
                walk(v, depth)

    walk(obj, 0)
    return lines


def _id_matches(value: Any, expected: int) -> bool:
    """vendor_id / product_id 相当の値が expected と一致するか。

    要実機検証: 値の表記（"0x0781", "0x0781  (SanDisk Corporation)", 整数など）は未確定の
    ため、整数、または文字列中の 0x 付き 16 進表記のいずれでも一致とみなす。
    """
    if isinstance(value, int):
        return value == expected
    if isinstance(value, str):
        return any(int(h, 16) == expected for h in re.findall(r"0x([0-9A-Fa-f]+)", value))
    return False


def find_macos_target(obj: Any, vid: int, pid: int) -> list[dict[str, Any]]:
    """system_profiler -json の木から VID:PID が一致する要素を探し、親の経路を返す。

    要実機検証: キー名 vendor_id / product_id / location_id は仮。
    経路の先頭側（バス名。例: "USB 3.1 Bus"）が P の判定根拠になる想定。
    """
    matches: list[dict[str, Any]] = []

    def walk(node: Any, path: list[str]) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, path)
            return
        if not isinstance(node, dict):
            return
        name = node.get("_name")
        here = [*path, str(name)] if name is not None else path
        if _id_matches(node.get("vendor_id"), vid) and _id_matches(node.get("product_id"), pid):
            matches.append(
                {
                    "path_from_root": here,
                    "location_id": node.get("location_id"),
                    "speed_values": {
                        k: v
                        for k, v in node.items()
                        if "speed" in k.lower() and isinstance(v, (str, int, float))
                    },
                }
            )
        for v in node.values():
            if isinstance(v, (list, dict)):
                walk(v, here)

    walk(obj, [])
    return matches


def _first_hex(value: Any) -> int | None:
    """vendor_id 等の値から最初の 0x 付き 16 進数を取り出す（要実機検証: 表記は未確定）。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"0x([0-9A-Fa-f]+)", value)
        if m:
            return int(m.group(1), 16)
    return None


def list_macos_devices(obj: Any) -> list[dict[str, Any]]:
    """system_profiler -json の木から vendor_id / product_id を持つ要素を列挙する（--list 用）。

    要実機検証: キー名 vendor_id / product_id / location_id は仮。
    """
    devices: list[dict[str, Any]] = []

    def walk(node: Any, path: list[str]) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, path)
            return
        if not isinstance(node, dict):
            return
        name = node.get("_name")
        here = [*path, str(name)] if name is not None else path
        if "vendor_id" in node or "product_id" in node:
            devices.append(
                {
                    "name": name,
                    "vid": _first_hex(node.get("vendor_id")),
                    "pid": _first_hex(node.get("product_id")),
                    "vendor_id_raw": node.get("vendor_id"),
                    "product_id_raw": node.get("product_id"),
                    "location_id": node.get("location_id"),
                    "path_from_root": here,
                }
            )
        for v in node.values():
            if isinstance(v, (list, dict)):
                walk(v, here)

    walk(obj, [])
    return devices


def format_macos_device_list(devices: list[dict[str, Any]]) -> list[str]:
    """--list 用の一覧表示（macOS）。"""
    if not devices:
        return ["（接続中の USB デバイスが見つかりませんでした）"]
    lines = ["VID:PID    location_id           経路（親バス > … > デバイス）"]
    for d in devices:
        if d["vid"] is not None and d["pid"] is not None:
            vidpid = f"{d['vid']:04X}:{d['pid']:04X}"
        else:
            vidpid = f"?（vendor_id={d['vendor_id_raw']!r}, product_id={d['product_id_raw']!r}）"
        lines.append(
            f"{vidpid}  {d.get('location_id') or '?':<20}  {' > '.join(d['path_from_root'])}"
        )
    lines.append(
        "※ macOS にはハブ番号/ポート番号・コンパニオンに相当する値が無いため、"
        "経路（親バス名）と location_id を表示しています（要実機検証）。"
    )
    return lines


def list_macos(ctx: Context) -> None:  # pragma: no cover - 実機 (macOS) でのみ動作
    devices: list[dict[str, Any]] = []
    for data_type in ("SPUSBDataType", "SPUSBHostDataType"):
        r = run_command([_SYSTEM_PROFILER, data_type, "-json"])
        if not r.get("ok"):
            ctx.record(f"system_profiler {data_type} -json", False, error=r.get("error"))
            continue
        try:
            devices = list_macos_devices(json.loads(r["stdout"].decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            ctx.record(f"system_profiler {data_type} -json", False, error=str(e))
            continue
        ctx.summary.append(f"（{data_type} より）")
        if devices:
            break  # 要実機検証: 新しい macOS では SPUSBHostDataType 側にのみ出る可能性がある
    ctx.summary.extend(format_macos_device_list(devices))


def summarize_macos_target(target: dict[str, Any]) -> list[str]:
    lines = [f"■ 対象デバイス {target['device']} の接続位置（P の判定根拠）"]
    if not target["matches"]:
        lines.append("  （見つかりませんでした）")
    for m in target["matches"]:
        lines.append("  経路: " + " > ".join(m["path_from_root"]))
        lines.append(f"  location_id: {m.get('location_id')}")
        speeds = ", ".join(f"{k}={v}" for k, v in m["speed_values"].items()) or "（なし）"
        lines.append(f"  speed を含む値: {speeds}")
    return lines


_IOREG_NODE = re.compile(r"\+-o (?P<name>.+?)\s+<class (?P<cls>[^,>]+)")
_IOREG_SPEED = re.compile(r'"(?P<key>[^"]*[Ss]peed[^"]*)"\s*=\s*(?P<val>.+?)\s*$')


def summarize_ioreg(text: str) -> list[str]:
    """ioreg -l の出力から、キー名に Speed を含むプロパティをノードごとに抜き出す。"""
    lines: list[str] = []
    node: str | None = None
    props: list[str] = []

    def flush() -> None:
        if node is not None and props:
            lines.append(f"- {node}: " + ", ".join(props))

    for line in text.splitlines():
        m = _IOREG_NODE.search(line)
        if m:
            flush()
            node = f"{m.group('name')} ({m.group('cls').strip()})"
            props = []
            continue
        m = _IOREG_SPEED.search(line)
        if m:
            props.append(f"{m.group('key')}={m.group('val')}")
    flush()
    return lines


def collect_macos(ctx: Context) -> None:  # pragma: no cover - 実機 (macOS) でのみ動作
    ctx.is_admin = os.geteuid() == 0
    for key, argv in (("sw_vers", ["/usr/bin/sw_vers"]), ("uname_m", ["/usr/bin/uname", "-m"])):
        r = run_command(argv)
        ctx.platform[key] = (r.get("stdout") or b"").decode("utf-8", errors="replace").strip()

    # 利用可能なデータタイプ一覧（macOS のバージョン差の確認用）
    r = run_command([_SYSTEM_PROFILER, "-listDataTypes"])
    data_types = (r.get("stdout") or b"").decode("utf-8", errors="replace")
    ctx.platform["system_profiler_data_types"] = data_types.split()
    ctx.record(
        "system_profiler -listDataTypes",
        bool(r.get("ok")),
        **({"error": r["error"]} if r.get("error") else {}),
    )

    spusb = _save_command(
        ctx,
        "system_profiler SPUSBDataType -json",
        [_SYSTEM_PROFILER, "SPUSBDataType", "-json"],
        ".json",
    )
    thunderbolt = _save_command(
        ctx,
        "system_profiler SPThunderboltDataType -json",
        [_SYSTEM_PROFILER, "SPThunderboltDataType", "-json"],
        ".thunderbolt.json",
    )
    ioreg_ok_before = len(ctx.saved)
    _save_command(
        ctx, "ioreg -p IOUSB -l -w 0", [_IOREG, "-p", "IOUSB", "-l", "-w", "0"], ".ioreg.txt"
    )

    # 要実機検証: 新しい macOS では USB 情報が SPUSBHostDataType 側に移っている可能性がある。
    # 一覧に存在する場合のみ追加で採取する（存在しなければ何もしない）。
    usbhost = None
    if "SPUSBHostDataType" in ctx.platform["system_profiler_data_types"]:
        usbhost = _save_command(
            ctx,
            "system_profiler SPUSBHostDataType -json",
            [_SYSTEM_PROFILER, "SPUSBHostDataType", "-json"],
            ".usbhost.json",
        )

    # 対象デバイスの接続位置。SPUSBDataType で見つからなければ SPUSBHostDataType も探す。
    target_source = "SPUSBDataType"
    ctx.target["matches"] = find_macos_target(spusb, ctx.vid, ctx.pid)
    if not ctx.target["matches"] and usbhost is not None:
        target_source = "SPUSBHostDataType"
        ctx.target["matches"] = find_macos_target(usbhost, ctx.vid, ctx.pid)
    ctx.target["source"] = target_source
    if not ctx.target["matches"] and find_macos_target(thunderbolt, ctx.vid, ctx.pid):
        ctx.target["found_in_thunderbolt"] = True
        ctx.warn(
            "対象デバイスは SPThunderboltDataType 側に見つかりました（Phase 1 対象外の構成）。"
        )
    ctx.check_target()

    # ---- 目視照合用の要約 ----
    s = ctx.summary
    s.extend(summarize_macos_target(ctx.target))
    s.append("■ system_profiler SPUSBDataType（キー名に speed を含む値を表示）")
    s.extend(summarize_system_profiler(spusb) or ["  （デバイスが見つかりませんでした）"])
    if usbhost is not None:
        s.append("■ system_profiler SPUSBHostDataType")
        s.extend(summarize_system_profiler(usbhost) or ["  （デバイスが見つかりませんでした）"])
    s.append("■ system_profiler SPThunderboltDataType（Thunderbolt 接続の機器は USB 側に出ません）")
    s.extend(summarize_system_profiler(thunderbolt) or ["  （項目なし）"])
    s.append("■ ioreg -p IOUSB（キー名に Speed を含むプロパティ）")
    if len(ctx.saved) > ioreg_ok_before:
        text = ctx.path(".ioreg.txt").read_text(encoding="utf-8", errors="replace")
        s.extend(summarize_ioreg(text) or ["  （Speed を含むプロパティが見つかりませんでした）"])
    else:
        s.append("  （採取失敗）")


def _import_windows_modules() -> tuple[Any, Any]:  # pragma: no cover - 実機 (Windows) でのみ動作
    """パッケージ側の Windows 採取層・解析層を読み込む。

    採取ロジックを二重に持たないため、インストールなしでも読めるよう
    リポジトリの src/ を sys.path に足して読み込む（標準ライブラリのみで動作する）。
    macOS では読み込まないので、tools/ だけを Mac にコピーしても動く。
    """
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from usb_link_check.platforms import win32_raw, windows

    return win32_raw, windows


def collect_windows(ctx: Context) -> None:  # pragma: no cover - 実機 (Windows) でのみ動作
    win32_raw, windows = _import_windows_modules()
    dump = win32_raw.build_dump(ctx)

    # 対象デバイスの接続位置（P の判定根拠）
    ctx.target["matches"] = windows.find_devices(dump, ctx.vid, ctx.pid)
    dump["target"] = ctx.target
    ctx.check_target()

    path = ctx.write_json(".json", dump)
    ctx.record("Windows 採取結果の保存", True, file=str(path))
    ctx.summary.extend(summarize_windows(dump))


def list_windows(ctx: Context) -> None:  # pragma: no cover - 実機 (Windows) でのみ動作
    win32_raw, windows = _import_windows_modules()
    dump = win32_raw.build_dump(ctx)
    ctx.summary.extend(format_windows_device_list(windows.describe_devices(dump)))


def _pad(text: str, width: int) -> str:
    """全角文字を幅 2 として数え、表示幅 width まで空白で埋める。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - shown, 0)


def format_windows_device_list(devices: list[dict[str, Any]]) -> list[str]:
    """--list 用の一覧表示（VID:PID、デバイス名、ハブ番号/ポート番号、コンパニオンの有無）。"""
    if not devices:
        return ["（接続中の USB デバイスが見つかりませんでした）"]
    widths = (9, 16, 22, 30)
    header = ("VID:PID", "ハブ/ポート", "コンパニオン", "実効リンク速度")
    lines = ["  ".join(_pad(h, w) for h, w in zip(header, widths, strict=True)) + "  デバイス名"]
    for d in devices:
        c = d.get("companion")
        comp = f"あり(ハブ{c['hub_index']}/ポート{c['port']})" if c else "なし"
        name = d.get("device_description") or "?"
        if d.get("is_hub"):
            name += "  [ハブ]"
        cols = (
            f"{d.get('vid') or 0:04X}:{d.get('pid') or 0:04X}",
            f"ハブ{d['hub_index']}/ポート{d['port']}",
            comp,
            # EX.Speed は SuperSpeed を表現しないため単独では表示しない（SPEC.md 4.2）
            effective_link_speed(d),
        )
        lines.append("  ".join(_pad(v, w) for v, w in zip(cols, widths, strict=True)) + f"  {name}")
    return lines


def _bit_names(expanded: dict[str, Any] | None) -> str:
    if not expanded:
        return "?"
    on = [name for name, v in expanded["bits"].items() if v]
    unknown = expanded["unknown_bits_hex"]
    extra = f" 未知ビット={unknown}" if unknown != "0x00000000" else ""
    return f"{expanded['hex']} [{', '.join(on) or 'なし'}]{extra}"


def effective_link_speed(rec: dict[str, Any]) -> str:
    """実効リンク速度の表示（SPEC.md 4.2「L の判定順序」）。

    EX.Speed は SuperSpeed を表現しないため、単独では表示しない。
    """
    bits = (rec.get("v2_flags") or {}).get("bits") or {}
    if bits.get("DeviceIsOperatingAtSuperSpeedPlusOrHigher"):
        return "10 Gbps 以上（SuperSpeedPlus で動作中）"
    if bits.get("DeviceIsOperatingAtSuperSpeedOrHigher"):
        return "5 Gbps（SuperSpeed で動作中）"
    return {0: "1.5 Mbps", 1: "12 Mbps", 2: "480 Mbps"}.get(rec.get("speed"), "不明")


def summarize_windows_target(target: dict[str, Any]) -> list[str]:
    lines = [f"■ 対象デバイス {target['device']} の接続位置（P の判定根拠）"]
    if not target.get("matches"):
        lines.append("  （見つかりませんでした）")
    for m in target.get("matches", []):
        route = " > ".join(f"ハブ[{e['hub_index']}]ポート{e['port']}" for e in m["path_from_root"])
        lines.append(f"  実効リンク速度: {effective_link_speed(m)}")
        lines.append(
            f"  ハブ番号={m['hub_index']} ({m.get('hub_description')}, {m.get('hub_type')})"
            f"  ポート番号={m['port']}  {m.get('device_description') or ''}"
        )
        lines.append(f"  経路: {route}")
        protocols = _bit_names(m.get("port_supported_usb_protocols"))
        lines.append(f"  このポートの SupportedUsbProtocols={protocols}")
        c = m.get("companion")
        if c:
            lines.append(
                f"  コンパニオン: ハブ[{c.get('hub_index')}]ポート{c.get('port')}"
                f"  SupportedUsbProtocols={_bit_names(c.get('supported_usb_protocols'))}"
            )
        else:
            lines.append("  コンパニオン: なし（CompanionPortNumber=0）")
        lines.append(
            f"  EX.Speed={m.get('speed')} ({m.get('speed_name')})"
            f"  V2.Flags={_bit_names(m.get('v2_flags'))}"
        )
    return lines


def summarize_windows(dump: dict[str, Any], companion_pairs: Any = None) -> list[str]:
    """Windows ダンプから、USBView と照合するための要約行を作る。

    companion_pairs: パッケージ側 platforms.windows.companion_pairs（テストから差し替え可能）。
    """
    if companion_pairs is None:  # pragma: no cover - 実機 (Windows) でのみ動作
        companion_pairs = _import_windows_modules()[1].companion_pairs
    lines: list[str] = []
    if "target" in dump:
        lines.extend(summarize_windows_target(dump["target"]))
    for hub in dump.get("hubs", []):
        info = (hub.get("hub_information_ex") or {}).get("decoded") or {}
        hub_type = info.get("HubType_name", "?")
        name = hub.get("description") or "?"
        lines.append(
            f"■ ハブ[{hub.get('index')}] {name}  HubType={hub_type}  ports={hub.get('port_count')}"
        )
        attempts = hub.get("open_attempts") or []
        if attempts and not attempts[-1].get("ok"):
            lines.append(f"    （開けませんでした: {attempts[-1].get('win32_error_message')}）")
            continue
        pairs = companion_pairs(dump, hub)
        if pairs:
            lines.append(
                "  コンパニオン対応（同じ物理コネクタの論理ポート）: "
                + ", ".join(f"{a}↔{b}" for a, b in pairs)
            )
        for port in hub.get("ports", []):
            ex_rec = port.get("connection_information_ex") or {}
            ex = ex_rec.get("decoded") or {}
            if not ex_rec.get("ok"):
                lines.append(
                    f"  Port {port.get('port')}: EX 取得失敗 ({ex_rec.get('win32_error_message')})"
                )
                continue
            if ex.get("ConnectionStatus", 0) == 0:
                continue
            dd = ex.get("DeviceDescriptor") or {}
            dev = port.get("device") or {}
            dev_name = dev.get("bus_reported_description") or dev.get("description") or "?"
            vidpid = f"{dd.get('idVendor', 0):04X}:{dd.get('idProduct', 0):04X}"
            hub_mark = "  [ハブ]" if ex.get("DeviceIsHub") else ""
            lines.append(f"  Port {port.get('port')}: {vidpid}  {dev_name}{hub_mark}")
            v2_rec = port.get("connection_information_ex_v2") or {}
            v2 = v2_rec.get("decoded")
            effective = effective_link_speed(
                {"speed": ex.get("Speed"), "v2_flags": (v2 or {}).get("Flags")}
            )
            lines.append(f"      実効リンク速度={effective}")
            lines.append(
                f"      状態={ex.get('ConnectionStatus_name')}  "
                f"EX.Speed={ex.get('Speed')} ({ex.get('Speed_name')}・単独では信用しない)  "
                f"bcdUSB={dd.get('bcdUSB_hex', '?')}"
            )
            if v2:
                lines.append(f"      V2.Flags={_bit_names(v2.get('Flags'))}")
                lines.append(
                    f"      V2.SupportedUsbProtocols={_bit_names(v2.get('SupportedUsbProtocols'))}"
                )
            else:
                lines.append(f"      V2 取得失敗 ({v2_rec.get('win32_error_message')})")
            pcp = (port.get("port_connector_properties") or {}).get("decoded")
            if pcp:
                lines.append(
                    f"      PortProperties={_bit_names(pcp.get('UsbPortProperties'))}  "
                    f"Companion=hub{pcp.get('CompanionIndex')}/port{pcp.get('CompanionPortNumber')}"
                )
    if not dump.get("hubs"):
        lines.append("（ハブが 1 つも列挙できませんでした）")
    return lines


# =====================================================================
# メイン
# =====================================================================


def _sanitize(ctx: Context) -> None:
    """採取したファイルに sanitize_dump.py を適用する。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sanitize_dump
    except ImportError:
        ctx.warn(
            "sanitize_dump.py を読み込めませんでした。コミット前に必ず"
            " `python tools/sanitize_dump.py` を実行してください。"
        )
        return
    total = 0
    for p in ctx.saved:
        total += sanitize_dump.sanitize_file(p)
    print(f"  シリアル番号・固有 ID を {total} 件 REDACTED に置換しました（sanitize_dump.py）")


def _write_meta(ctx: Context) -> None:
    meta = {
        "collector": "usb-link-check tools/collect_dump.py",
        "collector_version": COLLECTOR_VERSION,
        "label": ctx.label,
        "collected_at": _now_iso(),
        "platform": ctx.platform,
        "is_admin": ctx.is_admin,
        "target": ctx.target,
        "files": [p.name for p in ctx.saved],
        "items": ctx.items,
        "failures": [i["name"] for i in ctx.failures],
        "warnings": ctx.warnings,
    }
    ctx.write_json(".meta.json", meta)


def _run_list(system: str) -> int:  # pragma: no cover - 実機でのみ動作
    """--list: 接続中の USB デバイスを一覧表示する。ファイルは一切保存しない。"""
    ctx = Context("list", Path("."), 0, 0, quiet=True)
    try:
        (list_macos if system == "Darwin" else list_windows)(ctx)
    except Exception:
        ctx.record("デバイス一覧の取得", False, error=traceback.format_exc())
    print("接続中の USB デバイス（--device には VID:PID 列の値を指定してください）:")
    for line in ctx.summary:
        print(line)
    return 1 if ctx.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="usb-link-check 用の実機ダンプを採取する（標準ライブラリのみ）"
    )
    parser.add_argument(
        "--label",
        help="保存ファイル名の接頭辞（例: macos_ssd_fast, windows_usb2）。採取時は必須",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--device",
        help="採取対象デバイスの VID:PID（16 進、例: 0781:5591）。"
        "接続先のハブ番号・ポート番号を記録するために使う",
    )
    mode.add_argument(
        "--list",
        action="store_true",
        help="接続中の USB デバイスを一覧表示して終了する（何も保存しない）。"
        "--device に渡す VID:PID を調べるために使う",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"保存先ディレクトリ（既定: {DEFAULT_OUT_DIR}）",
    )
    parser.add_argument("--force", action="store_true", help="同名ファイルがあっても上書きする")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    system = platform.system()
    if system not in ("Darwin", "Windows"):
        print("ERROR: このスクリプトは macOS と Windows のみ対応しています", file=sys.stderr)
        return 4

    if args.list:
        return _run_list(system)

    if not args.label:
        parser.error("採取時は --label が必要です")
    try:
        label = validate_label(args.label)
        vid, pid = parse_device_spec(args.device)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4

    collect = collect_macos if system == "Darwin" else collect_windows

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob(f"{label}.*"))
    if existing and not args.force:
        print(
            f"ERROR: 同じラベルのファイルが既にあります（上書きは --force）: {label}",
            file=sys.stderr,
        )
        for p in existing:
            print(f"  {p}", file=sys.stderr)
        return 4

    ctx = Context(label, out_dir, vid, pid)
    print(f"採取開始: label={label}  対象={vid:04X}:{pid:04X}  OS={system} {platform.release()}")
    try:
        collect(ctx)
    except Exception:
        # ここに来るのは想定外のバグ。途中まで採れた分と例外内容は残す。
        ctx.record("採取処理全体", False, error=traceback.format_exc())
    _write_meta(ctx)
    _sanitize(ctx)

    print()
    print("=" * 70)
    print("保存したファイル:")
    for p in ctx.saved:
        print(f"  {p}")
    if ctx.failures:
        print("失敗した項目（採取は続行しました。詳細は .meta.json）:")
        for item in ctx.failures:
            print(f"  - {item['name']}: {item.get('error', '')}")
    for w in ctx.warnings:
        print(f"【要確認】{w}")
    print("=" * 70)
    print("速度に相当すると思われる値（USBView /「システム情報」と目視で照合してください）:")
    for line in ctx.summary:
        print(line)
    print("=" * 70)
    print(
        "※ 上記は目視照合用の簡易表示です。値の意味は未確定（要実機検証）のため、"
        "USBView / システム情報と食い違う箇所があれば報告してください。"
    )
    return 1 if ctx.failures else 0


if __name__ == "__main__":
    sys.exit(main())
