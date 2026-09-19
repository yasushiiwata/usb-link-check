"""抽象基底クラス（OS コマンド実行 / IOCTL 呼び出しを抽象化する）。

SPEC.md 9.2: 実行層（`load()`）だけを OS 依存にし、テストではフィクスチャを返す
スタブに差し替えられるようにする。解析と判定は生データの辞書に対して行う。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import Diagnosis, LinkSpeed

_DEVICE_SPEC_RE = re.compile(r"^(?P<vid>[0-9A-Fa-f]{1,4}):(?P<pid>[0-9A-Fa-f]{1,4})$")


@dataclass
class DeviceSummary:
    """検出した USB デバイス 1 台分の要約（表示・選択用）。"""

    name: str
    vid: int | None
    pid: int | None
    link_speed: LinkSpeed | None
    location: str
    is_mass_storage: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def vid_pid(self) -> str:
        if self.vid is None or self.pid is None:
            return "????:????"
        return f"{self.vid:04X}:{self.pid:04X}"

    def matches(self, spec: str) -> bool:
        """`--device` の指定（VID:PID またはデバイス名の部分一致）に一致するか。"""
        m = _DEVICE_SPEC_RE.match(spec.strip())
        if m:
            return (self.vid, self.pid) == (int(m.group("vid"), 16), int(m.group("pid"), 16))
        return spec.strip().lower() in self.name.lower()


class UsbPlatform(ABC):
    """OS 別の情報取得層。"""

    #: 表示用の OS 名（JSON 出力の "os"）
    os_name: str = "unknown"

    @abstractmethod
    def load(self) -> Any:
        """生データを取得する（実行層。テストではスタブに差し替える）。"""

    @abstractmethod
    def devices(self) -> list[DeviceSummary]:
        """検出した USB デバイスを列挙する。"""

    @abstractmethod
    def diagnose(self, device: DeviceSummary) -> Diagnosis:
        """1 台のデバイスについて判定を行う。"""

    def select(self, spec: str | None) -> list[DeviceSummary]:
        """`--device` の指定から対象候補を絞り込む（SPEC.md 7章）。

        未指定時は USB マスストレージクラスのデバイスを候補とする。
        候補が 1 つに定まらない場合でも**勝手に選ばない**（誤診断の温床になるため）。
        """
        devices = self.devices()
        if spec:
            return [d for d in devices if d.matches(spec)]
        return [d for d in devices if d.is_mass_storage]
