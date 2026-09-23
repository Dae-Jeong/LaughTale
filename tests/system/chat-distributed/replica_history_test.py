import asyncio
import importlib.util
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

spec = importlib.util.spec_from_file_location(
    "replica_history", Path(__file__).with_name("replica_history.py")
)
reader = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reader
spec.loader.exec_module(reader)


def snapshot():
    return reader.Snapshot(
        str(uuid4()), str(uuid4()), 0, 3, "0/200", "12345", 1, "2026-09-08T00:00:00Z"
    )


class Connection:
    def __init__(self, *, replay="0/200", head=3, recovery=True):
        self.replay, self.head, self.recovery = replay, head, recovery
        self.fetch_calls = self.observations = 0
        self.rows = [
            {"message_id": str(uuid4()), "seq": value, "text_sha256": "a" * 64}
            for value in range(1, 4)
        ]

    async def fetchval(self, *args):
        return self.head

    async def fetchrow(self, *args):
        self.observations += 1
        return {
            "in_recovery": self.recovery,
            "role": "chat_reader",
            "replay_lsn": self.replay,
        }

    async def fetch(self, sql, room, after, head, limit):
        self.fetch_calls += 1
        return [row for row in self.rows if after < row["seq"] <= head][:limit]

    @asynccontextmanager
    async def transaction(self, **kwargs):
        assert kwargs == {"isolation": "read_committed", "readonly": True}
        yield


class ReplicaTests(unittest.TestCase):
    def test_numeric_lsn_and_identity_gate(self):
        snap = snapshot()
        observed = {
            "system_identifier": "12345",
            "timeline": 1,
            "role": "chat_reader",
            "in_recovery": True,
            "replay_lsn": "0/200",
        }
        self.assertEqual(reader.gate(snap, observed), "eligible")
        self.assertEqual(
            reader.gate(snap, observed | {"replay_lsn": "0/FF"}), "replay_behind"
        )
        self.assertEqual(
            reader.gate(snap, observed | {"replay_lsn": None}), "replay_unknown"
        )
        self.assertEqual(
            reader.gate(snap, observed | {"system_identifier": "other"}),
            "identity_mismatch",
        )
        self.assertEqual(
            reader.gate(snap, observed | {"timeline": 2}), "identity_mismatch"
        )
        self.assertEqual(
            reader.gate(snap, observed | {"in_recovery": False}),
            "role_or_recovery_mismatch",
        )

    def test_lag_never_queries_replica_and_exhausted_budget_fails(self):
        async def scenario():
            primary, replica = Connection(), Connection(replay="0/FF")
            router = reader.SafeHistory(
                primary,
                replica,
                {"system_identifier": "12345", "timeline": 1},
                fallback_limit=1,
            )
            result = await router.page(snapshot(), 0)
            self.assertEqual(result["route"], "primary_fallback")
            self.assertEqual(replica.fetch_calls, 0)
            self.assertEqual(primary.fetch_calls, 1)
            with self.assertRaises(reader.ReadUnavailable):
                await router.page(snapshot(), 0)
            self.assertEqual(primary.fetch_calls, 1)

        asyncio.run(scenario())

    def test_caught_up_gate_uses_new_read_transaction_and_fixed_cutoff(self):
        async def scenario():
            primary, replica = Connection(), Connection()
            router = reader.SafeHistory(
                primary, replica, {"system_identifier": "12345", "timeline": 1}
            )
            result = await router.page(snapshot(), 0, 2)
            self.assertEqual(result["route"], "replica")
            self.assertEqual(result["next_cursor"], 2)
            self.assertEqual(result["snapshot_head_seq"], 3)
            self.assertEqual(primary.fetch_calls, 0)

        asyncio.run(scenario())

    def test_primary_permission_revoke_prevents_any_replica_access(self):
        async def scenario():
            primary, replica = Connection(head=None), Connection()
            router = reader.SafeHistory(
                primary, replica, {"system_identifier": "12345", "timeline": 1}
            )
            with self.assertRaises(reader.ReadUnavailable):
                await router.page(snapshot(), 0)
            self.assertEqual(replica.observations, 0)
            self.assertEqual(replica.fetch_calls, 0)

        asyncio.run(scenario())

    def test_missing_replica_rows_are_not_returned_as_success(self):
        async def scenario():
            primary, replica = Connection(), Connection()
            replica.rows = replica.rows[1:]
            router = reader.SafeHistory(
                primary, replica, {"system_identifier": "12345", "timeline": 1}
            )
            result = await router.page(snapshot(), 0)
            self.assertEqual(result["reason"], "replica_read_failed")
            self.assertEqual(result["rows"], primary.rows)

        asyncio.run(scenario())

    def test_missing_connection_can_fallback_without_replica_query(self):
        async def scenario():
            router = reader.SafeHistory(Connection(), None, {})
            result = await router.page(snapshot(), 0)
            self.assertEqual(result["reason"], "replay_observation_failed")
            self.assertEqual(result["route"], "primary_fallback")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
