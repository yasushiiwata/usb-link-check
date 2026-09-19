"""出力（テキスト / JSON）のテスト。SPEC.md 6章。"""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from usb_link_check.cli import build_parser, main, run
from usb_link_check.diagnosis import diagnose
from usb_link_check.models import Capability, LinkSpeed
from usb_link_check.platforms.windows import WindowsPlatform
from usb_link_check.report import render_device_list, render_text, to_json_dict

RAW = Path(__file__).parent / "fixtures" / "raw"
TARGET = (0x346D, 0x5678)

HIGH = LinkSpeed.HIGH_SPEED
SS = LinkSpeed.SUPER_SPEED


def platform_for(fixture: str) -> WindowsPlatform:
    return WindowsPlatform(json.loads((RAW / f"{fixture}.json").read_text(encoding="utf-8")))


def diagnosis_for(fixture: str):  # noqa: ANN201 - テスト内ヘルパー
    platform = platform_for(fixture)
    (device,) = [d for d in platform.devices() if (d.vid, d.pid) == TARGET]
    return platform.diagnose(device)


# ---------------------------------------------------------------- JSON 出力


def test_json_matches_spec_schema() -> None:
    payload = to_json_dict(
        diagnosis_for("windows_usb2"), "win32", timestamp="2026-09-20T12:00:00+09:00"
    )
    assert payload["schema_version"] == 1
    assert payload["os"] == "win32"
    assert payload["timestamp"] == "2026-09-20T12:00:00+09:00"
    assert payload["device"] == {"name": "USB Device", "vid": "0x346d", "pid": "0x5678"}
    assert payload["link_speed_mbps"] == 480
    assert payload["achievable_max_mbps"] == 5000
    # P ≥ 5Gbps かつ D = 5Gbps（EXACT）なので天井は 5Gbps で確定する
    assert payload["achievable_max_confidence"] == "exact"
    assert payload["verdict"] == "IMPROVABLE"
    kinds = [e["kind"] for e in payload["chain"]]
    assert kinds == ["port", "cable", "device"]
    for element in payload["chain"]:
        assert set(element) == {"kind", "name", "speed_mbps", "confidence", "is_bottleneck"}
        assert element["confidence"] in {"exact", "at_least", "unknown"}
    (suggestion,) = payload["suggestions"]
    assert set(suggestion) == {"target", "action", "expected_mbps", "certainty"}
    assert suggestion["certainty"] in {"confirmed", "likely", "unknown"}


def test_json_uses_null_and_unknown_for_unknown_values() -> None:
    d = diagnose(HIGH, Capability.exact(SS), Capability.unknown())  # B1
    payload = to_json_dict(d, "darwin")
    device = next(e for e in payload["chain"] if e["kind"] == "device")
    assert device["speed_mbps"] is None
    assert device["confidence"] == "unknown"
    assert payload["achievable_max_mbps"] is None
    assert payload["achievable_max_confidence"] == "unknown"


def test_json_is_serialisable_and_speeds_are_numbers() -> None:
    payload = json.loads(json.dumps(to_json_dict(diagnosis_for("windows_fast"), "win32")))
    assert payload["link_speed_mbps"] == 5000


# ---------------------------------------------------------------- テキスト出力


def test_text_marks_the_bottleneck_and_keeps_confidence() -> None:
    text = render_text(diagnosis_for("windows_usb2"))
    assert "← ボトルネック" in text
    assert "5 Gbps 以上（上限不明）" in text  # P は AT_LEAST
    assert "480 Mbps" in text
    assert "判定表 A1" in text


def test_text_shows_unknown_without_inventing_numbers() -> None:
    text = render_text(diagnose(HIGH, Capability.exact(SS), Capability.unknown()))
    device_line = next(line for line in text.splitlines() if "デバイス" in line)
    assert "不明" in device_line
    assert "到達しうる最高速: 不明" in text


def test_text_of_optimal_has_no_suggestions() -> None:
    text = render_text(diagnosis_for("windows_fast"))
    assert "改善提案" not in text
    assert "[OK]" in text


# ---------------------------------------------------------------- CLI


def run_cli(args: list[str], fixture: str) -> tuple[int, str]:
    parsed = build_parser().parse_args(args)
    out = io.StringIO()
    code = run(parsed, platform_for(fixture), out=out, err=io.StringIO())
    return code, out.getvalue()


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [("windows_fast", 0), ("windows_usb2", 1), ("windows_port2", 1)],
)
def test_cli_exit_codes_follow_the_verdict(fixture: str, expected_code: int) -> None:
    code, _ = run_cli(["--json"], fixture)
    assert code == expected_code


def test_cli_json_prints_only_json() -> None:
    _, out = run_cli(["--json", "--device", "346D:5678"], "windows_usb2")
    assert json.loads(out)["verdict"] == "IMPROVABLE"


def test_cli_list_shows_devices_and_exits_zero() -> None:
    code, out = run_cli(["--list"], "windows_fast")
    assert code == 0
    assert "346D:5678" in out
    assert "マスストレージ" in out


def test_cli_reports_not_detected_with_exit_code_2() -> None:
    code, out = run_cli(["--device", "FFFF:FFFF"], "windows_fast")
    assert code == 2
    assert "断定はできません" in out


def test_cli_does_not_choose_when_ambiguous() -> None:
    """曖昧な自動選択はしない（SPEC.md 7章）。"""
    code, out = run_cli(["--device", "USB"], "windows_fast")
    assert code == 3
    assert "1 つに定まりません" in out


def test_help_lists_every_documented_option(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    help_text = capsys.readouterr().out
    for option in ("--device", "--list", "--json", "--debug", "--version", "--help"):
        assert option in help_text


@pytest.mark.parametrize(
    ("system", "message"), [("Linux", "macOS と Windows"), ("Darwin", "未実装")]
)
def test_unsupported_os_exits_with_code_4(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, system: str, message: str
) -> None:
    monkeypatch.setattr("usb_link_check.cli.platform.system", lambda: system)
    assert main([]) == 4
    assert message in capsys.readouterr().err


def test_device_list_rendering_marks_unknown_speed() -> None:
    platform = platform_for("windows_fast")
    text = render_device_list(platform.devices())
    assert "5 Gbps" in text
    assert "480 Mbps" in text
