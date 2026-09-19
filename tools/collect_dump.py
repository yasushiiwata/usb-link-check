#!/usr/bin/env python3
"""実機ダンプ採取スクリプト（Python 標準ライブラリのみ・追加インストール不要）。

使い方:
    python tools/collect_dump.py --label macos_ssd_fast
    python tools/collect_dump.py --label windows_fast

保存先: tests/fixtures/raw/<label>.*（--out-dir で変更可）

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

注意: このスクリプト内の Windows 構造体レイアウト・IOCTL 番号・ビット割り当ては
Windows SDK (usbioctl.h / usbiodef.h / usbspec.h) の記述に基づく仮の定義であり、
「要実機検証」と記した箇所は USBView 等との照合で確定させる必要がある。
解釈を誤っていても後から読み直せるよう、構造体は必ず生バイト列 (raw_hex) を併記する。
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import platform
import re
import struct
import subprocess
import sys
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

COLLECTOR_VERSION = "0.1.0"
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


def expand_bits(value: int, names: dict[int, str]) -> dict[str, Any]:
    """ビットフィールドを「生の数値」と「ビットごとの真偽」の両方で表す。

    names に無いビットが立っていた場合も捨てずに unknown_bits_hex に残す。
    """
    known_mask = 0
    bits: dict[str, bool] = {}
    for bit, name in sorted(names.items()):
        bits[name] = bool((value >> bit) & 1)
        known_mask |= 1 << bit
    return {
        "value": value,
        "hex": f"0x{value:08X}",
        "binary": f"{value:032b}",
        "bits": bits,
        "unknown_bits_hex": f"0x{value & ~known_mask & 0xFFFFFFFF:08X}",
    }


class Context:
    """採取 1 回分の状態（保存ファイル・成否・警告）を保持する。"""

    def __init__(self, label: str, out_dir: Path) -> None:
        self.label = label
        self.out_dir = out_dir
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
        mark = "[OK]  " if ok else "[失敗]"
        detail = f" -> {info['file']}" if ok and "file" in info else ""
        if not ok and info.get("error"):
            detail = f": {info['error']}"
        print(f"  {mark} {name}{detail}")

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

    # ---- 目視照合用の要約 ----
    s = ctx.summary
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


# =====================================================================
# Windows
# =====================================================================
#
# 経路: SPEC.md 4.2（USBView と同じ）
#   SetupDiGetClassDevs(GUID_DEVINTERFACE_USB_HUB) → CreateFile → DeviceIoControl
#
# 以下の定義はすべて Windows SDK のヘッダ記述に基づく。要実機検証。

GUID_DEVINTERFACE_USB_HUB = "f18a0e88-c30c-11d0-8815-00a0c906bed8"
GUID_DEVINTERFACE_USB_HOST_CONTROLLER = "3abf6f2d-71c4-462a-8a92-1e6861e6af27"
# DEVPKEY_Device_BusReportedDeviceDesc（製品名文字列。USBView との照合用）
DEVPKEY_BUS_REPORTED_DEVICE_DESC = ("540b947e-8b40-45bc-a8a2-6a0b894cbda2", 4)

FILE_DEVICE_USB = 0x22  # = FILE_DEVICE_UNKNOWN (usbiodef.h)


def _usb_ctl(function: int) -> int:
    """CTL_CODE(FILE_DEVICE_USB, function, METHOD_BUFFERED, FILE_ANY_ACCESS)。"""
    return (FILE_DEVICE_USB << 16) | (function << 2)


# 関数番号は usbiodef.h より。要実機検証。
IOCTL_USB_GET_NODE_INFORMATION = _usb_ctl(258)  # 0x220408
IOCTL_USB_GET_NODE_CONNECTION_NAME = _usb_ctl(261)  # 0x220414
IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME = _usb_ctl(264)  # 0x220420
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = _usb_ctl(274)  # 0x220448
IOCTL_USB_GET_HUB_CAPABILITIES_EX = _usb_ctl(276)  # 0x220450
IOCTL_USB_GET_HUB_INFORMATION_EX = _usb_ctl(277)  # 0x220454
IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES = _usb_ctl(278)  # 0x220458
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 = _usb_ctl(279)  # 0x22045C

# 構造体サイズ。usbioctl.h は #include <pshpack1.h>（1 バイト境界パック）である前提。要実機検証。
# 実際の bytes_returned と食い違った場合はパック前提が誤っている。
DEVICE_DESCRIPTOR_SIZE = 18
CONN_INFO_EX_FIXED_SIZE = 35  # ConnectionIndex(4) + DeviceDescriptor(18) + 13
PIPE_INFO_SIZE = 11  # USB_ENDPOINT_DESCRIPTOR(7) + ScheduleOffset(4)
CONN_INFO_EX_BUFFER_SIZE = CONN_INFO_EX_FIXED_SIZE + PIPE_INFO_SIZE * 32
CONN_INFO_EX_V2_SIZE = 16
HUB_DESCRIPTOR_SIZE = 71  # USB_HUB_DESCRIPTOR（bRemoveAndPowerMask[64] を含む）
HUB_INFORMATION_EX_SIZE = 4 + 2 + HUB_DESCRIPTOR_SIZE  # 77
NODE_INFORMATION_SIZE = 4 + HUB_DESCRIPTOR_SIZE + 1  # 76
HUB_CAPABILITIES_EX_SIZE = 4
PORT_CONNECTOR_FIXED_SIZE = 16  # WCHAR 名前の直前まで
PORT_CONNECTOR_STRUCT_SIZE = 18  # sizeof（WCHAR[1] を含む）
NAME_HEADER_SIZE = 8  # ConnectionIndex(4) + ActualLength(4)
NAME_STRUCT_SIZE = 10  # sizeof（WCHAR[1] を含む）

# USB_DEVICE_SPEED (usbspec.h)。要実機検証: SuperSpeedPlus 機器でも 3 が返るとされる。
USB_DEVICE_SPEED = {0: "UsbLowSpeed", 1: "UsbFullSpeed", 2: "UsbHighSpeed", 3: "UsbSuperSpeed"}
# USB_CONNECTION_STATUS (usbioctl.h)
USB_CONNECTION_STATUS = {
    0: "NoDeviceConnected",
    1: "DeviceConnected",
    2: "DeviceFailedEnumeration",
    3: "DeviceGeneralFailure",
    4: "DeviceCausedOvercurrent",
    5: "DeviceNotEnoughPower",
    6: "DeviceNotEnoughBandwidth",
    7: "DeviceHubNestedTooDeeply",
    8: "DeviceInLegacyHub",
    9: "DeviceEnumerating",
    10: "DeviceReset",
}
USB_HUB_TYPE = {1: "UsbRootHub", 2: "Usb20Hub", 3: "Usb30Hub"}
USB_HUB_NODE = {0: "UsbHub", 1: "UsbMIParent"}
# USB_NODE_CONNECTION_INFORMATION_EX_V2_FLAGS のビット順。要実機検証。
V2_FLAG_BITS = {
    0: "DeviceIsOperatingAtSuperSpeedOrHigher",
    1: "DeviceIsSuperSpeedCapableOrHigher",
    2: "DeviceIsOperatingAtSuperSpeedPlusOrHigher",
    3: "DeviceIsSuperSpeedPlusCapableOrHigher",
}
# USB_PROTOCOLS のビット順。要実機検証。
# V2 の出力側 SupportedUsbProtocols は「ポートが対応するプロトコル」を示すとされる（P の候補）。
USB_PROTOCOL_BITS = {0: "Usb110", 1: "Usb200", 2: "Usb300"}
# USB_PORT_PROPERTIES のビット順。要実機検証。
PORT_PROPERTY_BITS = {
    0: "PortIsUserConnectable",
    1: "PortIsDebugCapable",
    2: "PortHasMultipleCompanions",
    3: "PortConnectorIsTypeC",
}
# USB_HUB_CAP_FLAGS のビット順。要実機検証。
HUB_CAPABILITY_BITS = {
    0: "HubIsHighSpeedCapable",
    1: "HubIsHighSpeed",
    2: "HubIsMultiTtCapable",
    3: "HubIsMultiTt",
    4: "HubIsRoot",
    5: "HubIsArmedWakeOnConnect",
    6: "HubIsBusPowered",
}

_DEVICE_DESCRIPTOR = struct.Struct("<BBHBBBBHHHBBBB")
_DEVICE_DESCRIPTOR_FIELDS = (
    "bLength",
    "bDescriptorType",
    "bcdUSB",
    "bDeviceClass",
    "bDeviceSubClass",
    "bDeviceProtocol",
    "bMaxPacketSize0",
    "idVendor",
    "idProduct",
    "bcdDevice",
    "iManufacturer",
    "iProduct",
    "iSerialNumber",
    "bNumConfigurations",
)
# CurrentConfigurationValue, Speed, DeviceIsHub, DeviceAddress, NumberOfOpenPipes, ConnectionStatus
_CONN_EX_TAIL = struct.Struct("<BBBHII")
_ENDPOINT_DESCRIPTOR = struct.Struct("<BBBBHB")
_HUB_DESCRIPTOR_HEAD = struct.Struct("<BBBHBB")
_USB30_HUB_DESCRIPTOR = struct.Struct("<BBBHBBBHH")


def _require(raw: bytes, size: int, what: str) -> None:
    if len(raw) < size:
        raise ValueError(f"{what}: {len(raw)} バイトしか返らなかった（期待 {size} バイト以上）")


def decode_device_descriptor(raw: bytes) -> dict[str, Any]:
    """USB_DEVICE_DESCRIPTOR (18 バイト)。"""
    _require(raw, DEVICE_DESCRIPTOR_SIZE, "USB_DEVICE_DESCRIPTOR")
    values = _DEVICE_DESCRIPTOR.unpack_from(raw)
    d: dict[str, Any] = dict(zip(_DEVICE_DESCRIPTOR_FIELDS, values, strict=True))
    d["bcdUSB_hex"] = f"0x{d['bcdUSB']:04X}"
    d["idVendor_hex"] = f"0x{d['idVendor']:04X}"
    d["idProduct_hex"] = f"0x{d['idProduct']:04X}"
    return d


def decode_connection_info_ex(raw: bytes) -> dict[str, Any]:
    """USB_NODE_CONNECTION_INFORMATION_EX。要実機検証（オフセット・パック）。"""
    _require(raw, CONN_INFO_EX_FIXED_SIZE, "USB_NODE_CONNECTION_INFORMATION_EX")
    (index,) = struct.unpack_from("<I", raw, 0)
    cfg, speed, is_hub, address, n_pipes, status = _CONN_EX_TAIL.unpack_from(raw, 22)
    pipes = []
    for i in range(n_pipes):
        off = CONN_INFO_EX_FIXED_SIZE + i * PIPE_INFO_SIZE
        if off + PIPE_INFO_SIZE > len(raw):
            break
        b_len, b_type, ep_addr, attrs, max_packet, interval = _ENDPOINT_DESCRIPTOR.unpack_from(
            raw, off
        )
        (schedule_offset,) = struct.unpack_from("<I", raw, off + 7)
        pipes.append(
            {
                "bEndpointAddress": ep_addr,
                "bmAttributes": attrs,
                "wMaxPacketSize": max_packet,
                "bInterval": interval,
                "ScheduleOffset": schedule_offset,
            }
        )
    return {
        "ConnectionIndex": index,
        "DeviceDescriptor": decode_device_descriptor(raw[4:22]),
        "CurrentConfigurationValue": cfg,
        "Speed": speed,
        "Speed_name": USB_DEVICE_SPEED.get(speed, f"UNKNOWN({speed})"),
        "DeviceIsHub": bool(is_hub),
        "DeviceAddress": address,
        "NumberOfOpenPipes": n_pipes,
        "ConnectionStatus": status,
        "ConnectionStatus_name": USB_CONNECTION_STATUS.get(status, f"UNKNOWN({status})"),
        "PipeList": pipes,
    }


def decode_connection_info_ex_v2(raw: bytes) -> dict[str, Any]:
    """USB_NODE_CONNECTION_INFORMATION_EX_V2。Flags は生の数値とビット展開の両方を持つ。"""
    _require(raw, CONN_INFO_EX_V2_SIZE, "USB_NODE_CONNECTION_INFORMATION_EX_V2")
    index, length, protocols, flags = struct.unpack_from("<IIII", raw, 0)
    return {
        "ConnectionIndex": index,
        "Length": length,
        "SupportedUsbProtocols": expand_bits(protocols, USB_PROTOCOL_BITS),
        "Flags": expand_bits(flags, V2_FLAG_BITS),
    }


def _decode_hub_descriptor(raw: bytes) -> dict[str, Any]:
    """USB_HUB_DESCRIPTOR の先頭 7 バイト（ポート数など）。"""
    _require(raw, _HUB_DESCRIPTOR_HEAD.size, "USB_HUB_DESCRIPTOR")
    b_len, b_type, n_ports, characteristics, pwr_good, ctrl_current = (
        _HUB_DESCRIPTOR_HEAD.unpack_from(raw)
    )
    return {
        "bDescriptorLength": b_len,
        "bDescriptorType": b_type,
        "bNumberOfPorts": n_ports,
        "wHubCharacteristics": characteristics,
        "bPowerOnToPowerGood": pwr_good,
        "bHubControlCurrent": ctrl_current,
    }


def _decode_usb30_hub_descriptor(raw: bytes) -> dict[str, Any]:
    """USB_30_HUB_DESCRIPTOR (12 バイト)。"""
    _require(raw, _USB30_HUB_DESCRIPTOR.size, "USB_30_HUB_DESCRIPTOR")
    fields = (
        "bLength",
        "bDescriptorType",
        "bNumberOfPorts",
        "wHubCharacteristics",
        "bPowerOnToPowerGood",
        "bHubControlCurrent",
        "bHubHdrDecLat",
        "wHubDelay",
        "DeviceRemovable",
    )
    return dict(zip(fields, _USB30_HUB_DESCRIPTOR.unpack_from(raw), strict=True))


def decode_hub_information_ex(raw: bytes) -> dict[str, Any]:
    """USB_HUB_INFORMATION_EX。HubType によって後続ディスクリプタの形式が変わる。"""
    _require(raw, 6, "USB_HUB_INFORMATION_EX")
    hub_type, highest_port = struct.unpack_from("<IH", raw, 0)
    desc_raw = raw[6:]
    d: dict[str, Any] = {
        "HubType": hub_type,
        "HubType_name": USB_HUB_TYPE.get(hub_type, f"UNKNOWN({hub_type})"),
        "HighestPortNumber": highest_port,
    }
    # 要実機検証: Usb30Hub は Usb30HubDescriptor、それ以外は UsbHubDescriptor（USBView に準拠）
    if hub_type == 3:
        d["Usb30HubDescriptor"] = _decode_usb30_hub_descriptor(desc_raw)
    else:
        d["UsbHubDescriptor"] = _decode_hub_descriptor(desc_raw)
    return d


def decode_node_information(raw: bytes) -> dict[str, Any]:
    """USB_NODE_INFORMATION（ポート数の取得用。HUB_INFORMATION_EX 失敗時の予備）。"""
    _require(raw, 4, "USB_NODE_INFORMATION")
    (node_type,) = struct.unpack_from("<I", raw, 0)
    d: dict[str, Any] = {
        "NodeType": node_type,
        "NodeType_name": USB_HUB_NODE.get(node_type, f"UNKNOWN({node_type})"),
    }
    if node_type == 0:
        d["HubDescriptor"] = _decode_hub_descriptor(raw[4:])
        if len(raw) >= NODE_INFORMATION_SIZE:
            d["HubIsBusPowered"] = bool(raw[4 + HUB_DESCRIPTOR_SIZE])
    return d


def decode_hub_capabilities_ex(raw: bytes) -> dict[str, Any]:
    """USB_HUB_CAPABILITIES_EX。"""
    _require(raw, HUB_CAPABILITIES_EX_SIZE, "USB_HUB_CAPABILITIES_EX")
    (flags,) = struct.unpack_from("<I", raw, 0)
    return {"CapabilityFlags": expand_bits(flags, HUB_CAPABILITY_BITS)}


def _decode_utf16z(raw: bytes) -> str:
    return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]


def decode_port_connector_properties(raw: bytes) -> dict[str, Any]:
    """USB_PORT_CONNECTOR_PROPERTIES。"""
    _require(raw, PORT_CONNECTOR_FIXED_SIZE, "USB_PORT_CONNECTOR_PROPERTIES")
    index, actual_len, props, companion_index, companion_port = struct.unpack_from("<IIIHH", raw, 0)
    return {
        "ConnectionIndex": index,
        "ActualLength": actual_len,
        "UsbPortProperties": expand_bits(props, PORT_PROPERTY_BITS),
        "CompanionIndex": companion_index,
        "CompanionPortNumber": companion_port,
        "CompanionHubSymbolicLinkName": _decode_utf16z(raw[PORT_CONNECTOR_FIXED_SIZE:]),
    }


def decode_name_struct(raw: bytes) -> dict[str, Any]:
    """USB_NODE_CONNECTION_DRIVERKEY_NAME / USB_NODE_CONNECTION_NAME（同形式）。"""
    _require(raw, NAME_HEADER_SIZE, "USB_NODE_CONNECTION_*_NAME")
    index, actual_len = struct.unpack_from("<II", raw, 0)
    return {
        "ConnectionIndex": index,
        "ActualLength": actual_len,
        "Name": _decode_utf16z(raw[NAME_HEADER_SIZE:]),
    }


def _norm_device_path(path: str) -> str:
    p = path.lower()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if p.startswith(prefix):
            return p[len(prefix) :]
    return p


# ---- ctypes 構造体（型は OS 非依存の基本型のみで定義） ----
_DWORD = ctypes.c_uint32


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):  # noqa: N801 - Win32 名に合わせる
    _fields_ = [
        ("cbSize", _DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", _DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("cbSize", _DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", _DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


DIGCF_PRESENT = 0x02
DIGCF_ALLCLASSES = 0x04
DIGCF_DEVICEINTERFACE = 0x10
SPDRP_DEVICEDESC = 0x00
SPDRP_SERVICE = 0x04
SPDRP_DRIVER = 0x09
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_LOCATION_INFORMATION = 0x0D
SPDRP_LOCATION_PATHS = 0x23
REG_MULTI_SZ = 7
DEVPROP_TYPE_STRING = 0x12
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3


class _WinApi:  # pragma: no cover - 実機 (Windows) でのみ動作
    """SetupAPI / kernel32 / cfgmgr32 の薄いラッパー。"""

    def __init__(self) -> None:
        self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)  # type: ignore[attr-defined]
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.cfgmgr32 = ctypes.WinDLL("cfgmgr32")  # type: ignore[attr-defined]
        self.invalid_handle = ctypes.c_void_p(-1).value
        p = ctypes.POINTER
        vp = ctypes.c_void_p
        sa, k32, cm = self.setupapi, self.kernel32, self.cfgmgr32

        sa.SetupDiGetClassDevsW.argtypes = [p(_GUID), ctypes.c_wchar_p, vp, _DWORD]
        sa.SetupDiGetClassDevsW.restype = vp
        sa.SetupDiEnumDeviceInterfaces.argtypes = [
            vp, p(_SP_DEVINFO_DATA), p(_GUID), _DWORD, p(_SP_DEVICE_INTERFACE_DATA)
        ]  # fmt: skip
        sa.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
        sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            vp, p(_SP_DEVICE_INTERFACE_DATA), vp, _DWORD, p(_DWORD), p(_SP_DEVINFO_DATA)
        ]  # fmt: skip
        sa.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
        sa.SetupDiEnumDeviceInfo.argtypes = [vp, _DWORD, p(_SP_DEVINFO_DATA)]
        sa.SetupDiEnumDeviceInfo.restype = ctypes.c_int
        sa.SetupDiGetDeviceInstanceIdW.argtypes = [
            vp, p(_SP_DEVINFO_DATA), ctypes.c_wchar_p, _DWORD, p(_DWORD)
        ]  # fmt: skip
        sa.SetupDiGetDeviceInstanceIdW.restype = ctypes.c_int
        sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            vp, p(_SP_DEVINFO_DATA), _DWORD, p(_DWORD), vp, _DWORD, p(_DWORD)
        ]  # fmt: skip
        sa.SetupDiGetDeviceRegistryPropertyW.restype = ctypes.c_int
        sa.SetupDiGetDevicePropertyW.argtypes = [
            vp, p(_SP_DEVINFO_DATA), p(_DEVPROPKEY), p(_DWORD), vp, _DWORD, p(_DWORD), _DWORD
        ]  # fmt: skip
        sa.SetupDiGetDevicePropertyW.restype = ctypes.c_int
        sa.SetupDiDestroyDeviceInfoList.argtypes = [vp]
        sa.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int

        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, _DWORD, _DWORD, vp, _DWORD, _DWORD, vp]
        k32.CreateFileW.restype = vp
        k32.DeviceIoControl.argtypes = [vp, _DWORD, vp, _DWORD, vp, _DWORD, p(_DWORD), vp]
        k32.DeviceIoControl.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [vp]
        k32.CloseHandle.restype = ctypes.c_int

        cm.CM_Get_Parent.argtypes = [p(_DWORD), _DWORD, ctypes.c_ulong]
        cm.CM_Get_Parent.restype = ctypes.c_uint32
        cm.CM_Get_Device_IDW.argtypes = [_DWORD, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
        cm.CM_Get_Device_IDW.restype = ctypes.c_uint32

    # ---- 権限 ----
    @staticmethod
    def is_admin() -> bool | None:
        try:
            return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return None

    @staticmethod
    def format_error(code: int) -> str:
        try:
            return ctypes.FormatError(code).strip()  # type: ignore[attr-defined]
        except Exception:
            return ""

    # ---- SetupAPI ----
    def _registry_property(self, hdev: int, devinfo: _SP_DEVINFO_DATA, prop: int) -> Any:
        regtype = _DWORD(0)
        size = _DWORD(0)
        buf = ctypes.create_string_buffer(2048)
        fn = self.setupapi.SetupDiGetDeviceRegistryPropertyW
        if not fn(hdev, ctypes.byref(devinfo), prop, ctypes.byref(regtype), buf, 2048,
                  ctypes.byref(size)):  # fmt: skip
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if not fn(hdev, ctypes.byref(devinfo), prop, ctypes.byref(regtype), buf, size.value,
                      ctypes.byref(size)):  # fmt: skip
                return None
        parts = [s for s in buf.raw[: size.value].decode("utf-16-le", "replace").split("\0") if s]
        if regtype.value == REG_MULTI_SZ:
            return parts
        return parts[0] if parts else ""

    def _string_device_property(
        self, hdev: int, devinfo: _SP_DEVINFO_DATA, key: tuple[str, int]
    ) -> str | None:
        pkey = _DEVPROPKEY(_GUID.from_str(key[0]), key[1])
        ptype = _DWORD(0)
        size = _DWORD(0)
        buf = ctypes.create_string_buffer(2048)
        ok = self.setupapi.SetupDiGetDevicePropertyW(
            hdev, ctypes.byref(devinfo), ctypes.byref(pkey), ctypes.byref(ptype), buf, 2048,
            ctypes.byref(size), 0,
        )  # fmt: skip
        if not ok or ptype.value != DEVPROP_TYPE_STRING:
            return None
        return _decode_utf16z(buf.raw[: size.value])

    def _instance_id(self, devinst: int) -> str | None:
        buf = ctypes.create_unicode_buffer(1024)
        if self.cfgmgr32.CM_Get_Device_IDW(devinst, buf, 1024, 0) != 0:
            return None
        return buf.value

    def _parent_instance_id(self, devinst: int) -> str | None:
        parent = _DWORD(0)
        if self.cfgmgr32.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != 0:
            return None
        return self._instance_id(parent.value)

    def _device_props(self, hdev: int, devinfo: _SP_DEVINFO_DATA) -> dict[str, Any]:
        return {
            "instance_id": self._instance_id(devinfo.DevInst),
            "parent_instance_id": self._parent_instance_id(devinfo.DevInst),
            "description": self._registry_property(hdev, devinfo, SPDRP_DEVICEDESC),
            "friendly_name": self._registry_property(hdev, devinfo, SPDRP_FRIENDLYNAME),
            "bus_reported_description": self._string_device_property(
                hdev, devinfo, DEVPKEY_BUS_REPORTED_DEVICE_DESC
            ),
            "driver_key": self._registry_property(hdev, devinfo, SPDRP_DRIVER),
            "service": self._registry_property(hdev, devinfo, SPDRP_SERVICE),
            "location_information": self._registry_property(
                hdev, devinfo, SPDRP_LOCATION_INFORMATION
            ),
            "location_paths": self._registry_property(hdev, devinfo, SPDRP_LOCATION_PATHS),
        }

    def enum_interfaces(self, guid_str: str) -> list[dict[str, Any]]:
        """デバイスインターフェース GUID で列挙し、デバイスパスと各種プロパティを返す。"""
        guid = _GUID.from_str(guid_str)
        sa = self.setupapi
        hdev = sa.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
        )
        if hdev is None or hdev == self.invalid_handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"SetupDiGetClassDevsW 失敗: {self.format_error(err)}")
        results: list[dict[str, Any]] = []
        detail_cbsize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        try:
            index = 0
            while True:
                ifdata = _SP_DEVICE_INTERFACE_DATA()
                ifdata.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
                if not sa.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(guid), index, ctypes.byref(ifdata)
                ):
                    err = ctypes.get_last_error()
                    if err != ERROR_NO_MORE_ITEMS:
                        results.append({"enum_error": err, "message": self.format_error(err)})
                    break
                index += 1
                required = _DWORD(0)
                sa.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(ifdata), None, 0, ctypes.byref(required), None
                )
                buf = ctypes.create_string_buffer(max(required.value, 8))
                struct.pack_into("<I", buf, 0, detail_cbsize)
                devinfo = _SP_DEVINFO_DATA()
                devinfo.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
                if not sa.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(ifdata), buf, required.value, None, ctypes.byref(devinfo)
                ):
                    err = ctypes.get_last_error()
                    results.append({"detail_error": err, "message": self.format_error(err)})
                    continue
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                results.append({"device_path": path, **self._device_props(hdev, devinfo)})
        finally:
            sa.SetupDiDestroyDeviceInfoList(hdev)
        return results

    def enum_usb_devices_by_driver_key(self) -> dict[str, dict[str, Any]]:
        """列挙子 "USB" の全デバイスを driver key → プロパティの辞書で返す。"""
        sa = self.setupapi
        hdev = sa.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
        if hdev is None or hdev == self.invalid_handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"SetupDiGetClassDevsW 失敗: {self.format_error(err)}")
        mapping: dict[str, dict[str, Any]] = {}
        try:
            index = 0
            while True:
                devinfo = _SP_DEVINFO_DATA()
                devinfo.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
                if not sa.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(devinfo)):
                    break
                index += 1
                props = self._device_props(hdev, devinfo)
                if props.get("driver_key"):
                    mapping[props["driver_key"].lower()] = props
        finally:
            sa.SetupDiDestroyDeviceInfoList(hdev)
        return mapping

    # ---- ハブを開く / IOCTL ----
    def open_hub(self, path: str) -> tuple[int | None, list[dict[str, Any]]]:
        """USBView と同じ GENERIC_WRITE で開き、失敗したらアクセス権なしで再試行する。"""
        attempts: list[dict[str, Any]] = []
        for label, access, share in (
            ("GENERIC_WRITE", GENERIC_WRITE, FILE_SHARE_WRITE),
            ("NO_ACCESS_RIGHTS", 0, FILE_SHARE_READ | FILE_SHARE_WRITE),
        ):
            h = self.kernel32.CreateFileW(path, access, share, None, OPEN_EXISTING, 0, None)
            if h is not None and h != self.invalid_handle:
                attempts.append({"access": label, "ok": True})
                return h, attempts
            err = ctypes.get_last_error()
            attempts.append(
                {"access": label, "ok": False, "win32_error": err,
                 "win32_error_message": self.format_error(err)}
            )  # fmt: skip
        return None, attempts

    def close(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)

    def ioctl(
        self, handle: int, code: int, in_bytes: bytes, out_size: int
    ) -> tuple[bool, int, bytes]:
        size = max(len(in_bytes), out_size)
        buf = ctypes.create_string_buffer(size)
        ctypes.memmove(buf, in_bytes, len(in_bytes))
        returned = _DWORD(0)
        ok = bool(
            self.kernel32.DeviceIoControl(
                handle, code, buf if in_bytes else None, len(in_bytes), buf, out_size,
                ctypes.byref(returned), None,
            )
        )  # fmt: skip
        err = 0 if ok else ctypes.get_last_error()
        return ok, err, buf.raw[: returned.value]


def _ioctl_record(  # pragma: no cover - 実機 (Windows) でのみ動作
    api: _WinApi,
    handle: int,
    name: str,
    code: int,
    in_bytes: bytes,
    out_size: int,
    decoder: Callable[[bytes], dict[str, Any]],
    raw_limit: int | None = None,
) -> dict[str, Any]:
    """IOCTL を 1 回実行し、生バイト列 (hex) と解釈値を併記した記録を返す。

    raw_limit: 可変長の UTF-16 文字列（デバイスパス等）を含む構造体では、その手前までの
    固定長部分だけを hex で保存する。hex の中の文字列は sanitize_dump.py が検出できず、
    シリアル番号が素通りしてしまうため。文字列部分は decoded 側に平文で入る。
    """
    ok, err, data = api.ioctl(handle, code, in_bytes, out_size)
    rec: dict[str, Any] = {
        "ioctl": name,
        "code": f"0x{code:08X}",
        "ok": ok,
        "input_length": len(in_bytes),
        "input_hex_head": in_bytes[:16].hex(" "),
        "output_buffer_size": out_size,
        "bytes_returned": len(data),
    }
    if not ok:
        rec["win32_error"] = err
        rec["win32_error_message"] = api.format_error(err)
    shown = data if raw_limit is None else data[:raw_limit]
    rec["raw_hex"] = shown.hex(" ")
    if raw_limit is not None and len(data) > raw_limit:
        rec["raw_hex_note"] = (
            f"先頭 {raw_limit} バイトのみ"
            "（以降は UTF-16 文字列。サニタイズのため decoded 側に記録）"
        )
    rec["decoded"] = None
    if data:
        try:
            rec["decoded"] = decoder(data)
        except Exception as e:  # 解釈失敗でも生データは残す
            rec["decode_error"] = f"{type(e).__name__}: {e}"
    return rec


def _ioctl_two_step(  # pragma: no cover - 実機 (Windows) でのみ動作
    api: _WinApi,
    handle: int,
    name: str,
    code: int,
    port: int,
    first_size: int,
    decoder: Callable[[bytes], dict[str, Any]],
    raw_limit: int,
) -> dict[str, Any]:
    """ActualLength を返す可変長構造体を、USBView と同じく 2 回に分けて取得する。"""
    first = _ioctl_record(
        api, handle, name, code, struct.pack("<I", port).ljust(first_size, b"\0"), first_size,
        decoder, raw_limit,
    )  # fmt: skip
    actual = (first.get("decoded") or {}).get("ActualLength", 0)
    if not first["ok"] or actual <= first_size:
        return first
    return _ioctl_record(
        api, handle, name, code, struct.pack("<I", port).ljust(actual, b"\0"), actual,
        decoder, raw_limit,
    )  # fmt: skip


def _collect_port(  # pragma: no cover - 実機 (Windows) でのみ動作
    api: _WinApi, handle: int, port: int, usb_devices: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    rec: dict[str, Any] = {"port": port}
    rec["connection_information_ex"] = ex = _ioctl_record(
        api, handle, "IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX",
        IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX,
        struct.pack("<I", port).ljust(CONN_INFO_EX_BUFFER_SIZE, b"\0"),
        CONN_INFO_EX_BUFFER_SIZE, decode_connection_info_ex,
    )  # fmt: skip
    # 入力: ConnectionIndex, Length=sizeof, SupportedUsbProtocols=Usb300 のみ（USBView に準拠）。
    # 要実機検証: 出力の SupportedUsbProtocols がポートの対応プロトコルを表すか。
    v2_in = struct.pack("<IIII", port, CONN_INFO_EX_V2_SIZE, 1 << 2, 0)
    rec["connection_information_ex_v2"] = _ioctl_record(
        api, handle, "IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2",
        IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2, v2_in, CONN_INFO_EX_V2_SIZE,
        decode_connection_info_ex_v2,
    )  # fmt: skip
    rec["port_connector_properties"] = _ioctl_two_step(
        api, handle, "IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES",
        IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES, port, PORT_CONNECTOR_STRUCT_SIZE,
        decode_port_connector_properties, PORT_CONNECTOR_FIXED_SIZE,
    )  # fmt: skip

    decoded = ex.get("decoded") or {}
    rec["device"] = None
    if decoded.get("ConnectionStatus", 0) != 0:
        dk = _ioctl_two_step(
            api, handle, "IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME",
            IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME, port, NAME_STRUCT_SIZE,
            decode_name_struct, NAME_HEADER_SIZE,
        )  # fmt: skip
        rec["driver_key_name"] = dk
        key = ((dk.get("decoded") or {}).get("Name") or "").lower()
        rec["device"] = usb_devices.get(key) if key else None
    if decoded.get("DeviceIsHub"):
        rec["node_connection_name"] = _ioctl_two_step(
            api, handle, "IOCTL_USB_GET_NODE_CONNECTION_NAME",
            IOCTL_USB_GET_NODE_CONNECTION_NAME, port, NAME_STRUCT_SIZE,
            decode_name_struct, NAME_HEADER_SIZE,
        )  # fmt: skip
    return rec


def _collect_hub(  # pragma: no cover - 実機 (Windows) でのみ動作
    api: _WinApi, hub: dict[str, Any], usb_devices: dict[str, dict[str, Any]]
) -> None:
    handle, attempts = api.open_hub(hub["device_path"])
    hub["open_attempts"] = attempts
    if handle is None:
        hub["ports"] = []
        return
    try:
        hub["node_information"] = _ioctl_record(
            api, handle, "IOCTL_USB_GET_NODE_INFORMATION", IOCTL_USB_GET_NODE_INFORMATION,
            bytes(NODE_INFORMATION_SIZE), NODE_INFORMATION_SIZE, decode_node_information,
        )  # fmt: skip
        hub["hub_information_ex"] = _ioctl_record(
            api, handle, "IOCTL_USB_GET_HUB_INFORMATION_EX", IOCTL_USB_GET_HUB_INFORMATION_EX,
            bytes(HUB_INFORMATION_EX_SIZE), HUB_INFORMATION_EX_SIZE, decode_hub_information_ex,
        )  # fmt: skip
        hub["hub_capabilities_ex"] = _ioctl_record(
            api, handle, "IOCTL_USB_GET_HUB_CAPABILITIES_EX", IOCTL_USB_GET_HUB_CAPABILITIES_EX,
            bytes(HUB_CAPABILITIES_EX_SIZE), HUB_CAPABILITIES_EX_SIZE,
            decode_hub_capabilities_ex,
        )  # fmt: skip

        n_ports = (hub["hub_information_ex"].get("decoded") or {}).get("HighestPortNumber")
        hub["port_count_source"] = "hub_information_ex.HighestPortNumber"
        if not n_ports:
            node = hub["node_information"].get("decoded") or {}
            n_ports = (node.get("HubDescriptor") or {}).get("bNumberOfPorts", 0)
            hub["port_count_source"] = "node_information.HubDescriptor.bNumberOfPorts"
        hub["port_count"] = n_ports

        hub["ports"] = []
        for port in range(1, n_ports + 1):
            try:
                hub["ports"].append(_collect_port(api, handle, port, usb_devices))
            except Exception:
                hub["ports"].append({"port": port, "exception": traceback.format_exc()})
    finally:
        api.close(handle)


def _count_ioctls(dump: dict[str, Any]) -> dict[str, dict[str, int]]:
    """IOCTL 名ごとの成功 / 失敗件数を数える（meta 用）。"""
    counts: dict[str, dict[str, int]] = {}

    def add(rec: Any) -> None:
        if isinstance(rec, dict) and "ioctl" in rec:
            c = counts.setdefault(rec["ioctl"], {"ok": 0, "failed": 0})
            c["ok" if rec["ok"] else "failed"] += 1

    for hub in dump.get("hubs", []):
        for key in ("node_information", "hub_information_ex", "hub_capabilities_ex"):
            add(hub.get(key))
        for port in hub.get("ports", []):
            for value in port.values():
                add(value)
    return counts


def collect_windows(ctx: Context) -> None:  # pragma: no cover - 実機 (Windows) でのみ動作
    api = _WinApi()
    ctx.is_admin = api.is_admin()
    ctx.platform["windows_build"] = sys.getwindowsversion().build  # type: ignore[attr-defined]
    ctx.platform["pointer_size"] = ctypes.sizeof(ctypes.c_void_p)
    print(
        f"  管理者権限: {'あり' if ctx.is_admin else 'なし'}"
        "（この採取方式は通常、管理者権限なしで動作する想定です）"
    )

    dump: dict[str, Any] = {
        "schema": "usb-link-check/windows-raw-dump",
        "schema_version": 1,
        "collector_version": COLLECTOR_VERSION,
        "collected_at": _now_iso(),
        "platform": ctx.platform,
        "is_admin": ctx.is_admin,
        "host_controllers": [],
        "hubs": [],
    }

    try:
        dump["host_controllers"] = api.enum_interfaces(GUID_DEVINTERFACE_USB_HOST_CONTROLLER)
        ctx.record("ホストコントローラの列挙", True, count=len(dump["host_controllers"]))
    except Exception as e:
        ctx.record("ホストコントローラの列挙", False, error=f"{type(e).__name__}: {e}")

    usb_devices: dict[str, dict[str, Any]] = {}
    try:
        usb_devices = api.enum_usb_devices_by_driver_key()
        ctx.record("USB デバイスの列挙（driver key 対応表）", True, count=len(usb_devices))
    except Exception as e:
        ctx.record(
            "USB デバイスの列挙（driver key 対応表）", False, error=f"{type(e).__name__}: {e}"
        )

    try:
        hubs = [h for h in api.enum_interfaces(GUID_DEVINTERFACE_USB_HUB) if "device_path" in h]
        dump["hubs"] = hubs
        ctx.record("USB ハブの列挙 (GUID_DEVINTERFACE_USB_HUB)", bool(hubs), count=len(hubs))
    except Exception as e:
        hubs = []
        ctx.record(
            "USB ハブの列挙 (GUID_DEVINTERFACE_USB_HUB)", False, error=f"{type(e).__name__}: {e}"
        )

    access_denied = False
    for i, hub in enumerate(hubs):
        hub["index"] = i
        try:
            _collect_hub(api, hub, usb_devices)
        except Exception:
            hub["exception"] = traceback.format_exc()
        attempts = hub.get("open_attempts", [])
        if attempts and not attempts[-1]["ok"]:
            if any(a.get("win32_error") == ERROR_ACCESS_DENIED for a in attempts):
                access_denied = True
            ctx.record(
                f"ハブ[{i}] を開く", False,
                error=f"{attempts[-1].get('win32_error_message')} ({hub.get('description')})",
            )  # fmt: skip

    # 下流ハブへのリンク（ポートのノード名 == ハブのデバイスパス）を記録しておく。
    # サニタイズ後はパス中のシリアルが REDACTED になり照合できなくなるため、採取時に確定させる。
    by_path = {_norm_device_path(h["device_path"]): h["index"] for h in hubs}
    for hub in hubs:
        for port in hub.get("ports", []):
            name = ((port.get("node_connection_name") or {}).get("decoded") or {}).get("Name")
            if name:
                port["downstream_hub_index"] = by_path.get(_norm_device_path(name))

    ioctl_counts = _count_ioctls(dump)
    for name, c in sorted(ioctl_counts.items()):
        ok = c["failed"] == 0
        extra = {} if ok else {"error": f"{c['failed']} 件失敗（詳細は JSON 内）"}
        ctx.record(name, ok, succeeded=c["ok"], failed=c["failed"], **extra)
    if any(
        rec.get("win32_error") == ERROR_ACCESS_DENIED
        for hub in hubs
        for port in hub.get("ports", [])
        for rec in port.values()
        if isinstance(rec, dict)
    ):
        access_denied = True

    if access_denied:
        ctx.warn(
            "ERROR_ACCESS_DENIED（アクセス拒否）が発生しました。管理者権限が必要な可能性があります。"
            " PowerShell を「管理者として実行」で開き、同じコマンドを再実行してください。"
        )

    dump["ioctl_counts"] = ioctl_counts
    path = ctx.write_json(".json", dump)
    ctx.record("Windows 採取結果の保存", True, file=str(path))
    ctx.summary.extend(summarize_windows(dump))


def _bit_names(expanded: dict[str, Any] | None) -> str:
    if not expanded:
        return "?"
    on = [name for name, v in expanded["bits"].items() if v]
    unknown = expanded["unknown_bits_hex"]
    extra = f" 未知ビット={unknown}" if unknown != "0x00000000" else ""
    return f"{expanded['hex']} [{', '.join(on) or 'なし'}]{extra}"


def summarize_windows(dump: dict[str, Any]) -> list[str]:
    """Windows ダンプから、USBView と照合するための要約行を作る。"""
    lines: list[str] = []
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
            lines.append(
                f"      状態={ex.get('ConnectionStatus_name')}  "
                f"EX.Speed={ex.get('Speed')} ({ex.get('Speed_name')})  "
                f"bcdUSB={dd.get('bcdUSB_hex', '?')}"
            )
            v2_rec = port.get("connection_information_ex_v2") or {}
            v2 = v2_rec.get("decoded")
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
    if not lines:
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
        "files": [p.name for p in ctx.saved],
        "items": ctx.items,
        "failures": [i["name"] for i in ctx.failures],
        "warnings": ctx.warnings,
    }
    ctx.write_json(".meta.json", meta)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="usb-link-check 用の実機ダンプを採取する（標準ライブラリのみ）"
    )
    parser.add_argument(
        "--label",
        required=True,
        help="保存ファイル名の接頭辞（例: macos_ssd_fast, windows_usb2）",
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

    try:
        label = validate_label(args.label)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4

    system = platform.system()
    if system == "Darwin":
        collect = collect_macos
    elif system == "Windows":
        collect = collect_windows
    else:
        print("ERROR: このスクリプトは macOS と Windows のみ対応しています", file=sys.stderr)
        return 4

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

    ctx = Context(label, out_dir)
    print(f"採取開始: label={label}  OS={system} {platform.release()}")
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
