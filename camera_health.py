"""Debounce camera observations and coalesce their durable notifications only."""

PREFIX = "camera-health:"
FAILURE = "📷 Камера недоступна. Контроль брака временно не работает; печать продолжается."
RECOVERY = "📷 Камера снова доступна, контроль восстанавливается."


class CameraHealth:
    def __init__(self, store, notify, clock):
        self.store, self.notify, self.clock = store, notify, clock

    def observe(self, available, job_id, interval):
        now = self.clock()
        # Allow ordinary slow status/history/photo reads. Idle hours or a new
        # job must not join unrelated samples into one consecutive streak.
        max_gap = max(60.0, 3 * float(interval))

        def update(state):
            # No outage has been delivered at startup. A recovered initial
            # outage that never reached Telegram needs no recovery message.
            state.setdefault("delivered_state", True)
            state.setdefault("delivered_sequence", 0)
            last = state.get("last_sample")
            if (state.get("job_id") != job_id or last is None
                    or now < last or now - last > max_gap):
                state.update(candidate=None, count=0, since=None)
            state.update(last_sample=now, job_id=job_id)
            confirmed = state.get("confirmed")
            if available == confirmed:
                state.update(candidate=None, count=0, since=None)
            else:
                if state.get("candidate") != available:
                    state.update(candidate=available, count=1, since=now)
                else:
                    state["count"] += 1
                if state["count"] >= 3 and now - state["since"] >= 20:
                    state["sequence"] = state.get("sequence", 0) + 1
                    state.update(confirmed=available, notice=None,
                                 candidate=None, count=0, since=None)
                    self.store.set("condition:camera", available)
            confirmed = state.get("confirmed")
            last_notice = state.get("last_notice")
            # At most one current event; intermediate confirmed transitions
            # invalidate older outbox items rather than building a backlog.
            if (confirmed is not None and state.get("notice") is None
                    and confirmed != state.get("delivered_state")
                    and (last_notice is None or now - last_notice >= 300)):
                key = PREFIX + str(state["sequence"]) + (":up" if confirmed else ":down")
                state["notice"] = key
                state["last_notice"] = now
                # Store.mutate + enqueue share a transaction. No network here.
                self.notify(RECOVERY if confirmed else FAILURE, key)
            return state

        self.store.mutate("camera_health", update, default={})

    @staticmethod
    def _parse(key):
        if not key.startswith(PREFIX):
            return None
        parts = key[len(PREFIX):].split(":")
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in {"up", "down"}:
            return None
        return int(parts[0]), parts[1] == "up"

    def delivery_is_current(self, key):
        if not key.startswith(PREFIX):
            return True
        parsed = self._parse(key)
        if parsed is None:
            return False
        state = self.store.get("camera_health", {})
        sequence, available = parsed
        return (state.get("notice") == key and state.get("sequence") == sequence
                and state.get("confirmed") == available)

    def delivered(self, key):
        parsed = self._parse(key)
        if parsed is None:
            return
        sequence, available = parsed
        now = self.clock()

        def update(state):
            state["last_notice"] = max(state.get("last_notice") or now, now)
            # A send may finish after another observation. Record what was
            # actually sent, without rolling confirmed state/notice backwards.
            if sequence >= state.get("delivered_sequence", 0):
                state["delivered_sequence"] = sequence
                state["delivered_state"] = available
            return state

        self.store.mutate("camera_health", update, default={})
