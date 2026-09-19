"""tools/collect_dump.py（採取スクリプト）のテスト。

Windows の採取処理・解析処理はパッケージ側にあるため（test_windows_parser.py で検証）、
ここでは採取スクリプト固有の部分（引数の検証・一覧表示の整形）だけを確認する。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from collect_dump import (
    format_windows_device_list,
    main,
    parse_device_spec,
    summarize_windows,
    validate_label,
)
from usb_link_check.platforms.windows import companion_pairs, describe_devices

RAW = Path(__file__).parent / "fixtures" / "raw"


def load(name: str) -> dict[str, Any]:
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("label", ["macos_ssd_fast", "windows_usb2", "a.b-c_1"])
def test_validate_label_accepts_safe_names(label: str) -> None:
    assert validate_label(label) == label


@pytest.mark.parametrize("label", ["", "../x", "a/b", "a b", ".hidden", "-x"])
def test_validate_label_rejects_unsafe_names(label: str) -> None:
    with pytest.raises(ValueError):
        validate_label(label)


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


def test_list_and_device_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        main(["--list", "--device", "0781:5591"])


def test_device_list_shows_hub_port_and_companion() -> None:
    lines = format_windows_device_list(describe_devices(load("windows_usb2")))
    ssd = next(line for line in lines if line.startswith("346D:5678"))
    assert "ハブ0/ポート7" in ssd
    assert "あり(ハブ0/ポート24)" in ssd
    # windows_port2 では同じデバイスがコンパニオンなしのポートに現れる
    port2 = next(
        line
        for line in format_windows_device_list(describe_devices(load("windows_port2")))
        if line.startswith("346D:5678")
    )
    assert "ハブ0/ポート8" in port2
    assert "なし" in port2


def test_device_list_shows_effective_speed_not_ex_speed_name() -> None:
    """EX.Speed の名前（5Gbps でも UsbHighSpeed）を速度として見せない（SPEC.md 4.2）。"""
    lines = format_windows_device_list(describe_devices(load("windows_fast")))
    ssd = next(line for line in lines if line.startswith("346D:5678"))
    assert "5 Gbps" in ssd
    assert "UsbHighSpeed" not in ssd


def test_summary_shows_effective_link_speed_not_only_ex_speed() -> None:
    """EX.Speed だけを見せると誤解を招くため、実効リンク速度を併記する（要望による）。"""
    lines = summarize_windows(load("windows_fast"), companion_pairs=companion_pairs)
    target_block = "\n".join(lines[:8])
    assert "実効リンク速度" in target_block
    assert "5 Gbps" in target_block
    assert "EX.Speed=2" in target_block  # 生値も併記する


def test_summary_lists_companion_pairs() -> None:
    lines = summarize_windows(load("windows_fast"), companion_pairs=companion_pairs)
    assert any("5↔25" in line and "7↔24" in line for line in lines)
