import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alarm import AlarmController
from storage import Store


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.clock = Clock()
        self.alarm = AlarmController(self.store, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def suspicious_frames(self, job="job:1", count=3):
        created = None
        for _ in range(count):
            created = self.alarm.observe(job, "body.gcode", True, 0.9, photo=b"jpeg")
        return created

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.alarm = AlarmController(self.store, clock=self.clock)

    def test_three_consecutive_frames_create_one_five_minute_alarm(self):
        self.assertIsNone(self.suspicious_frames(count=2))
        self.alarm.observe("job:1", "body.gcode", False, 0.1)
        self.assertIsNone(self.suspicious_frames(count=2))
        alert = self.suspicious_frames(count=1)
        self.assertEqual(alert["deadline"], self.clock.now + 300)
        self.assertEqual(alert["status"], "pending")
        self.assertNotIn("photo", alert)
        self.assertIsNone(self.suspicious_frames(count=5))
        self.assertIsNone(self.alarm.tick("job:1", "printing"))
        self.clock.advance(299)
        self.assertIsNone(self.alarm.tick("job:1", "printing"))
        self.clock.advance(1)
        due = self.alarm.tick("job:1", "printing")
        self.assertEqual(due["id"], alert["id"])
        self.assertEqual(due["status"], "pause_claimed")
        self.assertIsNone(self.alarm.tick("job:1", "printing"))

    def test_first_owner_continue_wins_and_mutes_for_ten_minutes(self):
        alert = self.suspicious_frames()
        response = self.alarm.decide(alert["id"], 11, "continue")
        self.assertTrue(response["accepted"])
        self.assertFalse(response["late"])
        self.assertEqual(response["alert"]["actor"], 11)
        rejected = self.alarm.decide(alert["id"], 22, "pause")
        self.assertFalse(rejected["accepted"])
        self.clock.advance(599)
        self.assertIsNone(self.suspicious_frames(count=5))
        self.restart()
        self.clock.advance(1)
        self.assertIsNone(self.suspicious_frames(count=2))
        again = self.suspicious_frames(count=1)
        self.assertIsNotNone(again)
        self.assertNotEqual(again["id"], alert["id"])

    def test_owner_pause_claims_action_without_waiting_for_deadline(self):
        alert = self.suspicious_frames()
        response = self.alarm.decide(alert["id"], 11, "pause")
        self.assertTrue(response["accepted"])
        self.assertFalse(response["late"])
        self.assertEqual(response["alert"]["status"], "pause_claimed")
        self.clock.advance(300)
        self.assertIsNone(self.alarm.tick("job:1", "printing"))

    def test_restart_preserves_deadline_but_never_reissues_claimed_pause(self):
        alert = self.suspicious_frames()
        self.clock.advance(240)
        self.restart()
        self.assertEqual(self.alarm.current()["deadline"], alert["deadline"])
        self.clock.advance(60)
        self.assertEqual(self.alarm.tick("job:1", "printing")["id"], alert["id"])
        self.restart()
        self.assertIsNone(self.alarm.tick("job:1", "printing"))
        self.alarm.mark_autopause(alert["id"])
        self.assertEqual(self.alarm.current()["status"], "autopaused")
        self.assertIsNone(self.alarm.tick("job:1", "printing"))

    def test_late_continue_is_accepted_after_pause_without_reserving_resume(self):
        alert = self.suspicious_frames()
        self.clock.advance(300)
        self.alarm.tick("job:1", "printing")
        self.alarm.mark_autopause(alert["id"])
        response = self.alarm.decide(alert["id"], 11, "continue")
        self.assertTrue(response["accepted"])
        self.assertTrue(response["late"])
        self.assertEqual(response["alert"]["decision"], "continue")
        self.assertEqual(response["alert"]["status"], "dismissed")
        self.assertIsNone(self.alarm.tick("job:1", "paused"))

    def test_printer_already_paused_makes_owner_reply_late(self):
        alert = self.suspicious_frames()
        self.assertIsNone(self.alarm.tick("job:1", "paused"))
        self.assertEqual(self.alarm.current()["status"], "paused")
        result = self.alarm.decide(alert["id"], 11, "pause")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["late"])
        self.clock.advance(300)
        self.assertIsNone(self.alarm.tick("job:1", "printing"))

    def test_new_job_clears_old_count_mute_and_due_alarm(self):
        old = self.suspicious_frames()
        self.alarm.decide(old["id"], 11, "continue")
        self.assertIsNotNone(self.suspicious_frames(job="job:2"))
        self.clock.advance(300)
        self.assertIsNone(self.alarm.tick("job:3", "printing"))
        self.assertIsNone(self.alarm.current())
        self.assertFalse(self.alarm.decide(old["id"], 11, "pause")["accepted"])
        self.assertIsNone(self.suspicious_frames(job="job:3", count=2))
        self.assertIsNotNone(self.suspicious_frames(job="job:3", count=1))

    def test_finished_job_closes_an_unanswered_alarm(self):
        self.suspicious_frames()
        self.clock.advance(300)
        self.assertIsNone(self.alarm.tick("job:1", "complete"))
        self.assertIsNone(self.alarm.current())

    def test_two_owners_cannot_win_the_same_alarm(self):
        alert = self.suspicious_frames()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda actor: self.alarm.decide(alert["id"], actor, "continue"), [11, 22]))
        self.assertEqual(sum(result["accepted"] for result in results), 1)
        self.assertIn(self.alarm.current()["actor"], [11, 22])

    def test_uncertain_pause_outcome_does_not_issue_a_retry(self):
        alert = self.suspicious_frames()
        self.clock.advance(300)
        self.alarm.tick("job:1", "printing")
        self.alarm.mark_autopause(alert["id"], "uncertain")
        self.restart()
        self.assertEqual(self.alarm.current()["status"], "uncertain")
        self.assertIsNone(self.alarm.tick("job:1", "printing"))


if __name__ == "__main__":
    unittest.main()
