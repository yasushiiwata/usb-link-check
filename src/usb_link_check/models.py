"""LinkSpeed, Confidence, Capability, ChainElement, Diagnosis (SPEC.md 3章)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class LinkSpeed(float, Enum):
    """リンク速度。内部表現は必ず Mbps の数値とする（SPEC.md 3.1）。

    float を継承しているため `<` と `min()` がそのまま使える。
    """

    LOW_SPEED = 1.5
    FULL_SPEED = 12
    HIGH_SPEED = 480
    SUPER_SPEED = 5000
    SUPER_SPEED_PLUS = 10000
    SUPER_SPEED_PLUS_2X2 = 20000

    @property
    def mbps(self) -> float | int:
        """JSON 出力用。整数で表せる値は int にする。"""
        return int(self.value) if float(self.value).is_integer() else self.value

    @property
    def display(self) -> str:
        return _SPEED_DISPLAY[self]

    @property
    def short(self) -> str:
        """`5 Gbps` のような短い表記。"""
        if self.value >= 1000:
            return f"{self.value / 1000:g} Gbps"
        return f"{self.value:g} Mbps"


_SPEED_DISPLAY = {
    LinkSpeed.LOW_SPEED: "USB 1.0 Low-Speed (1.5 Mbps)",
    LinkSpeed.FULL_SPEED: "USB 1.1 Full-Speed (12 Mbps)",
    LinkSpeed.HIGH_SPEED: "USB 2.0 High-Speed (480 Mbps)",
    LinkSpeed.SUPER_SPEED: "USB 3.2 Gen1 / SuperSpeed (5 Gbps)",
    LinkSpeed.SUPER_SPEED_PLUS: "USB 3.2 Gen2 / SuperSpeed+ (10 Gbps)",
    LinkSpeed.SUPER_SPEED_PLUS_2X2: "USB 3.2 Gen2x2 (20 Gbps)",
}


class Confidence(Enum):
    """確度（SPEC.md 3.2）。断定できることとできないことを分けるための値。"""

    EXACT = "exact"
    AT_LEAST = "at_least"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capability:
    """能力値と確度の組（SPEC.md 3.3）。"""

    speed: LinkSpeed | None  # UNKNOWN のとき None
    confidence: Confidence

    @classmethod
    def exact(cls, speed: LinkSpeed) -> Capability:
        return cls(speed, Confidence.EXACT)

    @classmethod
    def at_least(cls, speed: LinkSpeed) -> Capability:
        return cls(speed, Confidence.AT_LEAST)

    @classmethod
    def unknown(cls) -> Capability:
        return cls(None, Confidence.UNKNOWN)

    @property
    def is_known(self) -> bool:
        return self.speed is not None and self.confidence is not Confidence.UNKNOWN

    @property
    def lower_bound(self) -> LinkSpeed | None:
        """判定に使う下限値。EXACT なら確定値、AT_LEAST なら「これ以上」の値。"""
        return self.speed if self.is_known else None

    @property
    def display(self) -> str:
        """SPEC.md 3.2 / 6.1 の表示規則。推測値で穴埋めしない。"""
        if not self.is_known or self.speed is None:
            return "不明"
        if self.confidence is Confidence.AT_LEAST:
            return f"{self.speed.short} 以上（上限不明）"
        return self.speed.short


#: ポートのコネクタ形状（PortConnectorIsTypeC より。SPEC.md 5.4）
ConnectorType = Literal["type_c", "type_a", "unknown"]

_CONNECTOR_DISPLAY: dict[str, str] = {
    "type_c": "Type-C",
    "type_a": "Type-A",
    "unknown": "不明",
}


@dataclass(frozen=True)
class EMarker:
    """eMarker 搭載の推定（SPEC.md 5.4）。

    直接読み取る手段が無いため、**必ず推定**である。`EXACT` に相当する状態を持たない。
    """

    state: Literal["likely_present", "absent", "unknown"]
    certainty: Literal["likely", "unknown"]
    reason: str

    @property
    def display(self) -> str:
        label = {
            "likely_present": "あると推定",
            "absent": "なし",
            "unknown": "不明",
        }[self.state]
        return f"{label}（{self.reason}）確度: {self.certainty}"


def connector_display(connector: ConnectorType) -> str:
    return _CONNECTOR_DISPLAY[connector]


Verdict = Literal["OPTIMAL", "IMPROVABLE", "UNDETERMINED", "NOT_DETECTED"]

#: 判定と終了コードの対応（SPEC.md 6.3）
EXIT_CODES: dict[str, int] = {
    "OPTIMAL": 0,
    "IMPROVABLE": 1,
    "NOT_DETECTED": 2,
    "UNDETERMINED": 3,
}
EXIT_ERROR = 4

ElementKind = Literal["controller", "hub", "port", "cable", "device"]


@dataclass
class ChainElement:
    """接続チェーンの 1 要素（SPEC.md 3.4）。"""

    kind: ElementKind
    name: str
    capability: Capability
    is_bottleneck: bool = False
    #: OS の内部表記など、折り返さずにそのまま見せたい補助情報（例: Port_#0024.Hub_#0001）
    detail: str | None = None


@dataclass
class Suggestion:
    """改善提案（SPEC.md 3.4）。"""

    target: str  # "cable" / "port" / "hub"
    action: str  # 人間向けの日本語文
    expected: Capability
    certainty: Literal["confirmed", "likely", "unknown"]


@dataclass
class Diagnosis:
    """診断結果（SPEC.md 3.4）。"""

    link_speed: LinkSpeed | None
    chain: list[ChainElement]
    achievable_max: Capability
    verdict: Verdict
    suggestions: list[Suggestion] = field(default_factory=list)
    #: 判定表の行番号（"A1" 等）。表とテストを 1:1 対応させるために保持する。
    rule: str | None = None
    #: 対象デバイスの表示情報（名前・VID・PID）
    device_name: str | None = None
    vid: int | None = None
    pid: int | None = None
    #: ポートのコネクタ形状と eMarker の推定（SPEC.md 5.4）
    connector_type: ConnectorType = "unknown"
    emarker: EMarker | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def element(self, kind: ElementKind) -> ChainElement | None:
        return next((e for e in self.chain if e.kind == kind), None)

    @property
    def bottlenecks(self) -> list[ChainElement]:
        return [e for e in self.chain if e.is_bottleneck]
