"""tools/sanitize_dump.py のテストと、tests/fixtures/raw/ のサニタイズ漏れ検査。

注意: ここで使う文字列は置換パターンを確かめるための最小断片であり、実機出力の形式を
主張するものではない（パーサーのテストには使わないこと）。実機ダンプは fixtures/raw/ にのみ置く。
"""

from pathlib import Path

import pytest

import sanitize_dump
from sanitize_dump import REDACTED, sanitize_file, sanitize_text

RAW_DIR = Path(__file__).parent / "fixtures" / "raw"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('"serial_num" : "ABC123"', "ABC123"),
        ('"SerialNumber": "ABC123"', "ABC123"),
        ('"iSerialNumber": 3', "3"),
        ('"USB Serial Number" = "ABC123"', "ABC123"),
        ('"kUSBSerialNumberString" = "ABC123"', "ABC123"),
        ('"UsbDeviceSignature" = <8107915541424331>', "8107915541424331"),
        ('"volume_uuid" : "0000-1111"', "0000-1111"),
        ('{"iSerialNumber"=3,"idProduct"=1}', "=3"),
    ],
)
def test_sensitive_key_values_are_redacted(text: str, secret: str) -> None:
    out, count = sanitize_text(text)
    assert count == 1
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('"instance_id": "USB\\\\VID_0781&PID_5591\\\\ABC123"', "ABC123"),
        ("USB\\VID_0781&PID_5591&MI_00\\ABC123", "ABC123"),
        ('"Name": "USB#VID_05E3&PID_0626#ABC123#{f18a0e88-c30c-11d0-8815-00a0c906bed8}"', "ABC123"),
        ("\\\\?\\usb#vid_05e3&pid_0626#abc123#{f18a0e88-c30c-11d0-8815-00a0c906bed8}", "abc123"),
        ("USBSTOR\\Disk&Ven_X&Prod_Y&Rev_1\\ABC123&0", "ABC123"),
    ],
)
def test_windows_instance_ids_are_redacted(text: str, secret: str) -> None:
    out, count = sanitize_text(text)
    assert count == 1
    assert secret not in out
    # GUID 部分など、シリアル以外は残る
    if "{" in text:
        assert "{f18a0e88-c30c-11d0-8815-00a0c906bed8}" in out


@pytest.mark.parametrize(
    "text",
    [
        '"device_speed" : "super_speed"',
        '"Device Speed" = 3',
        '"idProduct" = 21905',
        '"device_path": "\\\\?\\usb#root_hub30#4&1a2b3c&0&0#{f18a0e88-c30c-11d0-8815-00a0c906bed8}',
    ],
)
def test_non_sensitive_values_are_kept(text: str) -> None:
    assert sanitize_text(text) == (text, 0)


def test_idempotent() -> None:
    text = (
        '{\n  "serial_num" : "ABC",\n  "instance_id": "USB\\\\VID_0781&PID_5591\\\\XYZ"\n}\n'
        '"USB Serial Number" = "ABC"\n'
    )
    once, n1 = sanitize_text(text)
    twice, n2 = sanitize_text(once)
    assert n1 == 3
    assert n2 == 0
    assert twice == once


def test_sanitize_file_preserves_other_bytes(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_bytes(b'  "USB Serial Number" = "ABC"\r\n  "Speed" = 3\r\n')
    assert sanitize_file(f) == 1
    assert f.read_bytes() == b'  "USB Serial Number" = "REDACTED"\r\n  "Speed" = 3\r\n'
    assert sanitize_file(f) == 0


def test_check_mode_does_not_write(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"serial_num" : "ABC"}', encoding="utf-8")
    assert sanitize_dump.main(["--check", str(f)]) == 1
    assert "ABC" in f.read_text(encoding="utf-8")
    assert sanitize_dump.main([str(f)]) == 0
    assert sanitize_dump.main(["--check", str(f)]) == 0


def test_fixtures_raw_contain_no_serial_numbers() -> None:
    """コミットされた実機ダンプにシリアル番号・固有 ID が残っていないこと（SPEC.md 10章）。"""
    leaks = {
        str(f.relative_to(RAW_DIR)): n
        for f in sanitize_dump.iter_target_files([RAW_DIR])
        if (n := sanitize_file(f, write=False))
    }
    assert leaks == {}, f"サニタイズ漏れ: {leaks}（python tools/sanitize_dump.py を実行すること）"
