"""Windows パーサーのテスト。すべて実機フィクスチャ（tests/fixtures/raw/）で検証する。

合成データではコンパニオンの扱いを誤っても気付けないため、SPEC.md 9.2 のとおり
実機ダンプを入力とする。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from usb_link_check.models import Capability, Confidence, LinkSpeed
from usb_link_check.platforms.windows import (
    WindowsPlatform,
    companion_pairs,
    connector_type,
    describe_devices,
    device_capability,
    device_name,
    find_devices,
    link_speed,
    port_capability,
    port_detail,
    port_name,
)

RAW = Path(__file__).parent / "fixtures" / "raw"
TARGET = (0x346D, 0x5678)  # 採取に使った USB メモリ

FIXTURES = ["windows_fast", "windows_usb2", "windows_port2", "windows_port11_ss_fail"]


def load(name: str) -> dict[str, Any]:
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))


def target_record(name: str) -> dict[str, Any]:
    (rec,) = find_devices(load(name), *TARGET)
    return rec


def diagnose(name: str):  # noqa: ANN201 - テスト内ヘルパー
    dump = load(name)
    platform = WindowsPlatform(dump)
    (device,) = [d for d in platform.devices() if (d.vid, d.pid) == TARGET]
    return platform.diagnose(device)


# ---------------------------------------------------------------- L の判定順序


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("windows_fast", LinkSpeed.SUPER_SPEED),
        ("windows_usb2", LinkSpeed.HIGH_SPEED),
        ("windows_port2", LinkSpeed.HIGH_SPEED),
        ("windows_port11_ss_fail", LinkSpeed.HIGH_SPEED),
    ],
)
def test_link_speed_uses_v2_flags_before_ex_speed(fixture: str, expected: LinkSpeed) -> None:
    """EX.Speed は SuperSpeed を表現しない（SPEC.md 4.2）。"""
    rec = target_record(fixture)
    assert rec["speed"] == 2  # 4 件すべて EX.Speed = 2 (UsbHighSpeed)
    assert link_speed(rec) is expected


def test_link_speed_of_5gbps_device_is_not_reported_as_usb2() -> None:
    """退行防止: EX.Speed だけを見ると 5Gbps 接続を 480Mbps と誤判定する。"""
    assert link_speed(target_record("windows_fast")) is not LinkSpeed.HIGH_SPEED


# ---------------------------------------------------------------- P の算出定義


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("windows_fast", Capability.at_least(LinkSpeed.SUPER_SPEED)),
        # USB3 コネクタ + USB2 ケーブル: 論理ポートは USB2 だがコンパニオンが USB3
        ("windows_usb2", Capability.at_least(LinkSpeed.SUPER_SPEED)),
        # USB2 専用コネクタ: コンパニオンなし
        ("windows_port2", Capability.exact(LinkSpeed.HIGH_SPEED)),
        ("windows_port11_ss_fail", Capability.at_least(LinkSpeed.SUPER_SPEED)),
    ],
)
def test_port_capability_is_the_connector_capability(fixture: str, expected: Capability) -> None:
    assert port_capability(target_record(fixture)) == expected


def test_port_capability_without_companion_is_exact_480mbps() -> None:
    rec = target_record("windows_port2")
    assert rec["companion"] is None
    assert port_capability(rec).confidence is Confidence.EXACT


def test_port_capability_uses_companion_not_just_the_logical_port() -> None:
    """コンパニオンを無視すると windows_usb2 の P を 480Mbps と誤る。"""
    rec = target_record("windows_usb2")
    assert rec["port_supported_usb_protocols"]["bits"]["Usb300"] is False
    assert rec["companion"]["supported_usb_protocols"]["bits"]["Usb300"] is True
    assert port_capability(rec).speed is LinkSpeed.SUPER_SPEED


def test_companion_pairs_are_bidirectional_in_fixture() -> None:
    dump = load("windows_fast")
    assert companion_pairs(dump, dump["hubs"][0]) == [(5, 25), (6, 21), (7, 24), (11, 23), (12, 22)]


# ---------------------------------------------------------------- D の判定ルール


@pytest.mark.parametrize("fixture", FIXTURES)
def test_device_capability_is_exact_5gbps_for_superspeed_capable_device(fixture: str) -> None:
    rec = target_record(fixture)
    assert device_capability(rec, link_speed(rec)) == Capability.exact(LinkSpeed.SUPER_SPEED)


def test_device_capability_of_full_speed_device_is_at_least(a_full_speed_record: dict) -> None:
    """SuperSpeed 非対応かつ L < 480Mbps のときは EXACT にしない（SPEC.md 4.2）。"""
    cap = device_capability(a_full_speed_record, link_speed(a_full_speed_record))
    assert cap.confidence is Confidence.AT_LEAST
    assert cap.speed is LinkSpeed.FULL_SPEED


@pytest.fixture
def a_full_speed_record() -> dict:
    # EKSA-H2 (3206:1032) は Full-Speed で接続されている USB2 デバイス
    (rec,) = find_devices(load("windows_fast"), 0x3206, 0x1032)
    return rec


# ------------------------------------- 実機データに無い分岐（10Gbps 機器・未知の値）
#
# 以下は自前の出力スキーマ上の分岐を確認するもの。ビット割り当て自体は未検証であり
# （SPEC.md 4.2: 10Gbps デバイス未入手）、10Gbps 機器のダンプが採取できたら差し替える。


def _flags(**bits: bool) -> dict[str, Any]:
    names = (
        "DeviceIsOperatingAtSuperSpeedOrHigher",
        "DeviceIsSuperSpeedCapableOrHigher",
        "DeviceIsOperatingAtSuperSpeedPlusOrHigher",
        "DeviceIsSuperSpeedPlusCapableOrHigher",
    )
    return {"v2_flags": {"bits": {n: bits.get(n, False) for n in names}}}


def test_link_speed_of_superspeed_plus_is_10gbps() -> None:
    rec = {"speed": 2, **_flags(DeviceIsOperatingAtSuperSpeedPlusOrHigher=True)}
    assert link_speed(rec) is LinkSpeed.SUPER_SPEED_PLUS


def test_device_capability_of_superspeed_plus_capable_is_at_least_10gbps() -> None:
    rec = {"speed": 2, **_flags(DeviceIsSuperSpeedPlusCapableOrHigher=True)}
    cap = device_capability(rec, link_speed(rec))
    assert cap == Capability.at_least(LinkSpeed.SUPER_SPEED_PLUS)


def test_unknown_ex_speed_value_is_not_guessed() -> None:
    """未知の値に遭遇しても例外で落とさず、推測もしない（SPEC.md 8章）。"""
    rec = {"speed": 7, **_flags()}
    assert link_speed(rec) is None
    assert device_capability(rec, None) == Capability.unknown()


def test_port_capability_is_unknown_when_no_usable_protocol_bit() -> None:
    rec: dict[str, Any] = {"port_supported_usb_protocols": {"value": 0b001}, "companion": None}
    assert port_capability(rec) == Capability.unknown()


# ---------------------------------------------------------------- 判定（中核）


def test_a1_windows_usb2_is_cable_bottleneck() -> None:
    d = diagnose("windows_usb2")
    assert d.rule == "A1"
    assert d.verdict == "IMPROVABLE"
    assert [e.kind for e in d.bottlenecks] == ["cable"]
    assert d.exit_code == 1


def test_a2_windows_port2_is_port_bottleneck() -> None:
    d = diagnose("windows_port2")
    assert d.rule == "A2"
    assert d.verdict == "IMPROVABLE"
    assert [e.kind for e in d.bottlenecks] == ["port"]


def test_a1_and_a2_differ_only_by_port_capability() -> None:
    """本ツールの中核。L も D も同じで、P だけが違う 2 件が別判定になること。"""
    usb2, port2 = diagnose("windows_usb2"), diagnose("windows_port2")
    assert usb2.link_speed is port2.link_speed is LinkSpeed.HIGH_SPEED
    assert usb2.element("device").capability == port2.element("device").capability
    assert usb2.element("port").capability != port2.element("port").capability
    assert (usb2.rule, port2.rule) == ("A1", "A2")
    assert [e.kind for e in usb2.bottlenecks] == ["cable"]
    assert [e.kind for e in port2.bottlenecks] == ["port"]


def test_a4_windows_fast_is_optimal() -> None:
    d = diagnose("windows_fast")
    assert d.rule == "A4"
    assert d.verdict == "OPTIMAL"
    assert d.link_speed is LinkSpeed.SUPER_SPEED
    assert d.exit_code == 0
    assert d.bottlenecks == []


def test_windows_fast_does_not_blame_the_cable() -> None:
    """SPEC.md 10: 高速接続時にケーブルをボトルネックと判定しないこと。"""
    d = diagnose("windows_fast")
    assert all(e.kind != "cable" for e in d.bottlenecks)


def test_port11_ss_fail_is_a1_with_port_wiring_hint() -> None:
    """直挿しでも P の過大評価により A1 になる（SPEC.md 4.2 既知の限界）。"""
    d = diagnose("windows_port11_ss_fail")
    assert d.rule == "A1"
    action = d.suggestions[0].action
    assert "直挿し" in action and "SuperSpeed 配線" in action


# ---------------------------------------------------------------- 表示用の値


@pytest.mark.parametrize("fixture", ["windows_fast", "windows_usb2", "windows_port2"])
def test_device_name_uses_the_child_product_name(fixture: str) -> None:
    """マスストレージは子デバイス側の製品名を採用し、末尾の汎用語を落とす。"""
    assert device_name(target_record(fixture)) == "Acer USB Flash Drive"


def test_device_name_falls_back_on_the_old_dump_format() -> None:
    """windows_port11_ss_fail は文字列ディスクリプタ・子デバイス名を持たない（SPEC.md 9.1）。

    名前解決が末尾までフォールバックする経路を実データで検証する。
    """
    rec = target_record("windows_port11_ss_fail")
    assert rec["child_names"] == []
    assert rec["manufacturer"] is None
    assert device_name(rec) == "USB Device"  # 汎用語だけの名前は落とさない


@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        # 1. FriendlyName が最優先
        ({"friendly_name": "My Drive", "service": "USBSTOR", "child_names": ["Child"]}, "My Drive"),
        # 2. マスストレージは子デバイス（USBSTOR 側に製品名が入る）
        (
            {
                "service": "USBSTOR",
                "child_names": ["Acer USB Flash Drive USB Device"],
                "manufacturer": "Acer",
                "product": "USB Device",
            },
            "Acer USB Flash Drive",  # 末尾の汎用語を落とす
        ),
        # 3. マスストレージ以外は文字列ディスクリプタを子より優先する
        (
            {
                "service": "usbccgp",
                "child_names": ["Bluetooth Device (Personal Area Network)"],
                "manufacturer": "AICSemi",
                "product": "AIC 8800D80",
            },
            "AICSemi AIC 8800D80",
        ),
        # 製造者名が製品名の接頭辞になっている場合は重ねない
        ({"manufacturer": "Acer", "product": "Acer Flash"}, "Acer Flash"),
        # 4 / 5. 文字列が無ければ BusReported → DeviceDesc
        ({"bus_reported_description": "USB Device", "description": "記憶装置"}, "USB Device"),
        ({"description": "USB 大容量記憶装置"}, "USB 大容量記憶装置"),
        # 6. 何も無ければ VID:PID
        ({"vid": 0x346D, "pid": 0x5678}, "346D:5678"),
    ],
)
def test_device_name_resolution_order(rec: dict[str, Any], expected: str) -> None:
    """SPEC.md 4.2 補助の解決順。"""
    assert device_name(rec) == expected


def test_connector_type_is_type_a_on_this_hardware() -> None:
    """要実機検証: Type-C ポートを持つ機体では type_c になるはず（開発機は全て Type-A）。"""
    assert connector_type(target_record("windows_fast")) == "type_a"


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        ({"PortConnectorIsTypeC": True}, "type_c"),
        ({"PortConnectorIsTypeC": False}, "type_a"),
        ({}, "unknown"),
    ],
)
def test_connector_type_reads_bit3(bits: dict[str, bool], expected: str) -> None:
    assert connector_type({"port_properties": {"bits": bits}}) == expected
    assert connector_type({}) == "unknown"


def test_port_name_and_detail() -> None:
    rec = target_record("windows_usb2")
    assert port_name(rec) == "ポート7（USB3コネクタ / Type-A / コンパニオン: ポート24）"
    assert port_detail(rec) == "Port_#0007.Hub_#0001"


def test_emarker_on_real_hardware_is_absent_because_ports_are_type_a() -> None:
    d = diagnose("windows_usb2")
    assert d.connector_type == "type_a"
    assert d.emarker is not None
    assert (d.emarker.state, d.emarker.certainty) == ("absent", "likely")


# ---------------------------------------------------------------- デバイス列挙


def test_low_speed_keyboard_is_not_reported_as_optimal() -> None:
    """実機データでの退行防止: D が AT_LEAST の機器を「最高速」と断定しない。"""
    platform = WindowsPlatform(load("windows_fast"))
    (keyboard,) = [d for d in platform.devices() if (d.vid, d.pid) == (0x04F2, 0x0400)]
    d = platform.diagnose(keyboard)
    assert d.link_speed is LinkSpeed.LOW_SPEED
    assert d.verdict != "OPTIMAL"
    assert d.rule == "B1"


def test_devices_excludes_hubs_and_finds_the_mass_storage() -> None:
    platform = WindowsPlatform(load("windows_fast"))
    devices = platform.devices()
    assert all(not d.raw["is_hub"] for d in devices)
    storage = [d for d in devices if d.is_mass_storage]
    assert [(d.vid, d.pid) for d in storage] == [TARGET]


def test_select_without_spec_picks_mass_storage_only() -> None:
    platform = WindowsPlatform(load("windows_fast"))
    assert [(d.vid, d.pid) for d in platform.select(None)] == [TARGET]


@pytest.mark.parametrize("spec", ["346d:5678", "346D:5678", "flash drive"])
def test_select_by_vid_pid_or_name(spec: str) -> None:
    platform = WindowsPlatform(load("windows_fast"))
    assert [(d.vid, d.pid) for d in platform.select(spec)] == [TARGET]


def test_select_unknown_device_returns_nothing() -> None:
    platform = WindowsPlatform(load("windows_fast"))
    assert platform.select("FFFF:FFFF") == []


@pytest.mark.parametrize("fixture", FIXTURES)
def test_all_fixtures_parse_without_unknown_values(fixture: str) -> None:
    """全フィクスチャで、対象デバイスの L / P / D が取得できること。"""
    rec = target_record(fixture)
    link = link_speed(rec)
    assert link is not None
    assert port_capability(rec).is_known
    assert device_capability(rec, link).is_known


@pytest.mark.parametrize("fixture", FIXTURES)
def test_describe_devices_finds_every_connected_port(fixture: str) -> None:
    dump = load(fixture)
    described = describe_devices(dump)
    connected = [
        p
        for h in dump["hubs"]
        for p in h["ports"]
        if (p["connection_information_ex"].get("decoded") or {}).get("ConnectionStatus")
    ]
    assert len(described) == len(connected)
