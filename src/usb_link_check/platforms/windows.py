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
from ..models import (
    Capability,
    ChainElement,
    ConnectorType,
    Diagnosis,
    LinkSpeed,
    connector_display,
)
from .base import DeviceSummary, PortSummary, UsbPlatform

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
                    "friendly_name": dev.get("friendly_name"),
                    "child_names": _child_names(dev.get("children") or []),
                    "bus_reported_description": dev.get("bus_reported_description"),
                    "description": dev.get("description"),
                    "manufacturer": (rec_strings := p.get("strings") or {}).get("iManufacturer"),
                    "product": rec_strings.get("iProduct"),
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


def connector_type(rec: dict[str, Any]) -> ConnectorType:
    """ポートのコネクタ形状（SPEC.md 5.4）。

    要実機検証: 開発機のポートはすべて Type-A で、Type-C 側は実機で確認できていない。
    """
    props = rec.get("port_properties")
    if not props:
        return "unknown"
    bits = props.get("bits") or {}
    if "PortConnectorIsTypeC" not in bits:
        return "unknown"
    return "type_c" if bits["PortConnectorIsTypeC"] else "type_a"


def port_kind(rec: dict[str, Any]) -> str:
    """そのポートが USB3 対応コネクタか USB2 専用かの表示。"""
    protocols = port_protocols(rec)
    return "USB3コネクタ" if protocols & _USB300 else "USB2専用"


def port_name(rec: dict[str, Any]) -> str:
    """人間が読めるポート表記（SPEC.md 6.1）。"""
    parts = [port_kind(rec), connector_display(connector_type(rec))]
    c = rec.get("companion")
    parts.append(f"コンパニオン: ポート{c['port']}" if c else "コンパニオンなし")
    return f"ポート{rec['port']}（{' / '.join(parts)}）"


def port_detail(rec: dict[str, Any]) -> str | None:
    """OS の内部表記（USBView / UsbTreeView との照合用）。"""
    return rec.get("location_information")


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


def _child_names(children: list[dict[str, Any]]) -> list[str]:
    """子デバイスの FriendlyName を深さ優先で集める（マスストレージの製品名は子側に入る）。"""
    names: list[str] = []
    for child in children:
        if child.get("friendly_name"):
            names.append(child["friendly_name"])
        names.extend(_child_names(child.get("children") or []))
    return names


def _from_string_descriptors(rec: dict[str, Any]) -> str | None:
    """iManufacturer + iProduct を組み立てる（重複する接頭辞は付けない）。"""
    manufacturer = (rec.get("manufacturer") or "").strip()
    product = (rec.get("product") or "").strip()
    if not product:
        return manufacturer or None
    if manufacturer and not product.lower().startswith(manufacturer.lower()):
        return f"{manufacturer} {product}"
    return product


#: 名前の末尾から落とす汎用的な語（SPEC.md 4.2 補助）
_GENERIC_SUFFIXES = (
    "USB Device",
    "USB デバイス",
)


def _strip_generic_suffix(name: str) -> str:
    """末尾の汎用語を落とす（例: `Acer USB Flash Drive USB Device` → `Acer USB Flash Drive`）。

    落とした結果が空になる場合や、名前そのものが汎用語だけの場合は落とさない。
    """
    for suffix in _GENERIC_SUFFIXES:
        if len(name) > len(suffix) and name.lower().endswith(" " + suffix.lower()):
            trimmed = name[: -(len(suffix) + 1)].strip()
            if trimmed:
                return trimmed
    return name


def device_name(rec: dict[str, Any]) -> str:
    """表示用のデバイス名（SPEC.md 4.2 補助の解決順）。取得できたものを順に採用する。"""
    child_names = rec.get("child_names") or []
    child = child_names[0] if child_names else None
    strings = _from_string_descriptors(rec)
    # マスストレージは製品名が子デバイス（USBSTOR）側に入るので子を優先する。
    # それ以外の複合デバイスでは子が機能名（例: Bluetooth Device (PAN)）になるため後回しにする。
    preferred = (child, strings) if is_mass_storage(rec) else (strings, child)
    candidates = (
        rec.get("friendly_name"),
        *preferred,
        rec.get("bus_reported_description"),
        rec.get("description"),
        rec.get("device_description"),
    )
    for candidate in candidates:
        if candidate and candidate.strip():
            return _strip_generic_suffix(candidate.strip())
    return f"{rec.get('vid') or 0:04X}:{rec.get('pid') or 0:04X}"


def is_mass_storage(rec: dict[str, Any]) -> bool:
    service = (rec.get("service") or "").lower()
    return service in MASS_STORAGE_SERVICES or rec.get("device_class") == USB_CLASS_MASS_STORAGE


def to_summary(rec: dict[str, Any]) -> DeviceSummary:
    link = link_speed(rec)
    return DeviceSummary(
        name=device_name(rec),
        vid=rec.get("vid"),
        pid=rec.get("pid"),
        link_speed=link,
        location=f"ハブ{rec['hub_index']}/ポート{rec['port']}",
        is_mass_storage=is_mass_storage(rec),
        port_capability=port_capability(rec),
        device_capability=device_capability(rec, link),
        connector=connector_type(rec),
        port_kind=port_kind(rec),
        raw=rec,
    )


def describe_ports(dump: dict[str, Any]) -> list[PortSummary]:
    """全ポート（空きポートを含む）の正体を返す（`--list --all` 用）。"""
    hubs = dump.get("hubs", [])
    by_index = {h["index"]: h for h in hubs if "index" in h}
    ports: list[PortSummary] = []
    for hub in hubs:
        for p in hub.get("ports", []):
            ex = _decoded(p, "connection_information_ex")
            v2 = _decoded(p, "connection_information_ex_v2")
            comp = companion(by_index, p)
            rec = {
                "hub_index": hub["index"],
                "port": p["port"],
                "port_supported_usb_protocols": v2.get("SupportedUsbProtocols"),
                "companion": comp,
                "port_properties": _decoded(p, "port_connector_properties").get(
                    "UsbPortProperties"
                ),
            }
            props = (rec["port_properties"] or {}).get("bits") or {}
            ports.append(
                PortSummary(
                    hub_index=hub["index"],
                    port=p["port"],
                    kind=port_kind(rec),
                    connector=connector_type(rec),
                    capability=port_capability(rec),
                    companion_port=comp["port"] if comp else None,
                    user_connectable=bool(props.get("PortIsUserConnectable")),
                    connection_status=ex.get("ConnectionStatus_name") or "不明",
                    connected=bool(ex.get("ConnectionStatus", 0)),
                )
            )
    return ports


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
        connector_type=connector_type(rec),
    )
    diagnosis.vid = rec.get("vid")
    diagnosis.pid = rec.get("pid")
    port_element = diagnosis.element("port")
    if port_element is not None:
        port_element.detail = port_detail(rec)
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

    def ports(self) -> list[PortSummary]:
        return describe_ports(self.dump)


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
