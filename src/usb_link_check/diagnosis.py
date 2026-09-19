"""SPEC.md 5章の判定ロジック（OS 非依存・純関数）。

`L = min(P, C, D)` を逆算して C（ケーブル能力）を推論する。
C は測定できないため、推論できない値は UNKNOWN のまま返す（SPEC.md 1.1）。
"""

from __future__ import annotations

from .models import (
    Capability,
    ChainElement,
    Confidence,
    ConnectorType,
    Diagnosis,
    EMarker,
    LinkSpeed,
    Suggestion,
    Verdict,
)

CABLE_NAME = "(識別不能)"

#: A1 の提案文。直挿しでも P の過大評価で A1 になりうるため断定しない（SPEC.md 4.2 既知の限界）
A1_ACTION = (
    "ケーブルを USB 3.x 対応品に交換してください。"
    "ケーブルを使わず直挿ししている場合は、そのポートの SuperSpeed 配線に問題がある"
    "可能性があります（フロントパネル配線の未接続など）。別のポートで試してください。"
)
#: A5 / B1 の提案文。測れないものを推測で埋めず、切り分け手順を渡す
SPLIT_TEST_ACTION_CABLE_OR_DEVICE = (
    "既知の高速ケーブルに差し替えて再測定してください。"
    "速度が上がればケーブル、変わらなければデバイス側が原因です。"
)
SPLIT_TEST_ACTION_CABLE_OR_PORT = (
    "既知の高速ケーブルに差し替えて再測定してください。"
    "速度が上がればケーブル、変わらなければポート側が原因です。"
)


def estimate_emarker(connector: ConnectorType, link_speed: LinkSpeed | None) -> EMarker:
    """eMarker 搭載の有無を推定する（SPEC.md 5.4）。

    直接読む手段が無いため必ず推定であり、断定しない。
    要実機検証: Type-C ポートを持つ機体で確認していない（開発機は全ポート Type-A）。
    """
    if connector == "type_a":
        # A-to-C / A-to-A ケーブルに eMarker は搭載されない
        return EMarker("absent", "likely", "ポートが Type-A のため")
    if connector == "type_c":
        if link_speed is not None and link_speed >= LinkSpeed.SUPER_SPEED:
            return EMarker(
                "likely_present",
                "likely",
                "Type-C ポートで 5Gbps 以上でリンクしているため、"
                "USB Type-C 仕様上 eMarker 搭載が必須のケーブルに該当",
            )
        # USB 2.0 のみの C-to-C は eMarker が任意。変換ケーブルの可能性もある
        return EMarker(
            "unknown",
            "unknown",
            "Type-C ポートだが 480Mbps 以下でリンクしており、"
            "USB 2.0 のみの C-to-C か変換ケーブルかを区別できないため",
        )
    return EMarker("unknown", "unknown", "ポートのコネクタ形状が不明なため")


def diagnose(
    link_speed: LinkSpeed | None,
    port: Capability,
    device: Capability,
    *,
    port_name: str = "ポート",
    device_name: str = "デバイス",
    hubs: list[ChainElement] | None = None,
    detected: bool = True,
    connector_type: ConnectorType = "unknown",
) -> Diagnosis:
    """L / P / D から判定を行う（SPEC.md 5章）。

    port / device が UNKNOWN の場合も推測で埋めず、判定不能として返す。
    hubs には経路上の外部ハブを上流から順に渡す（SPEC.md 5.3）。
    """
    result = _diagnose_core(
        link_speed,
        port,
        device,
        port_name=port_name,
        device_name=device_name,
        hubs=hubs,
        detected=detected,
    )
    result.connector_type = connector_type
    result.emarker = estimate_emarker(connector_type, link_speed)
    return result


def _diagnose_core(
    link_speed: LinkSpeed | None,
    port: Capability,
    device: Capability,
    *,
    port_name: str,
    device_name: str,
    hubs: list[ChainElement] | None,
    detected: bool,
) -> Diagnosis:
    hubs = hubs or []
    if not detected:
        return Diagnosis(
            link_speed=None,
            chain=[],
            achievable_max=Capability.unknown(),
            verdict="NOT_DETECTED",
            rule=None,
        )
    if link_speed is None:
        return _undetermined(link_speed, port, device, hubs, port_name, device_name, rule=None)

    # 5.3: 外部ハブがあれば上流側の実効能力は min(P, H)
    upstream, upstream_element = _upstream_capability(port, hubs, port_name)

    # D が AT_LEAST で、その下限値が L 以下なら D は天井として使えない
    # （例: Low-Speed で動作中の機器。本当は High-Speed 対応かもしれない）。
    # この場合 D 不明として扱い、切り分け手順を渡す（A3 / A4 と断定しない）。
    device_is_ceiling = device.is_known and (
        device.confidence is Confidence.EXACT
        or (device.lower_bound is not None and link_speed < device.lower_bound)
    )
    if device_is_ceiling and upstream.is_known:
        return _diagnose_known_device(
            link_speed, port, device, upstream, upstream_element, hubs, port_name, device_name
        )
    if upstream.is_known:
        return _diagnose_unknown_device(
            link_speed, port, device, upstream, upstream_element, hubs, port_name, device_name
        )
    # B3: P も不明 → 判定不能
    return _undetermined(link_speed, port, device, hubs, port_name, device_name, rule="B3")


def _upstream_capability(
    port: Capability, hubs: list[ChainElement], port_name: str
) -> tuple[Capability, str]:
    """上流側（ポートと外部ハブ）の実効能力 min(P, H) と、律速している要素名を返す。"""
    upstream = port
    name = port_name
    for hub in hubs:
        if not hub.capability.is_known:
            return Capability.unknown(), hub.name
        if not upstream.is_known or _below(hub.capability, upstream):
            upstream, name = hub.capability, hub.name
    return upstream, name


def _below(a: Capability, b: Capability) -> bool:
    """a の下限値が b の下限値より小さいか。"""
    if a.lower_bound is None or b.lower_bound is None:
        return False
    return a.lower_bound < b.lower_bound


def _diagnose_known_device(
    link: LinkSpeed,
    port: Capability,
    device: Capability,
    upstream: Capability,
    upstream_name: str,
    hubs: list[ChainElement],
    port_name: str,
    device_name: str,
) -> Diagnosis:
    """5.1: D が判明している場合（主に Windows）。"""
    p = upstream.lower_bound
    d = device.lower_bound
    assert p is not None and d is not None  # is_known 済み
    ceiling = min(p, d)
    is_hub_bottleneck = upstream_name != port_name

    if link < ceiling:
        # A1: ケーブルが律速（C = L と確定できる。ハブが律速の場合は L = H = p となり
        # この分岐に入らないため、ここに来るのはケーブル（または 4.2 の既知の限界）のみ）
        return _build(
            link, port, device, hubs, port_name, device_name,
            rule="A1",
            verdict="IMPROVABLE",
            cable=Capability.exact(link),
            bottleneck_kind="cable",
            achievable=capability_min(upstream, device),
            suggestions=[
                Suggestion(
                    target="cable",
                    action=A1_ACTION,
                    expected=capability_min(upstream, device),
                    certainty="confirmed",
                )
            ],
        )  # fmt: skip

    # ここから link == ceiling（link > ceiling は物理的に起こらないが、起きても同じ扱い）
    if link == d and link == p:
        # A4: L = P = D。現構成の最高速を達成済み
        return _build(
            link, port, device, hubs, port_name, device_name,
            rule="A4", verdict="OPTIMAL",
            cable=Capability.at_least(link),
            bottleneck_kind=None,
            achievable=capability_min(upstream, device),
            suggestions=[],
        )  # fmt: skip
    if link == d:
        # A3: L = D <= P。デバイスが天井
        return _build(
            link, port, device, hubs, port_name, device_name,
            rule="A3", verdict="OPTIMAL",
            cable=Capability.at_least(link),
            bottleneck_kind="device",
            achievable=capability_min(upstream, device),
            suggestions=[],
        )  # fmt: skip

    # L = P < D
    if upstream.confidence is Confidence.AT_LEAST:
        # A5: P の上限が不明なため、ポートとケーブルのどちらが律速か区別できない
        return _build(
            link, port, device, hubs, port_name, device_name,
            rule="A5", verdict="UNDETERMINED",
            cable=Capability.at_least(link),
            bottleneck_kind=None,
            achievable=Capability.unknown(),
            suggestions=[
                Suggestion(
                    target="cable",
                    action=SPLIT_TEST_ACTION_CABLE_OR_PORT,
                    expected=Capability.unknown(),
                    certainty="unknown",
                )
            ],
        )  # fmt: skip
    # A2: ポートが律速。ケーブルの上限は不明なので断定しない
    return _build(
        link, port, device, hubs, port_name, device_name,
        rule="A2", verdict="IMPROVABLE",
        cable=Capability.at_least(link),
        bottleneck_kind="hub" if is_hub_bottleneck else "port",
        bottleneck_name=upstream_name,
        achievable=capability_min(upstream, device),
        suggestions=[
            Suggestion(
                target="hub" if is_hub_bottleneck else "port",
                action=(
                    "デバイスを PC に直結してください（経路上のハブが律速しています）。"
                    if is_hub_bottleneck
                    else "より高速なポート / PC に変更すれば現在より速くなります。"
                    "ただし上限はケーブル次第で不明です（最大でデバイス能力まで）。"
                ),
                expected=Capability.at_least(link),
                certainty="likely",
            )
        ],
    )  # fmt: skip


def _diagnose_unknown_device(
    link: LinkSpeed,
    port: Capability,
    device: Capability,
    upstream: Capability,
    upstream_name: str,
    hubs: list[ChainElement],
    port_name: str,
    device_name: str,
) -> Diagnosis:
    """5.2: D が不明な場合（主に macOS）。"""
    p = upstream.lower_bound
    assert p is not None
    if link < p:
        # B1: ケーブルまたはデバイスが律速（切り分け不能）
        return _build(
            link, port, device, hubs, port_name, device_name,
            rule="B1", verdict="IMPROVABLE",
            cable=Capability.unknown(),
            bottleneck_kind=None,
            achievable=Capability.unknown(),
            suggestions=[
                Suggestion(
                    target="cable",
                    action=SPLIT_TEST_ACTION_CABLE_OR_DEVICE,
                    expected=Capability.unknown(),
                    certainty="unknown",
                )
            ],
        )  # fmt: skip
    # B2: L = P。ポートが律速の候補
    return _build(
        link, port, device, hubs, port_name, device_name,
        rule="B2", verdict="IMPROVABLE",
        cable=Capability.at_least(link),
        bottleneck_kind="hub" if upstream_name != port_name else "port",
        bottleneck_name=upstream_name,
        achievable=Capability.unknown(),
        suggestions=[
            Suggestion(
                target="port",
                action="より高速なポートで再測定してください。",
                expected=Capability.unknown(),
                certainty="unknown",
            )
        ],
    )  # fmt: skip


def _undetermined(
    link: LinkSpeed | None,
    port: Capability,
    device: Capability,
    hubs: list[ChainElement],
    port_name: str,
    device_name: str,
    *,
    rule: str | None,
) -> Diagnosis:
    """B3 相当。現在値のみ表示する。"""
    diagnosis = _build(
        link, port, device, hubs, port_name, device_name,
        rule=rule, verdict="UNDETERMINED",
        cable=Capability.unknown(),
        bottleneck_kind=None,
        achievable=Capability.unknown(),
        suggestions=[],
    )  # fmt: skip
    return diagnosis


def capability_min(a: Capability, b: Capability) -> Capability:
    """min(a, b)。片方が不明なら不明。

    EXACT な側が最小値と一致するなら結果も EXACT。
    例: P ≥ 5Gbps かつ D = 5Gbps（EXACT）なら min は 5Gbps で確定する
    （P は 5Gbps 以上なので、天井は D の 5Gbps に決まる）。
    それ以外は AT_LEAST（例: P ≥ 5Gbps かつ D = 10Gbps なら min は 5Gbps 以上）。
    """
    if not a.is_known or not b.is_known or a.speed is None or b.speed is None:
        return Capability.unknown()
    speed = LinkSpeed(min(a.speed, b.speed))
    for cap in (a, b):
        if cap.confidence is Confidence.EXACT and cap.speed == speed:
            return Capability.exact(speed)
    return Capability.at_least(speed)


def _build(
    link: LinkSpeed | None,
    port: Capability,
    device: Capability,
    hubs: list[ChainElement],
    port_name: str,
    device_name: str,
    *,
    rule: str | None,
    verdict: Verdict,
    cable: Capability,
    bottleneck_kind: str | None,
    achievable: Capability,
    suggestions: list[Suggestion],
    bottleneck_name: str | None = None,
) -> Diagnosis:
    chain: list[ChainElement] = []
    for hub in hubs:
        chain.append(
            ChainElement(
                kind="hub",
                name=hub.name,
                capability=hub.capability,
                is_bottleneck=bottleneck_kind == "hub" and hub.name == bottleneck_name,
            )
        )
    chain.append(ChainElement("port", port_name, port, is_bottleneck=bottleneck_kind == "port"))
    chain.append(ChainElement("cable", CABLE_NAME, cable, is_bottleneck=bottleneck_kind == "cable"))
    chain.append(
        ChainElement("device", device_name, device, is_bottleneck=bottleneck_kind == "device")
    )
    return Diagnosis(
        link_speed=link,
        chain=chain,
        achievable_max=achievable,
        verdict=verdict,
        suggestions=suggestions,
        rule=rule,
        device_name=device_name,
    )
