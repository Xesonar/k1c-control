"""Durable, owner-controlled access gate and Telegram administration UI.

Call after Store.consume_update and before dispatching printer operations. The
owner's identity is fixed by configuration; permission sets are read from SQLite
on every check, so revocation takes effect for already-running workflows too.
"""
import logging
import re
import time
import uuid

from adapters import ApiError


LOG = logging.getLogger("k1c-telegram")
KEY = "access_control"
PAGE_SIZE = 6
STATUS = {"allowed": "доступ разрешён", "pending": "ждёт решения", "denied": "заявка отклонена",
          "revoked": "доступ отозван", "blocked": "заблокирован"}


def _valid_id(value):
    return type(value) is int and 0 < value <= 2 ** 63 - 1


def configured_owner(config):
    owner = config.get('owner_id') if isinstance(config, dict) else None
    if not _valid_id(owner):
        raise ValueError('invalid_owner_id')
    return owner


def _name(sender):
    value = " ".join(str(sender.get("first_name") or "Пользователь").split())
    return "".join(char for char in value if ord(char) >= 32)[:48] or "Пользователь"


def _buttons(rows):
    return {"inline_keyboard": [[{"text": text, "callback_data": value} for text, value in row] for row in rows]}


class AccessController:
    def __init__(self, store, owner_id=None, initial_users=(), clock=time.time):
        if not _valid_id(owner_id):
            raise ValueError("invalid_owner")
        self.store, self.owner_id, self.clock = store, owner_id, clock

        def migrate(state):
            if state is None:
                users = {str(uid): self._record(uid, "allowed") for uid in initial_users if _valid_id(uid)}
                state = {"schema": 1, "owner_id": owner_id, "users": users}
                self.store.audit(owner_id, "access_migration", "complete", str(len(users)))
            # Legacy IDs are imported once, never restored after revocation.
            owner = state["users"].setdefault(str(owner_id), self._record(owner_id, "allowed"))
            owner["status"], owner["nonce"] = "allowed", None
            state["owner_id"] = owner_id
            return state

        self.store.mutate(KEY, migrate)

    @staticmethod
    def _record(uid, status, name="Пользователь"):
        return {"id": uid, "name": name, "status": status, "version": 1,
                "nonce": None, "last_request": None, "request_count": 0}

    def _state(self):
        return self.store.get(KEY)

    def is_owner(self, uid):
        return _valid_id(uid) and uid == self.owner_id

    def is_allowed(self, uid):
        if not _valid_id(uid):
            return False
        if self.is_owner(uid):
            return True
        return self._state()["users"].get(str(uid), {}).get("status") == "allowed"

    def allowed_users(self):
        return {self.owner_id} | {user["id"] for user in self._state()["users"].values() if user["status"] == "allowed"}

    @staticmethod
    def _identity(update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if isinstance(callback, dict) else update.get("message", {})
        if not isinstance(message, dict):
            return None
        sender = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {})
        if not isinstance(sender, dict):
            return None
        uid, chat = sender.get("id"), message.get("chat", {})
        if (not isinstance(chat, dict) or not _valid_id(uid) or sender.get("is_bot", False) or chat.get("type") != "private"
                or not _valid_id(chat.get("id")) or chat["id"] != uid):
            return None
        return uid, sender, message, callback

    @staticmethod
    def _send(telegram, uid, text, markup=None):
        try:
            return telegram.send_message(uid, text, markup)
        except ApiError as exc:
            LOG.info("access message unavailable reason=%s", exc.code)

    @staticmethod
    def _ack(telegram, callback, text=""):
        if callback:
            try:
                telegram.answer_callback(callback.get("id", ""), text)
            except ApiError as exc:
                LOG.info("access callback acknowledgement unavailable reason=%s", exc.code)

    def _remember_name(self, uid, sender):
        name = _name(sender)
        current = self._state()["users"].get(str(uid))
        if current and current.get("name") != name:
            def remember(state):
                user = state["users"].get(str(uid))
                if user:
                    user["name"] = name
                return state
            self.store.mutate(KEY, remember)

    def handle_update(self, update, telegram):
        identity = self._identity(update)
        if identity is None:
            return True
        uid, sender, message, callback = identity
        user = self._state()["users"].get(str(uid), {})
        if user.get("status") == "blocked" and not self.is_owner(uid):
            return True
        text = message.get("text", "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        data = callback.get("data", "") if callback else ""
        if not isinstance(data, str):
            return True
        if callback and data == "x:request":
            self._ack(telegram, callback)
            self._request(uid, sender, telegram)
            return True
        admin = data.startswith("x:") or (not callback and (command == "/admin" or text == "👑 Админка"))
        if admin:
            if not self.is_owner(uid):
                self._ack(telegram, callback, "Только владелец управляет доступом")
                if self.is_allowed(uid):
                    self._send(telegram, uid, "👑 Админка доступна только владельцу.")
                else:
                    self._gate(uid, telegram, user.get("status"))
                return True
            self._remember_name(uid, sender)
            self._ack(telegram, callback)
            self._admin(uid, data, telegram)
            return True
        if not self.is_allowed(uid):
            self._ack(telegram, callback, "Нужно разрешение владельца")
            self._gate(uid, telegram, user.get("status"))
            return True
        self._remember_name(uid, sender)
        return False

    def _gate(self, uid, telegram, status=None):
        text = "🔒 Доступ к K1C выдаёт владелец.\nНажми кнопку, чтобы отправить заявку."
        if status == "pending":
            text = "⏳ Заявка уже у владельца. После решения я сообщу тебе результат."
        elif status == "denied":
            text = "🔒 Владелец отклонил заявку. Повторно запросить доступ можно через 10 минут после прошлого запроса."
        elif status == "revoked":
            text = "🔒 Доступ отозван владельцем. Можно отправить новую заявку."
        self._send(telegram, uid, text, _buttons([[('📨 Запросить доступ', 'x:request')]]))

    def _request(self, uid, sender, telegram):
        if self.is_allowed(uid):
            self._send(telegram, uid, "✅ Доступ уже разрешён. Открой /start.")
            return
        outcome = {"value": "cooldown"}
        now = self.clock()

        def request(state):
            user = state["users"].setdefault(str(uid), self._record(uid, "revoked", _name(sender)))
            if user["status"] == "blocked":
                outcome["value"] = "blocked"
                return state
            if user["status"] == "allowed":
                outcome["value"] = "allowed"
                return state
            last = user.get("last_request")
            if last is not None and now - last < 600:
                return state
            if user["status"] != "pending":
                user.update(status="pending", nonce=uuid.uuid4().hex[:12], version=user["version"] + 1)
            user.update(name=_name(sender), last_request=now, request_count=user.get("request_count", 0) + 1)
            nonce = user["nonce"]
            markup = self._decision_buttons(uid, nonce)
            self.store.enqueue(f"access-request:{uid}:{nonce}:{user['request_count']}", [self.owner_id],
                               f"📨 Запрос доступа к K1C\n{user['name']} · {uid}", markup=markup)
            self.store.audit(uid, "access_request", "pending", str(uid))
            outcome["value"] = "sent"
            return state

        self.store.mutate(KEY, request)
        result = outcome["value"]
        if result == "blocked":
            return
        text = {"sent": "📨 Заявка отправлена владельцу. Я сообщу его решение.",
                "cooldown": "⏳ Заявка уже отправлена. Повторное напоминание доступно через 10 минут.",
                "allowed": "✅ Доступ уже разрешён. Открой /start."}[result]
        self._send(telegram, uid, text)

    @staticmethod
    def _decision_buttons(uid, nonce):
        return _buttons([[('✅ Разрешить', f'x:approve:{uid}:{nonce}'), ('❌ Отклонить', f'x:deny:{uid}:{nonce}')],
                         [('🚫 Заблокировать', f'x:block:{uid}:{nonce}')]])

    def _admin(self, uid, data, telegram):
        if not data or data == "x:admin":
            self._overview(telegram)
            return
        match = re.fullmatch(r"x:(requests|users|blocked):([0-9]{1,5})", data)
        if match:
            self._list(telegram, match[1], int(match[2]))
            return
        match = re.fullmatch(r"x:user:([0-9]{1,19})(?::v[0-9]+)?", data)
        if match:
            self._card(telegram, int(match[1]))
            return
        match = re.fullmatch(r"x:(approve|deny|block|revoke|unblock|grant):([0-9]{1,19}):([a-zA-Z0-9]{1,24})", data)
        if match:
            self._transition(telegram, match[1], int(match[2]), match[3])
            return
        self._send(telegram, uid, "Кнопка устарела. Открой /admin.")

    def _overview(self, telegram):
        users = list(self._state()["users"].values())
        count = lambda status: sum(user["status"] == status for user in users)
        text = (f"👑 Управление доступом\nВладелец: {self.owner_id}\n\n"
                f"Допущено: {len(self.allowed_users())}\nЗаявок: {count('pending')}\nЗаблокировано: {count('blocked')}")
        self._send(telegram, self.owner_id, text, _buttons([
            [(f"📨 Заявки · {count('pending')}", 'x:requests:0')],
            [(f"👥 Пользователи · {len(users)}", 'x:users:0')],
            [(f"🚫 Заблокированные · {count('blocked')}", 'x:blocked:0')]]))

    def _list(self, telegram, kind, page):
        users = list(self._state()["users"].values())
        if kind != "users":
            users = [user for user in users if user["status"] == ("pending" if kind == "requests" else "blocked")]
        users.sort(key=lambda user: (user["status"] != "pending", user["name"].lower(), user["id"]))
        pages = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        rows = [[(f"{'👑' if self.is_owner(user['id']) else '👤'} {user['name']} · {user['id']}",
                  f"x:user:{user['id']}:v{user['version']}")] for user in users[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        navigation = []
        if page:
            navigation.append(('← Назад', f'x:{kind}:{page-1}'))
        if page + 1 < pages:
            navigation.append(('Дальше →', f'x:{kind}:{page+1}'))
        if navigation:
            rows.append(navigation)
        rows.append([('👑 Админка', 'x:admin')])
        title = {"requests": "📨 Заявки", "users": "👥 Пользователи", "blocked": "🚫 Заблокированные"}[kind]
        self._send(telegram, self.owner_id, f"{title} · {page+1}/{pages}\nВыбери пользователя." if users else title + "\nСписок пуст.", _buttons(rows))

    def _card(self, telegram, uid):
        user = self._state()["users"].get(str(uid))
        if not user:
            self._send(telegram, self.owner_id, "Пользователь не найден. Открой /admin.")
            return
        role = "владелец" if self.is_owner(uid) else STATUS.get(user["status"], "без доступа")
        text = f"👤 {user['name']}\nID: {uid}\nСтатус: {role}"
        if self.is_owner(uid):
            markup = _buttons([[('👑 Админка', 'x:admin')]])
        elif user["status"] == "pending":
            markup = self._decision_buttons(uid, user["nonce"])
            markup["inline_keyboard"].append([{"text": "👑 Админка", "callback_data": "x:admin"}])
        else:
            token, rows = "v" + str(user["version"]), []
            if user["status"] == "allowed":
                rows.append([('🔒 Отозвать доступ', f'x:revoke:{uid}:{token}')])
            elif user["status"] == "blocked":
                rows.append([('✅ Разблокировать', f'x:unblock:{uid}:{token}')])
            else:
                rows.append([('✅ Разрешить доступ', f'x:grant:{uid}:{token}')])
            if user["status"] != "blocked":
                rows.append([('🚫 Заблокировать', f'x:block:{uid}:{token}')])
            rows.append([('👑 Админка', 'x:admin')])
            markup = _buttons(rows)
        self._send(telegram, self.owner_id, text, markup)

    def _transition(self, telegram, action, uid, token):
        if not _valid_id(uid) or self.is_owner(uid):
            self._send(telegram, self.owner_id, "Владелец сохраняет полный доступ. Его права изменить нельзя.")
            return
        outcome = {"changed": False}

        def change(state):
            user = state["users"].get(str(uid))
            if not user:
                return state
            current = user["status"]
            if action in {"approve", "deny"}:
                valid = current == "pending" and token == user.get("nonce")
            elif action == "block":
                valid = current != "blocked" and token == (user.get("nonce") if current == "pending" else "v" + str(user["version"]))
            else:
                valid = token == "v" + str(user["version"]) and (
                    action == "revoke" and current == "allowed" or action == "unblock" and current == "blocked"
                    or action == "grant" and current in {"denied", "revoked"})
            if not valid:
                return state
            status = {"approve": "allowed", "grant": "allowed", "deny": "denied", "block": "blocked",
                      "revoke": "revoked", "unblock": "revoked"}[action]
            user.update(status=status, nonce=None, version=user["version"] + 1)
            if action == "unblock":
                user["last_request"] = None
            self.store.audit(self.owner_id, "access_" + action, status, str(uid))
            outcome.update(changed=True, status=status)
            return state

        self.store.mutate(KEY, change)
        if not outcome["changed"]:
            self._send(telegram, self.owner_id, "Кнопка устарела: решение уже принято или права изменились. Открой /admin.")
            return
        notification = {"allowed": "✅ Владелец разрешил доступ к K1C. Открой /start.",
                        "denied": "❌ Владелец отклонил заявку на доступ к K1C.",
                        "blocked": "🚫 Владелец заблокировал доступ к K1C.",
                        "revoked": "🔒 Доступ к K1C отозван владельцем."}[outcome["status"]]
        if action == "unblock":
            notification = "✅ Блокировка снята. Доступ пока закрыт: отправь /start и запроси разрешение."
        self._send(telegram, uid, notification)
        self._card(telegram, uid)
