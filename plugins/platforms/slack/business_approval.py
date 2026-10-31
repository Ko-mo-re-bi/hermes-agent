"""Verified-relay business decisions. Never grants tool execution rights.

The database path is an explicit adapter setting, not an inbound event field.
Registration requires a trusted relay verifier; Slack cannot register cards.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import stat
import re
import sqlite3
import time
import uuid
from pathlib import Path

CHANNEL = "C0C84GJEC5R"
APPROVER = "U0C79V9C6N6"
CHOICES = {"business_approve": "approve", "business_deny": "deny"}
SWEEP_SECONDS = 60
OUTBOX_ERROR_AFTER = 3


ISSUER_FIELDS = {"hd", "team", "profile", "channel", "thread_ts", "message_ts",
                 "issued", "expires", "requester", "approver", "automatic_execution"}


def parse_issuer_document(text):
    """Parse a dedicated issuer record: exactly one key: value line per field."""
    record = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key not in ISSUER_FIELDS or key in record or not value.strip():
            raise ValueError("Invalid issuer record")
        record[key] = value.strip()
    if record.keys() != ISSUER_FIELDS:
        raise ValueError("Incomplete issuer record")
    for key in ("issued", "expires"):
        record[key] = float(record[key])
        if not math.isfinite(record[key]):
            raise ValueError("Invalid issuer timestamp")
    return record


def check_owned(path, *, directory=False):
    if path.resolve() != path:
        raise ValueError("Symlink paths are not accepted")
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (not expected(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != mode):
        raise ValueError("Path must be owned by the profile process UID with private permissions")


def issuer_verifier(root):
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Issuer root must be absolute")

    def verify(registration):
        try:
            check_owned(root, directory=True)
            path = root / (registration["hd"] + ".md")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    return False
                record = parse_issuer_document(stream.read())
            return record == dict(registration, approver=APPROVER, automatic_execution="none")
        except (OSError, ValueError):
            return False
    return verify


def initialize_database(path):
    """CLI-only bootstrap; never called by Slack ingress or Ledger.register."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Database path must be absolute")
    check_owned(path.parent, directory=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        check_owned(path)
    else:
        os.close(descriptor)
    with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS cards (
            request_id TEXT PRIMARY KEY, hd TEXT NOT NULL, team TEXT NOT NULL,
            profile TEXT NOT NULL, channel TEXT NOT NULL, message_ts TEXT NOT NULL,
            thread_ts TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL,
            requester TEXT NOT NULL, decision TEXT, decided REAL, actor TEXT, source TEXT,
            UNIQUE(profile, hd), UNIQUE(profile, team, channel, message_ts))''')
        migrate(db)


def migrate(db):
    """Add columns introduced after the first schema; existing rows keep NULL."""
    def present():
        return "outbox_written_at" in {row[1] for row in db.execute("PRAGMA table_info(cards)")}
    if not present():
        try:
            db.execute("ALTER TABLE cards ADD COLUMN outbox_written_at REAL")
        except sqlite3.OperationalError:
            if not present():  # a concurrent process may have added it first
                raise


# One statement, shared by the sweeper and by a late button press; first writer wins.
EXPIRE_SQL = ("UPDATE cards SET decision='expired', actor='system', decided=? "
              "WHERE decision IS NULL")


class Ledger:
    def __init__(self, path: str, *, profile: str, verifier=None):
        self.path = path
        self.profile = profile
        self.verifier = verifier
        if not profile or not Path(path).is_absolute():
            raise ValueError("An absolute database path and owning profile are required")

    def connect(self):
        # The issuer creates the database; ingress must never create an empty trust root.
        check_owned(Path(self.path).parent, directory=True)
        check_owned(Path(self.path))
        db = sqlite3.connect(Path(self.path).as_uri() + "?mode=rw", uri=True)
        try:
            with db:
                migrate(db)
        except BaseException:
            db.close()
            raise
        return db

    def register(self, *, hd: str, team: str, channel: str, message_ts: str,
                 thread_ts: str, issued: float, requester: str, now=None):
        now = time.time() if now is None else now
        if (not re.fullmatch(r"HD-[A-Za-z0-9-]+", hd) or not team or not requester
                or channel != CHANNEL or not thread_ts
                or not re.fullmatch(r"\d+\.\d+", message_ts)
                or not re.fullmatch(r"\d+\.\d+", thread_ts)
                or not math.isfinite(issued) or not issued <= now < issued + 86400):
            raise ValueError("Invalid registration or issuance time")
        registration = dict(hd=hd, team=team, profile=self.profile, channel=channel,
                            message_ts=message_ts, thread_ts=thread_ts, issued=issued,
                            expires=issued + 86400, requester=requester)
        # Only the local issuer record establishes registration provenance.
        if self.verifier is None or self.verifier(registration) is not True:
            raise ValueError("Verified work-relay registration is required")
        with self.connect() as db:
            db.execute("INSERT INTO cards (request_id, hd, team, profile, channel, message_ts, "
                       "thread_ts, issued, expires, requester) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("business-" + uuid.uuid4().hex, hd, team, self.profile, channel,
                        message_ts, thread_ts, issued, issued + 86400, requester))

    def decide(self, *, team, channel, message_ts, user, choice, hd=None, now=None, source="button"):
        if (user != APPROVER or channel != CHANNEL or choice not in CHOICES.values() or not team
                or source not in ("button", "reaction")):
            return None
        now = time.time() if now is None else now
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cards WHERE profile=? AND team=? AND channel=? AND message_ts=?",
                             (self.profile, team, channel, message_ts)).fetchone()
            if row is None or (hd is not None and row['hd'] != hd):
                return None
            if now < row['issued']:
                return None
            # Both writes are guarded by decision IS NULL, so only the first one sticks.
            if now >= row['expires']:
                db.execute(EXPIRE_SQL + " AND request_id=?", (now, row['request_id']))
            else:
                db.execute("UPDATE cards SET decision=?, decided=?, actor=?, source=? "
                           "WHERE request_id=? AND decision IS NULL",
                           (choice, now, user, source, row['request_id']))
            return dict(db.execute("SELECT * FROM cards WHERE request_id=?", (row['request_id'],)).fetchone())

    def expire_due(self, *, now=None):
        """Persist 'expired' for every undecided card past its deadline; return the rows it changed."""
        now = time.time() if now is None else now
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                EXPIRE_SQL + " AND profile=? AND expires<=? RETURNING *", (now, self.profile, now))]

    def mark_outbox_written(self, request_id, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("UPDATE cards SET outbox_written_at=? WHERE request_id=? AND profile=? "
                       "AND outbox_written_at IS NULL", (now, request_id, self.profile))

    def unwritten(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM cards WHERE profile=? AND decision IS NOT NULL "
                "AND outbox_written_at IS NULL ORDER BY decided", (self.profile,))]

    def contains(self, team, channel, message_ts):
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM cards WHERE profile=? AND team=? AND channel=? AND message_ts=?",
                (self.profile, team, channel, message_ts)).fetchone() is not None

    def results(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM cards WHERE profile=? AND decision IS NOT NULL ORDER BY decided",
                (self.profile,))]

    def pending(self, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM cards WHERE profile=? AND decision IS NULL AND issued<=? AND expires>?",
                (self.profile, now, now))]


def card(hd: str, summary: str):
    """Issuer posts these blocks, then registers the returned message timestamp."""
    return [{"type": "section", "text": {"type": "plain_text", "text": f"{hd}\n{summary}"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": label},
                 "action_id": action, "value": hd}
                for action, label in (("business_approve", "承認"), ("business_deny", "差戻し"))]}]


class BusinessApproval:
    def __init__(self, adapter, path, outbox, *, profile):
        self.adapter = adapter
        self.ledger = Ledger(path, profile=profile)
        self.outbox = Path(outbox)
        self.outbox_failures = {}
        self.outbox_reported = set()
        self.sweeper = None
        self.inbox = None
        if not Path(path).is_absolute() or not self.outbox.is_absolute():
            raise ValueError("Business approval paths must be absolute and profile-owned")
        with self.ledger.connect():
            pass

    @staticmethod
    def display_text(row):
        if row['decision'] == 'expired':
            return f"{row['hd']}: expired（期限切れ・未決定）\n依頼元: {row['requester']}（業務判断のみ・実行権限なし）"
        return (f"{row['hd']}: {row['decision']} / <@{APPROVER}>\n"
                f"依頼元: {row['requester']}（業務判断のみ・実行権限なし）")

    def receipt_path(self, row):
        return self.outbox / f"{row['request_id']}_Slack業務承認結果.md"

    def receipt_body(self, row):
        # outbox_written_at is delivery bookkeeping, not part of the decision record.
        record = {key: value for key, value in row.items() if key != 'outbox_written_at'}
        return self.display_text(row) + "\n\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n"

    def place_receipt(self, row, *, compare_existing):
        """Publish the receipt atomically: temp file, then os.link (never replaces a name)."""
        body = self.receipt_body(row).encode()
        target = self.receipt_path(row)
        self.outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.outbox / f".{row['request_id']}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if not compare_existing:
                    raise
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(body).digest():
                    raise ValueError("Existing receipt differs from the recorded decision") from None
        finally:
            temporary.unlink(missing_ok=True)

    def deliver(self, row, *, compare_existing=False):
        """Create the receipt after the DB decision; record success, count and report failures."""
        if row.get('outbox_written_at') is not None:
            return True
        key = row['request_id']
        try:
            self.place_receipt(row, compare_existing=compare_existing)
            self.ledger.mark_outbox_written(key)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.outbox_failures[key] = self.outbox_failures.get(key, 0) + 1
            if self.outbox_failures[key] >= OUTBOX_ERROR_AFTER and key not in self.outbox_reported:
                self.outbox_reported.add(key)
                logging.getLogger(__name__).error(
                    "Business approval result file not written for %s after %d attempts: %s",
                    key, self.outbox_failures[key], exc)
            return False
        self.outbox_failures.pop(key, None)
        self.outbox_reported.discard(key)
        return True

    async def sweep(self):
        """Persist expiries, then re-place any receipts a previous attempt left missing."""
        fresh = self.ledger.expire_due()
        for row in fresh:
            await self.render(row)
        done = {row['request_id'] for row in fresh}
        for row in self.ledger.unwritten():
            if row['request_id'] not in done:
                self.deliver(row, compare_existing=True)
        if self.inbox is not None:
            await self.inbox.sweep()

    async def _sweep_forever(self):
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("Business approval sweep failed", exc_info=True)
            await asyncio.sleep(SWEEP_SECONDS)

    def start(self):
        """Run the sweep at startup and every SWEEP_SECONDS. Needs a running event loop."""
        if self.sweeper is None or self.sweeper.done():
            self.sweeper = asyncio.get_running_loop().create_task(self._sweep_forever())

    async def stop(self):
        sweeper, self.sweeper = self.sweeper, None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)

    async def render(self, row):
        text = self.display_text(row)
        self.deliver(row)
        try:
            await self.adapter._get_client(row['channel'], team_id=row['team']).chat_update(
                channel=row['channel'], ts=row['message_ts'], text=text,
                blocks=[{"type": "section", "text": {"type": "plain_text", "text": text}}])
        except Exception:
            logging.getLogger(__name__).warning(
                "Business approval display update failed; durable result retained")
        if self.inbox is not None:
            await self.inbox.after_decision(row)

    async def reaction(self, event, body, removed=False):
        item = event.get('item') or {}
        if item.get('type') != 'message':
            return False
        team = self.adapter._event_team_id(event, body)
        if not self.ledger.contains(team, item.get('channel'), item.get('ts')):
            return False
        if removed:
            return True
        choice = {"white_check_mark": "approve", "x": "deny"}.get(event.get('reaction'))
        row = self.ledger.decide(team=team,
                                 channel=item.get('channel'), message_ts=item.get('ts'),
                                 user=event.get('user'), choice=choice, source='reaction')
        if row:
            await self.render(row)
        return True

    async def action(self, ack, body, action):
        await ack()
        row = self.ledger.decide(team=self.adapter._event_team_id({}, body),
                                 channel=(body.get('channel') or {}).get('id'),
                                 message_ts=(body.get('container') or {}).get('message_ts'),
                                 user=(body.get('user') or {}).get('id'),
                                 choice=CHOICES.get(action.get('action_id')), hd=action.get('value', ''))
        if row:
            await self.render(row)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('register', 'results', 'pending'):
        command_parser = sub.add_parser(command)
        command_parser.add_argument('--db', required=True)
        command_parser.add_argument('--profile', required=True)
        if command == 'register':
            command_parser.add_argument('--issuer-root', required=True)
            for field in ('hd', 'team', 'channel', 'message-ts', 'thread-ts', 'requester'):
                command_parser.add_argument('--' + field, required=True)
            command_parser.add_argument('--issued', required=True, type=float)
    args = parser.parse_args(argv)
    try:
        verifier = issuer_verifier(args.issuer_root) if args.command == 'register' else None
        ledger = Ledger(args.db, profile=args.profile, verifier=verifier)
        if args.command == 'register':
            registration = {key: getattr(args, key) for key in
                            ('hd', 'team', 'channel', 'message_ts', 'thread_ts', 'issued', 'requester')}
            # Verify before initialization so rejected evidence cannot create a trust store.
            evidence = dict(registration, profile=args.profile, expires=args.issued + 86400)
            if verifier(evidence) is not True:
                raise ValueError("Local issuer evidence rejected")
            initialize_database(args.db)
            ledger.register(**registration)
            rows = ledger.pending()
        else:
            rows = ledger.results() if args.command == 'results' else ledger.pending()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Registration/query rejected: {exc}\n")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()


def bind(adapter):
    """Bind one explicitly configured ledger to its owning adapter only."""
    path = adapter.config.extra.get("business_approval_db")
    if not path:
        return None
    handler = BusinessApproval(adapter, path, adapter.config.extra["business_approval_outbox"],
                               profile=adapter.config.extra["business_approval_profile"])
    for action_id in CHOICES:
        adapter._app.action(action_id)(handler.action)
    dm_enabled = adapter.config.extra.get("business_approval_dm_enabled") is True
    home_enabled = adapter.config.extra.get("business_approval_home_enabled") is True
    if dm_enabled or home_enabled:
        from plugins.platforms.slack.business_approval_inbox import (
            ApprovalInbox, DM_ACTIONS, HOME_ACTIONS, HOME_PAGE_ACTION,
        )
        handler.inbox = ApprovalInbox(handler, dm_enabled=dm_enabled,
                                     home_enabled=home_enabled)
        for action_id in (DM_ACTIONS if dm_enabled else {}):
            adapter._app.action(action_id)(handler.inbox.action)
        for action_id in (HOME_ACTIONS if home_enabled else {}):
            adapter._app.action(action_id)(handler.inbox.action)
        if home_enabled:
            adapter._app.action(HOME_PAGE_ACTION)(handler.inbox.action)
            original_home = adapter._handle_app_home_opened

            async def _approval_home_event(event, body=None):
                await original_home(event, body)
                await handler.inbox.home_opened(event, body)

            adapter._handle_app_home_opened = _approval_home_event
    return handler
