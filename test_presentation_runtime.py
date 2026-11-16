"""Production run() guards: presentation latency must not govern printer actions.

All transport is event-controlled fake I/O. These tests exercise poll/dispatch,
cache activation and panel delivery together, without naming cache internals.
"""
import queue
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters import ApiError
from bot import run
from manager import Manager
from storage import Store
from test_manager import Printer, Telegram

OWNER = 10001


class RuntimeTelegram(Telegram):
    def __init__(self):
        super().__init__()
        self.incoming = queue.Queue()
        self.polled = threading.Event()
        self.changed = threading.Condition()
        self._next_message = 0
        self.message_ids = {}
        self.hold_send = self.hold_edit = False
        self.ui_entered, self.ui_release = threading.Event(), threading.Event()
        self.edit_returned = threading.Event()
        self.edit_error = False
        self.late_frame = threading.Event()
        self.check_late = False

    def call(self, method, *_args, **_kwargs):
        return {'username': 'runtime_fake'} if method == 'getMe' else {}

    def get_updates(self, offset, timeout=15):
        self.polled.set()
        try:
            return self.incoming.get(timeout=.05)
        except queue.Empty:
            return []

    def _record(self, user, text, markup, message_id=None):
        with self.changed:
            self._next_message += 1
            message_id = message_id or self._next_message
            self.message_ids[user] = message_id
            self.messages.append((user, text, markup))
            if self.check_late and user in {OWNER, 22} and '🖨' in text:
                self.late_frame.set()
            self.changed.notify_all()
            return {'message_id': message_id}

    def send_message(self, user, text, markup=None):
        if self.hold_send and user == OWNER:
            self.ui_entered.set()
            if not self.ui_release.wait(5):
                raise ApiError('network')
        return self._record(user, text, markup)

    def edit_message(self, user, message_id, text, markup=None):
        if self.hold_edit and user == 22:
            self.ui_entered.set()
            if not self.ui_release.wait(5):
                raise ApiError('network')
            self.edit_returned.set()
            if self.edit_error:
                raise ApiError('http_400', http_status=400)
        self.edits.append((user, message_id, text, markup))
        return self._record(user, text, markup, message_id)

    def wait_frame(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                found = next((item for item in reversed(self.messages) if predicate(item)), None)
                if found:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.changed.wait(remaining)

    def button(self, user, contains):
        with self.changed:
            for recipient, _text, markup in reversed(self.messages):
                if recipient != user:
                    continue
                for row in (markup or {}).get('inline_keyboard', []):
                    for button in row:
                        if contains in button['text']:
                            return button['callback_data']
        raise AssertionError('missing button: ' + contains)


class Runtime:
    def __init__(self, case, printer=None):
        self.case = case
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'runtime.sqlite')
        self.printer, self.telegram = printer or Printer(), RuntimeTelegram()
        self.manager = Manager(dict({'owner_id':10001}, **{'allowed_user_ids': [OWNER, 22, 33], 'state_dir': self.directory.name}),
                               self.store, self.printer, self.telegram)
        # Only the production UI and dispatch loops matter to these scenarios.
        self.manager.tick = lambda: None
        self.manager.check_deadlines = lambda: None
        self.manager.flush_outbox = lambda: None
        self.manager.process_slicing_job = lambda: False
        self.manager.preflight.tick = lambda: False
        self.handlers, self.errors, self.releases = {}, [], [self.telegram.ui_release]
        self.next_update = 0

    def __enter__(self):
        self.signal_patch = patch('bot.signal.signal', side_effect=lambda sig, handler: self.handlers.setdefault(sig, handler))
        self.signal_patch.start()
        def serve():
            try:
                run({}, self.store, self.manager, self.telegram)
            except BaseException as exc:
                self.errors.append(exc)
        self.thread = threading.Thread(target=serve, name='production-runtime', daemon=True)
        self.thread.start()
        if not self.telegram.polled.wait(2):
            self.__exit__()
            self.case.fail('production runtime failed to start polling')
        return self

    def __exit__(self, *_args):
        stop = self.handlers.get(signal.SIGTERM)
        if stop:
            stop()
        for release in self.releases:
            release.set()
        self.telegram.incoming.put([])
        self.thread.join(5)
        self.signal_patch.stop()
        if self.thread.is_alive():
            raise AssertionError('runtime did not stop after releasing fake I/O; Store kept open')
        self.store.close()
        self.directory.cleanup()
        self.case.assertEqual(self.errors, [], 'runtime raised an uncaught exception')

    def text(self, text, user=OWNER):
        self.next_update += 1
        self.telegram.incoming.put([{'update_id': self.next_update, 'message': {
            'message_id': self.next_update, 'date': time.time(), 'from': {'id': user, 'first_name': 'User'},
            'chat': {'id': user, 'type': 'private'}, 'text': text}}])

    def callback(self, data, user=OWNER, message_id=1):
        self.next_update += 1
        self.telegram.incoming.put([{'update_id': self.next_update, 'callback_query': {
            'id': str(self.next_update), 'from': {'id': user, 'first_name': 'User'}, 'data': data,
            'message': {'message_id': message_id, 'text': 'K1C panel', 'chat': {'id': user, 'type': 'private'}}}}])

    def warm_panel(self, user=OWNER):
        self.text('/status', user)
        self.case.assertIsNotNone(self.telegram.wait_frame(lambda item: item[0] == user and '245' in item[1], timeout=4),
                                  'presentation never displayed a completed printer read')
        return self.telegram.message_ids[user]


class PresentationRuntimeTests(unittest.TestCase):
    def test_cold_ui_replies_before_first_printer_http_finishes(self):
        printer, entered, release = Printer(), threading.Event(), threading.Event()
        status = printer.status
        def blocked_status():
            entered.set()
            release.wait(5)
            return status()
        printer.status = blocked_status
        with Runtime(self, printer) as runtime:
            runtime.releases.append(release)
            runtime.text('/start')
            self.assertTrue(entered.wait(2), 'runtime did not begin the first printer read')
            reply = runtime.telegram.wait_frame(lambda item: item[0] == OWNER, timeout=1)
            self.assertIsNotNone(reply, 'cold presentation waited for printer HTTP')
            self.assertFalse(release.is_set(), 'test accidentally released the blocked printer')
            self.assertTrue(any(word in reply[1].lower() for word in ('провер', 'обновля')),
                            'cold UI should acknowledge checking rather than invent live status')

    def test_blocked_ui_delivery_does_not_delay_pause(self):
        paused = threading.Event()
        with Runtime(self) as runtime:
            pause = runtime.printer.pause
            def observe_pause():
                pause(); paused.set()
            runtime.printer.pause = observe_pause
            runtime.telegram.hold_send = True
            runtime.text('/status')
            self.assertTrue(runtime.telegram.ui_entered.wait(2), 'UI delivery never reached fake Telegram')
            runtime.text('/pause')
            self.assertTrue(paused.wait(1), 'physical pause waited behind blocked UI network delivery')
            self.assertFalse(runtime.telegram.ui_release.is_set())

    def test_displayed_printing_snapshot_cannot_authorize_pause_of_fresh_standby(self):
        with Runtime(self) as runtime:
            runtime.warm_panel()
            runtime.printer.state = 'standby'
            fresh_control_read = threading.Event()
            context = runtime.manager.context
            def observe_context(include_job=True):
                if include_job:
                    fresh_control_read.set()
                return context(include_job=include_job)
            runtime.manager.context = observe_context
            runtime.text('/pause')
            self.assertTrue(fresh_control_read.wait(1), 'physical command bypassed the fresh context boundary')
            self.assertIsNotNone(runtime.telegram.wait_frame(lambda item: item[0] == OWNER and 'неприменимо' in item[1], timeout=2))
            self.assertNotIn('pause', runtime.printer.calls, 'cached printing presentation caused a physical pause')

    def test_admin_and_revocation_drop_stale_ui_and_failed_edit_fallback(self):
        with Runtime(self) as runtime:
            ordinary_panel = runtime.warm_panel(22)
            runtime.telegram.hold_edit = True
            runtime.telegram.edit_error = True
            runtime.callback('n:status', user=22, message_id=ordinary_panel)
            self.assertTrue(runtime.telegram.ui_entered.wait(2), 'old panel edit did not reach Telegram')
            runtime.text('/status')  # Queued presentation must lose to the next Admin interaction.
            runtime.text('/admin')
            self.assertIsNotNone(runtime.telegram.wait_frame(lambda item: item[0] == OWNER and 'Управление доступом' in item[1], timeout=1),
                                 'opening Admin waited behind another user\'s UI request')
            runtime.callback('x:user:22')
            self.assertIsNotNone(runtime.telegram.wait_frame(lambda item: item[0] == OWNER and 'ID: 22' in item[1], timeout=1))
            revoke = runtime.telegram.button(OWNER, 'Отозвать')
            runtime.callback(revoke)
            self.assertIsNotNone(runtime.telegram.wait_frame(lambda item: item[0] == 22 and 'отозван' in item[1], timeout=1))
            self.assertFalse(runtime.manager.access.is_allowed(22))
            runtime.telegram.check_late = True
            runtime.telegram.ui_release.set()
            self.assertTrue(runtime.telegram.edit_returned.wait(1))
            runtime.text('/status', user=33)
            self.assertIsNotNone(runtime.telegram.wait_frame(lambda item: item[0] == 33, timeout=2))
            self.assertFalse(runtime.telegram.late_frame.wait(.2), 'stale UI fell back to a new message after Admin/revocation')
            self.assertEqual(runtime.store.get('panels')[str(OWNER)]['view'], 'admin')


if __name__ == '__main__':
    unittest.main(verbosity=2)
