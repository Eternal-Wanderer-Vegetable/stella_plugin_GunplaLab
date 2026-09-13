"""调度循环测试：DailyLoop（含分钟级）与 IntervalLoop。"""

import asyncio
import datetime as _dt

from core.scheduler import DailyLoop, IntervalLoop, _parse_hhmm


class TestNextRunTime:
    def test_future_today(self):
        loop = DailyLoop(lambda: asyncio.sleep(0), at="04:00")
        now = _dt.datetime(2026, 9, 13, 3, 0, 0)
        nxt = loop.next_run_time(now)
        assert nxt.date() == now.date()
        assert nxt.hour == 4
        assert 0 <= nxt.minute <= 30

    def test_past_goes_tomorrow(self):
        loop = DailyLoop(lambda: asyncio.sleep(0), at="04:00")
        now = _dt.datetime(2026, 9, 13, 23, 30, 0)
        nxt = loop.next_run_time(now)
        assert nxt.date() == _dt.date(2026, 9, 14)
        assert nxt.hour == 4

    def test_minute_precision(self):
        loop = DailyLoop(lambda: asyncio.sleep(0), at="09:30", jitter_minutes=0)
        now = _dt.datetime(2026, 9, 13, 10, 0, 0)
        nxt = loop.next_run_time(now)
        assert (nxt.hour, nxt.minute) == (9, 30)
        assert nxt.date() == _dt.date(2026, 9, 14)

    def test_parse_hhmm(self):
        assert _parse_hhmm("09:05", (4, 0)) == (9, 5)
        assert _parse_hhmm("bad", (4, 0)) == (4, 0)


class TestDailyLoopLifecycle:
    def test_job_runs_and_stops(self):
        counter = {"n": 0}

        async def job():
            counter["n"] += 1

        loop = DailyLoop(job, at="04:00", startup_delay=0.05)

        async def scenario():
            task = asyncio.get_running_loop().create_task(loop.run())
            await asyncio.sleep(1.6)  # 最小等待粒度 1s，之后作业应已执行
            assert counter["n"] >= 1
            loop.stop()
            await asyncio.wait_for(task, timeout=4)

        asyncio.run(scenario())


class TestIntervalLoop:
    def test_runs_twice(self):
        counter = {"n": 0}

        async def job():
            counter["n"] += 1

        loop = IntervalLoop(job, interval_minutes=1 / 12.0, startup_delay=0.05, jitter_seconds=0)

        async def scenario():
            task = asyncio.get_running_loop().create_task(loop.run())
            await asyncio.sleep(7.5)  # 最小粒度 1s：t≈1s 首跑，之后每 5s 一跑
            loop.stop()
            await asyncio.wait_for(task, timeout=4)

        asyncio.run(scenario())
        assert counter["n"] >= 2
