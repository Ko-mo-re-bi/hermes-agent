"""Limited stdlib smoke; no Hermes/profile/auth modules, no real Slack."""
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('coordination_home_isolated', ROOT / 'plugins/platforms/slack/coordination_home.py')
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)

SECRET = 'PRIVATE_SENTINEL_abc123'


def fixture(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE tasks(id TEXT, title TEXT, status TEXT, assignee TEXT, last_failure_error TEXT, created_at INTEGER);
    CREATE TABLE task_events(task_id TEXT, created_at INTEGER);
    CREATE TABLE task_attachments(id INTEGER, task_id TEXT, filename TEXT);
    CREATE TABLE kanban_notify_subs(task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT);''')
    db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)', ('t_0123abcd', SECRET, SECRET, SECRET, SECRET, 1))
    db.execute('INSERT INTO task_events VALUES(?,?)', ('t_0123abcd', 1000))
    db.execute('INSERT INTO task_attachments VALUES(?,?,?)', (1, 't_0123abcd', SECRET))
    db.execute('INSERT INTO kanban_notify_subs VALUES(?,?,?,?)', ('t_0123abcd', 'slack', 'C1', '123.456'))
    db.execute('INSERT INTO kanban_notify_subs VALUES(?,?,?,?)', ('t_0123abcd', 'slack', 'CSECRET', '123.456'))
    db.commit()
    return db


def adapter(path):
    client = SimpleNamespace(views_publish=AsyncMock())
    grants = [{'team_id': 'T1', 'user_id': u, 'board': 'public', 'channels': ['C1']} for u in ('U1', 'U2')]
    obj = SimpleNamespace(config=SimpleNamespace(extra={'coordination_home': {
        'enabled': True, 'boards': {'public': str(path)}, 'grants': grants}}),
        _team_clients={'T1': client}, _event_team_id=lambda e, b: e.get('team_id'))
    return obj, client


async def races(path):
    # This sandbox does not reliably wake select via the loop self-pipe.
    # A timer lets thread callbacks drain without blocking on that mechanism.
    running_loop = asyncio.get_running_loop()
    def tick():
        running_loop.call_later(0.01, tick)
    tick()
    obj, client = adapter(path)
    home = h.Home(obj)
    event = {'team_id': 'T1', 'user': 'U1'}
    lock = home.locks[('T1', 'U1')] = asyncio.Lock()
    await lock.acquire()
    waiting = asyncio.create_task(home.opened(event))
    # An event-loop barrier ensures the handler reached its lock wait.
    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    obj.config.extra['coordination_home']['enabled'] = False
    lock.release()
    await waiting
    assert client.views_publish.await_count == 0
    print('PASS grant revoked during lock wait')

    obj, client = adapter(path)
    home = h.Home(obj)
    original = h._read
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls = []
    def delayed(settings, page, team):
        calls.append(page)
        if page == 0:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        return h.Snapshot(True, (), 100), False
    h._read = delayed
    try:
        first = asyncio.create_task(home.opened(event))
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(home.opened({'team_id': 'T1', 'user': 'U2'}, page=1), 5)
        assert client.views_publish.await_count == 1
        obj.config.extra['coordination_home']['grants'] = []
        release.set()
        await first
        assert client.views_publish.await_count == 1
        print('PASS grant revoked during worker read; other viewer progresses')
    finally:
        release.set()
        h._read = original

    obj, client = adapter(path)
    home = h.Home(obj)
    entered = asyncio.Event()
    release = threading.Event()
    h._read = delayed
    try:
        first = asyncio.create_task(home.opened(event))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(home.opened(event, page=1))
        await loop.run_in_executor(None, lambda: None)
        release.set()
        await asyncio.gather(first, second)
        views = [c.kwargs['view'] for c in client.views_publish.call_args_list]
        assert '1ページ' in str(views[0]) and '2ページ' in str(views[-1])
        assert not home.locks
        print('PASS same viewer ordered; bounded lock state released')
    finally:
        release.set()
        h._read = original

    class Limited(Exception):
        response = SimpleNamespace(status_code=429, headers={'Retry-After': '60'})
    client.views_publish.reset_mock()
    client.views_publish.side_effect = Limited(SECRET)
    await home.opened(event)
    await home.opened({'team_id': 'T1', 'user': 'U2'})
    assert client.views_publish.await_count == 1
    print('PASS team-wide 429 cooldown')


def test_revision_contracts(tmp_path):
    logs = []
    class Capture(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())
    h.logger.addHandler(Capture())
    with tempfile.TemporaryDirectory(prefix='revision-smoke-', dir=tmp_path) as directory:
        root = Path(directory)
        path = root / 'board.db'
        writer = fixture(path)
        try:
            before = path.read_bytes()
            schema = writer.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
            data = h.snapshot(path, team='T1', channels=['C1'])
            assert data.connected and data.total == 1
            assert data.rows[0]['event_at'] == 1000
            assert data.rows[0]['artifacts'] == [1]
            assert data.rows[0]['threads'] == ['https://app.slack.com/client/T1/C1/thread/C1-123.456']
            payload = json.dumps(h.render(data, SECRET, 0), ensure_ascii=False)
            assert SECRET not in payload and 'CSECRET' not in payload
            assert '最終イベント: 1970年01月01日 09:16:40 JST' in payload
            assert 'アクセスリンク未接続' in payload and '"url"' in payload
            from contextlib import closing
            with closing(h._open(path)) as reader:
                assert reader.execute('PRAGMA query_only').fetchone()[0] == 1
                try:
                    reader.execute("UPDATE tasks SET status='done'")
                except sqlite3.OperationalError:
                    pass
                else:
                    raise AssertionError('UPDATE accepted')
            assert path.read_bytes() == before
            assert writer.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == schema
            print('PASS live committed WAL read; UPDATE rejected; schema/main bytes unchanged')
            print('PASS secret suppression; attachment IDs; JST event; scoped clickable thread')
            async def checked():
                try:
                    await races(path)
                except BaseException:
                    import traceback
                    traceback.print_exc()
                    for task in asyncio.all_tasks():
                        if task is not asyncio.current_task():
                            print(task, flush=True)
                    raise
            asyncio.run(checked())
        finally:
            writer.close()
        assert not h.snapshot(path).connected  # WAL header, absent sidecars: fail closed.
        print('PASS missing WAL sidecars refused without connecting')
        missing = root / 'missing' / 'db'
        assert not h.snapshot(missing).connected and not missing.parent.exists()
        old = root / 'old.db'
        with sqlite3.connect(old) as db:
            db.execute('CREATE TABLE cards(hd TEXT)')
        before = old.read_bytes(), old.stat().st_mtime_ns
        assert not h.approval_connected(old)
        assert (old.read_bytes(), old.stat().st_mtime_ns) == before
        assert SECRET not in str(logs)
        print('PASS missing path not created; legacy approval bytes/mtime unchanged; logs suppress secrets')
    print('LIMITED SMOKE ONLY: formal runner NOT RUN / BLOCKED')
