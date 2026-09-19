"""tools/collect_dump.py の OS 非依存部分のテスト。

構造体のデコード結果そのものは実機ダンプとの照合でしか検証できない（要実機検証）ため、
ここでは入力検証とビット展開の性質のみを確認する。
"""

import pytest

from collect_dump import V2_FLAG_BITS, expand_bits, validate_label


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
