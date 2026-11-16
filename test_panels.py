import tempfile
import unittest
from pathlib import Path
from adapters import ApiError
from panels import Panels
from storage import Store

class Telegram:
    def __init__(self):
        self.sent, self.edited = [], []
    def send_message(self, user, text, markup):
        self.sent.append((user,text,markup))
        return {'message_id':len(self.sent)}
    def edit_message(self,user,mid,text,markup):
        self.edited.append((user,mid,text,markup))
        return {'message_id':mid}

class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.temp.name)/'db'); self.telegram=Telegram()
        self.allowed={1,2};self.panels=Panels(self.store,self.telegram,lambda u:u in self.allowed)
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def test_refresh_edits_the_same_message_and_coalesces_identical_content(self):
        self.panels.show(1,'first',view='status');self.panels.show(1,'first',view='status')
        self.panels.show(1,'changed',view='status')
        self.assertEqual(len(self.telegram.sent),1);self.assertEqual(len(self.telegram.edited),1)
        self.assertEqual(self.telegram.edited[0][1],1)
    def test_restart_keeps_panel_identity_and_user_panels_remain_separate(self):
        self.panels.show(1,'first'); self.panels.show(2,'second')
        Panels(self.store,self.telegram,lambda _:True).show(1,'after restart')
        self.assertEqual(len(self.telegram.sent),2);self.assertEqual(self.telegram.edited[0][0:2],(1,1))
    def test_revoked_user_does_not_receive_updates(self):
        self.panels.show(1,'first');self.allowed.remove(1);self.panels.show(1,'private')
        self.assertEqual(len(self.telegram.sent),1);self.assertFalse(self.telegram.edited)
    def test_explicit_refresh_verifies_identical_panel_and_recreates_deleted_message(self):
        self.panels.show(1,'first')
        self.telegram.edit_message=lambda *a:(_ for _ in ()).throw(ApiError('http_400',http_status=400))
        self.panels.show(1,'first',force=True)
        self.assertEqual(len(self.telegram.sent),2)

    def test_deleted_panel_is_replaced_but_network_errors_do_not_duplicate_it(self):
        self.panels.show(1,'first')
        self.telegram.edit_message=lambda *a:(_ for _ in ()).throw(ApiError('http_400',http_status=400))
        self.panels.show(1,'new');self.assertEqual(len(self.telegram.sent),2)
        self.telegram.edit_message=lambda *a:(_ for _ in ()).throw(ApiError('network'))
        with self.assertRaises(ApiError):self.panels.show(1,'different')
        self.assertEqual(len(self.telegram.sent),2)

class AsyncPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'db');self.telegram=Telegram()
        self.allowed={1,2};self.panels=Panels(self.store,self.telegram,lambda u:u in self.allowed)
        self.panels.enable_async()
    def tearDown(self):
        self.panels.close();self.store.close();self.temp.cleanup()
    def test_pending_frames_coalesce_without_losing_a_fresh_reply_anchor(self):
        self.panels.show(1,'old');self.panels.flush_pending()
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'checking',view='status')
        record=self.panels.records()['1']
        self.panels.show(1,'newest background state',view='status',expected=record['revision'])
        self.assertEqual(len(self.panels._pending),1)
        self.panels.flush_pending()
        self.assertEqual([text for _,text,_ in self.telegram.sent],['old','newest background state'])
        self.assertEqual(self.telegram.edited,[])
        self.assertEqual(self.panels.records()['1']['message_id'],2)
    def test_inflight_fresh_send_result_is_adopted_by_a_newer_frame_of_same_interaction(self):
        import threading
        entered,release=threading.Event(),threading.Event();send=self.telegram.send_message
        def blocked(*args):entered.set();self.assertTrue(release.wait(2));return send(*args)
        self.telegram.send_message=blocked
        with self.panels.interaction(1,fresh=True):
            self.panels.show(1,'first')
            worker=threading.Thread(target=self.panels.flush_pending);worker.start()
            try:
                self.assertTrue(entered.wait(1));self.panels.show(1,'second')
            finally:release.set();worker.join(2)
        self.assertEqual(len(self.telegram.sent),1)
        self.assertEqual(self.telegram.edited[-1][1:3],(1,'second'))
        self.assertEqual(self.panels.records()['1']['message_id'],1)
    def test_distinct_explicit_interaction_retains_its_own_fresh_reply(self):
        import threading
        entered,release=threading.Event(),threading.Event();send=self.telegram.send_message
        def blocked(*args):entered.set();self.assertTrue(release.wait(2));return send(*args)
        self.telegram.send_message=blocked
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'first')
        worker=threading.Thread(target=self.panels.flush_pending);worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.panels.interaction(1,fresh=True):self.panels.show(1,'new explicit command')
        finally:release.set();worker.join(2)
        self.assertEqual(len(self.telegram.sent),2)
        self.assertEqual(self.panels.records()['1']['message_id'],2)
        self.assertFalse(self.telegram.edited)
    def test_admin_selection_during_failed_edit_prevents_fallback_and_rollback(self):
        import threading
        self.panels.show(1,'old');self.panels.flush_pending()
        entered,release=threading.Event(),threading.Event()
        def fail(*args):
            entered.set();self.assertTrue(release.wait(2));raise ApiError('http_400',http_status=400)
        self.telegram.edit_message=fail;self.panels.show(1,'stale refresh')
        worker=threading.Thread(target=self.panels.flush_pending);worker.start()
        try:
            self.assertTrue(entered.wait(1));self.panels.select(1,'admin')
        finally:release.set();worker.join(2)
        self.assertEqual(len(self.telegram.sent),1)
        self.assertEqual(self.panels.records()['1']['view'],'admin')
    def test_revocation_before_delivery_or_during_failed_edit_discards_ui(self):
        import threading
        self.panels.show(1,'pending');self.allowed.remove(1);self.panels.flush_pending()
        self.assertFalse(self.telegram.sent)
        self.allowed.add(1);self.panels.show(1,'old');self.panels.flush_pending()
        entered,release=threading.Event(),threading.Event()
        def fail(*args):
            entered.set();self.assertTrue(release.wait(2));raise ApiError('http_400',http_status=400)
        self.telegram.edit_message=fail;old_fingerprint=self.panels.records()['1']['fingerprint'];self.panels.show(1,'private update')
        worker=threading.Thread(target=self.panels.flush_pending);worker.start()
        try:self.assertTrue(entered.wait(1));self.allowed.remove(1)
        finally:release.set();worker.join(2)
        self.assertEqual(len(self.telegram.sent),1)
        self.assertEqual(self.panels.records()['1']['fingerprint'],old_fingerprint)
    def test_ambiguous_fresh_send_is_not_automatically_duplicated(self):
        sends=[]
        def accepted_but_timeout(*args):sends.append(args);raise ApiError('network')
        self.telegram.send_message=accepted_but_timeout
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'new panel',view='status')
        self.panels.flush_pending();record=self.panels.records()['1']
        self.assertTrue(record['delivery_unknown']);self.assertFalse(self.panels._pending)
        self.panels.show(1,'background repeat',view='status',expected=record['revision']);self.panels.flush_pending()
        self.assertEqual(len(sends),1)
        self.telegram.send_message=Telegram.send_message.__get__(self.telegram,Telegram)
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'explicit retry')
        self.panels.flush_pending();self.assertEqual(len(self.telegram.sent),1)
        self.assertFalse(self.panels.records()['1']['delivery_unknown'])
    def test_shutdown_discards_pending_and_does_not_publish_late_http_result(self):
        import threading
        entered,release=threading.Event(),threading.Event();send=self.telegram.send_message
        def blocked(*args):entered.set();self.assertTrue(release.wait(2));return send(*args)
        self.telegram.send_message=blocked
        self.panels.show(1,'pending');worker=threading.Thread(target=self.panels.flush_pending);worker.start()
        try:
            self.assertTrue(entered.wait(1));before=self.panels.records()['1'];self.panels.close()
        finally:release.set();worker.join(2)
        self.assertEqual(self.panels.records()['1'],before)
        self.assertFalse(self.panels._pending)
    def test_payload_is_copied_and_waiting_queue_is_bounded_per_user(self):
        markup={'inline_keyboard':[[{'text':'correct','callback_data':'n:home'}]]}
        self.panels.show(1,'first',markup);markup['inline_keyboard'][0][0]['text']='mutated'
        self.panels.flush_pending();self.assertEqual(self.telegram.sent[0][2]['inline_keyboard'][0][0]['text'],'correct')
        for index in range(100):self.panels.show(1,str(index))
        self.assertEqual(len(self.panels._pending),1)

    def test_429_cooldown_survives_coalescing_and_new_explicit_screen(self):
        now=[1000];self.panels.clock=lambda:now[0]
        calls=[];send=self.telegram.send_message
        def rate_limited(*args):
            calls.append(args)
            if len(calls)==1:raise ApiError('http_429',http_status=429,retry_after=30)
            return send(*args)
        self.telegram.send_message=rate_limited
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'first')
        self.panels.flush_pending()
        record=self.panels.records()['1']
        self.panels.show(1,'background',expected=record['revision']);self.panels.flush_pending()
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'new explicit')
        self.panels.flush_pending();self.assertEqual(len(calls),1)
        now[0]+=30;self.panels.flush_pending()
        self.assertEqual(len(calls),2)
        self.assertEqual(self.telegram.sent[-1][1],'new explicit')

    def test_429_from_inflight_old_screen_delays_an_already_queued_new_screen(self):
        import threading
        now=[1000];self.panels.clock=lambda:now[0]
        entered,release=threading.Event(),threading.Event();calls=[]
        def limited(*args):
            calls.append(args);entered.set();self.assertTrue(release.wait(2))
            raise ApiError('http_429',http_status=429,retry_after=30)
        self.telegram.send_message=limited
        with self.panels.interaction(1,fresh=True):self.panels.show(1,'first')
        worker=threading.Thread(target=self.panels.flush_pending);worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.panels.interaction(1,fresh=True):self.panels.show(1,'second')
        finally:release.set();worker.join(2)
        self.panels.flush_pending()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.panels._pending[1]['retry_at'],1030)
