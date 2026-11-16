"""Loopback-only HTTP adapter embedded in the running K1C bot process."""
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from adapters import ApiError
from web_auth import COOKIE, WebAuth, WebError
from web_actions import WebActions
from media_store import MediaError

LOG = logging.getLogger('k1c-web')
MAX_STL = 20 * 1024 * 1024
MAX_GCODE = 128 * 1024 * 1024
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def clean_json(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,dict):return {str(k):clean_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean_json(v) for v in value]
    return value


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, manager, config=None, media=None):
        self.manager, self.config, self.media = manager, config or {}, media
        host = self.config.get('web_host','127.0.0.1')
        if host != '127.0.0.1':raise ValueError('web_bind_must_be_loopback')
        self.static_dir = Path(self.config.get('web_static_dir',Path(__file__).parent/'web')).resolve()
        self.origin = self.config.get('web_origin','').rstrip('/')
        if self.origin and (urlsplit(self.origin).scheme!='https' or not urlsplit(self.origin).netloc):
            raise ValueError('web_origin_must_be_https')
        self.actions = WebActions(manager)
        self.auth = WebAuth(self.config.get('web_auth_db',Path(self.config.get('state_dir',manager.uploads.parent))/'web.sqlite3'),
                            manager.access.is_allowed, manager.access.is_owner,
                            clock=manager.clock, session_seconds=self.config.get('web_session_seconds',43200))
        self.worker = None
        super().__init__((host,int(self.config.get('web_port',3340))),Handler)

    def start(self):
        self.worker = threading.Thread(target=lambda:self.serve_forever(poll_interval=.1),name='k1c-web',daemon=True)
        self.worker.start()
        return self

    def close(self):
        if self.worker and self.worker.is_alive():
            self.shutdown()
            self.worker.join(5)
        self.server_close()
        self.actions.close()
        self.auth.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'K1C'
    sys_version = ''

    def log_message(self, *_):
        # Never log query strings, cookies, submitted usernames or passwords.
        pass

    def _headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',CSP)
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers()

    def _json(self, status, value, extra=None):
        raw = json.dumps(clean_json(value),ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        self._headers(status,'application/json; charset=utf-8',len(raw),extra)
        if self.command!='HEAD':self.wfile.write(raw)

    def _ok(self, value, status=200, extra=None):
        self._json(status,{'ok':True,'data':value},extra)

    def _error(self, exc):
        self._json(exc.status,{'ok':False,'error':{'code':exc.code,'message':exc.message}})

    def _length(self, maximum):
        if self.headers.get('Transfer-Encoding'):
            raise WebError('transfer_encoding_unsupported','Передай файл с известным размером.',400)
        try:length=int(self.headers.get('Content-Length','-1'))
        except ValueError:raise WebError('invalid_length','Неверный размер запроса.')
        if length<0:raise WebError('length_required','Не указан размер запроса.',411)
        if length>maximum:raise WebError('request_too_large','Размер файла или запроса превышает лимит.',413)
        return length

    def _body(self):
        if self.headers.get_content_type()!='application/json':
            raise WebError('json_required','Нужен запрос в формате JSON.',415)
        length=self._length(16384)
        raw=self.rfile.read(length)
        if len(raw)!=length:raise WebError('incomplete_request','Запрос передан не полностью.')
        try:
            value=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError,UnicodeError):raise WebError('invalid_json','Не удалось прочитать JSON.')
        if not isinstance(value,dict):raise WebError('invalid_json','Ожидается объект JSON.')
        return value

    def _token(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            return cookie[COOKIE].value if COOKIE in cookie else ''
        except Exception:return ''

    def _session(self, mutation=False):
        session=self.server.auth.session(self._token())
        if not session:raise WebError('login_required','Войди в свой аккаунт K1C.',401)
        if mutation:
            self._origin()
            supplied=self.headers.get('X-CSRF-Token','')
            if not supplied or not secrets.compare_digest(supplied,session['csrf']):
                raise WebError('csrf_invalid','Проверка сессии не пройдена. Обнови страницу.',403)
        return session

    def _origin(self):
        origin=self.headers.get('Origin')
        if origin and (not self.server.origin or origin.rstrip('/')!=self.server.origin):
            raise WebError('origin_invalid','Запрос с другого сайта отклонён.',403)

    def _client_ip(self):
        value=self.headers.get('X-Real-IP') if self.server.config.get('web_trust_proxy',True) else None
        try:return str(ipaddress.ip_address(value)) if value else self.client_address[0]
        except ValueError:return self.client_address[0]

    def _cookie(self, token, remove=False):
        return f'{COOKIE}={token}; Path=/; Max-Age={0 if remove else self.server.auth.session_seconds}; HttpOnly; Secure; SameSite=Strict'

    def _mutate(self, session, body, operation, fingerprint=None):
        uid=session['user']['telegram_id']
        request_id=body.get('request_id')
        fingerprint_body={k:('[REDACTED]' if k.lower() in {'password','new_password'} else v) for k,v in body.items()}
        fingerprint=fingerprint or hashlib.sha256(json.dumps({'path':urlsplit(self.path).path,'body':fingerprint_body},sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        cached=self.server.auth.claim_request(uid,request_id,fingerprint)
        if cached is not None:
            self._json(cached['status'],cached['body'])
            return
        try:
            self.server.actions.allowed(uid)
            data=operation('web:'+str(uid)+':'+request_id)
            reply={'status':200,'body':{'ok':True,'data':clean_json(data)}}
        except WebError as exc:
            reply={'status':exc.status,'body':{'ok':False,'error':{'code':exc.code,'message':exc.message}}}
        except MediaError as exc:
            reply={'status':exc.status,'body':{'ok':False,'error':{'code':exc.code,'message':'Видео сейчас недоступно. Обнови раздел видео.'}}}
        except ApiError as exc:
            reply={'status':503,'body':{'ok':False,'error':{'code':exc.code,'message':'Не удалось подтвердить действие. Проверь пульт перед новой командой.','uncertain':bool(exc.uncertain)}}}
        except Exception:
            # A claimed physical command is never replayed after an unknown outcome.
            reply={'status':500,'body':{'ok':False,'error':{'code':'action_unknown','message':'Результат команды уточняется. Проверь состояние K1C; автоматически команда не повторяется.'}}}
        self.server.auth.complete_request(uid,request_id,reply)
        self._json(reply['status'],reply['body'])

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def _guard(self, fn):
        self.connection.settimeout(30)
        try:fn()
        except WebError as exc:self._error(exc)
        except MediaError as exc:self._error(WebError(exc.code,'Видео сейчас недоступно. Обнови раздел видео.',exc.status))
        except ApiError as exc:self._error(WebError(exc.code,'K1C сейчас не смог выполнить запрос.',503))
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        except Exception as exc:
            LOG.warning('web request failed type=%s',type(exc).__name__)
            self._error(WebError('web_internal','Не удалось выполнить запрос. Повтори позже.',500))

    def _get(self):
        url=urlsplit(self.path);path=unquote(url.path);query=parse_qs(url.query)
        if not path.startswith('/api/'):
            return self._static(path)
        session=self._session();uid=session['user']['telegram_id'];actions=self.server.actions
        if path=='/api/me':return self._ok(session)
        if path=='/api/status':
            value=actions.status(uid)
            if self.server.media:value['media']=self.server.media.status()
            return self._ok(value)
        if path=='/api/camera/snapshot':
            photo=self.server.manager._photo()
            self._headers(200,'image/jpeg',len(photo))
            if self.command!='HEAD':self.wfile.write(photo)
            return
        if path=='/api/history':return self._ok(actions.history(uid))
        if path=='/api/files':return self._ok(actions.files(uid))
        if path=='/api/queue':return self._ok(actions.queue(uid))
        if path=='/api/jobs':return self._ok(actions.jobs(uid,query.get('asset_id',[None])[0],query.get('qid',[None])[0]))
        if path=='/api/access':return self._ok({'accounts':self.server.auth.users(uid),'users':actions.access(uid)})
        if path.startswith('/api/media'):
            return self._get_media(uid,path,query)
        raise WebError('not_found','Страница не найдена.',404)

    def _post(self):
        path=unquote(urlsplit(self.path).path)
        if path=='/api/login':
            self._origin();body=self._body()
            token,session=self.server.auth.login(body.get('username'),body.get('password'),self._client_ip())
            return self._ok(session,extra={'Set-Cookie':self._cookie(token)})
        session=self._session(mutation=True);uid=session['user']['telegram_id']
        if path=='/api/logout':
            self.server.auth.logout(self._token())
            return self._ok({'logged_out':True},extra={'Set-Cookie':self._cookie('',True)})
        if path=='/api/uploads':return self._upload(session)
        body=self._body()
        if path=='/api/action':return self._mutate(session,body,lambda key:self.server.actions.action(uid,body,key))
        if path=='/api/jobs':return self._mutate(session,body,lambda key:self.server.actions.job_action(uid,body,key))
        if path=='/api/access':
            def account_action(_):
                action=body.get('action');auth=self.server.auth
                if action=='create':return auth.create(uid,body.get('telegram_id'),body.get('username'),body.get('password'))
                if action=='reset':return auth.reset(uid,body.get('telegram_id'),body.get('password'))
                if action=='delete':return auth.delete(uid,body.get('telegram_id'))
                return self.server.actions.access_change(uid,body)
            return self._mutate(session,body,account_action)
        if path=='/api/media/clip':
            media=self._media()
            seconds=body.get('seconds',30)
            if type(seconds) is not int or not 1<=seconds<=300:raise WebError('invalid_duration','Выбери длительность от 1 до 300 секунд.')
            return self._mutate(session,body,lambda _:media.request_clip(uid,seconds,self.server.manager.context()))
        match=re.fullmatch(r'/api/media/([A-Za-z0-9_-]{1,128})/delete',path)
        if match:
            media=self._media()
            self.server.actions.owner(uid)
            return self._mutate(session,body,lambda _:media.owner_delete(uid,match.group(1),is_owner=True))
        raise WebError('not_found','Действие не найдено.',404)

    def _upload(self, session):
        name=unquote(self.headers.get('X-Filename',''))
        if (not name or Path(name).name!=name or '\\' in name or any(ord(c)<32 for c in name) or len(name.encode())>180):
            raise WebError('invalid_filename','Неверное имя файла.')
        suffix=Path(name).suffix.lower()
        if suffix not in {'.stl','.gcode','.g','.gco','.nc'}:raise WebError('unsupported_file','Пришли STL или готовый G-code.',415)
        mime=self.headers.get_content_type()
        accepted={'application/octet-stream','model/stl','application/sla'} if suffix=='.stl' else {'application/octet-stream','text/plain','text/x-gcode','application/x-gcode'}
        if mime not in accepted:raise WebError('unsupported_mime','Тип содержимого не соответствует модели или G-code.',415)
        length=self._length(MAX_STL if suffix=='.stl' else MAX_GCODE)
        if length<1:raise WebError('empty_file','Файл пуст.')
        tmp=self.server.manager.uploads/('web-upload-'+uuid.uuid4().hex+'.part')
        digest=hashlib.sha256()
        try:
            fd=os.open(str(tmp),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as output:
                remaining=length
                while remaining:
                    chunk=self.rfile.read(min(65536,remaining))
                    if not chunk:raise WebError('incomplete_upload','Файл передан не полностью.')
                    output.write(chunk);digest.update(chunk);remaining-=len(chunk)
            body={'request_id':self.headers.get('X-Request-ID'),'filename':name,'size':length,'sha256':digest.hexdigest()}
            self._mutate(session,body,lambda _:self.server.actions.upload(session['user']['telegram_id'],name,tmp,length))
        finally:
            tmp.unlink(missing_ok=True)

    def _media(self):
        if self.server.media is None:raise WebError('media_unavailable','Видео пока недоступно.',503)
        return self.server.media

    def _get_media(self, uid, path, query):
        media=self._media()
        if path=='/api/media':return self._ok({'status':media.status(),'items':media.list(uid,limit=50)})
        if path=='/api/media/live/index.m3u8':
            manifest=media.live_manifest()
            lines=[]
            for line in manifest.splitlines():
                if line and not line.startswith('#'):
                    if not re.fullmatch(r'[a-f0-9]{32}-[0-9]{9}\.ts',line):raise WebError('invalid_manifest','Видеопоток пока не готов.',503)
                    line='/api/media/live/'+line
                lines.append(line)
            return self._media_content(('\n'.join(lines)+'\n').encode(),'application/vnd.apple.mpegurl')
        match=re.fullmatch(r'/api/media/live/([A-Za-z0-9_.-]{1,180})',path)
        if match:return self._media_content(media.live_segment(match.group(1)),'video/mp2t')
        match=re.fullmatch(r'/api/media/jobs/([A-Za-z0-9_-]{1,128})',path)
        if match:return self._ok(media.get(uid,match.group(1)))
        match=re.fullmatch(r'/api/media/([A-Za-z0-9_-]{1,128})/download',path)
        if match:return self._media_content(media.download(uid,match.group(1)),'video/mp4')
        raise WebError('not_found','Видео не найдено.',404)

    def _media_content(self, value, default_mime):
        if isinstance(value,str) and value.lstrip().startswith('#EXTM3U'):
            value=value.encode()
        if isinstance(value,bytes):
            self._headers(200,default_mime,len(value))
            if self.command!='HEAD':self.wfile.write(value)
            return
        if isinstance(value,dict):
            if isinstance(value.get('data'),bytes):return self._media_content(value['data'],value.get('mime',default_mime))
            return self._file(Path(value['path']),value.get('mime',default_mime),value.get('name',value.get('filename')))
        return self._file(Path(value),default_mime)

    def _file(self, path, mime, name=None):
        try:
            fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            file=os.fdopen(fd,'rb')
        except OSError:raise WebError('file_missing','Файл сейчас недоступен.',404)
        with file:
            info=os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):raise WebError('file_missing','Файл недоступен.',404)
            size=info.st_size;start=0;end=size-1;status=200
            range_header=self.headers.get('Range')
            if range_header:
                match=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header)
                if not match or size==0 or not any(match.groups()):
                    return self._range_error(size)
                a,b=match.groups()
                if a:
                    start=int(a);end=min(int(b),size-1) if b else size-1
                else:
                    amount=int(b)
                    if amount<=0:return self._range_error(size)
                    start=max(0,size-amount)
                if start>=size or start>end:return self._range_error(size)
                status=206
            extra={'Accept-Ranges':'bytes'}
            if status==206:extra['Content-Range']=f'bytes {start}-{end}/{size}'
            if name:
                safe=re.sub(r'[^A-Za-z0-9_.-]','_',str(name))[:120]
                extra['Content-Disposition']='inline; filename="'+safe+'"'
            length=max(0,end-start+1)
            self._headers(status,mime,length,extra)
            if self.command=='HEAD':return
            file.seek(start)
            remaining=length
            private=urlsplit(self.path).path.startswith('/api/')
            token=self._token() if private else None
            while remaining:
                if private and not self.server.auth.session(token):
                    # Headers may already be sent: close the response rather than
                    # append an error document to a revoked private download.
                    self.close_connection=True
                    return
                chunk=file.read(min(65536,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)

    def _range_error(self, size):
        self._headers(416,'application/octet-stream',0,{'Content-Range':f'bytes */{size}'})

    def _static(self, path):
        if path=='/' or ('.' not in Path(path).name and not path.startswith('/assets/')):
            path='/index.html'
        file=(self.server.static_dir/path.lstrip('/')).resolve()
        if not file.is_relative_to(self.server.static_dir) or not file.is_file():raise WebError('not_found','Страница не найдена.',404)
        if file.suffix.lower() not in {'.html','.js','.css','.svg','.png','.ico','.woff','.woff2'}:
            raise WebError('not_found','Страница не найдена.',404)
        return self._file(file,mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
