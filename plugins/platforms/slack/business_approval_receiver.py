"""Explicit, idempotent receipt hand-off for Slack business approvals.

Not wired to Slack ingress, exec approval, a background job, or automatic
execution. A trusted local operator claims and separately acknowledges a
validated receipt after downstream handling. Reusing the approval SQLite file
avoids a second task queue.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from plugins.platforms.slack.business_approval import (
    BusinessApproval,
    Ledger,
    check_owned,
)

_REQUEST_ID = re.compile(r"business-[0-9a-f]{32}\Z")
_OPERATOR = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_EVIDENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,199}\Z")
_MAX_RECEIPT_BYTES = 262144
_MAX_LEASE_SECONDS = 86400
_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_approval_receipts (
    request_id TEXT PRIMARY KEY REFERENCES cards(request_id),
    profile TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('claimed', 'acked')),
    claim_token TEXT NOT NULL,
    claimed_by TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    lease_until REAL NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    acked_at REAL,
    acked_by TEXT,
    evidence_ref TEXT
)
"""


class ReceiptReceiver:
    """A local hand-off ledger, not an executor or an identity provider.

    The OS owner of the private DB/outbox is the trust boundary. Operator
    labels are audit labels, not independent authentication. External business
    effects must be independently deduplicated using request_id.
    """

    def __init__(self, db_path: str, outbox: str, *, profile: str):
        self.ledger = Ledger(db_path, profile=profile)
        self.outbox = Path(outbox)
        if not self.outbox.is_absolute():
            raise ValueError("Outbox must use an absolute path")

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ValueError("Invalid request_id")

    @staticmethod
    def _validate_operator(operator: str) -> None:
        if not isinstance(operator, str) or not _OPERATOR.fullmatch(operator):
            raise ValueError("Invalid operator label")

    @staticmethod
    def _validate_now(now: float) -> None:
        if not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise ValueError("Invalid timestamp")

    def _readonly(self):
        db_path = Path(self.ledger.path)
        check_owned(db_path.parent, directory=True)
        check_owned(db_path)
        db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _has_table(db) -> bool:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='business_approval_receipts'"
        ).fetchone() is not None

    @staticmethod
    def _ensure_table(db) -> None:
        db.execute(_SCHEMA)

    def _card(self, db, request_id: str) -> dict:
        self._validate_request_id(request_id)
        row = db.execute(
            "SELECT * FROM cards WHERE request_id=? AND profile=? "
            "AND decision IS NOT NULL AND outbox_written_at IS NOT NULL",
            (request_id, self.ledger.profile),
        ).fetchone()
        if row is None:
            raise ValueError("No delivered decision for this profile and request")
        return dict(row)

    def _receipt_sha256(self, row: dict) -> str:
        self._validate_request_id(row["request_id"])
        check_owned(self.outbox, directory=True)
        path = self.outbox / (row["request_id"] + "_Slack業務承認結果.md")
        expected = (
            BusinessApproval.display_text(row)
            + "\n\n"
            + json.dumps(
                {key: value for key, value in row.items() if key != "outbox_written_at"},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        if len(expected) > _MAX_RECEIPT_BYTES:
            raise ValueError("Receipt exceeds size limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError("Receipt ownership, link count or mode invalid")
            actual = stream.read(_MAX_RECEIPT_BYTES + 1)
        if actual != expected:
            raise ValueError("Receipt does not match the durable decision")
        return hashlib.sha256(actual).hexdigest()

    def list_status(self, *, now: float | None = None, limit: int = 100) -> list[dict]:
        """Read-only status: never treats a missing receipt as acknowledged."""
        now = time.time() if now is None else now
        self._validate_now(now)
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid limit")
        with closing(self._readonly()) as db:
            has_table = self._has_table(db)
            rows = db.execute(
                "SELECT * FROM cards WHERE profile=? AND decision IS NOT NULL "
                "ORDER BY decided DESC LIMIT ?",
                (self.ledger.profile, limit),
            ).fetchall()
            result = []
            for item in rows:
                row = dict(item)
                status = "receipt_pending"
                if row["outbox_written_at"] is not None:
                    try:
                        self._receipt_sha256(row)
                    except (OSError, ValueError):
                        status = "receipt_invalid"
                    else:
                        status = "ready"
                        if has_table:
                            receipt = db.execute(
                                "SELECT state, lease_until FROM business_approval_receipts "
                                "WHERE request_id=? AND profile=?",
                                (row["request_id"], self.ledger.profile),
                            ).fetchone()
                            if receipt:
                                if receipt["state"] == "acked":
                                    status = "acked"
                                elif receipt["lease_until"] > now:
                                    status = "claimed"
                result.append({
                    "request_id": row["request_id"],
                    "hd": row["hd"],
                    "decision": row["decision"],
                    "status": status,
                })
            return result

    def claim(
        self,
        request_id: str,
        *,
        operator: str,
        now: float | None = None,
        lease_seconds: int = 600,
    ) -> dict:
        """At most one unexpired local claim; expired claims may be retried."""
        self._validate_request_id(request_id)
        self._validate_operator(operator)
        now = time.time() if now is None else now
        self._validate_now(now)
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("Invalid lease")
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = self._card(db, request_id)
                digest = self._receipt_sha256(row)
                self._ensure_table(db)
                current = db.execute(
                    "SELECT state, lease_until FROM business_approval_receipts "
                    "WHERE request_id=? AND profile=?",
                    (request_id, self.ledger.profile),
                ).fetchone()
                if current and current["state"] == "acked":
                    raise ValueError("Already acknowledged")
                if current and current["lease_until"] > now:
                    raise ValueError("Another unexpired claim exists")
                token = uuid.uuid4().hex
                fields = (self.ledger.profile, operator, now, now + lease_seconds, digest)
                if current:
                    db.execute(
                        "UPDATE business_approval_receipts SET state='claimed', "
                        "claim_token=?, claimed_by=?, claimed_at=?, lease_until=?, "
                        "receipt_sha256=?, acked_at=NULL, acked_by=NULL, evidence_ref=NULL "
                        "WHERE request_id=? AND profile=?",
                        (token, operator, now, now + lease_seconds, digest,
                         request_id, self.ledger.profile),
                    )
                else:
                    db.execute(
                        "INSERT INTO business_approval_receipts "
                        "(request_id, profile, state, claim_token, claimed_by, "
                        "claimed_at, lease_until, receipt_sha256) "
                        "VALUES (?, ?, 'claimed', ?, ?, ?, ?, ?)",
                        (request_id, self.ledger.profile, token, operator,
                         now, now + lease_seconds, digest),
                    )
        return {
            "request_id": request_id,
            "status": "claimed",
            "claim_token": token,
            "lease_until": now + lease_seconds,
        }

    def ack(
        self,
        request_id: str,
        *,
        operator: str,
        claim_token: str,
        evidence_ref: str,
        now: float | None = None,
    ) -> dict:
        """Acknowledgement is explicit after handling; no auto execution."""
        self._validate_request_id(request_id)
        self._validate_operator(operator)
        if not isinstance(claim_token, str) or not re.fullmatch(r"[0-9a-f]{32}", claim_token):
            raise ValueError("Invalid claim token")
        if not isinstance(evidence_ref, str) or not _EVIDENCE.fullmatch(evidence_ref):
            raise ValueError("An opaque evidence reference is required")
        now = time.time() if now is None else now
        self._validate_now(now)
        with closing(self.ledger.connect()) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = self._card(db, request_id)
                digest = self._receipt_sha256(row)
                if not self._has_table(db):
                    raise ValueError("No claim exists")
                current = db.execute(
                    "SELECT * FROM business_approval_receipts "
                    "WHERE request_id=? AND profile=?",
                    (request_id, self.ledger.profile),
                ).fetchone()
                if current is None or current["claim_token"] != claim_token:
                    raise ValueError("Unknown or superseded claim")
                if current["receipt_sha256"] != digest:
                    raise ValueError("Receipt changed after claim")
                if current["state"] == "acked":
                    if current["acked_by"] != operator or current["evidence_ref"] != evidence_ref:
                        raise ValueError("Acknowledgement mismatch")
                    return {"request_id": request_id, "status": "acked", "duplicate": True}
                if current["claimed_by"] != operator or current["lease_until"] <= now:
                    raise ValueError("Claim owner or lease invalid")
                updated = db.execute(
                    "UPDATE business_approval_receipts "
                    "SET state='acked', acked_at=?, acked_by=?, evidence_ref=? "
                    "WHERE request_id=? AND profile=? AND state='claimed' "
                    "AND claim_token=? AND lease_until>?",
                    (now, operator, evidence_ref, request_id,
                     self.ledger.profile, claim_token, now),
                )
                if updated.rowcount != 1:
                    raise ValueError("Claim was not acknowledged")
        return {"request_id": request_id, "status": "acked", "duplicate": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--profile", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=100)
    claiming = sub.add_parser("claim")
    claiming.add_argument("--request-id", required=True)
    claiming.add_argument("--operator", required=True)
    claiming.add_argument("--lease-seconds", type=int, default=600)
    acknowledging = sub.add_parser("ack")
    acknowledging.add_argument("--request-id", required=True)
    acknowledging.add_argument("--operator", required=True)
    acknowledging.add_argument("--claim-token", required=True)
    acknowledging.add_argument("--evidence-ref", required=True)
    args = parser.parse_args(argv)
    receiver = ReceiptReceiver(args.db, args.outbox, profile=args.profile)
    try:
        if args.command == "list":
            result = receiver.list_status(limit=args.limit)
        elif args.command == "claim":
            result = receiver.claim(
                args.request_id, operator=args.operator, lease_seconds=args.lease_seconds
            )
        else:
            result = receiver.ack(
                args.request_id, operator=args.operator,
                claim_token=args.claim_token, evidence_ref=args.evidence_ref,
            )
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, "Result hand-off rejected: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
