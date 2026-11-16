"""Web projections and adapters over the one running Manager, not a second controller."""
from contextlib import contextmanager
import hashlib
import math
from pathlib import Path
import threading
import uuid

from adapters import ApiError, _temperature
from status_cache import StatusCache
from web_auth import WebError

ASSET_FIELDS = ('id','name','source_name','origin','source_kind','created','version','ready','preparation','size','temperatures','estimated_time','filament_grams')
JOB_FIELDS = ('id','asset_id','version','owner','source_name','kind','temperatures','status','stage','created','finished','error','progress','qid')
PREFLIGHT_FIELDS = ('id','asset_id','filename','phase','stage','detail','reason','manual_hold','desired','applied','at_gate','started')


def fields(value, names):
    return {k: value[k] for k in names if k in value} if isinstance(value, dict) else {}


class WebPresenter:
    def __init__(self):
        self.local = threading.local()

    @contextmanager
    def collecting(self, user):
        previous = getattr(self.local, 'current', None)
        cards = []
        self.local.current = (user, cards)
        try:
            yield cards
        finally:
            self.local.current = previous

    def capture(self, user, text, markup=None, view='message', data=None):
        current = getattr(self.local, 'current', None)
        if current is None:
            return False
        # A web worker must not turn a UI response into a Telegram message.
        if user == current[0]:
            current[1].append({'text':str(text), 'view':view, 'markup':markup, 'data':data or {}})
        return True


class CapturedTelegram:
    """Presentation-only adapter for AccessController's existing transition rules."""
    def __init__(self):
        self.messages = []
    def send_message(self, uid, text, markup=None):
        self.messages.append({'telegram_id':uid,'text':text,'markup':markup})
        return {'message_id':len(self.messages)}


class WebActions:
    def __init__(self, manager):
        self.manager = manager
        self.presenter = getattr(manager, 'web_presenter', None) or WebPresenter()
        manager.web_presenter = self.presenter
        self.own_cache = None

    def close(self):
        if self.own_cache:
            self.own_cache.close()

    def allowed(self, uid):
        if self.manager.stopping or not self.manager.access.is_allowed(uid):
            raise WebError('access_revoked', 'Доступ к K1C закрыт.', 403)

    def owner(self, uid):
        self.allowed(uid)
        if not self.manager.access.is_owner(uid):
            raise WebError('owner_required', 'Действие доступно только владельцу.', 403)

    def _capture(self, uid, fn):
        self.allowed(uid)
        with self.presenter.collecting(uid) as cards:
            fn()
        return cards

    def status(self, uid):
        self.allowed(uid)
        cache = self.manager.ui_status_cache
        if cache is None:
            if self.own_cache is None:
                self.own_cache = StatusCache(lambda:self.manager.context(include_job=False), stopping=lambda:self.manager.stopping)
            cache = self.own_cache
        snap = cache.read()
        context = snap.get('context') or {}
        data = context.get('data') or {}
        progress = data.get('virtual_sdcard', {}).get('progress', 0)
        progress = max(0, min(1, progress)) if isinstance(progress, (float,int)) and math.isfinite(progress) else 0
        elapsed = context.get('duration', 0)
        printer = {'ready':bool(context.get('ready')) and not bool(snap.get('error')), 'state':context.get('state','unknown'),
                   'filename':context.get('filename',''), 'progress':progress, 'elapsed':elapsed,
                   'remaining':elapsed*(1-progress)/progress if progress>.05 and isinstance(elapsed,(int,float)) else None}
        for key, dest in [('extruder','nozzle'),('heater_bed','bed')]:
            printer[dest] = fields(data.get(key,{}), ('temperature','target','power'))
        snapshot = self.manager.store.get('snapshot', {})
        if snapshot.get('filename') == printer['filename']:
            printer['job_id'] = snapshot.get('job_id')
        workflow = self.manager._workflow()
        return {'connection':{k:snap.get(k) for k in ('state','age','error','refreshing')}, 'printer':printer,
                'preflight':fields(self.manager.preflight.status(), PREFLIGHT_FIELDS),
                'camera':self.manager.store.get('camera_health',{}),
                'detector':{'auto_enabled':self.manager._auto_enabled(),'observation':self.manager.store.get('detector_observation')},
                'alarm':self.manager.alarms.current(),
                'queue':{'count':len(workflow.get('queue',[])),'awaiting':workflow.get('awaiting',False)}}

    def history(self, uid):
        self.allowed(uid)
        jobs, error = [], None
        try:
            for j in self.manager.printer.history(50):
                jobs.append(fields(j, ('job_id','filename','status','start_time','end_time','print_duration','total_duration','filament_used')))
        except ApiError as exc:
            error = exc.code
        return {'jobs':jobs,'actions':self.manager.store.recent_audit(50),'error':error}

    def files(self, uid):
        # Reuse catalog fingerprint/version merge rules, capturing only its UI.
        cards = self._capture(uid, lambda:self.manager._files(uid, 0))
        assets = sorted(self.manager._assets().values(), key=lambda a:a.get('created',0), reverse=True)
        return {'files':[fields(a,ASSET_FIELDS) for a in assets], 'cards':cards}

    def queue(self, uid):
        self.allowed(uid)
        state = self.manager._workflow()
        assets = self.manager._assets()
        return dict(state, queue=[dict(q,asset=fields(assets.get(q['asset_id'],{}),ASSET_FIELDS)) for q in state.get('queue',[])])

    def jobs(self, uid, asset_id=None, qid=None):
        self.allowed(uid)
        jobs = self.manager.store.get('slicing_jobs',{})
        result = {'jobs':[fields(j,JOB_FIELDS) for j in jobs.values()],
                  'preflight':fields(self.manager.preflight.status(),PREFLIGHT_FIELDS)}
        if asset_id:
            asset = self.manager._assets().get(asset_id)
            if not asset:
                raise WebError('file_missing', 'Файл не найден.', 404)
            result.update(asset=fields(asset,ASSET_FIELDS), cards=self._capture(uid,lambda:self.manager._file_card(uid,asset_id,qid)))
            result['confirmation'] = self._confirmation(result['cards'], uid, 'start')
        return result

    def _confirmation(self, cards, uid, action):
        tickets = self.manager.store.get('tickets',{})
        for card in cards:
            for row in (card.get('markup') or {}).get('inline_keyboard',[]):
                for button in row:
                    token = button.get('callback_data','')
                    item = tickets.get(token[2:]) if token.startswith('t:') else None
                    if item and item['action']==action and item['actor']==uid:
                        return {'token':token,'expires':item['expires'],'message':card['text']}
        return None

    def _ticket(self, uid, token, wanted):
        if not isinstance(token,str) or not token.startswith('t:'):
            raise WebError('confirmation_required','Нужно новое подтверждение действия.',409)
        item = self.manager._take_ticket(token[2:],uid)
        if not item or item['action'] != wanted:
            raise WebError('confirmation_expired','Подтверждение устарело или принадлежит другому пользователю.',409)
        payload = dict(item['payload'], _expires=item['expires'])
        return payload

    def action(self, uid, body, key):
        self.allowed(uid)
        action = body.get('action')
        sender = 'Веб · '+str(uid)
        m = self.manager
        deadline = m.clock()+60
        with self.presenter.collecting(uid) as cards, m.physical_lock:
            self.allowed(uid)
            if m.clock()>deadline:raise WebError('command_expired','Команда устарела во время ожидания. Подтверди действие заново.',409)
            if action in {'pause','resume','cancel-request'}:
                m._control('cancel' if action=='cancel-request' else action,uid,sender,key,deadline=deadline)
            elif action=='cancel-confirm':
                payload = self._ticket(uid,body.get('token'),'cancel')
                m._control('cancel',uid,sender,key,expected=payload,deadline=payload['_expires'])
            elif action in {'temperature','temp'}:
                heater = body.get('heater')
                target = float(_temperature(heater,body.get('target')))
                m.preflight.request_temperature(uid,heater,target,key)
            elif action=='light':
                if type(body.get('on')) is not bool:raise WebError('invalid_light','Выбери включение или выключение света.')
                m._light_action(body['on'],uid,sender,key,deadline)
            elif action=='power':
                self.owner(uid)
                if body.get('token'):
                    payload = self._ticket(uid,body['token'],'confirm_power_off')
                    m._power_action('confirm_power_off',payload,uid,sender,key)
                else:
                    if type(body.get('on')) is not bool:raise WebError('invalid_power','Выбери включение или выключение питания.')
                    m._power_action('power_on' if body['on'] else 'power_off',{'_expires':deadline},uid,sender,key)
            elif action=='control':
                self.owner(uid)
                if type(body.get('enabled')) is not bool:raise WebError('invalid_control','Выбери режим автоматического контроля.')
                m._detector_toggle(body['enabled'],uid,sender,deadline)
            elif action=='alarm':
                if body.get('decision') not in {'pause','continue'}:raise WebError('invalid_alarm','Выбери паузу или продолжение наблюдения.')
                m._alarm_response('a:'+str(body.get('alarm_id',''))+':'+body['decision'],uid,sender)
            else:
                raise WebError('unknown_action','Неизвестное действие.')
        return {'cards':cards, 'confirmation':self._confirmation(cards,uid,'cancel' if action=='cancel-request' else 'confirm_power_off'),
                'preflight':fields(m.preflight.status(),PREFLIGHT_FIELDS)}

    def job_action(self, uid, body, key):
        self.allowed(uid)
        m, action = self.manager, body.get('action')
        asset_id = body.get('asset_id')
        deadline=m.clock()+60
        with self.presenter.collecting(uid) as cards, m.physical_lock:
            self.allowed(uid)
            if m.clock()>deadline:raise WebError('command_expired','Команда устарела во время ожидания. Подтверди действие заново.',409)
            if action=='start-confirm':
                if body.get('table_clear') is not True or body.get('plate_installed') is not True:
                    raise WebError('table_confirmation_required','Подтверди: стол пуст и PEI-пластина установлена.',409)
                payload = self._ticket(uid,body.get('token'),'start')
                if asset_id and payload.get('asset_id')!=asset_id:raise WebError('file_changed','Подтверждение относится к другому файлу.',409)
                m._start(uid,'Веб · '+str(uid),payload,key)
            elif action in {'remove-queue','clear-queue'}:
                if action=='remove-queue' and not isinstance(body.get('qid'),str):raise WebError('invalid_queue','Задание очереди не выбрано.')
                m._workflow_callback('remove_queue' if action=='remove-queue' else 'clear_queue',body,uid,'Веб · '+str(uid),key)
            else:
                asset = m._assets().get(asset_id)
                if not asset:raise WebError('file_missing','Файл не найден.',404)
                if action=='prepare':m._prepare_start(uid,{'asset_id':asset_id,'qid':body.get('qid')})
                elif action=='enqueue':m._enqueue(uid,'Веб · '+str(uid),asset_id)
                elif action=='parameters':
                    if asset.get('source_kind')!='stl':raise WebError('gcode_parameters','Готовый G-code сохраняет свои команды температур.')
                    if body.get('version')!=asset.get('version'):raise WebError('file_changed','Параметры файла уже изменились.',409)
                    temps = body.get('temperatures')
                    if not isinstance(temps,dict) or set(temps)!={'nozzle','bed'}:raise WebError('invalid_temperature','Укажи температуры сопла и стола.')
                    nozzle, bed = float(_temperature('extruder',temps['nozzle'])), float(_temperature('heater_bed',temps['bed']))
                    if nozzle<170:raise WebError('invalid_temperature','Для STL сопло должно быть не ниже 170°C.')
                    m._queue_preparation(uid,asset,temperatures={'nozzle':nozzle,'bed':bed})
                else:raise WebError('unknown_job_action','Неизвестное действие с заданием.')
        result = {'cards':cards,'preflight':fields(m.preflight.status(),PREFLIGHT_FIELDS)}
        if asset_id:result['asset']=fields(m._assets().get(asset_id,{}),ASSET_FIELDS)
        return result

    def upload(self, uid, name, path, size):
        self.allowed(uid)
        m = self.manager
        kind = 'stl' if Path(name).suffix.lower()=='.stl' else 'gcode'
        if kind=='stl' and (not m.slicer or not m.slicer.configured):raise WebError('slicer_unavailable','Подготовка STL сейчас недоступна.',503)
        asset_id = 'w'+uuid.uuid4().hex[:16]
        destination = m.uploads/(asset_id+'.source.'+('stl' if kind=='stl' else 'gcode'))
        Path(path).replace(destination)
        asset = {'id':asset_id,'origin':'local','name':Path(name).stem+'.gcode','source_name':name,'source_kind':kind,
                 'owner':uid,'created':m.clock(),'version':1,'ready':False,'size':size,
                 'temperatures':m.store.get('stl_preferences',{}).get(str(uid),{'nozzle':240,'bed':70}), 'source_path':str(destination)}
        try:
            with self.presenter.collecting(uid):
                queued=m._queue_preparation(uid,asset,new=True)
            if not queued:
                self.allowed(uid)
                raise WebError('preparation_queue_full','Очередь подготовки заполнена. Дождись завершения текущих задач.',429)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return {'asset':fields(asset,ASSET_FIELDS)}

    def access(self, uid):
        self.owner(uid)
        state = self.manager.store.get('access_control',{})
        return [{'telegram_id':u['id'],'name':u.get('name',''),'status':u['status'],'version':u['version']} for u in state.get('users',{}).values()]

    def access_change(self, uid, body):
        self.owner(uid)
        m, target = self.manager, body.get('telegram_id')
        state = m.store.get('access_control',{}).get('users',{}).get(str(target))
        if not state or type(target) is not int:raise WebError('user_missing','Пользователь не найден.',404)
        if body.get('version')!=state.get('version'):raise WebError('access_changed','Права уже изменились. Обнови список.',409)
        action = body.get('action')
        if action not in {'approve','deny','grant','revoke','block','unblock'}:raise WebError('invalid_access_action','Неизвестное действие с доступом.')
        token = state.get('nonce') if state['status']=='pending' else 'v'+str(state['version'])
        captured = CapturedTelegram()
        m.access._transition(captured,action,target,token)
        after = m.store.get('access_control',{}).get('users',{}).get(str(target),{})
        if after.get('version')==state['version']:raise WebError('access_changed','Сейчас это действие неприменимо.',409)
        return {'telegram_id':target,'status':after['status'],'version':after['version']}
