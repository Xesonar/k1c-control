"""Persistent Obico temporal warning judge, without Django.

Derived from TheSpaghettiDetective/obico-server contributors' prediction.py,
failure_detection.py and settings.py at commit
df8190ef7500459599ebf398c3d1f653754792fd. AGPL-3.0-only; see
SCORING_LICENSE.txt and SCORING_README.txt. This normalized score is not a
probability. The caller supplies fresh successful inference frames at 10 s
intervals and owns alerts, consecutive-frame checks and physical actions.
"""
import math
import time


PARAMS = {
    "EWM_SPAN": 12,
    "ROLLING_WIN_SHORT": 310,
    "ROLLING_WIN_LONG": 7200,
    "THRESHOLD_LOW": 0.38,
    "THRESHOLD_HIGH": 0.78,
    "INIT_SAFE_FRAME_NUM": 30,
    "ROLLING_MEAN_SHORT_MULTIPLE": 3.8,
    "ESCALATING_FACTOR": 1.75,
}
STATE_KEY = "obico_scoring"


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _sum_confidence(detections):
    if not isinstance(detections, (list, tuple)) or len(detections) > 1000:
        raise ValueError("invalid_scoring_detections")
    total = 0.0
    for detection in detections:
        if not isinstance(detection, (list, tuple)) or len(detection) != 3:
            raise ValueError("invalid_scoring_detection")
        label, confidence, box = detection
        if (label != "failure" or not _finite(confidence) or not 0 <= confidence <= 1
                or not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_finite(n) for n in box)):
            raise ValueError("invalid_scoring_detection")
        total += confidence
    return total


def _next_rolling_mean(value, mean, count, window):
    # This is the upstream approximation, including its count + 1 startup.
    return mean + (value - mean) / float(window if window <= count else count + 1)


def _update(state, value):
    state["current_p"] = value
    state["current_frame_num"] += 1
    state["lifetime_frame_num"] += 1
    alpha = 2 / (PARAMS["EWM_SPAN"] + 1)
    state["ewm_mean"] = value * alpha + state["ewm_mean"] * (1 - alpha)
    state["rolling_mean_short"] = _next_rolling_mean(
        value, state["rolling_mean_short"], state["current_frame_num"], PARAMS["ROLLING_WIN_SHORT"])
    state["rolling_mean_long"] = _next_rolling_mean(
        value, state["rolling_mean_long"], state["lifetime_frame_num"], PARAMS["ROLLING_WIN_LONG"])


def _warning(state, sensitivity):
    if state["current_frame_num"] < PARAMS["INIT_SAFE_FRAME_NUM"]:
        return False
    adjusted = (state["ewm_mean"] - state["rolling_mean_long"]) * sensitivity
    if adjusted < PARAMS["THRESHOLD_LOW"]:
        return False
    if adjusted > PARAMS["THRESHOLD_HIGH"]:
        return True
    return adjusted > ((state["rolling_mean_short"] - state["rolling_mean_long"])
                       * PARAMS["ROLLING_MEAN_SHORT_MULTIPLE"])


def _normalized_score(state, sensitivity):
    def scale(value, old_min, old_max, new_min, new_max):
        mapped = (value - old_min) * (new_max - new_min) / (old_max - old_min) + new_min
        return min(new_max, max(new_min, mapped))

    warning = (state["rolling_mean_short"] - state["rolling_mean_long"]) * PARAMS["ROLLING_MEAN_SHORT_MULTIPLE"]
    warning = min(PARAMS["THRESHOLD_HIGH"], max(PARAMS["THRESHOLD_LOW"], warning))
    failure = warning * PARAMS["ESCALATING_FACTOR"]
    value = (state["ewm_mean"] - state["rolling_mean_long"]) * sensitivity
    if value > failure:
        return scale(value, failure, failure * 1.5, 2.0 / 3.0, 1.0)
    if value > warning:
        return scale(value, warning, failure, 1.0 / 3.0, 2.0 / 3.0)
    return scale(value, 0, warning, 0, 1.0 / 3.0)


class Scorer:
    def __init__(self, store, config=None, clock=time.time):
        self.store = store
        self.clock = clock
        self.sensitivity = (config or {}).get("detector_sensitivity", 1.0)
        if not _finite(self.sensitivity) or self.sensitivity <= 0:
            raise ValueError("invalid_detector_sensitivity")

    def classify(self, job_key, detections):
        if not isinstance(job_key, str) or not job_key:
            raise ValueError("invalid_scoring_job")
        value = _sum_confidence(detections)
        observed_at = self.clock()
        if not _finite(observed_at):
            raise ValueError("invalid_scoring_clock")

        def advance(previous):
            if previous is None:
                previous = {"version": 1, "lifetime_frame_num": 0, "rolling_mean_long": 0.0}
            if previous.get("version") != 1:
                raise ValueError("unsupported_scoring_state")
            state = dict(previous)
            if state.get("job_key") != job_key:
                # Match PrinterPrediction.reset_for_new_print(): retain the
                # long environmental baseline, reset all per-print means.
                state.update(job_key=job_key, current_frame_num=0, current_p=0.0,
                             ewm_mean=0.0, rolling_mean_short=0.0)
            _update(state, value)
            state["observed_at"] = observed_at
            return state

        state = self.store.mutate(STATE_KEY, advance, default=None)
        ready = state["current_frame_num"] >= PARAMS["INIT_SAFE_FRAME_NUM"]
        suspicious = _warning(state, self.sensitivity)
        score = _normalized_score(state, self.sensitivity)
        if ready:
            detail = "Obico temporal: %s; sum=%.3f; normalized=%.3f" % (
                "warning" if suspicious else "observing", value, score)
        else:
            detail = "Калибровка детектора: %d/%d кадров" % (
                state["current_frame_num"], PARAMS["INIT_SAFE_FRAME_NUM"])
        return {"suspicious": suspicious, "score": score, "ready": ready, "detail": detail}
