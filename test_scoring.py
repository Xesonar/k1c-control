import tempfile
import unittest
from pathlib import Path

from scoring import Scorer
from storage import Store


def failures(*confidences):
    return [["failure", confidence, [100, 100, 20, 20]] for confidence in confidences]


class ScorerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.store = Store(self.path)
        self.now = 1000.0
        self.scorer = Scorer(self.store, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def frame(self, detections=(), job="job-a"):
        self.now += 10
        return self.scorer.classify(job, list(detections))

    def test_new_print_needs_thirty_valid_frames_before_warnings(self):
        for _ in range(29):
            result = self.frame(failures(0.9, 0.9))
            self.assertFalse(result["ready"])
            self.assertFalse(result["suspicious"])
        self.assertTrue(self.frame(failures(0.9, 0.9))["ready"])

    def test_worked_upstream_mean_example_through_public_result(self):
        for _ in range(30):
            self.frame()
        result = self.frame(failures(1.0))
        # EWMA=2/13, long mean=1/32; (2/13 - 1/32) / .38 / 3.
        # A literal worked example, independent of the module helpers.
        self.assertAlmostEqual(result["score"], 0.10754048582995951, places=12)
        self.assertFalse(result["suspicious"])
        self.assertTrue(result["ready"])

    def test_several_weak_boxes_can_warn_without_any_strong_box(self):
        for _ in range(60):
            self.frame()
        decisions = [self.frame(failures(0.5, 0.5, 0.5)) for _ in range(12)]
        self.assertTrue(any(result["suspicious"] for result in decisions))
        self.assertGreater(decisions[-1]["score"], 1 / 3)

    def test_single_suspicious_frame_after_clean_baseline_does_not_warn(self):
        for _ in range(60):
            self.frame()
        self.assertFalse(self.frame(failures(0.9))["suspicious"])
        for _ in range(20):
            self.assertFalse(self.frame()["suspicious"])

    def test_restart_keeps_calibration_and_the_exact_temporal_history(self):
        for _ in range(60):
            self.frame()
        for _ in range(2):
            self.frame(failures(0.5, 0.5, 0.5))
        self.store.close()
        self.store = Store(self.path)
        self.scorer = Scorer(self.store, clock=lambda: self.now)
        result = self.frame(failures(0.5, 0.5, 0.5))
        self.assertTrue(result["ready"])
        self.assertTrue(result["suspicious"])

    def test_new_job_resets_warning_and_requires_new_calibration(self):
        for _ in range(60):
            self.frame()
        for _ in range(12):
            self.frame(failures(0.5, 0.5, 0.5))
        result = self.frame(job="job-b")
        self.assertFalse(result["ready"])
        self.assertFalse(result["suspicious"])
        for _ in range(28):
            self.assertFalse(self.frame(job="job-b")["ready"])
        self.assertTrue(self.frame(job="job-b")["ready"])

    def test_environmental_long_baseline_survives_new_job_as_in_upstream(self):
        for _ in range(120):
            self.frame(failures(1.0))
        for _ in range(30):
            self.frame(job="job-b")
        self.assertEqual(self.frame(failures(1.0), job="job-b")["score"], 0.0)

    def test_invalid_detector_result_never_teaches_a_clean_frame(self):
        for _ in range(28):
            self.frame()
        for invalid in (None, [["failure", float("nan"), [1, 2, 3, 4]]], failures(1.1),
                        [["other-model", 0.9, [1, 2, 3, 4]]]):
            with self.assertRaises(ValueError):
                self.scorer.classify("job-a", invalid)
        self.assertFalse(self.frame()["ready"])
        self.assertTrue(self.frame()["ready"])


if __name__ == "__main__":
    unittest.main()
