import threading
import time
import unittest

from status_cache import StatusCache


class StatusError(Exception):
    code = "network"


class StatusCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.caches = []

    def tearDown(self):
        for cache in self.caches:
            cache.close(timeout=1)

    def cache(self, fetch, **kwargs):
        cache = StatusCache(fetch, clock=lambda: self.now, **kwargs)
        self.caches.append(cache)
        return cache

    def completed(self, cache):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            view = cache.read()
            if not view["refreshing"]:
                return view
            threading.Event().wait(.001)
        self.fail("background status did not finish")

    def test_cold_read_returns_while_printer_request_is_still_blocked(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        views = []

        def fetch():
            entered.set()
            release.wait(3)
            raise StatusError("private transport detail must not reach the UI")

        cache = self.cache(fetch)

        def read():
            views.append(cache.read())
            done.set()

        reader = threading.Thread(target=read)
        reader.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(done.wait(1), "UI read waited for offline HTTP")
            self.assertEqual(views[0]["state"], "checking")
            self.assertIsNone(views[0]["context"])
            self.assertTrue(views[0]["refreshing"])
            self.assertEqual(views[0]["generation"], 0)
        finally:
            release.set()
            reader.join(2)
        view = self.completed(cache)
        self.assertEqual(view["state"], "offline")
        self.assertEqual(view["error"], "network")
        self.assertEqual(view["checked_at"], 0)
        self.assertIsNone(view["success_at"])
        self.assertEqual(view["generation"], 1)

    def test_many_readers_share_one_inflight_refresh(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def fetch():
            calls.append(1)
            entered.set()
            release.wait(3)
            return {"state": "standby"}

        cache = self.cache(fetch)
        cache.read()
        self.assertTrue(entered.wait(1))
        readers = [threading.Thread(target=cache.read) for _ in range(30)]
        try:
            for reader in readers:
                reader.start()
            for reader in readers:
                reader.join(1)
                self.assertFalse(reader.is_alive(), "reader waited for the refresh")
            self.assertEqual(calls, [1])
            self.assertFalse(cache.request())
        finally:
            release.set()
        self.assertEqual(self.completed(cache)["state"], "fresh")

    def test_fresh_ttl_then_stale_snapshot_during_refresh(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                entered.set()
                release.wait(3)
            return {"state": "printing", "seen_at": 123}

        cache = self.cache(fetch)
        first = self.completed(cache)
        self.assertEqual(first["state"], "fresh")
        self.assertEqual(first["age"], 0)
        self.now = 1.99
        self.assertEqual(cache.read()["state"], "fresh")
        self.assertEqual(len(calls), 1)
        self.now = 2
        try:
            view = cache.read()
            self.assertTrue(entered.wait(1))
            self.assertEqual(view["state"], "stale")
            self.assertEqual(view["age"], 2)
            self.assertEqual(view["context"]["seen_at"], 123)
            self.assertEqual(view["success_at"], 0)
            self.assertTrue(view["refreshing"])
        finally:
            release.set()
        self.assertEqual(self.completed(cache)["success_at"], 2)

    def test_offline_retry_has_backoff_and_recovers(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) == 1:
                raise StatusError()
            return {"state": "standby"}

        cache = self.cache(fetch)
        self.assertEqual(self.completed(cache)["state"], "offline")
        self.now = 4.99
        for _ in range(20):
            self.assertEqual(cache.read()["state"], "offline")
        self.assertEqual(len(calls), 1)
        self.now = 5
        view = self.completed(cache)
        self.assertEqual(len(calls), 2)
        self.assertEqual(view["state"], "fresh")
        self.assertIsNone(view["error"])
        self.assertEqual(view["success_at"], 5)
        self.assertEqual(view["generation"], 2)

    def test_failed_refresh_retains_success_without_calling_it_fresh(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                raise StatusError()
            return {"ready": True, "seen_at": 1000}

        cache = self.cache(fetch)
        self.completed(cache)
        self.now = 2
        view = self.completed(cache)
        self.assertEqual(view["context"], {"ready": True, "seen_at": 1000})
        self.assertEqual(view["state"], "stale")
        self.assertEqual(view["error"], "network")
        self.assertEqual(view["checked_at"], 2)
        self.assertEqual(view["success_at"], 0)
        self.assertEqual(view["age"], 2)

    def test_success_and_reads_do_not_share_mutable_context(self):
        source = {"data": {"extruder": {"target": 240}}, "seen_at": 1000}
        cache = self.cache(lambda: source)
        view = self.completed(cache)
        source["data"]["extruder"]["target"] = 0
        view["context"]["data"]["extruder"]["target"] = 999
        next_view = cache.read()
        self.assertEqual(next_view["context"]["data"]["extruder"]["target"], 240)
        self.assertEqual(next_view["context"]["seen_at"], 1000)

    def test_close_does_not_wait_for_http_or_publish_its_late_result(self):
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            release.wait(3)
            return {"state": "printing"}

        cache = self.cache(fetch)
        cache.read()
        self.assertTrue(entered.wait(1))
        try:
            before = time.monotonic()
            cache.close()
            self.assertLess(time.monotonic() - before, .2)
            self.assertFalse(cache.request())
            self.assertFalse(cache.read()["refreshing"])
        finally:
            release.set()
        cache.close(timeout=1)
        self.assertIsNone(cache.read()["context"])
        self.assertIsNone(cache.read()["checked_at"])

    def test_external_stopping_prevents_new_and_late_updates(self):
        entered, release = threading.Event(), threading.Event()
        stopping = [False]

        def fetch():
            entered.set()
            release.wait(3)
            return {"ready": True}

        cache = self.cache(fetch, stopping=lambda: stopping[0])
        cache.read()
        self.assertTrue(entered.wait(1))
        stopping[0] = True
        release.set()
        cache._thread.join(1)
        self.assertFalse(cache._thread.is_alive())
        self.assertIsNone(cache.read()["context"])
        self.assertIsNone(cache.read()["checked_at"])
        self.assertFalse(cache.request())

    def test_unknown_errors_are_sanitized_and_invalid_results_are_offline(self):
        def fetch():
            raise RuntimeError("https://secret:token@private-server/")

        cache = self.cache(fetch)
        view = self.completed(cache)
        self.assertEqual(view["error"], "status_unavailable")
        self.assertNotIn("secret", repr(view))
        invalid = self.cache(lambda: None)
        self.assertEqual(self.completed(invalid)["state"], "offline")


if __name__ == "__main__":
    unittest.main()
