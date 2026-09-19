"""判定ロジックのテスト。SPEC.md 5章 判定表 A1-A5 / B1-B3 と 1:1 対応させる。

実機フィクスチャを使った A1 / A2 の検証は tests/test_windows_parser.py 側にある。
"""

import pytest

from usb_link_check.diagnosis import diagnose
from usb_link_check.models import Capability, ChainElement, Confidence, LinkSpeed

HIGH = LinkSpeed.HIGH_SPEED
SS = LinkSpeed.SUPER_SPEED
SSP = LinkSpeed.SUPER_SPEED_PLUS


def test_a1_cable_is_bottleneck_when_link_below_port_and_device() -> None:
    d = diagnose(HIGH, Capability.exact(SSP), Capability.exact(SS))
    assert d.rule == "A1"
    assert d.verdict == "IMPROVABLE"
    assert [e.kind for e in d.bottlenecks] == ["cable"]
    # C = L（EXACT）と確定できる
    cable = d.element("cable")
    assert cable is not None
    assert cable.capability == Capability.exact(HIGH)
    assert d.achievable_max == Capability.exact(SS)


def test_a1_suggestion_mentions_both_cable_and_port_wiring() -> None:
    """直挿しでも P の過大評価で A1 になりうるため断定しない（SPEC.md 4.2 既知の限界）。"""
    d = diagnose(HIGH, Capability.at_least(SS), Capability.exact(SS))
    assert d.rule == "A1"
    action = d.suggestions[0].action
    assert "ケーブル" in action
    assert "直挿し" in action
    assert "SuperSpeed 配線" in action


def test_a2_port_is_bottleneck() -> None:
    d = diagnose(HIGH, Capability.exact(HIGH), Capability.exact(SS))
    assert d.rule == "A2"
    assert d.verdict == "IMPROVABLE"
    assert [e.kind for e in d.bottlenecks] == ["port"]
    # ケーブルは L 以上としか分からない
    cable = d.element("cable")
    assert cable is not None
    assert cable.capability == Capability.at_least(HIGH)
    assert d.suggestions[0].certainty == "likely"


def test_a2_does_not_promise_a_specific_speed() -> None:
    d = diagnose(HIGH, Capability.exact(HIGH), Capability.exact(SS))
    expected = d.suggestions[0].expected
    assert expected.confidence is Confidence.AT_LEAST
    assert "上限はケーブル次第" in d.suggestions[0].action


def test_a3_device_is_the_ceiling() -> None:
    d = diagnose(SS, Capability.exact(SSP), Capability.exact(SS))
    assert d.rule == "A3"
    assert d.verdict == "OPTIMAL"
    assert [e.kind for e in d.bottlenecks] == ["device"]
    assert d.suggestions == []


def test_a4_optimal() -> None:
    d = diagnose(SS, Capability.exact(SS), Capability.exact(SS))
    assert d.rule == "A4"
    assert d.verdict == "OPTIMAL"
    assert d.bottlenecks == []
    assert d.exit_code == 0


def test_a4_when_port_is_at_least_and_device_is_the_same_speed() -> None:
    """P が AT_LEAST でも min(P, D) = D = L なら最高速を達成している（SPEC.md 5.1）。"""
    d = diagnose(SS, Capability.at_least(SS), Capability.exact(SS))
    assert d.rule == "A4"
    assert d.verdict == "OPTIMAL"


def test_a5_cannot_separate_port_from_cable_when_port_is_at_least() -> None:
    """L = P の下限値 < D で P が AT_LEAST のときは A1 と A2 を区別できない。"""
    d = diagnose(SS, Capability.at_least(SS), Capability.at_least(SSP))
    assert d.rule == "A5"
    assert d.verdict == "UNDETERMINED"
    assert d.exit_code == 3
    assert d.bottlenecks == []
    assert d.achievable_max == Capability.unknown()
    assert "速度が上がればケーブル" in d.suggestions[0].action
    assert d.suggestions[0].certainty == "unknown"


def test_at_least_device_at_its_lower_bound_is_not_treated_as_the_ceiling() -> None:
    """D が AT_LEAST でその下限値 = L のとき、A3（デバイスが天井）と断定してはならない。

    Low-Speed で動作している機器が、本当は High-Speed 対応かもしれない（SPEC.md 3.1）。
    """
    d = diagnose(
        LinkSpeed.LOW_SPEED,
        Capability.at_least(SS),
        Capability.at_least(LinkSpeed.LOW_SPEED),
    )
    assert d.rule == "B1"
    assert d.verdict != "OPTIMAL"


def test_b1_cable_or_device_when_device_capability_unknown() -> None:
    d = diagnose(HIGH, Capability.exact(SS), Capability.unknown())
    assert d.rule == "B1"
    assert d.verdict == "IMPROVABLE"
    assert d.bottlenecks == []
    assert "変わらなければデバイス側" in d.suggestions[0].action
    # 測れないものを数値で埋めない
    assert d.achievable_max == Capability.unknown()
    cable = d.element("cable")
    assert cable is not None
    assert cable.capability == Capability.unknown()


def test_b2_port_is_the_candidate_when_device_capability_unknown() -> None:
    d = diagnose(SS, Capability.exact(SS), Capability.unknown())
    assert d.rule == "B2"
    assert d.verdict == "IMPROVABLE"
    assert [e.kind for e in d.bottlenecks] == ["port"]
    assert d.achievable_max == Capability.unknown()


def test_b3_undetermined_when_port_capability_unknown() -> None:
    d = diagnose(HIGH, Capability.unknown(), Capability.unknown())
    assert d.rule == "B3"
    assert d.verdict == "UNDETERMINED"
    assert d.exit_code == 3


def test_hub_is_the_bottleneck_when_it_limits_the_upstream() -> None:
    """SPEC.md 5.3: 上流側の実効能力は min(P, H)。ハブが律速なら直結を提案する。"""
    hub = ChainElement("hub", "Generic USB Hub", Capability.exact(HIGH))
    d = diagnose(HIGH, Capability.at_least(SS), Capability.exact(SS), hubs=[hub])
    assert [e.kind for e in d.bottlenecks] == ["hub"]
    assert d.suggestions[0].target == "hub"
    assert "直結" in d.suggestions[0].action


def test_not_detected_returns_exit_code_2() -> None:
    d = diagnose(None, Capability.unknown(), Capability.unknown(), detected=False)
    assert d.verdict == "NOT_DETECTED"
    assert d.exit_code == 2


def test_unknown_link_speed_is_undetermined() -> None:
    d = diagnose(None, Capability.exact(SS), Capability.exact(SS))
    assert d.verdict == "UNDETERMINED"
    assert d.link_speed is None


@pytest.mark.parametrize(
    ("cap", "expected"),
    [
        (Capability.exact(SS), "5 Gbps"),
        (Capability.at_least(SS), "5 Gbps 以上（上限不明）"),
        (Capability.unknown(), "不明"),
        (Capability.exact(HIGH), "480 Mbps"),
        (Capability.exact(LinkSpeed.LOW_SPEED), "1.5 Mbps"),
    ],
)
def test_capability_display_never_hides_uncertainty(cap: Capability, expected: str) -> None:
    assert cap.display == expected
