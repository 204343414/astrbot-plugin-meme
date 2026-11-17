"""投递：一条链上排好几种发法，前面挂了就自动换后面的。

梗图发不出去的场面很难看（群里只剩一句「发送失败」），所以这里的规矩是
**永远有兜底**：图 → 图+字 → 纯字，一级一级退，退到底也要说句人话。
每个失败都只在日志里留一行，不往群里刷错误信息。
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Sender = Callable[[], Awaitable[object]]


@dataclass
class Attempt:
    """一种发法。``name`` 会出现在日志里，起个能一眼看懂的名字。"""

    name: str
    func: Sender


@dataclass
class DeliveryResult:
    used: str = ""
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.used)

    def summary(self) -> str:
        if self.ok:
            return self.used
        return "、".join(f"{name}:{why}" for name, why in self.errors) or "没有可用的发送方式"


async def deliver(attempts: list[Attempt]) -> DeliveryResult:
    """按顺序试，第一个不抛异常的就是成功。"""
    result = DeliveryResult()
    for attempt in attempts:
        if attempt.func is None:
            continue
        try:
            outcome = attempt.func()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 - 兜底就是要接住一切
            logger.warning("[Meme] 发送失败（%s）：%s", attempt.name, exc)
            result.errors.append((attempt.name, f"{type(exc).__name__}: {exc}"))
            continue
        result.used = attempt.name
        return result
    logger.warning("[Meme] 所有发送方式都失败了：%s", result.summary())
    return result
