import tempfile
import threading
import unittest
from pathlib import Path

from manager import Manager
from adapters import ApiError
from storage import Store


def gcode(nozzle=240,bed=70):
    return ('EXCLUDE_OBJECT_DEFINE NAME=model CENTER=110,110 POLYGON=[[100,100],[120,100],[120,120],[100,120],[100,100]]\n'
            f'START_PRINT EXTRUDER_TEMP={nozzle} BED_TEMP={bed}\nG90\nM83\nG1 X110 Y110 E0.1\n')

class Telegram:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.callbacks = []
        self.edits = []

    def send_message(self, user, text, markup=None):
        self.messages.append((user, text, markup))
        return {"message_id": len(self.messages)}

    def edit_message(self,user,message_id,text,markup=None):
        self.edits.append((user,message_id,text,markup))
        self.messages.append((user,text,markup))
        return {'message_id':message_id}

    def send_photo(self, user, caption, photo, markup=None):
        self.photos.append((user, caption, photo, markup))
        return {"message_id": len(self.photos)}

    def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))

    def download_file(self, file_id, destination, max_bytes=20 * 1024 * 1024):
        Path(destination).write_text(gcode())
        return Path(destination)


class Printer:
    def __init__(self):
        self.state = "printing"
        self.job_id = "job-1"
        self.filename = "part.gcode"
        self.calls = []
        self.remote = {}
        self.upload_calls = []

    def status(self):
        return {"webhooks": {"state": "ready"}, "print_stats": {"state": self.state, "filename": self.filename, "print_duration": 180, "total_duration": 200}, "virtual_sdcard": {"progress": .5}, "extruder": {"temperature": 245, "target": 245}, "heater_bed": {"temperature": 80, "target": 80}}

    def history(self, limit=10):
        return [{"job_id": self.job_id, "filename": self.filename, "status": "in_progress" if self.state in {"printing", "paused"} else "completed", "start_time": 900}]

    def pause(self):
        self.calls.append("pause")
        self.state = "paused"

    def resume(self):
        self.calls.append("resume")
        self.state = "printing"

    def cancel(self):
        self.calls.append("cancel")
        self.state = "cancelled"

    def start(self, filename):
        self.calls.append(("start", filename))
        self.filename = filename
        self.state = "printing"

    def photo(self):
        return b"\xff\xd8test-jpeg"

    def files(self, limit=12):
        return [{"path": "part.gcode", "size": 100, "modified": 1}]

    def metadata(self, filename):
        return {"estimated_time": 360, "size": 100}

    def upload(self,filename,path,progress=None):
        content=Path(path).read_bytes();self.remote['telegram/'+filename]=content
        self.upload_calls.append(filename)
        if progress:progress(len(content),len(content))
        return 'telegram/'+filename

    def download_gcode(self,filename,destination):
        Path(destination).write_bytes(self.remote.get(filename,gcode().encode()))
        return Path(destination)

    def start_calibrated(self,filename):
        self.job_id='calibrated-job';self.start(filename)

    def set_temperature(self,heater,target):
        self.calls.append(('temperature',heater,target))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.telegram = Telegram()
        self.printer = Printer()
        self.now = 1000
        self.config = dict({'owner_id':10001}, **{"allowed_user_ids": [10001, 22, 33], "state_dir": self.temp.name})
        self.manager = Manager(self.config, self.store, self.printer, self.telegram, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def update(self, text, user=10001, update_id=1, date=None):
        return {"update_id": update_id, "message": {"message_id": update_id, "date": self.now if date is None else date, "from": {"id": user, "first_name": "Owner"}, "chat": {"id": user, "type": "private"}, "text": text}}

    def callback(self, data, update_id=100, user=10001):
        return {"update_id": update_id, "callback_query": {"id": str(update_id), "from": {"id": user, "first_name": "Owner"}, "message": {"chat": {"id": user, "type": "private"}}, "data": data}}

    def button(self, text):
        for _, _, markup in reversed(self.telegram.messages):
            for row in (markup or {}).get("inline_keyboard", []):
                for button in row:
                    if text in button["text"]:
                        return button["callback_data"]
        self.fail("button not found: " + text)

    def test_unknown_control_opens_access_gate_without_controlling_printer(self):
        self.manager.handle_update(self.update("/pause", user=99))
        self.manager.flush_outbox()
        self.assertEqual(self.printer.calls, [])
        self.assertTrue(self.telegram.messages)
        self.assertIn("доступ", self.telegram.messages[-1][1].lower())

    def test_pause_is_not_replayed_after_restart(self):
        update = self.update("/pause")
        self.manager.handle_update(update)
        restarted = Manager(self.config, self.store, self.printer, self.telegram, clock=lambda: self.now)
        self.printer.state = "printing"
        restarted.handle_update(update)
        restarted.flush_outbox()
        self.assertEqual(self.printer.calls, ["pause"])
        self.assertEqual({m[0] for m in self.telegram.messages}, {10001, 22, 33})

    def test_stale_control_is_rejected_but_status_still_works(self):
        self.manager.handle_update(self.update("/pause", date=939))
        self.manager.handle_update(self.update("/status", update_id=2, date=900))
        self.assertEqual(self.printer.calls, [])
        self.assertIn("старше минуты", self.telegram.messages[0][1])
        self.assertIn("245", self.telegram.messages[-1][1])

    def test_free_text_questions_never_issue_physical_commands(self):
        self.manager.handle_update(self.update("а можно поставить на паузу?"))
        self.assertEqual(self.printer.calls, [])

    def test_resume_requires_the_printer_to_be_paused(self):
        self.manager.handle_update(self.update("/resume"))
        self.assertEqual(self.printer.calls, [])
        self.printer.state = "paused"
        self.manager.handle_update(self.update("/resume", update_id=2))
        self.assertEqual(self.printer.calls, ["resume"])

    def test_cancel_requires_confirmation_bound_to_the_same_job(self):
        self.manager.handle_update(self.update("/cancel"))
        self.assertEqual(self.printer.calls, [])
        confirm = self.button("Да, отменить")
        self.printer.job_id = "job-2"
        self.manager.handle_update(self.callback(confirm))
        self.assertEqual(self.printer.calls, [])
        self.assertIn("Задание изменилось", self.telegram.messages[-1][1])

    def test_unauthorized_callback_cannot_use_an_owner_confirmation(self):
        self.manager.handle_update(self.update("/cancel"))
        confirm = self.button("Да, отменить")
        self.manager.handle_update(self.callback(confirm, user=99))
        self.assertEqual(self.printer.calls, [])
        self.manager.handle_update(self.callback(confirm, update_id=101))
        self.assertEqual(self.printer.calls, ["cancel"])

    def test_control_timeout_is_not_retried(self):
        def uncertain():
            self.printer.calls.append("pause")
            raise ApiError("network", uncertain=True)
        self.printer.pause = uncertain
        update = self.update("/pause")
        self.manager.handle_update(update)
        self.manager.handle_update(update)
        self.assertEqual(self.printer.calls, ["pause"])
        self.assertIn("не подтверждён", self.telegram.messages[-1][1])

    def test_file_selection_does_not_start_until_plate_confirmation(self):
        self.printer.state = "standby"
        self.manager.handle_update(self.update("/files"))
        self.manager.handle_update(self.callback(self.button("part.gcode")))
        self.manager.handle_update(self.callback(self.button("Подготовить к печати"), update_id=101))
        self.assertEqual(self.printer.calls, [])
        self.manager.process_slicing_job()
        asset_id=next(iter(self.store.get('assets')))
        self.manager._file_card(10001,asset_id)
        confirm = self.button("Стол пуст — печатать")
        self.manager.handle_update(self.callback(confirm, update_id=102))
        self.manager.handle_update(self.callback(confirm, update_id=103))
        self.manager.preflight.tick()
        self.assertEqual(len(self.printer.calls),1)
        self.assertEqual(self.printer.calls[0][0],'start')

    def test_new_print_prevents_a_previously_confirmed_idle_start(self):
        self.printer.state = "standby"
        self.manager.handle_update(self.update("/files"))
        self.manager.handle_update(self.callback(self.button("part.gcode")))
        self.manager.handle_update(self.callback(self.button("Подготовить к печати"), update_id=101))
        self.manager.process_slicing_job()
        self.manager._file_card(10001,next(iter(self.store.get('assets'))))
        confirm=self.button('Стол пуст — печатать')
        self.printer.state = "printing"
        self.manager.handle_update(self.callback(confirm, update_id=102))
        self.assertEqual(self.printer.calls, [])

    def test_received_file_is_saved_and_waits_for_a_chosen_action(self):
        update = self.update("")
        update["message"]["document"] = {"file_id": "telegram-file", "file_name": "case.gcode", "file_size": 4}
        self.manager.handle_update(update)
        self.assertEqual(self.printer.calls, [])
        self.assertEqual(list((Path(self.temp.name)/'uploads').iterdir()),[])
        self.assertEqual(next(iter(self.store.get('slicing_jobs').values()))['status'],'queued')
        self.manager.process_slicing_job()
        self.manager._file_card(10001,next(iter(self.store.get('assets'))))
        self.assertIsNotNone(self.button("В очередь"))

    def test_large_file_uses_the_existing_orca_route_instead_of_partial_download(self):
        update = self.update("")
        update["message"]["document"] = {"file_id": "large", "file_name": "case.gcode", "file_size": 21 * 1024 * 1024}
        self.manager.handle_update(update)
        self.assertEqual(list((Path(self.temp.name) / "uploads").iterdir()), [])
        self.assertIn("OrcaSlicer", self.telegram.messages[-1][1])

    def test_queue_survives_restart_and_never_auto_starts_after_completion(self):
        self.manager.handle_update(self.update("/files"))
        self.manager.handle_update(self.callback(self.button("part.gcode")))
        self.manager.handle_update(self.callback(self.button("В очередь"), update_id=101))
        restarted = Manager(self.config, self.store, self.printer, self.telegram, clock=lambda: self.now)
        restarted.tick()
        self.printer.state = "complete"
        restarted.tick()
        self.assertEqual(self.printer.calls, [])
        restarted.handle_update(self.update("/queue", update_id=102))
        self.assertIn("1. part.gcode", self.telegram.messages[-1][1])

    def test_power_off_while_printing_requires_confirmation_but_heat_does_not_block(self):
        class Power:
            def __init__(self):
                self.calls = []
            def status(self):
                return {"configured": True, "on": True}
            def set(self, on):
                self.calls.append(on)
        power = Power()
        manager = Manager(self.config, self.store, self.printer, self.telegram, power=power, clock=lambda: self.now)
        manager.handle_update(self.update("/power"))
        manager.handle_update(self.callback(self.button("Выключить")))
        self.assertEqual(power.calls, [])
        manager.handle_update(self.callback(self.button("Да, выключить"), update_id=101))
        self.assertEqual(power.calls, [False])
        self.printer.state = "standby"
        manager.handle_update(self.update("/power", update_id=102))
        manager.handle_update(self.callback(self.button("Выключить"), update_id=103))
        self.assertEqual(power.calls, [False, False])

    def test_missing_socket_explains_unavailable_instead_of_claiming_success(self):
        self.manager.handle_update(self.update("/power"))
        self.manager.handle_update(self.callback(self.button("Включить")))
        self.assertIn("ещё не подключена", self.telegram.messages[-1][1])

    def test_photo_delivery_failure_identifies_telegram_instead_of_printer(self):
        self.telegram.send_photo = lambda *args, **kwargs: (_ for _ in ()).throw(ApiError("network"))
        self.manager.handle_update(self.update("/photo"))
        self.assertIn("Telegram", self.telegram.messages[-1][1])
        self.assertNotIn("K1C сейчас недоступен", self.telegram.messages[-1][1])

    def test_photo_source_failure_identifies_camera_and_offers_retry(self):
        self.printer.photo = lambda: (_ for _ in ()).throw(ApiError("network"))
        self.manager.handle_update(self.update("/photo"))
        self.assertIn("Камера", self.telegram.messages[-1][1])
        self.assertIsNotNone(self.button("Повторить"))

    def test_photo_cache_reuses_only_recent_success_and_never_serves_stale_on_failure(self):
        calls = []
        def capture():
            calls.append(1)
            return b"image-" + str(len(calls)).encode()
        self.printer.photo = capture
        first = self.manager._photo()
        self.assertEqual(self.manager._photo(), first)
        self.assertEqual(len(calls), 1)
        self.now += 3
        self.assertNotEqual(self.manager._photo(), first)
        self.now += 3
        self.printer.photo = lambda: (_ for _ in ()).throw(ApiError("network"))
        with self.assertRaises(ApiError):
            self.manager._photo()

    def test_photo_requests_coalesce_while_status_of_another_owner_is_responsive(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def capture():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return b"image"
        self.printer.photo = capture
        photo_worker = threading.Thread(target=lambda: self.manager.handle_update(self.update("/photo")))
        status_worker = threading.Thread(target=lambda: self.manager.handle_update(self.update("/status", user=22, update_id=2)))
        photo_worker.start()
        try:
            self.assertTrue(entered.wait(1))
            status_worker.start()
            status_worker.join(.5)
            self.assertFalse(status_worker.is_alive(), "status blocked behind photo capture")
        finally:
            release.set()
            photo_worker.join(2)
            status_worker.join(2)

    def test_concurrent_photo_consumers_share_one_camera_fetch(self):
        entered, release = threading.Event(), threading.Event()
        calls, results = [], []
        def capture():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return b"one-image"
        self.printer.photo = capture
        workers = [threading.Thread(target=lambda: results.append(self.manager._photo())) for _ in range(2)]
        workers[0].start()
        try:
            self.assertTrue(entered.wait(1))
            workers[1].start()
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
        self.assertEqual(calls, [1])
        self.assertEqual(results, [b"one-image", b"one-image"])

    def test_status_avoids_history_roundtrip_but_controls_still_require_fresh_history(self):
        calls = []
        history = self.printer.history
        self.printer.history = lambda limit=10: calls.append(limit) or history(limit)
        self.manager.handle_update(self.update("/status"))
        self.assertEqual(calls, [])
        self.manager.handle_update(self.update("/pause", update_id=2))
        self.assertEqual(calls, [1])

    def test_light_invalidates_camera_frame_without_waiting_for_pending_photo(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def capture():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return b"image"
        self.printer.photo = capture
        self.printer.set_light = lambda on: self.printer.calls.append(("light", on))
        photo_worker = threading.Thread(target=self.manager._photo)
        light_worker = threading.Thread(target=lambda: (self.manager.handle_update(self.update("/light_on")), done.set()))
        photo_worker.start()
        try:
            self.assertTrue(entered.wait(1))
            light_worker.start()
            self.assertTrue(done.wait(.5), "light command waited for camera capture")
        finally:
            release.set()
            photo_worker.join(2)
            light_worker.join(2)
        self.manager._photo()
        self.assertEqual(calls, [1, 1], "frame captured before light change must not be reused")

    def test_transition_notification_camera_never_blocks_owner_controls(self):
        self.manager.tick()
        self.printer.state = "paused"
        entered, release = threading.Event(), threading.Event()
        def capture():
            entered.set()
            self.assertTrue(release.wait(2))
            return b"image"
        self.printer.photo = capture
        monitor = threading.Thread(target=self.manager.tick)
        control = threading.Thread(target=lambda: self.manager.handle_update(self.update("/resume")))
        monitor.start()
        try:
            self.assertTrue(entered.wait(1))
            control.start()
            control.join(.5)
            self.assertFalse(control.is_alive(), "owner control waited for transition snapshot")
            self.assertEqual(self.printer.calls, ["resume"])
        finally:
            release.set()
            monitor.join(2)
            control.join(2)
        # The camera completed after the owner changed the state: do not attach
        # that later frame to the old paused-state event.
        transitions = [item for item in self.store.pending() if "Печать на паузе" in item["text"]]
        self.assertEqual(len(transitions), 3)
        self.assertTrue(all(item["photo"] is None for item in transitions))

    def test_light_requires_fresh_explicit_command_and_is_not_replayed(self):
        self.printer.light_status = lambda: False
        self.printer.set_light = lambda on: self.printer.calls.append(("light", on))
        self.manager.handle_update(self.update("можно включить свет?"))
        self.assertEqual(self.printer.calls, [])
        self.manager.handle_update(self.update("/light_on", update_id=2, date=939))
        self.assertEqual(self.printer.calls, [])
        update = self.update("/light_on", update_id=3)
        self.manager.handle_update(update)
        self.manager.handle_update(update)
        self.assertEqual(self.printer.calls, [("light", True)])
        self.assertTrue(any(item["action"] == "light_on" and item["result"] == "accepted" for item in self.store.recent_audit()))

    def test_light_buttons_are_owner_bound_and_expire(self):
        self.printer.light_status = lambda: False
        self.printer.set_light = lambda on: self.printer.calls.append(("light", on))
        self.manager.handle_update(self.update("/light"))
        on = self.button("Включить свет")
        self.manager.handle_update(self.callback(on, user=99))
        self.assertEqual(self.printer.calls, [])
        self.now += 61
        self.manager.handle_update(self.callback(on, update_id=101))
        self.assertEqual(self.printer.calls, [])
        self.manager.handle_update(self.update("/light", update_id=102))
        self.manager.handle_update(self.callback(self.button("Включить свет"), update_id=103))
        self.assertEqual(self.printer.calls, [("light", True)])

    def test_failed_callback_ack_does_not_discard_light_action(self):
        self.printer.light_status = lambda: False
        self.printer.set_light = lambda on: self.printer.calls.append(("light", on))
        self.manager.handle_update(self.update("/light"))
        self.telegram.answer_callback = lambda *args, **kwargs: (_ for _ in ()).throw(ApiError("network"))
        self.manager.handle_update(self.callback(self.button("Включить свет")))
        self.assertEqual(self.printer.calls, [("light", True)])

    def test_uncertain_light_command_is_audited_and_never_replayed(self):
        def set_light(on):
            self.printer.calls.append(("light", on))
            raise ApiError("network", uncertain=True)
        self.printer.set_light = set_light
        update = self.update("/light_off")
        self.manager.handle_update(update)
        self.manager.handle_update(update)
        self.assertEqual(self.printer.calls, [("light", False)])
        self.assertEqual(self.store.recent_audit(1)[0]["result"], "uncertain")
        self.assertIn("не подтверждён", self.telegram.messages[-1][1])

    def test_unconfirmed_light_action_offers_light_readback(self):
        self.printer.set_light = lambda on: (_ for _ in ()).throw(ApiError("light_unconfirmed", uncertain=True))
        self.manager.handle_update(self.update("/light_on"))
        self.assertIn("света не подтверждён", self.telegram.messages[-1][1])
        self.assertEqual(self.button("Проверить свет"), "n:light")

    def test_light_is_not_executed_after_shutdown(self):
        self.printer.set_light = lambda on: self.printer.calls.append(("light", on))
        self.manager.stopping = True
        self.manager.handle_update(self.update("/light_on"))
        self.assertEqual(self.printer.calls, [])

    def test_only_the_immutable_owner_sees_admin_controls(self):
        self.manager.handle_update(self.update("/start"))
        owner_keyboard = self.telegram.messages[-1][2]
        self.assertTrue(any(button['text']=="👑 Админка" for row in owner_keyboard['inline_keyboard'] for button in row))
        self.manager.handle_update(self.update("/start", user=22, update_id=2))
        user_keyboard = self.telegram.messages[-1][2]
        self.assertFalse(any(button['text']=="👑 Админка" for row in user_keyboard['inline_keyboard'] for button in row))
        self.assertEqual(self.manager.owners, {10001})
        self.manager.handle_update(self.update("/pause", user=22, update_id=3))
        self.assertEqual(self.printer.calls, ["pause"], "approved users retain print control")

    def test_ordinary_users_cannot_change_power_or_ai_settings(self):
        self.manager.handle_update(self.update("/power", user=22))
        self.assertNotIn("Выключить", str(self.telegram.messages[-1][2]))
        self.manager._power_action("power_on", {"_expires": self.now + 60}, 22, "User", "denied")
        self.assertIn("только владелец", self.telegram.messages[-1][1])
        self.manager._detector_toggle(True, 22, "User", self.now + 60)
        self.assertFalse(self.store.get("detector_auto_enabled", False))
        self.assertIn("только владелец", self.telegram.messages[-1][1])

    def test_revoke_during_printer_check_prevents_physical_command(self):
        revoked = []
        allowed = self.manager.access.is_allowed
        self.manager.access.is_allowed = lambda user: allowed(user) and not (user == 22 and revoked)
        status = self.printer.status
        def revoke_during_status():
            revoked.append(True)
            return status()
        self.printer.status = revoke_during_status
        self.manager.handle_update(self.update("/pause", user=22))
        self.assertEqual(self.printer.calls, [])

    def test_queued_notifications_are_not_delivered_after_revocation(self):
        self.manager.notify("Private printer event", "test-revoke")
        allowed = self.manager.access.is_allowed
        self.manager.access.is_allowed = lambda user: allowed(user) and user != 22
        self.manager.flush_outbox()
        self.assertEqual({user for user, _, _ in self.telegram.messages}, {10001, 33})

    def test_unknown_access_request_reaches_only_owner_then_approval_enables_print_control(self):
        self.manager.handle_update(self.update("/pause", user=99))
        self.assertEqual(self.printer.calls, [])
        self.manager.handle_update(self.callback(self.button("Запросить доступ"), user=99))
        self.manager.flush_outbox()
        requests = [(user, text) for user, text, _ in self.telegram.messages if "Запрос доступа" in text]
        self.assertEqual({user for user, _ in requests}, {10001})
        self.manager.handle_update(self.callback(self.button("Разрешить"), update_id=101))
        self.manager.handle_update(self.update("/pause", user=99, update_id=102))
        self.assertEqual(self.printer.calls, ["pause"])
        self.assertTrue(self.manager.access.is_allowed(99))
        restarted = Manager(self.config, self.store, self.printer, self.telegram, clock=lambda: self.now)
        self.assertTrue(restarted.access.is_allowed(99))
        self.assertEqual(restarted.owners, {10001})

    def test_actual_access_revocation_disables_old_light_ticket_immediately(self):
        self.printer.light_status = lambda: False
        self.printer.set_light = lambda on: self.printer.calls.append(("light", on))
        self.manager.handle_update(self.update("/light", user=22))
        light = self.button("Включить свет")
        self.manager.handle_update(self.callback("x:user:22", update_id=100))
        revoke = self.button("Отозвать доступ")
        self.manager.handle_update(self.callback(revoke, update_id=101))
        self.assertFalse(self.manager.access.is_allowed(22))
        self.manager.handle_update(self.callback(light, user=22, update_id=102))
        self.assertEqual(self.printer.calls, [])

    def slicing_manager(self):
        class Slicer:
            configured = True
            def __init__(self):
                self.calls = []
            def slice(self, source, destination, temperatures=None):
                self.calls.append((source, destination))
                self.temperatures=dict(temperatures or {'nozzle':240,'bed':70})
                Path(destination).write_text(gcode(**self.temperatures))
                return {"path": str(destination), "size": Path(destination).stat().st_size,
                        "estimated_time": 720, "filament_grams": 8.5, "material": "PETG"}
        slicer = Slicer()
        manager = Manager(self.config, self.store, self.printer, self.telegram,
                          slicer=slicer, clock=lambda: self.now)
        return manager, slicer

    def stl_update(self, update_id=1, user=10001, size=80, name="case.stl"):
        update = self.update("", user=user, update_id=update_id)
        update["message"]["document"] = {"file_id": "telegram-stl", "file_name": name, "file_size": size}
        return update

    def test_stl_is_prepared_in_worker_and_never_starts_automatically(self):
        manager, slicer = self.slicing_manager()
        self.printer.state = "standby"
        manager.handle_update(self.stl_update())
        self.assertEqual(slicer.calls, [])
        self.assertTrue(manager.process_slicing_job())
        manager.flush_outbox()
        self.assertEqual(self.printer.calls, [])
        self.assertEqual(len(slicer.calls), 1)
        asset_id=next(iter(self.store.get('assets')))
        manager._file_card(10001,asset_id)
        self.assertIn("PETG", self.telegram.messages[-1][1])
        self.assertIn("12 мин", self.telegram.messages[-1][1])
        self.assertIn("8.5 г", self.telegram.messages[-1][1])
        self.assertIsNotNone(self.button("Стол пуст — печатать"))
        self.assertEqual({user for user, text, _ in self.telegram.messages if "готов к печати" in text}, {10001})
        manager.handle_update(self.callback(self.button("Стол пуст — печатать")))
        self.assertEqual(self.printer.calls,[])
        manager.preflight.tick()
        self.assertEqual(len(self.printer.calls), 1)
        self.assertEqual(self.printer.calls[0][0], "start")

    def test_stl_download_and_slicing_do_not_block_status_or_actions(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update())
        entered, release = threading.Event(), threading.Event()
        def download(file_id, destination, max_bytes=20 * 1024 * 1024):
            entered.set()
            self.assertTrue(release.wait(2))
            Path(destination).write_text("solid test")
        self.telegram.download_file = download
        worker = threading.Thread(target=manager.process_slicing_job)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            manager.handle_update(self.update("/status", update_id=2))
            manager.handle_update(self.update("/pause", update_id=3))
            self.assertEqual(self.printer.calls, ["pause"])
        finally:
            release.set()
            worker.join(2)

    def test_stl_duplicate_update_and_restart_job_are_not_duplicated(self):
        manager, slicer = self.slicing_manager()
        update = self.stl_update()
        manager.handle_update(update)
        manager.handle_update(update)
        jobs = self.store.get("slicing_jobs")
        self.assertEqual(len(jobs), 1)
        job_id = next(iter(jobs))
        jobs[job_id]["status"] = "running"
        self.store.set("slicing_jobs", jobs)
        restarted = Manager(self.config, self.store, self.printer, self.telegram,
                            slicer=slicer, clock=lambda: self.now)
        self.assertEqual(self.store.get("slicing_jobs")[job_id]["status"], "queued")
        self.assertTrue(restarted.process_slicing_job())
        self.assertFalse(restarted.process_slicing_job())
        self.assertEqual(len(slicer.calls), 1)
        self.assertEqual(self.printer.calls, [])

    def test_stl_rejects_unauthed_oversize_and_full_queue_before_downloading(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update(user=99))
        manager.handle_update(self.stl_update(update_id=2, size=21 * 1024 * 1024))
        self.assertEqual(self.store.get("slicing_jobs", {}), {})
        for identity in range(3, 12):
            manager.handle_update(self.stl_update(update_id=identity))
        self.assertEqual(len(self.store.get("slicing_jobs")), 8)
        self.assertEqual(slicer.calls, [])
        self.assertEqual(list((Path(self.temp.name) / "uploads").iterdir()), [])
        self.assertIn("заполнена", self.telegram.messages[-1][1])

    def test_stl_failure_retains_failed_job_and_only_notifies_requester(self):
        manager, slicer = self.slicing_manager()
        slicer.slice = lambda *args,**kwargs: (_ for _ in ()).throw(ApiError("slicer_invalid_stl"))
        manager.handle_update(self.stl_update())
        self.assertTrue(manager.process_slicing_job())
        manager.flush_outbox()
        self.assertEqual(next(iter(self.store.get("slicing_jobs").values()))["status"], "failed")
        self.assertEqual(self.printer.calls, [])
        self.assertEqual({user for user, text, _ in self.telegram.messages if "Не удалось подготовить файл" in text}, {10001})

    def test_stl_busy_printer_offers_queue_and_one_click_rechecks_state(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update())
        manager.process_slicing_job()
        manager.flush_outbox()
        manager._file_card(10001,next(iter(self.store.get('assets'))))
        self.assertIn("занят", self.telegram.messages[-1][1])
        self.assertIsNotNone(self.button("В очередь"))
        asset_id = next(iter(self.store.get("assets")))
        self.printer.state = "standby"
        manager._file_card(10001, asset_id)
        start = self.button("Стол пуст — печатать")
        self.printer.state = "printing"
        manager.handle_update(self.callback(start))
        self.assertEqual(self.printer.calls, [])

    def test_revocation_during_slicing_never_publishes_asset_or_ready_notice(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update(user=22))
        revoked = []
        allowed = manager.access.is_allowed
        manager.access.is_allowed = lambda user: allowed(user) and not (user == 22 and revoked)
        slice_model = slicer.slice
        def revoke_after_slice(source, destination, temperatures=None):
            result = slice_model(source, destination,temperatures=temperatures)
            revoked.append(True)
            return result
        slicer.slice = revoke_after_slice
        manager.process_slicing_job()
        manager.flush_outbox()
        self.assertFalse(any(a.get('ready') for a in self.store.get('assets',{}).values()))
        self.assertEqual(next(iter(self.store.get("slicing_jobs").values()))["status"], "failed")
        self.assertFalse(any("PETG · готово" in text for _, text, _ in self.telegram.messages))

    def test_slicer_busy_is_deferred_across_restart_then_finishes_without_redownload(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update())
        calls, downloads = [], []
        download = self.telegram.download_file
        self.telegram.download_file = lambda *args, **kwargs: (downloads.append(1), download(*args, **kwargs))[1]
        successful = slicer.slice
        def busy_then_ready(source, destination, temperatures=None):
            calls.append(1)
            if len(calls) == 1:
                raise ApiError("slicer_busy", http_status=503)
            return successful(source, destination,temperatures=temperatures)
        slicer.slice = busy_then_ready
        self.assertTrue(manager.process_slicing_job())
        job_id, job = next(iter(self.store.get("slicing_jobs").items()))
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["next_attempt"], self.now + 5)
        self.assertFalse(manager.process_slicing_job(), "future retry must not spin")
        restarted = Manager(self.config, self.store, self.printer, self.telegram, slicer=slicer, clock=lambda: self.now)
        self.assertFalse(restarted.process_slicing_job(), "restart must preserve retry deadline")
        self.now += 5
        self.assertTrue(restarted.process_slicing_job())
        self.assertEqual(self.store.get("slicing_jobs")[job_id]["status"], "ready")
        self.assertEqual(downloads, [1])
        self.assertEqual(calls, [1, 1])
        self.assertFalse((manager.uploads / (job_id + ".stl")).exists())
        asset=next(iter(self.store.get('assets').values()))
        self.assertTrue(Path(asset['local_path']).exists())
        self.assertTrue(Path(asset['source_path']).exists(),'STL retained for temperature edits')
        self.assertFalse(any("preparation-failed:" in item["event_key"] for item in self.store.pending()))

    def test_repeated_slicer_busy_has_bounded_backoff_and_one_terminal_failure(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update())
        calls = []
        def busy(*args,**kwargs):
            calls.append(1)
            raise ApiError("slicer_busy", http_status=503)
        slicer.slice = busy
        for attempt in range(12):
            self.assertTrue(manager.process_slicing_job())
            job = next(iter(self.store.get("slicing_jobs").values()))
            if attempt < 11:
                self.assertEqual(job["status"], "queued")
                self.assertGreater(job["next_attempt"], self.now)
                self.assertLessEqual(job["next_attempt"] - self.now, 30)
                self.assertFalse(manager.process_slicing_job())
                self.assertFalse(any(item["event_key"].startswith("preparation-failed:") for item in self.store.pending()))
                self.now = job["next_attempt"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["busy_attempts"], 12)
        self.assertEqual(len(calls), 12)
        failures = [item for item in self.store.pending() if item["event_key"].startswith("preparation-failed:")]
        self.assertEqual(len(failures), 1)
        self.assertEqual(list(manager.uploads.iterdir()), [])

    def test_invalid_stl_cleanup_removes_source_and_unregistered_output(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update())
        def bad_model(source, destination,temperatures=None):
            Path(destination).write_text("partial invalid output")
            raise ApiError("slicer_invalid_model")
        slicer.slice = bad_model
        manager.process_slicing_job()
        self.assertEqual(list(manager.uploads.iterdir()), [])
        self.assertFalse(any(a.get('ready') for a in self.store.get('assets',{}).values()))

    def test_revocation_during_download_skips_slice_and_cleans_source(self):
        manager, slicer = self.slicing_manager()
        manager.handle_update(self.stl_update(user=22))
        allowed, revoked = manager.access.is_allowed, []
        manager.access.is_allowed = lambda user: allowed(user) and not (user == 22 and revoked)
        def download_then_revoke(file_id, destination, max_bytes):
            Path(destination).write_text("downloaded model")
            revoked.append(True)
        self.telegram.download_file = download_then_revoke
        manager.process_slicing_job()
        self.assertEqual(slicer.calls, [])
        self.assertEqual(list(manager.uploads.iterdir()), [])
        self.assertEqual(next(iter(self.store.get("slicing_jobs").values()))["status"], "failed")

    def test_expired_stl_start_refreshes_card_without_starting(self):
        manager, slicer = self.slicing_manager()
        self.printer.state = "standby"
        manager.handle_update(self.stl_update())
        manager.process_slicing_job()
        manager.flush_outbox()
        manager._file_card(10001,next(iter(self.store.get('assets'))))
        stale = self.button("Стол пуст — печатать")
        self.now += 1801
        manager.handle_update(self.callback(stale))
        self.assertEqual(self.printer.calls, [])
        refreshed = self.button("Стол пуст — печатать")
        self.assertNotEqual(stale, refreshed)
        manager.handle_update(self.callback(refreshed, update_id=101))
        manager.preflight.tick()
        self.assertEqual(len(self.printer.calls), 1)

    def prepared_stl(self, user=10001):
        manager,slicer=self.slicing_manager()
        self.printer.state='standby'
        manager.handle_update(self.stl_update(user=user))
        manager.process_slicing_job()
        asset=next(iter(self.store.get('assets').values()))
        manager._file_card(user,asset['id'])
        return manager,slicer,asset

    def test_v3_slow_transfer_finishes_before_30_minute_confirmation_begins(self):
        manager,slicer=self.slicing_manager()
        self.printer.state='standby'
        upload=self.printer.upload
        def slow_upload(filename,path,progress=None):
            self.now+=1900
            returned=upload(filename,path,progress=progress)
            self.printer.remote[returned]='; Moonraker метаданные\n'.encode()+self.printer.remote[returned]
            return returned
        self.printer.upload=slow_upload
        manager.handle_update(self.stl_update())
        manager.process_slicing_job()
        asset=next(iter(self.store.get('assets').values()))
        self.assertTrue(asset['ready'])
        self.assertEqual(asset['ready_at'],self.now)
        remote=self.printer.remote[asset['ready_filename']]
        self.assertTrue(remote[asset['gate']['start']:asset['gate']['end']].startswith(b'M25 ; K1C_BOT_GATE_'))
        manager._file_card(10001,asset['id'])
        button=self.button('Стол пуст — печатать')
        self.assertEqual(self.store.get('tickets')[button[2:]]['expires']-self.now,1800)
        upload_count=len(self.printer.upload_calls)
        self.now+=1700
        manager.handle_update(self.callback(button))
        self.assertEqual(len(self.printer.upload_calls),upload_count,'no file transfer after confirmation')
        self.assertEqual(manager.preflight.status()['phase'],'accepted')
        self.assertEqual(self.printer.calls,[],'physical start runs in the dedicated worker')
        manager.preflight.tick()
        self.assertEqual(self.printer.calls[0][0],'start')

    def test_v3_background_upload_does_not_hold_status_or_control_lock(self):
        manager,slicer=self.slicing_manager()
        manager.handle_update(self.stl_update())
        entered,release=threading.Event(),threading.Event()
        upload=self.printer.upload
        def blocked(filename,path,progress=None):
            entered.set();self.assertTrue(release.wait(2));return upload(filename,path,progress=progress)
        self.printer.upload=blocked
        worker=threading.Thread(target=manager.process_slicing_job);worker.start()
        try:
            self.assertTrue(entered.wait(1))
            manager.handle_update(self.update('/status',update_id=2))
            manager.handle_update(self.update('/pause',update_id=3))
            self.assertEqual(self.printer.calls,['pause'])
        finally:release.set();worker.join(2)

    def test_v3_imported_gcode_preserves_targets_and_missing_start_never_uploads(self):
        def download(file_id,destination,max_bytes):
            Path(destination).write_text(gcode(235,65) if file_id=='good' else 'G1 X10 E1\n')
        self.telegram.download_file=download
        good=self.stl_update(name='part.gcode');good['message']['document']['file_id']='good'
        self.manager.handle_update(good);self.manager.process_slicing_job()
        asset=next(iter(self.store.get('assets').values()))
        self.assertEqual((asset['gate']['nozzle'],asset['gate']['bed']),(235,65))
        self.assertIn(b'START_PRINT EXTRUDER_TEMP=235 BED_TEMP=65',self.printer.remote[asset['ready_filename']])
        bad=self.stl_update(update_id=2,name='unsafe.gcode');bad['message']['document']['file_id']='bad'
        self.manager.handle_update(bad);count=len(self.printer.upload_calls);self.manager.process_slicing_job()
        self.assertEqual(len(self.printer.upload_calls),count)
        failed=[a for a in self.store.get('assets').values() if a['source_name']=='unsafe.gcode'][0]
        self.assertFalse(failed['ready'])
        self.manager._file_card(10001,failed['id'])
        self.assertIn('START_PRINT',self.telegram.messages[-1][1])

    def test_v3_stl_temperature_edit_invalidates_start_and_reuses_original_source(self):
        manager,slicer,asset=self.prepared_stl()
        stale=self.button('Стол пуст — печатать')
        original=Path(asset['source_path']).read_bytes()
        downloaded=self.telegram.download_file
        self.telegram.download_file=lambda *a,**k:(_ for _ in ()).throw(AssertionError('STL should be retained'))
        manager._set_model_temperature(22,{'asset_id':asset['id'],'version':asset['version'],'heater':'nozzle','delta':5})
        updated=self.store.get('assets')[asset['id']]
        self.assertEqual(updated['version'],asset['version']+1);self.assertFalse(updated['ready'])
        manager.handle_update(self.callback(stale))
        self.assertEqual(self.printer.calls,[]);self.assertFalse(manager.preflight.busy())
        self.now+=2;manager.process_slicing_job()
        current=self.store.get('assets')[asset['id']]
        self.assertTrue(current['ready']);self.assertEqual(current['gate']['nozzle'],245)
        self.assertNotEqual(current['ready_filename'],asset['ready_filename'])
        self.assertEqual(Path(current['source_path']).read_bytes(),original)
        self.assertEqual(slicer.temperatures,{'nozzle':245.0,'bed':70})
        self.telegram.download_file=downloaded

    def test_v3_manual_heat_is_available_to_approved_users_and_honestly_pending(self):
        self.manager.handle_update(self.update('/nozzle 250',user=22))
        op=next(iter(self.store.get('temperature_operations').values()))
        self.assertEqual((op['heater'],op['target'],op['status']),('extruder',250,'pending'))
        self.assertIn('ожидает применения',self.telegram.messages[-1][1])
        self.assertEqual(self.printer.calls,[])
        self.manager.handle_update(self.update('/bed 101',user=22,update_id=2))
        self.assertEqual(len(self.store.get('temperature_operations')),1)
        self.manager.handle_update(self.update('/bed 70',user=99,update_id=3))
        self.assertEqual(len(self.store.get('temperature_operations')),1)
        self.manager.preflight.tick()
        self.assertIn(('temperature','extruder',250.0),self.printer.calls)

    def test_v3_temperature_preferences_are_per_user_and_do_not_change_existing_files(self):
        manager,slicer,asset=self.prepared_stl()
        manager._set_model_temperature(22,{'asset_id':None,'heater':'bed','delta':5})
        self.assertEqual(self.store.get('stl_preferences')['22']['bed'],75)
        self.assertEqual(self.store.get('assets')[asset['id']]['temperatures'],{'nozzle':240,'bed':70})
        manager.handle_update(self.stl_update(update_id=2,user=22,name='second.stl'))
        other=[a for a in self.store.get('assets').values() if a['id']!=asset['id']][0]
        self.assertEqual(other['temperatures'],{'nozzle':240,'bed':75.0})
        self.assertNotIn(str(10001),self.store.get('stl_preferences'))

    def test_v3_identical_dashboard_refreshes_are_coalesced_and_registered_panels_use_4_15_seconds(self):
        self.manager._status(10001);count=len(self.telegram.messages)
        self.manager._status(10001)
        self.assertEqual(self.telegram.edits[-1][1],1,'explicit navigation verifies the existing panel')
        count=len(self.telegram.edits)
        self.store.set('snapshot',{'state':'printing'})
        self.printer.photo=lambda:(_ for _ in ()).throw(AssertionError('panel refresh must not fetch camera'))
        self.now+=3;self.manager.refresh_panels()
        self.assertEqual(self.store.get('panels')[str(10001)]['updated_at'],1000)
        self.now+=1;self.manager.refresh_panels()
        self.assertEqual(self.store.get('panels')[str(10001)]['updated_at'],1004)
        self.store.set('snapshot',{'state':'standby'});self.printer.state='standby'
        self.now+=14;self.manager.refresh_panels()
        self.assertEqual(self.store.get('panels')[str(10001)]['updated_at'],1004)
        self.now+=1;self.manager.refresh_panels()
        self.assertEqual(len(self.telegram.edits),count+1)
        self.assertEqual(self.store.get('panels')[str(10001)]['message_id'],1)

    def test_v3_internal_gate_pause_never_leaks_native_resume_or_ai_observations(self):
        manager,slicer,asset=self.prepared_stl()
        manager.handle_update(self.callback(self.button('Стол пуст — печатать')))
        manager.preflight.tick()
        state=self.store.get('preflight');state.update(phase='gate',at_gate=True,job_id=self.printer.job_id)
        self.store.set('preflight',state);self.printer.state='paused'
        manager.detector=type('Detector',(),{'analyze':lambda *_:(_ for _ in ()).throw(AssertionError('no AI during gate'))})()
        manager.tick()
        manager.handle_update(self.update('/resume',update_id=101))
        manager.handle_update(self.update('/pause',update_id=102))
        self.assertEqual([call for call in self.printer.calls if call in {'pause','resume'}],[])
        self.assertTrue(self.store.get('preflight')['manual_hold'])
        events=[item['text'] for item in self.store.pending()]
        self.assertFalse(any('Печать на паузе' in text for text in events))
        state=self.store.get('preflight');state['phase']='failed';self.store.set('preflight',state)
        manager.handle_update(self.update('/resume',update_id=103))
        self.assertNotIn('resume',self.printer.calls)
        self.assertIn('старая',self.telegram.messages[-1][1].replace('старой','старая'))

    def test_v3_slow_refresh_cannot_overwrite_new_files_navigation(self):
        self.manager._status(10001)
        self.store.set('snapshot',{'state':'printing'});self.now+=4
        entered,release=threading.Event(),threading.Event();context=self.manager.context
        def delayed(include_job=True):
            entered.set();self.assertTrue(release.wait(2));return context(include_job)
        self.manager.context=delayed
        worker=threading.Thread(target=self.manager.refresh_panels);worker.start()
        try:
            self.assertTrue(entered.wait(1));self.manager._files(10001)
        finally:release.set();worker.join(2)
        self.assertEqual(self.store.get('panels')[str(10001)]['view'],'files')
        self.assertIn('Файлы',self.telegram.messages[-1][1])
        self.assertEqual(len(self.telegram.edits),1)

    def test_v3_changed_remote_file_downloads_new_bytes_even_if_old_source_is_cached(self):
        self.printer.state='standby'
        modified=[1]
        self.printer.files=lambda limit=1000:[{'path':'part.gcode','size':100,'modified':modified[0]}]
        self.manager._files(10001);asset=next(iter(self.store.get('assets').values()))
        self.printer.remote['part.gcode']=gcode(235,65).encode()
        self.manager._prepare_start(10001,{'asset_id':asset['id']});self.manager.process_slicing_job()
        first=self.store.get('assets')[asset['id']]
        self.assertEqual(first['gate']['nozzle'],235)
        self.assertTrue(Path(first['source_path']).exists())
        modified[0]=2;self.printer.remote['part.gcode']=gcode(245,75).encode()
        self.manager._files(10001)
        self.manager._prepare_start(10001,{'asset_id':asset['id']});self.manager.process_slicing_job()
        second=self.store.get('assets')[asset['id']]
        self.assertEqual((second['gate']['nozzle'],second['gate']['bed']),(245,75))
        self.assertGreater(second['version'],first['version'])
        self.assertNotEqual(second['ready_filename'],first['ready_filename'])

    def test_v3_stale_remote_listing_cannot_replace_a_concurrently_newer_version(self):
        self.manager._files(10001);asset=next(iter(self.store.get('assets').values()))
        entered,release=threading.Event(),threading.Event()
        def old_listing(limit=1000):
            entered.set();self.assertTrue(release.wait(2));return [{'path':'part.gcode','size':101,'modified':2}]
        self.printer.files=old_listing
        worker=threading.Thread(target=lambda:self.manager._files(10001));worker.start()
        try:
            self.assertTrue(entered.wait(1))
            newer=dict(asset,version=asset['version']+1,fingerprint=[3,102],ready=True,ready_filename='newer.gcode')
            self.store.mutate('assets',lambda assets:dict(assets,**{asset['id']:newer}),{})
        finally:release.set();worker.join(2)
        saved=self.store.get('assets')[asset['id']]
        self.assertEqual(saved['version'],newer['version']);self.assertEqual(saved['ready_filename'],'newer.gcode')

    def test_v3_heat_buttons_ignore_old_uncertain_and_superseded_requests(self):
        self.store.set('temperature_operations',{
            'older':{'heater':'heater_bed','target':95,'status':'uncertain','created':1},
            'replaced':{'heater':'heater_bed','target':85,'status':'superseded','created':2},
            'latest':{'heater':'heater_bed','target':80,'status':'applied','created':3}})
        self.manager._temperature_card(10001)
        button=self.button('Стол +5°')
        self.assertEqual(self.store.get('tickets')[button[2:]]['payload'],{'heater':'heater_bed','delta':5})
        self.assertNotIn('85° — ожидает',self.telegram.messages[-1][1])
        self.assertIn('95° — результат неизвестен',self.telegram.messages[-1][1])

    def test_v3_cancel_after_native_start_with_lagging_stats_cancels_exact_intent_once(self):
        manager,slicer,asset=self.prepared_stl()
        manager.handle_update(self.callback(self.button('Стол пуст — печатать')))
        self.printer.start_calibrated=lambda name:self.printer.calls.append(('start',name))
        manager.preflight.tick()
        self.assertEqual(manager.preflight.status()['phase'],'preparing')
        self.assertEqual(self.printer.state,'standby')
        manager.handle_update(self.update('/cancel',update_id=101))
        confirm=self.button('Да, отменить')
        self.assertNotIn('cancel',self.printer.calls)
        manager.handle_update(self.callback(confirm,update_id=102))
        manager.handle_update(self.callback(confirm,update_id=103))
        self.assertEqual(self.printer.calls.count('cancel'),1)
        self.assertEqual(manager.preflight.status()['phase'],'cancelled')

    def test_v3_hold_before_first_stats_update_keeps_native_resume_out_of_owner_handler(self):
        manager,slicer,asset=self.prepared_stl()
        manager.handle_update(self.callback(self.button('Стол пуст — печатать')))
        manager.handle_update(self.update('/pause',update_id=101))
        self.assertTrue(manager.preflight.status()['manual_hold'])
        self.assertFalse(manager.preflight.tick())
        self.assertEqual(self.printer.calls,[])
        manager.handle_update(self.update('/resume',update_id=102))
        self.assertFalse(manager.preflight.status()['manual_hold'])
        self.assertNotIn('resume',self.printer.calls)

    def test_v3_history_outage_never_converts_our_gate_into_native_resume(self):
        manager,slicer,asset=self.prepared_stl()
        manager.handle_update(self.callback(self.button('Стол пуст — печатать')))
        manager.preflight.tick()
        state=self.store.get('preflight');state.update(phase='failed',at_gate=True,job_id=self.printer.job_id)
        self.store.set('preflight',state);self.printer.state='paused'
        self.printer.history=lambda limit=1:(_ for _ in ()).throw(ApiError('network'))
        manager.handle_update(self.update('/resume',update_id=101))
        self.assertNotIn('resume',self.printer.calls)
        self.assertIn('старой карте',self.telegram.messages[-1][1])

    def test_v3_pause_flag_at_m25_before_worker_observation_does_not_bypass_gate(self):
        manager,slicer,asset=self.prepared_stl()
        manager.handle_update(self.callback(self.button('Стол пуст — печатать')))
        manager.preflight.tick()
        state=self.store.get('preflight');state.update(phase='preparing',at_gate=False,job_id=self.printer.job_id)
        self.store.set('preflight',state);self.printer.state='paused'
        status=self.printer.status
        def at_gate_before_worker():
            data=status();data['pause_resume']={'is_paused':True}
            data['virtual_sdcard']['file_position']=asset['gate']['end'];return data
        self.printer.status=at_gate_before_worker
        manager.handle_update(self.update('/resume',update_id=101))
        self.assertNotIn('resume',self.printer.calls)

    def test_explicit_text_status_reanchors_panel_after_later_chat_messages(self):
        self.manager.handle_update(self.update('/start'))
        previous=self.store.get('panels')[str(10001)]['message_id']
        latest=self.telegram.send_message(10001,'Позднее уведомление принтера')['message_id']
        self.manager.handle_update(self.update('/status',update_id=2))
        current=self.store.get('panels')[str(10001)]['message_id']
        self.assertGreater(current,latest,'explicit command must reply near the latest request')
        self.assertNotEqual(current,previous)

    def test_background_offline_refresh_replaces_stale_online_status_once_without_requery(self):
        self.manager._status(10001)
        self.manager._temperature_card(22)
        self.store.set('snapshot',{'state':'standby'})
        calls=[]
        def offline(include_job=True):
            calls.append(1);raise ApiError('network')
        self.manager.context=offline
        self.now+=15;self.manager.refresh_panels()
        self.assertEqual(calls,[1],'one failed printer request should feed every registered panel')
        for user in (10001,22):
            record=self.store.get('panels')[str(user)]
            self.assertEqual(record['updated_at'],self.now)
            self.assertTrue(any(uid==user and ('нет связи' in text or 'недоступен' in text) for uid,text,_ in self.telegram.messages))
        edits=len(self.telegram.edits)
        self.now+=15;self.manager.refresh_panels()
        self.assertEqual(len(self.telegram.edits),edits,'unchanged offline panels must not spam edits')

    def test_background_refresh_edits_the_latest_explicit_panel_without_new_messages(self):
        self.manager.handle_update(self.update('/start'))
        self.telegram.send_message(10001,'Новое уведомление')
        self.manager.handle_update(self.update('/status',update_id=2))
        current=self.store.get('panels')[str(10001)]['message_id']
        status=self.printer.status
        def changed():
            data=status();data['extruder']['temperature']=246;return data
        self.printer.status=changed
        self.store.set('snapshot',{'state':'printing'});self.now+=4
        self.manager.refresh_panels()
        self.assertEqual(self.telegram.edits[-1][1],current)
        self.assertEqual(self.store.get('panels')[str(10001)]['message_id'],current)

    def test_text_callback_updates_the_clicked_panel_instead_of_another_old_anchor(self):
        self.manager.handle_update(self.update('/start'))
        clicked=self.store.get('panels')[str(10001)]['message_id']
        self.manager.handle_update(self.update('/status',update_id=2))
        self.assertNotEqual(self.store.get('panels')[str(10001)]['message_id'],clicked)
        update=self.callback('n:temperature')
        update['callback_query']['message'].update(message_id=clicked,text='Старая панель K1C')
        self.manager.handle_update(update)
        self.assertEqual(self.telegram.edits[-1][1],clicked)
        self.assertEqual(self.store.get('panels')[str(10001)]['message_id'],clicked)

    def test_photo_callback_opens_a_new_text_panel_instead_of_editing_media(self):
        self.manager.handle_update(self.update('/start'))
        original=self.store.get('panels')[str(10001)]['message_id']
        update=self.callback('n:status')
        update['callback_query']['message'].update(message_id=50,photo=[{'file_id':'photo'}],caption='Камера')
        self.manager.handle_update(update)
        current=self.store.get('panels')[str(10001)]['message_id']
        self.assertNotEqual(current,original)
        self.assertNotEqual(current,50)
        self.assertFalse(self.telegram.edits)

    def test_heat_delta_resolves_fresh_target_instead_of_cached_display(self):
        cached=self.manager.context(include_job=False)
        class Presentation:
            def read(self):return {'context':cached,'error':None,'state':'fresh','age':0,'generation':1,'refreshing':False}
        self.manager.ui_status_cache=Presentation()
        status=self.printer.status
        def actual():
            data=status();data['extruder']['target']=200;return data
        self.printer.status=actual
        self.manager._temperature_card(10001)
        self.manager.handle_update(self.callback(self.button('Сопло +5°')))
        operation=list(self.store.get('temperature_operations').values())[-1]
        self.assertEqual(operation['target'],205)
        self.assertEqual(self.printer.calls,[])

    def test_heat_delta_during_preflight_uses_requested_not_temporary_native_nozzle_target(self):
        manager,slicer,asset=self.prepared_stl()
        manager.handle_update(self.callback(self.button('Стол пуст — печатать')));manager.preflight.tick()
        status=self.printer.status
        def probe_temperature():
            data=status();data['extruder']['target']=140;return data
        self.printer.status=probe_temperature
        manager._temperature_card(10001)
        manager.handle_update(self.callback(self.button('Сопло +5°'),update_id=101))
        self.assertEqual(manager.preflight.status()['desired']['extruder'],245)

    def test_heat_delta_rechecks_deadline_after_slow_fresh_read(self):
        self.manager._temperature_card(10001);button=self.button('Стол +5°')
        status=self.printer.status
        def slow():self.now+=61;return status()
        self.printer.status=slow
        self.manager.handle_update(self.callback(button))
        self.assertEqual(self.store.get('temperature_operations'),{})
        self.assertIn('устарела',self.telegram.messages[-1][1])

    def suspicious_manager(self):
        class Detector:
            def __init__(self):
                self.frames = 0
            def analyze(self, photo):
                self.frames += 1
                return {"detections": [] if self.frames <= 60 else [["failure", .9, [1, 2, 3, 4]]] * 3}
            def status(self):
                return {"available": True}
        self.config["analysis_interval"] = 0
        manager = Manager(self.config, self.store, self.printer, self.telegram, detector=Detector(), clock=lambda: self.now)
        for _ in range(80):
            self.now += 10
            manager.tick()
            if manager.alarms.current():
                return manager
        self.fail("temporal warning did not create an alarm")
        return manager

    def test_unanswered_alarm_pauses_after_five_minutes_even_after_restart(self):
        self.suspicious_manager()
        self.now += 299
        self.manager.tick()
        self.assertEqual(self.printer.calls, [])
        self.now += 1
        restarted = Manager(self.config, self.store, self.printer, self.telegram, clock=lambda: self.now)
        restarted.tick()
        restarted.tick()
        self.assertEqual(self.printer.calls, ["pause"])

    def test_late_continue_never_resumes_and_first_owner_wins(self):
        manager = self.suspicious_manager()
        alert = manager.alarms.current()
        self.now += 300
        manager.tick()
        manager.handle_update(self.callback("a:" + alert["id"] + ":continue"))
        manager.handle_update(self.callback("a:" + alert["id"] + ":pause", update_id=101, user=22))
        self.assertEqual(self.printer.calls, ["pause"])
        self.assertEqual(manager.alarms.current()["actor"], 10001)

    def test_a_replacement_job_does_not_inherit_an_old_alarm(self):
        manager = self.suspicious_manager()
        self.printer.job_id = "new-job"
        self.now += 300
        manager.tick()
        self.assertEqual(self.printer.calls, [])

    def test_camera_loss_only_notifies(self):
        class Detector:
            def analyze(self, photo):
                raise AssertionError("must not analyze missing image")
        self.printer.photo = lambda: (_ for _ in ()).throw(ApiError("camera_offline"))
        manager = Manager(self.config, self.store, self.printer, self.telegram, detector=Detector(), clock=lambda: self.now)
        for _ in range(3):
            manager.tick()
            self.now += 10
        self.now += 600
        manager.tick()
        manager.flush_outbox()
        self.assertEqual(self.printer.calls, [])
        alerts = [text for _, text, _ in self.telegram.messages if "Камера недоступна" in text]
        self.assertEqual(len(alerts), 3)

    def test_cancel_without_a_stable_job_id_does_not_offer_an_unsafe_confirmation(self):
        self.printer.history = lambda limit=10: (_ for _ in ()).throw(ApiError("history_offline"))
        self.manager.handle_update(self.update("/cancel"))
        self.assertEqual(self.printer.calls, [])
        self.assertIn("ID текущей печати", self.telegram.messages[-1][1])

    def test_shutdown_with_null_print_stats_is_reported(self):
        self.printer.status = lambda: {"webhooks": {"state": "shutdown", "message": "MCU disconnected"}, "print_stats": None}
        self.manager.handle_update(self.update("/status"))
        self.manager.tick()
        self.manager.flush_outbox()
        self.assertIn("Klipper не готов", self.telegram.messages[0][1])
        self.assertTrue(any("MCU disconnected" in text for _, text, _ in self.telegram.messages))

    def test_rejected_photo_falls_back_to_the_same_alarm_text_and_buttons(self):
        def reject(*args, **kwargs):
            raise ApiError("http_400", http_status=400)
        self.telegram.send_photo = reject
        manager = self.suspicious_manager()
        manager.flush_outbox()
        delivered = [(user, text, markup) for user, text, markup in self.telegram.messages if "Подозрение на брак" in text]
        self.assertEqual({user for user, _, _ in delivered}, {10001, 22, 33})
        self.assertTrue(all(markup.get("inline_keyboard") for _, _, markup in delivered))

    def test_deadline_watchdog_does_not_wait_for_telegram_delivery(self):
        manager = self.suspicious_manager()
        entered, release = threading.Event(), threading.Event()
        def blocked_photo(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("delivery test release timed out")
        self.telegram.send_photo = blocked_photo
        worker = threading.Thread(target=manager.flush_outbox)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.now += 300
            manager.check_deadlines()
            self.assertEqual(self.printer.calls, ["pause"])
        finally:
            release.set()
            worker.join(3)

    def test_job_is_rechecked_after_slow_photo_before_autopause(self):
        manager = self.suspicious_manager()
        history = self.printer.history
        self.printer.history = lambda limit=10: (_ for _ in ()).throw(ApiError("history_offline"))
        manager.tick()
        self.printer.history = history
        self.now += 300
        def photo_changes_job():
            self.printer.job_id = "replacement-job"
            return b"\xff\xd8image\xff\xd9"
        self.printer.photo = photo_changes_job
        manager.tick()
        self.assertEqual(self.printer.calls, [])

    def test_external_orca_job_failure_offers_queue_recovery(self):
        self.manager.tick()
        self.printer.state = "error"
        self.manager.tick()
        self.manager.flush_outbox()
        self.assertIsNotNone(self.button("Повторить"))
        self.assertIsNotNone(self.button("Завершить очередь"))
        self.assertEqual(self.printer.calls, [])

    def test_detector_validation_mode_never_issues_an_automatic_pause(self):
        self.config["detector_auto_enabled"] = False
        manager = self.suspicious_manager()
        self.now += 600
        manager.check_deadlines()
        manager.tick()
        manager.flush_outbox()
        self.assertEqual(self.printer.calls, [])
        self.assertTrue(any("Автопауза пока отключена" in text for _, text, _, _ in self.telegram.photos))

    def test_owner_can_enable_autopause_after_live_connection_without_replaying_old_alarm(self):
        self.config["detector_auto_enabled"] = False
        manager = self.suspicious_manager()
        self.now += 600
        manager.handle_update(self.update("/control", update_id=80))
        manager.handle_update(self.callback(self.button("Включить автопаузу"), update_id=81))
        manager.check_deadlines()
        self.assertEqual(self.printer.calls, [])
        self.assertIn("Автопауза: включена", self.telegram.messages[-1][1])

    def test_autopause_cannot_be_enabled_with_a_missing_camera(self):
        self.config["detector_auto_enabled"] = False
        manager = self.suspicious_manager()
        self.printer.photo = lambda: (_ for _ in ()).throw(ApiError("camera_offline"))
        manager.handle_update(self.update("/control", update_id=80))
        manager.handle_update(self.callback(self.button("Включить автопаузу"), update_id=81))
        manager.handle_update(self.update("/control", update_id=82))
        self.assertIn("Автопауза: пока отключена", self.telegram.messages[-1][1])

    def test_slow_model_check_cannot_activate_an_expired_control(self):
        self.config["detector_auto_enabled"] = False
        manager = self.suspicious_manager()
        manager.handle_update(self.update("/control", update_id=80))
        button = self.button("Включить автопаузу")
        def slow_status():
            self.now += 61
            return {"available": True}
        manager.detector.status = slow_status
        manager.handle_update(self.callback(button, update_id=81))
        self.assertIn("Кнопка устарела", self.telegram.messages[-1][1])
        manager.handle_update(self.update("/control", update_id=82))
        self.assertIn("Автопауза: пока отключена", self.telegram.messages[-1][1])


if __name__ == "__main__":
    unittest.main()
