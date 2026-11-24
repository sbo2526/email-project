import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, render_template, request, session

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
APP_SECRET = os.environ.get('APP_SECRET', '')
if len(ADMIN_PASSWORD) < 12 or len(APP_SECRET) < 32:
    raise RuntimeError('Set ADMIN_PASSWORD (at least 12 characters) and APP_SECRET (at least 32 characters).')

app = Flask(__name__)
app.secret_key = APP_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('COOKIE_SECURE') == '1'),
                  MAX_CONTENT_LENGTH=16384)
cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET.encode()).digest()))
DATA_DIR = Path(os.environ.get('DATA_DIR', './data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'mailboxes.sqlite3'
API = 'https://api.mail.tm'
login_attempts = defaultdict(deque)
attempt_lock = Lock()


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS mailboxes (
          id INTEGER PRIMARY KEY, address TEXT NOT NULL UNIQUE,
          password_encrypted BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')

init_db()


class ProviderError(Exception):
    def __init__(self, message, status=502):
        self.message, self.status = message, status


def api(method, path, payload=None, token=None):
    headers = {'Accept': 'application/ld+json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        response = requests.request(method, API + path, json=payload, headers=headers, timeout=15, allow_redirects=False)
    except requests.RequestException:
        raise ProviderError('ارتباط با سرویس ایمیل برقرار نشد. دوباره تلاش کنید.')
    if response.status_code == 429:
        raise ProviderError('سرویس محدودیت درخواست دارد. کمی صبر کنید و دوباره تلاش کنید.', 429)
    if not response.ok:
        if response.status_code in (400, 422) and path == '/accounts':
            raise ProviderError('ساخت ایمیل ناموفق بود؛ ممکن است این نام قبلاً گرفته شده باشد. نام دیگری انتخاب کنید.', 400)
        if response.status_code in (401, 404):
            raise ProviderError('حساب یا پیام در سرویس ایمیل پیدا نشد؛ ممکن است منقضی یا حذف شده باشد.', 404)
        raise ProviderError('سرویس ایمیل درخواست را نپذیرفت. بعداً دوباره تلاش کنید.')
    try:
        return response.json()
    except ValueError:
        raise ProviderError('پاسخ سرویس ایمیل قابل خواندن نیست.')


@app.errorhandler(ProviderError)
def provider_error(error):
    return jsonify(error=error.message), error.status


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    return response


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


@app.before_request
def protect():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        expected = session.get('csrf', '')
        actual = request.headers.get('X-CSRF-Token', '')
        if not expected or not hmac.compare_digest(expected, actual):
            return jsonify(error='نشست نامعتبر است؛ صفحه را تازه‌سازی کنید.'), 403
    if request.path.startswith('/api/') and request.path not in ('/api/state', '/api/login') and not session.get('logged_in'):
        return jsonify(error='ابتدا وارد شوید.'), 401


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/health')
def health():
    return 'ok', 200


@app.get('/api/state')
def state():
    return jsonify(authenticated=bool(session.get('logged_in')), csrf=csrf_token())


@app.post('/api/login')
def login():
    # A lightweight per-process throttle, intended for a single replica.
    ip = request.remote_addr or 'unknown'
    now = time.monotonic()
    with attempt_lock:
        q = login_attempts[ip]
        while q and q[0] < now - 600:
            q.popleft()
        if len(q) >= 5:
            return jsonify(error='تلاش‌های ناموفق زیاد است؛ ۱۰ دقیقه بعد امتحان کنید.'), 429
        password = (request.get_json(silent=True) or {}).get('password', '')
        if not isinstance(password, str) or not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            q.append(now)
            return jsonify(error='رمز ورود اشتباه است.'), 401
        q.clear()
    session.clear()
    session['logged_in'] = True
    return jsonify(ok=True, csrf=csrf_token())


@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify(ok=True)


def mailbox(mailbox_id):
    with db() as con:
        row = con.execute('SELECT * FROM mailboxes WHERE id=?', (mailbox_id,)).fetchone()
    if row is None:
        raise ProviderError('ایمیل در فهرست پیدا نشد.', 404)
    return row


def password_for(row):
    try:
        return cipher.decrypt(row['password_encrypted']).decode()
    except (InvalidToken, ValueError):
        raise ProviderError('رمز قابل بازیابی نیست؛ مقدار APP_SECRET باید همان مقدار زمان ساخت ایمیل باشد.', 500)


def token_for(row):
    result = api('POST', '/token', {'address': row['address'], 'password': password_for(row)})
    token = result.get('token')
    if not isinstance(token, str) or not token:
        raise ProviderError('سرویس ایمیل توکن معتبری برنگرداند.')
    return token


@app.get('/api/mailboxes')
def list_mailboxes():
    with db() as con:
        rows = con.execute('SELECT id,address,created_at FROM mailboxes ORDER BY id DESC').fetchall()
    return jsonify([dict(row) for row in rows])


@app.post('/api/mailboxes')
def create_mailbox():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '')
    if not isinstance(username, str):
        return jsonify(error='نام ایمیل معتبر نیست.'), 400
    username = username.strip().lower()
    if username and not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,23}', username):
        return jsonify(error='نام باید ۳ تا ۲۴ کاراکتر و شامل حروف انگلیسی، عدد، نقطه، خط تیره یا زیرخط باشد.'), 400
    if not username:
        username = 'mail' + secrets.token_hex(6)
    domains = api('GET', '/domains')
    members = domains.get('hydra:member', domains.get('member', []))
    available = [x.get('domain') for x in members if x.get('isActive') and not x.get('isPrivate') and x.get('domain')]
    if not available:
        raise ProviderError('در حال حاضر دامنه‌ای از طرف سرویس در دسترس نیست.')
    address = username + '@' + available[0]
    password = secrets.token_urlsafe(24)
    api('POST', '/accounts', {'address': address, 'password': password})
    # Record credentials immediately after creation; do not require a second remote call.
    try:
        with db() as con:
            cursor = con.execute('INSERT INTO mailboxes(address,password_encrypted) VALUES (?,?)',
                                 (address, cipher.encrypt(password.encode())))
            new_id = cursor.lastrowid
    except sqlite3.Error:
        # In case the remote account exists but local storage failed, return credentials
        # so the operator can recover the account manually instead of losing access.
        return jsonify(error='ایمیل ساخته شد اما ذخیره‌سازی محلی ناموفق بود. این اطلاعات را فوراً ذخیره کنید.',
                       recovery={'address': address, 'password': password}), 500
    return jsonify(id=new_id, address=address), 201


@app.get('/api/mailboxes/<int:mailbox_id>/credentials')
def credentials(mailbox_id):
    row = mailbox(mailbox_id)
    return jsonify(address=row['address'], password=password_for(row))


@app.delete('/api/mailboxes/<int:mailbox_id>')
def delete_mailbox(mailbox_id):
    mailbox(mailbox_id)
    with db() as con:
        con.execute('DELETE FROM mailboxes WHERE id=?', (mailbox_id,))
    return jsonify(ok=True)


@app.get('/api/mailboxes/<int:mailbox_id>/messages')
def messages(mailbox_id):
    page = request.args.get('page', '1')
    if not page.isdecimal() or not 1 <= int(page) <= 10000:
        return jsonify(error='شماره صفحه نامعتبر است.'), 400
    result = api('GET', '/messages?page=' + str(int(page)), token=token_for(mailbox(mailbox_id)))
    members = result.get('hydra:member', result.get('member', []))
    total = result.get('hydra:totalItems', result.get('totalItems'))
    return jsonify(messages=[{'id': m.get('id'), 'subject': m.get('subject') or '(بدون موضوع)',
                              'from': (m.get('from') or {}).get('address', ''),
                              'intro': m.get('intro', ''), 'createdAt': m.get('createdAt', ''),
                              'seen': m.get('seen', False)} for m in members], total=total,
                   hasNext=bool((result.get('hydra:view') or result.get('view') or {}).get('hydra:next')))


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'tr', 'li'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


@app.get('/api/mailboxes/<int:mailbox_id>/messages/<message_id>')
def message_detail(mailbox_id, message_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', message_id):
        return jsonify(error='شناسه پیام نامعتبر است.'), 400
    msg = api('GET', '/messages/' + message_id, token=token_for(mailbox(mailbox_id)))
    body = msg.get('text') or ''
    if not body and msg.get('html'):
        parser = TextExtractor()
        html = msg['html']
        parser.feed('\n'.join(html) if isinstance(html, list) else str(html))
        body = ''.join(parser.parts).strip()
    return jsonify(subject=msg.get('subject') or '(بدون موضوع)',
                   sender=(msg.get('from') or {}).get('address', ''),
                   date=msg.get('createdAt', ''), text=body or '(متن پیام موجود نیست)',
                   hasAttachments=bool(msg.get('attachments')))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
