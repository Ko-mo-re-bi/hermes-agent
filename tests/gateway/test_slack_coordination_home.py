"""Observe-only Home contracts; all transport calls are fake."""
import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.platforms.slack.coordination_home import Home, approval_connected, render, snapshot


@pytest.fixture
def board(tmp_path):
    path = tmp_path / 'board.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, last_failure_error TEXT)')
        db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?)', [
            (f't_{i:08x}', '日本語' * 1100, 'running', 'worker', 'error') for i in range(100)])
    return path


def adapter(path):
    client = SimpleNamespace(views_publish=AsyncMock())
    obj = SimpleNamespace(config=SimpleNamespace(extra={'coordination_home': {
        'enabled': True, 'boards': {'public': str(path)},
        'grants': [{'team_id': 'T1', 'user_id': 'U1', 'board': 'public'}]}}),
        _team_clients={'T1': client}, _get_client=Mock(return_value=client),
        _event_team_id=lambda e, b: e.get('team_id') or (b or {}).get('team', {}).get('id', ''))
    return obj, client


def test_absent_and_legacy_do_not_create_or_migrate(tmp_path, monkeypatch):
    from plugins.platforms.slack.business_approval import Ledger
    monkeypatch.setattr(Ledger, 'connect', Mock(side_effect=AssertionError('migration')))
    missing = tmp_path / 'missing' / 'new.db'
    assert not snapshot(missing).connected
    assert not approval_connected(missing)
    assert not missing.parent.exists()
    old = tmp_path / 'old.db'
    with sqlite3.connect(old) as db:
        db.execute('CREATE TABLE cards(hd TEXT)')
    before = (old.read_bytes(), old.stat().st_mtime_ns, set(tmp_path.iterdir()))
    assert not snapshot(old).connected
    assert not approval_connected(old)
    assert (old.read_bytes(), old.stat().st_mtime_ns, set(tmp_path.iterdir())) == before
    Ledger.connect.assert_not_called()


def test_empty_is_distinct_and_long_japanese_is_paginated(board):
    seen = []
    before = board.read_bytes(), board.stat().st_mtime_ns
    for page in range(5):
        data = snapshot(board, page)
        seen.extend(r['id'] for r in data.rows)
        view = render(data, 'public', page, now=1000)
        assert len(view['blocks']) <= 100
        for block in view['blocks']:
            if 'text' in block:
                assert len(block['text']['text']) <= 3000
                assert block['text']['type'] == 'plain_text'
        assert '時刻: 不明' in str(view)
        assert 'オンライン状態は取得していません' in str(view)
        assert '更新' not in str([b for b in view['blocks'] if b['type'] == 'actions'])
    assert len(set(seen)) == 100
    assert (board.read_bytes(), board.stat().st_mtime_ns) == before
    with sqlite3.connect(board) as db:
        db.execute('DELETE FROM tasks')
    assert 'タスク0件' in str(render(snapshot(board), 'public', 0))
    assert '未接続' in str(render(snapshot(board.parent / 'absent'), 'public', 0))


@pytest.mark.parametrize('event,body', [
    ({'team_id': 'T2', 'user': 'U1'}, {}),
    ({'team_id': 'T1', 'user': 'U2', 'display_name': 'U1'}, {}),
    ({'team_id': 'T1', 'user': 'U1', 'bot_id': 'B1'}, {}),
    ({'user': 'U1'}, {}),
])
def test_denied_identity_never_reads_or_publishes(board, event, body, monkeypatch):
    obj, client = adapter(board)
    reader = Mock(side_effect=AssertionError('unauthorized read'))
    monkeypatch.setattr('plugins.platforms.slack.coordination_home.snapshot', reader)
    asyncio.run(Home(obj).opened(event, body))
    reader.assert_not_called()
    client.views_publish.assert_not_awaited()


def test_disabled_and_ungranted_board_fail_closed(board, monkeypatch):
    obj, client = adapter(board)
    reader = Mock(side_effect=AssertionError('unauthorized read'))
    monkeypatch.setattr('plugins.platforms.slack.coordination_home.snapshot', reader)
    obj.config.extra['coordination_home']['grants'][0]['board'] = 'secret'
    asyncio.run(Home(obj).opened({'team_id': 'T1', 'user': 'U1'}))
    obj.config.extra = {}
    asyncio.run(Home(obj).opened({'team_id': 'T1', 'user': 'U1'}))
    reader.assert_not_called()
    client.views_publish.assert_not_awaited()


def test_page_action_rechecks_grant_and_429_defers_without_retry(board, monkeypatch):
    obj, client = adapter(board)
    clock = [100.0]
    monkeypatch.setattr('plugins.platforms.slack.coordination_home.time.monotonic', lambda: clock[0])
    class Limited(Exception):
        response = SimpleNamespace(status_code=429, headers={'Retry-After': '30'})
    client.views_publish.side_effect = Limited()
    home = Home(obj)
    before = board.read_bytes()
    async def exercise():
        await home.opened({'team_id': 'T1', 'user': 'U1'})
        await home.opened({'team_id': 'T1', 'user': 'U1'})
        assert client.views_publish.await_count == 1
        clock[0] = 131
        client.views_publish.side_effect = OSError('secret must not be logged')
        await home.opened({'team_id': 'T1', 'user': 'U1'})
        assert client.views_publish.await_count == 2
        client.views_publish.side_effect = None
        ack = AsyncMock()
        await home.action(ack, {'team': {'id': 'T1'}, 'user': {'id': 'U1'}}, {'value': '1'})
        ack.assert_awaited_once()
        assert 't_00000014' in str(client.views_publish.call_args)
        obj.config.extra['coordination_home']['grants'] = []
        await home.action(ack, {'team': {'id': 'T1'}, 'user': {'id': 'U1'}}, {'value': '2'})
        assert client.views_publish.await_count == 3
    asyncio.run(exercise())
    assert board.read_bytes() == before


def test_real_home_branch_preserves_messages_path(board):
    from tests.gateway.test_slack_approval_buttons import _make_adapter
    obj = _make_adapter()
    obj.config.extra = adapter(board)[0].config.extra
    obj._team_clients['T1'].views_publish = AsyncMock()
    obj._seed_dm_session = Mock()
    obj._set_assistant_suggested_prompts = AsyncMock()
    obj._cache_agent_view_context = Mock()
    async def exercise():
        await obj._handle_app_home_opened({'tab': 'home', 'user': 'U1'}, {'team_id': 'T1'})
        obj._team_clients['T1'].views_publish.assert_awaited_once()
        obj._seed_dm_session.assert_not_called()
        await obj._handle_app_home_opened({'tab': 'messages', 'user': 'U1', 'channel': 'D1'}, {'team_id': 'T1'})
        obj._seed_dm_session.assert_called_once()
        obj._set_assistant_suggested_prompts.assert_awaited_once()
    asyncio.run(exercise())


def test_native_api_wal_snapshot_and_readonly_contract(tmp_path):
    from hermes_cli import kanban_db as kb
    from plugins.platforms.slack.coordination_home import _open
    path = tmp_path / 'native.db'
    from hermes_cli.kanban_db_connect import connect
    writer = connect(path)
    try:
        task_id = kb.create_task(writer, title='native fixture', assignee='fixture', workspace_kind='scratch')
        before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir() if p.is_file()}
        data = snapshot(path)
        assert data.connected
        assert any(r["id"] == task_id for r in data.rows)
        assert path.read_bytes() == before[path.name][1]
    finally:
        writer.close()
    # A WAL-header database with no existing sidecars is refused before connect.
    assert not snapshot(path).connected


def test_threads_require_explicit_channel_scope(board):
    with sqlite3.connect(board) as db:
        db.execute('CREATE TABLE kanban_notify_subs(task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT)')
        db.executemany('INSERT INTO kanban_notify_subs VALUES(?,?,?,?)', [
            ('t_00000000', 'slack', 'C1', '123.456'), ('t_00000000', 'slack', 'CSECRET', '987.654')])
    data = snapshot(board, team='T1', channels=['C1'])
    assert data.rows[0]['threads'] == ['https://app.slack.com/client/T1/C1/thread/C1-123.456']
    assert 'CSECRET' not in str(data)
    assert snapshot(board).rows[0].get('threads') is None


def test_thread_url_action_registered_and_ack_only(board):
    from plugins.platforms.slack.adapter import SlackAdapter
    obj = SlackAdapter.__new__(SlackAdapter)
    obj._app = Mock()
    obj.config = SimpleNamespace(extra={})
    obj._register_plugin_action_handlers = Mock()
    obj._wire_plugin_handlers = Mock()
    registered = {}
    obj._app.action.side_effect = lambda name: lambda handler: registered.update({name: handler})
    obj._register_bolt_handlers()
    obj._coordination_home.opened = AsyncMock(side_effect=AssertionError('DB path forbidden'))
    ack = AsyncMock()
    asyncio.run(registered['hermes_observe_thread'](ack, {}, {'url': 'https://invalid.example'}))
    ack.assert_awaited_once_with()
    assert not obj._coordination_home.locks
    obj._coordination_home.opened.assert_not_called()
