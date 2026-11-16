"""K1C owner workflow. All physical actions pass fresh, durable checks."""
import logging
import math
import hashlib
from functools import wraps
import os
import re
import threading
import time
import uuid
from pathlib import Path

from adapters import ApiError
from panels import Panels
from status_cache import StatusCache
from camera_health import CameraHealth
from gcode_prepare import prepare_gcode, inspect_gate
from preflight import Preflight
from access import AccessController, configured_owner
from alarm import AlarmController
from detector import DetectorError
from scoring import Scorer
from video_request import clip_seconds

LOG = logging.getLogger("k1c-telegram")
ACTIVE = {"printing", "paused"}
IDLE = {"standby", "complete", "cancelled", "error"}
LABELS = {"standby": "ожидание", "printing": "печатает", "paused": "пауза", "complete": "завершено", "cancelled": "отменено", "error": "ошибка"}
KEYBOARD = {"keyboard": [["📊 Статус", "📷 Фото"], ["🎛 Управление", "💡 Свет"], ["📁 Файлы", "📋 Очередь"], ["🕓 История", "🧠 Контроль"], ["🔌 Питание", "❓ Помощь"]], "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "Статус, фото или действие…"}


def duration(value):
    minutes = max(0, int(value or 0)) // 60
    return f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes >= 60 else f"{minutes} мин"


def authorized(message, owners):
    sender = message.get("from", {}).get("id")
    return (type(sender) is int and sender in owners and message.get("chat", {}).get("type") == "private"
            and message.get("chat", {}).get("id") == sender and not message.get("from", {}).get("is_bot", False))


def physical_action(method):
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with self.physical_lock:
            return method(self, *args, **kwargs)
    return serialized


class NotConfiguredPower:
    def status(self):
        return {"configured": False, "on": None, "detail": "Розетка ещё не подключена: ждём устройство и проверку его приложения/API."}


class Manager:
    def __init__(self, config, store, printer, telegram, detector=None, power=None, clock=time.time, slicer=None, preflight=None):
        self.config, self.store, self.printer, self.telegram = config, store, printer, telegram
        legacy_users = set(config.get("allowed_user_ids", []))
        if any(type(x) is not int or x <= 0 for x in legacy_users):
            raise ValueError("invalid_allowlist")
        self.owners = {configured_owner(config)}
        self.access = AccessController(store, owner_id=configured_owner(config), initial_users=legacy_users, clock=clock)
        self.clock, self.detector = clock, detector
        self.camera_health = CameraHealth(store, self.notify, clock)
        self.slicer = slicer
        self.slicing_lock = threading.Lock()
        self.slicing_wakeup = threading.Event()
        self.power = power or NotConfiguredPower()
        self.lock = threading.RLock()
        self.physical_lock = threading.RLock()
        self.delivery_lock = threading.Lock()
        self.photo_lock = threading.Lock()
        self._photo_cache = None
        self._photo_epoch = 0
        self.stopping = False
        self.alarms = AlarmController(store, clock=clock)
        self.scorer = Scorer(store, config, clock=clock)
        self.uploads = Path(config.get("state_dir", "/var/lib/k1c-telegram")) / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._recover_slicing_jobs()
        self.panels = Panels(store, telegram, self.access.is_allowed, clock)
        self.ui_status_cache=None
        self.preflight = preflight or Preflight(store, printer, self.context, self.access.is_allowed,
                                               self.notify, self.physical_lock, clock=clock, stopping=lambda:self.stopping)

    def enable_fast_ui(self):
        if self.ui_status_cache is None:
            self.ui_status_cache=StatusCache(lambda:self.context(include_job=False),ttl=2,retry_interval=5,stopping=lambda:self.stopping)
        self.panels.enable_async()

    def close_fast_ui(self):
        if self.ui_status_cache is not None:self.ui_status_cache.close()
        self.panels.close()

    def _presentation(self,context=None,presentation=None):
        # This helper is exclusively for rendering. Physical paths call context.
        if presentation is None and context is None and self.ui_status_cache is not None:
            presentation=self.ui_status_cache.read()
        if presentation is not None:
            context=False if presentation['error'] else presentation['context']
        return context,presentation

    @staticmethod
    def _presentation_data(presentation):
        if presentation is None:return {}
        return {'status_refreshing':presentation['refreshing'],'status_generation':presentation['generation']}

    @staticmethod
    def _presentation_label(presentation):
        if not presentation or presentation.get('age') is None:return ''
        age=presentation['age']
        when=f'{age:.0f} с' if age<60 else duration(age)
        if presentation.get('error'):return 'Последняя успешная связь: '+when+' назад.'
        if presentation['state']=='stale':return '🕓 Данные '+when+' назад; проверяю обновление.'
        return ''

    def _keyboard(self, user):
        rows = [[('📊 Пульт','n:status'),('📷 Камера','n:photo')], [('🌡 Нагрев','n:temperature'),('💡 Свет','n:light')],
                [('📁 Файлы','n:files'),('📋 Очередь','n:queue')], [('🕓 История','n:history'),('🧠 Контроль','n:control')], [('🔌 Питание','n:power'),('❓ Помощь','n:help')]]
        if self.access.is_owner(user):rows.append([('👑 Админка','x:admin')])
        return self._buttons(rows)

    def _show(self,user,text,markup=None,view='message',data=None,expected=None):
        if not self.access.is_allowed(user):return None
        presenter = getattr(self, 'web_presenter', None)
        if presenter is not None and presenter.capture(user,text,markup,view,data):
            return {'web_captured':True}
        selected = self._keyboard(user) if markup is None or markup is KEYBOARD else markup
        return self.panels.show(user,text,selected,view=view,data=data,expected=expected,force=expected is None)

    def send(self,user,text,markup=None):
        return self._show(user,text,markup)

    def notify(self, text, key=None, photo=None, markup=None):
        self.store.enqueue(key or uuid.uuid4().hex, sorted(self.access.allowed_users()), text, photo, markup)

    def flush_outbox(self):
        if not self.delivery_lock.acquire(blocking=False):
            return
        try:
            self._deliver_outbox()
        finally:
            self.delivery_lock.release()

    def _deliver_outbox(self):
        for item in self.store.pending(limit=12):
            if self.stopping:
                return
            if (not self.access.is_allowed(item["recipient"])
                    or not self.camera_health.delivery_is_current(item["event_key"])):
                self.store.delivered(item["id"])
                continue
            try:
                if item["photo"]:
                    try:
                        self.telegram.send_photo(item["recipient"], item["text"], item["photo"], item["markup"])
                    except ApiError as exc:
                        if exc.http_status != 400 and exc.code not in {"invalid_photo", "invalid_jpeg"}:
                            raise
                        self.telegram.send_message(item["recipient"], item["text"] + "\nФото не удалось доставить.", item["markup"])
                else:
                    self.telegram.send_message(item["recipient"], item["text"], item["markup"])
                self.camera_health.delivered(item["event_key"])
                self.store.delivered(item["id"])
            except ApiError as exc:
                if getattr(exc, "http_status", None) in {400, 403}:
                    # Telegram cannot contact someone who has not started the bot.
                    self.store.delivered(item["id"])
                    LOG.info("recipient unavailable id=%s", item["recipient"])
                else:
                    self.store.retry(item["id"], max(exc.retry_after or 0, min(600, 5 * 2 ** min(item["attempts"], 6))))
                    LOG.warning("notification deferred reason=%s", exc.code)

    def context(self, include_job=True):
        data = self.printer.status()
        stats = data.get("print_stats") or {}
        if not isinstance(stats, dict):
            stats = {}
        ready = data.get("webhooks", {}).get("state") == "ready"
        result = {"ready": ready, "state": stats.get("state", "unknown"), "filename": stats.get("filename", ""),
                  "duration": stats.get("print_duration", 0), "total_duration": stats.get("total_duration", 0),
                  "job_id": None, "data": data, "seen_at": self.clock()}
        if include_job and ready and result["filename"]:
            try:
                latest = self.printer.history(1)
                if latest and latest[0].get("filename") == result["filename"]:
                    job = latest[0]
                    if result["state"] not in ACTIVE or job.get("status") == "in_progress":
                        result["job_id"] = job.get("job_id")
            except ApiError:
                pass
        return result

    def _fresh(self, message):
        date = message.get("date")
        return isinstance(date, (int, float)) and -10 <= self.clock() - date <= 60

    def _owner_name(self, sender):
        return str(sender.get("first_name") or sender["id"])[:60]

    def handle_update(self,update,*,consumed=False):
        callback=update.get('callback_query')
        message=callback.get('message',{}) if isinstance(callback,dict) else update.get('message',{})
        sender=callback.get('from',{}) if isinstance(callback,dict) else message.get('from',{})
        with self.panels.interaction(sender.get('id'),message=message if callback else None,fresh=not callback):
            return self._handle_update(update,consumed=consumed)

    def _handle_update(self, update, *, consumed=False):
        # Runtime consumes ordered updates before dispatching them concurrently.
        if not consumed and not self.store.consume_update(update["update_id"]):
            return
        callback=update.get('callback_query',{})
        message=callback.get('message',{}) if callback else update.get('message',{})
        sender=callback.get('from',{}) if callback else message.get('from',{})
        text=message.get('text','').strip()
        command=text.split()[0].split('@')[0].lower() if text else ''
        if (authorized({'from':sender,'chat':message.get('chat',{})},self.owners)
                and (str(callback.get('data','')).startswith('x:') or command=='/admin' or text=='👑 Админка')):
            # Reserve navigation before the access controller's direct Telegram I/O.
            self.panels.select(sender['id'],'admin')
        if self.access.handle_update(update,self.telegram):return
        if "callback_query" in update:
            self._callback(update["callback_query"], update["update_id"])
            return
        message = update.get("message", {})
        if not authorized(message, self.access.allowed_users()):
            return
        user = message["from"]["id"]
        sender = self._owner_name(message["from"])
        text = message.get("text", "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        light_actions = {"/light_on": True, "/light_off": False, "💡 Включить свет": True, "🌑 Выключить свет": False}
        controls = {"/pause": "pause", "/resume": "resume", "/cancel": "cancel", "⏸ Пауза": "pause", "▶️ Продолжить": "resume", "⛔ Отменить печать": "cancel"}
        explicit = command if command in controls or command in light_actions else text
        try:
            if self._temperature_input(message, command, text):
                return
            if "document" in message:
                self._receive_file(message)
            elif command == "/start":
                self.store.mutate("peers", lambda peers: sorted(set(peers + [user])), [])
                self._home(user)
            elif explicit in controls or explicit in light_actions:
                if not self._fresh(message):
                    self.send(user, "⌛ Команда управления старше минуты. Отправь её снова.")
                    return
                key, deadline = "update:" + str(update["update_id"]), message["date"] + 60
                if explicit in light_actions:
                    self._light_action(light_actions[explicit], user, sender, key, deadline)
                else:
                    self._control(controls[explicit], user, sender, key, deadline=deadline)
            else:
                # A slow snapshot/upload to Telegram must not hold up status or
                # physical controls for the other owners.
                self._query(user, text, command)
        except ApiError as exc:
            self._error(user, exc)

    def _home(self,user):
        self._status(user)

    def _query(self, user, text, command):
        query = text.lower().replace("ё", "е")
        try:
            seconds = clip_seconds(text)
        except ValueError:
            self.send(user,'Укажи от 1 до 300 секунд: например, «30 сек» или «видео 2 мин».');return
        if seconds is not None:
            self._request_video(user,seconds);return
        if command == "/photo" or re.search(r"фото|камер|снимок", query):
            self._send_photo(user)
        elif command == "/light" or re.search(r"свет|подсвет", query):
            self._light_card(user)
        elif command in {"/temperature", "/heat"} or text == "🌡 Нагрев":
            self._temperature_card(user)
        elif command == "/print" or text == "🎛 Управление":
            self._print_card(user)
        elif command == "/files" or "файл" in query:
            self._files(user)
        elif command == "/queue" or "очеред" in query:
            self._queue(user)
        elif command == "/history" or re.search(r"истори|кто запуск|последни.*печат", query):
            self._history(user)
        elif command == "/power" or re.search(r"питани|розетк", query):
            self._power_card(user)
        elif command == "/control" or re.search(r"брак|контрол|детектор", query):
            self._detector_card(user)
        elif command == "/status" or re.search(r"статус|температ|сопл|сколько|что.*печат|как.*принтер|готов|прогресс", query):
            self._status(user)
        else:
            self.send(user, "❓ K1C · помощь\n\nНаблюдение\n/status — прогресс и температуры\n/photo — камера · /history — история\n\nУправление\n/print — действия с печатью\n/light — свет · /power — розетка\n/control — контроль брака\n\nФайлы и задания\n/files — выбрать G-code · /queue — очередь\nПришли STL — подготовлю для PETG. После подготовки нажми «Стол пуст — печатать». Готовый G-code можно запускать или добавлять в очередь.\n\nМожно спросить: «что печатается», «сколько осталось», «покажи камеру».")

    def _request_video(self,user,seconds):
        from media_store import MediaError
        media = getattr(self,'media_client',None)
        if media is None:
            self.send(user,'Видеоархив пока не подключён.');return
        if not self.access.is_allowed(user) or self.stopping:return
        try:
            context = self.context()
            if not self.access.is_allowed(user) or self.stopping:return
            media.request_clip(user,seconds,context,notify=True)
        except MediaError as exc:
            messages = {'no_active_job':'Сейчас нет активной печати для записи.',
                        'no_stable_job_id':'Идентификатор печати ещё уточняется. Повтори через несколько секунд.',
                        'archive_full':'Видеоархив заполнен. Новые записи приостановлены; освободи место на сайте.',
                        'media_busy':'Уже готовится несколько клипов. Дождись результата.'}
            self.send(user,messages.get(exc.code,'Запись сейчас недоступна. Попробуй через несколько секунд.'));return
        self.send(user,'🎞 Сохраняю последние '+str(seconds)+' секунд текущей печати. Пришлю готовый клип сюда.')

    def _navigation(self,user,target):
        self.store.mutate('temperature_inputs',lambda items:{k:v for k,v in items.items() if k!=str(user)},{})
        if target.startswith('file:'):
            self._file_card(user,target.split(':',1)[1]);return
        if target.startswith('jobtemp:'):
            self._job_temperature_card(user,target.split(':',1)[1]);return
        handlers={'home':self._home,'status':self._status,'photo':self._send_photo,'light':self._light_card,
                  'print':self._print_card,'power':self._power_card,'files':self._files,'queue':self._queue,
                  'history':self._history,'control':self._detector_card,'help':lambda u:self._query(u,'/help','/help'),
                  'temperature':self._temperature_card,'nexttemp':lambda u:self._job_temperature_card(u,None)}
        if target in handlers:handlers[target](user)

    def _status(self,user,context=None,expected=None,presentation=None):
        context,presentation=self._presentation(context,presentation)
        metadata=self._presentation_data(presentation)
        if context is None and presentation is not None:
            self._show(user,'⏳ Проверяю связь с K1C…\n\nПульт готов. Состояние принтера уточняется в фоне.',self._keyboard(user),view='status',data=metadata,expected=expected);return
        try:
            if context is False:raise ApiError('network')
            context=context or self.context(include_job=False)
        except ApiError:
            self._show(user,'🔴 K1C · нет связи\n\nПринтер пока не отвечает. Можно открыть файлы или проверить питание.\n'+self._presentation_label(presentation),self._keyboard(user),view='status',data=metadata,expected=expected);return
        phase=self.preflight.status()
        labels={'queued':'Запуск в очереди','starting':'Начинаю подготовку стола','preparing':'Нагрев и новый адаптивный замер',
                'waiting_gate':'Ожидаю завершения замера','at_gate':'Проверяю карту перед печатью','rechecking':'Меняю нагрев и повторяю замер',
                'recalibrating':'Новый замер при итоговой температуре','held':'Пауза пользователя','printing':'Печать идёт',
                'failed':'Подготовка остановлена','interrupted':'Нужна проверка после перезапуска'}
        state=context['state'];internal=self.preflight.owns(context) and phase.get('phase')!='printing'
        title=phase.get('stage',labels.get(phase.get('phase'),phase.get('phase',''))) if internal or self.preflight.busy() else LABELS.get(state,state)
        if not context['ready']:title='Klipper не готов'
        lines=['🖨 K1C  ·  '+str(title)]
        if context['filename']:
            lines+=['','📄 '+Path(context['filename']).name]
        if state in ACTIVE and not internal:
            p=max(0,min(1,context['data'].get('virtual_sdcard',{}).get('progress',0)));bars=int(p*10)
            lines.append('▰'*bars+'▱'*(10-bars)+f'  {p*100:.0f}%')
            if p>.05 and context['duration']>0:lines.append('⏳ Осталось ≈ '+duration(context['duration']*(1-p)/p))
        if internal:
            lines+=['','Стол будет проверен заново перед первой линией.']
            if phase.get('manual_hold'):lines.append('⏸ Продолжение ждёт твоего разрешения.')
            if phase.get('detail'):lines.append(str(phase['detail'])[:160])
            if phase.get('reason')=='temperature_too_low':lines.append('Нагрев выключен или слишком низкий. Подними температуру для продолжения.')
        temps=[]
        for heater,label in [('extruder','Сопло'),('heater_bed','Стол')]:
            sensor=context['data'].get(heater,{})
            if isinstance(sensor.get('temperature'),(int,float)):temps.append(f"{label} {sensor['temperature']:.0f}° → {sensor.get('target',0):.0f}°")
        if temps:lines+=['','🌡 '+'  ·  '.join(temps)]
        label=self._presentation_label(presentation)
        if label:lines+=['',label]
        rows=[]
        if state in ACTIVE or self.preflight.busy():
            action='resume' if state=='paused' or phase.get('manual_hold') else 'pause'
            rows.append([('▶️ Продолжить' if action=='resume' else '⏸ Пауза',self._ticket(action,actor=user)),('⛔ Отменить…',self._ticket('request_cancel',actor=user))])
        rows += [[('📷 Камера','n:photo'),('🌡 Нагрев','n:temperature'),('💡 Свет','n:light')],
                 [('📁 Файлы','n:files'),('📋 Очередь','n:queue')],[('↻ Обновить','n:status'),('⋯ Ещё','n:help')]]
        if self.access.is_owner(user):rows.append([('👑 Админка','x:admin')])
        self._show(user,'\n'.join(lines),self._buttons(rows),view='status',data=metadata,expected=expected)

    def refresh_panels(self):
        records=self.panels.records()
        snapshot=self.store.get('snapshot',{})
        active=(snapshot.get('state') in ACTIVE or self.preflight.busy() or any(j.get('status') in {'queued','running'} for j in self.store.get('slicing_jobs',{}).values()))
        interval=4 if active else 15
        due=[(int(uid),r) for uid,r in records.items() if r.get('view') in {'status','temperature','file','jobtemp'} and not r.get('delivery_unknown') and self.clock()-r.get('updated_at',0)>=(1 if r.get('data',{}).get('status_refreshing') else interval) and self.access.is_allowed(int(uid))]
        context=None
        loaded=False
        presentation=None
        for user,record in due:
            try:
                view,data=record['view'],record.get('data',{})
                if view in {'status','temperature'} and not loaded:
                    loaded=True
                    if self.ui_status_cache is not None:presentation=self.ui_status_cache.read()
                    else:
                        try:context=self.context(include_job=False)
                        except ApiError:context=False
                if view=='status':self._status(user,context,expected=record.get('revision',0),presentation=presentation)
                elif view=='temperature':self._temperature_card(user,context,expected=record.get('revision',0),presentation=presentation)
                elif view=='file':self._file_card(user,data['asset_id'],data.get('qid'),expected=record.get('revision',0))
                elif view=='jobtemp':self._job_temperature_card(user,data.get('asset_id'),expected=record.get('revision',0))
            except (ApiError,PermissionError):
                LOG.info('panel refresh deferred')

    def _send_photo(self, user):
        retry = self._buttons([[('↻ Повторить фото', 'n:photo'), ('📊 Статус', 'n:status')]])
        try:
            photo = self._photo()
        except ApiError as exc:
            LOG.warning("photo acquisition failed reason=%s", exc.code)
            self.send(user, "📷 Камера сейчас недоступна\n\nНе удалось получить свежий снимок. Повтори запрос через несколько секунд.\nКод: " + exc.code, retry)
            return
        if not self.access.is_allowed(user):
            return
        try:
            self.telegram.send_photo(user, "📷 K1C · свежий кадр", photo,
                                     self._buttons([[('↻ Новый кадр', 'n:photo'), ('💡 Свет', 'n:light')], [('📊 Статус', 'n:status'), ('🎛 Управление', 'n:print')]]))
        except ApiError as exc:
            LOG.warning("photo delivery failed reason=%s", exc.code)
            self.send(user, "📤 Не удалось отправить фото в Telegram\n\nКадр с K1C получен. Попробуй запросить фото ещё раз.\nКод: " + exc.code, retry)

    def _print_card(self, user):
        if self.ui_status_cache is not None:
            self._status(user);return
        context = self.context(include_job=False)
        lines = ["🎛 K1C · управление печатью", "", LABELS.get(context["state"], context["state"]).capitalize()]
        if context["filename"]:
            lines.append("📄 " + context["filename"])
        rows = []
        if context["ready"] and context["state"] == "printing":
            rows.append([('⏸ Поставить на паузу', self._ticket('pause', actor=user))])
        elif context["ready"] and context["state"] == "paused":
            rows.append([('▶️ Продолжить печать', self._ticket('resume', actor=user))])
        if context["ready"] and context["state"] in ACTIVE:
            rows.append([('⛔ Отменить печать…', self._ticket('request_cancel', actor=user))])
        else:
            lines.append("Выбери готовый G-code в разделе «Файлы».")
        rows.append([('📷 Камера', 'n:photo'), ('💡 Свет', 'n:light')])
        rows.append([('↻ Обновить', 'n:print'), ('📊 Статус', 'n:status')])
        self.send(user, "\n".join(lines), self._buttons(rows))

    def _light_card(self, user):
        on = self.printer.light_status()
        label = "включён" if on is True else "выключен" if on is False else "состояние неизвестно"
        self.send(user, "💡 K1C · свет\n\nСейчас: " + label,
                  self._buttons([[('💡 Включить свет', self._ticket('light_on', actor=user)), ('🌑 Выключить свет', self._ticket('light_off', actor=user))], [('📷 Камера', 'n:photo'), ('📊 Статус', 'n:status')]]))

    @physical_action
    def _light_action(self, on, user, sender, key, expires):
        if self.clock() > expires:
            self.send(user, "⌛ Кнопка света устарела. Открой «Свет» заново.")
            return
        action = "light_on" if on else "light_off"
        if self.stopping or not self.access.is_allowed(user):
            return
        if not self.store.claim_action(key + ":" + action):
            return
        self.store.audit(user, action, "claimed")
        try:
            self.printer.set_light(on)
        except ApiError as exc:
            self.store.audit(user, action, "uncertain" if exc.uncertain else "failed", exc.code)
            raise
        self.store.audit(user, action, "accepted")
        # Invalidate without waiting for an unrelated in-flight camera fetch.
        # Its old epoch prevents that fetch from populating a usable cache.
        self._photo_epoch += 1
        self._photo_cache = None
        self.notify("💡 " + sender + (" включил свет K1C." if on else " выключил свет K1C."),
                    "action:" + key + ":" + action,
                    markup=self._buttons([[('📷 Камера', 'n:photo'), ('💡 Свет', 'n:light')]]))

    def _error(self, user, exc):
        if exc.code.startswith(("light_", "invalid_light")):
            text = ("⚠️ Результат команды света не подтверждён\n\nПроверь состояние света перед повтором."
                    if exc.uncertain else "💡 Свет сейчас недоступен\n\nНе удалось связаться с управлением подсветкой. Попробуй ещё раз.")
            self.send(user, text + "\nКод: " + exc.code,
                      self._buttons([[('💡 Проверить свет', 'n:light'), ('📷 Камера', 'n:photo')]]))
            return
        if exc.uncertain:
            self.send(user, "⚠️ Результат команды не подтверждён\n\nСвязь оборвалась во время выполнения. Проверь «Статус» перед повтором.",
                      self._buttons([[('📊 Статус', 'n:status'), ('📷 Камера', 'n:photo')]]))
        else:
            text = "🔴 K1C · запрос не выполнен\n\nПроверь связь с принтером и повтори запрос.\nКод: " + exc.code
            self.send(user, text, self._buttons([[('📊 Проверить статус', 'n:status'), ('📷 Камера', 'n:photo')]]))

    def _ticket(self, action, payload=None, actor=None, ttl=60):
        token = uuid.uuid4().hex[:16]
        now = self.clock()
        item = {"action": action, "payload": payload or {}, "actor": actor, "expires": now + ttl}
        selected=[]
        def add(items):
            items={key:value for key,value in items.items() if value['expires']>=now}
            for key,value in items.items():
                if value['action']==action and value['payload']==item['payload'] and value['actor']==actor:
                    selected.append(key);return items
            items[token]=item;selected.append(token);return items
        self.store.mutate('tickets',add,{})
        return 't:'+selected[0]

    def _take_ticket(self, token, actor):
        selected = []
        def take(items):
            item = items.get(token)
            if item and item["expires"] >= self.clock() and item["actor"] in {None, actor}:
                selected.append(item)
                del items[token]
            return items
        self.store.mutate("tickets", take, {})
        return selected[0] if selected else None

    def _buttons(self, rows):
        return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}

    def _temperature_card(self,user,context=None,expected=None,presentation=None):
        context,presentation=self._presentation(context,presentation)
        metadata=self._presentation_data(presentation)
        if context is None and presentation is not None:
            self._show(user,'⏳ Проверяю текущие температуры…\n\nСостояние нагрева уточняется в фоне.',self._keyboard(user),view='temperature',data=metadata,expected=expected);return
        try:
            if context is False:raise ApiError('network')
            context=context or self.context(include_job=False)
        except ApiError:
            self._show(user,'🌡 Нагрев · принтер пока недоступен.\n'+self._presentation_label(presentation),view='temperature',data=metadata,expected=expected);return
        lines=['🌡 K1C · нагрев сейчас','']
        rows=[]
        for heater,label,maximum in [('extruder','Сопло',300),('heater_bed','Стол',100)]:
            sensor=context['data'].get(heater,{})
            target=float(sensor.get('target',0))
            lines.append(f"{label}: {sensor.get('temperature',0):.0f}° → {target:.0f}°")
            rows.append([(label+' −5°',self._ticket('heat_delta',{'heater':heater,'delta':-5},user)),
                         (label+' +5°',self._ticket('heat_delta',{'heater':heater,'delta':5},user)),
                         ('Числом…',self._ticket('heat_input',{'heater':heater},user))])
        status=self.preflight.status()
        if self.preflight.busy():
            desired=status.get('desired',{})
            if desired:lines+=['',f"Для печати запрошено: сопло {desired.get('extruder',0):.0f}° · стол {desired.get('heater_bed',0):.0f}°"]
        if self.preflight.busy():lines+=['','Во время замера запрос примется сейчас, нагрев изменится после освобождения контроллера.']
        operations=self.store.get('temperature_operations',{})
        if isinstance(operations,dict):
            values=operations.get('operations',operations)
            values=list(values.values()) if isinstance(values,dict) else values if isinstance(values,list) else []
            pending=[op for op in values if isinstance(op,dict) and op.get('status') not in {'applied','failed','cancelled','done','superseded','duplicate'}]
            for op in pending[-4:]:
                label='сопло' if op.get('heater')=='extruder' else 'стол'
                state='результат неизвестен' if op.get('status') in {'uncertain','interrupted'} else 'ожидает применения'
                lines.append(f"⏳ Запрос: {label} {op.get('target',0):.0f}° — {state}.")
        label=self._presentation_label(presentation)
        if label:lines+=['',label]
        rows += [[('PETG 240° / 70°',self._ticket('heat_baseline',actor=user)),('Выключить нагрев',self._ticket('heat_off',actor=user))],
                 [('Для следующего STL','n:nexttemp'),('↻ Обновить','n:temperature')],[('🏠 Пульт','n:home')]]
        self._show(user,'\n'.join(lines),self._buttons(rows),view='temperature',data=metadata,expected=expected)

    def _heat_delta(self,user,payload,key):
        heater,delta=payload.get('heater'),payload.get('delta')
        limits={'extruder':300,'heater_bed':100}
        if heater not in limits or type(delta) not in {int,float} or delta not in {-5,5}:raise ApiError('invalid_temperature')
        context=self.context(include_job=False)
        if self.clock()>payload['_expires']:
            self.send(user,'Кнопка нагрева устарела во время проверки. Открой нагрев снова.');return
        if not self.access.is_allowed(user) or self.stopping:return
        if not context['ready']:raise ApiError('printer_not_ready')
        state=self.preflight.status()
        attached=state.get('phase') in {'accepted','starting','preparing','heating','calibrating','gate','held'}
        base=state.get('desired',{}).get(heater) if attached else None
        if base is None:
            pending=[op for op in self.store.get('temperature_operations',{}).values()
                     if op.get('heater')==heater and op.get('intent_id') is None and op.get('status') in {'pending','claimed'} and self.access.is_allowed(op.get('user'))]
            pending.sort(key=lambda op:op.get('created',0))
            base=pending[-1]['target'] if pending else context['data'].get(heater,{}).get('target')
        if type(base) not in {int,float} or not math.isfinite(base):raise ApiError('heater_state_unknown')
        self.preflight.request_temperature(user,heater,max(0,min(limits[heater],base+delta)),key)
        self._temperature_card(user)

    def _job_temperature_card(self,user,asset_id,expected=None):
        asset=self._assets().get(asset_id) if asset_id else None
        if asset_id and (not asset or asset.get('source_kind')!='stl'):
            self._show(user,'Готовый G-code сохраняет свои температурные команды. Параметры подготовки можно менять у STL.',expected=expected);return
        temps=dict(asset.get('temperatures',{'nozzle':240,'bed':70}) if asset else self.store.get('stl_preferences',{}).get(str(user),{'nozzle':240,'bed':70}))
        lines=['🌡 '+('Температуры этой модели' if asset else 'Новые STL · твои настройки'),'',
               '📄 '+asset.get('source_name',asset['name']) if asset else 'Применятся только к новым STL, отправленным тобой.',
               f"Сопло {temps['nozzle']:.0f}°  ·  стол {temps['bed']:.0f}°"]
        if asset:lines+=['','Изменение заново подготовит и передаст файл. Старое подтверждение запуска перестанет действовать.']
        rows=[]
        for heater,label in [('nozzle','Сопло'),('bed','Стол')]:
            payload={'asset_id':asset_id,'version':asset.get('version') if asset else None,'heater':heater}
            rows.append([(label+' −5°',self._ticket('model_delta',dict(payload,delta=-5),user,300)),
                         (label+' +5°',self._ticket('model_delta',dict(payload,delta=5),user,300)),
                         ('Числом…',self._ticket('model_input',payload,user,300))])
        rows.append([('По умолчанию 240° / 70°',self._ticket('model_reset',{'asset_id':asset_id,'version':asset.get('version') if asset else None},user,300))])
        rows.append([('📄 Вернуться к файлу','n:file:'+asset_id)] if asset_id else [('🌡 Нагрев сейчас','n:temperature')])
        self._show(user,'\n'.join(lines),self._buttons(rows),view='jobtemp',data={'asset_id':asset_id},expected=expected)

    def _set_model_temperature(self,user,payload,value=None):
        asset_id=payload.get('asset_id');asset=self._assets().get(asset_id) if asset_id else None
        if asset_id and (not asset or asset.get('source_kind')!='stl' or asset.get('version')!=payload.get('version')):
            self.send(user,'Параметры уже изменились. Обновил карточку.');self._job_temperature_card(user,asset_id);return
        temps=dict(asset.get('temperatures',{'nozzle':240,'bed':70}) if asset else self.store.get('stl_preferences',{}).get(str(user),{'nozzle':240,'bed':70}))
        if payload.get('reset'):temps={'nozzle':240,'bed':70}
        else:
            heater=payload['heater'];target=float(value if value is not None else temps[heater]+payload['delta'])
            if not (170<=target<=300 if heater=='nozzle' else 0<=target<=100):
                self.send(user,'Для STL: сопло 170–300°, стол 0–100°.');return
            temps[heater]=target
        if asset:
            self._queue_preparation(user,asset,temperatures=temps)
        else:
            self.store.mutate('stl_preferences',lambda prefs:dict(prefs,**{str(user):temps}),{})
        self._job_temperature_card(user,asset_id)

    def _ask_temperature(self,user,payload,model=False):
        item=dict(payload,kind='model' if model else 'heat',expires=self.clock()+300)
        self.store.mutate('temperature_inputs',lambda items:dict(items,**{str(user):item}),{})
        heater=payload['heater'];label='сопла' if heater in {'nozzle','extruder'} else 'стола'
        self._show(user,'🌡 Введи температуру '+label+' одним числом.\nНапример: 240\n\n/cancel_input — отменить.',self._buttons([[('Отмена','n:temperature')]]),view='input')

    def _temperature_input(self,message,command,text):
        user=message['from']['id'];sender=self._owner_name(message['from'])
        pending=self.store.get('temperature_inputs',{}).get(str(user))
        if command=='/cancel_input':
            self.store.mutate('temperature_inputs',lambda values:{k:v for k,v in values.items() if k!=str(user)},{})
            self._temperature_card(user);return True
        direct=command in {'/nozzle','/bed'}
        numeric=re.fullmatch(r'\d{1,3}(?:[.,]\d{1,2})?',text)
        if not direct and not (pending and numeric):
            if pending:self.store.mutate('temperature_inputs',lambda values:{k:v for k,v in values.items() if k!=str(user)},{})
            return False
        if not self._fresh(message):self.send(user,'Команда нагрева устарела. Отправь её снова.');return True
        if direct:
            parts=text.split()
            if len(parts)!=2 or not re.fullmatch(r'\d{1,3}(?:[.,]\d{1,2})?',parts[1]):
                self.send(user,'Напиши /nozzle 240 или /bed 70.');return True
            value=float(parts[1].replace(',','.'));heater='extruder' if command=='/nozzle' else 'heater_bed'
        else:
            if pending['expires']<self.clock():self.send(user,'Ввод температуры истёк. Открой нагрев заново.');return True
            value=float(text.replace(',','.'));heater=pending['heater']
            self.store.mutate('temperature_inputs',lambda values:{k:v for k,v in values.items() if k!=str(user)},{})
            if pending['kind']=='model':self._set_model_temperature(user,pending,value);return True
        if not 0<=value<=(300 if heater=='extruder' else 100):
            self.send(user,'Сопло: 0–300°, стол: 0–100°. Ноль выключает нагрев.');return True
        self.preflight.request_temperature(user,heater,value,'message:'+str(user)+':'+str(message['message_id'])+':heat')
        self._temperature_card(user)
        return True

    def _cancel_preflight_lag(self,user,sender,key,context,pf,expected,deadline):
        if not pf.get('id'):return False
        if pf.get('phase')=='cancelled' and pf.get('job_id') is not None and context.get('job_id')==pf.get('job_id') and context.get('filename')==pf.get('filename'):
            self.send(user,'Отмена уже отправлена. Обнови статус, чтобы проверить результат.');return True
        if context['state'] in ACTIVE:return False
        if pf.get('phase')=='accepted':
            if expected and expected.get('preflight_id')!=pf['id']:
                self.send(user,'Подготовка изменилась. Подтверди отмену заново.');return True
            if self.stopping or not self.access.is_allowed(user):return True
            if deadline is not None and self.clock()>deadline:return True
            self.preflight.cancel(user);self._status(user);return True
        if not pf.get('started') or pf.get('phase') in {'printing','complete','cancelled'}:return False
        baseline=(context.get('filename')==pf.get('baseline_filename') and context.get('job_id')==pf.get('baseline_job_id'))
        matched=(context.get('filename')==pf.get('filename') and (pf.get('job_id') is None or context.get('job_id')==pf.get('job_id')))
        if not context['ready'] or not (baseline or matched):
            self.send(user,'Состояние изменилось. Автоматически отменять другое задание не буду. Обнови пульт.');return True
        if expected is None:
            payload={'preflight_id':pf['id'],'lag_cancel':True}
            self.send(user,'⛔ Отменить подготовку?\n'+pf['filename'],self._buttons([[('Да, отменить',self._ticket('cancel',payload,user))],[('Оставить подготовку',self._ticket('dismiss',actor=user))]]));return True
        if expected.get('preflight_id')!=pf['id'] or not expected.get('lag_cancel'):
            self.send(user,'Подготовка изменилась. Подтверди отмену заново.');return True
        if self.stopping or not self.access.is_allowed(user) or (deadline is not None and self.clock()>deadline):return True
        if not self.store.claim_action('preflight:cancel:'+pf['id']):return True
        self.preflight.cancel(user)
        self.store.audit(user,'cancel','claimed',pf['filename'])
        try:self.printer.cancel()
        except ApiError as exc:
            self.store.audit(user,'cancel','uncertain' if exc.uncertain else 'failed',exc.code);raise
        self.store.audit(user,'cancel','accepted',pf['filename'])
        self.notify('⛔ '+sender+' отменил подготовку печати.','preflight-cancel:'+pf['id'])
        self._failed_queue(dict(context,filename=pf['filename'],job_id=pf.get('job_id')))
        return True

    @physical_action
    def _control(self, action, user, sender, key, expected=None, deadline=None):
        context = self.context()
        pf=self.preflight.status()
        lag=(context['state'] in IDLE and ((pf.get('phase')=='accepted') or (pf.get('started') and context.get('filename')==pf.get('baseline_filename') and context.get('job_id')==pf.get('baseline_job_id') and pf.get('phase') not in {'printing','complete','cancelled'})))
        internal=self.preflight.owns(context) or bool(lag)
        if internal and action in {'pause','resume'}:
            if deadline is not None and self.clock()>deadline:return
            if not self.access.is_allowed(user):return
            if pf.get('phase') in {'failed','interrupted'}:
                self.send(user,'Замер стола не подтверждён. Отмени это задание и запусти его заново: продолжение по старой карте запрещено.',self._buttons([[('⛔ Отменить…',self._ticket('request_cancel',actor=user)),('🏠 Пульт','n:home')]]));return
            self.preflight.hold(user,action=='pause')
            self.store.audit(user,'preflight_hold','held' if action=='pause' else 'released')
            position=context['data'].get('virtual_sdcard',{}).get('file_position')
            before_gate=type(position) in {int,float} and math.isfinite(position) and 0<=position<pf.get('gate',{}).get('end',0)
            if (action=='resume' and pf.get('phase') not in {'failed','interrupted'} and not pf.get('at_gate') and before_gate
                    and context['state']=='paused' and context['data'].get('pause_resume',{}).get('is_paused')):
                # An explicit user's PAUSE before our M25 may be released only
                # by the user's explicit Continue, never by the worker.
                if self.store.claim_action(key+':pre-gate-resume'):self.printer.resume()
            self._status(user);return
        if action=='cancel' and self._cancel_preflight_lag(user,sender,key,context,pf,expected,deadline):return
        wanted = {"pause": {"printing"}, "resume": {"paused"}, "cancel": ACTIVE}[action]
        if not context["ready"] or context["state"] not in wanted:
            self.send(user, "Сейчас это действие неприменимо: " + LABELS.get(context["state"], context["state"]))
            return
        if expected and not self._same_job(expected, context):
            self.send(user, "Задание изменилось. Подтверждение отмены больше не действует.")
            return
        if action == "cancel" and expected is None:
            if not context.get("job_id") and not internal:
                self.send(user, "Не удалось подтвердить ID текущей печати. Обнови статус и попробуй ещё раз; без идентификатора старое подтверждение отмены нельзя безопасно привязать к заданию.")
                return
            data = self._ticket("cancel", self._job_ref(context), user)
            self.send(user, "⛔ Отменить текущую печать?\n" + context["filename"], self._buttons([[('Да, отменить', data)], [('Оставить печать', self._ticket('dismiss', actor=user))]]))
            return
        if deadline is not None and self.clock() > deadline:
            self.send(user, "Команда устарела во время проверки связи. Отправь её снова.")
            return
        if self.stopping or not self.access.is_allowed(user):
            return
        if not self.store.claim_action(key + ":" + action):
            self.send(user, "Эта команда уже обработана.")
            return
        self.store.audit(user, action, "claimed", context["filename"])
        try:
            if action=='cancel':self.preflight.cancel(user)
            getattr(self.printer, action)()
        except ApiError as exc:
            self.store.audit(user, action, "uncertain" if exc.uncertain else "failed", exc.code)
            raise
        self.store.audit(user, action, "accepted", context["filename"])
        verbs = {"pause": "поставил печать на паузу", "resume": "отправил команду продолжения", "cancel": "отменил печать"}
        self.notify("🖨 " + sender + " " + verbs[action] + ".\n" + context["filename"], "action:" + key + ":" + action)
        if action == "cancel":
            self._failed_queue(context)

    def _job_ref(self, context):
        result={key:context.get(key) for key in ('job_id','filename','total_duration')}
        if self.preflight.owns(context):result['preflight_id']=self.preflight.status().get('id')
        return result

    def _same_job(self, expected, context):
        if expected.get('preflight_id'):
            return self.preflight.owns(context) and expected['preflight_id']==self.preflight.status().get('id')
        return expected.get('job_id') is not None and expected['job_id']==context.get('job_id')

    def _callback(self, callback, update_id):
        message = callback.get("message", {})
        sender = callback.get("from", {})
        check = {"from": sender, "chat": message.get("chat", {})}
        if not authorized(check, self.access.allowed_users()):
            return
        user, data = sender["id"], callback.get("data", "")
        try:
            self.telegram.answer_callback(callback["id"])
        except ApiError as exc:
            # A transient Telegram ack error must not consume and lose a valid
            # printer action before it reaches the durable command checks.
            LOG.warning("callback acknowledgement failed reason=%s", exc.code)
        try:
            if data.startswith("n:"):
                self._navigation(user, data[2:])
                return
            if data.startswith("a:"):
                self._alarm_response(data, user, self._owner_name(sender))
                return
            if not data.startswith("t:"):
                return
            ticket = self._take_ticket(data[2:], user)
            if ticket is None:
                old = self.store.get("tickets", {}).get(data[2:])
                if (old and old.get("actor") == user and old.get("action") == "start"
                        and old.get("expires", 0) < self.clock()):
                    asset_id = old.get("payload", {}).get("asset_id")
                    if self._assets().get(asset_id, {}).get("ready"):
                        self.send(user, "⌛ Подтверждение запуска истекло. Обновил карточку: проверь стол перед новым нажатием.")
                        self._file_card(user, asset_id)
                        return
                self.send(user, "⌛ Кнопка устарела или уже использована. Открой нужное меню снова.")
                return
            action, payload = ticket["action"], ticket["payload"]
            payload["_expires"] = ticket["expires"]
            if action == "cancel":
                self._control("cancel", user, self._owner_name(sender), "ticket:" + data[2:], payload, ticket["expires"])
            elif action == "dismiss":
                self.send(user, "Действие отменено.")
            else:
                self._workflow_callback(action, payload, user, self._owner_name(sender), "ticket:" + data[2:])
        except ApiError as exc:
            self._error(user, exc)

    def _workflow(self):
        return self.store.get("workflow", {"queue": [], "running": None, "awaiting": False})

    def _assets(self):
        return self.store.get("assets", {})

    def _files(self, user, page=0):
        assets = self._assets()
        observed={key:value.get('version',0) for key,value in assets.items()}
        remote = []
        offline = False
        try:
            for item in self.printer.files(1000):
                filename = item["path"]
                if Path(filename).name.startswith('k1c-ready-'):
                    continue
                if Path(filename).suffix.lower() not in {".gcode", ".g", ".gco", ".nc"}:
                    continue
                asset_id = "r" + hashlib.sha256(filename.encode()).hexdigest()[:16]
                old=assets.get(asset_id,{})
                fingerprint=[item.get('modified',0),item.get('size',0)]
                if old.get('fingerprint')==fingerprint:assets[asset_id]=old
                else:assets[asset_id]={'id':asset_id,'name':filename,'source_name':filename,'source_filename':filename,'server_filename':filename,'size':item.get('size',0),'origin':'printer','source_kind':'remote','created':item.get('modified',0),'fingerprint':fingerprint,'version':old.get('version',0)+1,'ready':False}
                remote.append(asset_id)
        except ApiError:
            offline = True
        updates={key:assets[key] for key in remote}
        def merge(current):
            for key,value in updates.items():
                if current.get(key,{}).get('fingerprint')!=value.get('fingerprint'):
                    if key in current and current[key].get('version',0)!=observed.get(key,0):continue
                    value=dict(value,version=current.get(key,{}).get('version',0)+1)
                    current[key]=value
            return current
        assets=self.store.mutate('assets',merge,{})
        local = sorted((key for key, value in assets.items() if value["origin"] == "local"), key=lambda key: assets[key]["created"], reverse=True)
        local_paths = {assets[key].get("server_filename") for key in local}
        ids = local + [key for key in remote if assets[key]["server_filename"] not in local_paths]
        pages = max(1, (len(ids) + 7) // 8)
        page = max(0, min(int(page), pages - 1))
        rows = [[(assets[key]["name"][-58:], self._ticket("file", {"asset_id": key}, user, 300))] for key in ids[page * 8:(page + 1) * 8]]
        navigation = []
        if page:
            navigation.append(("← Пред. страница", self._ticket("files_page", {"page": page - 1}, user, 300)))
        if page + 1 < pages:
            navigation.append(("След. страница →", self._ticket("files_page", {"page": page + 1}, user, 300)))
        if navigation:
            rows.append(navigation)
        message = "📁 Файлы. Выбери задание или пришли готовый .gcode."
        message += "\nСтраница " + str(page + 1) + "/" + str(pages)
        if offline:
            message += "\nПринтер недоступен: показаны сохранённые на VPS файлы."
        if not ids:
            message += "\nПока нет доступных файлов."
        self._show(user,message,self._buttons(rows) if rows else self._keyboard(user),view='files',data={'page':page})

    def _receive_file(self, message):
        user, document = message['from']['id'], message['document']
        name = Path(str(document.get('file_name', '')).replace('\\', '/')).name
        kind = 'stl' if Path(name).suffix.lower() == '.stl' else 'gcode'
        if (not name or Path(name).suffix.lower() not in {'.stl', '.gcode', '.g', '.gco', '.nc'}
                or any(ord(c) < 32 for c in name) or len(name.encode()) > 180
                or not isinstance(document.get('file_id'), str)):
            self.send(user, 'Пришли STL для PETG или готовый G-code. 3MF пока не поддерживается.')
            return
        size = document.get('file_size', 0)
        if type(size) is not int or not 0 <= size <= 20 * 1024 * 1024:
            self.send(user, 'Файл должен быть не больше 20 МБ. Большой G-code загрузи через OrcaSlicer и выбери в «Файлах».')
            return
        if kind == 'stl' and (not self.slicer or not self.slicer.configured):
            self.send(user, 'Подготовка STL пока недоступна. Можно прислать готовый G-code.')
            return
        asset_id = 's' + uuid.uuid4().hex[:16]
        temperatures = self.store.get('stl_preferences', {}).get(str(user), {'nozzle': 240, 'bed': 70})
        asset = {'id': asset_id, 'origin': 'local', 'name': Path(name).stem + '.gcode', 'source_name': name,
                 'source_kind': kind, 'file_id': document['file_id'], 'owner': user, 'created': self.clock(),
                 'version': 1, 'ready': False, 'size': size, 'temperatures': temperatures,
                 'source_path': str(self.uploads / (asset_id + '.source.' + ('stl' if kind == 'stl' else 'gcode')))}
        if self._queue_preparation(user,asset,new=True):self._file_card(user,asset_id)

    def _recover_slicing_jobs(self):
        def recover(jobs):
            for job in jobs.values():
                if job.get('status') in {'queued','running'} and 'asset_id' not in job:
                    job.update(status='failed',error='preparation_update_required')
                    if self.access.is_allowed(job.get('owner')):
                        self.store.enqueue('preparation-migration:'+job['id'],[job['owner']],'Подготовка файлов обновлена. Выбери сохранённый G-code в «Файлах» или пришли STL снова.')
                if job.get('status') == 'running':
                    job['status'] = 'queued'
                    job.pop('run_token', None)
                if not self.access.is_allowed(job.get('owner')) and job.get('status') in {'queued', 'running'}:
                    job.update(status='failed', error='access_revoked')
            return jobs
        jobs = self.store.mutate('slicing_jobs', recover, {})
        for job in jobs.values():
            if job.get('status') in {'ready', 'failed'}:
                self._cleanup_slicing_files(job['id'])

    def _cleanup_slicing_files(self, job_id):
        job = self.store.get('slicing_jobs', {}).get(job_id, {})
        if job.get('status') not in {'ready', 'failed'}:
            return
        assets = self._assets()
        keep = {a.get('local_path') for a in assets.values()}
        for asset in assets.values():
            if asset.get('ready') or asset.get('version', 0) > job.get('version', 0):
                keep.add(asset.get('source_path'))
        candidates = [job.get(key) for key in ('source_path', 'raw_path', 'wrapper_path', 'verify_path')]
        # Also clean files produced by the previous deployed worker schema.
        candidates += [str(self.uploads / (job_id + suffix)) for suffix in ('.stl', '.gcode')]
        for value in candidates:
            if value and value not in keep:
                path = Path(value)
                if path.parent != self.uploads:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOG.warning('preparation temporary file cleanup deferred')

    def _queue_preparation(self, user, asset, new=False, temperatures=None, qid=None):
        if not self.access.is_allowed(user):
            return
        asset = dict(asset)
        if temperatures is not None:
            asset.update(temperatures=dict(temperatures), version=asset.get('version', 0) + 1)
        elif not new:
            current = self._assets().get(asset['id'], asset)
            active = next((j for j in self.store.get('slicing_jobs', {}).values()
                           if j.get('asset_id', j['id']) == asset['id'] and j.get('version', 1) == current.get('version', 1)
                           and j.get('status') in {'queued', 'running'}), None)
            if active:
                return active['id']
            asset['version'] = current.get('version', 0) + 1
        asset.setdefault('version', 1)
        asset.update(ready=False, preparation='queued')
        asset.setdefault('source_kind', 'remote' if asset.get('origin') == 'printer' else 'gcode')
        asset.setdefault('source_name', asset['name'])
        asset.setdefault('source_filename', asset.get('server_filename'))
        asset.setdefault('source_path',asset.get('local_path') or str(self.uploads / (asset['id'] + '.source.gcode')))
        job_id = 'p' + uuid.uuid4().hex[:16]
        job = {'id': job_id, 'asset_id': asset['id'], 'version': asset['version'], 'owner': user,
               'source_name': asset['source_name'], 'kind': asset['source_kind'], 'file_id': asset.get('file_id'),
               'source_filename': asset.get('source_filename'), 'source_path': asset['source_path'],
               'temperatures': dict(asset.get('temperatures', {'nozzle': 240, 'bed': 70})),
               'status': 'queued', 'stage': 'queued', 'created': self.clock(), 'attempts': 0, 'qid': qid,
               'next_attempt': self.clock() + (2 if temperatures is not None else 0),
               'raw_path': str(self.uploads / (job_id + '.raw.gcode')),
               'wrapper_path': str(self.uploads / (job_id + '.ready.gcode')),
               'verify_path': str(self.uploads / (job_id + '.verify.gcode'))}
        def queue(jobs):
            if temperatures is not None and self._assets().get(asset['id'],{}).get('version')!=asset['version']-1:
                raise ApiError('preparation_asset_changed')
            active = [j for j in jobs.values() if j.get('status') in {'queued', 'running'}
                      and j.get('asset_id', j['id']) != asset['id']]
            if len(active) >= 8:
                raise ApiError('slicing_queue_full')
            for previous in jobs.values():
                if previous.get('asset_id', previous['id']) == asset['id'] and previous.get('status') in {'queued', 'running'}:
                    previous.update(status='failed', error='superseded')
            terminal = sorted((j for j in jobs.values() if j.get('status') not in {'queued', 'running'}), key=lambda j: j['created'], reverse=True)[:64]
            result = {j['id']: j for j in terminal + active}
            result[job_id] = job
            self.store.mutate('assets', lambda assets: dict(assets, **{asset['id']: asset}), {})
            return result
        try:
            self.store.mutate('slicing_jobs', queue, {})
        except ApiError as exc:
            if exc.code=='preparation_asset_changed':
                self._job_temperature_card(user,asset['id']);return None
            if exc.code != 'slicing_queue_full':
                raise
            self.send(user, 'Очередь подготовки заполнена (8 моделей). Подожди завершения текущих задач.')
            return None
        self.store.audit(user, 'prepare', 'queued', asset['source_name'])
        self.slicing_wakeup.set()
        return job_id

    @staticmethod
    def _slicing_measure(value):
        return float(value) if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None

    def _job_current(self, job):
        if not self.access.is_allowed(job['owner']):
            raise ApiError('slicer_access_revoked')
        asset = self._assets().get(job.get('asset_id', job['id']), {})
        if asset.get('version') != job.get('version', 1):
            raise ApiError('preparation_superseded')
        return asset

    def _preparation_stage(self, job, stage, progress=None):
        def update(jobs):
            current = jobs.get(job['id'], {})
            if current.get('run_token') == job['run_token'] and current.get('status') == 'running':
                current['stage'] = stage
                if progress is not None:
                    current['progress'] = progress
            return jobs
        self.store.mutate('slicing_jobs', update, {})

    def process_slicing_job(self):
        """Prepare, upload and verify one version before offering any start."""
        if self.stopping or not self.slicing_lock.acquire(blocking=False):
            return False
        job = None
        try:
            token, claimed = uuid.uuid4().hex, []
            def claim(jobs):
                pending = sorted((j for j in jobs.values() if j.get('status') == 'queued' and j.get('next_attempt', 0) <= self.clock()), key=lambda j: j['created'])
                if pending:
                    selected = pending[0]
                    selected.update(status='running', run_token=token, attempts=selected.get('attempts', 0) + 1)
                    claimed.append(dict(selected))
                return jobs
            self.store.mutate('slicing_jobs', claim, {})
            if not claimed:
                return False
            job = claimed[0]
            asset = self._job_current(job)
            source = Path(job['source_path'])
            self._preparation_stage(job, 'download')
            if job['kind']=='remote' or not (source.is_file() and not source.is_symlink() and source.stat().st_size > 0):
                if job.get('file_id'):
                    self.telegram.download_file(job['file_id'], source, max_bytes=20 * 1024 * 1024)
                else:
                    self.printer.download_gcode(job['source_filename'], source)
            if self.stopping:
                return True
            self._job_current(job)
            result = {}
            raw = source
            if job['kind'] == 'stl':
                if not self.slicer or not self.slicer.configured:
                    raise ApiError('slicer_not_configured')
                self._preparation_stage(job, 'slicing')
                raw = Path(job['raw_path'])
                result = self.slicer.slice(source, raw, temperatures=job['temperatures'])
            if self.stopping:
                return True
            self._job_current(job)
            self._preparation_stage(job, 'gate')
            gate_token = uuid.uuid4().hex[:16]
            prepare_gcode(raw, job['wrapper_path'], gate_token)
            self._job_current(job)
            self._preparation_stage(job, 'upload', 0)
            last_progress = [0.0]
            def progress(sent, total):
                now = self.clock()
                if now - last_progress[0] >= .5 or sent == total:
                    self._preparation_stage(job, 'upload', int(100 * sent / max(total, 1)))
                    last_progress[0] = now
            filename = self.printer.upload('k1c-ready-' + asset['id'] + '-v' + str(job['version']) + '-' + gate_token[:8] + '.gcode', job['wrapper_path'], progress=progress)
            if self.stopping:
                return True
            self._job_current(job)
            self._preparation_stage(job, 'verify')
            self.printer.download_gcode(filename, job['verify_path'])
            gate = inspect_gate(job['verify_path'], gate_token)
            self._job_current(job)
            if self.stopping:
                return True
            def ready(jobs):
                current = jobs.get(job['id'], {})
                if current.get('run_token') != token or current.get('status') != 'running':
                    return jobs
                asset = self._job_current(job)
                asset.update(ready=True, ready_filename=filename, gate=gate, ready_at=self.clock(),
                             preparation='ready', local_path=job['wrapper_path'], sliced=job['kind'] == 'stl',
                             material='PETG' if job['kind'] == 'stl' else 'G-code', size=Path(job['wrapper_path']).stat().st_size,
                             estimated_time=self._slicing_measure(result.get('estimated_time')),
                             filament_grams=self._slicing_measure(result.get('filament_grams')))
                self.store.mutate('assets', lambda assets: dict(assets, **{asset['id']: asset}), {})
                current.update(status='ready', stage='ready', finished=self.clock())
                current.pop('next_attempt', None)
                self.store.enqueue('preparation-ready:' + job['id'], [job['owner']], '✅ ' + asset['source_name'] + ' готов к печати. Перед запуском проверь стол.',
                                   markup=self._buttons([[('Открыть готовый файл', 'n:file:' + asset['id'])]]))
                self.store.audit(job['owner'], 'prepare', 'ready', asset['source_name'])
                return jobs
            self.store.mutate('slicing_jobs', ready, {})
        except Exception as exc:
            if job is None:
                raise
            if not self.stopping:
                code = exc.code if isinstance(exc, ApiError) else 'slicer_internal'
                deferred = []
                def failed(jobs):
                    current = jobs.get(job['id'], {})
                    if current.get('run_token') != token or current.get('status') != 'running':
                        return jobs
                    if code == 'slicer_busy' and self.access.is_allowed(job['owner']):
                        n = current.get('busy_attempts', 0) + 1
                        current['busy_attempts'] = n
                        if n < 12:
                            current.update(status='queued',stage='queued',next_attempt=self.clock()+min(30,5*2**min(n-1,3)))
                            current.pop('run_token', None)
                            deferred.append(True)
                            return jobs
                    current.update(status='failed', error=code, finished=self.clock())
                    current.pop('next_attempt', None)
                    if self.access.is_allowed(job['owner']) and code != 'preparation_superseded':
                        self.store.enqueue('preparation-failed:' + job['id'], [job['owner']],
                                           '❗ Не удалось подготовить файл ' + job['source_name'] + '.\n' + self._preparation_error(code))
                    self.store.audit(job['owner'], 'prepare', 'failed', code)
                    return jobs
                self.store.mutate('slicing_jobs', failed, {})
                LOG.info('preparation result reason=%s', code)
        finally:
            try:
                if job and not self.stopping:
                    self._cleanup_slicing_files(job['id'])
            finally:
                self.slicing_lock.release()
        return True

    @staticmethod
    def _preparation_error(code):
        if code.startswith('gcode_'):
            return 'Для свежего адаптивного замера нужен G-code с одним START_PRINT и границами модели. Пересохрани файл в профиле K1C OrcaSlicer. Код: ' + code
        return 'Можно исправить модель и отправить файл снова. Код: ' + code

    def _sliced_card(self, asset, user, context, qid=None):
        lines = ['🧩 K1C · файл готов', '📄 ' + asset.get('source_name', asset['name']), '']
        if asset.get('estimated_time') is not None:
            lines.append('⏱ Печать ≈ ' + duration(asset['estimated_time']))
        if asset.get('filament_grams') is not None:
            lines.append(f"🧵 PETG ≈ {asset['filament_grams']:.1f} г")
        if asset.get('source_kind') == 'stl':
            t = asset.get('temperatures', {'nozzle':240,'bed':70})
            lines.append(f"🌡 PETG · сопло {t['nozzle']:.0f}° · стол {t['bed']:.0f}°")
        else:
            lines.append('🌡 Температуры сохранены из G-code')
        lines.append('Перед каждой печатью — новый адаптивный замер.')
        rows = []
        if context and context['ready'] and context['state'] in IDLE and not self.preflight.busy():
            lines += ['', 'Стол пуст, PEI-пластина установлена?']
            payload = {'asset_id': asset['id'], 'version': asset['version'], 'qid': qid}
            rows.append([('✅ Стол пуст — печатать', self._ticket('start', payload, user, 1800))])
            lines.append('Подтверждение действует 30 минут.')
        else:
            lines += ['', 'Принтер занят: файл сохранён, можно добавить в очередь.' if context and context['ready'] else 'Принтер пока недоступен. Файл сохранён.']
        rows.append([('➕ В очередь', self._ticket('enqueue', {'asset_id':asset['id']}, user, 300))])
        if asset.get('source_kind') == 'stl':
            rows.append([('🌡 Температуры этой модели', 'n:jobtemp:' + asset['id'])])
        rows.append([('↻ Готовность', 'n:file:' + asset['id']), ('🏠 Пульт', 'n:home')])
        return '\n'.join(lines), self._buttons(rows)

    def _file_card(self, user, asset_id, qid=None,expected=None):
        asset = self._assets().get(asset_id)
        if not asset:
            self._show(user,'Файл больше недоступен. Открой список файлов снова.',expected=expected)
            return
        presentation=None
        if asset.get('ready') and asset.get('gate') and asset.get('ready_filename'):
            if self.ui_status_cache is not None:
                context,presentation=self._presentation()
            else:
                try:context=self.context(include_job=False)
                except ApiError:context=None
            text,markup=self._sliced_card(asset,user,context or None,qid)
            if presentation and presentation['state']=='checking':text=text.replace('Принтер пока недоступен. Файл сохранён.','Проверяю готовность принтера. Файл подготовлен.')
            label=self._presentation_label(presentation)
            if label:text+='\n'+label
        else:
            jobs = [j for j in self.store.get('slicing_jobs', {}).values() if j.get('asset_id',j['id']) == asset_id and j.get('version',1) == asset.get('version',1)]
            job = max(jobs, key=lambda j:j['created']) if jobs else None
            labels = {'queued':'В очереди подготовки','download':'Скачиваю исходный файл','slicing':'Готовлю PETG','gate':'Добавляю новый адаптивный замер','upload':'Передаю в принтер','verify':'Проверяю переданный файл','failed':'Подготовка остановлена'}
            stage = job.get('stage',job['status']) if job else None
            if job and job['status'] == 'failed':stage='failed'
            text = '🧩 K1C · подготовка\n📄 ' + asset.get('source_name',asset['name']) + '\n\n' + labels.get(stage,'Файл ещё не подготовлен для запуска.')
            if stage=='upload':text += ' · ' + str(job.get('progress',0)) + '%'
            rows=[]
            if job and job['status']=='failed':text += '\n' + self._preparation_error(job.get('error','unknown'))
            if not job or job['status']=='failed':rows.append([('Подготовить к печати',self._ticket('prepare_start',{'asset_id':asset_id,'qid':qid},user,300))])
            if asset.get('source_kind')=='stl':rows.append([('🌡 Температуры модели','n:jobtemp:'+asset_id)])
            rows.append([('➕ В очередь',self._ticket('enqueue',{'asset_id':asset_id},user,300))])
            rows.append([('↻ Обновить','n:file:'+asset_id),('🏠 Пульт','n:home')])
            markup=self._buttons(rows)
        self._show(user,text,markup,view='file',data=dict(self._presentation_data(presentation),asset_id=asset_id,qid=qid),expected=expected)

    def _enqueue(self, user, sender, asset_id):
        if not self.access.is_allowed(user):
            return
        asset = self._assets().get(asset_id)
        if not asset:
            self.send(user, "Файл не найден.")
            return
        item = {"id": uuid.uuid4().hex[:12], "asset_id": asset_id, "actor": user, "created": self.clock()}
        def add(workflow):
            if len(workflow["queue"]) >= 100:
                raise ApiError("queue_full")
            workflow["queue"].append(item)
            return workflow
        self.store.mutate("workflow", add, {"queue": [], "running": None, "awaiting": False})
        self.store.audit(user, "enqueue", "saved", asset["name"])
        self.notify("📋 " + sender + " добавил в очередь: " + asset["name"], "enqueue:" + item["id"])
        self._queue(user)

    def _queue(self, user, page=0):
        workflow, assets = self._workflow(), self._assets()
        rows, lines = [], ["📋 Очередь K1C"]
        if workflow["running"]:
            lines.append("Текущее задание: " + workflow["running"].get("filename", "проверяется"))
        pages = max(1, (len(workflow["queue"]) + 7) // 8)
        page = max(0, min(int(page), pages - 1))
        for index, item in enumerate(workflow["queue"][page * 8:(page + 1) * 8], page * 8 + 1):
            asset = assets.get(item["asset_id"], {})
            lines.append(str(index) + ". " + asset.get("name", "файл недоступен"))
            rows.append([("Убрать №" + str(index), self._ticket("remove_queue", {"qid": item["id"]}, user, 300))])
        if workflow["queue"]:
            first = workflow["queue"][0]
            rows.insert(0, [("▶️ Следующее задание", self._ticket("prepare_start", {"asset_id": first["asset_id"], "qid": first["id"]}, user, 300))])
            rows.append([("Завершить очередь", self._ticket("clear_queue", actor=user, ttl=300))])
            navigation = []
            if page:
                navigation.append(("← Пред. страница", self._ticket("queue_page", {"page": page - 1}, user, 300)))
            if page + 1 < pages:
                navigation.append(("След. страница →", self._ticket("queue_page", {"page": page + 1}, user, 300)))
            if navigation:
                rows.append(navigation)
            lines.append("Каждый запуск требует подтверждения очистки стола. Автоматического перехода нет.")
        else:
            lines.append("Ожидающих заданий нет.")
        self.send(user, "\n".join(lines)[:3900], self._buttons(rows) if rows else KEYBOARD)

    def _prepare_start(self, user, payload):
        asset = self._assets().get(payload['asset_id'])
        if not asset:
            self.send(user,'Файл не найден.');return
        if not asset.get('ready') or not asset.get('gate'):
            self._queue_preparation(user,asset,qid=payload.get('qid'))
        self._file_card(user,asset['id'],payload.get('qid'))

    @physical_action
    def _start(self,user,sender,payload,key):
        if self.stopping or not self.access.is_allowed(user):return
        asset=self._assets().get(payload['asset_id'])
        if not asset or not asset.get('ready') or not asset.get('gate') or payload.get('version')!=asset.get('version'):
            self.send(user,'Параметры файла изменились. Открой готовую карточку и подтверди запуск заново.')
            if asset:self._file_card(user,asset['id'],payload.get('qid'))
            return
        if self.clock()>payload['_expires']:
            self._file_card(user,asset['id'],payload.get('qid'));return
        workflow=self._workflow()
        if payload.get('qid') and (not workflow['queue'] or workflow['queue'][0]['id']!=payload['qid']):
            self.send(user,'Очередь изменилась. Выбери следующее задание заново.');return
        context=self.context()
        if not context['ready'] or context['state'] not in IDLE or self.preflight.busy():
            self.send(user,'Принтер уже занят или не готов. Файл сохранён.');return
        if self.stopping or not self.access.is_allowed(user):return
        self.preflight.begin(user,sender,asset,key,qid=payload.get('qid'))
        self._status(user)

    def _failed_queue(self, context):
        workflow = self._workflow()
        running = workflow.get("running")
        if not running:
            reference = context if context.get("filename") else self.store.get("snapshot", {})
            filename = reference.get("filename")
            if not filename:
                return
            failure_id = reference.get("job_id")
            last = workflow.get("last_failed") or {}
            if last.get("failure_id") == failure_id and last.get("filename") == filename:
                return
            asset_id = "r" + hashlib.sha256(filename.encode()).hexdigest()[:16]
            asset = {"id": asset_id, "origin": "printer", "name": filename, "server_filename": filename, "size": 0, "created": self.clock()}
            self.store.mutate("assets", lambda assets: dict(assets, **{asset_id: asset}), {})
            running = {"asset_id": asset_id, "filename": filename, "started": self.clock(), "failure_id": failure_id}
        else:
            running["failure_id"] = context.get("job_id")
        workflow["running"] = None
        workflow["awaiting"] = True
        workflow["last_failed"] = running
        self.store.set("workflow", workflow)
        payload = {"asset_id": running["asset_id"]}
        rows = [[("Повторить", self._ticket("prepare_start", payload, ttl=86400))]]
        if workflow["queue"]:
            first = workflow["queue"][0]
            rows.append([("Следующее", self._ticket("prepare_start", {"asset_id": first["asset_id"], "qid": first["id"]}, ttl=86400))])
        rows.append([("Завершить очередь", self._ticket("clear_queue", ttl=86400))])
        self.notify("📋 Очередь ждёт решения после ошибки или отмены. Оставшиеся файлы сохранены.", "queue-failed:" + str(running["started"]), markup=self._buttons(rows))

    def _history(self, user):
        lines = ["🕓 Последние задания"]
        try:
            for job in self.printer.history(8):
                lines.append("• " + job.get("filename", "?") + " — " + job.get("status", "?") + " · " + duration(job.get("print_duration")))
        except ApiError:
            lines.append("История принтера сейчас недоступна.")
        lines.append("\nДействия владельцев:")
        for item in self.store.recent_audit(8):
            lines.append(str(item["actor"]) + " · " + item["action"] + " · " + item["result"] + (" · " + item["detail"][:80] if item["detail"] else ""))
        self.send(user, "\n".join(lines)[:3900])

    def _workflow_callback(self, action, payload, user, sender, key):
        if not self.access.is_allowed(user):
            return
        if action=='heat_delta':self._heat_delta(user,payload,key)
        elif action=='heat':
            self.preflight.request_temperature(user,payload['heater'],payload['target'],key)
            self._temperature_card(user)
        elif action in {'heat_baseline','heat_off'}:
            self.preflight.request_temperature(user,'extruder',240 if action=='heat_baseline' else 0,key+':nozzle')
            self.preflight.request_temperature(user,'heater_bed',70 if action=='heat_baseline' else 0,key+':bed')
            self._temperature_card(user)
        elif action=='heat_input':self._ask_temperature(user,payload)
        elif action=='model_input':self._ask_temperature(user,payload,model=True)
        elif action=='model_delta':self._set_model_temperature(user,payload)
        elif action=='model_reset':self._set_model_temperature(user,dict(payload,reset=True))
        elif action in {"pause", "resume", "request_cancel"}:
            self._control("cancel" if action == "request_cancel" else action, user, sender, key, deadline=payload["_expires"])
        elif action in {"light_on", "light_off"}:
            self._light_action(action == "light_on", user, sender, key, payload["_expires"])
        elif action == "files_page":
            self._files(user, payload["page"])
        elif action == "queue_page":
            self._queue(user, payload["page"])
        elif action == "file":
            self._file_card(user, payload["asset_id"])
        elif action == "enqueue":
            self._enqueue(user, sender, payload["asset_id"])
        elif action == "prepare_start":
            self._prepare_start(user, payload)
        elif action == "start":
            self._start(user, sender, payload, key)
        elif action == "remove_queue":
            def remove(workflow):
                workflow["queue"] = [item for item in workflow["queue"] if item["id"] != payload["qid"]]
                return workflow
            self.store.mutate("workflow", remove, self._workflow())
            self.notify("📋 " + sender + " убрал задание из очереди.")
            self._queue(user)
        elif action == "clear_queue":
            self.store.mutate("workflow", lambda workflow: dict(workflow, queue=[], awaiting=False), self._workflow())
            self.notify("📋 " + sender + " завершил очередь. Текущая печать не отменяется.")
        elif action in {"power_on", "power_off", "confirm_power_off"}:
            self._power_action(action, payload, user, sender, key)
        elif action in {"enable_detection", "disable_detection"}:
            self._detector_toggle(action == "enable_detection", user, sender, payload["_expires"])

    def _power_card(self, user):
        status = self.power.status()
        label = "включено" if status.get("on") is True else "выключено" if status.get("on") is False else "неизвестно"
        text = "🔌 Питание K1C: " + label
        if not status.get("configured"):
            text = "🔌 Управление питанием ждёт подключения розетки.\n" + status.get("detail", "")
        if self.access.is_owner(user):
            markup = self._buttons([[("Включить", self._ticket("power_on", actor=user)), ("Выключить", self._ticket("power_off", actor=user))]])
        else:
            text += "\n\nУправлять питанием может только владелец."
            markup = self._buttons([[('📊 Статус', 'n:status')]])
        self.send(user, text, markup)

    @physical_action
    def _power_action(self, action, payload, user, sender, key):
        if not self.access.is_owner(user):
            self.send(user, "🔒 Управлять питанием может только владелец.")
            return
        if not self.power.status().get("configured"):
            self.send(user, "Розетка ещё не подключена. Кнопки готовы, но реальное питание станет доступно после проверки устройства и его API.")
            return
        on = action == "power_on"
        if not on:
            try:
                context = self.context()
            except ApiError:
                context = None
            if action == "power_off" and (context is None or not context["ready"] or context["state"] in ACTIVE):
                warning = "Печать идёт. Точно выключить питание? Задание будет прервано." if context and context["state"] in ACTIVE else "Принтер недоступен: состояние печати неизвестно. Точно выключить питание?"
                reference = self._job_ref(context) if context else {"unknown": True}
                self.send(user, "🔌 " + warning, self._buttons([[("Да, выключить питание", self._ticket("confirm_power_off", reference, user))], [("Оставить включённым", self._ticket("dismiss", actor=user))]]))
                return
            if action == "confirm_power_off" and context and context["state"] in ACTIVE and not payload.get("unknown") and not self._same_job(payload, context):
                self.send(user, "За время подтверждения началось другое задание. Нажми «Выключить» заново.")
                return
        if self.clock() > payload["_expires"]:
            self.send(user, "Подтверждение питания устарело. Открой меню заново.")
            return
        if self.stopping or not self.access.is_owner(user):
            return
        if not self.store.claim_action(key + ":power"):
            return
        self.store.audit(user, "power_on" if on else "power_off", "claimed")
        try:
            self.power.set(on)
        except ApiError as exc:
            self.store.audit(user, "power", "uncertain" if exc.uncertain else "failed", exc.code)
            raise
        self.store.audit(user, "power_on" if on else "power_off", "accepted")
        self.notify("🔌 " + sender + (" включил питание K1C." if on else " выключил питание K1C."), "action:" + key + ":power")
        self.store.set("power_wait", {"deadline": self.clock() + 180, "key": key} if on else None)

    def _detector_card(self, user):
        status = self.detector.status() if self.detector else {"available": False, "detail": "не подключён"}
        auto = self._auto_enabled()
        lines = ["🧠 Контроль брака: " + ("модель доступна" if status.get("available") else "недоступен"),
                 "Автопауза: " + ("включена — 5 минут на ответ" if auto else "пока отключена: детектор проходит проверку"), "«Дальше» даёт 10 минут тишины.",
                 "Проверяются несколько кадров подряд; мелкие дефекты качества могут остаться незамеченными."]
        snapshot = self.store.get("snapshot")
        if not snapshot or snapshot["state"] not in ACTIVE:
            lines.append("Сейчас активное задание не подтверждено; изображения для анализа не поступают.")
        elif not snapshot.get("job_id"):
            lines.append("История не подтвердила ID задания; автоматическая пауза недоступна.")
        observation = self.store.get("detector_observation")
        if observation and snapshot and observation["job_id"] == snapshot.get("job_id"):
            lines.append(observation["detail"])
        lines.append("В начале каждой печати контроль набирает первые наблюдения около 5 минут.")
        if not self.store.get("camera_live_checked", False):
            lines.append("Живой кадр принтера пока не получен.")
        alert = self.alarms.current()
        if alert:
            lines.append("Последняя тревога: " + alert["status"])
            if alert["status"] == "pending" and auto:
                lines.append("До паузы: " + str(max(0, int(alert["deadline"] - self.clock()))) + " сек")
        label, action = ("Отключить автопаузу", "disable_detection") if auto else ("Включить автопаузу", "enable_detection")
        if self.access.is_owner(user):
            markup = self._buttons([[(label, self._ticket(action, actor=user))]])
        else:
            lines.append("Настройки контроля меняет только владелец.")
            markup = self._buttons([[('📊 Статус', 'n:status')]])
        self.send(user, "\n".join(lines), markup)

    def _auto_enabled(self):
        return self.store.get("detector_auto_enabled", self.config.get("detector_auto_enabled", True)) is True

    @physical_action
    def _detector_toggle(self, enabled, user, sender, expires):
        if not self.access.is_owner(user):
            self.send(user, "🔒 Настройки контроля меняет только владелец.")
            return
        if enabled:
            if not self.detector or not self.detector.status().get("available"):
                self.send(user, "Модель детектора сейчас недоступна. Сначала нужно восстановить её работу.")
                return
            context = self.context()
            if not context["ready"]:
                self.send(user, "Сначала включи принтер и дождись готовности Klipper.")
                return
            self._photo(force=True)
        if self.clock() > expires:
            self.send(user, "Кнопка устарела во время проверки связи. Открой «Контроль» и выбери действие снова.")
            return
        if self.stopping or not self.access.is_owner(user):
            return
        self.alarms.reset()
        self.store.set("detector_auto_enabled", enabled)
        self.store.audit(user, "detection_mode", "enabled" if enabled else "disabled")
        self.notify("🧠 " + sender + (" включил автопаузу: 5 минут на ответ после тревоги." if enabled else " отключил автоматическую паузу по сигналам детектора."))
        self._detector_card(user)

    @physical_action
    def _alarm_response(self, data, user, sender):
        parts = data.split(":")
        if len(parts) != 3 or parts[2] not in {"pause", "continue"}:
            return
        alert = self.alarms.current()
        if not alert or alert["id"] != parts[1]:
            self.send(user, "Эта тревога уже закрыта или относится к другому заданию.")
            return
        context = self.context()
        if not context["ready"] or context.get("job_id") != alert["job_key"] or context["state"] not in ACTIVE:
            self.send(user, "Текущее задание не совпадает с тревогой. Старую кнопку не выполняю.")
            return
        if not self.access.is_allowed(user):
            return
        outcome = self.alarms.decide(parts[1], user, parts[2])
        if not outcome["accepted"]:
            self.send(user, "Решение по тревоге уже принято другим владельцем.")
            return
        if parts[2] == "continue":
            text = "🧠 " + sender + " выбрал «Дальше». Контроль вернётся через 10 минут."
            if outcome["late"] or context["state"] == "paused":
                text += "\nПечать уже на паузе. Для возобновления нужна отдельная кнопка «Продолжить»."
            self.notify(text, "alarm-answer:" + parts[1])
        elif context["state"] == "printing" and not outcome["late"]:
            try:
                self._control("pause", user, sender, "alarm-answer:" + parts[1], expected=self._job_ref(context), deadline=self.clock() + 60)
                self.alarms.mark_autopause(parts[1], "accepted")
            except ApiError as exc:
                self.alarms.mark_autopause(parts[1], "uncertain" if exc.uncertain else "failed")
                raise
        else:
            self.notify("⏸ " + sender + " подтвердил паузу. Принтер уже остановлен.", "alarm-answer:" + parts[1])

    def _condition(self, name, available, failure_text, recovery_text, initial=False):
        old = self.store.get("condition:" + name)
        if old == available:
            return
        self.store.set("condition:" + name, available)
        if available and old is False:
            self.notify(recovery_text)
        elif not available and (old is True or initial):
            self.notify(failure_text)

    def _photo(self, *, force=False):
        # Coalesce concurrent camera consumers and reuse a frame for at most 2s.
        # Failure never falls back to an old frame, including detector input.
        with self.photo_lock:
            epoch, cached = self._photo_epoch, self._photo_cache
            if not force and cached and cached[0] == epoch and 0 <= self.clock() - cached[1] < 2:
                return cached[2]
            photo = self.printer.photo()
            self._photo_cache = (epoch, self.clock(), photo)
            self.store.set("camera_live_checked", True)
            return photo

    def _photo_optional(self):
        try:
            return self._photo()
        except ApiError:
            return None

    def _observe(self, context):
        # Update workflow atomically; the network snapshot is deferred until
        # tick releases the owner lock, so camera I/O cannot block controls.
        photo_event = None
        previous = self.store.get("snapshot")
        current = {key: context.get(key) for key in ("state", "filename", "job_id", "duration", "ready", "seen_at")}
        sequence = previous.get("sequence", 0) if previous else 0
        changed = previous and any(previous.get(key) != current.get(key) for key in ("state", "job_id", "ready"))
        if changed:
            sequence += 1
            state = context["state"]
            headings = {"printing": "▶️ Печать идёт", "paused": "⏸ Печать на паузе", "complete": "✅ Печать завершена", "cancelled": "⛔ Печать отменена", "error": "❗ Ошибка печати"}
            internal=self.preflight.owns(context) and self.preflight.status().get('phase')!='printing'
            if state in headings and not internal:
                text = headings[state] + "\n" + context["filename"] + "\nВремя: " + duration(context["duration"])
                photo_event = {"text": text, "key": "printer:" + str(context["job_id"]) + ":" + str(sequence),
                               "state": context["state"], "job_id": context.get("job_id"), "filename": context["filename"]}
            if state in {"cancelled", "error"}:
                self._failed_queue(context)
            elif state == "complete":
                workflow = self._workflow()
                workflow["running"] = None
                workflow["awaiting"] = True
                self.store.set("workflow", workflow)
                if workflow["queue"]:
                    item = workflow["queue"][0]
                    self.notify("📋 Следующее задание готово. Освободи стол и подтверди запуск через очередь.", "queue-ready:" + str(context["job_id"]) + ":" + str(sequence), markup=self._buttons([[("Следующее задание", self._ticket("prepare_start", {"asset_id": item["asset_id"], "qid": item["id"]}, ttl=86400))]]))
        current["sequence"] = sequence
        self.store.set("snapshot", current)
        return photo_event

    def _send_photo_events(self, events):
        if not events or self.stopping:
            return
        photo = self._photo_optional()
        current = None
        if photo is not None:
            try:
                current = self.context()
            except ApiError:
                pass
        for event in events:
            # A concurrent owner may have changed the job while the camera was
            # busy. Keep the event, but do not mislabel a later job's picture.
            same = (current and current["ready"] and current["state"] == event["state"]
                    and current.get("job_id") == event["job_id"] and current["filename"] == event["filename"])
            self.notify(event["text"], event["key"], photo=photo if same else None)

    @physical_action
    def _autopause(self, context=None):
        # Only the current stable history job may inherit a persisted deadline.
        alert = self.alarms.current()
        if not self._auto_enabled() or not alert or alert["status"] != "pending" or self.stopping:
            return
        context = self.context()
        if self.preflight.owns(context) and self.preflight.status().get('phase')!='printing':return
        if not context["ready"]:
            return
        if not context.get("job_id") or self.stopping:
            return
        alert = self.alarms.tick(context["job_id"], context["state"])
        if not alert:
            return
        key = "autopause:" + alert["id"]
        if not self.store.claim_action(key):
            return
        self.store.audit("detector", "pause", "claimed", context["filename"])
        try:
            self.printer.pause()
            result = "accepted"
            text = "⏸ За 5 минут никто не ответил на тревогу. Команда паузы принята принтером."
        except ApiError as exc:
            result = "uncertain" if exc.uncertain else "failed"
            text = "❗ Автопауза не подтверждена. Проверь состояние K1C. Команда автоматически не повторяется."
        self.alarms.mark_autopause(alert["id"], result)
        self.store.audit("detector", "pause", result, context["filename"])
        self.notify(text + "\n" + context["filename"], key)

    def check_deadlines(self):
        """Independent watchdog: never waits for Telegram delivery or inference."""
        alert = self.alarms.current()
        if not self._auto_enabled() or self.stopping or not alert or alert["status"] != "pending" or alert["deadline"] > self.clock():
            return
        try:
            self._autopause()
        except ApiError:
            self.notify("❗ Срок тревоги истёк, но связь с K1C не позволяет подтвердить задание и поставить паузу. Проверка продолжится после восстановления связи.", "alarm-offline:" + alert["id"])

    def tick(self):
        if self.stopping:
            return
        try:
            context = self.context()
        except ApiError:
            self._condition("printer", False, "⚪ Связь с K1C потеряна. Состояние печати неизвестно.", "🟢 Связь с K1C восстановлена.")
            return
        self._condition("printer", True, "", "🟢 Связь с K1C восстановлена.")
        photo_events = []
        with self.lock:
            if self.stopping:
                return
            if self.clock() - context["seen_at"] > 3:
                context = self.context()
            if not context["ready"]:
                self._condition("klipper", False, "❗ Klipper не готов: " + str(context["data"].get("webhooks", {}).get("message", ""))[:800], "🟢 Klipper снова готов.", initial=True)
                self._failed_queue(context)
                return
            self._condition("klipper", True, "", "🟢 Klipper снова готов.")
            photo_event = self._observe(context)
            if photo_event:
                photo_events.append(photo_event)
            if context["state"] not in ACTIVE:
                self.alarms.tick(context.get("job_id"), context["state"])
            waiting = self.store.get("power_wait")
            if waiting:
                self.store.set("power_wait", None)
                photo_events.append({"text": "🟢 После включения K1C готов к работе.", "key": "power-ready:" + waiting["key"],
                                     "state": context["state"], "job_id": context.get("job_id"), "filename": context["filename"]})
        self._send_photo_events(photo_events)
        if context["state"] in ACTIVE:
            self._autopause(context)
        if (self.detector and context["state"] == "printing" and context.get("job_id")
                and not (self.preflight.owns(context) and self.preflight.status().get('phase')!='printing')
                and self.clock() - self.store.get("last_analysis", 0) >= self.config.get("analysis_interval", 10)):
            self.store.set("last_analysis", self.clock())
            try:
                photo = self._photo()
                self.camera_health.observe(True, context["job_id"], self.config.get("analysis_interval", 10))
            except ApiError:
                self.camera_health.observe(False, context["job_id"], self.config.get("analysis_interval", 10))
                return
            try:
                result = self.detector.analyze(photo)
                self._condition("detector", True, "", "🧠 Детектор снова работает.")
            except DetectorError:
                self._condition("detector", False, "🧠 Детектор недоступен. Автоматический контроль брака временно не работает; печать продолжается.", "", initial=True)
                return
            # Inference is deliberately outside the owner command lock. Re-check
            # the job after it: a user may have cancelled/replaced the print.
            with self.lock:
                current = self.context()
                if (not self.stopping and current["ready"] and current["state"] == "printing"
                        and current["job_id"] == context["job_id"]):
                    judgement = self.scorer.classify(current["job_id"], result["detections"])
                    self.store.set("detector_observation", {"job_id": current["job_id"], "ready": judgement["ready"], "detail": judgement["detail"], "at": self.clock()})
                    alert = self.alarms.observe(current["job_id"], current["filename"], judgement["suspicious"], judgement["score"])
                    if alert:
                        markup = self._buttons([[("⏸ Пауза", "a:" + alert["id"] + ":pause"), ("▶️ Дальше", "a:" + alert["id"] + ":continue")]])
                        reaction = "Без ответа владельца через 5 минут принтер встанет на паузу." if self._auto_enabled() else "Это пробный сигнал детектора. Автопауза пока отключена; можешь поставить паузу вручную."
                        self.notify("⚠️ Подозрение на брак\n" + current["filename"] + "\nПроверь снимок. " + reaction, "alarm:" + alert["id"], photo=photo, markup=markup)
