"""Persistent requests and results. SQLite is part of Python's standard library."""
import json
import sqlite3
import uuid
import hashlib
import secrets
import time
import re
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                request TEXT NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL,
                catalogue_json TEXT NOT NULL, assessments_json TEXT NOT NULL DEFAULT '[]',
                usage_json TEXT NOT NULL DEFAULT '[]', error TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS requests_created ON requests(created_at)')
            if 'user_id' not in {r['name'] for r in db.execute('PRAGMA table_info(requests)')}:
                db.execute('ALTER TABLE requests ADD COLUMN user_id TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS requests_user ON requests(user_id,created_at)')
            db.execute('''CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                csrf TEXT NOT NULL, expires REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS login_attempts (
                bucket TEXT NOT NULL, attempted_at REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS login_rate ON login_attempts(bucket,attempted_at)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE requests SET status='interrupted', error=?, updated_at=? WHERE status IN ('queued','running')",
                       ('The server restarted before this assessment finished. Saved results remain available. No automatic retry was made.', now()))

    def create(self, request, model, catalogue, user_id):
        ident = str(uuid.uuid4())
        stamp = now()
        with self.connect() as db:
            db.execute('INSERT INTO requests (id,created_at,updated_at,request,status,model,catalogue_json,user_id) VALUES (?,?,?,?,?,?,?,?)',
                       (ident, stamp, stamp, request, 'queued', model, json.dumps(catalogue, ensure_ascii=False),user_id))
        return ident

    def set_status(self, ident, status, error=None):
        if status not in {'queued','running','complete','failed','interrupted'}:
            raise ValueError('Invalid job status')
        with self.connect() as db:
            db.execute('UPDATE requests SET status=?, error=?, updated_at=? WHERE id=?', (status,error,now(),ident))

    def append(self, ident, assessments, usage):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT assessments_json,usage_json FROM requests WHERE id=?',(ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            results=json.loads(row['assessments_json'])+assessments
            calls=json.loads(row['usage_json'])+[usage]
            db.execute('UPDATE requests SET assessments_json=?,usage_json=?,updated_at=? WHERE id=?',
                       (json.dumps(results,ensure_ascii=False),json.dumps(calls),now(),ident))

    def get(self, ident, user_id, include_catalogue=False):
        with self.connect() as db:
            row=db.execute('SELECT * FROM requests WHERE id=? AND user_id=?',(ident,user_id)).fetchone()
        if row is None:
            return None
        data=dict(row)
        catalogue=json.loads(data.pop('catalogue_json'))
        data['total']=len(catalogue['products'])
        data['assessments']=json.loads(data.pop('assessments_json'))
        data['usage']=json.loads(data.pop('usage_json'))
        data['complete']=data['status']=='complete'
        if include_catalogue:
            data['catalogue']=catalogue
        return data

    def recent(self, user_id, limit=100):
        with self.connect() as db:
            rows=db.execute('SELECT id,created_at,updated_at,request,status,model,error FROM requests WHERE user_id=? ORDER BY created_at DESC LIMIT ?', (user_id,limit)).fetchall()
        return [dict(row) for row in rows]

    def add_user(self, username, password):
        username=username.strip().lower()
        if not re.fullmatch(r'[a-z0-9_.-]{3,40}',username):
            raise ValueError('Username must be 3–40 characters: letters, numbers, dot, dash or underscore.')
        if not 12 <= len(password) <= 256:
            raise ValueError('Password must contain 12–256 characters.')
        ident=str(uuid.uuid4())
        hashed=generate_password_hash(password,method='scrypt')
        try:
            with self.connect() as db:
                db.execute('INSERT INTO users VALUES (?,?,?,?)',(ident,username,hashed,now()))
        except sqlite3.IntegrityError:
            raise ValueError('That username already exists.') from None
        return ident

    def reset_password(self, username, password):
        if not 12<=len(password)<=256:raise ValueError('Password must contain 12–256 characters.')
        hashed=generate_password_hash(password,method='scrypt')
        with self.connect() as db:
            row=db.execute('SELECT id FROM users WHERE username=?',(username.strip().lower(),)).fetchone()
            if not row:raise ValueError('User not found.')
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(hashed,row['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(row['id'],))

    def login_allowed(self, address, username):
        buckets=['ip:'+address,'user:'+username]
        current=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_attempts WHERE attempted_at<?',(current-300,))
            for bucket,limit in zip(buckets,[30,10]):
                count=db.execute('SELECT count(*) FROM login_attempts WHERE bucket=?',(bucket,)).fetchone()[0]
                if count>=limit:return False
            db.executemany('INSERT INTO login_attempts VALUES (?,?)',[(b,current) for b in buckets])
        return True

    def authenticate(self, username, password, dummy_hash):
        with self.connect() as db:
            row=db.execute('SELECT * FROM users WHERE username=?',(username.strip().lower(),)).fetchone()
        valid=check_password_hash(row['password_hash'] if row else dummy_hash,password)
        return dict(row) if row and valid else None

    def new_session(self, user_id):
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,csrf,time.time()+7*86400))
        return token

    def session(self, token):
        with self.connect() as db:
            row=db.execute('''SELECT s.user_id,s.csrf,s.expires,u.username FROM sessions s
                JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?''',
                (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return dict(row) if row else None

    def revoke(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

    def submissions_today(self, user_id):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM requests WHERE user_id=? AND created_at>=?',
                              (user_id,now()[:10])).fetchone()[0]

    def claim_legacy(self, username):
        with self.connect() as db:
            user=db.execute('SELECT id FROM users WHERE username=?',(username.strip().lower(),)).fetchone()
            if not user:raise ValueError('User not found.')
            return db.execute('UPDATE requests SET user_id=? WHERE user_id IS NULL',(user['id'],)).rowcount
