"""Windows の USB ダンプ（win32_raw.build_dump の出力）を解析する層。

SPEC.md 4.2 の確定事項に従う。特に:
- L は V2.Flags → EX.Speed の順で判定する（EX.Speed は SuperSpeed を表現しない）
- P は「物理コネクタの能力」= そのポート ∪ コンパニオンポートの SupportedUsbProtocols
- D は V2.Flags から求める
実行層（IOCTL 呼び出し）は win32_raw 側にあり、本モジュールは辞書の解析だけを行う。
"""

from __future__ import annotations

from typing import Any

from ..diagnosis import diagnose
from ..models import Capability, ChainElement, Diagnosis, LinkSpeed
from .base import DeviceSummary, UsbPlatform

#: SetupAPI のサービス名からマスストレージを判定する（bDeviceClass は 0 のことがある）
MASS_STORAGE_SERVICES = {"usbstor", "uaspstor"}
USB_CLASS_MASS_STORAGE = 8

# USB_PROTOCOLS のビット（SPEC.md 4.2）
_USB110 = 1 << 0
_USB200 = 1 << 1
_USB300 = 1 << 2


def _decoded(owner: dict[str, Any] | None, key: str) -> dict[str, Any]:
    return ((owner or {}).get(key) or {}).get("decoded") or {}


def _find_port(hub: dict[str, Any] | None, port: int) -> dict[str, Any] | None:
    return next((p for p in (hub or {}).get("ports", []) if p.get("port") == port), None)


def _norm_device_path(path: str) -> str:
    p = path.lower()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if p.startswith(prefix):
            return p[len(prefix) :]
    return p


def companion(
    hubs_by_index: dict[int, dict[str, Any]], port: dict[str, Any]
) -> dict[str, Any] | None:
    """ポートのコンパニオン（同じ物理コネクタの USB2 / USB3 の片割れ）を返す。

    SPEC.md 4.2: USB3 コネクタは USB2 論理ポートと USB3 論理ポートの 2 つとして現れ、
    CompanionPortNumber で相互に参照し合う。コンパニオンは別のハブにあることがある。
    """
    pcp = _decoded(port, "port_connector_properties")
    cport = pcp.get("CompanionPortNumber")
    if not cport:
        return None
    link = _norm_device_path(pcp.get("CompanionHubSymbolicLinkName") or "")
    chub_index = next(
        (
            i
            for i, h in hubs_by_index.items()
            if _norm_device_path(h.get("device_path", "")) == link
        ),
        None,
    )
    cport_rec = _find_port(hubs_by_index.get(chub_index), cport) if chub_index is not None else None
    return {
        "hub_index": chub_index,
        "port": cport,
        "supported_usb_protocols": _decoded(cport_rec, "connection_information_ex_v2").get(
            "SupportedUsbProtocols"
        ),
    }


def companion_pairs(dump: dict[str, Any], hub: dict[str, Any]) -> list[tuple[int, int]]:
    """同一ハブ内のコンパニオン対応 (USB2 論理ポート, USB3 論理ポート) を返す。"""
    by_index = {h["index"]: h for h in dump.get("hubs", []) if "index" in h}
    pairs = set()
    for p in hub.get("ports", []):
        c = companion(by_index, p)
        if c and c["hub_index"] == hub.get("index"):
            pairs.add(tuple(sorted((p["port"], c["port"]))))
    return sorted(pairs)  # type: ignore[arg-type]


def describe_devices(dump: dict[str, Any]) -> list[dict[str, Any]]:
    """接続中の全デバイスについて、ハブ番号・ポート番号・経路・ポート能力を返す。"""
    hubs = dump.get("hubs", [])
    by_index = {h["index"]: h for h in hubs if "index" in h}
    parent_of: dict[int, tuple[int, int]] = {}
    for h in hubs:
        for p in h.get("ports", []):
            if p.get("downstream_hub_index") is not None:
                parent_of[p["downstream_hub_index"]] = (h["index"], p["port"])

    found: list[dict[str, Any]] = []
    for h in hubs:
        for p in h.get("ports", []):
            ex = _decoded(p, "connection_information_ex")
            dd = ex.get("DeviceDescriptor") or {}
            if ex.get("ConnectionStatus", 0) == 0:
                continue
            path = [{"hub_index": h["index"], "port": p["port"]}]
            cur, seen = h["index"], set()
            while cur in parent_of and cur not in seen:
                seen.add(cur)
                cur, up_port = parent_of[cur]
                path.insert(0, {"hub_index": cur, "port": up_port})
            v2 = _decoded(p, "connection_information_ex_v2")
            dev = p.get("device") or {}
            found.append(
                {
                    "vid": dd.get("idVendor"),
                    "pid": dd.get("idProduct"),
                    "is_hub": bool(ex.get("DeviceIsHub")),
                    "connection_status": ex.get("ConnectionStatus_name"),
                    "hub_index": h["index"],
                    "hub_description": h.get("description"),
                    "hub_type": _decoded(h, "hub_information_ex").get("HubType_name"),
                    "port": p["port"],
                    "path_from_root": path,
                    "device_description": dev.get("bus_reported_description")
                    or dev.get("description"),
                    "device_class": dd.get("bDeviceClass"),
                    "service": dev.get("service"),
                    "location_information": dev.get("location_information"),
                    "port_supported_usb_protocols": v2.get("SupportedUsbProtocols"),
                    "port_properties": _decoded(p, "port_connector_properties").get(
                        "UsbPortProperties"
                    ),
                    "companion": companion(by_index, p),
                    "speed": ex.get("Speed"),
                    "speed_name": ex.get("Speed_name"),
                    "v2_flags": v2.get("Flags"),
                }
            )
    return found


def find_devices(dump: dict[str, Any], vid: int, pid: int) -> list[dict[str, Any]]:
    """VID:PID が一致する接続中デバイスを返す。"""
    return [d for d in describe_devices(dump) if (d["vid"], d["pid"]) == (vid, pid)]


def _flag(rec: dict[str, Any], name: str) -> bool:
    return bool(((rec.get("v2_flags") or {}).get("bits") or {}).get(name))


def link_speed(rec: dict[str, Any]) -> LinkSpeed | None:
    """L を判定する（SPEC.md 4.2「L の判定順序」）。

    EX.Speed は SuperSpeed を表現しないため、必ず V2.Flags を先に見る。
    要実機検証: 20 Gbps (Gen2x2) は API 上 SuperSpeedPlus と区別できないため 10 Gbps とする。
    """
    if _flag(rec, "DeviceIsOperatingAtSuperSpeedPlusOrHigher"):
        return LinkSpeed.SUPER_SPEED_PLUS
    if _flag(rec, "DeviceIsOperatingAtSuperSpeedOrHigher"):
        return LinkSpeed.SUPER_SPEED
    return {
        0: LinkSpeed.LOW_SPEED,
        1: LinkSpeed.FULL_SPEED,
        2: LinkSpeed.HIGH_SPEED,
    }.get(rec.get("speed"))  # 未知の値は推測せず None（UNKNOWN）


def _protocols(expanded: dict[str, Any] | None) -> int:
    return int((expanded or {}).get("value") or 0)


def port_protocols(rec: dict[str, Any]) -> int:
    """P の根拠となる SupportedUsbProtocols の和集合（SPEC.md 4.2）。"""
    own = _protocols(rec.get("port_supported_usb_protocols"))
    comp = _protocols((rec.get("companion") or {}).get("supported_usb_protocols"))
    return own | comp


def capability_from_protocols(protocols: int) -> Capability:
    """SupportedUsbProtocols の和集合から P を求める（SPEC.md 4.2 の表）。"""
    if protocols & _USB300:
        # Usb300 は 5 / 10 / 20 Gbps を区別しないため上限は不明
        return Capability.at_least(LinkSpeed.SUPER_SPEED)
    if protocols & _USB200:
        return Capability.exact(LinkSpeed.HIGH_SPEED)
    return Capability.unknown()


def port_capability(rec: dict[str, Any]) -> Capability:
    """P（物理コネクタの能力）。論理ポート単体の値では判定しない。"""
    return capability_from_protocols(port_protocols(rec))


def device_capability(rec: dict[str, Any], link: LinkSpeed | None) -> Capability:
    """D（デバイス能力）を判定する（SPEC.md 4.2「D の判定ルール」）。"""
    if _flag(rec, "DeviceIsSuperSpeedPlusCapableOrHigher"):
        return Capability.at_least(LinkSpeed.SUPER_SPEED_PLUS)
    if _flag(rec, "DeviceIsSuperSpeedCapableOrHigher"):
        return Capability.exact(LinkSpeed.SUPER_SPEED)
    if link is None:
        return Capability.unknown()
    if link is LinkSpeed.HIGH_SPEED:
        return Capability.exact(LinkSpeed.HIGH_SPEED)
    # SuperSpeed 非対応は分かるが、High-Speed 機が不良ケーブルで落ちている可能性を排除できない
    return Capability.at_least(link)


def port_name(rec: dict[str, Any]) -> str:
    """表示用のポート名。ハブ番号・ポート番号とコンパニオンの有無を含める。"""
    protocols = port_protocols(rec)
    kind = "USB3 コネクタ" if protocols & _USB300 else "USB2 コネクタ"
    location = rec.get("location_information") or f"ハブ{rec['hub_index']}/ポート{rec['port']}"
    return f"{location} ({kind})"


def hub_elements(dump: dict[str, Any], rec: dict[str, Any]) -> list[ChainElement]:
    """経路上の外部ハブを上流から順に返す（ルートハブはポートとして扱うので除く）。

    TODO: 実機ダンプ待ち（外部ハブ経由の構成）。現在のフィクスチャ 4 件はいずれも
    ルートハブ直結のため、この関数は実機データで検証できていない（SPEC.md 5.3）。
    """
    by_index = {h["index"]: h for h in dump.get("hubs", []) if "index" in h}
    elements: list[ChainElement] = []
    path = rec.get("path_from_root") or []
    for upstream, entry in zip(path, path[1:], strict=False):
        hub = by_index.get(entry["hub_index"])
        if hub is None:
            continue
        upstream_port = _find_port(by_index.get(upstream["hub_index"]), upstream["port"])
        # ハブの能力は、そのハブが挿さっている上流ポートの能力で決まる
        upstream_rec = {
            "port_supported_usb_protocols": _decoded(
                upstream_port, "connection_information_ex_v2"
            ).get("SupportedUsbProtocols"),
            "companion": companion(by_index, upstream_port or {}),
        }
        elements.append(
            ChainElement(
                kind="hub",
                name=hub.get("description") or f"ハブ{entry['hub_index']}",
                capability=port_capability(upstream_rec),
            )
        )
    return elements


def device_name(rec: dict[str, Any]) -> str:
    return rec.get("device_description") or f"{rec.get('vid') or 0:04X}:{rec.get('pid') or 0:04X}"


def is_mass_storage(rec: dict[str, Any]) -> bool:
    service = (rec.get("service") or "").lower()
    return service in MASS_STORAGE_SERVICES or rec.get("device_class") == USB_CLASS_MASS_STORAGE


def to_summary(rec: dict[str, Any]) -> DeviceSummary:
    return DeviceSummary(
        name=device_name(rec),
        vid=rec.get("vid"),
        pid=rec.get("pid"),
        link_speed=link_speed(rec),
        location=f"ハブ{rec['hub_index']}/ポート{rec['port']}",
        is_mass_storage=is_mass_storage(rec),
        raw=rec,
    )


def diagnose_device(dump: dict[str, Any], rec: dict[str, Any]) -> Diagnosis:
    """1 台のデバイスについて L / P / D を求めて判定する。"""
    link = link_speed(rec)
    diagnosis = diagnose(
        link,
        port_capability(rec),
        device_capability(rec, link),
        port_name=port_name(rec),
        device_name=device_name(rec),
        hubs=hub_elements(dump, rec),
    )
    diagnosis.vid = rec.get("vid")
    diagnosis.pid = rec.get("pid")
    return diagnosis


class WindowsPlatform(UsbPlatform):
    """Windows 実装。`dump` を渡せば実機なしで動作する（テスト用スタブ）。"""

    os_name = "win32"

    def __init__(self, dump: dict[str, Any] | None = None) -> None:
        self._dump = dump

    def load(self) -> dict[str, Any]:  # pragma: no cover - 実機 (Windows) でのみ動作
        if self._dump is None:
            from . import win32_raw

            self._dump = win32_raw.build_dump(_SilentRecorder())
        return self._dump

    @property
    def dump(self) -> dict[str, Any]:
        return self.load()

    def devices(self) -> list[DeviceSummary]:
        return [to_summary(rec) for rec in describe_devices(self.dump) if not rec["is_hub"]]

    def diagnose(self, device: DeviceSummary) -> Diagnosis:
        return diagnose_device(self.dump, device.raw)


class _SilentRecorder:  # pragma: no cover - 実機 (Windows) でのみ動作
    """win32_raw.build_dump が要求する記録先。CLI では進捗を表示しない。"""

    def __init__(self) -> None:
        self.platform: dict[str, Any] = {}
        self.is_admin: bool | None = None
        self.items: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def record(self, name: str, ok: bool, **info: Any) -> None:
        self.items.append({"name": name, "ok": ok, **info})

    def info(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        self.warnings.append(message)
