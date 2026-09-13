"""后台任务调度：一条经 context.register_task 登记的常驻 asyncio 循环。

不引入 APScheduler，两个宿主（AstrBot / Stella）行为完全一致，
terminate 时通过 stop() 在 5 秒内退出。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import random
import re
from typing import Awaitable, Callable

logger = logging.getLogger("gunplalab.scheduler")

TICK_SECONDS = 30.0
RETRY_DELAY_SECONDS = 600.0
# 启动后尽快做首轮同步（ETag 校验通常极轻），缩短插件就绪前的盲区
STARTUP_DELAY_SECONDS = 3.0


class DailyLoop:
    """每日定点任务循环（快照同步 / 今日胶情推送共用）。

    用法：
        loop = DailyLoop(job, at="04:00")
        handle = context.register_task(loop.run(), "...")
        ...
        loop.stop(); await loop.wait_stopped()
    """

    def __init__(
        self,
        job: Callable[[], Awaitable[object]],
        at: str = "04:00",
        *,
        startup_delay: float = STARTUP_DELAY_SECONDS,
        jitter_minutes: float = 30.0,
    ):
        self._job = job
        hour, minute = _parse_hhmm(at, default=(4, 0))
        self._hour = max(0, min(23, hour))
        self._minute = max(0, min(59, minute))
        self._jitter = max(0.0, jitter_minutes) * 60.0
        self._startup_delay = startup_delay
        self._stop_event = asyncio.Event()
        self._done_event = asyncio.Event()

    # -- 生命周期 -------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    async def wait_stopped(self, timeout: float = 4.0) -> None:
        try:
            await asyncio.wait_for(self._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    # -- 调度 -----------------------------------------------------------

    def next_run_time(self, now: _dt.datetime | None = None) -> _dt.datetime:
        """同步时刻：at + 抖动；已过则排到明天。"""
        now = now or _dt.datetime.now()
        jitter = random.uniform(0, self._jitter)
        candidate = now.replace(
            hour=self._hour, minute=self._minute, second=0, microsecond=0
        ) + _dt.timedelta(seconds=jitter)
        if candidate <= now:
            candidate += _dt.timedelta(days=1)
        return candidate

    async def run(self) -> None:
        next_at = _dt.datetime.now() + _dt.timedelta(seconds=self._startup_delay)
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=max(1.0, (next_at - _dt.datetime.now()).total_seconds()),
                    )
                    break  # stop 被置位
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._job()
                    next_at = self.next_run_time()
                except Exception as exc:
                    logger.warning("定时任务失败，%d 秒后重试：%s", RETRY_DELAY_SECONDS, exc)
                    next_at = _dt.datetime.now() + _dt.timedelta(seconds=RETRY_DELAY_SECONDS)
        finally:
            self._done_event.set()


class IntervalLoop:
    """固定间隔轮询循环（盯盘订阅用）：每 interval_minutes ± 抖动执行一次。"""

    def __init__(
        self,
        job: Callable[[], Awaitable[object]],
        interval_minutes: float = 60.0,
        *,
        startup_delay: float = 90.0,
        jitter_seconds: float = 300.0,
    ):
        self._job = job
        self._interval = max(5.0, float(interval_minutes) * 60.0)  # 下限 5 秒，防误配置
        self._startup_delay = startup_delay
        self._jitter = max(0.0, jitter_seconds)
        self._stop_event = asyncio.Event()
        self._done_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def wait_stopped(self, timeout: float = 4.0) -> None:
        try:
            await asyncio.wait_for(self._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        next_at = _dt.datetime.now() + _dt.timedelta(seconds=self._startup_delay)
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=max(1.0, (next_at - _dt.datetime.now()).total_seconds()),
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._job()
                except Exception as exc:
                    logger.warning("轮询任务失败：%s", exc)
                next_at = _dt.datetime.now() + _dt.timedelta(
                    seconds=self._interval + random.uniform(0, self._jitter)
                )
        finally:
            self._done_event.set()


def _parse_hhmm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text or "").strip())
    if not m:
        return default
    return int(m.group(1)), int(m.group(2))
