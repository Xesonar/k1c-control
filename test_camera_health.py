"""Regression feedback loop for camera availability notification flapping.

Exercises the real Manager.tick -> camera sampling -> durable outbox -> delivery
path with a stable printing job. All I/O uses existing test doubles; no network
or physical printer command is involved.
"""
import tempfile
import unittest
from pathlib import Path

from adapters import ApiError
from manager import Manager
from storage import Store
from test_manager import Printer, Telegram


class QuietDetector:
    def __init__(self):
        self.frames = []

    def analyze(self, photo):
        self.frames.append(photo)
        return {"detections": []}


class CameraHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.addCleanup(self.store.close)
        self.printer = Printer()
        self.telegram = Telegram()
        self.now = 1000.0
        self.camera_available = True
        self.photo_samples = 0

        def photo():
            self.photo_samples += 1
            if not self.camera_available:
                raise ApiError("camera_offline")
            return b"\xff\xd8camera-frame\xff\xd9"

        self.printer.photo = photo
        self.config = dict({'owner_id':10001}, **{"allowed_user_ids": [10001], "state_dir": self.temp.name,
                       "analysis_interval": 10, "detector_auto_enabled": False})
        self.detector = QuietDetector()
        self.manager = Manager(self.config, self.store, self.printer, self.telegram,
                               detector=self.detector, clock=lambda: self.now)
        self.addCleanup(self.manager.close_fast_ui)

    def sample(self, available, advance=10, flush=True):
        # Ten seconds expires the real 2s frame cache and reaches each analysis.
        self.now += advance
        self.camera_available = available
        self.manager.tick()
        if flush:
            self.manager.flush_outbox()

    def repeat(self, available, count=3, **kwargs):
        for _ in range(count):
            self.sample(available, **kwargs)

    def restart(self):
        self.manager.close_fast_ui()
        self.manager = Manager(self.config, self.store, self.printer, self.telegram,
                               detector=self.detector, clock=lambda: self.now)
        self.addCleanup(self.manager.close_fast_ui)

    def tearDown(self):
        self.assertEqual(self.printer.calls, [])

    def camera_notifications(self):
        return [text for _, text, _ in self.telegram.messages
                if "Камера недоступна" in text or "Камера снова доступна" in text]

    def test_one_failed_frame_between_successes_does_not_announce_outage_and_recovery(self):
        # Minimized user symptom: healthy frame, one failed frame, healthy frame.
        for available in (True, False, True):
            self.sample(available)
        self.assertEqual(self.photo_samples, 3)
        self.assertEqual(self.printer.calls, [])
        self.assertEqual(self.camera_notifications(), [],
                         "An isolated failed sample announced an outage and recovery")

    def test_alternating_single_frame_failures_do_not_create_notification_storm(self):
        for _ in range(3):
            self.sample(True)
        # A stable job with isolated failures: never a sustained outage/recovery.
        for _ in range(8):
            self.sample(False)
            self.sample(True)
        notifications = self.camera_notifications()
        self.assertEqual(self.photo_samples, 19)
        self.assertEqual(self.printer.calls, [])
        self.assertEqual(
            len(notifications), 0,
            f"Single-frame flapping generated {len(notifications)} camera notices "
            "during one uninterrupted printing job",
        )


    def test_sustained_loss_and_recovery_each_emit_once(self):
        self.repeat(True)
        self.repeat(False, count=2)
        self.assertEqual(self.camera_notifications(), [])
        self.sample(False)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.assertIn("Камера недоступна", self.camera_notifications()[0])
        self.now += 300
        self.repeat(True, count=2)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.sample(True)
        self.repeat(True)
        self.assertEqual(len(self.camera_notifications()), 2)
        self.assertIn("Камера снова доступна", self.camera_notifications()[1])

    def test_three_samples_alone_do_not_skip_twenty_second_debounce(self):
        self.config["analysis_interval"] = 0
        self.repeat(False, advance=1)
        self.assertEqual(self.camera_notifications(), [])
        self.sample(False, advance=18)
        self.assertEqual(len(self.camera_notifications()), 1)

    def test_one_good_frame_does_not_recover_confirmed_outage(self):
        self.repeat(False)
        self.now += 300
        self.sample(True)
        self.repeat(False)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.assertIn("Камера недоступна", self.camera_notifications()[0])

    def test_cooldown_coalesces_intermediate_recoveries(self):
        self.repeat(False)
        self.repeat(True)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.repeat(False)
        self.now += 300
        self.repeat(False)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.repeat(True)
        self.assertEqual(len(self.camera_notifications()), 2)
        self.assertIn("Камера снова доступна", self.camera_notifications()[-1])

    def test_restart_preserves_confirmed_health_and_notification_cooldown(self):
        self.repeat(False)
        self.restart()
        self.repeat(False)
        self.repeat(True)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.now += 300
        self.sample(True)
        self.assertEqual(len(self.camera_notifications()), 2)

    def test_restart_preserves_a_recent_candidate_streak(self):
        self.repeat(False, count=2)
        self.assertEqual(self.camera_notifications(), [])
        self.restart()
        self.sample(False)
        self.assertEqual(len(self.camera_notifications()), 1)

    def test_hours_between_attempts_reset_candidate_streak(self):
        self.repeat(False, count=2)
        self.now += 3600
        self.sample(False)
        self.assertEqual(self.camera_notifications(), [])
        self.repeat(False, count=2)
        self.assertEqual(len(self.camera_notifications()), 1)

    def test_new_job_resets_candidate_streak(self):
        self.repeat(False, count=2)
        self.printer.job_id = "replacement-job"
        self.sample(False)
        self.assertEqual(self.camera_notifications(), [])
        self.repeat(False, count=2)
        self.assertEqual(len(self.camera_notifications()), 1)

    def test_queued_outage_that_recovered_before_delivery_is_discarded(self):
        self.repeat(True)
        self.repeat(False, flush=False)
        self.assertTrue(self.store.pending())
        self.now += 300
        self.repeat(True, flush=False)
        self.manager.flush_outbox()
        self.assertEqual(self.camera_notifications(), [])
        self.assertFalse(self.store.pending())

    def test_only_latest_confirmed_camera_event_is_delivered_from_backlog(self):
        self.repeat(True)
        self.repeat(False, flush=False)
        self.now += 300
        self.repeat(True, flush=False)
        self.now += 300
        self.repeat(False, flush=False)
        self.manager.flush_outbox()
        self.assertEqual(len(self.camera_notifications()), 1)
        self.assertIn("Камера недоступна", self.camera_notifications()[0])

    def test_initial_undelivered_outage_does_not_create_a_recovery_notice(self):
        self.repeat(False, flush=False)
        self.assertTrue(self.store.pending())
        self.now += 300
        self.repeat(True, flush=False)
        self.manager.flush_outbox()
        self.assertEqual(self.camera_notifications(), [])
        self.assertFalse(self.store.pending())

    def test_cooldown_starts_after_delayed_actual_delivery(self):
        self.repeat(True)
        self.repeat(False, flush=False)
        self.now += 600
        self.manager.flush_outbox()
        self.assertEqual(len(self.camera_notifications()), 1)
        self.repeat(True)
        self.assertEqual(len(self.camera_notifications()), 1)
        self.now += 300
        self.sample(True)
        self.assertEqual(len(self.camera_notifications()), 2)

    def test_detector_receives_real_frames_before_health_debounce_finishes(self):
        self.sample(True)
        self.assertEqual(len(self.detector.frames), 1)
        self.sample(False)
        self.assertEqual(len(self.detector.frames), 1)
        self.sample(True)
        self.assertEqual(len(self.detector.frames), 2)
        self.assertEqual(self.photo_samples, 3)
        self.assertEqual(self.camera_notifications(), [])


if __name__ == "__main__":
    unittest.main()
