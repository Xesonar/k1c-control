"""One persistent, coalesced inline control panel per private user."""
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager

from adapters import ApiError

LOG=logging.getLogger("k1c-telegram")


class Panels:
    def __init__(self, store, telegram, allowed, clock=time.time):
        self.store, self.telegram, self.allowed, self.clock = store, telegram, allowed, clock
        self._locks, self._guard = {}, threading.Lock()
        self._interaction = threading.local()
        self.async_enabled=False
        self._closed=False
        self._state_lock=threading.RLock()
        self._writer_lock=threading.Lock()
        self._pending={}
        self._retry_until={}
        self.wakeup=threading.Event()

    def enable_async(self):
        with self._state_lock:
            self.async_enabled=True
            self._closed=False

    def close(self):
        with self._state_lock:
            self._closed=True
            self._pending.clear()
            self._retry_until.clear()
            self.wakeup.set()

    def _enqueue(self,user,text,markup,view,data,expected,force,fingerprint):
        with self._state_lock:
            if self._closed or not self.allowed(user):return None
            previous=self.records().get(str(user),{})
            if expected is not None and (previous.get('revision',0)!=expected or previous.get('delivery_unknown')):return None
            if user not in self._pending and len(self._pending)>=64:raise ApiError('panel_queue_full')
            interaction=getattr(self._interaction,'current',None)
            interaction=interaction if interaction and interaction['user']==user and expected is None else None
            screen=previous.get('screen',0)
            target=previous.get('message_id')
            if expected is None:
                if interaction and interaction.get('screen') is not None:
                    if interaction['screen']!=screen or previous.get('delivery_unknown'):return None
                else:
                    screen+=1
                    if interaction:
                        target=None if interaction['fresh'] else interaction.get('anchor') or target
                        interaction.update(screen=screen,fresh=False,anchor=target)
            revision=previous.get('revision',0)+1
            record={'message_id':target,'fingerprint':previous.get('fingerprint') if target==previous.get('message_id') else None,
                    'view':view,'data':data or {},'updated_at':self.clock(),'revision':revision,'screen':screen,'delivery_unknown':False}
            self.store.mutate('panels',lambda records:dict(records,**{str(user):record}),{})
            self._pending[user]={'user':user,'text':text,'markup':json.loads(json.dumps(markup)),'fingerprint':fingerprint,
                                 'revision':revision,'screen':screen,'force':force,'retry_at':self._retry_until.get(user,0)}
            self.wakeup.set()
            return {'message_id':target,'queued':True}

    def _valid(self,request,exact=True):
        if self._closed or not self.allowed(request['user']):return None
        record=self.records().get(str(request['user']),{})
        if record.get('screen',0)!=request['screen']:return None
        if exact and record.get('revision',0)!=request['revision']:return None
        return record

    def flush_pending(self,limit=8):
        if not self.async_enabled or not self._writer_lock.acquire(blocking=False):return 0
        sent=0
        try:
            for _ in range(limit):
                with self._state_lock:
                    if self._closed:return sent
                    user=next((uid for uid,request in self._pending.items() if request['retry_at']<=self.clock()),None)
                    if user is None:return sent
                    request=self._pending.pop(user)
                    record=self._valid(request)
                    if not record:continue
                    target=record.get('message_id')
                    unchanged=target and record.get('fingerprint')==request['fingerprint'] and not request['force']
                # No state/user lock spans Telegram I/O. The one writer orders it.
                sending=False
                try:
                    result={'message_id':target} if unchanged else None
                    if result is None and target:
                        try:result=self.telegram.edit_message(user,target,request['text'],request['markup'])
                        except ApiError as exc:
                            if exc.code=='message_not_modified':result={'message_id':target}
                            elif exc.http_status not in {400,404}:raise
                    if result is None:
                        with self._state_lock:
                            if not self._valid(request):continue
                        sending=True
                        result=self.telegram.send_message(user,request['text'],request['markup'])
                    message_id=result.get('message_id') if isinstance(result,dict) else target
                    if type(message_id) is not int or message_id<=0:raise ApiError('invalid_message_id')
                    with self._state_lock:
                        # A newer frame on the same screen inherits a newly sent
                        # message ID; navigation/revocation discards late results.
                        if not self._valid(request,exact=False):continue
                        if message_id:
                            def commit(records):
                                current=records.get(str(user),{})
                                if not self.allowed(user) or current.get('screen',0)!=request['screen']:return records
                                current.update(message_id=message_id,fingerprint=request['fingerprint'])
                                return records
                            self.store.mutate('panels',commit,{})
                    sent+=1
                except ApiError as exc:
                    with self._state_lock:
                        if exc.http_status==429:
                            until=self.clock()+max(1,exc.retry_after or 0)
                            self._retry_until[user]=max(self._retry_until.get(user,0),until)
                            if user in self._pending:self._pending[user]['retry_at']=self._retry_until[user]
                        if sending and exc.http_status!=429:
                            # A lost sendMessage response can mean the fresh
                            # panel already exists. Do not create copies on retry.
                            if self._valid(request,exact=False):
                                def uncertain(records):
                                    record=records.get(str(user),{})
                                    record.update(delivery_unknown=True,updated_at=self.clock())
                                    return records
                                self.store.mutate('panels',uncertain,{})
                                pending=self._pending.get(user)
                                if pending and pending['screen']==request['screen']:self._pending.pop(user,None)
                            continue
                        if self._valid(request) and user not in self._pending:
                            request['retry_at']=self.clock()+max(1,exc.retry_after or 0)
                            self._pending[user]=request
                    LOG.info('panel delivery deferred reason=%s',exc.code)
            return sent
        finally:self._writer_lock.release()

    @contextmanager
    def interaction(self,user,message=None,fresh=False):
        previous=getattr(self._interaction,'current',None)
        anchor=None
        if isinstance(message,dict):
            mid=message.get('message_id')
            if type(mid) is int and mid>0:
                if isinstance(message.get('text'),str):anchor=mid
                else:fresh=True
        self._interaction.current={'user':user,'anchor':anchor,'fresh':fresh}
        try:yield
        finally:self._interaction.current=previous

    def _lock(self, user):
        with self._guard:
            return self._locks.setdefault(user, threading.RLock())

    def records(self):
        return self.store.get('panels', {})

    def select(self,user,view):
        lock=self._state_lock if self.async_enabled else self._lock(user)
        with lock:
            if self._closed:return
            self._pending.pop(user,None)
            def update(records):
                record=records.get(str(user))
                if record:record.update(view=view,revision=record.get('revision',0)+1,
                                        screen=record.get('screen',0)+1,updated_at=self.clock())
                return records
            self.store.mutate('panels',update,{})

    def show(self, user, text, markup=None, view='message', data=None, expected=None, force=False):
        if not self.allowed(user):
            return None
        text = text[:4000]
        fingerprint = hashlib.sha256(json.dumps([text, markup], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.async_enabled:
            return self._enqueue(user,text,markup,view,data,expected,force,fingerprint)
        with self._lock(user):
            if not self.allowed(user):return None
            previous = self.records().get(str(user), {})
            interaction=getattr(self._interaction,'current',None)
            interaction=interaction if interaction and interaction['user']==user and expected is None else None
            fresh=bool(interaction and interaction['fresh'])
            target=interaction.get('anchor') if interaction else None
            target=target or previous.get('message_id')
            if expected is not None and previous.get('revision',0)!=expected:return None
            if previous.get('fingerprint') == fingerprint and not force and not fresh and target==previous.get('message_id'):
                result = {'message_id': previous['message_id']}
            else:
                result = None
                if target and not fresh:
                    try:
                        result = self.telegram.edit_message(user,target,text,markup)
                    except ApiError as exc:
                        if exc.code == 'message_not_modified':
                            result = {'message_id':target}
                        elif exc.http_status not in {400, 404}:
                            raise
                        # Deleted/otherwise uneditable panels are replaced once.
                if result is None:
                    if not self.allowed(user):
                        return None
                    result = self.telegram.send_message(user, text, markup)
                if result is True or not isinstance(result, dict):
                    result = {'message_id':target}
            message_id = result.get('message_id') or target
            if interaction and message_id:
                interaction.update(fresh=False,anchor=message_id)
            if message_id:
                record = {'message_id': message_id, 'fingerprint': fingerprint, 'view': view,
                          'data': data or {}, 'updated_at': self.clock(),'revision':previous.get('revision',0)+1}
                self.store.mutate('panels', lambda records: dict(records, **{str(user): record}), {})
            return result
