"""Opt-in, profile-bound Slack DM and Home inbox for locally issued approvals.

No Slack event creates an approval. The original, verified issuance and the
private business.sqlite `cards` table remain the source of authority.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import re
import sqlite3
import time
import uuid

from plugins.platforms.slack.business_approval import APPROVER

LOG = logging.getLogger(__name__)
DM_ACTIONS = {"business_dm_approve": "approve", "business_dm_deny": "deny"}
HOME_ACTIONS = {"business_home_approve": "approve", "business_home_deny": "deny"}
HOME_PAGE_ACTION = "business_home_page"
_REQUEST = re.compile(r"business-[a-f0-9]{32}\Z")
_DM_CHANNEL = re.compile(r"D[A-Z0-9]+\Z")
_TEAM = re.compile(r"T[A-Z0-9]+\Z")
_TS = re.compile(r"[0-9]+\.[0-9]+\Z")
_NOTICE_SCHEMA = """CREATE TABLE IF NOT EXISTS business_approval_dm_notices (
    request_id TEXT PRIMARY KEY REFERENCES cards(request_id),
    profile TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('sending','sent')),
    claim_token TEXT NOT NULL,
    lease_until REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    dm_channel TEXT,
    dm_ts TEXT,
    updated_at REAL
)"""


def _data(value):
    if isinstance(value, dict):
        return value
    data = getattr(value, "data", None)
    return data if isinstance(data, dict) else {}


def _section(value):
    return {"type": "section", "text": {"type": "plain_text", "text": value[:2900], "emoji": False}}


def _deadline(row):
    return datetime.fromtimestamp(
        row["expires"], timezone(timedelta(hours=9))
    ).strftime("%Y-%m-%d %H:%M JST")


def _request_details(row):
    """Link to the registered canonical Slack thread, never a derived authority."""
    team, channel, thread = (row.get(key) for key in ('team', 'channel', 'thread_ts'))
    if (not isinstance(team, str) or not _TEAM.fullmatch(team)
            or not isinstance(channel, str) or not re.fullmatch(r"C[A-Z0-9]+", channel)
            or not isinstance(thread, str) or not _TS.fullmatch(thread)):
        return None
    url = f"https://app.slack.com/client/{team}/{channel}/thread/{channel}-{thread}"
    return {"type": "section", "text": {"type": "mrkdwn",
            "text": f"<{url}|依頼の詳細を見る>"}}


def _buttons(row, *, home=False):
    actions = HOME_ACTIONS if home else DM_ACTIONS
    return {"type": "actions", "elements": [
        {"type": "button", "action_id": action_id,
         "text": {"type": "plain_text", "text": "承認する" if choice == "approve" else "差し戻す"},
         "style": "primary" if choice == "approve" else "danger",
         "value": row["request_id"]}
        for action_id, choice in actions.items()]}


# Display text comes only from the verified issuer record. A field the issuer
# did not declare is shown as an explicit marker, never filled in by guessing;
# the canonical request link below the summary leads to the original request.
_NOT_STATED = "（発行原本に記載なし）"


def _display_value(row, key):
    value = row.get(key) if isinstance(row, dict) else None
    return value if isinstance(value, str) and value else _NOT_STATED


def _summary_lines(row):
    return (f"作業名: {_display_value(row, 'title')}\n"
            f"承認後の結果: {_display_value(row, 'outcome')}\n"
            f"影響範囲: {_display_value(row, 'scope')}")


class ApprovalInbox:
    """User-visible approval UI. Never interprets a click as execution permission."""

    def __init__(self, manager, *, dm_enabled=False, home_enabled=False):
        self.manager = manager
        self.ledger = manager.ledger
        self.adapter = manager.adapter
        self.dm_enabled = dm_enabled is True
        self.home_enabled = home_enabled is True

    def _client(self, team):
        if not isinstance(team, str) or not _TEAM.fullmatch(team):
            return None
        clients = getattr(self.adapter, "_team_clients", None)
        if not isinstance(clients, dict):
            return None
        return clients.get(team)

    def _db_card(self, request_id, team):
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            return None
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM cards WHERE request_id=? AND profile=? AND team=?",
                             (request_id, self.ledger.profile, team)).fetchone()
            return dict(row) if row else None

    def delivered(self):
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='business_approval_dm_notices'").fetchone()
            if table is None:
                return []
            return [dict(r) for r in db.execute(
                "SELECT * FROM business_approval_dm_notices WHERE profile=? AND state='sent'",
                (self.ledger.profile,))]

    def _claim(self, request_id, now):
        """Lease one notification; a crash or failed API call is retryable."""
        token = uuid.uuid4().hex
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM cards WHERE request_id=? AND profile=? "
                                 "AND decision IS NULL AND issued<=? AND expires>?",
                                 (request_id, self.ledger.profile, now, now)).fetchone()
                if row is None:
                    return None
                db.execute(_NOTICE_SCHEMA)
                old = db.execute("SELECT * FROM business_approval_dm_notices WHERE request_id=? "
                                 "AND profile=?", (request_id, self.ledger.profile)).fetchone()
                if old and (old["state"] == "sent" or old["lease_until"] > now):
                    return None
                if old:
                    db.execute("UPDATE business_approval_dm_notices SET state='sending', "
                               "claim_token=?, lease_until=?, attempts=attempts+1 "
                               "WHERE request_id=? AND profile=?",
                               (token, now+300, request_id, self.ledger.profile))
                else:
                    db.execute("INSERT INTO business_approval_dm_notices "
                               "(request_id,profile,state,claim_token,lease_until,attempts) "
                               "VALUES (?,?,'sending',?,?,1)",
                               (request_id, self.ledger.profile, token, now+300))
        return token

    def _finish(self, request_id, token, channel, ts):
        with closing(self.ledger.connect()) as db:
            with db:
                db.execute("UPDATE business_approval_dm_notices SET state='sent', "
                           "dm_channel=?,dm_ts=?,lease_until=0 WHERE request_id=? "
                           "AND profile=? AND state='sending' AND claim_token=?",
                           (channel, ts, request_id, self.ledger.profile, token))

    def _retry(self, request_id, token, now):
        with closing(self.ledger.connect()) as db:
            with db:
                db.execute("UPDATE business_approval_dm_notices SET lease_until=? "
                           "WHERE request_id=? AND profile=? AND state='sending' AND claim_token=?",
                           (now+60, request_id, self.ledger.profile, token))

    async def sweep(self, *, now=None):
        if not self.dm_enabled:
            return
        now = time.time() if now is None else now
        # A bounded tick cannot flood a DM after catching up from downtime.
        attempts = 0
        for row in self.ledger.pending(now=now):
            if attempts >= 12:
                break
            client = self._client(row["team"])
            if client is None:
                continue
            token = self._claim(row["request_id"], now)
            if not token:
                continue
            attempts += 1
            try:
                result = _data(await client.conversations_open(users=APPROVER))
                convo = result.get("channel") or {}
                dm_channel = convo.get("id")
                if result.get("ok") is not True or not isinstance(dm_channel, str) or not _DM_CHANNEL.fullmatch(dm_channel):
                    raise ValueError("Verified DM channel was not returned")
                # The identity is established through Slack's authenticated conversations.open.
                if convo.get("user") not in (None, APPROVER):
                    raise ValueError("DM recipient mismatch")
                # Re-check actual wall time after the awaited Slack API call.
                # The optional 'now' is a sweep/lease snapshot and may be stale;
                # relying on it here risks sending an already-expired approval.
                if time.time() >= row["expires"]:
                    self._retry(row["request_id"], token, now)
                    continue
                blocks = [_section(f"{_summary_lines(row)}\n"
                                   f"案件ID: {row['hd']}\n"
                                   f"依頼元: {row['requester']}\n"
                                   f"期限: {_deadline(row)}\n判断のみ・自動実行なし"),
                          _buttons(row)]
                details = _request_details(row)
                if details is not None:
                    blocks.append(details)
                result = _data(await client.chat_postMessage(
                    channel=dm_channel,
                    text=f"承認依頼: {_display_value(row, 'title')}（{row['hd']}）", blocks=blocks,
                    client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, row["request_id"]))))
                ts = result.get("ts")
                if (result.get("ok") is not True or not isinstance(ts, str)
                        or not _TS.fullmatch(ts) or result.get("channel", dm_channel) != dm_channel):
                    raise ValueError("Slack DM acknowledgement missing or channel mismatch")
                self._finish(row["request_id"], token, dm_channel, ts)
            except Exception as exc:  # health: allow BLE001 -- external Slack API faults must defer delivery without logging sensitive payloads
                self._retry(row["request_id"], token, now)
                LOG.warning("Approval DM delivery deferred: %s", type(exc).__name__)
        # A decision can commit just before the gateway stops. Repair DM UI on
        # restart without re-posting a second actionable approval request.
        await self._reconcile_decisions()

    async def _reconcile_decisions(self):
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='business_approval_dm_notices'").fetchone()
            if not exists:
                return
            rows = [dict(row) for row in db.execute(
                "SELECT cards.* FROM cards JOIN business_approval_dm_notices n "
                "ON cards.request_id=n.request_id WHERE cards.profile=? "
                "AND n.profile=? AND n.state='sent' AND n.updated_at IS NULL "
                "AND cards.decision IS NOT NULL ORDER BY cards.decided LIMIT 12",
                (self.ledger.profile, self.ledger.profile))]
        for row in rows:
            await self.after_decision(row)

    def _dm_row(self, request_id, team, channel, ts):
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            return None
        if not isinstance(channel, str) or not _DM_CHANNEL.fullmatch(channel):
            return None
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='business_approval_dm_notices'").fetchone()
            if not table:
                return None
            row = db.execute("SELECT cards.* FROM cards "
                             "JOIN business_approval_dm_notices n ON n.request_id=cards.request_id "
                             "WHERE cards.request_id=? AND cards.profile=? AND cards.team=? "
                             "AND n.profile=? AND n.state='sent' AND n.dm_channel=? AND n.dm_ts=?",
                             (request_id, self.ledger.profile, team, self.ledger.profile,
                              channel, ts)).fetchone()
            return dict(row) if row else None

    async def action(self, ack, body, action):
        await ack()
        action_id = action.get("action_id") or ""
        user = (body.get("user") or {}).get("id")
        team = self.adapter._event_team_id({}, body)
        if user != APPROVER or self._client(team) is None:
            return
        if action_id == HOME_PAGE_ACTION and self.home_enabled:
            value = action.get("value")
            if ((body.get("view") or {}).get("type") == "home"
                    and isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,3})", value)):
                await self.publish_home(team, page=int(value))
            return
        request_id = action.get("value")
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            return
        if action_id in DM_ACTIONS and self.dm_enabled:
            container = body.get("container") or {}
            channel = (body.get("channel") or {}).get("id")
            if container.get("type") != "message" or channel != container.get("channel_id"):
                return
            row = self._dm_row(request_id, team, channel, container.get("message_ts"))
            choice = DM_ACTIONS[action_id]
        elif action_id in HOME_ACTIONS and self.home_enabled:
            if (body.get("view") or {}).get("type") != "home":
                return
            row = self._db_card(request_id, team)
            choice = HOME_ACTIONS[action_id]
        else:
            return
        if row is None:
            return
        result = self.ledger.decide(team=team, channel=row["channel"],
                                    message_ts=row["message_ts"], user=user,
                                    hd=row["hd"], choice=choice)
        if result is not None:
            await self.manager.render(result)

    async def home_opened(self, event, body):
        if not self.home_enabled or event.get("tab") != "home" or event.get("user") != APPROVER:
            return
        raw = self.adapter.config.extra.get("coordination_home")
        if isinstance(raw, dict) and raw.get("enabled") is True:
            # The registered coordination handler already published the single view.
            return
        team = self.adapter._event_team_id(event, body)
        await self.publish_home(team)

    def home_blocks(self, team, *, page=0, integrated=False):
        """Build a bounded approval section without publishing another Home."""
        if not self.home_enabled or self._client(team) is None:
            return []
        rows = sorted((r for r in self.ledger.pending() if r["team"] == team),
                      key=lambda r: (r["expires"], r["request_id"]))
        page_size = 8 if integrated else 20
        if page < 0 or page > 9999 or (rows and page * page_size >= len(rows)):
            return []
        blocks = [_section(f"業務承認 / 未処理 {len(rows)}件 / {page+1}ページ"),
                  _section("承認・差戻しは業務判断のみ。実行権限は付与されません。")]
        for row in rows[page * page_size:(page+1) * page_size]:
            blocks.extend((_section(f"作業名: {_display_value(row, 'title')}\n"
                                     f"案件ID: {row['hd']}\n"
                                     f"承認後の結果: {_display_value(row, 'outcome')} / "
                                     f"影響範囲: {_display_value(row, 'scope')}\n"
                                     f"依頼元: {row['requester']} / 期限: {_deadline(row)}"),
                           _buttons(row, home=True)))
            details = _request_details(row)
            if details is not None:
                blocks.append(details)
        paging = []
        for label, number in (("前へ", page-1), ("次へ", page+1)):
            if 0 <= number < (len(rows) + page_size - 1) // page_size:
                paging.append({"type": "button", "action_id": HOME_PAGE_ACTION,
                               "text": {"type": "plain_text", "text": label},
                               "value": str(number)})
        if paging:
            blocks.append({"type": "actions", "elements": paging})
        return blocks

    async def publish_home(self, team, *, page=0):
        client = self._client(team)
        if client is None or not self.home_enabled:
            return
        raw = self.adapter.config.extra.get("coordination_home")
        if isinstance(raw, dict) and raw.get("enabled") is True:
            # One publisher per App Home. No fallback if observation grants deny.
            from plugins.platforms.slack.coordination_home import bind as bind_home
            home = bind_home(self.adapter)
            if home.settings(team, APPROVER) is not None:
                await home.opened({"user": APPROVER, "team_id": team, "tab": "home"},
                                  {}, approval_page=page)
            return
        blocks = self.home_blocks(team, page=page)
        if not blocks:
            return
        try:
            result = _data(await client.views_publish(
                user_id=APPROVER, view={"type": "home", "blocks": blocks}))
            if result.get("ok") is not True:
                raise ValueError("App Home publish rejected")
        except Exception as exc:  # health: allow BLE001 -- optional Slack UI failure must not leak request payloads
            LOG.warning("Approval App Home publish failed: %s", type(exc).__name__)

    async def after_decision(self, row):
        if self.dm_enabled:
            for notice in self.delivered():
                if notice["request_id"] != row["request_id"] or notice["updated_at"] is not None:
                    continue
                client = self._client(row["team"])
                if client is None:
                    break
                try:
                    text = (f"作業名: {_display_value(row, 'title')}（{row['hd']}）: "
                            f"{row['decision']}（処理済み・自動実行なし）")
                    result = _data(await client.chat_update(
                        channel=notice["dm_channel"], ts=notice["dm_ts"],
                        text=text, blocks=[_section(text)]))
                    if result.get("ok") is not True:
                        raise ValueError("DM update rejected")
                    with closing(self.ledger.connect()) as db:
                        with db:
                            db.execute("UPDATE business_approval_dm_notices SET updated_at=? "
                                       "WHERE request_id=? AND profile=? AND state='sent'",
                                       (time.time(), row["request_id"], self.ledger.profile))
                except Exception as exc:  # health: allow BLE001 -- preserve committed decision when optional Slack update fails
                    LOG.warning("Approval DM state refresh failed: %s", type(exc).__name__)
        await self.publish_home(row["team"])
