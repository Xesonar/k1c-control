import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from storage import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def restart(self):
        self.store.close()
        self.store = Store(self.path)

    def test_values_are_independent_and_survive_restart(self):
        original = {"queue": ["body.gcode"]}
        self.store.set("printer", original)
        original["queue"].append("not-saved.gcode")
        read = self.store.get("printer")
        read["queue"].clear()
        self.restart()
        self.assertEqual(self.store.get("printer"), {"queue": ["body.gcode"]})

    def test_mutation_is_atomic_and_callback_failure_rolls_back(self):
        self.store.set("queue", ["first.gcode"])
        result = self.store.mutate("queue", lambda value: value + ["second.gcode"])
        result.clear()

        def failing(value):
            value.append("not-saved.gcode")
            raise RuntimeError("cancel mutation")

        with self.assertRaises(RuntimeError):
            self.store.mutate("queue", failing)
        self.restart()
        self.assertEqual(self.store.get("queue"), ["first.gcode", "second.gcode"])

    def test_update_and_physical_action_cannot_be_reclaimed_after_restart(self):
        self.assertTrue(self.store.consume_update(101))
        self.assertEqual(self.store.get("offset"), 102)
        self.assertTrue(self.store.claim_action("telegram:101:pause"))
        self.restart()
        self.assertFalse(self.store.consume_update(101))
        self.assertFalse(self.store.claim_action("telegram:101:pause"))
        self.assertTrue(self.store.consume_update(102))
        self.assertTrue(self.store.claim_action("telegram:102:pause"))
        self.assertEqual(self.store.get("offset"), 103)

    def test_old_duplicate_does_not_rewind_offset(self):
        self.store.consume_update(101)
        self.store.consume_update(102)
        self.assertFalse(self.store.consume_update(101))
        self.assertEqual(self.store.get("offset"), 103)

    def test_outbox_retries_only_undelivered_recipients_after_restart(self):
        before = time.time()
        markup = {"inline_keyboard": [[{"text": "Пауза", "callback_data": "alarm:1"}]]}
        self.store.enqueue("job:77:complete", [11, 22, 11], "Готово", b"\xff\xd8jpeg", markup)
        self.store.enqueue("job:77:complete", [11, 22], "Must not replace original")
        rows = self.store.pending(now=before + 1)
        self.assertEqual([row["recipient"] for row in rows], [11, 22])
        self.store.delivered(rows[0]["id"])
        self.store.retry(rows[1]["id"], delay=60)
        self.assertEqual(self.store.pending(now=before + 1), [])
        self.restart()
        due = self.store.pending(now=before + 120)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["recipient"], 22)
        self.assertEqual(due[0]["text"], "Готово")
        self.assertEqual(due[0]["photo"], b"\xff\xd8jpeg")
        self.assertEqual(due[0]["markup"], markup)
        self.assertEqual(due[0]["attempts"], 1)
        self.store.delivered(due[0]["id"])
        self.store.enqueue("job:77:complete", [11, 22], "Must not send again")
        self.assertEqual(self.store.pending(now=before + 120), [])

    def test_audit_is_persistent_and_newest_first(self):
        self.store.audit(11, "pause", "claimed", "alarm 3")
        self.store.audit(22, "resume", "rejected", "print not paused")
        self.restart()
        rows = self.store.recent_audit(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], 22)
        self.assertEqual(rows[0]["action"], "resume")
        self.assertEqual(rows[0]["result"], "rejected")
        self.assertEqual(rows[0]["detail"], "print not paused")

    def test_concurrent_connections_claim_physical_action_only_once(self):
        other = Store(self.path)
        try:
            stores = [self.store, other] * 8
            with ThreadPoolExecutor(max_workers=8) as pool:
                claimed = list(pool.map(lambda store: store.claim_action("alarm:3:pause"), stores))
            self.assertEqual(sum(claimed), 1)
        finally:
            other.close()

    def test_callback_failure_rolls_back_nested_notification_and_state(self):
        self.store.set("alarm", {"state": "waiting"})

        def fail_after_notification(value):
            self.store.enqueue("alarm:3", [11], "Check print")
            self.store.set("other-key", "not committed")
            raise RuntimeError("abort all changes")

        with self.assertRaises(RuntimeError):
            self.store.mutate("alarm", fail_after_notification)
        self.assertEqual(self.store.pending(now=time.time() + 1), [])
        self.assertIsNone(self.store.get("other-key"))
        self.assertEqual(self.store.get("alarm"), {"state": "waiting"})


if __name__ == "__main__":
    unittest.main()
