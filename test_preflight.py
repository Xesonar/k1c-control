import copy
from pathlib import Path
import tempfile
import threading
import unittest

from adapters import ApiError
from preflight import OPERATIONS, STATE, Preflight
from storage import Store


class NativePrinter:
    def __init__(self):
        self.calls = []
        self.value = {'ready': True, 'state': 'standby', 'filename': 'previous.gcode', 'job_id': 'old-job',
            'data': {'virtual_sdcard': {'file_position': 0}, 'extruder': {'target': 240, 'temperature': 240},
                     'heater_bed': {'target': 70, 'temperature': 70},
                     'bed_mesh': {'mesh_matrix': [[0.0, .1], [.1, 0.0]]}}}
        self.recalibration_hook = None
        self.start_error = self.resume_error = self.mesh_error = self.heat_error = None

    def context(self):
        return copy.deepcopy(self.value)

    def start_calibrated(self, filename):
        self.calls.append(('start_calibrated', filename))
        if self.start_error:
            raise self.start_error
        self.value.update(filename=filename, job_id='new-job', state='printing')

    def set_temperature(self, heater, target):
        self.calls.append(('temperature', heater, target))
        if self.heat_error:
            raise self.heat_error
        self.value['data'][heater].update(target=target, temperature=target)

    def recalibrate(self, bed, nozzle):
        self.calls.append(('recalibrate', bed, nozzle))
        if self.recalibration_hook:
            self.recalibration_hook()
        if self.mesh_error:
            raise self.mesh_error
        self.value['data']['heater_bed'].update(target=bed, temperature=bed)
        self.value['data']['extruder'].update(target=nozzle, temperature=nozzle)

    def resume_gate(self, gate):
        self.calls.append(('resume_gate', gate['state_name']))
        if self.resume_error:
            raise self.resume_error
        self.value['state'] = 'printing'


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.sqlite')
        self.printer = NativePrinter()
        self.users = {1, 2}
        self.now = 1000
        self.messages = []
        self.physical_lock = threading.RLock()
        self.stopping = False
        self.asset = {'id': 'model', 'version': 1, 'ready': True,
            'ready_filename': 'telegram/prepared.gcode', 'server_filename': 'telegram/source.gcode',
            'gate': {'start': 180, 'end': 200, 'bed': 70, 'nozzle': 240,
                     'token': 'abcdefgh', 'state_name': 'K1C_GATE_abcdefgh'}}
        self.store.set('assets', {'model': self.asset})
        self.controller = self.make_controller()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def make_controller(self):
        return Preflight(self.store, self.printer, self.printer.context, lambda user: user in self.users,
                         lambda text, key=None: self.messages.append((text, key)), self.physical_lock,
                         clock=lambda: self.now, stopping=lambda: self.stopping)

    def start(self):
        self.controller.begin(1, 'User', self.asset, 'start-key')
        self.assertEqual(self.printer.calls, [], 'begin must only queue the accepted intent')
        self.controller.tick()  # one native calibrated start
        self.controller.tick()  # bind the newly observed stable history job
        self.assertEqual(self.controller.status()['job_id'], 'new-job')

    def gate(self):
        self.printer.value['state'] = 'paused'
        self.printer.value['data']['virtual_sdcard']['file_position'] = self.asset['gate']['end']

    def resumes(self):
        return [call for call in self.printer.calls if call[0] == 'resume_gate']

    def test_begin_then_native_gate_resumes_exactly_once(self):
        self.start()
        self.assertEqual(self.printer.calls, [('start_calibrated', 'telegram/prepared.gcode')])
        self.gate()
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.resumes(), [('resume_gate', 'K1C_GATE_abcdefgh')])
        self.assertEqual(self.controller.status()['phase'], 'printing')
        self.assertFalse(self.controller.owns(self.printer.context()))

    def test_bed_change_waits_for_gate_then_new_mesh_before_resume(self):
        self.start()
        operation = self.controller.request_temperature(2, 'heater_bed', 80, 'bed-change')
        self.assertEqual(operation['status'], 'pending')
        self.controller.tick()
        self.assertEqual(len(self.printer.calls), 1, 'do not enqueue heater RPC into a busy START_PRINT')
        self.gate();self.controller.tick()
        self.assertEqual(self.printer.calls[-1], ('recalibrate', 80, 240))
        self.assertEqual(self.resumes(), [])
        self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.controller.status()['mesh_bed'], 80)

    def test_new_bed_request_during_probe_requires_another_fresh_cycle(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed-80')
        self.printer.recalibration_hook = lambda: self.controller.request_temperature(2, 'heater_bed', 90, 'bed-90')
        self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.printer.recalibration_hook = None
        self.controller.tick()
        self.assertEqual([call for call in self.printer.calls if call[0] == 'recalibrate'],
                         [('recalibrate', 80, 240), ('recalibrate', 90, 240)])
        self.assertEqual(self.resumes(), [])
        self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_cancel_remains_possible_during_long_probe_and_never_resumes(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            self.assertTrue(release.wait(2))
        self.printer.recalibration_hook = probe
        worker = threading.Thread(target=self.controller.tick)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            acquired = self.physical_lock.acquire(timeout=.2)
            self.assertTrue(acquired, 'long probe held the physical lock and blocked CANCEL')
            if acquired:
                self.physical_lock.release()
            self.controller.cancel(2)
        finally:
            release.set();worker.join(2)
        self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.controller.status()['phase'], 'cancelled')

    def test_manual_hold_during_probe_never_autoresumes(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        self.printer.recalibration_hook = lambda: self.controller.hold(2, True)
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.controller.status()['phase'], 'held')
        self.controller.hold(2, False);self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_low_nozzle_and_disabled_bed_apply_but_hold_the_model(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'extruder', 0, 'off-nozzle')
        self.controller.request_temperature(2, 'heater_bed', 0, 'off-bed')
        for _ in range(4):
            self.controller.tick()
        self.assertIn(('temperature', 'extruder', 0), self.printer.calls)
        self.assertIn(('temperature', 'heater_bed', 0), self.printer.calls)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.controller.status()['phase'], 'held')
        self.controller.request_temperature(2, 'extruder', 240, 'restore-nozzle')
        self.controller.request_temperature(2, 'heater_bed', 70, 'restore-bed')
        for _ in range(4):
            self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_nozzle_change_requires_actual_heat_not_only_accepted_target(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'extruder', 250, 'heat-nozzle')
        def slow_heat(heater, target):
            self.printer.calls.append(('temperature', heater, target))
            self.printer.value['data'][heater]['target'] = target
        self.printer.set_temperature = slow_heat
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.controller.status()['phase'], 'heating')
        self.printer.value['data']['extruder']['temperature'] = 250
        self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_initial_unheated_bed_profile_uses_fresh_native_mesh_without_waiting_for_zero_celsius(self):
        self.asset['gate']['bed'] = 0
        self.store.set('assets', {'model': self.asset})
        self.printer.value['data']['heater_bed'].update(target=0, temperature=25)
        self.start();self.gate();self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertFalse(self.controller.status()['bed_off_hold'])

    def test_explicit_bed_off_on_initial_cold_profile_holds_until_positive_target_and_reprobe(self):
        self.asset['gate']['bed'] = 0
        self.store.set('assets', {'model': self.asset})
        self.printer.value['data']['heater_bed'].update(target=0, temperature=25)
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 0, 'explicit-off')
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertTrue(self.controller.status()['bed_off_hold'])
        self.controller.request_temperature(2, 'heater_bed', 70, 'positive-bed')
        self.controller.tick()
        self.assertEqual(self.printer.calls[-1], ('recalibrate', 70, 240))
        self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_revoked_pending_bed_request_does_not_hold_an_initial_unheated_profile(self):
        self.asset['gate']['bed'] = 0
        self.store.set('assets', {'model': self.asset})
        self.printer.value['data']['heater_bed'].update(target=0, temperature=25)
        self.start();self.controller.request_temperature(2, 'heater_bed', 70, 'bed')
        self.users.remove(2);self.gate();self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_revoking_a_later_heat_request_does_not_undo_an_already_applied_manual_off_hold(self):
        self.asset['gate']['bed'] = 0
        self.store.set('assets', {'model': self.asset})
        self.printer.value['data']['heater_bed'].update(target=0, temperature=25)
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 0, 'explicit-off')
        record = self.controller._record_applied
        def concurrent_target(state, heater, target):
            if heater == 'heater_bed':
                self.users.add(3)
                self.controller.request_temperature(3, 'heater_bed', 70, 'later-target')
                self.users.remove(3)
            record(state, heater, target)
        self.controller._record_applied = concurrent_target
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertTrue(self.controller.status()['bed_off_hold'])

    def test_wrong_pause_position_never_becomes_an_auto_resume(self):
        self.start();self.gate()
        self.printer.value['data']['virtual_sdcard']['file_position'] += 1
        self.controller.tick()
        self.assertEqual(self.controller.status()['phase'], 'interrupted')
        self.assertTrue(self.controller.owns(self.printer.context()), 'ordinary RESUME must remain blocked')
        self.gate();self.controller.hold(1, False);self.controller.tick()
        self.assertEqual(self.resumes(), [])

    def test_replacement_history_job_is_never_resumed(self):
        self.start();self.gate()
        self.printer.value['job_id'] = 'someone-elses-job'
        self.controller.tick()
        self.assertEqual(self.controller.status()['phase'], 'interrupted')
        self.assertEqual(self.resumes(), [])

    def test_missing_or_old_job_id_does_not_bind_or_resume(self):
        self.controller.begin(1, 'User', self.asset, 'start-key');self.controller.tick();self.gate()
        self.printer.value['job_id'] = 'old-job'
        self.controller.tick()
        self.assertIsNone(self.controller.status()['job_id'])
        self.assertEqual(self.resumes(), [])

    def test_brief_history_registration_delay_does_not_abort_preparation(self):
        self.controller.begin(1, 'User', self.asset, 'start-key');self.controller.tick();self.gate()
        self.printer.value['job_id'] = None
        self.controller.tick()
        self.assertEqual(self.controller.status()['phase'], 'preparing')
        self.assertEqual(self.resumes(), [])
        self.printer.value['job_id'] = 'new-job'
        self.controller.tick();self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_bound_history_id_missing_read_keeps_gate_owned_until_the_id_recovers(self):
        self.start();self.gate()
        self.printer.value['job_id'] = None
        self.assertTrue(self.controller.owns(self.printer.context()))
        self.assertFalse(self.controller.tick())
        self.assertEqual(self.controller.status()['phase'], 'preparing')
        self.assertEqual(self.controller.status()['job_id'], 'new-job')
        self.assertEqual(self.resumes(), [])
        self.printer.value['job_id'] = 'new-job'
        self.controller.tick();self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_mesh_failure_stops_instead_of_loading_old_mesh_or_retrying(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        self.printer.mesh_error = ApiError('network', uncertain=True)
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.controller.status()['phase'], 'failed')
        self.assertEqual(len([call for call in self.printer.calls if call[0] == 'recalibrate']), 1)
        self.assertEqual(self.resumes(), [])
        self.assertTrue(self.controller.owns(self.printer.context()))

    def test_gate_without_active_mesh_cannot_resume(self):
        self.start();self.gate()
        self.printer.value['data']['bed_mesh']['mesh_matrix'] = []
        self.controller.tick()
        self.assertEqual(self.controller.status()['phase'], 'interrupted')
        self.assertEqual(self.resumes(), [])

    def test_successful_probe_rpc_without_mesh_or_stable_bed_cannot_resume(self):
        for failure in ('mesh', 'bed'):
            with self.subTest(failure=failure):
                if self.controller.busy() or self.controller.status()['phase'] != 'idle':
                    self.controller.cancel(1)
                self.printer.value.update(state='standby', filename='previous.gcode', job_id='old-job')
                self.printer.value['data']['bed_mesh']['mesh_matrix'] = [[0.0, .1], [.1, 0.0]]
                self.printer.value['data']['heater_bed'].update(target=70, temperature=70)
                self.controller.begin(1, 'User', self.asset, 'start-' + failure)
                self.controller.tick();self.controller.tick();self.gate()
                self.controller.request_temperature(2, 'heater_bed', 80, 'bed-' + failure)
                def invalid_probe(bed, nozzle):
                    self.printer.calls.append(('recalibrate', bed, nozzle))
                    self.printer.value['data']['heater_bed'].update(target=bed, temperature=bed)
                    if failure == 'mesh':
                        self.printer.value['data']['bed_mesh']['mesh_matrix'] = []
                    else:
                        self.printer.value['data']['heater_bed']['temperature'] = 50
                self.printer.recalibrate = invalid_probe
                self.controller.tick();self.controller.tick()
                self.assertEqual(self.controller.status()['phase'], 'failed')
                self.assertEqual(self.resumes(), [])

    def test_restart_interrupts_pending_start_and_gated_preparation(self):
        self.controller.begin(1, 'User', self.asset, 'start-key')
        self.controller = self.make_controller();self.controller.tick()
        self.assertEqual(self.printer.calls, [])
        self.assertEqual(self.controller.status()['phase'], 'interrupted')
        # A separately accepted start can be observed at its gate, but another
        # restart still cannot infer permission to issue RESUME.
        self.controller.cancel(1)
        self.controller.begin(1, 'User', self.asset, 'second-key');self.controller.tick();self.controller.tick();self.gate()
        self.controller = self.make_controller();self.controller.hold(1, False);self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertTrue(self.controller.owns(self.printer.context()))

    def test_revoked_before_start_and_during_probe_cannot_start_the_model(self):
        self.controller.begin(1, 'User', self.asset, 'start-key')
        self.users.remove(1);self.controller.tick()
        self.assertEqual(self.printer.calls, [])
        self.users.add(1);self.controller.cancel(1)
        self.controller.begin(1, 'User', self.asset, 'second-key');self.controller.tick();self.controller.tick();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        self.printer.recalibration_hook = lambda: self.users.remove(2)
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.controller.status()['phase'], 'failed')

    def test_revoked_queued_heat_is_cancelled_and_never_applied(self):
        self.start();self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        self.users.remove(2);self.gate();self.controller.tick()
        self.assertEqual([call for call in self.printer.calls if call[0] == 'recalibrate'], [])
        self.assertEqual(self.controller.status()['desired']['heater_bed'], 70)
        self.assertEqual(next(iter(self.store.get(OPERATIONS).values()))['status'], 'cancelled')

    def test_manual_heat_outside_preparation_is_once_and_no_reprobe_during_print(self):
        self.printer.value['state'] = 'printing'
        self.controller.request_temperature(2, 'heater_bed', 80, 'heat')
        self.controller.request_temperature(2, 'heater_bed', 80, 'heat')
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.printer.calls, [('temperature', 'heater_bed', 80)])
        self.assertEqual(next(iter(self.store.get(OPERATIONS).values()))['status'], 'applied')

    def test_uncertain_manual_heat_and_resume_are_not_repeated(self):
        self.printer.heat_error = ApiError('network', uncertain=True)
        self.controller.request_temperature(2, 'extruder', 200, 'heat')
        self.controller.tick();self.controller.tick()
        self.assertEqual(len(self.printer.calls), 1)
        self.assertEqual(next(iter(self.store.get(OPERATIONS).values()))['status'], 'uncertain')
        self.printer.calls.clear();self.printer.heat_error = None
        self.start();self.gate();self.printer.resume_error = ApiError('network', uncertain=True)
        self.controller.tick();self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.controller.status()['phase'], 'failed')

    def test_resume_timeout_after_actual_model_movement_is_honest_and_does_not_suppress_ai(self):
        self.start();self.gate()
        def accepted_but_reply_lost(gate):
            self.printer.calls.append(('resume_gate', gate['state_name']))
            self.printer.value['state'] = 'printing'
            self.printer.value['data']['virtual_sdcard']['file_position'] = gate['end'] + 1
            raise ApiError('network', uncertain=True)
        self.printer.resume_gate = accepted_but_reply_lost
        self.controller.tick();self.controller.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.controller.status()['reason'], 'resume_uncertain')
        self.assertIn('не подтверждён', self.controller.status()['stage'])
        self.assertIn('могла выполниться', self.messages[-1][0])
        self.assertNotIn('не начата', self.messages[-1][0])
        self.assertFalse(self.controller.owns(self.printer.context()))
        self.gate()
        self.assertTrue(self.controller.owns(self.printer.context()), 'uncertain gate still paused must remain protected')

    def test_duplicate_begin_is_once_and_changed_asset_cannot_start(self):
        self.controller.begin(1, 'User', self.asset, 'start-key')
        self.controller.begin(1, 'User', self.asset, 'start-key')
        self.controller.tick();self.controller.tick()
        self.assertEqual(len([call for call in self.printer.calls if call[0] == 'start_calibrated']), 1)
        self.controller.cancel(1)
        self.printer.value['state'] = 'standby'
        self.controller.begin(1, 'User', self.asset, 'new-key')
        changed = dict(self.asset, version=2)
        self.store.set('assets', {'model': changed})
        self.controller.tick()
        self.assertEqual(len([call for call in self.printer.calls if call[0] == 'start_calibrated']), 1)

    def test_queue_change_before_background_start_prevents_start(self):
        self.store.set('workflow', {'queue': [{'id': 'q1', 'asset_id': 'model'}], 'running': None})
        self.controller.begin(1, 'User', self.asset, 'start-key', qid='q1')
        self.store.set('workflow', {'queue': [], 'running': None})
        self.controller.tick()
        self.assertEqual(self.printer.calls, [])
        self.assertEqual(self.controller.status()['reason'], 'queue_changed')

    def test_native_start_acceptance_moves_the_confirmed_queue_entry_once(self):
        self.store.set('workflow', {'queue': [{'id': 'q1', 'asset_id': 'model'}, {'id': 'q2', 'asset_id': 'model'}], 'running': None})
        self.controller.begin(1, 'User', self.asset, 'start-key', qid='q1')
        self.controller.tick();self.controller.tick()
        workflow = self.store.get('workflow')
        self.assertEqual(workflow['running']['qid'], 'q1')
        self.assertEqual([item['id'] for item in workflow['queue']], ['q2'])

    def test_unexpected_probe_exception_cannot_retry_or_resume_old_mesh(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        self.printer.mesh_error = RuntimeError('not a public transport error')
        self.controller.tick();self.controller.tick()
        self.assertEqual(self.controller.status()['phase'], 'failed')
        self.assertEqual(len([call for call in self.printer.calls if call[0] == 'recalibrate']), 1)
        self.assertEqual(self.resumes(), [])

    def test_late_probe_reply_after_shutdown_never_touches_closed_store(self):
        self.start();self.gate()
        self.controller.request_temperature(2, 'heater_bed', 80, 'bed')
        entered, release = threading.Event(), threading.Event()
        errors = []
        def long_probe():
            entered.set()
            self.assertTrue(release.wait(2))
        self.printer.recalibration_hook = long_probe
        def run():
            try:
                self.controller.tick()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.stopping = True
            self.store.close()
        finally:
            release.set();worker.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(self.resumes(), [])

    def test_temperature_limits_and_stopping_are_enforced(self):
        for heater, target in [('extruder', 301), ('heater_bed', 101), ('extruder', float('nan')), ('other', 100), ('extruder', True)]:
            with self.subTest(heater=heater, target=target), self.assertRaises(ApiError):
                self.controller.request_temperature(1, heater, target, str((heater, target)))
        self.controller.request_temperature(1, 'extruder', 200, 'heat')
        self.stopping = True;self.controller.tick()
        self.assertEqual(self.printer.calls, [])

    def test_temperature_racing_final_resume_cannot_attach_after_the_final_checks(self):
        self.start();self.gate()
        attempted, completed = threading.Event(), threading.Event()
        operations, errors, workers = [], [], []
        original_claim = self.store.claim_action
        original_resume = self.printer.resume_gate
        def request():
            attempted.set()
            try:
                operations.append(self.controller.request_temperature(2, 'heater_bed', 80, 'racing-target'))
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()
        def claim(key):
            if key.startswith('preflight:resume:'):
                worker = threading.Thread(target=request)
                workers.append(worker);worker.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(completed.wait(.1), 'new bed target slipped between thermal checks and resume claim')
            return original_claim(key)
        def resume(gate):
            # Once the short decision lock is released, the request must be
            # accepted even while the physical RPC is still pending. It is now
            # a current-print heater request, never a stale preparation target.
            self.assertTrue(completed.wait(1))
            original_resume(gate)
        self.store.claim_action = claim
        self.printer.resume_gate = resume
        try:
            self.controller.tick()
        finally:
            for worker in workers:
                worker.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.resumes()), 1)
        self.assertIsNone(operations[0]['intent_id'])
        self.assertEqual(self.controller.status()['desired']['heater_bed'], 70)
        self.controller.tick()
        self.assertEqual(self.printer.calls[-1], ('temperature', 'heater_bed', 80))

    def test_new_target_or_hold_accepted_during_final_read_prevents_resume(self):
        self.start();self.gate()
        # Complete native proof without resuming by temporarily holding. Then
        # inject the next change at the final RPC read, before its locked check.
        self.controller.hold(2, True);self.controller.tick();self.controller.hold(2, False)
        state = self.controller._state()
        context = self.controller.context
        def final_read():
            self.controller.request_temperature(2, 'heater_bed', 80, 'new-before-claim')
            return context()
        self.controller.context = final_read
        self.controller._resume(state)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.controller.status()['desired']['heater_bed'], 80)
        self.controller.request_temperature(2, 'heater_bed', 70, 'restore-before-hold')
        self.controller.context = lambda: (self.controller.hold(2, True), context())[1]
        self.controller._resume(self.controller._state())
        self.assertEqual(self.resumes(), [])


if __name__ == '__main__':
    unittest.main()
