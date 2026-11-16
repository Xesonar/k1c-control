import tempfile
import threading
import unittest
from pathlib import Path

from access import AccessController, KEY
from test_support import OWNER_ID
from adapters import ApiError
from storage import Store


class Telegram:
    def __init__(self):
        self.messages = []
        self.callbacks = []
        self.fail_ack = False

    def send_message(self, user, text, markup=None):
        self.messages.append((user, text, markup))

    def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))
        if self.fail_ack:
            raise ApiError("network")


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.telegram = Telegram()
        self.now = 1000
        self.access = AccessController(self.store, owner_id=OWNER_ID, initial_users=(OWNER_ID, 22, 33), clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def message(user, text="/start"):
        return {"message": {"from": {"id": user, "first_name": "Имя " + str(user)},
                            "chat": {"type": "private", "id": user}, "text": text}}

    @staticmethod
    def callback(user, data):
        return {"callback_query": {"id": "callback-1", "from": {"id": user, "first_name": "Имя " + str(user)},
                                   "message": {"chat": {"type": "private", "id": user}}, "data": data}}

    def handle(self, update):
        return self.access.handle_update(update, self.telegram)

    def record(self, uid):
        return self.store.get(KEY)["users"].get(str(uid))

    def request(self, uid=99):
        self.assertTrue(self.handle(self.callback(uid, "x:request")))
        return self.record(uid)["nonce"]

    def decide(self, action, uid=99, token=None, actor=OWNER_ID):
        token = token or self.record(uid)["nonce"] or "v" + str(self.record(uid)["version"])
        self.assertTrue(self.handle(self.callback(actor, f"x:{action}:{uid}:{token}")))

    def test_legacy_access_is_preserved_but_only_one_owner_exists(self):
        self.assertEqual(self.access.allowed_users(), {OWNER_ID, 22, 33})
        self.assertTrue(self.access.is_owner(OWNER_ID))
        self.assertFalse(self.access.is_owner(22))
        self.assertFalse(self.access.is_owner(True))
        self.assertFalse(self.handle(self.message(22, "/status")))
        self.assertTrue(self.handle(self.message(22, "/admin")))
        self.assertIn("только владельцу", self.telegram.messages[-1][1])

    def test_unknown_messages_get_request_button_without_owner_notification(self):
        for text in ("/start", "/status", "/admin", "случайное сообщение"):
            with self.subTest(text=text):
                self.assertTrue(self.handle(self.message(99, text)))
                self.assertEqual(self.telegram.messages[-1][0], 99)
                self.assertEqual(self.telegram.messages[-1][2]["inline_keyboard"][0][0]["callback_data"], "x:request")
        self.assertEqual(self.store.pending(), [])
        self.assertIsNone(self.record(99))

    def test_groups_sender_mismatch_bots_and_invalid_ids_are_silently_ignored(self):
        updates = []
        for kind, value in (("type", "group"), ("id", 55)):
            update = self.message(99)
            update["message"]["chat"][kind] = value
            updates.append(update)
        bot = self.message(99)
        bot["message"]["from"]["is_bot"] = True
        updates.append(bot)
        updates.extend(self.message(uid) for uid in (-1, 0, True, "99"))
        forwarded = self.callback(OWNER_ID, "x:admin")
        forwarded["callback_query"]["message"]["chat"]["id"] = 99
        updates.append(forwarded)
        for update in updates:
            self.assertTrue(self.handle(update))
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.telegram.callbacks, [])

    def test_request_is_durable_deduplicated_and_only_notifies_owner(self):
        nonce = self.request()
        self.request()
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["recipient"], OWNER_ID)
        self.assertIn(f"x:approve:99:{nonce}", str(pending[0]["markup"]))
        self.assertEqual(self.record(99)["request_count"], 1)
        self.now += 600
        self.assertEqual(self.request(), nonce)
        self.assertEqual(len(self.store.pending()), 2)
        self.assertEqual(self.record(99)["request_count"], 2)

    def test_concurrent_request_clicks_create_one_pending_notification(self):
        workers = [threading.Thread(target=self.handle, args=(self.callback(99, "x:request"),)) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.record(99)["request_count"], 1)

    def test_forged_admin_decisions_never_grant_access(self):
        nonce = self.request()
        state = self.store.get(KEY)
        for actor in (22, 33, 99, 100):
            with self.subTest(actor=actor):
                self.decide("approve", token=nonce, actor=actor)
                self.assertFalse(self.access.is_allowed(99))
        # Names may be shown in the gate, but the permission state is untouched.
        self.assertEqual(self.store.get(KEY), state)

    def test_owner_approval_is_bound_to_pending_user_and_nonce(self):
        nonce = self.request()
        self.decide("approve", token="wrongnonce")
        self.assertFalse(self.access.is_allowed(99))
        self.request(100)
        self.decide("approve", uid=100, token=nonce)
        self.assertFalse(self.access.is_allowed(100))
        self.decide("approve", token=nonce)
        self.assertTrue(self.access.is_allowed(99))
        self.assertFalse(self.handle(self.message(99, "/photo")))
        self.assertTrue(any(user == 99 and "разрешил" in text for user, text, _ in self.telegram.messages))

    def test_deny_and_new_request_rotate_nonce_and_reject_old_approval(self):
        old_nonce = self.request()
        self.decide("deny", token=old_nonce)
        self.assertFalse(self.access.is_allowed(99))
        self.assertEqual(self.record(99)["status"], "denied")
        self.request()
        self.assertEqual(self.record(99)["status"], "denied")
        self.now += 601
        new_nonce = self.request()
        self.assertNotEqual(new_nonce, old_nonce)
        self.decide("approve", token=old_nonce)
        self.assertFalse(self.access.is_allowed(99))
        self.decide("approve", token=new_nonce)
        self.assertTrue(self.access.is_allowed(99))

    def test_revoke_immediately_removes_access_and_old_approval_never_restores_it(self):
        nonce = self.request()
        self.decide("approve", token=nonce)
        version = "v" + str(self.record(99)["version"])
        self.decide("revoke", token=version)
        self.assertFalse(self.access.is_allowed(99))
        self.assertNotIn(99, self.access.allowed_users())
        self.decide("approve", token=nonce)
        self.assertFalse(self.access.is_allowed(99))
        self.assertTrue(self.handle(self.message(99, "/pause")))

    def test_stale_version_buttons_do_not_grant_or_revoke_after_new_decision(self):
        self.decide("revoke", uid=22)
        grant = "v" + str(self.record(22)["version"])
        self.decide("grant", uid=22, token=grant)
        self.assertTrue(self.access.is_allowed(22))
        self.decide("revoke", uid=22)
        self.decide("grant", uid=22, token=grant)
        self.assertFalse(self.access.is_allowed(22))

    def test_block_suppresses_requests_until_owner_unblocks(self):
        nonce = self.request()
        self.decide("block", token=nonce)
        messages = len(self.telegram.messages)
        notifications = len(self.store.pending())
        self.now += 601
        self.assertTrue(self.handle(self.callback(99, "x:request")))
        self.assertTrue(self.handle(self.message(99)))
        self.assertEqual(len(self.telegram.messages), messages)
        self.assertEqual(len(self.store.pending()), notifications)
        self.decide("unblock")
        self.assertFalse(self.access.is_allowed(99))
        self.assertEqual(self.record(99)["status"], "revoked")
        self.request()
        self.assertEqual(self.record(99)["status"], "pending")

    def test_owner_cannot_be_revoked_blocked_or_denied(self):
        for action in ("block", "revoke", "deny", "approve", "unblock", "grant"):
            self.handle(self.callback(OWNER_ID, f"x:{action}:{OWNER_ID}:v1"))
            self.assertTrue(self.access.is_allowed(OWNER_ID))
            self.assertEqual(self.record(OWNER_ID)["status"], "allowed")

    def test_failed_callback_ack_does_not_discard_permission_change(self):
        nonce = self.request()
        self.telegram.fail_ack = True
        self.decide("approve", token=nonce)
        self.assertTrue(self.access.is_allowed(99))

    def test_restart_migration_never_readds_revoked_legacy_access(self):
        self.decide("revoke", uid=22)
        self.request()
        nonce = self.record(99)["nonce"]
        audits = len(self.store.recent_audit(100))
        restarted = AccessController(self.store, owner_id=OWNER_ID, initial_users=(OWNER_ID, 22, 33), clock=lambda: self.now)
        self.assertFalse(restarted.is_allowed(22))
        self.assertEqual(self.record(99)["nonce"], nonce)
        self.assertEqual(len(self.store.recent_audit(100)), audits)
        self.assertEqual(restarted.allowed_users(), {OWNER_ID, 33})

    def test_admin_ui_has_counts_user_cards_and_pagination(self):
        for uid in range(100, 108):
            self.request(uid)
        self.assertTrue(self.handle(self.message(OWNER_ID, "/admin")))
        self.assertIn("Заявок: 8", self.telegram.messages[-1][1])
        self.handle(self.callback(OWNER_ID, "x:requests:0"))
        self.assertIn("1/2", self.telegram.messages[-1][1])
        markup = self.telegram.messages[-1][2]
        self.assertTrue(any(button["callback_data"] == "x:requests:1" for row in markup["inline_keyboard"] for button in row))
        self.handle(self.callback(OWNER_ID, "x:user:22"))
        self.assertIn("ID: 22", self.telegram.messages[-1][1])
        self.assertIn("x:revoke:22:v1", str(self.telegram.messages[-1][2]))

    def test_request_and_permission_audits_are_saved(self):
        self.request()
        self.decide("approve")
        self.decide("revoke")
        records = self.store.recent_audit(10)
        self.assertTrue(any(record["actor"] == 99 and record["action"] == "access_request" for record in records))
        self.assertTrue(any(record["actor"] == OWNER_ID and record["action"] == "access_approve" for record in records))
        self.assertTrue(any(record["actor"] == OWNER_ID and record["action"] == "access_revoke" for record in records))


if __name__ == "__main__":
    unittest.main()
