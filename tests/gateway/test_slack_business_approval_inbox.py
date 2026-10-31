"""DM and App Home approval behavior: approved issuer only, no execution rights."""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.platforms.slack.business_approval import (
    APPROVER, CHANNEL, BusinessApproval, Ledger, initialize_database,
)
from plugins.platforms.slack.business_approval_inbox import ApprovalInbox


@pytest.fixture
def approval(tmp_path):
    tmp_path.chmod(0o700)
    db = tmp_path / "business.sqlite"
    initialize_database(str(db))
    ledger = Ledger(str(db), profile="koumei", verifier=lambda x: True)
    now = time.time()
    ledger.register(hd="HD-20261031-001", team="TWORK", channel=CHANNEL,
                    message_ts="100.1", thread_ts="99.1", issued=now-2,
                    requester="joushou", now=now-2)
    client = Mock()
    client.conversations_open = AsyncMock(return_value={"ok": True, "channel": {"id": "D123ABC", "user": APPROVER}})
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "101.2", "channel": "D123ABC"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.views_publish = AsyncMock(return_value={"ok": True})
    adapter = Mock()
    adapter._team_clients = {"TWORK": client}
    adapter._get_client.return_value = client
    adapter._event_team_id.side_effect = lambda event, body=None: (event.get("team_id") or ((body or {}).get("team") or {}).get("id") or (body or {}).get("team_id") or "")
    adapter.config.extra = {
        "business_approval_db": str(db),
        "business_approval_outbox": str(tmp_path / "outbox"),
        "business_approval_profile": "koumei",
        "business_approval_dm_enabled": True,
        "business_approval_home_enabled": True,
    }
    handler = BusinessApproval(adapter, str(db), str(tmp_path / "outbox"), profile="koumei")
    inbox = ApprovalInbox(handler, dm_enabled=True, home_enabled=True)
    handler.inbox = inbox
    return handler, inbox, client, adapter


def body(channel="D123ABC", ts="101.2", user=APPROVER, team="TWORK"):
    return {"team": {"id": team}, "channel": {"id": channel},
            "container": {"type": "message", "channel_id": channel, "message_ts": ts},
            "user": {"id": user}}


def run(coro):
    return asyncio.run(coro)


def test_sweep_sends_one_dm_and_updates_after_decision(approval):
    handler, inbox, client, _ = approval
    run(inbox.sweep())
    run(inbox.sweep())
    client.chat_postMessage.assert_awaited_once()
    sent = client.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "D123ABC"
    assert sent["blocks"][1]["elements"][0]["action_id"] == "business_dm_approve"
    request_id = handler.ledger.pending()[0]["request_id"]
    assert sent["blocks"][1]["elements"][0]["value"] == request_id
    run(inbox.action(AsyncMock(), body(), {"action_id": "business_dm_approve", "value": request_id}))
    assert handler.ledger.results()[0]["decision"] == "approve"
    client.chat_update.assert_awaited()
    run(inbox.sweep())
    client.chat_postMessage.assert_awaited_once()
    run(inbox.action(AsyncMock(), body(), {"action_id": "business_dm_deny", "value": request_id}))
    assert handler.ledger.results()[0]["decision"] == "approve"


@pytest.mark.parametrize("changes", [
    {"channel": "DOTHER"}, {"ts": "101.3"}, {"user": "UOTHER"},
    {"team": "TOTHER"},
])
def test_wrong_dm_identity_is_rejected(approval, changes):
    handler, inbox, _, _ = approval
    run(inbox.sweep())
    request_id = handler.ledger.pending()[0]["request_id"]
    ack = AsyncMock()
    run(inbox.action(ack, body(**changes), {"action_id": "business_dm_approve", "value": request_id}))
    ack.assert_awaited_once()
    assert handler.ledger.results() == []


def test_unregistered_or_forged_dm_value_is_rejected(approval):
    handler, inbox, _, _ = approval
    run(inbox.sweep())
    run(inbox.action(AsyncMock(), body(), {"action_id": "business_dm_approve", "value": "business-" + "f"*32}))
    assert handler.ledger.results() == []


def test_home_authorizes_exact_owner_and_exposes_pending_only(approval):
    handler, inbox, client, _ = approval
    run(inbox.home_opened({"user": APPROVER, "tab": "home", "team_id": "TWORK"}, {}))
    view = client.views_publish.call_args.kwargs["view"]
    assert view["type"] == "home"
    assert any("HD-20261031-001" in b.get("text", {}).get("text", "") for b in view["blocks"])
    request_id = handler.ledger.pending()[0]["request_id"]
    assert any(x.get("action_id") == "business_home_approve" and x.get("value") == request_id
               for b in view["blocks"] for x in b.get("elements", []))
    run(inbox.home_opened({"user": "UOTHER", "tab": "home", "team_id": "TWORK"}, {}))
    assert client.views_publish.await_count == 1
    ack = AsyncMock()
    run(inbox.action(ack, {"user": {"id": APPROVER}, "team": {"id": "TWORK"},
                          "view": {"type": "home"}},
                     {"action_id": "business_home_approve", "value": request_id}))
    assert handler.ledger.results()[0]["decision"] == "approve"
    assert client.views_publish.await_count >= 2


def test_home_rejects_wrong_team_actor_and_missing_view(approval):
    handler, inbox, _, _ = approval
    rid = handler.ledger.pending()[0]["request_id"]
    for x in ({"user": {"id": "UOTHER"}, "team": {"id": "TWORK"}, "view": {"type": "home"}},
              {"user": {"id": APPROVER}, "team": {"id": "TOTHER"}, "view": {"type": "home"}},
              {"user": {"id": APPROVER}, "team": {"id": "TWORK"}}):
        run(inbox.action(AsyncMock(), x, {"action_id": "business_home_approve", "value": rid}))
    assert handler.ledger.results() == []


def test_failed_dm_post_retries_without_marking_delivered(approval):
    handler, inbox, client, _ = approval
    client.chat_postMessage.side_effect = RuntimeError("temporary")
    run(inbox.sweep())
    assert inbox.delivered() == []
    client.chat_postMessage.side_effect = None
    run(inbox.sweep(now=time.time() + 61))
    assert len(inbox.delivered()) == 1
    assert client.chat_postMessage.await_count == 2


def test_invalid_slack_response_never_marks_delivered(approval):
    handler, inbox, client, _ = approval
    client.chat_postMessage.return_value = {"ok": False, "error": "not_in_channel"}
    run(inbox.sweep())
    assert inbox.delivered() == []


def test_dm_disabled_is_noop_and_home_still_works(approval):
    handler, _, client, _ = approval
    inbox = ApprovalInbox(handler, dm_enabled=False, home_enabled=True)
    run(inbox.sweep())
    client.chat_postMessage.assert_not_called()
    run(inbox.home_opened({"user": APPROVER, "tab": "home", "team_id": "TWORK"}, {}))
    client.views_publish.assert_awaited_once()


def test_missing_db_does_not_bootstrap(tmp_path):
    tmp_path.chmod(0o700)
    adapter = Mock()
    missing = tmp_path / "absent.sqlite"
    with pytest.raises((OSError, ValueError)):
        handler = BusinessApproval(adapter, str(missing), str(tmp_path / "outbox"), profile="koumei")
        ApprovalInbox(handler, dm_enabled=True, home_enabled=True)
    assert not missing.exists()


def test_larger_pending_queue_does_not_starve_beyond_first_page(approval):
    handler, inbox, client, _ = approval
    now = time.time()
    issuer = Ledger(handler.ledger.path, profile="koumei", verifier=lambda evidence: True)
    for i in range(1, 16):
        issuer.register(hd=f"HD-20261031-{i+1:03}", team="TWORK", channel=CHANNEL,
                        message_ts=f"{200+i}.1", thread_ts="99.1", issued=now-2,
                        requester="operator", now=now-2)
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": f"{1000+i}.1"} for i in range(17)]
    run(inbox.sweep(now=now))
    run(inbox.sweep(now=now+1))
    assert len(inbox.delivered()) == 16
    assert client.chat_postMessage.await_count == 16


def test_slack_response_missing_ok_is_not_accepted(approval):
    _, inbox, client, _ = approval
    client.chat_postMessage.return_value = {"ts": "101.2", "channel": "D123ABC"}
    run(inbox.sweep())
    assert inbox.delivered() == []


def test_delivery_uses_stable_message_deduplication_id(approval):
    _, inbox, client, _ = approval
    run(inbox.sweep())
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_postMessage.call_args.kwargs["client_msg_id"]


def test_existing_bind_wires_inbox_actions_and_home_events(approval):
    from plugins.platforms.slack.business_approval import bind
    _, _, client, adapter = approval
    handlers = {}

    def register(action_id):
        def decorator(func):
            handlers[action_id] = func
            return func
        return decorator

    adapter._app.action.side_effect = register
    original = AsyncMock()
    adapter._handle_app_home_opened = original
    bound = bind(adapter)
    assert bound.inbox is not None
    assert all(k in handlers for k in (
        "business_dm_approve", "business_dm_deny",
        "business_home_approve", "business_home_deny",
        "business_approve", "business_deny"))

    async def exercise():
        await adapter._handle_app_home_opened({"user": APPROVER, "tab": "home", "team_id": "TWORK"}, {})
        assert client.views_publish.await_count >= 1
        await adapter._handle_app_home_opened({"user": APPROVER, "tab": "messages", "team_id": "TWORK"}, {})
    run(exercise())
    assert original.await_count == 2


def test_existing_periodic_sweep_automatically_queues_dm(approval):
    handler, inbox, client, _ = approval
    run(handler.sweep())
    client.chat_postMessage.assert_awaited_once()
    assert len(inbox.delivered()) == 1


def test_after_decision_replay_does_not_issue_second_dm(approval):
    handler, inbox, client, _ = approval
    run(handler.sweep())
    row=handler.ledger.pending()[0]
    committed=handler.ledger.decide(team="TWORK",channel=CHANNEL,message_ts="100.1",
                                     user=APPROVER,choice="deny")
    run(handler.render(committed))
    run(handler.render(committed))
    assert len(handler.ledger.results()) == 1
    client.chat_postMessage.assert_awaited_once()
    assert len(inbox.delivered()) == 1
    assert client.views_publish.await_count >= 1


def test_restart_reconciles_unedited_decision_in_dm(approval):
    handler, inbox, client, _ = approval
    run(inbox.sweep())
    assert client.chat_update.await_count == 0
    row = handler.ledger.pending()[0]
    committed = handler.ledger.decide(team=row['team'], channel=row['channel'],
                                      message_ts=row['message_ts'], user=APPROVER,
                                      choice='approve')
    assert committed['decision'] == 'approve'
    # Simulate a gateway crash after the database commit but before UI rendering.
    restarted = ApprovalInbox(handler, dm_enabled=True, home_enabled=True)
    handler.inbox = restarted
    run(restarted.sweep())
    assert client.chat_update.await_count == 1
    assert restarted.delivered()[0]['updated_at'] is not None


def test_failed_status_update_is_retried_after_restart(approval):
    handler, inbox, client, _ = approval
    run(inbox.sweep())
    row = handler.ledger.pending()[0]
    committed = handler.ledger.decide(team=row['team'], channel=row['channel'],
                                      message_ts=row['message_ts'], user=APPROVER,
                                      choice='deny')
    client.chat_update.return_value = {'ok': False, 'error': 'ratelimited'}
    run(handler.render(committed))
    assert inbox.delivered()[0]['updated_at'] is None
    client.chat_update.return_value = {'ok': True}
    restarted = ApprovalInbox(handler, dm_enabled=True, home_enabled=True)
    handler.inbox = restarted
    run(restarted.sweep())
    # Original canonical message update + failed DM refresh + recovered DM refresh.
    assert client.chat_update.await_count == 3
    assert restarted.delivered()[0]['updated_at'] is not None


def test_home_paginates_25_pending_approvals(approval):
    handler, inbox, client, _ = approval
    issuer = Ledger(handler.ledger.path, profile='koumei', verifier=lambda x: True)
    now = time.time()
    for i in range(24):
        issuer.register(hd=f'HD-20261031-PAGE-{i:02}', team='TWORK', channel=CHANNEL,
                        message_ts=f'{500+i}.1', thread_ts='99.1',
                        issued=now-2, requester='operator', now=now-2)
    run(inbox.home_opened({'user': APPROVER, 'tab': 'home', 'team_id': 'TWORK'}, {}))
    first = client.views_publish.call_args.kwargs['view']
    assert '未処理 25件' in first['blocks'][0]['text']['text']
    buttons = [b for block in first['blocks'] for b in block.get('elements', [])]
    pager = [b for b in buttons if b.get('action_id') == 'business_home_page']
    assert len(pager) == 1 and pager[0]['value'] == '1'
    request = {'user': {'id': APPROVER}, 'team': {'id': 'TWORK'}, 'view': {'type': 'home'}}
    run(inbox.action(AsyncMock(), request, {'action_id': 'business_home_page', 'value': '1'}))
    next_page = client.views_publish.call_args.kwargs['view']
    assert '2ページ' in next_page['blocks'][0]['text']['text']
    assert len([x for b in next_page['blocks'] for x in b.get('elements', [])
                if x.get('action_id') in ('business_home_approve', 'business_home_deny')]) == 10
    previous = [x for b in next_page['blocks'] for x in b.get('elements', [])
                if x.get('action_id') == 'business_home_page']
    assert len(previous) == 1 and previous[0]['value'] == '0'


def test_forged_slack_post_channel_does_not_mark_delivery(approval):
    _, inbox, client, _ = approval
    client.chat_postMessage.return_value = {'ok': True, 'ts': '101.2', 'channel': 'DOTHER'}
    run(inbox.sweep())
    assert inbox.delivered() == []


def test_default_opt_out_preserves_existing_actions_without_new_surfaces(approval):
    from plugins.platforms.slack.business_approval import bind
    _, _, _, adapter = approval
    adapter.config.extra.pop('business_approval_dm_enabled')
    adapter.config.extra.pop('business_approval_home_enabled')
    handlers = {}

    def decorator(action_id):
        def add(fn):
            handlers[action_id] = fn
            return fn
        return add

    adapter._app.action.side_effect = decorator
    adapter._handle_app_home_opened = AsyncMock()
    bound = bind(adapter)
    assert bound.inbox is None
    assert set(handlers) == {'business_approve', 'business_deny'}
    assert adapter._handle_app_home_opened.await_count == 0


def test_dm_and_home_offer_canonical_request_details_link(approval):
    handler, inbox, client, _ = approval
    run(inbox.sweep())
    dm_blocks = client.chat_postMessage.call_args.kwargs['blocks']
    assert any('詳細' in b.get('text', {}).get('text', '') and
               '/thread/' in b.get('text', {}).get('text', '')
               for b in dm_blocks)
    run(inbox.home_opened({'user': APPROVER, 'tab': 'home', 'team_id': 'TWORK'}, {}))
    home_blocks = client.views_publish.call_args.kwargs['view']['blocks']
    assert any('詳細' in b.get('text', {}).get('text', '') and
               '/thread/' in b.get('text', {}).get('text', '')
               for b in home_blocks)


def test_pending_dm_and_home_explain_decision_deadline(approval):
    _, inbox, client, _ = approval
    run(inbox.sweep())
    dm_text = client.chat_postMessage.call_args.kwargs['blocks'][0]['text']['text']
    assert '期限:' in dm_text and 'JST' in dm_text
    run(inbox.home_opened({'user': APPROVER, 'tab': 'home', 'team_id': 'TWORK'}, {}))
    home_text = ' '.join(b.get('text', {}).get('text', '') for b in
                         client.views_publish.call_args.kwargs['view']['blocks'])
    assert '期限:' in home_text and 'JST' in home_text

def test_coordination_home_is_single_publisher_for_approval_and_observation(approval, tmp_path):
    from plugins.platforms.slack.coordination_home import bind as bind_home
    handler, inbox, client, adapter = approval
    adapter.config.extra["coordination_home"] = {
        "enabled": True, "boards": {"sample": str(tmp_path / "missing.sqlite")},
        "grants": [{"team_id": "TWORK", "user_id": APPROVER, "board": "sample"}],
    }
    adapter._business_approval = handler
    # The read-only observation DB may be disconnected; approvals still render.
    async def exercise():
        await bind_home(adapter).opened(
            {"user": APPROVER, "team_id": "TWORK", "tab": "home"}, {})
        await inbox.home_opened(
            {"user": APPROVER, "team_id": "TWORK", "tab": "home"}, {})
    run(exercise())
    assert client.views_publish.await_count == 1
    view = client.views_publish.call_args.kwargs["view"]
    assert any("業務承認" in b.get("text", {}).get("text", "") for b in view["blocks"])
    assert any("観測専用" in b.get("text", {}).get("text", "") for b in view["blocks"])


def test_decision_refreshes_shared_home_without_clobbering_observation(approval, tmp_path):
    from plugins.platforms.slack.coordination_home import bind as bind_home
    handler, inbox, client, adapter = approval
    adapter.config.extra["coordination_home"] = {
        "enabled": True, "boards": {"sample": str(tmp_path / "missing.sqlite")},
        "grants": [{"team_id": "TWORK", "user_id": APPROVER, "board": "sample"}],
    }
    adapter._business_approval = handler
    run(inbox.publish_home("TWORK"))
    assert client.views_publish.await_count == 1
    view = client.views_publish.call_args.kwargs["view"]
    assert view["blocks"][0]["text"]["text"].startswith("業務承認")
    assert any("観測専用" in b.get("text", {}).get("text", "") for b in view["blocks"])


def test_no_shared_home_publish_without_exact_observation_grant(approval, tmp_path):
    handler, inbox, client, adapter = approval
    adapter.config.extra["coordination_home"] = {
        "enabled": True, "boards": {"sample": str(tmp_path / "missing.sqlite")},
        "grants": [{"team_id": "TWORK", "user_id": "UOTHER", "board": "sample"}],
    }
    adapter._business_approval = handler
    run(inbox.publish_home("TWORK"))
    client.views_publish.assert_not_called()

