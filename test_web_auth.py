import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from web_auth import WebAuth, WebError

OWNER=10001


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.allowed={OWNER,22};self.now=1000
        self.auth=WebAuth(Path(self.tmp.name)/'web.sqlite3',lambda u:u in self.allowed,lambda u:u==OWNER,clock=lambda:self.now)
        self.addCleanup(self.auth.close)
        self.auth.create(OWNER,OWNER,'owner','a-long-owner-password')

    def test_password_is_salted_hash_and_session_token_is_not_persisted(self):
        token,session=self.auth.login('OWNER','a-long-owner-password','127.0.0.1')
        row=self.auth.db.execute('SELECT * FROM users').fetchone()
        self.assertNotEqual(row['password_hash'],b'a-long-owner-password')
        self.assertEqual(len(row['salt']),16)
        self.assertGreaterEqual(row['iterations'],600000)
        saved=self.auth.db.execute('SELECT token_hash FROM sessions').fetchone()[0]
        self.assertEqual(saved,hashlib.sha256(token.encode()).hexdigest())
        self.assertTrue(session['user']['is_owner'])
        self.assertEqual(os.stat(self.auth.path).st_mode&0o777,0o600)

    def test_revoke_is_effective_for_existing_session_immediately(self):
        self.auth.create(OWNER,22,'second','a-long-second-password')
        token,_=self.auth.login('second','a-long-second-password','127.0.0.1')
        self.allowed.remove(22)
        self.assertIsNone(self.auth.session(token))
        with self.assertRaises(WebError) as c:self.auth.login('second','a-long-second-password','127.0.0.1')
        self.assertEqual(c.exception.status,401)

    def test_reset_invalidates_sessions_and_old_password(self):
        token,_=self.auth.login('owner','a-long-owner-password','127.0.0.1')
        self.auth.reset(OWNER,OWNER,'a-different-password')
        self.assertIsNone(self.auth.session(token))
        with self.assertRaises(WebError):self.auth.login('owner','a-long-owner-password','127.0.0.1')
        self.assertIsNotNone(self.auth.login('owner','a-different-password','127.0.0.1')[1])

    def test_only_owner_creates_resets_or_deletes_accounts(self):
        for fn in [lambda:self.auth.create(22,22,'second','a-long-second-password'),lambda:self.auth.reset(22,OWNER,'a-different-password'),lambda:self.auth.delete(22,OWNER)]:
            with self.assertRaises(WebError) as c:fn()
            self.assertEqual(c.exception.status,403)
        with self.assertRaises(WebError):self.auth.create(OWNER,999,'outsider','a-long-unknown-password')

    def test_rate_limit_is_persistent_and_has_generic_login_failure(self):
        for _ in range(5):
            with self.assertRaises(WebError) as c:self.auth.login('owner','incorrect-password','127.0.0.1')
            self.assertEqual(c.exception.code,'bad_credentials')
        with self.assertRaises(WebError) as c:self.auth.login('owner','a-long-owner-password','127.0.0.1')
        self.assertEqual(c.exception.status,429)
        self.now+=901
        self.assertIsNotNone(self.auth.login('owner','a-long-owner-password','127.0.0.1')[1])

    def test_claimed_request_is_never_replayed_or_reused_for_other_body(self):
        self.assertIsNone(self.auth.claim_request(OWNER,'request-12345','first'))
        with self.assertRaises(WebError) as c:self.auth.claim_request(OWNER,'request-12345','first')
        self.assertEqual(c.exception.code,'request_in_progress')
        result={'status':200,'body':{'ok':True,'data':{'accepted':True}}}
        self.auth.complete_request(OWNER,'request-12345',result)
        self.assertEqual(self.auth.claim_request(OWNER,'request-12345','first'),result)
        with self.assertRaises(WebError) as c:self.auth.claim_request(OWNER,'request-12345','other')
        self.assertEqual(c.exception.code,'request_id_conflict')


    def test_expired_and_logged_out_sessions_are_rejected(self):
        token,_=self.auth.login('owner','a-long-owner-password','127.0.0.1')
        self.now+=43201
        self.assertIsNone(self.auth.session(token))
        token,_=self.auth.login('owner','a-long-owner-password','127.0.0.1')
        self.auth.logout(token)
        self.assertIsNone(self.auth.session(token))

    def test_delete_removes_all_sessions_for_that_account(self):
        self.auth.create(OWNER,22,'second','a-long-second-password')
        token,_=self.auth.login('second','a-long-second-password','127.0.0.1')
        self.auth.delete(OWNER,22)
        self.assertIsNone(self.auth.session(token))
        self.assertFalse(any(u['telegram_id']==22 for u in self.auth.users(OWNER)))


if __name__=='__main__':unittest.main()
