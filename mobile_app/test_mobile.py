import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import server
from history import History


class MultiUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'history.sqlite3'
        self.service=server.App('fake-token',self.path,'https://match.example.test',max_jobs=2,daily_limit=2)
        self.alice=self.service.store.add_user('alice','alice-long-password')
        self.bob=self.service.store.add_user('bob','bob-long-password')
        self.web=server.create_app(self.service);self.a=self.web.test_client();self.b=self.web.test_client()
        self.ca=self.login(self.a,'alice','alice-long-password');self.cb=self.login(self.b,'bob','bob-long-password')

    def tearDown(self):
        for _ in range(300):
            if not self.service.active_users:break
            time.sleep(.01)
        self.tmp.cleanup()

    def get(self,client,path):return client.get(path,base_url=self.service.origin)
    def post(self,client,path,data,csrf='',origin=None):
        return client.post(path,json=data,base_url=self.service.origin,headers={'Origin':origin or self.service.origin,'X-CSRF-Token':csrf})
    def login(self,client,username,password):
        result=self.post(client,'/api/login',dict(username=username,password=password));self.assertEqual(result.status_code,200)
        self.assertIn('Secure',result.headers['Set-Cookie']);self.assertIn('HttpOnly',result.headers['Set-Cookie'])
        return self.get(client,'/api/session').json['csrf']
    def wait(self):
        for _ in range(300):
            if not self.service.active_users:return
            time.sleep(.01)
        self.fail('Worker failed to finish')

    def test_private_history_and_direct_access(self):
        ident=self.service.store.create('alice private','model',{'products':[]},self.alice)
        self.assertEqual(self.get(self.a,'/api/history').json['requests'][0]['id'],ident)
        self.assertEqual(self.get(self.b,'/api/history').json['requests'],[])
        self.assertEqual(self.get(self.b,'/api/requests/'+ident).status_code,404)
        self.assertEqual(self.get(self.a,'/api/requests/'+ident).status_code,200)
        self.assertEqual(self.get(self.web.test_client(),'/api/history').status_code,401)

    def test_sessions_persist_expire_and_revoke(self):
        raw=self.a.get_cookie(server.COOKIE,domain='match.example.test').value
        reopened=History(self.path);self.assertEqual(reopened.session(raw)['user_id'],self.alice)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions WHERE token_hash=?',(raw,)).fetchone()[0],0)
        self.post(self.a,'/api/logout',{},self.ca)
        self.assertIsNone(reopened.session(raw))
        self.login(self.a,'alice','alice-long-password')
        reopened.reset_password('alice','new-alice-password')
        self.assertEqual(self.get(self.a,'/api/history').status_code,401)
        sid=reopened.new_session(self.alice)
        with reopened.connect() as db:db.execute('UPDATE sessions SET expires=0')
        self.assertIsNone(reopened.session(sid))

    def test_security_boundaries(self):
        self.assertEqual(self.post(self.a,'/api/requests',{'request':'pump'}).status_code,403)
        self.assertEqual(self.post(self.a,'/api/requests',{'request':'pump'},self.ca,'https://evil.test').status_code,403)
        self.assertEqual(self.a.get('/',base_url='https://evil.test').status_code,403)
        for path in ['/TOKEN.txt','/data/history.sqlite3','/../TOKEN.txt']:
            self.assertEqual(self.get(self.a,path).status_code,404)
        self.assertEqual(self.post(self.a,'/api/requests',{'request':'x'*12001},self.ca).status_code,400)
        self.assertEqual(self.a.post('/api/login',data='x'*60001,base_url=self.service.origin,headers={'Origin':self.service.origin}).status_code,413)

    def test_parallel_users_limits_and_saved_results(self):
        cat={'products':[{'id':str(i),'name':'P'} for i in range(12)],'catalogue_conventions':{}}
        release=threading.Event()
        def evaluate(token,text,catalogue,products):
            release.wait(2)
            return [{'product_id':p['id'],'summary':text} for p in products],{}
        with patch.object(server.matcher,'load_catalogue',return_value=cat),patch.object(server.matcher,'evaluate_batch',side_effect=evaluate) as calls:
            ra=self.post(self.a,'/api/requests',{'request':'alice request'},self.ca)
            rb=self.post(self.b,'/api/requests',{'request':'bob request'},self.cb)
            self.assertEqual(ra.status_code,202);self.assertEqual(rb.status_code,202)
            self.assertEqual(self.post(self.a,'/api/requests',{'request':'double'},self.ca).status_code,429)
            release.set();self.wait()
            a=self.get(self.a,'/api/requests/'+ra.json['id']).json
            b=self.get(self.b,'/api/requests/'+rb.json['id']).json
            self.assertTrue(a['complete']);self.assertTrue(b['complete']);self.assertEqual(len(a['assessments']),12)
            self.assertEqual(a['assessments'][0]['summary'],'alice request');self.assertEqual(b['assessments'][0]['summary'],'bob request')
            self.assertEqual(calls.call_count,4)
            self.get(self.a,'/api/requests/'+ra.json['id']);self.assertEqual(calls.call_count,4)
            self.post(self.a,'/api/requests',{'request':'second'},self.ca);self.wait()
            self.assertEqual(self.post(self.a,'/api/requests',{'request':'over quota'},self.ca).status_code,429)

    def test_login_rate_limit_and_password_hash(self):
        with self.service.store.connect() as db:
            hashed=db.execute('SELECT password_hash FROM users WHERE id=?',(self.alice,)).fetchone()[0]
        self.assertTrue(hashed.startswith('scrypt:'));self.assertNotIn('alice-long-password',hashed)
        for _ in range(11):result=self.post(self.a,'/api/login',{'username':'alice','password':'wrong'})
        self.assertEqual(result.status_code,429)

    def test_legacy_is_private_until_claimed(self):
        ident=self.service.store.create('old shared request','model',{'products':[]},None)
        self.assertIsNone(self.service.store.get(ident,self.alice));self.assertIsNone(self.service.store.get(ident,self.bob))
        self.assertEqual(self.service.store.claim_legacy('alice'),1)
        self.assertIsNotNone(self.service.store.get(ident,self.alice));self.assertIsNone(self.service.store.get(ident,self.bob))

    def test_empty_catalogue_and_failures(self):
        with patch.object(server.matcher,'load_catalogue',return_value={'products':[],'catalogue_conventions':{}}),patch.object(server.matcher,'evaluate_batch') as calls:
            r=self.post(self.a,'/api/requests',{'request':'test'},self.ca);self.wait()
            self.assertTrue(self.get(self.a,'/api/requests/'+r.json['id']).json['complete']);calls.assert_not_called()
        with patch.object(server.matcher,'load_catalogue',return_value={'products':[{'id':'1'}],'catalogue_conventions':{}}),patch.object(server.matcher,'evaluate_batch',side_effect=RuntimeError('Provider unavailable')):
            r=self.post(self.a,'/api/requests',{'request':'test'},self.ca);self.wait()
            self.assertEqual(self.get(self.a,'/api/requests/'+r.json['id']).json['status'],'failed')
        self.assertFalse(self.service.active_users)

if __name__=='__main__':unittest.main()
