"""Business decisions remain durable, scoped and independent of exec approvals."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.platforms.slack.business_approval import (
    APPROVER, CHANNEL, BusinessApproval, Ledger, card, initialize_database,
    issuer_verifier, main, parse_issuer_document,
)


@pytest.fixture
def ledger(tmp_path):
    tmp_path.chmod(0o700)
    initialize_database(str(tmp_path / 'business.sqlite'))
    store = Ledger(str(tmp_path / 'business.sqlite'), profile='appdev',
                   verifier=lambda registration: True)
    store.register(hd='HD-123', team='T1', channel=CHANNEL, message_ts='100.1',
                   thread_ts='99.1', issued=1000, requester='joushou', now=1000)
    return store


def decide(ledger, **overrides):
    args = dict(team='T1', channel=CHANNEL, message_ts='100.1', user=APPROVER,
                choice='approve', now=1001)
    return ledger.decide(**(args | overrides))


@pytest.mark.parametrize('override', [
    {'team': 'T2'}, {'channel': 'C2'}, {'message_ts': '100.2'}, {'user': 'U2'},
    {'now': 999}, {'hd': 'HD-other'}, {'choice': 'always'},
])
def test_scope_and_expiry(ledger, override):
    assert decide(ledger, **override) is None
    assert ledger.results() == []


def test_atomic_competition_replay_and_restart(ledger):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda choice: decide(ledger, choice=choice), ['approve', 'deny']))
    assert results[0]['decision'] == results[1]['decision']
    restarted = Ledger(ledger.path, profile='appdev', verifier=lambda registration: True)
    assert decide(restarted)['decision'] == results[0]['decision']
    assert len(restarted.results()) == 1
    with pytest.raises(sqlite3.IntegrityError):
        ledger.register(hd='HD-123', team='T1', channel=CHANNEL, message_ts='200.1',
                        thread_ts='99.1', issued=1000, requester='other', now=1000)


def test_real_handlers_share_decision_and_render_without_exec(ledger, monkeypatch):
    import tools.approval as execution
    resolver = Mock(side_effect=AssertionError('business approval touched exec'))
    monkeypatch.setattr(execution, 'resolve_gateway_approval', resolver)
    client = Mock(chat_update=AsyncMock())
    adapter = Mock()
    adapter._event_team_id.return_value = 'T1'
    adapter._get_client.return_value = client
    handler = BusinessApproval(adapter, ledger.path, str(Path(ledger.path).parent / "outbox"), profile='appdev')
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    event = dict(item=dict(type='message', channel=CHANNEL, ts='100.1'),
                 user=APPROVER, reaction='white_check_mark')
    async def exercise():
        assert await handler.reaction(event, {})
        assert await handler.reaction(event, {}, removed=True)
        ack = AsyncMock()
        await handler.action(ack, dict(channel={'id': CHANNEL}, user={'id': APPROVER},
                                      container={'message_ts': '100.1'}),
                             {'action_id': 'business_deny', 'value': 'HD-123'})
        ack.assert_awaited_once()
    asyncio.run(exercise())
    assert ledger.results()[0]['decision'] == 'approve'
    assert ledger.results()[0]['source'] == 'reaction'
    assert 'approve' in client.chat_update.call_args.kwargs['text']
    assert len(list(handler.outbox.glob('*.md'))) == 1
    resolver.assert_not_called()
    assert card('HD-123', 'Review')[1]['elements'][0]['value'] == 'HD-123'


def test_deny_button_survives_display_failure(ledger, monkeypatch):
    adapter = Mock()
    adapter._event_team_id.return_value = 'T1'
    adapter._get_client.return_value.chat_update = AsyncMock(side_effect=RuntimeError('offline'))
    handler = BusinessApproval(adapter, ledger.path, str(Path(ledger.path).parent / 'outbox'), profile='appdev')
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    asyncio.run(handler.action(AsyncMock(), dict(
        channel={'id': CHANNEL}, user={'id': APPROVER}, container={'message_ts': '100.1'}),
        {'action_id': 'business_deny', 'value': 'HD-123'}))
    assert ledger.results()[0]['decision'] == 'deny'
    assert 'deny' in next(handler.outbox.glob('*.md')).read_text()


@pytest.mark.parametrize('issued', [1001, -85400, float('nan'), float('inf')])
def test_invalid_issuance_does_not_register(tmp_path, issued):
    with pytest.raises(ValueError):
        Ledger(str(tmp_path / 'bad.sqlite'), profile='appdev', verifier=lambda row: True).register(
            hd='HD-1', team='T1', channel=CHANNEL, message_ts='100.1', thread_ts='99.1',
            issued=issued, requester='joushou', now=1000)


def test_registration_requires_relay_verification(tmp_path):
    store = Ledger(str(tmp_path / 'business.sqlite'), profile='appdev')
    with pytest.raises(ValueError, match='relay'):
        store.register(hd='HD-123', team='T1', channel=CHANNEL, message_ts='100.1',
                       thread_ts='99.1', issued=1000, requester='joushou', now=1000)
    assert not Path(store.path).exists()


def test_profile_isolation_and_separate_request_identity(ledger):
    other = Ledger(ledger.path, profile='other')
    assert decide(other) is None
    assert other.results() == []
    assert other.pending(now=1001) == []
    request = ledger.pending(now=1001)[0]
    assert request['request_id'].startswith('business-')
    assert request['request_id'] != request['hd']
    assert request['expires'] == 87400
    decide(ledger)
    assert ledger.pending(now=1001) == []
    result = ledger.results()[0]
    assert result['actor'] == APPROVER
    assert result['source'] == 'button'


def test_rejected_relay_receipt_creates_no_database(tmp_path):
    calls = []
    def reject(registration):
        calls.append(registration)
        return False
    store = Ledger(str(tmp_path / 'business.sqlite'), profile='appdev', verifier=reject)
    with pytest.raises(ValueError, match='relay'):
        store.register(hd='HD-123', team='T1', channel=CHANNEL, message_ts='100.1',
                       thread_ts='99.1', issued=1000, requester='joushou', now=1000)
    assert calls[0]['profile'] == 'appdev'
    assert not Path(store.path).exists()


def test_bolt_routes_business_decisions_without_generic_reactions(ledger, monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.slack.adapter import SlackAdapter
    adapter = SlackAdapter(PlatformConfig(enabled=True, extra={
        'business_approval_db': ledger.path,
        'business_approval_outbox': str(Path(ledger.path).parent / 'outbox'),
        'business_approval_profile': 'appdev',
    }))
    actions, events = {}, {}
    app = Mock()
    app.action.side_effect = lambda key: lambda fn: actions.setdefault(key, fn)
    app.event.side_effect = lambda key: lambda fn: events.setdefault(key, fn)
    adapter._app = app
    adapter._handle_slack_reaction = AsyncMock()
    adapter._handle_approval_action = AsyncMock(side_effect=AssertionError('exec card route'))
    adapter._get_client = Mock(return_value=Mock(chat_update=AsyncMock()))
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    adapter._register_bolt_handlers()
    async def exercise():
        await actions['business_deny'](AsyncMock(), {
            'team': {'id': 'T1'}, 'channel': {'id': CHANNEL},
            'container': {'message_ts': '100.1'}, 'user': {'id': APPROVER},
        }, {'action_id': 'business_deny', 'value': 'HD-123'})
        await events['reaction_added']({
            'team': 'T1', 'item': {'type': 'message', 'channel': CHANNEL, 'ts': '100.1'},
            'user': APPROVER, 'reaction': 'white_check_mark',
        }, say=None, body={'team_id': 'T1'})
        await events['reaction_added']({
            'team': 'T1', 'item': {'type': 'message', 'channel': CHANNEL, 'ts': '200.1'},
            'user': APPROVER, 'reaction': 'white_check_mark',
        }, say=None, body={'team_id': 'T1'})
    asyncio.run(exercise())
    assert ledger.results()[0]['decision'] == 'deny'
    assert ledger.results()[0]['source'] == 'button'
    adapter._handle_approval_action.assert_not_awaited()
    adapter._handle_slack_reaction.assert_awaited_once()


@pytest.fixture
def issuer(tmp_path):
    tmp_path.chmod(0o700)
    root = tmp_path / 'issuer'
    root.mkdir(mode=0o700)
    record = dict(hd='HD-123', team='T1', profile='appdev', channel=CHANNEL,
                  message_ts='100.1', thread_ts='99.1', issued=1000.0,
                  expires=87400.0, requester='joushou', approver=APPROVER,
                  automatic_execution='none')
    path = root / 'HD-123.md'
    path.write_text(''.join(f'{key}: {value}\n' for key, value in record.items()))
    path.chmod(0o600)
    return root, path, record


def test_local_evidence_and_cli_restart(issuer, tmp_path, monkeypatch, capsys):
    root, path, record = issuer
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    assert parse_issuer_document(path.read_text()) == record
    db = str(tmp_path / 'cli.sqlite')
    args = ['register', '--db', db, '--profile', 'appdev', '--issuer-root', str(root)]
    for key in ('hd', 'team', 'channel', 'message_ts', 'thread_ts', 'issued', 'requester'):
        args += ['--' + key.replace('_', '-'), str(record[key])]
    main(args)
    assert 'HD-123' in capsys.readouterr().out
    assert Path(db).stat().st_mode & 0o777 == 0o600
    restarted = Ledger(db, profile='appdev')
    assert restarted.contains('T1', CHANNEL, '100.1')
    assert len(restarted.pending(now=1001)) == 1
    assert decide(restarted)['decision'] == 'approve'
    for command in ('results', 'pending'):
        main([command, '--db', db, '--profile', 'appdev'])
        output = capsys.readouterr().out
        assert ('HD-123' in output) == (command == 'results')
    assert len(Ledger(db, profile='appdev').results()) == 1
    with pytest.raises(SystemExit):
        main(args + ['--trusted'])


@pytest.mark.parametrize('field', [
    'hd', 'team', 'profile', 'channel', 'message_ts', 'thread_ts', 'issued',
    'expires', 'requester', 'approver', 'automatic_execution',
])
def test_each_issuer_field_must_match(issuer, field):
    root, path, record = issuer
    registration = {key: value for key, value in record.items()
                    if key not in ('approver', 'automatic_execution')}
    assert issuer_verifier(root)(registration) is True
    record[field] = 'wrong'
    path.write_text(''.join(f'{key}: {value}\n' for key, value in record.items()))
    assert issuer_verifier(root)(registration) is False


def test_missing_or_untrusted_evidence_cannot_initialize(issuer, tmp_path, monkeypatch):
    root, path, record = issuer
    args = ['register', '--db', str(tmp_path / 'absent.sqlite'), '--profile', 'appdev',
            '--issuer-root', str(root)]
    for key in ('hd', 'team', 'channel', 'message_ts', 'thread_ts', 'issued', 'requester'):
        args += ['--' + key.replace('_', '-'), str(record[key])]
    path.unlink()
    with pytest.raises(SystemExit):
        main(args)
    assert not (tmp_path / 'absent.sqlite').exists()
    with pytest.raises((sqlite3.OperationalError, OSError)):
        BusinessApproval(Mock(), str(tmp_path / 'absent.sqlite'), str(tmp_path / 'out'), profile='appdev')
    assert not (tmp_path / 'absent.sqlite').exists()


@pytest.mark.parametrize('mode', [0o755, 0o777])
def test_database_parent_must_be_private(tmp_path, mode):
    tmp_path.chmod(mode)
    with pytest.raises(ValueError):
        initialize_database(str(tmp_path / 'new.sqlite'))
    assert not (tmp_path / 'new.sqlite').exists()


def test_expiry_last_instant_and_first_instant(ledger):
    assert decide(ledger, now=87399.999)['decision'] == 'approve'
    # An already-decided card keeps its first decision, whatever the clock says.
    assert decide(ledger, now=87400)['decision'] == 'approve'
    ledger.register(hd='HD-124', team='T1', channel=CHANNEL, message_ts='300.1',
                    thread_ts='99.1', issued=1000, requester='joushou', now=1000)
    late = decide(ledger, message_ts='300.1', now=87400)  # first instant past expiry
    assert late['decision'] == 'expired'
    assert decide(ledger, message_ts='300.1', now=87401)['decision'] == 'expired'


def test_evidence_permissions_and_owner(issuer, monkeypatch):
    import os
    root, path, record = issuer
    registration = {key: value for key, value in record.items()
                    if key not in ('approver', 'automatic_execution')}
    path.chmod(0o644)
    assert issuer_verifier(root)(registration) is False
    path.chmod(0o600)
    uid = os.geteuid()
    monkeypatch.setattr(os, 'geteuid', lambda: uid + 1)
    assert issuer_verifier(root)(registration) is False
    with pytest.raises(ValueError):
        initialize_database(str(root / 'foreign.sqlite'))


# --- expiry persistence and outbox hand-off -------------------------------

def make_handler(ledger):
    adapter = Mock()
    adapter._event_team_id.return_value = 'T1'
    adapter._get_client.return_value = Mock(chat_update=AsyncMock())
    handler = BusinessApproval(adapter, ledger.path, str(Path(ledger.path).parent / 'outbox'), profile='appdev')
    return handler, adapter._get_client.return_value


def receipt(handler, row):
    return handler.outbox / f"{row['request_id']}_Slack業務承認結果.md"


def press(action_id):
    body = dict(channel={'id': CHANNEL}, user={'id': APPROVER}, container={'message_ts': '100.1'})
    return body, {'action_id': action_id, 'value': 'HD-123'}


def test_expire_due_persists_once_and_leaves_pending(ledger):
    assert ledger.expire_due(now=87399) == []
    rows = ledger.expire_due(now=87400)
    assert [(r['decision'], r['actor'], r['decided']) for r in rows] == [('expired', 'system', 87400)]
    assert ledger.expire_due(now=90000) == []
    assert ledger.pending(now=87400) == []
    stored = ledger.results()
    assert len(stored) == 1 and stored[0]['decision'] == 'expired' and stored[0]['decided'] == 87400


def test_expiry_does_not_touch_decided_rows(ledger):
    assert decide(ledger)['decision'] == 'approve'
    assert ledger.expire_due(now=90000) == []
    assert ledger.results()[0]['decision'] == 'approve'


def test_late_press_and_sweep_converge_on_first_writer(ledger):
    with ThreadPoolExecutor(max_workers=2) as pool:
        late = pool.submit(decide, ledger, now=90000, choice='approve')
        sweep = pool.submit(ledger.expire_due, now=90001)
    assert late.result()['decision'] == 'expired'
    rows = ledger.results()
    assert len(rows) == 1 and rows[0]['decision'] == 'expired' and rows[0]['actor'] == 'system'
    assert rows[0]['decided'] in (90000, 90001)
    assert len(sweep.result()) <= 1
    assert decide(ledger, now=1001)['decision'] == 'expired'  # never becomes an approval later


def test_expired_cli_results_and_pending(ledger, capsys):
    ledger.expire_due(now=87400)
    main(['results', '--db', ledger.path, '--profile', 'appdev'])
    assert '"decision": "expired"' in capsys.readouterr().out
    main(['pending', '--db', ledger.path, '--profile', 'appdev'])
    assert 'HD-123' not in capsys.readouterr().out


def test_sweep_renders_expired_and_writes_receipt_once(ledger, monkeypatch):
    handler, client = make_handler(ledger)
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 87400)
    asyncio.run(handler.sweep())
    asyncio.run(handler.sweep())
    row = ledger.results()[0]
    assert row['decision'] == 'expired' and row['outbox_written_at'] == 87400
    assert 'expired' in client.chat_update.call_args.kwargs['text']
    assert client.chat_update.await_count == 1
    assert 'expired' in receipt(handler, row).read_text()
    assert [p.name for p in handler.outbox.iterdir()] == [receipt(handler, row).name]


def test_outbox_written_once_and_replay_keeps_original(ledger, monkeypatch):
    handler, _ = make_handler(ledger)
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    asyncio.run(handler.action(AsyncMock(), *press('business_approve')))
    row = ledger.results()[0]
    original = receipt(handler, row).read_bytes()
    assert row['outbox_written_at'] == 1001
    asyncio.run(handler.action(AsyncMock(), *press('business_deny')))
    assert receipt(handler, row).read_bytes() == original
    assert 'outbox_written_at' not in original.decode()
    assert len(list(handler.outbox.iterdir())) == 1


def test_failed_placement_leaves_unwritten_then_sweep_retries(ledger, monkeypatch):
    handler, _ = make_handler(ledger)
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    real_link = os.link
    monkeypatch.setattr(os, 'link', Mock(side_effect=OSError('disk full')))
    asyncio.run(handler.action(AsyncMock(), *press('business_approve')))
    row = ledger.results()[0]
    assert row['decision'] == 'approve' and row['outbox_written_at'] is None
    assert not receipt(handler, row).exists()
    assert list(handler.outbox.iterdir()) == []  # temp file cleaned up
    monkeypatch.setattr(os, 'link', real_link)
    asyncio.run(handler.sweep())
    assert receipt(handler, row).exists()
    assert ledger.results()[0]['outbox_written_at'] is not None
    assert ledger.unwritten() == []


def test_repeated_outbox_failure_logged_once_after_three(ledger, monkeypatch, caplog):
    handler, _ = make_handler(ledger)
    decide(ledger)
    monkeypatch.setattr(os, 'link', Mock(side_effect=OSError('read-only')))
    with caplog.at_level('ERROR'):
        for _ in range(2):
            asyncio.run(handler.sweep())
        assert not [r for r in caplog.records if r.levelname == 'ERROR']
        for _ in range(4):
            asyncio.run(handler.sweep())
    errors = [r for r in caplog.records if r.levelname == 'ERROR']
    assert len(errors) == 1 and 'read-only' in errors[0].getMessage()


def test_retry_accepts_identical_existing_receipt(ledger):
    handler, _ = make_handler(ledger)
    row = decide(ledger)
    handler.outbox.mkdir()
    receipt(handler, row).write_text(handler.receipt_body(row))
    assert handler.deliver(row) is False  # first placement treats an existing name as failure
    asyncio.run(handler.sweep())
    assert ledger.results()[0]['outbox_written_at'] is not None


def test_retry_with_different_existing_receipt_errors_without_overwrite(ledger, caplog):
    handler, _ = make_handler(ledger)
    row = decide(ledger)
    handler.outbox.mkdir()
    receipt(handler, row).write_text('tampered\n')
    with caplog.at_level('ERROR'):
        for _ in range(5):
            asyncio.run(handler.sweep())
    assert receipt(handler, row).read_text() == 'tampered\n'
    assert ledger.results()[0]['outbox_written_at'] is None
    assert len([r for r in caplog.records if r.levelname == 'ERROR']) == 1
    assert len(list(handler.outbox.iterdir())) == 1


def test_migration_adds_column_and_keeps_existing_rows(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'old.sqlite'
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE cards (
            request_id TEXT PRIMARY KEY, hd TEXT NOT NULL, team TEXT NOT NULL,
            profile TEXT NOT NULL, channel TEXT NOT NULL, message_ts TEXT NOT NULL,
            thread_ts TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL,
            requester TEXT NOT NULL, decision TEXT, decided REAL, actor TEXT, source TEXT,
            UNIQUE(profile, hd), UNIQUE(profile, team, channel, message_ts))''')
        db.execute("INSERT INTO cards VALUES ('business-old', 'HD-9', 'T1', 'appdev', ?, '100.1', "
                   "'99.1', 1000, 87400, 'joushou', 'approve', 1001, ?, 'button')", (CHANNEL, APPROVER))
    initialize_database(str(path))
    initialize_database(str(path))  # idempotent
    store = Ledger(str(path), profile='appdev')
    rows = store.results()
    assert rows[0]['decision'] == 'approve' and rows[0]['outbox_written_at'] is None
    assert [r['request_id'] for r in store.unwritten()] == ['business-old']
    # A Ledger opened directly on an un-migrated database upgrades it too.
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE cards DROP COLUMN outbox_written_at')
    assert Ledger(str(path), profile='appdev').results()[0]['decision'] == 'approve'


def test_sweeper_task_runs_at_start_and_stops(ledger, monkeypatch):
    handler, _ = make_handler(ledger)
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 87400)
    async def exercise():
        handler.start()
        task = handler.sweeper
        handler.start()
        assert handler.sweeper is task
        for _ in range(100):
            await asyncio.sleep(0)
            if ledger.results():
                break
        await handler.stop()
        assert task.done() and handler.sweeper is None
    asyncio.run(exercise())
    assert ledger.results()[0]['decision'] == 'expired'


def _display_doc(record, **display):
    merged = dict(record)
    merged.update(display)
    return ''.join(f'{key}: {value}\n' for key, value in merged.items())


def test_issuer_document_accepts_optional_display_fields(issuer):
    _, path, record = issuer
    path.write_text(_display_doc(record, title='在庫棚卸しの実施',
                                 outcome='棚卸し結果が確定する', scope='本番データの変更なし'))
    parsed = parse_issuer_document(path.read_text())
    assert parsed['title'] == '在庫棚卸しの実施'
    assert parsed['outcome'] == '棚卸し結果が確定する'
    assert parsed['scope'] == '本番データの変更なし'


def test_issuer_document_rejects_bad_display_fields(issuer):
    _, _, record = issuer
    with pytest.raises(ValueError):
        parse_issuer_document(_display_doc(record, summary='未定義の項目'))
    with pytest.raises(ValueError):
        parse_issuer_document(_display_doc(record, title='a') + 'title: b\n')
    with pytest.raises(ValueError):
        parse_issuer_document(_display_doc(record, title='x' * 201))
    with pytest.raises(ValueError):
        parse_issuer_document(_display_doc(record, title='ok').replace('requester: joushou\n', ''))


def test_display_fields_must_match_issuer_document(issuer):
    root, path, record = issuer
    path.write_text(_display_doc(record, title='在庫棚卸しの実施'))
    registration = {key: value for key, value in record.items()
                    if key not in ('approver', 'automatic_execution')}
    assert issuer_verifier(root)(registration) is False
    assert issuer_verifier(root)(dict(registration, title='別の作業')) is False
    assert issuer_verifier(root)(dict(registration, title='在庫棚卸しの実施')) is True


def test_register_stores_display_fields(issuer, tmp_path, monkeypatch):
    root, path, record = issuer
    path.write_text(_display_doc(record, title='在庫棚卸しの実施',
                                 outcome='棚卸し結果が確定する', scope='本番データの変更なし'))
    monkeypatch.setattr('plugins.platforms.slack.business_approval.time.time', lambda: 1001)
    db = str(tmp_path / 'display.sqlite')
    initialize_database(db)
    store = Ledger(db, profile='appdev', verifier=issuer_verifier(root))
    store.register(hd='HD-123', team='T1', channel=CHANNEL, message_ts='100.1',
                   thread_ts='99.1', issued=1000.0, requester='joushou',
                   title='在庫棚卸しの実施', outcome='棚卸し結果が確定する',
                   scope='本番データの変更なし', now=1001)
    row = store.pending(now=1001)[0]
    assert row['title'] == '在庫棚卸しの実施'
    assert row['outcome'] == '棚卸し結果が確定する'
    assert row['scope'] == '本番データの変更なし'


def test_migration_adds_display_columns_and_keeps_existing_rows(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'legacy.sqlite'
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE cards (
            request_id TEXT PRIMARY KEY, hd TEXT NOT NULL, team TEXT NOT NULL,
            profile TEXT NOT NULL, channel TEXT NOT NULL, message_ts TEXT NOT NULL,
            thread_ts TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL,
            requester TEXT NOT NULL, decision TEXT, decided REAL, actor TEXT, source TEXT,
            UNIQUE(profile, hd), UNIQUE(profile, team, channel, message_ts))''')
        db.execute("INSERT INTO cards VALUES ('business-old', 'HD-9', 'T1', 'appdev', ?, '100.1', "
                   "'99.1', 1000, 87400, 'joushou', NULL, NULL, NULL, NULL)", (CHANNEL,))
    initialize_database(str(path))
    store = Ledger(str(path), profile='appdev')
    row = store.pending(now=1001)[0]
    assert row['hd'] == 'HD-9'
    assert row['title'] is None and row['outcome'] is None and row['scope'] is None
