import asyncio

from pyleak import no_task_leaks
import pytest

from aizk.utilities.async_utils import (
    gather_with_concurrency,
    map_concurrently,
    run_async,
)


def test_run_async_without_running_loop():
    async def sample() -> int:
        return 7

    assert run_async(sample) == 7


@pytest.mark.asyncio(loop_scope="function")
async def test_run_async_with_running_loop():
    async def sample() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    with pytest.raises(RuntimeError, match="run_async cannot"):
        run_async(sample)


@pytest.mark.asyncio(loop_scope="function")
async def test_gather_with_concurrency_limits_parallelism():
    concurrency_limit = 3
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def task(identifier: int) -> int:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            return identifier
        finally:
            async with lock:
                in_flight -= 1

    tasks = [task(i) for i in range(6)]
    results = await gather_with_concurrency(tasks, concurrency_limit)

    assert results == list(range(6))
    assert peak <= concurrency_limit


@pytest.mark.asyncio(loop_scope="function")
async def test_gather_with_concurrency_return_exceptions():
    async def task(identifier: int) -> int:
        if identifier == 2:
            raise ValueError("boom")
        return identifier

    results = await gather_with_concurrency([task(i) for i in range(4)], 2, return_exceptions=True)

    assert results[0] == 0
    assert isinstance(results[2], ValueError)


@pytest.mark.asyncio(loop_scope="function")
async def test_map_concurrently_preserves_order():
    async def double(value: int) -> int:
        await asyncio.sleep(0.005)
        return value * 2

    results = await map_concurrently(range(5), double, concurrency=2)

    assert results == [0, 2, 4, 6, 8]


@pytest.mark.asyncio(loop_scope="function")
async def test_map_concurrently_no_task_leaks_with_exceptions():
    async def maybe_fail(value: int) -> int:
        await asyncio.sleep(0.005)
        if value == 2:
            raise ValueError("boom")
        return value

    async with no_task_leaks(action="raise"):
        results = await map_concurrently(
            [0, 1, 2, 3],
            maybe_fail,
            concurrency=2,
            return_exceptions=True,
        )

    assert results[0] == 0
    assert isinstance(results[2], ValueError)
