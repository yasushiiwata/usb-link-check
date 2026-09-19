"""Windows の USB 情報を Win32 API (ctypes) で採取する層 (SPEC.md 4.2)。

構造体レイアウト・IOCTL 番号・ビット割り当ては Windows SDK (usbioctl.h / usbiodef.h /
usbspec.h) の記述に基づく。実機ダンプで確定した項目は SPEC.md 4.2 に記録してある。
確定していない項目には「要実機検証」と記した。

解釈を誤っていても後から読み直せるよう、構造体は必ず生バイト列 (raw_hex) を併記する。
採取結果の辞書（ダンプ）を解析するのは platforms/windows.py 側であり、本モジュールは
採取だけを行う。tools/collect_dump.py も本モジュールを使う（採取ロジックを二重に持たない）。
"""

from __future__ import annotations

import ctypes
import datetime
import struct
import sys
import traceback
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from usb_link_check import __version__


class Recorder(Protocol):
    """採取の進捗・成否を記録する側（tools/collect_dump.py の Context）の最小要件。"""

    platform: dict[str, Any]
    is_admin: bool | None

    def record(self, name: str, ok: bool, **info: Any) -> None: ...

    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...


def _now_iso() -> str:
    """採取時刻（ローカルタイムゾーン付き ISO 8601）。"""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


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


# 経路: SPEC.md 4.2（USBView と同じ）
#   SetupDiGetClassDevs(GUID_DEVINTERFACE_USB_HUB) → CreateFile → DeviceIoControl

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
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = _usb_ctl(260)  # 0x220410
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
# USB_DESCRIPTOR_REQUEST: ConnectionIndex(4) + SetupPacket(8) + Data[]
DESCRIPTOR_REQUEST_HEADER_SIZE = 12
STRING_DESCRIPTOR_MAX = 255  # bLength は 1 バイト
USB_REQUEST_GET_DESCRIPTOR = 0x06
USB_STRING_DESCRIPTOR_TYPE = 0x03
DEFAULT_LANGID = 0x0409  # en-US。index 0 から取得できた最初の LANGID を優先する

# USB_DEVICE_SPEED (usbspec.h)。
# 実機ダンプで確定 (SPEC.md 4.2): SuperSpeed で動作中でも 2 (UsbHighSpeed) が返る。
# このフィールドから L を判定してはならない。L は platforms/windows.py の link_speed() で求める。
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
# USB_NODE_CONNECTION_INFORMATION_EX_V2_FLAGS のビット順（SPEC.md 4.2）。
# bit0 / bit1 は実機ダンプで確定。bit2 / bit3 は要実機検証（10Gbps デバイス未入手）。
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


def build_descriptor_request(
    port: int, desc_type: int, index: int, langid: int, length: int
) -> bytes:
    """USB_DESCRIPTOR_REQUEST（ConnectionIndex + SetupPacket）を組み立てる。"""
    setup = struct.pack(
        "<BBHHH",
        0x80,  # bmRequest: Device-to-host, Standard, Device
        USB_REQUEST_GET_DESCRIPTOR,
        (desc_type << 8) | index,
        langid,
        length,
    )
    return struct.pack("<I", port) + setup + bytes(length)


def decode_string_descriptor(raw: bytes) -> dict[str, Any]:
    """USB_DESCRIPTOR_REQUEST の応答から USB_STRING_DESCRIPTOR を取り出す。

    index 0 の応答は文字列ではなく LANGID の配列である。
    """
    _require(raw, DESCRIPTOR_REQUEST_HEADER_SIZE + 2, "USB_DESCRIPTOR_REQUEST")
    data = raw[DESCRIPTOR_REQUEST_HEADER_SIZE:]
    b_length, b_type = data[0], data[1]
    payload = data[2 : max(b_length, 2)]
    return {
        "bLength": b_length,
        "bDescriptorType": b_type,
        # 文字列は解釈済みの値だけを残す（hex に入れるとサニタイズで検出できないため）
        "string": payload.decode("utf-16-le", errors="replace").rstrip("\x00"),
        "langids": [
            int.from_bytes(payload[i : i + 2], "little") for i in range(0, len(payload) - 1, 2)
        ],
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
# CM_DRP_* は SPDRP_* + 1（cfgmgr32.h）
CM_DRP_DEVICEDESC = SPDRP_DEVICEDESC + 1
CM_DRP_FRIENDLYNAME = SPDRP_FRIENDLYNAME + 1
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
        cm.CM_Get_Child.argtypes = [p(_DWORD), _DWORD, ctypes.c_ulong]
        cm.CM_Get_Child.restype = ctypes.c_uint32
        cm.CM_Get_Sibling.argtypes = [p(_DWORD), _DWORD, ctypes.c_ulong]
        cm.CM_Get_Sibling.restype = ctypes.c_uint32
        cm.CM_Get_DevNode_Registry_PropertyW.argtypes = [
            _DWORD, ctypes.c_ulong, p(ctypes.c_ulong), vp, p(ctypes.c_ulong), ctypes.c_ulong
        ]  # fmt: skip
        cm.CM_Get_DevNode_Registry_PropertyW.restype = ctypes.c_uint32
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

    def _devnode_string(self, devinst: int, prop: int) -> str | None:
        """CM_Get_DevNode_Registry_PropertyW で文字列プロパティを読む。"""
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_string_buffer(size.value)
        regtype = ctypes.c_ulong(0)
        ret = self.cfgmgr32.CM_Get_DevNode_Registry_PropertyW(
            devinst, prop, ctypes.byref(regtype), buf, ctypes.byref(size), 0
        )
        if ret != 0:
            return None
        return _decode_utf16z(buf.raw[: size.value]) or None

    def _children(self, devinst: int, depth: int = 2) -> list[dict[str, Any]]:
        """子デバイスの名前を集める（USB マスストレージの製品名は子側に入る）。"""
        children: list[dict[str, Any]] = []
        if depth <= 0:
            return children
        child = _DWORD(0)
        if self.cfgmgr32.CM_Get_Child(ctypes.byref(child), devinst, 0) != 0:
            return children
        while True:
            children.append(
                {
                    "instance_id": self._instance_id(child.value),
                    "friendly_name": self._devnode_string(child.value, CM_DRP_FRIENDLYNAME),
                    "description": self._devnode_string(child.value, CM_DRP_DEVICEDESC),
                    "children": self._children(child.value, depth - 1),
                }
            )
            sibling = _DWORD(0)
            if self.cfgmgr32.CM_Get_Sibling(ctypes.byref(sibling), child.value, 0) != 0:
                break
            child = sibling
        return children

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
            "children": self._children(devinfo.DevInst),
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


def _string_descriptor(  # pragma: no cover - 実機 (Windows) でのみ動作
    api: _WinApi, handle: int, port: int, index: int, langid: int
) -> dict[str, Any]:
    return _ioctl_record(
        api, handle, "IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION (string)",
        IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION,
        build_descriptor_request(
            port, USB_STRING_DESCRIPTOR_TYPE, index, langid, STRING_DESCRIPTOR_MAX
        ),
        DESCRIPTOR_REQUEST_HEADER_SIZE + STRING_DESCRIPTOR_MAX,
        decode_string_descriptor,
        raw_limit=DESCRIPTOR_REQUEST_HEADER_SIZE,
    )  # fmt: skip


def _collect_strings(  # pragma: no cover - 実機 (Windows) でのみ動作
    api: _WinApi, handle: int, port: int, descriptor: dict[str, Any]
) -> dict[str, Any]:
    """製造者名・製品名の文字列ディスクリプタを取得する（表示用。SPEC.md 4.2 補助）。

    **iSerialNumber は意図的に要求しない**（機器の資産情報をダンプに残さないため。
    CLAUDE.md 必須ルール 3）。
    """
    result: dict[str, Any] = {}
    langid_rec = _string_descriptor(api, handle, port, 0, 0)
    langids = ((langid_rec.get("decoded") or {}).get("langids") or []) if langid_rec["ok"] else []
    langid = langids[0] if langids else DEFAULT_LANGID
    result["langid"] = f"0x{langid:04X}"
    result["langids_available"] = [f"0x{v:04X}" for v in langids]
    for field in ("iManufacturer", "iProduct"):
        index = descriptor.get(field) or 0
        if not index:
            continue
        rec = _string_descriptor(api, handle, port, index, langid)
        if rec["ok"]:
            result[field] = (rec.get("decoded") or {}).get("string")
        else:
            result[f"{field}_error"] = rec.get("win32_error_message")
    return result


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
        rec["strings"] = _collect_strings(api, handle, port, decoded.get("DeviceDescriptor") or {})
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


def build_dump(ctx: Recorder) -> dict[str, Any]:  # pragma: no cover - 実機 (Windows) でのみ動作
    """ハブ・ポート情報を採取してダンプ辞書を返す（保存はしない）。"""
    api = _WinApi()
    ctx.is_admin = api.is_admin()
    ctx.platform["windows_build"] = sys.getwindowsversion().build  # type: ignore[attr-defined]
    ctx.platform["pointer_size"] = ctypes.sizeof(ctypes.c_void_p)
    ctx.info(
        f"  管理者権限: {'あり' if ctx.is_admin else 'なし'}"
        "（この採取方式は通常、管理者権限なしで動作する想定です）"
    )

    dump: dict[str, Any] = {
        "schema": "usb-link-check/windows-raw-dump",
        "schema_version": 1,
        "collector_version": __version__,
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
    return dump
