from __future__ import annotations

import os
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nsebot import logstore, rules
from nsebot.dynamodb import DynamoDBStore
from nsebot.migrate import migrate


class _MemoryStore(DynamoDBStore):
    def __init__(self):
        self.items = []
        self.updates = []
        self.index_rows = []
        self.counter = 0

    def next_id(self, name):
        self.counter += 1
        return self.counter

    def put(self, pk, sk, values):
        self.items.append((pk, sk, values))

    def query_index(self, index_name, pk):
        return self.index_rows

    def update(self, pk, sk, values):
        self.updates.append((pk, sk, values))

    def ensure_counter_at_least(self, name, value):
        self.counter = max(self.counter, value)


class _PaginatedTable:
    def __init__(self):
        self.calls = []

    def query(self, **request):
        self.calls.append(request)
        if "ExclusiveStartKey" not in request:
            return {
                "Items": [{"pk": "COMMANDS", "sk": "1", "gsi1pk": "pending", "id": 1}],
                "LastEvaluatedKey": {"pk": "COMMANDS", "sk": "1"},
            }
        return {"Items": [{"pk": "COMMANDS", "sk": "2", "id": 2}]}


class _WriteTable:
    def __init__(self):
        self.put_request = None
        self.update_request = None

    def put_item(self, **request):
        self.put_request = request

    def update_item(self, **request):
        self.update_request = request


class DynamoDBStoreTests(unittest.TestCase):
    def test_backend_selection_uses_dynamodb_without_sqlite_initialization(self):
        with TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"DATABASE_BACKEND": "dynamodb"}), \
                patch("nsebot.logstore.DynamoDBStore") as store_factory, \
                patch("nsebot.logstore.initialize") as initialize:
            result = logstore.connect(Path(temporary))

        self.assertIs(result, store_factory.return_value)
        store_factory.assert_called_once_with()
        initialize.assert_not_called()

    def test_sqlite_remains_available_for_local_usage(self):
        with TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite"}):
            connection = logstore.connect(Path(temporary))
            try:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            finally:
                connection.close()

    def test_dynamodb_query_paginates_and_removes_internal_keys(self):
        store = DynamoDBStore.__new__(DynamoDBStore)
        store._table = _PaginatedTable()

        rows = store._query("condition", index_name="gsi1")

        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertNotIn("pk", rows[0])
        self.assertNotIn("gsi1pk", rows[0])
        self.assertEqual(store._table.calls[0]["IndexName"], "gsi1")
        self.assertNotIn("ConsistentRead", store._table.calls[0])
        self.assertIn("ExclusiveStartKey", store._table.calls[1])

    def test_writes_normalize_floats_and_guard_insert_and_update_keys(self):
        store = DynamoDBStore.__new__(DynamoDBStore)
        store._table = _WriteTable()

        store.put("TRADES", "DAY#1", {"price": 1.25}, if_absent=True)
        store.update("TRADES", "DAY#1", {"price": 2.5})

        self.assertEqual(store._table.put_request["Item"]["price"], Decimal("1.25"))
        self.assertEqual(
            store._table.put_request["ConditionExpression"],
            "attribute_not_exists(pk)",
        )
        self.assertEqual(
            store._table.update_request["ExpressionAttributeValues"][":value0"],
            Decimal("2.5"),
        )
        self.assertEqual(
            store._table.update_request["ConditionExpression"],
            "attribute_exists(pk)",
        )

    def test_pending_commands_use_the_gsi_and_updates_target_the_key(self):
        store = _MemoryStore()
        store.index_rows = [{"id": 12, "name": "ARM"}]

        pending = logstore.get_pending_command(store)
        logstore.update_command(store, 12, "complete", "armed")

        self.assertEqual(pending["id"], 12)
        self.assertEqual(
            store.updates,
            [("COMMANDS", "COMMAND#00000000000000000012", {
                "status": "complete",
                "result_text": "armed",
                "gsi1pk": "COMMAND_STATUS#processed",
            })],
        )

    def test_migration_adds_index_keys_and_preserves_record_ids(self):
        store = _MemoryStore()

        logstore.import_sqlite_record(store, "rules_versions", {
            "id": 9, "created_at": "now", "yaml": "rules", "note": "n", "status": "scheduled",
        })
        logstore.import_sqlite_record(store, "commands", {
            "id": 13, "ts": "now", "name": "ARM", "payload_json": "{}",
            "status": "pending", "result_text": None,
        })

        self.assertEqual(store.items[0][0:2], ("RULES", "VERSION#00000000000000000009"))
        self.assertEqual(store.items[0][2]["gsi1pk"], "RULE_STATUS#scheduled")
        self.assertEqual(store.items[1][0:2], ("COMMANDS", "COMMAND#00000000000000000013"))
        self.assertEqual(store.items[1][2]["gsi1pk"], "COMMAND_STATUS#pending")
        self.assertEqual(store.counter, 13)

    def test_sqlite_migration_copies_rows_and_advances_counters(self):
        with TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "algo.db"
            source = logstore.connect(Path(temporary))
            logstore.insert_event(source, {
                "day": "2026-11-26",
                "ts": "2026-11-26T09:15:00+05:30",
                "state_from": "IDLE",
                "state_to": "BLOCKED",
                "reason": "holiday",
                "reason_text": "Holiday",
                "snapshot_json": "{}",
            })
            source.close()
            store = _MemoryStore()

            with patch.dict(os.environ, {"DATABASE_BACKEND": "dynamodb"}), \
                    patch("nsebot.migrate.logstore.connect", return_value=store):
                counts = migrate(source_path, Path(temporary))

        self.assertEqual(counts["events"], 1)
        self.assertEqual(store.items[0][0:2], ("EVENTS#2026-11-26", "EVENT#00000000000000000001"))
        self.assertEqual(store.counter, 1)

    def test_rule_document_loader_accepts_dynamodb_yaml_string(self):
        yaml_text = rules.DEFAULT_RULES_PATH.read_text(encoding="utf-8")

        document = rules._document_from_row(yaml_text)

        self.assertIsInstance(document, rules.Rules)


if __name__ == "__main__":
    unittest.main()
