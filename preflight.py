"""Durable native-print preparation gate; never replays physical commands.

The trusted file executes START_PRINT then SAVE_GCODE_STATE/M25. Only that
exact file, a newly observed history job ID, and the exact byte after M25 can
receive resume_gate. Long probing RPCs never hold the owner's physical lock.
"""
import math
import re
import threading
import time
import uuid

from adapters import ApiError

ACTIVE = {'accepted', 'starting', 'preparing', 'gate', 'heating', 'calibrating', 'held', 'resuming'}
GATE_PHASES = {'gate', 'heating', 'calibrating', 'held'}
IDLE = {'standby', 'complete', 'cancelled', 'error'}
STAGES = {'accepted': 'Задание принято', 'starting': 'Запускаю подготовку принтера',
          'preparing': 'Прогрев и новый замер стола', 'gate': 'Проверяю готовность к печати',
          'calibrating': 'Новый замер при выбранной температуре стола',
          'heating': 'Довожу температуры до выбранных значений', 'held': 'Подготовка приостановлена',
          'resuming': 'Начинаю печать', 'printing': 'Печать началась', 'complete': 'Печать завершена',
          'failed': 'Подготовка остановлена', 'interrupted': 'Подготовка прервана',
          'cancelled': 'Подготовка отменена'}
STATE = 'preflight'
OPERATIONS = 'temperature_operations'


def _number(value, maximum=None):
    return (type(value) in {int, float} and math.isfinite(value) and value >= 0
            and (maximum is None or value <= maximum))


def _filename(value):
    return (isinstance(value, str) and bool(value) and not value.startswith('/') and
            not any(part in {'', '.', '..'} for part in value.split('/')) and
            not any(ord(char) < 32 for char in value) and '\\' not in value)


class Preflight:
    def __init__(self, store, printer, context, allowed, notify, physical_lock,
                 clock=time.time, stopping=lambda: False):
        self.store, self.printer, self.context = store, printer, context
        self.allowed, self.notify, self.physical_lock = allowed, notify, physical_lock
        self.clock, self.stopping = clock, stopping
        self.lock = threading.RLock()
        self.worker_lock = threading.Lock()
        self._recover()

    def _recover(self):
        def interrupt(state):
            if state and state.get('phase') in ACTIVE:
                state.update(phase='interrupted', reason='restart', manual_hold=True, updated=self.clock())
            return state
        self.store.mutate(STATE, interrupt, None)
        def cancel(operations):
            for operation in operations.values():
                if operation.get('status') in {'pending', 'claimed'}:
                    operation.update(status='interrupted', reason='restart', finished=self.clock())
            return operations
        self.store.mutate(OPERATIONS, cancel, {})

    def _state(self):
        return self.store.get(STATE)

    def status(self):
        state = self._state()
        if not state:
            return {'phase': 'idle', 'stage': 'Нет подготовки к печати', 'busy': False, 'at_gate': False}
        stage = ('Результат продолжения не подтверждён' if state.get('reason') == 'resume_uncertain'
                 else STAGES.get(state['phase'], 'Подготовка принтера'))
        return dict(state, stage=stage,
                    busy=state['phase'] in ACTIVE, at_gate=bool(state.get('at_gate')))

    def busy(self):
        return self.status()['busy']

    def owns(self, context):
        state = self._state()
        position = context.get('data', {}).get('virtual_sdcard', {}).get('file_position')
        if (state and state['phase'] in {'failed', 'interrupted'} and self._same_job(state, context)
                and context.get('state') == 'printing' and type(position) in {int, float}
                and math.isfinite(position) and position > state['gate']['end']):
            # A timed-out M24 may have succeeded. Live model movement is proof
            # that this is no longer a stopped preparation; do not suppress AI.
            return False
        return bool(state and state['phase'] not in {'printing', 'complete', 'cancelled'}
                    and context.get('filename') == state['filename']
                    and (state.get('job_id') is None or context.get('job_id') is None
                         or state['job_id'] == context.get('job_id')))

    @staticmethod
    def _gate(asset):
        gate = asset.get('gate')
        if not isinstance(gate, dict):
            raise ApiError('preflight_invalid_gate')
        token = gate.get('token')
        if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_]{8,64}', token)
                or gate.get('state_name') != 'K1C_GATE_' + token
                or type(gate.get('end')) is not int or gate['end'] <= 0
                or not _number(gate.get('bed'), 100) or not _number(gate.get('nozzle'), 300)):
            raise ApiError('preflight_invalid_gate')
        return dict(gate)

    def begin(self, user, sender, asset, key, qid=None):
        if not self.allowed(user):
            raise ApiError('preflight_access_revoked')
        gate = self._gate(asset)
        filename = asset.get('ready_filename')
        if not asset.get('ready') or not _filename(filename):
            raise ApiError('preflight_file_not_ready')
        if not isinstance(key, str) or not key:
            raise ApiError('preflight_invalid_key')
        with self.lock:
            outcome = {}
            def accept(current):
                if current and current.get('key') == key:
                    outcome.update(current)
                    return current
                if current and current.get('phase') in ACTIVE:
                    raise ApiError('preflight_busy')
                if self.stopping() or not self.allowed(user):
                    raise ApiError('preflight_access_revoked')
                saved = self.store.get('assets', {}).get(asset.get('id'))
                if (not saved or saved.get('version') != asset.get('version')
                        or saved.get('ready_filename') != filename or not saved.get('ready')
                        or saved.get('gate') != gate):
                    raise ApiError('preflight_asset_changed')
                if not self.store.claim_action('preflight:accept:' + key):
                    outcome.update(phase='duplicate', busy=False)
                    return current
                current = {'id': uuid.uuid4().hex, 'key': key, 'user': user, 'sender': str(sender)[:60],
                    'asset_id': asset['id'], 'asset_version': asset.get('version'), 'filename': filename,
                    'gate': gate, 'qid': qid, 'phase': 'accepted', 'created': self.clock(), 'updated': self.clock(),
                    'desired': {'heater_bed': float(gate['bed']), 'extruder': float(gate['nozzle'])},
                    'applied': {'heater_bed': float(gate['bed']), 'extruder': float(gate['nozzle'])},
                    'revisions': {'heater_bed': 0, 'extruder': 0}, 'mesh_bed': float(gate['bed']),
                    'bed_off_hold': False, 'applied_bed_off_hold': False,
                    'manual_hold': False, 'at_gate': False, 'native_verified': False,
                    'job_id': None, 'baseline_job_id': None, 'baseline_filename': None,
                    'reason': None}
                outcome.update(current)
                self.store.audit(user, 'print_preparation', 'accepted', filename)
                return current
            self.store.mutate(STATE, accept, None)
            return dict(outcome, accepted=outcome.get('phase') != 'duplicate')

    def request_temperature(self, user, heater, target, key):
        limits = {'extruder': 300, 'heater_bed': 100}
        if heater not in limits or not _number(target, limits[heater]):
            raise ApiError('invalid_temperature')
        if not self.allowed(user):
            raise ApiError('temperature_access_revoked')
        if not isinstance(key, str) or not key:
            raise ApiError('invalid_temperature_key')
        with self.lock:
            outcome = {}
            def register(operations):
                previous = next((op for op in operations.values() if op['key'] == key), None)
                if previous:
                    outcome.update(previous)
                    return operations
                if self.stopping() or not self.allowed(user):
                    raise ApiError('temperature_access_revoked')
                if sum(op['status'] in {'pending', 'claimed'} for op in operations.values()) >= 32:
                    raise ApiError('temperature_queue_full')
                state = self._state()
                attached = state and state['phase'] in ACTIVE - {'resuming'}
                intent_id = state['id'] if attached else None
                if not self.store.claim_action('temperature:accept:' + key):
                    outcome.update(status='duplicate')
                    return operations
                for operation in operations.values():
                    if (operation['status'] == 'pending' and operation['heater'] == heater
                            and operation.get('intent_id') == intent_id):
                        operation.update(status='superseded', finished=self.clock())
                operation = {'id': uuid.uuid4().hex, 'key': key, 'user': user, 'heater': heater,
                    'target': float(target), 'status': 'pending', 'created': self.clock(), 'intent_id': intent_id}
                if attached:
                    state['desired'][heater] = float(target)
                    state['revisions'][heater] += 1
                    if heater == 'heater_bed':
                        state['bed_off_hold'] = target == 0
                    state['updated'] = self.clock()
                    operation['revision'] = state['revisions'][heater]
                    self.store.set(STATE, state)
                operations[operation['id']] = operation
                terminal = sorted((op for op in operations.values() if op['status'] not in {'pending', 'claimed'}),
                                  key=lambda op: op['created'], reverse=True)[:64]
                kept = {op['id']: op for op in terminal}
                kept.update({identity: op for identity, op in operations.items() if op['status'] in {'pending', 'claimed'}})
                outcome.update(operation)
                self.store.audit(user, 'temperature', 'pending', heater + ':' + str(target))
                return kept
            self.store.mutate(OPERATIONS, register, {})
            return dict(outcome, detail='Ожидает применения' if outcome.get('status') == 'pending' else outcome.get('status'))

    def hold(self, user, on=True):
        if not self.allowed(user) or type(on) is not bool:
            raise ApiError('preflight_access_revoked')
        with self.lock:
            def change(state):
                if state and state['phase'] in ACTIVE:
                    state.update(manual_hold=on, updated=self.clock())
                return state
            self.store.mutate(STATE, change, None)
        return self.status()

    def cancel(self, user):
        if not self.allowed(user):
            raise ApiError('preflight_access_revoked')
        with self.lock:
            state = self._state()
            if state and state['phase'] not in {'complete', 'cancelled'}:
                state.update(phase='cancelled', manual_hold=True, reason='cancelled', updated=self.clock())
                self.store.set(STATE, state)
                self._cancel_operations(state['id'], 'cancelled')
        return self.status()

    def _cancel_operations(self, intent_id, reason):
        def cancel(operations):
            for operation in operations.values():
                if operation.get('intent_id') == intent_id and operation['status'] in {'pending', 'claimed'}:
                    operation.update(status='cancelled' if operation['status'] == 'pending' else 'interrupted',
                                     reason=reason, finished=self.clock())
            return operations
        self.store.mutate(OPERATIONS, cancel, {})

    def _fail(self, state, reason, phase='failed'):
        if self.stopping():
            return
        with self.lock:
            current = self._state()
            if not current or current['id'] != state['id'] or current['phase'] not in ACTIVE:
                return
            current.update(phase=phase, reason=reason, manual_hold=True, updated=self.clock())
            self.store.set(STATE, current)
            self._cancel_operations(current['id'], reason)
            self.store.audit(current['user'], 'print_preparation', phase, reason)
        if reason == 'resume_uncertain':
            text = ('⚠️ Результат продолжения не подтверждён.\nКоманда могла выполниться, и модель может печататься. '
                    'Проверь пульт и камеру. Автоматически повторять команду не буду.')
        else:
            text = '⏸ Подготовка остановлена. Печать модели не начата.\nКод: ' + reason
        self.notify(text, 'preflight-stop:' + state['id'])

    def _valid_asset(self, state):
        asset = self.store.get('assets', {}).get(state['asset_id'], {})
        return (asset.get('ready') is True and asset.get('version') == state['asset_version']
                and asset.get('ready_filename') == state['filename'] and asset.get('gate') == state['gate'])

    def _same_job(self, state, context):
        return (context.get('ready') and state.get('job_id') is not None
                and context.get('job_id') == state['job_id'] and context.get('filename') == state['filename'])

    def _exact_gate(self, state, context):
        position = context.get('data', {}).get('virtual_sdcard', {}).get('file_position')
        return (self._same_job(state, context) and context.get('state') == 'paused'
                and type(position) in {int, float} and math.isfinite(position)
                and position == state['gate']['end'])

    @staticmethod
    def _valid_mesh(context):
        matrix = context.get('data', {}).get('bed_mesh', {}).get('mesh_matrix')
        return (isinstance(matrix, list) and len(matrix) >= 2
                and all(isinstance(row, list) and len(row) == len(matrix[0]) and len(row) >= 2
                        and all(type(value) in {int, float} and math.isfinite(value) for value in row)
                        for row in matrix))

    def _change_phase(self, state, phase, **values):
        def change(current):
            if current and current['id'] == state['id'] and current['phase'] in ACTIVE:
                current.update(phase=phase, updated=self.clock(), **values)
            return current
        self.store.mutate(STATE, change, None)

    def _prune_revoked(self):
        with self.lock:
            def prune(operations):
                state = self._state()
                changed = False
                for operation in operations.values():
                    if operation['status'] != 'pending' or self.allowed(operation['user']):
                        continue
                    operation.update(status='cancelled', reason='access_revoked', finished=self.clock())
                    if (state and operation.get('intent_id') == state['id']
                            and operation.get('revision') == state['revisions'][operation['heater']]):
                        heater = operation['heater']
                        state['desired'][heater] = state['applied'][heater]
                        state['revisions'][heater] += 1
                        if heater == 'heater_bed':
                            state['bed_off_hold'] = state.get('applied_bed_off_hold', False)
                        changed = True
                if changed:
                    self.store.set(STATE, state)
                return operations
            self.store.mutate(OPERATIONS, prune, {})

    def _set_operation(self, operation, status, reason=None):
        def update(operations):
            current = operations.get(operation['id'])
            if current and current['status'] in {'pending', 'claimed'}:
                current.update(status=status, finished=self.clock(), reason=reason)
            return operations
        self.store.mutate(OPERATIONS, update, {})

    def _start(self, state):
        with self.physical_lock:
            context = self.context()
            with self.lock:
                current = self._state()
                if not current or current['id'] != state['id'] or current['phase'] != 'accepted' or self.stopping():
                    return False
                if not self.allowed(current['user']) or not self._valid_asset(current):
                    self._fail(current, 'access_or_file_changed')
                    return False
                if not context.get('ready') or context.get('state') not in IDLE:
                    self._fail(current, 'printer_busy')
                    return False
                if current.get('qid'):
                    workflow = self.store.get('workflow', {'queue': []})
                    if (not workflow['queue'] or workflow['queue'][0].get('id') != current['qid']
                            or workflow['queue'][0].get('asset_id') != current['asset_id']):
                        self._fail(current, 'queue_changed')
                        return False
                if current.get('manual_hold'):
                    return False
                if not self.store.claim_action('preflight:start:' + current['id']):
                    self._fail(current, 'start_already_claimed', 'interrupted')
                    return False
                self._change_phase(current, 'starting', baseline_job_id=context.get('job_id'),
                                   baseline_filename=context.get('filename'), started=self.clock())
                self.store.audit(current['user'], 'start', 'claimed', current['filename'])
            try:
                self.printer.start_calibrated(current['filename'])
            except Exception as exc:
                self._fail(current, 'start_uncertain' if getattr(exc, 'uncertain', True) else exc.code)
                return True
            if self.stopping():
                return True
            self._change_phase(current, 'preparing')
            self.store.audit(current['user'], 'start', 'accepted', current['filename'])
            def running(workflow):
                workflow['running'] = {'qid': current.get('qid'), 'asset_id': current['asset_id'],
                    'filename': current['filename'], 'actor': current['user'], 'started': self.clock()}
                workflow['queue'] = [item for item in workflow.get('queue', []) if item['id'] != current.get('qid')]
                workflow['awaiting'] = False
                return workflow
            self.store.mutate('workflow', running, {'queue': [], 'running': None, 'awaiting': False})
        self.notify('🖨 Принтер прогревается и заново измеряет стол. Модель пока не печатается.',
                    'preflight-start:' + current['id'])
        return True

    def _manual_temperature(self):
        operations = self.store.get(OPERATIONS, {})
        pending = sorted((op for op in operations.values() if op['status'] == 'pending' and op.get('intent_id') is None),
                         key=lambda op: op['created'])
        if not pending:
            return False
        operation = pending[0]
        with self.physical_lock:
            context = self.context()
            with self.lock:
                operation = self.store.get(OPERATIONS, {}).get(operation['id'])
                if not operation or operation['status'] != 'pending':
                    return True
                if self.stopping():
                    return False
                if not self.allowed(operation['user']):
                    self._set_operation(operation, 'cancelled', 'access_revoked')
                    return True
                if not context.get('ready'):
                    self._set_operation(operation, 'failed', 'printer_not_ready')
                    return True
                if not self.store.claim_action('temperature:apply:' + operation['id']):
                    self._set_operation(operation, 'interrupted', 'already_claimed')
                    return True
                self._set_operation(operation, 'claimed')
            try:
                self.printer.set_temperature(operation['heater'], operation['target'])
            except Exception as exc:
                if self.stopping():
                    return True
                self._set_operation(operation, 'uncertain' if getattr(exc, 'uncertain', True) else 'failed',
                                    getattr(exc, 'code', 'temperature_internal'))
            else:
                if self.stopping():
                    return True
                self._set_operation(operation, 'applied')
        return True

    @staticmethod
    def _target(context, heater):
        value = context.get('data', {}).get(heater, {}).get('target')
        return float(value) if _number(value) else None

    @staticmethod
    def _warm(context, heater, target):
        sensor = context.get('data', {}).get(heater, {})
        return (_number(sensor.get('temperature')) and _number(sensor.get('target'))
                and abs(sensor['target'] - target) < .1
                and ((heater == 'heater_bed' and target == 0) or abs(sensor['temperature'] - target) <= 2)
                and (heater != 'extruder' or sensor['temperature'] >= 170))

    def _record_applied(self, state, heater, target):
        with self.lock:
            current = self._state()
            if current and current['id'] == state['id'] and current['phase'] in ACTIVE:
                current['applied'][heater] = target
                if heater == 'heater_bed':
                    current['applied_bed_off_hold'] = target == 0 and state.get('bed_off_hold', False)
                self.store.set(STATE, current)

    def _gate_temperature(self, state, context):
        operations = self.store.get(OPERATIONS, {})
        for heater in ('heater_bed', 'extruder'):
            desired = state['desired'][heater]
            matching = [op for op in operations.values() if op['status'] == 'pending'
                        and op.get('intent_id') == state['id'] and op['heater'] == heater
                        and op.get('revision') == state['revisions'][heater]]
            operation = matching[0] if matching else None
            if operation and self._target(context, heater) == desired:
                self._set_operation(operation, 'applied')
                self._record_applied(state, heater, desired)
                continue
            if self._target(context, heater) == desired:
                continue
            with self.physical_lock:
                fresh = self.context()
                with self.lock:
                    latest = self._state()
                    if (not latest or latest['id'] != state['id'] or latest['phase'] not in ACTIVE
                            or not self._exact_gate(latest, fresh) or self.stopping()):
                        return False
                    if not self.allowed(latest['user']) or (operation and not self.allowed(operation['user'])):
                        self._fail(latest, 'access_revoked')
                        return True
                    if latest['revisions'][heater] != state['revisions'][heater]:
                        return False
                    action_key = 'preflight:temperature:' + state['id'] + ':' + heater + ':' + str(state['revisions'][heater])
                    if not self.store.claim_action(action_key):
                        self._fail(latest, 'temperature_result_unconfirmed')
                        return True
                    if operation:
                        self._set_operation(operation, 'claimed')
                try:
                    self.printer.set_temperature(heater, desired)
                except Exception as exc:
                    if self.stopping():
                        return True
                    if operation:
                        self._set_operation(operation, 'uncertain' if getattr(exc, 'uncertain', True) else 'failed',
                                            getattr(exc, 'code', 'temperature_internal'))
                    self._fail(latest, 'temperature_uncertain' if getattr(exc, 'uncertain', True) else exc.code)
                    return True
                if self.stopping():
                    return True
                if operation:
                    self._set_operation(operation, 'applied')
                self._record_applied(state, heater, desired)
            return True
        return False

    def _recalibrate(self, state):
        bed, nozzle = state['desired']['heater_bed'], state['desired']['extruder']
        cycle = uuid.uuid4().hex
        operation_ids = []
        with self.physical_lock:
            fresh = self.context()
            with self.lock:
                current = self._state()
                if (not current or current['id'] != state['id'] or current['phase'] not in ACTIVE
                        or self.stopping() or not self._exact_gate(current, fresh)):
                    return False
                if not self.allowed(current['user']):
                    self._fail(current, 'access_revoked')
                    return True
                if current['revisions'] != state['revisions'] or current.get('manual_hold'):
                    return False
                if not self.store.claim_action('preflight:probe:' + cycle):
                    return False
                def claim(operations):
                    for operation in operations.values():
                        if (operation['status'] == 'pending' and operation.get('intent_id') == state['id']
                                and operation.get('revision') == state['revisions'][operation['heater']]):
                            if not self.allowed(operation['user']):
                                raise ApiError('temperature_access_revoked')
                            operation.update(status='claimed')
                            operation_ids.append(dict(operation))
                    return operations
                self.store.mutate(OPERATIONS, claim, {})
                self._change_phase(current, 'calibrating', cycle=cycle)
                self.store.audit(current['user'], 'new_bed_mesh', 'claimed', str(bed))
        # Deliberately outside physical_lock: CANCEL and owner controls remain
        # available while the native Klipper script waits/heats/probes.
        try:
            if self.stopping():
                return False
            current = self._state()
            if not current or current['id'] != state['id'] or current['phase'] != 'calibrating':
                return False
            self.printer.recalibrate(bed, nozzle)
            if self.stopping():
                return True
            fresh = self.context()
        except Exception as exc:
            if self.stopping():
                return True
            for operation in operation_ids:
                self._set_operation(operation, 'uncertain' if getattr(exc, 'uncertain', True) else 'failed',
                                    getattr(exc, 'code', 'mesh_internal'))
            self._fail(state, 'mesh_uncertain' if getattr(exc, 'uncertain', True) else 'mesh_failed')
            return True
        if self.stopping():
            return True
        with self.lock:
            current = self._state()
            if (not current or current['id'] != state['id'] or current.get('cycle') != cycle
                    or current['phase'] != 'calibrating'):
                return True
            if (self.stopping() or not self.allowed(current['user'])
                    or any(not self.allowed(op['user']) for op in operation_ids)
                    or not self._exact_gate(current, fresh) or not self._valid_mesh(fresh)
                    or not self._warm(fresh, 'heater_bed', bed)):
                self._fail(current, 'preparation_changed')
                return True
            current.update(phase='gate', mesh_bed=bed, applied={'heater_bed': bed, 'extruder': nozzle},
                           applied_bed_off_hold=False, updated=self.clock())
            self.store.set(STATE, current)
            for operation in operation_ids:
                self._set_operation(operation, 'applied')
            self.store.audit(current['user'], 'new_bed_mesh', 'accepted', str(bed))
        return True

    def _resume(self, state):
        with self.physical_lock:
            fresh = self.context()
            # This is the linearization point shared with request_temperature
            # and hold: no newly accepted bed target/hold can be attached to the
            # preparation after checks but before the durable resume decision.
            with self.lock:
                current = self._state()
                if (not current or current['id'] != state['id'] or current['phase'] not in GATE_PHASES
                        or self.stopping() or not self._exact_gate(current, fresh)):
                    return False
                if not self.allowed(current['user']):
                    self._fail(current, 'access_revoked')
                    return True
                bed, nozzle = current['desired']['heater_bed'], current['desired']['extruder']
                if (current.get('manual_hold') or current.get('bed_off_hold') or nozzle < 170 or current['mesh_bed'] != bed
                        or not current.get('native_verified') or not self._valid_mesh(fresh)
                        or not self._warm(fresh, 'heater_bed', bed) or not self._warm(fresh, 'extruder', nozzle)):
                    return False
                if not self.store.claim_action('preflight:resume:' + current['id']):
                    self._fail(current, 'resume_already_claimed', 'interrupted')
                    return True
                self._change_phase(current, 'resuming', model_started=self.clock())
            try:
                self.printer.resume_gate(current['gate'])
            except Exception as exc:
                self._fail(current, 'resume_uncertain' if getattr(exc, 'uncertain', True) else exc.code)
                return True
            if self.stopping():
                return True
            self._change_phase(current, 'printing')
            self.store.audit(current['user'], 'gate_resume', 'accepted', current['filename'])
        self.notify('▶️ Новый замер завершён. Печать модели началась.', 'preflight-ready:' + current['id'])
        return True

    def tick(self):
        if self.stopping() or not self.worker_lock.acquire(blocking=False):
            return False
        try:
            self._prune_revoked()
            state = self._state()
            if not state or state['phase'] not in ACTIVE:
                return self._manual_temperature()
            if not self.allowed(state['user']):
                self._fail(state, 'access_revoked', 'interrupted')
                return True
            if state['phase'] == 'accepted':
                if self._manual_temperature():
                    return True
                return self._start(state)
            if state['phase'] in {'starting', 'calibrating', 'resuming'}:
                self._fail(state, 'operation_incomplete', 'interrupted')
                return True
            context = self.context()
            if not context.get('ready'):
                self._fail(state, 'printer_not_ready', 'interrupted')
                return True
            if state.get('job_id') is None:
                if (context.get('filename') == state['filename'] and context.get('state') in {'printing', 'paused'}
                        and context.get('job_id') is not None and context['job_id'] != state.get('baseline_job_id')):
                    self._change_phase(state, 'preparing', job_id=context['job_id'])
                    return True  # Observe the same stable ID again before gate resume.
                if ((context.get('state') in {'printing', 'paused'} and context.get('filename') != state['filename'])
                        or self.clock() - state.get('started', self.clock()) > 30):
                    self._fail(state, 'job_not_confirmed', 'interrupted')
                return False
            if context.get('filename') == state['filename'] and context.get('job_id') is None:
                # History can briefly fail while status still shows our unique
                # file. Keep ownership (and the resume guard), but do not infer
                # either permission to resume or evidence of a different job.
                return False
            if not self._same_job(state, context):
                self._fail(state, 'job_changed', 'interrupted')
                return True
            if context.get('state') == 'printing' and not state.get('at_gate'):
                if self.clock() - state.get('started', self.clock()) > 1800:
                    self._fail(state, 'preparation_timeout', 'interrupted')
                return False
            if not self._exact_gate(state, context):
                self._fail(state, 'pause_position_not_confirmed', 'interrupted')
                return True
            if not state.get('native_verified'):
                if (not self._valid_mesh(context) or not self._warm(context, 'heater_bed', state['gate']['bed'])):
                    self._fail(state, 'native_mesh_not_confirmed', 'interrupted')
                    return True
            self._change_phase(state, 'gate', at_gate=True, native_verified=True)
            state = self._state()
            bed, nozzle = state['desired']['heater_bed'], state['desired']['extruder']
            if bed > 0 and nozzle >= 170 and (bed != state['mesh_bed'] or self._target(context, 'heater_bed') != bed):
                if state.get('manual_hold'):
                    # Accept heat changes during a user's hold, but never move
                    # the tool or probe until the user explicitly continues.
                    if self._gate_temperature(state, context):
                        return True
                else:
                    return self._recalibrate(state)
            if self._gate_temperature(state, context):
                return True
            if state.get('manual_hold') or state.get('bed_off_hold') or nozzle < 170:
                self._change_phase(state, 'held', reason='user_hold' if state.get('manual_hold') else 'temperature_too_low')
                return False
            if not self._warm(context, 'heater_bed', bed) or not self._warm(context, 'extruder', nozzle):
                self._change_phase(state, 'heating')
                return False
            return self._resume(state)
        except ApiError:
            # A read failure is not evidence of a failed physical action. Any
            # physical action above has its own durable, terminal outcome path.
            return False
        finally:
            self.worker_lock.release()
