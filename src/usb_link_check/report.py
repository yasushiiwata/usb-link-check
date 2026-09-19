"""テキスト / JSON 出力 (SPEC.md 6章)。

確度が AT_LEAST の値には必ず「以上（上限不明）」を付け、UNKNOWN の値は「不明」と表示する。
数値を推測で埋めない。
"""

from __future__ import annotations

import datetime
import json
import unicodedata
from typing import Any

from .models import Capability, Diagnosis, LinkSpeed
from .platforms.base import DeviceSummary

SCHEMA_VERSION = 1

_KIND_LABELS = {
    "controller": "コントローラ",
    "hub": "ハブ",
    "port": "ポート",
    "cable": "ケーブル",
    "device": "デバイス",
}
_VERDICT_STYLE = {
    "OPTIMAL": "green",
    "IMPROVABLE": "yellow",
    "UNDETERMINED": "bright_black",
    "NOT_DETECTED": "red",
}
_VERDICT_HEADLINE = {
    "OPTIMAL": "[OK] 現構成の最高速で接続しています",
    "IMPROVABLE": "[FAIL] 改善の余地があります",
    "UNDETERMINED": "[UNKNOWN] 情報が足りず判定できません",
    "NOT_DETECTED": "[NOT FOUND] 対象デバイスが USB ツリーに見つかりません",
}


def _pad(text: str, width: int) -> str:
    """全角文字を幅 2 として数え、表示幅 width まで空白で埋める。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - shown, 0)


def _speed_mbps(speed: LinkSpeed | None) -> float | int | None:
    return speed.mbps if speed is not None else None


def _capability_json(cap: Capability) -> tuple[float | int | None, str]:
    if not cap.is_known:
        return None, "unknown"
    return _speed_mbps(cap.speed), cap.confidence.value


def to_json_dict(
    diagnosis: Diagnosis, os_name: str, *, timestamp: str | None = None
) -> dict[str, Any]:
    """SPEC.md 6.2 のスキーマに従う辞書を返す。"""
    max_mbps, max_conf = _capability_json(diagnosis.achievable_max)
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp or _now(),
        "os": os_name,
        "device": {
            "name": diagnosis.device_name,
            "vid": f"0x{diagnosis.vid:04x}" if diagnosis.vid is not None else None,
            "pid": f"0x{diagnosis.pid:04x}" if diagnosis.pid is not None else None,
        },
        "link_speed_mbps": _speed_mbps(diagnosis.link_speed),
        "chain": [
            {
                "kind": e.kind,
                "name": e.name,
                "speed_mbps": _capability_json(e.capability)[0],
                "confidence": _capability_json(e.capability)[1],
                "is_bottleneck": e.is_bottleneck,
            }
            for e in diagnosis.chain
        ],
        "achievable_max_mbps": max_mbps,
        "achievable_max_confidence": max_conf,
        "verdict": diagnosis.verdict,
        "rule": diagnosis.rule,
        "suggestions": [
            {
                "target": s.target,
                "action": s.action,
                "expected_mbps": _capability_json(s.expected)[0],
                "certainty": s.certainty,
            }
            for s in diagnosis.suggestions
        ],
    }


def to_json(diagnosis: Diagnosis, os_name: str, *, timestamp: str | None = None) -> str:
    return json.dumps(to_json_dict(diagnosis, os_name, timestamp=timestamp), ensure_ascii=False)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def render_text(diagnosis: Diagnosis) -> str:
    """既定のテキスト出力（SPEC.md 6.1）。rich のマークアップを含まない素のテキスト。"""
    lines = ["現在の接続"]
    for e in diagnosis.chain:
        label = _KIND_LABELS.get(e.kind, e.kind)
        value = e.capability.display
        if e.kind == "cable" and e.capability.is_known:
            value = f"推定 {value}"
        mark = "  ← ボトルネック" if e.is_bottleneck else ""
        lines.append(f"  {_pad(label, 12)}: {_pad(e.name, 32)} {value}{mark}")
    lines.append("  " + "─" * 58)
    link = diagnosis.link_speed
    lines.append(f"  {_pad('リンク速度', 12)}: {link.display if link else '不明'}")
    lines.append("")
    lines.append(f"判定  {_VERDICT_HEADLINE[diagnosis.verdict]}")
    if diagnosis.rule:
        lines.append(f"      （判定表 {diagnosis.rule}）")
    lines.append(f"      到達しうる最高速: {diagnosis.achievable_max.display}")
    if diagnosis.suggestions:
        lines.append("")
        lines.append("改善提案")
        for i, s in enumerate(diagnosis.suggestions, 1):
            lines.append(f"  {i}. {s.action}")
            lines.append(f"     期待できる速度: {s.expected.display}（確度: {s.certainty}）")
    return "\n".join(lines)


def print_report(diagnosis: Diagnosis, *, file: Any = None) -> None:  # pragma: no cover
    """rich があれば色付きで、無ければ素のテキストで出力する。"""
    text = render_text(diagnosis)
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        print(text, file=file)
        return
    console = Console(file=file)
    style = _VERDICT_STYLE[diagnosis.verdict]
    body, _, verdict_part = text.partition("\n判定  ")
    console.print(Text(body))
    console.print(Panel(Text("判定  " + verdict_part), border_style=style))


def render_device_list(devices: list[DeviceSummary]) -> str:
    """`--list` の出力。"""
    if not devices:
        return "USB デバイスが見つかりませんでした。"
    widths = (9, 12, 22)
    header = ("VID:PID", "リンク速度", "位置")
    lines = ["  ".join(_pad(h, w) for h, w in zip(header, widths, strict=True)) + "  デバイス名"]
    for d in devices:
        speed = d.link_speed.short if d.link_speed else "不明"
        mark = "  [マスストレージ]" if d.is_mass_storage else ""
        cols = (d.vid_pid, speed, d.location)
        lines.append(
            "  ".join(_pad(v, w) for v, w in zip(cols, widths, strict=True)) + f"  {d.name}{mark}"
        )
    return "\n".join(lines)
