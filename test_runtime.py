"""Exercise the production polling loop with a slow camera and fast status."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import run, UpdateDispatcher
from manager import Manager
from storage import Store
from test_manager import Printer, Telegram


class RuntimeTelegram(Telegram):
    def __init__(self, updates):
        super().__init__()
        self.updates = updates
        self.polled = threading.Event()
        self.answered = threading.Event()
        self.manager = None

    def call(self, method):
        return {'username': 'test'} if method == 'getMe' else {}

    def get_updates(self, offset, timeout=15):
        self.polled.set()
        updates, self.updates = self.updates, []
        if updates:
            return updates
        # Return control periodically, just like a bounded Telegram poll.
        time.sleep(.005)
        return []

    def send_message(self, user, text, markup=None):
        result = super().send_message(user, text, markup)
        self.answered.set()
        return result

    def edit_message(self,user,message_id,text,markup=None):
        result=super().edit_message(user,message_id,text,markup)
        self.answered.set()
        return result


class RuntimeTests(unittest.TestCase):
    def test_start_replies_while_the_first_printer_check_is_still_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite');entered,release=threading.Event(),threading.Event()
            printer=Printer();status=printer.status
            def slow_status():
                entered.set();release.wait(3);return status()
            printer.status=slow_status
            update={'update_id':1,'message':{'message_id':1,'date':time.time(),'from':{'id':10001},'chat':{'id':10001,'type':'private'},'text':'/start'}}
            telegram=RuntimeTelegram([update]);manager=Manager(dict({'owner_id':10001}, **{'allowed_user_ids':[10001],'state_dir':directory}),store,printer,telegram)
            manager.tick=lambda:None;manager.check_deadlines=lambda:None;manager.flush_outbox=lambda:None
            handlers={}
            with patch('bot.signal.signal',side_effect=lambda sig,handler:handlers.setdefault(sig,handler)):
                runtime=threading.Thread(target=run,args=({},store,manager,telegram));runtime.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(telegram.answered.wait(.3),'first /start waited for printer HTTP instead of showing a checking panel')
                    self.assertTrue(any('Проверя' in text or 'проверя' in text for _,text,_ in telegram.messages))
                finally:
                    for handler in handlers.values():handler()
                    release.set();runtime.join(4);store.close()
                self.assertFalse(runtime.is_alive())

    def test_slow_background_panel_io_does_not_starve_physical_owner_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite');entered,release=threading.Event(),threading.Event()
            printer=Printer();telegram=RuntimeTelegram([])
            manager=Manager(dict({'owner_id':10001}, **{'allowed_user_ids':[10001],'state_dir':directory}),store,printer,telegram)
            manager._status(10001)
            store.mutate('panels',lambda records:{uid:dict(record,updated_at=time.time()-20) for uid,record in records.items()},{})
            store.set('snapshot',{'state':'printing'})
            edit=telegram.edit_message
            def blocked_edit(*args,**kwargs):
                entered.set();release.wait(3);return edit(*args,**kwargs)
            telegram.edit_message=blocked_edit
            status=printer.status
            def changed_status():
                data=status();data['extruder']['temperature']=246;return data
            printer.status=changed_status
            manager.tick=lambda:None;manager.check_deadlines=lambda:None;manager.flush_outbox=lambda:None
            handlers={}
            def update(identity,text):return {'update_id':identity,'message':{'message_id':identity,'date':time.time(),'from':{'id':10001},'chat':{'id':10001,'type':'private'},'text':text}}
            with patch('bot.signal.signal',side_effect=lambda sig,handler:handlers.setdefault(sig,handler)):
                runtime=threading.Thread(target=run,args=({},store,manager,telegram));runtime.start()
                try:
                    self.assertTrue(entered.wait(1),'background edit did not begin')
                    telegram.updates=[update(1,'/status'),update(2,'/pause')]
                    deadline=time.monotonic()+.3
                    while 'pause' not in printer.calls and time.monotonic()<deadline:time.sleep(.005)
                    self.assertIn('pause',printer.calls,'physical action waited behind UI network I/O')
                finally:
                    for handler in handlers.values():handler()
                    release.set();runtime.join(4);store.close()
                self.assertFalse(runtime.is_alive())

    def test_dispatch_is_bounded_and_preserves_action_order_across_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite')
            started, release = threading.Event(), threading.Event()
            class Handler:
                owners = {10001, 22}
                stopping = False
                def __init__(self):
                    self.calls = []
                def handle_update(self, update, *, consumed=False):
                    self.calls.append((update['update_id'], consumed))
                    if update['update_id'] == 1:
                        started.set()
                        release.wait(2)
            handler = Handler()
            dispatcher = UpdateDispatcher(store, handler, capacity=2)
            def update(identity, user=10001):
                return {'update_id': identity, 'message': {'from': {'id': user},
                    'chat': {'id': user, 'type': 'private'}, 'text': '/pause'}}
            submitted = threading.Event()
            third = threading.Thread(target=lambda: (dispatcher.submit(update(3, user=22)), submitted.set()))
            try:
                self.assertTrue(dispatcher.submit(update(1)))
                self.assertTrue(started.wait(1))
                self.assertTrue(dispatcher.submit(update(2)))
                third.start()
                self.assertFalse(submitted.wait(.1), 'queue exceeded its configured bound')
                self.assertEqual(store.get('offset'), 3, 'blocked update was consumed too early')
                release.set()
                self.assertTrue(submitted.wait(1))
                dispatcher.queues['owner'].join()
                self.assertEqual(handler.calls, [(1, True), (2, True), (3, True)])
                self.assertFalse(dispatcher.submit(update(2)), 'duplicate update was queued')
                self.assertEqual(store.get('offset'), 4)
                self.assertFalse(dispatcher.submit(update(4, user=99)))
                self.assertEqual(store.get('offset'), 5)
                self.assertEqual(len(handler.calls), 3)
            finally:
                release.set()
                third.join(2)
                dispatcher.close()
                store.close()

    def test_shutdown_does_not_dispatch_consumed_pending_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite')
            entered, release = threading.Event(), threading.Event()
            class Handler:
                owners = {10001}
                stopping = False
                def __init__(self):
                    self.calls = []
                def handle_update(self, update, *, consumed=False):
                    self.calls.append(update['update_id'])
                    entered.set()
                    release.wait(2)
            handler = Handler()
            dispatcher = UpdateDispatcher(store, handler)
            def update(identity):
                return {'update_id': identity, 'message': {'from': {'id': 10001},
                    'chat': {'id': 10001, 'type': 'private'}, 'text': '/pause'}}
            try:
                dispatcher.submit(update(1))
                self.assertTrue(entered.wait(1))
                dispatcher.submit(update(2))
                handler.stopping = True
                release.set()
                dispatcher.close()
                self.assertEqual(handler.calls, [1])
                self.assertFalse(store.consume_update(2), 'pending control could replay after restart')
                self.assertFalse(dispatcher.submit(update(3)))
            finally:
                release.set()
                dispatcher.close()
                store.close()

    def test_unknown_private_user_reaches_access_gate_instead_of_being_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite')
            telegram, printer = Telegram(), Printer()
            manager = Manager(dict({'owner_id':10001}, **{'allowed_user_ids': [10001], 'state_dir': directory}), store, printer, telegram)
            dispatcher = UpdateDispatcher(store, manager)
            update = {'update_id': 1, 'message': {'date': time.time(),
                      'from': {'id': 99}, 'chat': {'id': 99, 'type': 'private'}, 'text': '/pause'}}
            try:
                self.assertTrue(dispatcher.submit(update))
                dispatcher.queues['owner'].join()
                self.assertEqual(printer.calls, [])
                self.assertEqual(telegram.messages[-1][0], 99)
                self.assertIn('доступ', telegram.messages[-1][1].lower())
            finally:
                dispatcher.close()
                store.close()

    def test_stl_preparation_worker_keeps_same_owner_status_responsive(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite')
            entered,release,status_received,checking_received=threading.Event(),threading.Event(),threading.Event(),threading.Event()
            class Slicer:
                configured = True
                def slice(self, source, destination):
                    Path(destination).write_text('; PETG\nG28\n')
                    return {'material': 'PETG', 'estimated_time': 10, 'filament_grams': 1}
            def update(identity, text=''):
                return {'update_id': identity, 'message': {'message_id': identity, 'date': time.time(),
                    'from': {'id': 10001, 'first_name': 'Owner'}, 'chat': {'id': 10001, 'type': 'private'}, 'text': text}}
            document = update(1)
            document['message']['document'] = {'file_id': 'model', 'file_name': 'case.stl', 'file_size': 80}
            telegram = RuntimeTelegram([document])
            original_send = telegram.send_message
            def send(user, text, markup=None):
                if '245' in text:status_received.set()
                if 'Проверяю' in text:checking_received.set()
                return original_send(user, text, markup)
            def slow_download(file_id, destination, max_bytes=20 * 1024 * 1024):
                entered.set()
                release.wait(2)
                Path(destination).write_text('solid model')
            telegram.send_message = send
            original_edit=telegram.edit_message
            def edit(user,message_id,text,markup=None):
                if '245' in text:status_received.set()
                if 'Проверяю' in text:checking_received.set()
                return original_edit(user,message_id,text,markup)
            telegram.edit_message=edit
            telegram.download_file = slow_download
            manager = Manager(dict({'owner_id':10001}, **{'allowed_user_ids': [10001], 'state_dir': directory}), store, Printer(), telegram, slicer=Slicer())
            manager.tick = lambda: None
            manager.check_deadlines = lambda: None
            manager.flush_outbox = lambda: None
            signal_handlers = {}
            with patch('bot.signal.signal', side_effect=lambda sig, handler: signal_handlers.setdefault(sig, handler)):
                runtime = threading.Thread(target=run, args=({}, store, manager, telegram))
                runtime.start()
                try:
                    self.assertTrue(entered.wait(1), 'background STL download never started')
                    telegram.updates = [update(2, '/status')]
                    self.assertTrue(checking_received.wait(.35),'checking status waited for STL download/slice')
                    self.assertTrue(status_received.wait(1.5),'fresh status was not delivered after background check')
                finally:
                    for handler in signal_handlers.values():
                        handler()
                    release.set()
                    runtime.join(3)
                    store.close()
                self.assertFalse(runtime.is_alive(), 'runtime failed to stop')

    def test_status_does_not_wait_for_the_same_owners_slow_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite')
            entered, release = threading.Event(), threading.Event()
            printer = Printer()
            def slow_photo():
                entered.set()
                release.wait(2)
                return b'\xff\xd8jpeg\xff\xd9'
            printer.photo = slow_photo
            def update(identity, text):
                return {'update_id': identity, 'message': {'message_id': identity, 'date': time.time(),
                    'from': {'id': 10001, 'first_name': 'Owner'}, 'chat': {'id': 10001, 'type': 'private'}, 'text': text}}
            telegram = RuntimeTelegram([update(1, '/photo'), update(2, '/status')])
            manager = Manager(dict({'owner_id':10001}, **{'allowed_user_ids': [10001], 'state_dir': directory}), store, printer, telegram)
            # Avoid unrelated monitor notifications in this latency measurement.
            manager.tick = lambda: None
            manager.check_deadlines = lambda: None
            manager.flush_outbox = lambda: None
            signal_handlers = {}
            with patch('bot.signal.signal', side_effect=lambda sig, handler: signal_handlers.setdefault(sig, handler)):
                runtime = threading.Thread(target=run, args=({}, store, manager, telegram))
                runtime.start()
                try:
                    self.assertTrue(entered.wait(1), 'camera handler did not start')
                    started = time.monotonic()
                    self.assertTrue(telegram.answered.wait(.35), 'status is blocked behind the slow photo')
                    self.assertLess(time.monotonic() - started, .35)
                finally:
                    for handler in signal_handlers.values():
                        handler()
                    release.set()
                    runtime.join(3)
                    store.close()
                self.assertFalse(runtime.is_alive(), 'runtime failed to stop')
