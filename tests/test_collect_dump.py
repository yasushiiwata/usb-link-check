"""tools/collect_dump.py の OS 非依存部分のテスト。

構造体のデコード結果そのものは実機ダンプとの照合でしか検証できない（要実機検証）ため、
ここでは入力検証・ビット展開・対象デバイスの接続位置の特定（自前の出力スキーマ上の処理）
のみを確認する。
"""

import pytest

from collect_dump import (
    USB_PROTOCOL_BITS,
    V2_FLAG_BITS,
    companion_pairs,
    describe_windows_devices,
    expand_bits,
    find_windows_target,
    format_windows_device_list,
    main,
    parse_device_spec,
    validate_label,
)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("0781:5591", (0x0781, 0x5591)),
        ("781:5591", (0x0781, 0x5591)),
        ("abcd:EF01", (0xABCD, 0xEF01)),
    ],
)
def test_parse_device_spec(spec: str, expected: tuple[int, int]) -> None:
    assert parse_device_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "0781", "0781-5591", "12345:1", "SanDisk"])
def test_parse_device_spec_rejects_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_device_spec(spec)


# 以下はこのスクリプト自身が出力する辞書スキーマ（OS の出力形式ではない）を最小限に組み立てたもの。
ROOT_PATH = "\\\\?\\usb#root_hub30#4&0&0#{f18a0e88-c30c-11d0-8815-00a0c906bed8}"
EXT_PATH = "\\\\?\\usb#vid_05e3&pid_0626#5&0&0#{f18a0e88-c30c-11d0-8815-00a0c906bed8}"


def _port(
    port: int,
    *,
    vid: int = 0,
    pid: int = 0,
    protocols: int = 0,
    companion: int = 0,
    companion_hub: str = "",
    downstream: int | None = None,
) -> dict:
    rec: dict = {
        "port": port,
        "connection_information_ex": {
            "decoded": {
                "ConnectionStatus": 1 if vid else 0,
                "Speed": 2,
                "Speed_name": "UsbHighSpeed",
                "DeviceDescriptor": {"idVendor": vid, "idProduct": pid},
            }
        },
        "connection_information_ex_v2": {
            "decoded": {
                "SupportedUsbProtocols": expand_bits(protocols, USB_PROTOCOL_BITS),
                "Flags": expand_bits(0, V2_FLAG_BITS),
            }
        },
        "port_connector_properties": {
            "decoded": {
                "CompanionPortNumber": companion,
                "CompanionHubSymbolicLinkName": companion_hub,
            }
        },
    }
    if downstream is not None:
        rec["downstream_hub_index"] = downstream
    return rec


def _dump() -> dict:
    root_link = ROOT_PATH.replace("\\\\?\\", "").upper()
    return {
        "hubs": [
            {
                "index": 0,
                "device_path": ROOT_PATH,
                "ports": [
                    _port(3, protocols=0b011, downstream=1, vid=0x05E3, pid=0x0626),
                    _port(
                        5,
                        vid=0x0781,
                        pid=0x5591,
                        protocols=0b011,
                        companion=25,
                        companion_hub=root_link,
                    ),
                    _port(25, protocols=0b100, companion=5, companion_hub=root_link),
                ],
            },
            {
                "index": 1,
                "device_path": EXT_PATH,
                "ports": [_port(2, vid=0x1234, pid=0x0001, protocols=0b011)],
            },
        ]
    }


def test_find_windows_target_records_hub_port_and_companion() -> None:
    (m,) = find_windows_target(_dump(), 0x0781, 0x5591)
    assert (m["hub_index"], m["port"]) == (0, 5)
    assert m["path_from_root"] == [{"hub_index": 0, "port": 5}]
    assert m["port_supported_usb_protocols"]["value"] == 0b011
    assert m["companion"]["hub_index"] == 0
    assert m["companion"]["port"] == 25
    assert m["companion"]["supported_usb_protocols"]["value"] == 0b100


def test_find_windows_target_follows_downstream_hub() -> None:
    (m,) = find_windows_target(_dump(), 0x1234, 0x0001)
    assert m["path_from_root"] == [{"hub_index": 0, "port": 3}, {"hub_index": 1, "port": 2}]
    assert m["companion"] is None


def test_find_windows_target_not_found() -> None:
    assert find_windows_target(_dump(), 0xFFFF, 0xFFFF) == []


def test_format_windows_device_list_shows_hub_port_and_companion() -> None:
    lines = format_windows_device_list(describe_windows_devices(_dump()))
    ssd = next(line for line in lines if line.startswith("0781:5591"))
    assert "ハブ0/ポート5" in ssd
    assert "あり(ハブ0/ポート25)" in ssd
    other = next(line for line in lines if line.startswith("1234:0001"))
    assert "ハブ1/ポート2" in other
    assert "なし" in other
    hub = next(line for line in lines if line.startswith("05E3:0626"))
    assert "[ハブ]" not in hub  # DeviceIsHub は未設定


def test_list_and_device_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        main(["--list", "--device", "0781:5591"])


def test_companion_pairs() -> None:
    dump = _dump()
    assert companion_pairs(dump, dump["hubs"][0]) == [(5, 25)]


@pytest.mark.parametrize("label", ["macos_ssd_fast", "windows_usb2", "a.b-c_1"])
def test_validate_label_accepts_safe_names(label: str) -> None:
    assert validate_label(label) == label


@pytest.mark.parametrize("label", ["", "../x", "a/b", "a b", ".hidden", "-x"])
def test_validate_label_rejects_unsafe_names(label: str) -> None:
    with pytest.raises(ValueError):
        validate_label(label)


def test_expand_bits_keeps_raw_value_and_unknown_bits() -> None:
    out = expand_bits(0b1_0000_0101, V2_FLAG_BITS)
    assert out["value"] == 0b1_0000_0101
    assert out["hex"] == "0x00000105"
    assert list(out["bits"].values()) == [True, False, True, False]
    # 定義外のビット (bit 8) も捨てずに残す
    assert out["unknown_bits_hex"] == "0x00000100"
