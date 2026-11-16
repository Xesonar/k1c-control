import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from manager import Manager
from storage import Store
from test_manager import Printer,Telegram,gcode
from web_auth import COOKIE
from web_service import WebServer

OWNER=10001


class FakeMedia:
    def __init__(self,path):self.path=path;self.clips=[]
    def status(self,context=None):return {'available':True}
    def list(self,uid,limit=50):return [{'id':'sample','type':'clip'}]
    def request_clip(self,uid,seconds,context):self.clips.append((uid,seconds,context));return {'id':'request123','status':'queued'}
    def get(self,uid,request_id):return {'id':request_id,'status':'queued'}
    def download(self,uid,id):return {'path':str(self.path),'size':self.path.stat().st_size,'mime':'video/mp4','name':'sample.mp4'}
    def live_manifest(self):return '#EXTM3U\n#EXTINF:2.0,\n'+'a'*32+'-000000001.ts\n'
    def live_segment(self,name):return {'path':str(self.path),'mime':'video/mp2t'}
    def owner_delete(self,uid,id,*,is_owner):
        if not is_owner:raise AssertionError('backend must check owner before delegate')
        return {'deleted':True}


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state.sqlite');self.addCleanup(self.store.close)
        self.printer,self.telegram=Printer(),Telegram();self.now=1000
        self.manager=Manager(dict({'owner_id':10001}, **{'state_dir':str(self.root),'allowed_user_ids':[OWNER,22]}),self.store,self.printer,self.telegram,clock=lambda:self.now)
        static=self.root/'web';static.mkdir();(static/'index.html').write_text('<!doctype html><p>K1C</p>')
        mediafile=self.root/'sample.mp4';mediafile.write_bytes(bytes(range(100)))
        self.media=FakeMedia(mediafile)
        self.web=WebServer(self.manager,{'web_port':0,'web_origin':'https://k1c.example','web_static_dir':str(static),'state_dir':str(self.root)},self.media).start()
        self.addCleanup(self.web.close)
        self.web.auth.create(OWNER,OWNER,'owner','a-long-owner-password')
        self.web.auth.create(OWNER,22,'second','a-long-second-password')
        self.cookie='';self.csrf=''
        self.login()

    def request(self,method,path,body=None,headers=None,auth=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.web.server_port,timeout=5)
        heads={'Origin':'https://k1c.example'}
        if auth:heads.update(Cookie=self.cookie,**{'X-CSRF-Token':self.csrf})
        if body is not None and not isinstance(body,bytes):body=json.dumps(body).encode();heads['Content-Type']='application/json'
        heads.update(headers or {})
        conn.request(method,path,body,heads);r=conn.getresponse();data=r.read();status=r.status;hs=dict(r.getheaders());conn.close()
        if hs.get('Content-Type','').startswith('application/json'):data=json.loads(data)
        return status,data,hs

    def login(self,username='owner',password='a-long-owner-password'):
        code,data,hs=self.request('POST','/api/login',{'username':username,'password':password},auth=False)
        self.assertEqual(code,200,data)
        self.cookie=hs['Set-Cookie'].split(';')[0];self.csrf=data['data']['csrf'];return hs

    def mutate(self,path,body):
        return self.request('POST',path,dict(body,request_id=uuid.uuid4().hex))

    def test_cookie_csrf_origin_and_every_private_endpoint_require_auth(self):
        hs=self.login()
        for flag in ['HttpOnly','Secure','SameSite=Strict','Path=/']:self.assertIn(flag,hs['Set-Cookie'])
        for path in ['/api/status','/api/media/sample/download','/api/media/live/index.m3u8','/api/camera/snapshot']:
            self.assertEqual(self.request('GET',path,auth=False)[0],401)
        self.assertEqual(self.request('POST','/api/action',{'action':'pause','request_id':'request-test-1'},headers={'X-CSRF-Token':''})[0],403)
        self.assertEqual(self.request('POST','/api/action',{'action':'pause','request_id':'request-test-2'},headers={'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.printer.calls,[])

    def test_queries_capture_real_manager_ui_without_telegram_messages(self):
        for path in ['/api/me','/api/status','/api/files','/api/queue','/api/history','/api/jobs','/api/media']:
            code,data,_=self.request('GET',path);self.assertEqual(code,200,data)
        self.assertEqual(self.telegram.messages,[]);self.assertEqual(self.telegram.photos,[])
        self.assertEqual(self.printer.calls,[])

    def test_status_returns_immediately_while_printer_status_is_slow(self):
        original=self.printer.status
        def slow():time.sleep(.2);return original()
        self.printer.status=slow
        start=time.monotonic();code,data,_=self.request('GET','/api/status')
        self.assertEqual(code,200);self.assertLess(time.monotonic()-start,.15)
        self.assertIn(data['data']['connection']['state'],{'checking','fresh'})

    def test_shared_manager_pause_is_at_most_once_and_does_not_send_web_ui_to_telegram(self):
        body={'action':'pause','request_id':'request-pause-1'}
        a=self.request('POST','/api/action',body);b=self.request('POST','/api/action',body)
        self.assertEqual(a[0],200,a[1]);self.assertEqual(a[1],b[1])
        self.assertEqual(self.printer.calls,['pause'])
        self.assertIs(self.web.manager,self.manager)
        self.assertEqual(self.telegram.messages,[])
        self.assertEqual(self.request('POST','/api/action',dict(body,action='resume'))[0],409)

    def test_cancel_confirmation_is_bound_to_current_job(self):
        code,data,_=self.mutate('/api/action',{'action':'cancel-request'})
        self.assertEqual(code,200,data);token=data['data']['confirmation']['token']
        self.assertEqual(self.printer.calls,[])
        self.printer.job_id='replacement'
        code,data,_=self.mutate('/api/action',{'action':'cancel-confirm','token':token})
        self.assertEqual(code,200,data);self.assertEqual(self.printer.calls,[])
        self.assertTrue(any('Задание изменилось' in c['text'] for c in data['data']['cards']))

    def test_temperature_uses_native_pending_operation_with_numeric_target(self):
        code,data,_=self.mutate('/api/action',{'action':'temperature','heater':'extruder','target':245})
        self.assertEqual(code,200,data)
        self.assertEqual(self.printer.calls,[])
        self.manager.preflight.tick()
        self.assertIn(('temperature','extruder',245),self.printer.calls)
        self.assertEqual(self.mutate('/api/action',{'action':'temperature','heater':'extruder','target':999})[0],503)

    def test_revocation_blocks_an_already_authenticated_session(self):
        self.login('second','a-long-second-password')
        def revoke(s):s['users']['22']['status']='revoked';return s
        self.store.mutate('access_control',revoke)
        self.assertEqual(self.request('GET','/api/status')[0],401)
        self.assertEqual(self.mutate('/api/action',{'action':'pause'})[0],401)
        self.assertEqual(self.printer.calls,[])

    def test_nonowner_cannot_manage_accounts_power_or_detector(self):
        self.login('second','a-long-second-password')
        self.assertEqual(self.request('GET','/api/access')[0],403)
        for body in [{'action':'power','on':False},{'action':'control','enabled':False}]:
            self.assertEqual(self.mutate('/api/action',body)[0],403)
        self.assertEqual(self.mutate('/api/access',{'action':'reset','telegram_id':OWNER,'password':'a-new-owner-password'})[0],403)
        self.assertEqual(self.printer.calls,[])

    def test_private_range_and_live_manifest_paths(self):
        code,data,hs=self.request('GET','/api/media/sample/download',headers={'Range':'bytes=10-19'})
        self.assertEqual(code,206);self.assertEqual(data,bytes(range(10,20)));self.assertEqual(hs['Content-Range'],'bytes 10-19/100')
        code,data,hs=self.request('GET','/api/media/sample/download',headers={'Range':'bytes=100-200'})
        self.assertEqual(code,416)
        code,data,_=self.request('GET','/api/media/live/index.m3u8')
        self.assertEqual(code,200);self.assertIn(b'/api/media/live/',data)

    def test_upload_prepares_existing_pipeline_without_starting_print(self):
        self.printer.state='standby';raw=gcode().encode()
        code,data,_=self.request('POST','/api/uploads',raw,{'X-Filename':'part.gcode','X-Request-ID':'request-upload-1','Content-Type':'application/octet-stream'})
        self.assertEqual(code,200,data);asset_id=data['data']['asset']['id']
        self.assertEqual(self.printer.calls,[])
        self.manager.process_slicing_job()
        code,data,_=self.request('GET','/api/jobs?asset_id='+asset_id)
        self.assertTrue(data['data']['asset']['ready'],data)
        token=data['data']['confirmation']['token']
        code,data,_=self.mutate('/api/jobs',{'action':'start-confirm','asset_id':asset_id,'token':token})
        self.assertEqual(code,409)
        self.assertEqual(self.printer.calls,[])
        code,data,_=self.mutate('/api/jobs',{'action':'start-confirm','asset_id':asset_id,'token':token,'table_clear':True,'plate_installed':True})
        self.assertEqual(code,200,data)
        self.assertEqual(self.printer.calls,[])
        self.manager.preflight.tick()
        self.assertEqual(len(self.printer.calls),1)
        self.assertEqual(self.printer.calls[0][0],'start')
        self.assertEqual(self.printer.job_id,'calibrated-job')

    def test_upload_rejects_mismatched_mime_and_path_names(self):
        for name,mime in [('part.gcode','image/png'),('../part.gcode','application/octet-stream'),('part.exe','application/octet-stream')]:
            self.assertGreaterEqual(self.request('POST','/api/uploads',b'G1 X1',{'X-Filename':name,'X-Request-ID':'request-upload-test','Content-Type':mime})[0],400)
        self.assertEqual(self.manager._assets(),{})

    def test_full_preparation_queue_rejects_upload_without_telegram_or_orphan(self):
        self.store.set('slicing_jobs',{str(i):{'id':str(i),'asset_id':str(i),'status':'queued'} for i in range(8)})
        before=set(self.manager.uploads.iterdir())
        code,data,_=self.request('POST','/api/uploads',gcode().encode(),{'X-Filename':'extra.gcode','X-Request-ID':'request-full-queue-1','Content-Type':'application/octet-stream'})
        self.assertEqual(code,429,data)
        self.assertEqual(data['error']['code'],'preparation_queue_full')
        self.assertEqual(self.telegram.messages,[])
        self.assertEqual(self.manager._assets(),{})
        self.assertEqual(set(self.manager.uploads.iterdir()),before)


    def test_concurrent_retry_does_not_repeat_an_inflight_physical_command(self):
        entered,release=threading.Event(),threading.Event()
        original=self.printer.pause
        def blocked():
            entered.set()
            if not release.wait(3):raise AssertionError('test release missing')
            original()
        self.printer.pause=blocked
        body={'action':'pause','request_id':'request-inflight-1'}
        results=[]
        thread=threading.Thread(target=lambda:results.append(self.request('POST','/api/action',body)))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            code,data,_=self.request('POST','/api/action',body)
            self.assertEqual(code,409,data)
            self.assertEqual(data['error']['code'],'request_in_progress')
        finally:
            release.set();thread.join(3)
        self.assertEqual(len(results),1)
        self.assertEqual(results[0][0],200,results)
        self.assertEqual(self.printer.calls,['pause'])

    def test_private_media_mutation_is_role_checked_and_clip_context_is_fresh(self):
        self.printer.job_id='current-fresh-job'
        code,data,_=self.mutate('/api/media/clip',{'seconds':30})
        self.assertEqual(code,200,data)
        self.assertEqual(self.media.clips[0][2]['job_id'],'current-fresh-job')
        code,data,_=self.mutate('/api/media/sample/delete',{})
        self.assertEqual(code,200,data)
        self.assertTrue(data['data']['deleted'])
        self.login('second','a-long-second-password')
        self.assertEqual(self.mutate('/api/media/sample/delete',{})[0],403)

    def test_revocation_stops_an_existing_private_download_before_next_chunk(self):
        from types import SimpleNamespace
        from web_service import Handler
        self.media.path.write_bytes(b'v'*(65536*3))
        handler=object.__new__(Handler)
        handler.server=self.web;handler.path='/api/media/sample/download';handler.command='GET'
        handler.headers={'Cookie':self.cookie};handler.close_connection=False
        handler._headers=lambda *args:None
        received=[]
        def write(chunk):
            received.append(chunk)
            self.web.auth.logout(self.cookie.split('=',1)[1])
        handler.wfile=SimpleNamespace(write=write)
        handler._file(self.media.path,'video/mp4')
        self.assertEqual(sum(map(len,received)),65536)
        self.assertTrue(handler.close_connection)

    def test_wrong_actor_cannot_consume_an_owner_confirmation(self):
        code,data,_=self.mutate('/api/action',{'action':'cancel-request'})
        token=data['data']['confirmation']['token']
        self.login('second','a-long-second-password')
        self.assertEqual(self.mutate('/api/action',{'action':'cancel-confirm','token':token})[0],409)
        self.assertEqual(self.printer.calls,[])
        self.login()
        self.assertEqual(self.mutate('/api/action',{'action':'cancel-confirm','token':token})[0],200)
        self.assertEqual(self.printer.calls,['cancel'])

    def test_static_traversal_symlink_and_private_config_are_not_served(self):
        (self.root/'secret.js').write_text('PRIVATE')
        (self.root/'web'/'escape.js').symlink_to(self.root/'secret.js')
        for path in ['/../secret.js','/%2e%2e/secret.js','/escape.js','/web_auth.py']:
            code,data,_=self.request('GET',path,auth=False)
            self.assertEqual(code,404,(path,data))
        code,data,hs=self.request('GET','/',auth=False)
        self.assertEqual(code,200)
        for header in ['Content-Security-Policy','X-Content-Type-Options','Cache-Control']:
            self.assertIn(header,hs)

    def test_head_suffix_range_and_multi_range(self):
        code,data,hs=self.request('HEAD','/api/media/sample/download',headers={'Range':'bytes=-10'})
        self.assertEqual(code,206);self.assertEqual(data,b'')
        self.assertEqual(hs['Content-Length'],'10')
        code,data,_=self.request('GET','/api/media/sample/download',headers={'Range':'bytes=-10'})
        self.assertEqual(data,bytes(range(90,100)))
        self.assertEqual(self.request('GET','/api/media/sample/download',headers={'Range':'bytes=1-2,4-5'})[0],416)

    def test_password_mutation_does_not_store_a_fast_password_verifier(self):
        body={'action':'create','request_id':'request-credentials-1','telegram_id':22,'username':'already-present','password':'a-secret-new-password'}
        # Conflict is also a durable outcome; fingerprint must not depend on the secret.
        self.request('POST','/api/access',body)
        saved=self.web.auth.db.execute('SELECT fingerprint FROM requests WHERE request_id=?',(body['request_id'],)).fetchone()[0]
        import hashlib
        sanitized=dict(body,password='[REDACTED]')
        expected=hashlib.sha256(json.dumps({'path':'/api/access','body':sanitized},sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        self.assertEqual(saved,expected)


if __name__=='__main__':unittest.main()
