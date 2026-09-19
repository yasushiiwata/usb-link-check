"""テキスト / JSON 出力 (SPEC.md 6章)。

確度が AT_LEAST の値には必ず「以上（上限不明）」を付け、UNKNOWN の値は「不明」と表示する。
数値を推測で埋めない。
"""

from __future__ import annotations

import datetime
import json
import unicodedata
from typing import Any

from .models import Capability, Confidence, Diagnosis, LinkSpeed, connector_display
from .platforms.base import DeviceSummary, PortSummary

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


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """全角文字を幅 2 として数え、表示幅 width まで空白で埋める。"""
    return text + " " * max(width - _width(text), 0)


#: 行頭に置かない文字（簡易的な禁則処理）
_NO_LINE_START = "、。，．）」』】〉〕］｝!?,.:;）"


def wrap_japanese(text: str, width: int, indent: str = "") -> list[str]:
    """日本語を表示幅で折り返す（SPEC.md 6.1）。

    rich の既定の折り返しは空白区切りのため「より 高速な ポート」のように不自然に切れる。
    ここでは表示幅で折り、行頭に句読点・閉じ括弧が来る場合は 1 文字前で折る。
    """
    limit = max(width - _width(indent), 8)
    lines: list[str] = []
    current = ""
    for ch in text:
        if _width(current) + _width(ch) > limit:
            if ch in _NO_LINE_START and current:
                current, moved = current[:-1], current[-1]
                lines.append(indent + current)
                current = moved + ch
            else:
                lines.append(indent + current)
                current = ch
        else:
            current += ch
    if current:
        lines.append(indent + current)
    return lines or [indent]


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
                "name": f"{e.name} [{e.detail}]" if e.detail else e.name,
                "speed_mbps": _capability_json(e.capability)[0],
                "confidence": _capability_json(e.capability)[1],
                "is_bottleneck": e.is_bottleneck,
            }
            for e in diagnosis.chain
        ],
        "achievable_max_mbps": max_mbps,
        "achievable_max_confidence": max_conf,
        "connector_type": diagnosis.connector_type,
        "emarker": (
            {
                "state": diagnosis.emarker.state,
                "certainty": diagnosis.emarker.certainty,
                "reason": diagnosis.emarker.reason,
            }
            if diagnosis.emarker is not None
            else None
        ),
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


def render_text(diagnosis: Diagnosis, *, width: int = 76) -> str:
    """既定のテキスト出力（SPEC.md 6.1）。rich のマークアップを含まない素のテキスト。"""
    lines = ["現在の接続"]
    for e in diagnosis.chain:
        label = _KIND_LABELS.get(e.kind, e.kind)
        value = e.capability.display
        if e.kind == "cable" and e.capability.is_known:
            value = f"推定 {value}"
        mark = "  ← ボトルネック" if e.is_bottleneck else ""
        head = f"  {_pad(label, 12)}: "
        continuation = " " * _width(head)
        name_and_value = f"{e.name}  {value}{mark}"
        if _width(head + name_and_value) <= width:
            lines.append(head + name_and_value)
        else:
            # 名前が長い場合は、名前を折り返してから能力を次の行に置く
            wrapped = wrap_japanese(e.name, width - _width(head))
            lines.append(head + wrapped[0])
            lines.extend(continuation + line for line in wrapped[1:])
            lines.append(f"{continuation}{value}{mark}")
        if e.detail:
            # 内部表記は照合に使うので折り返さずそのまま出す
            lines.append(f"{continuation}[{e.detail}]")
        if e.kind == "cable" and diagnosis.emarker is not None:
            emarker_lines = wrap_japanese(
                diagnosis.emarker.display, width, indent=" " * len(f"  {_pad('', 12)}: ")
            )
            lines.append(f"  {_pad('eMarker', 12)}: {emarker_lines[0].lstrip()}")
            lines.extend(emarker_lines[1:])
    lines.append("  " + "─" * 58)
    link = diagnosis.link_speed
    lines.append(f"  {_pad('リンク速度', 12)}: {link.display if link else '不明'}")
    lines.append("")
    lines.append(f"判定  {_VERDICT_HEADLINE[diagnosis.verdict]}")
    if diagnosis.rule:
        lines.append(f"      （判定表 {diagnosis.rule}）")
    lines.append(f"      現構成で到達しうる最高速: {diagnosis.achievable_max.display}")
    if diagnosis.suggestions:
        lines.append("")
        lines.append("改善提案")
        for i, s in enumerate(diagnosis.suggestions, 1):
            wrapped = wrap_japanese(s.action, width, indent="     ")
            lines.append(f"  {i}. {wrapped[0].lstrip()}")
            lines.extend(wrapped[1:])
            lines.append(f"     期待できる速度: {s.expected.display}（確度: {s.certainty}）")
    return "\n".join(lines)


def print_report(diagnosis: Diagnosis, *, file: Any = None) -> None:  # pragma: no cover
    """rich があれば色付きで、無ければ素のテキストで出力する。

    折り返しは自前で行う（rich の空白区切りの折り返しは日本語で不自然に切れる）。
    そのため rich 側の折り返しは無効にする。
    """
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        print(render_text(diagnosis), file=file)
        return
    console = Console(file=file)
    # パネルの枠と余白のぶんを引いた幅で折り返す
    text = render_text(diagnosis, width=max(console.width - 6, 40))
    style = _VERDICT_STYLE[diagnosis.verdict]
    body, _, verdict_part = text.partition("\n判定  ")
    console.print(Text(body, no_wrap=True, overflow="fold"))
    console.print(
        Panel(Text("判定  " + verdict_part, no_wrap=True, overflow="fold"), border_style=style)
    )


def _short(cap: Capability) -> str:
    """一覧用の短い能力表記。AT_LEAST は「5 Gbps+」と表す。"""
    if not cap.is_known or cap.speed is None:
        return "不明"
    return f"{cap.speed.short}+" if cap.confidence is not Confidence.EXACT else cap.speed.short


#: --list の 3 値の状態マーク（SPEC.md 6.1.1）
_STATUS_MARKS = {"underperforming": "⚠", "undetermined": "?", "ok": ""}
_STATUS_LEGEND = (
    "⚠ = 能力より遅くリンクしていることが確定（L < min(P, D)）。"
    "--device で詳細を確認してください。",
    "? = 判別できません（P または D の上限が不明なため、落ちているのか天井なのか分かりません）。",
    "無印 = 現構成で出せる最高速で動作していることが確定（L = min(P, D)）。"
    "ポートを変えれば速くなる構成もこれに該当します（--device で確認してください）。",
)


def render_device_list(devices: list[DeviceSummary]) -> str:
    """`--list` の出力（SPEC.md 6.1.1）。状態を ⚠ / ? / 無印 の 3 値で示す。"""
    if not devices:
        return "USB デバイスが見つかりませんでした。"
    widths = (2, 9, 10, 10, 10, 18)
    header = ("", "VID:PID", "L(実効)", "P(ポート)", "D(デバイス)", "位置")
    lines = ["  ".join(_pad(h, w) for h, w in zip(header, widths, strict=True)) + "  デバイス名"]
    for d in devices:
        mark = "  [マスストレージ]" if d.is_mass_storage else ""
        cols = (
            _STATUS_MARKS[d.status],
            d.vid_pid,
            d.link_speed.short if d.link_speed else "不明",
            _short(d.port_capability),
            _short(d.device_capability),
            d.location,
        )
        lines.append(
            "  ".join(_pad(v, w) for v, w in zip(cols, widths, strict=True)) + f"  {d.name}{mark}"
        )
    lines.append("")
    lines.extend(_STATUS_LEGEND)
    return "\n".join(lines)


def render_port_list(ports: list[PortSummary]) -> str:
    """`--list --all` の出力。空きポートも含め、その穴の正体を示す。"""
    if not ports:
        return "ポートが見つかりませんでした。"
    widths = (16, 12, 8, 10, 14)
    header = ("位置", "種別", "コネクタ", "P(ポート)", "コンパニオン")
    lines = ["  ".join(_pad(h, w) for h, w in zip(header, widths, strict=True)) + "  状態"]
    for p in ports:
        status = p.connection_status
        if not p.user_connectable:
            status += "（内部/接続不可）"
        cols = (
            p.location,
            p.kind,
            connector_display(p.connector),
            _short(p.capability),
            f"ポート{p.companion_port}" if p.companion_port else "なし",
        )
        lines.append(
            "  ".join(_pad(v, w) for v, w in zip(cols, widths, strict=True)) + f"  {status}"
        )
    lines.append("")
    lines.append(
        "「USB3コネクタ」はコンパニオンを持つか、そのポート自身が USB3 に対応している穴です。"
    )
    lines.append(
        "同じ物理コネクタが USB2 用と USB3 用の 2 つのポートとして現れます（SPEC.md 4.2）。"
    )
    return "\n".join(lines)
