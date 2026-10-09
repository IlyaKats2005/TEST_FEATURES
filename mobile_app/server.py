#!/usr/bin/env python3
"""PocketMatch multi-user API. Local: python3 mobile_app/server.py.
Production: gunicorn --chdir mobile_app --workers 1 --threads 8 'server:create_app()'
"""
import argparse
import getpass
import hmac
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, send_from_directory, g
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
import product_matcher as matcher
from history import History
from catalogue_editor import register_editor, is_owner

COOKIE='pocketmatch_session'
STATIC=ROOT/'static'


def db_path():
    return os.environ.get('HISTORY_DB',str(ROOT/'data'/'history.sqlite3'))


class App:
    def __init__(self,token,path,origin,max_jobs=2,daily_limit=10):
        self.token=token;self.origin=origin.rstrip('/')
        self.store=History(path);self.store.recover_interrupted()
        self.max_jobs=max_jobs;self.daily_limit=daily_limit
        self.guard=threading.Lock();self.active_users=set()
        self.dummy_hash=generate_password_hash('invalid-account-placeholder',method='scrypt')

    def submit(self,user_id,text):
        with self.guard:
            if user_id in self.active_users:raise RuntimeError('You already have an assessment running. Open it in History.')
            if len(self.active_users)>=self.max_jobs:raise RuntimeError('The server is busy. Try again shortly.')
            if self.store.submissions_today(user_id)>=self.daily_limit:raise RuntimeError('Your daily assessment limit has been reached. Try again tomorrow (UTC).')
            catalogue=matcher.load_catalogue()
            ident=self.store.create(text,matcher.MODEL,catalogue,user_id)
            self.active_users.add(user_id)
            try:threading.Thread(target=self.work,args=(ident,user_id,text,catalogue),daemon=True).start()
            except Exception:
                self.active_users.discard(user_id)
                self.store.set_status(ident,'failed','Could not start assessment. No model call was made.')
                raise
            return ident

    def work(self,ident,user_id,text,catalogue):
        try:
            self.store.set_status(ident,'running')
            for start in range(0,len(catalogue['products']),matcher.BATCH_SIZE):
                rows,usage=matcher.evaluate_batch(self.token,text,catalogue,catalogue['products'][start:start+matcher.BATCH_SIZE])
                self.store.append(ident,rows,usage)
            self.store.set_status(ident,'complete')
        except Exception as exc:
            self.store.set_status(ident,'failed',str(exc) if isinstance(exc,RuntimeError) else 'Assessment stopped because of a server error. Saved results remain available.')
        finally:
            with self.guard:self.active_users.discard(user_id)


def create_app(service=None):
    if os.environ.get('HF_TOKEN_FILE'):
        matcher.TOKEN_PATH=Path(os.environ['HF_TOKEN_FILE'])
    if os.environ.get('CATALOGUE_FILE'):
        matcher.CATALOGUE_PATH=Path(os.environ['CATALOGUE_FILE'])
    if service is None:
        origin=os.environ.get('PUBLIC_ORIGIN',f'http://127.0.0.1:{os.environ.get("PORT","8001")}').rstrip('/')
        parsed=urlsplit(origin)
        if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise RuntimeError('PUBLIC_ORIGIN must be an exact origin without a path, credentials, or query.')
        if parsed.scheme!='https' and parsed.hostname not in {'localhost','127.0.0.1'}:
            raise RuntimeError('Public deployments require an HTTPS PUBLIC_ORIGIN.')
        jobs=int(os.environ.get('MAX_CONCURRENT_JOBS','2'));daily=int(os.environ.get('DAILY_REQUEST_LIMIT','10'))
        if not 1<=jobs<=8 or daily<1:raise RuntimeError('Use 1–8 concurrent jobs and a positive daily request limit.')
        service=App(matcher.load_token(),db_path(),origin,jobs,daily)
    web=Flask(__name__,static_folder=None)
    web.config.update(MAX_CONTENT_LENGTH=60000)
    web.extensions['pocketmatch']=service

    def error(message,status):return jsonify(error=message),status

    @web.before_request
    def protect():
        if request.content_length and request.content_length>60000:return error('Request body is too large.',413)
        if request.host!=urlsplit(service.origin).netloc:return error('Unrecognized host. Use the configured PUBLIC_ORIGIN.',403)
        if request.method=='POST' and request.headers.get('Origin')!=service.origin:return error('Request origin rejected. Reload the app at its configured address.',403)
        g.account=None
        if request.path.startswith('/api/') and request.path not in {'/api/login'}:
            g.account=service.store.session(request.cookies.get(COOKIE,''))
            if not g.account:return error('Sign in to your account.',401)
            if request.method=='POST' and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),g.account['csrf']):
                return error('Session changed. Reload and try again.',403)

    @web.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if service.origin.startswith('https://'):response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @web.errorhandler(HTTPException)
    def http_error(exc):return error(exc.description,exc.code)

    @web.get('/')
    def index():return send_from_directory(STATIC,'index.html')

    @web.get('/healthz')
    def health():return jsonify(ok=True)

    @web.get('/<name>')
    def asset(name):
        if name not in {'app.js','styles.css','sw.js','manifest.webmanifest','icon-192.png','icon-512.png','catalogue-editor.js','catalogue-editor.css'}:return error('Not found.',404)
        return send_from_directory(STATIC,name)

    @web.post('/api/login')
    def login():
        data=request.get_json(silent=True)
        if not isinstance(data,dict):return error('Invalid request.',400)
        username=data.get('username','');password=data.get('password','')
        if not isinstance(username,str) or not isinstance(password,str) or not 1<=len(username)<=40 or not 1<=len(password)<=256:return error('Enter your username and password.',400)
        username=username.strip().lower()
        # Only trust client IP supplied by our explicitly configured single proxy.
        address=request.remote_addr or 'unknown'
        if os.environ.get('TRUST_CADDY_PROXY')=='1':
            address=request.headers.get('X-PocketMatch-Client-IP',address)
        if not service.store.login_allowed(address,username):return error('Too many sign-in attempts. Try again in five minutes.',429)
        user=service.store.authenticate(username,password,service.dummy_hash)
        if not user:return error('Incorrect username or password.',401)
        sid=service.store.new_session(user['id'])
        previous=request.cookies.get(COOKIE)
        if previous:service.store.revoke(previous)
        response=jsonify(ok=True)
        response.set_cookie(COOKIE,sid,max_age=7*86400,httponly=True,secure=service.origin.startswith('https://'),samesite='Strict',path='/')
        return response

    @web.get('/api/session')
    def session():return jsonify(csrf=g.account['csrf'],username=g.account['username'],daily_limit=service.daily_limit,is_owner=is_owner(service.store,g.account))

    @web.post('/api/logout')
    def logout():
        service.store.revoke(request.cookies.get(COOKIE,''))
        response=jsonify(ok=True);response.delete_cookie(COOKIE,path='/',secure=service.origin.startswith('https://'),httponly=True,samesite='Strict')
        return response

    @web.get('/api/catalogue')
    def catalogue():
        try:
            data=matcher.load_catalogue()
            return jsonify(count=len(data['products']),categories=sorted({p.get('category','Other') for p in data['products']}),model=matcher.MODEL)
        except (OSError,ValueError):return error('Catalogue is unavailable or invalid.',400)

    @web.get('/api/history')
    def history():return jsonify(requests=service.store.recent(g.account['user_id']))

    @web.get('/api/requests/<ident>')
    def detail(ident):
        item=service.store.get(ident,g.account['user_id'])
        return jsonify(item) if item else error('Request not found.',404)

    @web.post('/api/requests')
    def submit():
        data=request.get_json(silent=True);text=data.get('request') if isinstance(data,dict) else None
        if not isinstance(text,str) or not 1<=len(text.strip())<=12000:return error('Enter 1–12,000 characters.',400)
        try:ident=service.submit(g.account['user_id'],text.strip())
        except RuntimeError as exc:return error(str(exc),429)
        except (OSError,ValueError):return error('Cannot load the catalogue or save the request.',400)
        return jsonify(id=ident),202
    register_editor(web, service, matcher)
    return web


def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--create-user',metavar='USERNAME')
    group.add_argument('--reset-password',metavar='USERNAME')
    group.add_argument('--claim-legacy',metavar='USERNAME',help='Assign old shared history to this account')
    args=parser.parse_args()
    if args.create_user or args.reset_password or args.claim_legacy:
        store=History(db_path())
        try:
            if args.claim_legacy:
                print(f'Assigned {store.claim_legacy(args.claim_legacy)} old requests to {args.claim_legacy}.');return
            password=getpass.getpass('New account password (12+ characters): ')
            if password!=getpass.getpass('Repeat password: '):raise ValueError('Passwords did not match.')
            if args.create_user:store.add_user(args.create_user,password)
            else:store.reset_password(args.reset_password,password)
            print('Account saved. Password resets revoke existing sessions.')
        except ValueError as exc:raise SystemExit(str(exc)) from None
        return
    web=create_app()
    if urlsplit(web.extensions['pocketmatch'].origin).hostname not in {'localhost','127.0.0.1'}:
        raise SystemExit('Use the supplied Gunicorn/Docker deployment for public hosting. This command is for local development.')
    web.run(host='127.0.0.1',port=int(os.environ.get('PORT','8001')),debug=False,use_reloader=False)

if __name__=='__main__':main()
