"""Receipt consumer tests: no Slack posting, no exec approval, no real DB writes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from unittest.mock import Mock

import pytest

from plugins.platforms.slack.business_approval import (
    APPROVER,
    CHANNEL,
    BusinessApproval,
    Ledger,
    initialize_database,
)
from plugins.platforms.slack.business_approval_receiver import ReceiptReceiver


@pytest.fixture
def delivered(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "business.sqlite"
    initialize_database(str(path))
    ledger = Ledger(str(path), profile="koumei", verifier=lambda evidence: True)
    ledger.register(
        hd="HD-TEST-1", team="T1", channel=CHANNEL,
        message_ts="100.1", thread_ts="99.1",
        issued=1000, requester="operator", now=1000,
        title="受領確認の検証", outcome="結果ファイルが受領される",
        scope="業務への影響なし",
    )
    row = ledger.decide(
        team="T1", channel=CHANNEL, message_ts="100.1",
        user=APPROVER, choice="approve", now=1001,
    )
    handler = BusinessApproval(
        Mock(), str(path), str(tmp_path / "outbox"), profile="koumei"
    )
    assert handler.deliver(row) is True
    row = ledger.results()[0]
    assert row["outbox_written_at"] is not None
    receiver = ReceiptReceiver(str(path), str(handler.outbox), profile="koumei")
    receipt_path = handler.receipt_path(row)
    return receiver, row["request_id"], receipt_path, path


def test_explicit_claim_and_ack_are_durable_and_idempotent(delivered):
    receiver, request_id, _, db = delivered
    assert receiver.list_status(now=1002)[0]["status"] == "ready"
    first = receiver.claim(
        request_id, operator="joushou", now=1002, lease_seconds=60
    )
    assert first["status"] == "claimed"
    assert receiver.list_status(now=1003)[0]["status"] == "claimed"
    result = receiver.ack(
        request_id, operator="joushou", claim_token=first["claim_token"],
        evidence_ref="HD-TEST-1/handled", now=1004,
    )
    assert result == {"request_id": request_id, "status": "acked", "duplicate": False}
    assert receiver.ack(
        request_id, operator="joushou", claim_token=first["claim_token"],
        evidence_ref="HD-TEST-1/handled", now=1005,
    )["duplicate"] is True
    restarted = ReceiptReceiver(str(db), str(receiver.outbox), profile="koumei")
    assert restarted.list_status(now=2000)[0]["status"] == "acked"
    with pytest.raises(ValueError, match="Already acknowledged"):
        restarted.claim(request_id, operator="joushou", now=2000)


def test_concurrent_claim_allows_only_one_owner(delivered):
    receiver, request_id, _, _ = delivered

    def try_claim(_):
        try:
            return receiver.claim(request_id, operator="joushou", now=1002)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(try_claim, range(2)))
    assert sum(r is not None for r in results) == 1
    assert receiver.list_status(now=1003)[0]["status"] == "claimed"


def test_expired_lease_recovery_rejects_stale_token(delivered):
    receiver, request_id, _, _ = delivered
    stale = receiver.claim(
        request_id, operator="joushou", now=1002, lease_seconds=10
    )
    with pytest.raises(ValueError, match="unexpired"):
        receiver.claim(request_id, operator="joushou", now=1003)
    assert receiver.list_status(now=1012)[0]["status"] == "ready"
    current = receiver.claim(
        request_id, operator="joushou", now=1012, lease_seconds=10
    )
    assert current["claim_token"] != stale["claim_token"]
    with pytest.raises(ValueError, match="superseded"):
        receiver.ack(
            request_id, operator="joushou", claim_token=stale["claim_token"],
            evidence_ref="HD-TEST-1/handled", now=1013,
        )
    assert receiver.ack(
        request_id, operator="joushou", claim_token=current["claim_token"],
        evidence_ref="HD-TEST-1/handled", now=1014,
    )["status"] == "acked"


@pytest.mark.parametrize("variant", ["tampered", "missing", "bad_mode", "symlink"])
def test_invalid_receipt_never_claimed_or_acknowledged(delivered, variant):
    receiver, request_id, receipt, db = delivered
    if variant == "tampered":
        receipt.write_text("changed decision\n")
    elif variant == "missing":
        receipt.unlink()
    elif variant == "bad_mode":
        receipt.chmod(0o644)
    else:
        original = receipt.read_bytes()
        receipt.unlink()
        other = receipt.parent / "another.md"
        other.write_bytes(original)
        other.chmod(0o600)
        receipt.symlink_to(other)
    assert receiver.list_status(now=1002)[0]["status"] == "receipt_invalid"
    with pytest.raises((ValueError, OSError)):
        receiver.claim(request_id, operator="joushou", now=1002)
    with sqlite3.connect(db) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='business_approval_receipts'"
        ).fetchone()
    assert exists is None


def test_profile_and_delivery_boundary(delivered):
    receiver, request_id, _, db = delivered
    other = ReceiptReceiver(str(db), str(receiver.outbox), profile="different")
    assert other.list_status() == []
    with pytest.raises(ValueError, match="No delivered decision"):
        other.claim(request_id, operator="joushou", now=1002)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE cards SET outbox_written_at=NULL WHERE request_id=?",
            (request_id,),
        )
    assert receiver.list_status(now=1002)[0]["status"] == "receipt_pending"
    with pytest.raises(ValueError, match="No delivered decision"):
        receiver.claim(request_id, operator="joushou", now=1002)


def test_ack_requires_matching_claim_owner_evidence_and_lease(delivered):
    receiver, request_id, _, _ = delivered
    claim = receiver.claim(request_id, operator="joushou", now=1002, lease_seconds=10)
    token = claim["claim_token"]
    with pytest.raises(ValueError, match="operator"):
        receiver.ack(
            request_id, operator="unknown person", claim_token=token,
            evidence_ref="record/123", now=1003,
        )
    with pytest.raises(ValueError, match="owner"):
        receiver.ack(
            request_id, operator="other", claim_token=token,
            evidence_ref="record/123", now=1003,
        )
    with pytest.raises(ValueError, match="evidence"):
        receiver.ack(
            request_id, operator="joushou", claim_token=token,
            evidence_ref="https://example.com/?token=secret", now=1003,
        )
    with pytest.raises(ValueError, match="lease"):
        receiver.ack(
            request_id, operator="joushou", claim_token=token,
            evidence_ref="record/123", now=1012,
        )
    assert receiver.list_status(now=1013)[0]["status"] == "ready"


def test_no_database_creation_on_status_or_claim(tmp_path):
    tmp_path.chmod(0o700)
    db = tmp_path / "missing.sqlite"
    receiver = ReceiptReceiver(str(db), str(tmp_path / "outbox"), profile="koumei")
    with pytest.raises(OSError):
        receiver.list_status()
    with pytest.raises(OSError):
        receiver.claim("business-" + "a" * 32, operator="joushou")
    assert not db.exists()


def test_only_receipt_metadata_is_exposed(delivered):
    receiver, request_id, _, _ = delivered
    result = receiver.list_status()
    assert result == [{
        "request_id": request_id,
        "hd": "HD-TEST-1",
        "decision": "approve",
        "status": "ready",
    }]
    assert "requester" not in result[0]
    assert "actor" not in result[0]
