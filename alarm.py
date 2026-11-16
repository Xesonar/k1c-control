"""Persistent defect-alert decisions; this module never controls the printer."""
import time
import uuid


class AlarmController:
    def __init__(self, store, clock=time.time, delay=300, mute=600, consecutive=3):
        if delay < 0 or mute < 0 or consecutive < 1:
            raise ValueError("invalid_alarm_timing")
        self.store = store
        self.clock = clock
        self.delay = delay
        self.mute = mute
        self.consecutive = consecutive

    @staticmethod
    def _empty(job=None):
        return {"job": job, "bad_count": 0, "mute_until": 0, "alert": None}

    def current(self):
        return self.store.get("alarm_state", self._empty())["alert"]

    def reset(self):
        self.store.set("alarm_state", self._empty())

    def observe(self, job_key, filename, suspicious, score, photo=None):
        now = self.clock()
        created = False

        def update(state):
            nonlocal created
            if state["job"] != job_key:
                state = self._empty(job_key)
            alert = state["alert"]
            if now < state["mute_until"] or (alert and alert["status"] in {"pending", "pause_claimed"}):
                state["bad_count"] = 0
                return state
            state["bad_count"] = state["bad_count"] + 1 if suspicious else 0
            if state["bad_count"] >= self.consecutive:
                state["alert"] = {
                    "id": uuid.uuid4().hex,
                    "job_key": job_key,
                    "filename": filename,
                    "created": now,
                    "deadline": now + self.delay,
                    "status": "pending",
                    "decision": None,
                    "actor": None,
                    "score": score,
                }
                state["bad_count"] = 0
                created = True
            return state

        state = self.store.mutate("alarm_state", update, self._empty())
        return state["alert"] if created else None

    def tick(self, job_key, state):
        now = self.clock()
        claimed = False

        def update(saved):
            nonlocal claimed
            if saved["job"] != job_key or state not in {"printing", "paused"}:
                return self._empty(job_key)
            alert = saved["alert"]
            if not alert or alert["status"] != "pending":
                return saved
            if state == "paused":
                alert["status"] = "paused"
            elif now >= alert["deadline"]:
                alert["status"] = "pause_claimed"
                claimed = True
            return saved

        saved = self.store.mutate("alarm_state", update, self._empty())
        return saved["alert"] if claimed else None

    def mark_autopause(self, alert_id, result="accepted"):
        status = {"accepted": "autopaused", "uncertain": "uncertain", "failed": "failed"}.get(result)
        if status is None:
            raise ValueError("invalid_autopause_result")

        def update(state):
            alert = state["alert"]
            if alert and alert["id"] == alert_id:
                # Keep any owner's decision recorded while the physical request
                # was in flight; its result must never trigger a resume/retry.
                alert["status"] = status
            return state

        self.store.mutate("alarm_state", update, self._empty())

    def decide(self, alert_id, actor, decision):
        if decision not in {"pause", "continue"}:
            raise ValueError("invalid_alarm_decision")
        now = self.clock()
        accepted = False
        late = False
        matched = False

        def update(state):
            nonlocal accepted, late, matched
            alert = state["alert"]
            if not alert or alert["id"] != alert_id:
                return state
            matched = True
            late = alert["status"] != "pending"
            if alert["decision"] is not None:
                return state
            accepted = True
            alert["decision"] = decision
            alert["actor"] = actor
            if decision == "continue":
                state["mute_until"] = now + self.mute
                state["bad_count"] = 0
                alert["status"] = "dismissed"
            elif not late:
                alert["status"] = "pause_claimed"
            return state

        saved = self.store.mutate("alarm_state", update, self._empty())
        return {"accepted": accepted, "late": late, "alert": saved["alert"] if matched else None}
