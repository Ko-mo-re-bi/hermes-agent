"""Memory-only worker ownership contracts; no gateway/profile imports."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock


def load_home():
    spec = importlib.util.spec_from_file_location('home_ownership_isolated', Path(__file__).resolve().parents[2] / 'plugins/platforms/slack/coordination_home.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def ownership_contracts(h):
    loop = asyncio.get_running_loop()
    client = SimpleNamespace(views_publish=AsyncMock())
    grants = [{'team_id': 'T1', 'user_id': f'U{i}', 'board': 'b'} for i in range(10)]
    adapter = SimpleNamespace(config=SimpleNamespace(extra={'coordination_home': {
        'enabled': True, 'boards': {'b': '/unused'}, 'grants': grants}}),
        _team_clients={'T1': client}, _event_team_id=lambda e, b: 'T1')
    home = h.Home(adapter)
    event = {'user': 'U0'}
    release = threading.Event()
    entered = asyncio.Event()
    count = 0
    peak = 0
    mutex = threading.Lock()
    def read(*args):
        nonlocal count, peak
        with mutex:
            count += 1
            peak = max(peak, count)
            loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5)
            return h.Snapshot(True), False
        finally:
            with mutex:
                count -= 1
    original = h._read
    h._read = read
    async def barrier():
        future = loop.create_future()
        loop.call_soon(future.set_result, None)
        await future
    async def cancelled(task):
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
    try:
        owner = asyncio.create_task(home.opened(event))
        await entered.wait()
        owner.cancel()
        await barrier()
        waiter = asyncio.create_task(home.opened(event))
        await barrier()
        await home.opened(event)  # third request coalesces
        owner.cancel()  # repeated cancellation must still drain
        await barrier()
        assert count == peak == 1 and not owner.done()
        waiter.cancel()
        await cancelled(waiter)
        assert home.locks[('T1', 'U0')].locked()
        release.set()
        await cancelled(owner)
        assert not home.locks and not home.pending and count == 0
        assert client.views_publish.await_count == 0
        await home.opened(event)
        assert client.views_publish.await_count == 1
        print('PASS active/repeated cancel; next request; pending bound; waiter cancel then owner release')

        # Release an owner and cancel its waiter in the same loop turn.
        lock = home.locks[('T1', 'U0')] = asyncio.Lock()
        await lock.acquire()
        waiter = asyncio.create_task(home.opened(event))
        await barrier()
        lock.release()
        waiter.cancel()
        await cancelled(waiter)
        assert not home.locks and not home.pending
        print('PASS owner release immediately followed by waiter cancel')

        release.clear()
        entered.clear()
        peak = 0
        tasks = [asyncio.create_task(home.opened({'user': f'U{i}'})) for i in range(10)]
        await entered.wait()
        await barrier()
        await barrier()
        assert count <= h.READ_WORKERS
        for task in tasks:
            task.cancel()
        await barrier()
        assert count <= h.READ_WORKERS and any(not t.done() for t in tasks)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert peak <= h.READ_WORKERS and count == 0 and not home.locks and not home.pending
        print('PASS global finite read bound and cancellation cleanup')

        release.clear()
        entered.clear()
        task = asyncio.create_task(home.opened(event))
        await entered.wait()
        # wait_for cancels its input on timeout, then waits for worker cleanup.
        timed = asyncio.create_task(asyncio.wait_for(task, timeout=0))
        await barrier()
        assert not task.done()
        release.set()
        try:
            await timed
        except TimeoutError:
            pass
        else:
            raise AssertionError('timeout expected')
        assert not home.locks and not home.pending and count == 0
        print('PASS timeout cancellation drains worker')
        ack = AsyncMock()
        await home.thread_action(ack, {'user': {'id': 'U0'}}, {'url': 'https://invalid.example'})
        ack.assert_awaited_once_with()
    finally:
        release.set()
        h._read = original


def test_worker_ownership():
    asyncio.run(ownership_contracts(load_home()))
