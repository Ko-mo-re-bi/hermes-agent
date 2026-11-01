"""Opt-in, read-only board snapshots on the existing Slack receiver."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from copy import deepcopy
import logging
import math
import re
from pathlib import Path
import sqlite3
import time

logger = logging.getLogger(__name__)
PAGE_SIZE = 20
ACTION = "hermes_observe_page"
READ_WORKERS = 4


@dataclass(frozen=True)
class Snapshot:
    connected: bool
    rows: tuple[dict, ...] = ()
    total: int = 0


def _open(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Unavailable database")
    # Live WAL requires existing shared memory. Refuse before connecting when
    # SQLite could create sidecars; never use immutable on a live database.
    with path.open("rb") as header:
        wal = header.read(20)[18:20] == b"\x02\x02"
    if wal:
        if not all(Path(str(path) + suffix).is_file() for suffix in ("-wal", "-shm")):
            raise ValueError("WAL sidecars unavailable")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    db.execute("PRAGMA query_only=ON")
    db.row_factory = sqlite3.Row
    return db


def snapshot(path, page=0, *, team="", channels=()):
    """Read only known columns; no native connect/bootstrap/migration calls."""
    try:
        with closing(_open(path)) as db:
            db.execute("BEGIN")
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            if not {"id", "title", "status", "assignee"} <= columns:
                return Snapshot(False)
            wanted = [c for c in ("id", "status", "assignee", "completed_at", "started_at", "created_at",
                                  "last_heartbeat_at", "last_failure_error") if c in columns]
            total = db.execute("SELECT count(*) FROM tasks").fetchone()[0]
            rows = [dict(r) for r in db.execute(
                f"SELECT {','.join(wanted)} FROM tasks ORDER BY id LIMIT ? OFFSET ?",
                (PAGE_SIZE, page * PAGE_SIZE))]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            _add_events(db, tables, rows)
            _add_artifacts(db, tables, rows)
            _add_threads(db, tables, rows, team, channels)
            return Snapshot(True, tuple(rows), total)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return Snapshot(False)


def _has_columns(db, table, required):
    return required <= {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _add_artifacts(db, tables, rows):
    if "task_attachments" not in tables or not _has_columns(db, "task_attachments", {"task_id", "id"}):
        return
    for row in rows:
        row["artifact_count"] = db.execute(
            "SELECT count(*) FROM task_attachments WHERE task_id=?", (row["id"],)).fetchone()[0]
        row["artifacts"] = [r[0] for r in db.execute(
            "SELECT id FROM task_attachments WHERE task_id=? ORDER BY id DESC LIMIT 3", (row["id"],))]


def _add_threads(db, tables, rows, team, channels):
    if "kanban_notify_subs" not in tables or not re.fullmatch(r"T[A-Z0-9]+", team):
        return
    if not _has_columns(db, "kanban_notify_subs", {"task_id", "platform", "chat_id", "thread_id"}):
        return
    for row in rows:
        row["threads"] = []
        for channel, thread in db.execute(
                "SELECT chat_id, thread_id FROM kanban_notify_subs "
                "WHERE task_id=? AND platform='slack' LIMIT 10", (row["id"],)):
            if (channel in channels and re.fullmatch(r"[CG][A-Z0-9]+", channel)
                    and re.fullmatch(r"[0-9]+\.[0-9]+", thread or "")):
                row["threads"].append(
                    f"https://app.slack.com/client/{team}/{channel}/thread/{channel}-{thread}")


def approval_connected(path):
    """Only probe the known schema. Never expose unbound cross-board approval data."""
    try:
        with closing(_open(path)) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(cards)")}
            return {"hd", "team", "profile", "channel", "message_ts", "decision"} <= columns
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def _add_events(db, tables, rows):
    if "task_events" in tables and _has_columns(db, "task_events", {"task_id", "created_at"}):
        for row in rows:
            row["event_at"] = db.execute(
                "SELECT MAX(created_at) FROM task_events WHERE task_id=?", (row["id"],)).fetchone()[0]


def _section(value):
    return {"type": "section", "text": {"type": "plain_text", "text": value[:2900], "emoji": False}}


STATUSES = {s: s for s in ("triage", "backlog", "todo", "ready", "running", "blocked", "review", "done", "archived", "scheduled")}
ASSIGNEES = {"hermes": "Hermes", "muse": "Muse", "grokbot": "GrokBot"}


def _stamp(value):
    try:
        return datetime.fromtimestamp(float(value), timezone(timedelta(hours=9))).strftime("%Y年%m月%d日 %H:%M:%S JST")
    except (ValueError, TypeError, OverflowError, OSError):
        return "不明"


def _task_id(row):
    value = row.get("id")
    return value if isinstance(value, str) and re.fullmatch(r"t_[0-9a-f]{8}", value) else "不明"


def _row_blocks(row):
    label, stamp = "時刻", None
    for key, name in (("event_at", "最終イベント"), ("completed_at", "完了日時"),
                      ("started_at", "開始日時"), ("created_at", "作成日時")):
        if row.get(key) is not None:
            label, stamp = name, row[key]
            break
    blocks = [_section(
        f"{_task_id(row)} / 状態: {STATUSES.get(row.get('status'), '不明')} / "
        f"担当: {ASSIGNEES.get(row.get('assignee'), '不明')}\n"
        f"{label}: {_stamp(stamp)}\nheartbeat（稼働保証なし）: {_stamp(row.get('last_heartbeat_at'))}\n"
        f"エラー: {'あり（詳細非表示）' if row.get('last_failure_error') else 'なし'}")]
    for url in row.get("threads", [])[:1]:
        blocks.append({"type": "actions", "elements": [{"type": "button",
            "action_id": "hermes_observe_thread", "text": {"type": "plain_text", "text": "既存thread"}, "url": url}]})
    return blocks


def render(data, board, page, approval=False, now=None):
    now = time.time() if now is None else now
    blocks = [_section(f"観測専用 / {page + 1}ページ / 観測時刻 {_stamp(now)}\n"
                       "実行・登録・承認操作なし。オンライン状態は取得していません。\n"
                       "Hermes / Muse / GrokBot: 観測状態未確認"),
              _section("業務承認DB: " + ("schema確認済み（binding未確認・内容非表示）" if approval else "未接続"))]
    if not data.connected:
        blocks.append(_section("ボード未接続（DBなし・旧schema・読取不可・WAL sidecar不足）"))
    elif not data.total:
        blocks.append(_section("接続済み / タスク0件"))
    else:
        blocks.append(_section(f"接続済み / 全{data.total}件（区分はページ内）"))
        for title, rows in (("判断待ち（task状態・承認binding未接続）", [r for r in data.rows if r.get("status") == "review"]),
                            ("進行・待機", [r for r in data.rows if r.get("status") != "review"])):
            blocks.append(_section(title))
            for row in rows:
                blocks.extend(_row_blocks(row))
        blocks.append(_section("最近の添付（成果物か未確認・ページ内・attachment ID降順、全体の時系列未接続）"))
        for row in data.rows:
            if row.get("artifacts"):
                blocks.append(_section(f"{_task_id(row)}: 添付{row.get('artifact_count', len(row['artifacts']))}件（成果物か未確認、アクセスリンク未接続）"))
        buttons = []
        for label, target in (("前へ", page - 1), ("次へ", page + 1)):
            if 0 <= target < (data.total + PAGE_SIZE - 1) // PAGE_SIZE:
                buttons.append({"type": "button", "action_id": ACTION + ("_prev" if target < page else "_next"),
                                "text": {"type": "plain_text", "text": label}, "value": str(target)})
        if buttons:
            blocks.append({"type": "actions", "elements": buttons})
    return {"type": "home", "blocks": blocks}


def _read(settings, page, team):
    _board, path, approval_path, channels = settings
    return (snapshot(path, page, team=team, channels=channels if isinstance(channels, list) else []),
            approval_connected(approval_path) if approval_path else False)


class Home:
    def __init__(self, adapter):
        self.adapter = adapter
        self.locks = {}
        self.pending = set()
        self.cooldown = {}
        self.read_slots = asyncio.Semaphore(READ_WORKERS)

    def settings(self, team, user):
        raw = self.adapter.config.extra.get("coordination_home")
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return None
        # Exact team/user pairs, with a single explicit board per audience.
        if not re.fullmatch(r"T[A-Z0-9]+", team or "") or not re.fullmatch(r"[UW][A-Z0-9]+", user or ""):
            return None
        grants = raw.get("grants")
        if not isinstance(grants, list):
            return None
        for grant in grants:
            if (isinstance(grant, dict) and grant.get("team_id") == team and team
                    and grant.get("user_id") == user and user):
                board = grant.get("board")
                boards = raw.get("boards", {})
                if isinstance(board, str) and isinstance(boards, dict) and board in boards:
                    return deepcopy((board, boards[board], raw.get("approval_db"), grant.get("channels", [])))
        return None

    async def opened(self, event, body=None, page=0, approval_page=0):
        team = self.adapter._event_team_id(event, body)
        user = event.get("user")
        settings = self.settings(team, user)
        if (settings is None or event.get("bot_id") or event.get("app_id")
                or team not in self.adapter._team_clients):
            logger.info("Slack observe Home denied: disabled or unauthorized identity")
            return
        key = (team, user)
        # One active and one queued refresh per viewer; excess events coalesce.
        if key in self.pending:
            return
        lock = self.locks.setdefault(key, asyncio.Lock())
        self.pending.add(key)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            self.pending.discard(key)
            settings = self.settings(team, user)
            client = self.adapter._team_clients.get(team)
            if settings is None or client is None or time.monotonic() < self.cooldown.get(team, 0):
                return
            async with self.read_slots:
                data, approval = await self._worker(settings, page, team)
            if (self.settings(team, user) != settings or self.adapter._team_clients.get(team) is not client
                    or time.monotonic() < self.cooldown.get(team, 0)):
                return
            board = settings[0]
            view = render(data, board, page, approval)
            business = getattr(self.adapter, "_business_approval", None)
            inbox = getattr(business, "inbox", None)
            if (user == "U0C79V9C6N6" and inbox is not None
                    and inbox.home_enabled and inbox._client(team) is client):
                try:
                    approval_blocks = await asyncio.to_thread(
                        inbox.home_blocks, team, page=approval_page, integrated=True)
                except (ValueError, OSError, sqlite3.Error):
                    approval_blocks = []
                    logger.warning("Combined Home approvals unavailable")
                if (self.settings(team, user) != settings
                        or self.adapter._team_clients.get(team) is not client):
                    return
                if approval_blocks:
                    # Slack Home permits at most 100 blocks. Prioritize decisions.
                    room = max(0, 100 - len(approval_blocks))
                    view["blocks"] = approval_blocks + view["blocks"][:room]
            try:
                await client.views_publish(user_id=user, view=view)
            except Exception as exc:  # health: allow BLE001 -- views_publish boundary: 429 handled via Retry-After cooldown; other failures are logged and dropped so a Home failure never breaks the adapter
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) == 429:
                    try:
                        delay = float(response.headers.get("Retry-After", "60"))
                        if not math.isfinite(delay) or delay < 0:
                            delay = 60
                    except (TypeError, ValueError):
                        delay = 60
                    self.cooldown[team] = time.monotonic() + delay
                    logger.warning("Slack observe Home rate limited; deferred until Retry-After")
                else:
                    logger.warning("Slack observe Home publish failed; no retry")

        finally:
            if acquired:
                lock.release()
            else:
                self.pending.discard(key)
            if not lock.locked() and key not in self.pending:
                self.locks.pop(key, None)

    async def _worker(self, settings, page, team):
        # Cancellation cannot stop a thread: drain it before releasing either slot.
        worker = asyncio.create_task(asyncio.to_thread(_read, settings, page, team))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def thread_action(self, ack, body, action):
        await ack()

    async def action(self, ack, body, action):
        await ack()
        try:
            page = int(action.get("value", ""))
        except (ValueError, TypeError):
            return
        if not 0 <= page <= 100000:
            return
        await self.opened({"user": (body.get("user") or {}).get("id")}, body, page)


def bind(adapter):
    home = getattr(adapter, "_coordination_home", None)
    if home is None:
        home = adapter._coordination_home = Home(adapter)
    return home
