"""The hash chain, tested both offline and against a real PostgreSQL 18.

The offline tests pin the serialisation contract. The `db` tests prove the properties that only a
real server can demonstrate: that the chain links across inserts, that `verify_chain` names the
exact sequence where a tampered row breaks it, and that the application role cannot rewrite history.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
from ledger import GENESIS, ChainStatus, build_entry, canonical, compute_hash, verify_entries

AT = datetime(2026, 8, 22, 2, 14, 7, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# The serialisation contract. Treat these as frozen: any change here invalidates every historical
# hash, and the correct response would be to version the chain, not to update the test.
# ---------------------------------------------------------------------------------------------


def test_canonical_json_is_sorted_and_has_no_spaces() -> None:
    text_out = canonical({"b": 2, "a": 1, "c": {"z": 1, "y": 2}})
    assert text_out == '{"a":1,"b":2,"c":{"y":2,"z":1}}'


def test_canonical_json_is_order_independent() -> None:
    assert canonical({"a": 1, "b": 2}) == canonical({"b": 2, "a": 1})


def test_hash_depends_on_the_predecessor() -> None:
    entry = build_entry("plan", {"runbook": "crashloop_rollback"}, AT, "healops", 1, None)
    assert compute_hash(GENESIS, entry) != compute_hash("a" * 64, entry)


def test_hash_depends_on_the_payload() -> None:
    a = build_entry("plan", {"runbook": "crashloop_rollback"}, AT, "healops", 1, None)
    b = build_entry("plan", {"runbook": "cpu_scale_up"}, AT, "healops", 1, None)
    assert compute_hash(GENESIS, a) != compute_hash(GENESIS, b)


def test_seq_is_not_part_of_the_hash() -> None:
    # The hash must commit to CONTENT. Including the database-assigned sequence would make it
    # depend on insertion order, which is not what tamper-evidence is about.
    entry = build_entry("plan", {"x": 1}, AT, "healops", 1, None)
    assert "seq" not in canonical(entry)


def test_unknown_event_type_is_rejected() -> None:
    # A typo'd event type would otherwise enter the chain and be permanent.
    assert "plan" in ledger.EVENT_TYPES
    assert "definitely_not_an_event" not in ledger.EVENT_TYPES


def test_refusals_and_denials_are_auditable_event_types() -> None:
    # "The system correctly decided not to act" is part of the record, not an absence from it.
    assert {"policy", "shadow", "compensate", "promote", "freeze"} <= ledger.EVENT_TYPES


# ---------------------------------------------------------------------------------------------
# Offline verification -- what `healopsctl replay` and the CI ledger gate use.
# ---------------------------------------------------------------------------------------------


def _chain(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    prev = GENESIS
    for i, payload in enumerate(payloads):
        entry = build_entry("plan", payload, AT, "healops", 1, None)
        digest = compute_hash(prev, entry)
        entries.append(
            {
                "seq": i + 1,
                "event_type": "plan",
                "payload": payload,
                "actor": "healops",
                "incident_id": 1,
                "execution_id": None,
                "occurred_at": AT.isoformat(),
                "prev_hash": prev,
                "hash": digest,
            }
        )
        prev = digest
    return entries


def test_offline_verification_accepts_an_intact_chain() -> None:
    status = verify_entries(_chain([{"i": i} for i in range(10)]))
    assert status == ChainStatus(valid=True, entries=10)


def test_offline_verification_detects_an_edited_payload() -> None:
    entries = _chain([{"i": i} for i in range(10)])
    entries[4]["payload"] = {"i": "tampered"}

    status = verify_entries(entries)
    assert status.valid is False
    assert status.broken_at_seq == 5


def test_offline_verification_detects_a_removed_entry() -> None:
    entries = _chain([{"i": i} for i in range(10)])
    del entries[5]

    status = verify_entries(entries)
    assert status.valid is False


# ---------------------------------------------------------------------------------------------
# Against a real PostgreSQL 18.
# ---------------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.asyncio
async def test_append_links_entries_into_a_chain(db_session: AsyncSession) -> None:
    first = await ledger.append(db_session, "plan_requested", {"runbook": "crashloop_rollback"})
    second = await ledger.append(db_session, "plan", {"actions": []})
    third = await ledger.append(db_session, "policy", {"decision": "allow"})

    assert len({first, second, third}) == 3

    rows = (
        await db_session.execute(
            text(
                "SELECT prev_hash, hash FROM audit_ledger "
                "WHERE hash = ANY(:hashes) ORDER BY seq"
            ),
            {"hashes": [first, second, third]},
        )
    ).all()

    assert len(rows) == 3
    assert rows[1].prev_hash == first
    assert rows[2].prev_hash == second


@pytest.mark.db
@pytest.mark.asyncio
async def test_verify_chain_accepts_what_append_wrote(db_session: AsyncSession) -> None:
    for i in range(5):
        await ledger.append(db_session, "execute", {"step": i}, incident_id=1)
    await db_session.flush()

    status = await ledger.verify_chain(db_session)
    assert status.valid is True, status.detail
    assert status.entries >= 5


@pytest.mark.db
@pytest.mark.asyncio
async def test_verify_chain_names_the_sequence_where_tampering_happened(
    db_session: AsyncSession,
) -> None:
    """The demo moment: edit a row as the superuser, then watch the chain point at it.

    `healops_app` cannot do this at all -- UPDATE and DELETE are revoked. The test runs as the
    owner precisely to show that even a privileged edit is detectable.
    """
    hashes = [await ledger.append(db_session, "execute", {"step": i}) for i in range(5)]
    await db_session.flush()

    target = hashes[2]
    seq = (
        await db_session.execute(
            text("SELECT seq FROM audit_ledger WHERE hash = :h"), {"h": target}
        )
    ).scalar_one()

    await db_session.execute(
        text("UPDATE audit_ledger SET payload = :p WHERE hash = :h"),
        {"p": '{"step": "tampered"}', "h": target},
    )
    await db_session.flush()

    status = await ledger.verify_chain(db_session)
    assert status.valid is False
    assert status.broken_at_seq == seq
    assert "does not match its hash" in (status.detail or "")


@pytest.mark.db
@pytest.mark.asyncio
async def test_application_role_cannot_rewrite_history(db_session: AsyncSession) -> None:
    """An audit table the application can rewrite is not an audit table."""
    granted = (
        await db_session.execute(
            text(
                "SELECT privilege_type FROM information_schema.table_privileges "
                "WHERE grantee = 'healops_app' AND table_name = 'audit_ledger'"
            )
        )
    ).scalars()

    privileges = set(granted)
    assert privileges == {"SELECT", "INSERT"}, (
        f"healops_app must hold INSERT and SELECT only, found {sorted(privileges)}"
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_ledger_is_partitioned_by_month(db_session: AsyncSession) -> None:
    # An append-only table that only ever grows should be partitioned before it needs to be.
    partitions = (
        await db_session.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'audit_ledger'"
            )
        )
    ).scalars()

    names = sorted(partitions)
    assert len(names) >= 3, "current month plus lookahead partitions must exist"
    assert all(n.startswith("audit_ledger_") for n in names)


@pytest.mark.db
@pytest.mark.asyncio
async def test_missing_partition_would_fail_the_insert(db_session: AsyncSession) -> None:
    """A missing partition is an insert failure, and under fail-closed that means no remediation.

    That is the correct behaviour -- an action with no audit record must not happen -- but it is
    only safe because `ensure_ledger_partition` runs ahead of time. This test documents the
    consequence so nobody "fixes" it by making the ledger optional.
    """
    with pytest.raises(Exception, match="partition"):
        await db_session.execute(
            text(
                "INSERT INTO audit_ledger "
                "(occurred_at, actor, event_type, payload, prev_hash, hash) "
                "VALUES ('1999-01-01T00:00:00Z', 'healops', 'plan', '{}', :g, :h)"
            ),
            {"g": GENESIS, "h": "f" * 64},
        )
