import os
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from functools import wraps
import time
import re
import mysql.connector
from mysql.connector import Error, pooling
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, abort, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import requests
import certifi
import json
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import cloudinary
import cloudinary.uploader

try:
    from flask_caching import Cache
    HAS_CACHE = True
except ImportError:
    HAS_CACHE = False
    Cache = None

load_dotenv()
print("DB_SSL_DISABLED raw value:", repr(os.getenv('DB_SSL_DISABLED')))

app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-secret-key-in-production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['JSON_SORT_KEYS'] = False  # small perf win on JSON responses

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# ---- Cache ----
# IMPORTANT: SimpleCache lives in one process's memory. If you run gunicorn
# with more than 1 worker (you should, for real traffic), each worker gets
# its OWN copy of this cache — a "cached" homepage still gets hit N times
# (once per worker) instead of once. Set REDIS_URL to share one cache across
# all workers; this is the single biggest lever for surviving a traffic spike
# on one video, since every viewer of that video hits the same cache keys.
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

if HAS_CACHE:
    cache = Cache(app, config={
        'CACHE_TYPE': 'RedisCache',
        'CACHE_REDIS_URL': REDIS_URL,
        'CACHE_DEFAULT_TIMEOUT': 60,
        'CACHE_KEY_PREFIX': 'kss:',
    })
    print(f"Cache: Redis ({REDIS_URL})")
else:
    cache = None

    

def cached_fetch(key, timeout, loader):
    """Get-or-set helper: read `key` from cache, or run `loader()` and store
    the result. Centralizes the pattern already used on the homepage so the
    same caching can be applied to hot per-video pages without repeating
    the get/None-check/set boilerplate everywhere."""
    if not cache:
        return loader()
    value = cache.get(key)
    if value is None:
        value = loader()
        cache.set(key, value, timeout=timeout)
    return value


# ============================================================
# DATABASE (pooled)
# ============================================================

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = int(os.getenv('DB_PORT', '3306'))
DB_USER = os.getenv('DB_USER', 'root')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'kss')

# Cloud MySQL providers (PlanetScale, Aiven, Railway, DO) require TLS.
# Set DB_SSL_DISABLED=1 only for a DB on the same private network.
DB_SSL_DISABLED = os.getenv('DB_SSL_DISABLED', '0') == '1'

DB_CONFIG = {
    'host': DB_HOST,
    'port': DB_PORT,
    'user': DB_USER,
    'password': DB_PASSWORD,
    'database': DB_NAME,
    'connection_timeout': 8,      # fail fast instead of hanging
    'autocommit': False,
    'use_pure': True,             # pure-Python path; C extension has 3.14 bugs
}

if not DB_SSL_DISABLED:
    # Point the connector at certifi's CA bundle. On Linux containers,
    # the default system CA store may not exist, causing the connector
    # to hang or raise during SSL setup.
    DB_CONFIG['ssl_ca'] = certifi.where()
    DB_CONFIG['ssl_verify_cert'] = True



# Tune to MySQL max_connections minus what admin tools need.
#
# Each gunicorn WORKER process gets its own pool of this size. So total
# connections opened = DB_POOL_SIZE * number of gunicorn workers. With
# `gunicorn -w 4 --threads 8`, a pool of 10 per worker = 40 connections,
# and with 8 threads sharing 10 connections per worker, threads will queue
# for a connection under load. As a rule of thumb, set DB_POOL_SIZE close
# to your per-worker thread count, and make sure
# DB_POOL_SIZE * workers < your MySQL plan's max_connections (leave ~20%
# headroom for admin/migration connections).
DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '20'))

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="kss_pool",
            pool_size=DB_POOL_SIZE,
            pool_reset_session=True,
            **DB_CONFIG,
        )
    return _pool


def init_pool():
    """Warm the pool at import time so the first request doesn't hang."""
    try:
        get_pool()
        print(f"DB pool ready ({DB_POOL_SIZE} connections).")
    except Error as e:
        # Don't crash the app if the DB is briefly unreachable — let
        # requests retry and surface the error where it's visible.
        print(f"DB pool warmup failed: {e}")


def get_db_connection():
    """Return a pooled connection (or None on failure)."""
    try:
        conn = get_pool().get_connection()
        # Lightweight liveness probe — cheap COM_PING (~0.1ms).
        # If the connection is dead, drop it back and get another.
        try:
            conn.ping(reconnect=False, attempts=1, delay=0)
        except Error:
            try:
                conn.close()
            except Exception:
                pass
            conn = get_pool().get_connection()
        return conn
    except Error as e:
        print(f'Database pool error: {e}')
        return None


def _safe_close(cursor, connection):
    if cursor:
        try:
            cursor.close()
        except Exception:
            pass
    if connection:
        try:
            connection.close()   # returns to the pool
        except Exception:
            pass


def fetch_one(query, params=None):
    connection = get_db_connection()
    if not connection:
        return None
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(query, params or ())
        return cursor.fetchone()
    except Error as e:
        print(f'Database fetch_one error: {e}')
        return None
    finally:
        _safe_close(cursor, connection)


def fetch_all(query, params=None):
    connection = get_db_connection()
    if not connection:
        return []
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(query, params or ())
        return cursor.fetchall()
    except Error as e:
        print(f'Database fetch_all error: {e}')
        return []
    finally:
        _safe_close(cursor, connection)


def execute_query(query, params=None, return_lastrowid=False):
    connection = get_db_connection()
    if not connection:
        return None
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(query, params or ())
        connection.commit()
        if return_lastrowid:
            return cursor.lastrowid
        return True
    except Error as e:
        try:
            connection.rollback()
        except Exception:
            pass
        print(f'Database execute error: {e}')
        return None
    finally:
        _safe_close(cursor, connection)


# ============================================================
# BUNNY HELPERS
# ============================================================

BUNNY_API_BASE = 'https://video.bunnycdn.com'
BUNNY_TUS_ENDPOINT = 'https://video.bunnycdn.com/tusupload'
BUNNY_API_KEY = os.getenv('BUNNY_API_KEY', '')
BUNNY_LIBRARY_ID = os.getenv('BUNNY_LIBRARY_ID', '')
BUNNY_CDN_HOSTNAME = os.getenv('BUNNY_CDN_HOSTNAME', '')

PAYSTACK_SECRET_KEY = os.getenv('PAYSTACK_SECRET_KEY', '')
PAYSTACK_PUBLIC_KEY = os.getenv('PAYSTACK_PUBLIC_KEY', '')

VIDEO_RENTAL_DURATION_HOURS = 48

CHANNEL_SUBSCRIPTION_PRICE = float(
    os.getenv('KSS_CHANNEL_SUBSCRIPTION_PRICE', '50.00')
)
CHANNEL_SUBSCRIPTION_DAYS = int(
    os.getenv('KSS_CHANNEL_SUBSCRIPTION_DAYS', '30')
)


# ---- Concurrent session limits ----
MAX_CONCURRENT_SESSIONS = int(os.getenv('KSS_MAX_SESSIONS', '3'))
SESSION_IDLE_DAYS = int(os.getenv('KSS_SESSION_IDLE_DAYS', '14'))
SESSION_RECHECK_SECONDS = int(os.getenv('KSS_SESSION_RECHECK_SECONDS', '300'))


def bunny_headers():
    return {'AccessKey': BUNNY_API_KEY, 'Content-Type': 'application/json'}


def bunny_create_video(title):
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not BUNNY_LIBRARY_ID:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')
    url = f'{BUNNY_API_BASE}/library/{BUNNY_LIBRARY_ID}/videos'
    s = requests.Session()
    s.trust_env = False
    response = s.post(url, headers=bunny_headers(),
                      json={'title': title}, timeout=30,
                      verify=certifi.where())
    if not response.ok:
        raise RuntimeError(
            f'Bunny video creation failed: {response.status_code} {response.text}'
        )
    return response.json()


def bunny_get_video(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not library_id:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')
    url = f'{BUNNY_API_BASE}/library/{library_id}/videos/{video_id}'
    response = requests.get(url, headers={'AccessKey': BUNNY_API_KEY},
                            timeout=30, verify=certifi.where())
    if not response.ok:
        raise RuntimeError(
            f'Bunny video lookup failed: {response.status_code} {response.text}'
        )
    return response.json()


def bunny_update_video(video_id, title, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not library_id:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')
    url = f'{BUNNY_API_BASE}/library/{library_id}/videos/{video_id}'
    response = requests.post(url, headers=bunny_headers(),
                             json={'title': title}, timeout=30,
                             verify=certifi.where())
    if not response.ok:
        raise RuntimeError(
            f'Bunny video update failed: {response.status_code} {response.text}'
        )
    return response.json()


def bunny_delete_video(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    url = f'{BUNNY_API_BASE}/library/{library_id}/videos/{video_id}'
    response = requests.delete(url, headers={'AccessKey': BUNNY_API_KEY},
                               timeout=30, verify=certifi.where())
    if not response.ok:
        raise RuntimeError(
            f'Bunny video deletion failed: {response.status_code} {response.text}'
        )
    return True


def bunny_create_upload_signature(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not library_id:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')
    expiration_time = int(time.time()) + 3600
    signature_string = (str(library_id) + str(BUNNY_API_KEY) +
                        str(expiration_time) + str(video_id))
    signature = hashlib.sha256(signature_string.encode('utf-8')).hexdigest()
    return {
        'tus_endpoint': BUNNY_TUS_ENDPOINT,
        'library_id': str(library_id),
        'bunny_video_id': str(video_id),
        'signature': signature,
        'expiration': expiration_time,
    }


def bunny_status_to_kss_status(bunny_status):
    status_map = {
        0: 'processing', 1: 'processing', 2: 'processing',
        3: 'processing', 4: 'ready', 5: 'failed', 6: 'failed',
    }
    return status_map.get(bunny_status, 'processing')


# ============================================================
# PAYSTACK HELPERS
# ============================================================

def paystack_headers():
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def money_to_subunit(amount):
    try:
        value = Decimal(str(amount)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Payment amount is invalid.')
    if value <= 0:
        raise ValueError('Payment amount must be greater than zero.')
    return int(value * 100)


def generate_payment_reference(payment_type, user_id):
    short_type = {
        "video_purchase": "PUR",
        "video_rental": "REN",
        "channel_subscription": "SUB",
    }.get(payment_type, "PAY")
    return f"KSS-{short_type}-{user_id}-{uuid.uuid4().hex[:12].upper()}"


def paystack_initialize(email, amount, currency, reference, metadata):
    if not PAYSTACK_SECRET_KEY:
        raise Exception("PAYSTACK_SECRET_KEY is not configured.")
    payload = {
        "email": email,
        "amount": money_to_subunit(amount),
        "currency": currency,
        "reference": reference,
        "callback_url": url_for("payment_callback", _external=True),
        "metadata": metadata,
    }
    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers=paystack_headers(), json=payload, timeout=30
    )
    if not response.ok:
        raise Exception(
            f"Paystack initialization failed: {response.status_code} {response.text}"
        )
    result = response.json()
    if not result.get("status"):
        raise Exception(result.get("message", "Unable to initialize payment."))
    return result["data"]


def is_valid_paystack_webhook():
    signature = request.headers.get('X-Paystack-Signature', '')
    if not PAYSTACK_SECRET_KEY or not signature:
        return False
    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode('utf-8'),
        request.get_data(),
        hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def paystack_verify(reference):
    if not PAYSTACK_SECRET_KEY:
        raise Exception("PAYSTACK_SECRET_KEY is not configured.")
    response = requests.get(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers=paystack_headers(), timeout=30
    )
    if not response.ok:
        raise Exception(
            f"Paystack verification failed: {response.status_code} {response.text}"
        )
    return response.json()


# ============================================================
# AUTH / USER HELPERS
# ============================================================


def _hash_ip(ip):
    return hashlib.sha256((ip or '').encode('utf-8')).hexdigest()


def _describe_device(user_agent):
    if not user_agent:
        return 'Unknown device'
    ua = user_agent.lower()
    if 'iphone' in ua:
        return 'iPhone'
    if 'ipad' in ua:
        return 'iPad'
    if 'android' in ua:
        return 'Android device'
    if 'windows' in ua:
        return 'Windows PC'
    if 'mac os' in ua or 'macintosh' in ua:
        return 'Mac'
    if 'linux' in ua:
        return 'Linux PC'
    return 'Unknown device'


def create_session_row(user_id):
    token = secrets.token_urlsafe(32)
    ua = (request.headers.get('User-Agent') or '')[:500]
    ip_hash = _hash_ip(request.remote_addr or '')
    execute_query("""
        INSERT INTO user_sessions
            (session_token, user_id, ip_hash, user_agent,
             created_at, last_seen_at)
        VALUES (%s, %s, %s, %s, NOW(), NOW())
    """, (token, user_id, ip_hash, ua))
    return token


def delete_session_row(token):
    if not token:
        return
    execute_query(
        'DELETE FROM user_sessions WHERE session_token = %s',
        (token,)
    )


def delete_all_sessions_for_user(user_id):
    execute_query(
        'DELETE FROM user_sessions WHERE user_id = %s',
        (user_id,)
    )


def get_active_sessions(user_id):
    return fetch_all("""
        SELECT session_token, ip_hash, user_agent,
               created_at, last_seen_at
        FROM user_sessions
        WHERE user_id = %s
          AND last_seen_at > NOW() - INTERVAL %s DAY
        ORDER BY last_seen_at DESC
    """, (user_id, SESSION_IDLE_DAYS))


def purge_stale_sessions(user_id):
    execute_query("""
        DELETE FROM user_sessions
        WHERE user_id = %s
          AND last_seen_at < NOW() - INTERVAL %s DAY
    """, (user_id, SESSION_IDLE_DAYS))


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)


def get_current_user():
    """Return the logged-in user, cached for the lifetime of the request.

    ``before_request`` and ``context_processor`` both need the user; caching
    on ``g`` collapses that to a single query per request.
    """
    if hasattr(g, '_kss_current_user'):
        return g._kss_current_user

    user_id = session.get('user_id')
    if not user_id:
        g._kss_current_user = None
        return None

    user = fetch_one("""
        SELECT
            user_id, full_name, username, email, phone,
            profile_image, role, is_active,
            email_verified, phone_verified, last_login_at, created_at
        FROM users
        WHERE user_id = %s
        LIMIT 1
    """, (user_id,))
    g._kss_current_user = user
    return user


def login_user(user):
    session.clear()
    session.permanent = True
    session['user_id'] = user['user_id']
    session['role'] = user['role']
    session['username'] = user['username']
    session['email'] = user['email']

    # Register this login as an active DB session
    token = create_session_row(user['user_id'])
    session['session_token'] = token
    session['session_validated_at'] = int(time.time())


def logout_user():
    token = session.get('session_token')
    delete_session_row(token)
    session.clear()


def is_logged_in():
    return 'user_id' in session


def is_admin():
    return session.get('role') == 'admin'


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not is_logged_in():
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login', next=request.url))
        user = get_current_user()
        if not user or not user['is_active']:
            logout_user()
            flash('Your account is inactive.', 'danger')
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not is_logged_in():
            flash('Please log in as an administrator.', 'warning')
            return redirect(url_for('login'))
        user = get_current_user()
        if not user:
            logout_user()
            return redirect(url_for('login'))
        if user['role'] != 'admin':
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('index'))
        if not user['is_active']:
            logout_user()
            flash('Your account is inactive.', 'danger')
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped_view


# ============================================================
# ACCESS CONTROL (single-query)
# ============================================================

_ACCESS_SQL = """
    SELECT
        (SELECT role FROM users WHERE user_id = %s) AS role,
        (SELECT 1 FROM video_purchases
           WHERE user_id = %s AND video_id = %s AND status = 'paid'
           LIMIT 1) AS purchased,
        (SELECT 1 FROM video_rentals
           WHERE user_id = %s AND video_id = %s AND status = 'active'
             AND expires_at > NOW()
           LIMIT 1) AS rented,
        (SELECT 1 FROM channel_subscriptions
           WHERE user_id = %s AND channel_id = %s AND status = 'active'
             AND expires_at > NOW()
             AND started_at <= %s AND expires_at >= %s
           LIMIT 1) AS subscribed
"""


def _access_params(user_id, video):
    return (
        user_id,
        user_id, video['video_id'],
        user_id, video['video_id'],
        user_id, video.get('channel_id'),
        video.get('published_at'), video.get('published_at'),
    )


def has_video_access(user_id, video):
    if not video:
        return False
    if video.get('is_free'):
        return True
    if not user_id:
        return False

    row = fetch_one(_ACCESS_SQL, _access_params(user_id, video))
    if not row:
        return False
    if row.get('role') == 'admin':
        return True
    if row.get('purchased'):
        return True
    if row.get('rented'):
        return True
    if video.get('subscription_enabled') and row.get('subscribed'):
        return True
    return False


def get_video_access_type(user_id, video):
    if not video:
        return None
    if video.get('is_free'):
        return 'free'
    if not user_id:
        return None

    row = fetch_one(_ACCESS_SQL, _access_params(user_id, video))
    if not row:
        return None
    if row.get('role') == 'admin':
        return 'admin'
    if row.get('purchased'):
        return 'purchase'
    if row.get('rented'):
        return 'rental'
    if video.get('subscription_enabled') and row.get('subscribed'):
        return 'subscription'
    return None


def annotate_video_lock_status(videos, user_id):
    """Add an ``is_locked`` field to video cards without doing one query/card."""
    if not videos:
        return videos

    paid_ids = set()
    protected_ids = [v['video_id'] for v in videos if not v.get('is_free')]

    if user_id and protected_ids:
        user = fetch_one(
            'SELECT role FROM users WHERE user_id = %s LIMIT 1', (user_id,)
        )
        if user and user.get('role') == 'admin':
            paid_ids.update(protected_ids)
        else:
            placeholders = ', '.join(['%s'] * len(protected_ids))
            purchased = fetch_all(
                f'''SELECT video_id FROM video_purchases
                    WHERE user_id = %s AND status = 'paid'
                      AND video_id IN ({placeholders})''',
                tuple([user_id] + protected_ids)
            )
            rentals = fetch_all(
                f'''SELECT video_id FROM video_rentals
                    WHERE user_id = %s AND status = 'active'
                      AND expires_at > NOW()
                      AND video_id IN ({placeholders})''',
                tuple([user_id] + protected_ids)
            )
            subscriptions = fetch_all(
                f'''SELECT v.video_id FROM videos v
                    JOIN channel_subscriptions cs
                      ON cs.channel_id = v.channel_id
                    WHERE cs.user_id = %s
                      AND cs.status = 'active'
                      AND cs.expires_at > NOW()
                      AND v.subscription_enabled = 1
                      AND cs.started_at <= v.published_at
                      AND cs.expires_at >= v.published_at
                      AND v.video_id IN ({placeholders})''',
                tuple([user_id] + protected_ids)
            )
            paid_ids.update(row['video_id'] for row in purchased)
            paid_ids.update(row['video_id'] for row in rentals)
            paid_ids.update(row['video_id'] for row in subscriptions)

    for video in videos:
        video['is_locked'] = (
            not bool(video.get('is_free'))
            and video['video_id'] not in paid_ids
        )
    return videos


def annotate_purchase_status(videos, user_id):
    """Mark each video with is_purchased so the template can hide
    the price tag for content the user already owns."""
    if not videos:
        return

    if not user_id:
        for v in videos:
            v['is_purchased'] = False
        return

    video_ids = [v['video_id'] for v in videos]
    placeholders = ','.join(['%s'] * len(video_ids))

    # FIX: was querying a table called "purchases" that doesn't exist.
    # The real table is video_purchases and successful rows use status 'paid'.
    purchased_rows = fetch_all(f"""
        SELECT video_id
        FROM video_purchases
        WHERE user_id = %s
          AND video_id IN ({placeholders})
          AND status = 'paid'
    """, [user_id, *video_ids])

    purchased_ids = {row['video_id'] for row in purchased_rows}
    for v in videos:
        v['is_purchased'] = v['video_id'] in purchased_ids


def generate_video_slug(title, video_id=None):
    base_slug = re.sub('[^a-z0-9]+', '-', title.lower().strip()).strip('-')
    if not base_slug:
        base_slug = 'video'
    slug = base_slug
    counter = 2
    while True:
        if video_id:
            existing = fetch_one(
                'SELECT video_id FROM videos WHERE slug = %s AND video_id <> %s LIMIT 1',
                (slug, video_id)
            )
        else:
            existing = fetch_one(
                'SELECT video_id FROM videos WHERE slug = %s LIMIT 1',
                (slug,)
            )
        if not existing:
            return slug
        slug = f'{base_slug}-{counter}'
        counter += 1


def save_video_tags(video_id, tags_string, connection):
    """Save tags and video_tags relationships using an existing connection."""
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            'DELETE FROM video_tags WHERE video_id = %s', (video_id,)
        )
        if not tags_string:
            return
        raw_tags = tags_string.split(',')
        tags = []
        for tag in raw_tags:
            tag = tag.strip()
            if not tag:
                continue
            tag = tag[:100]
            if tag.lower() not in [existing.lower() for existing in tags]:
                tags.append(tag)
        for tag_name in tags:
            tag_slug = re.sub('[^a-z0-9]+', '-', tag_name.lower().strip()).strip('-')
            if not tag_slug:
                continue
            cursor.execute(
                'SELECT tag_id FROM tags WHERE slug = %s LIMIT 1', (tag_slug,)
            )
            tag_row = cursor.fetchone()
            if tag_row:
                tag_id = tag_row['tag_id']
            else:
                cursor.execute(
                    '''INSERT INTO tags (tag_name, slug, created_at)
                       VALUES (%s, %s, NOW())''',
                    (tag_name, tag_slug)
                )
                tag_id = cursor.lastrowid
            cursor.execute(
                '''INSERT IGNORE INTO video_tags (video_id, tag_id)
                   VALUES (%s, %s)''',
                (video_id, tag_id)
            )
    finally:
        cursor.close()


# ============================================================
# ERROR HANDLERS & REQUEST HOOKS
# ============================================================

@app.errorhandler(404)
def page_not_found(error):
    return (render_template('404.html'), 404)


@app.errorhandler(500)
def internal_server_error(error):
    return (render_template('404.html'), 500)


@app.before_request
def load_logged_in_user():
    user_id = session.get('user_id')
    if not user_id:
        return

    # Only re-validate the DB session once every SESSION_RECHECK_SECONDS.
    # Between checks, trust the Flask cookie. This keeps the cost near zero
    # even at thousands of concurrent users.
    now_ts = int(time.time())
    last_check = session.get('session_validated_at', 0)

    if now_ts - last_check > SESSION_RECHECK_SECONDS:
        token = session.get('session_token')
        if token:
            row = fetch_one("""
                SELECT 1 FROM user_sessions
                WHERE session_token = %s
                  AND last_seen_at > NOW() - INTERVAL %s DAY
                LIMIT 1
            """, (token, SESSION_IDLE_DAYS))

            if not row:
                # Session was revoked elsewhere. Log out silently.
                session.clear()
                g._kss_current_user = None
                return

            execute_query("""
                UPDATE user_sessions
                SET last_seen_at = NOW()
                WHERE session_token = %s
            """, (token,))

        session['session_validated_at'] = now_ts
        session.modified = True

    user = get_current_user()
    if user and not user.get('is_active'):
        session.clear()
        g._kss_current_user = None


@app.context_processor
def inject_global_variables():
    user = get_current_user()
    return {
        'current_user': user,
        'logged_in': bool(user),
        'current_year': datetime.now().year,
    }


# ============================================================
# TEMPLATE FILTERS
# ============================================================

@app.template_filter("duration")
def format_duration(seconds):
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@app.template_filter("views")
def format_views(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


# ============================================================
# ADMIN — BUNNY
# ============================================================

@app.route('/admin/api/bunny/create-video', methods=['POST'])
@admin_required
def admin_bunny_create_video():
    data = request.get_json(silent=True) or {}
    title = data.get('title', '').strip()
    video_id = data.get('video_id')
    if not title:
        return jsonify({'success': False, 'message': 'Video title is required.'}), 400
    if not video_id:
        return jsonify({'success': False, 'message': 'KSS video ID is required.'}), 400
    try:
        video_id = int(video_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid KSS video ID.'}), 400

    video = fetch_one("""
        SELECT video_id, title, bunny_video_id
        FROM videos
        WHERE video_id = %s AND status <> 'deleted'
        LIMIT 1
    """, (video_id,))
    if not video:
        return jsonify({'success': False, 'message': 'KSS video not found.'}), 404
    if video['bunny_video_id']:
        return jsonify({'success': True, 'already_exists': True,
                        'bunny_video_id': video['bunny_video_id']})
    try:
        bunny_video = bunny_create_video(title)
        bunny_video_id = bunny_video.get('guid') or bunny_video.get('videoId')
        if not bunny_video_id:
            raise RuntimeError('Bunny did not return a Video ID.')
        execute_query("""
            UPDATE videos
            SET bunny_video_id = %s, bunny_library_id = %s,
                status = 'processing', updated_at = NOW()
            WHERE video_id = %s
        """, (bunny_video_id, BUNNY_LIBRARY_ID, video_id))
        return jsonify({'success': True, 'already_exists': False,
                        'bunny_video_id': bunny_video_id})
    except Exception as e:
        app.logger.exception('Bunny video creation failed')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/api/bunny/upload-auth', methods=['POST'])
@admin_required
def admin_bunny_upload_auth():
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    if not video_id:
        return jsonify({'success': False, 'message': 'Video ID is required.'}), 400
    try:
        video_id = int(video_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid video ID.'}), 400

    video = fetch_one("""
        SELECT video_id, bunny_video_id, bunny_library_id
        FROM videos
        WHERE video_id = %s AND status <> 'deleted'
        LIMIT 1
    """, (video_id,))
    if not video:
        return jsonify({'success': False, 'message': 'Video not found.'}), 404
    if not video['bunny_video_id']:
        return jsonify({'success': False,
                        'message': 'A Bunny video has not been created yet.'}), 400
    try:
        upload = bunny_create_upload_signature(
            video['bunny_video_id'], video.get('bunny_library_id')
        )
        return jsonify({'success': True, **upload})
    except Exception as e:
        app.logger.exception('Bunny upload authorization failed')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/api/bunny/status/<int:video_id>')
@admin_required
def admin_bunny_status(video_id):
    video = fetch_one("""
        SELECT video_id, bunny_video_id, bunny_library_id, status
        FROM videos
        WHERE video_id = %s AND status <> 'deleted'
        LIMIT 1
    """, (video_id,))
    if not video:
        return jsonify({'success': False, 'message': 'Video not found.'}), 404
    if not video['bunny_video_id']:
        return jsonify({'success': True, 'connected': False,
                        'kss_status': video['status']})
    try:
        bunny = bunny_get_video(video['bunny_video_id'],
                                video.get('bunny_library_id'))
        bunny_status = bunny.get('status')
        kss_status = bunny_status_to_kss_status(bunny_status)
        duration = bunny.get('length') or 0
        encode_progress = bunny.get('encodeProgress') or 0
        if video['status'] != 'published':
            execute_query("""
                UPDATE videos
                SET status = %s, duration_seconds = %s, updated_at = NOW()
                WHERE video_id = %s
            """, (kss_status, duration, video_id))
        thumbnail_url = None
        if BUNNY_CDN_HOSTNAME:
            thumbnail_url = (f'https://{BUNNY_CDN_HOSTNAME}/'
                             f'{video["bunny_video_id"]}/thumbnail.jpg')
        return jsonify({
            'success': True, 'connected': True,
            'bunny_video_id': video['bunny_video_id'],
            'bunny_status': bunny_status,
            'encode_progress': encode_progress,
            'duration_seconds': duration,
            'kss_status': kss_status,
            'thumbnail_url': thumbnail_url,
        })
    except Exception as e:
        app.logger.exception('Bunny status check failed')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/api/bunny/delete/<int:video_id>', methods=['POST'])
@admin_required
def admin_bunny_delete(video_id):
    video = fetch_one("""
        SELECT video_id, bunny_video_id, bunny_library_id
        FROM videos WHERE video_id = %s LIMIT 1
    """, (video_id,))
    if not video:
        return jsonify({'success': False, 'message': 'Video not found.'}), 404
    if not video['bunny_video_id']:
        return jsonify({'success': True})
    try:
        bunny_delete_video(video['bunny_video_id'],
                           video.get('bunny_library_id'))
        execute_query("""
            UPDATE videos
            SET bunny_video_id = NULL, bunny_library_id = NULL,
                duration_seconds = NULL, thumbnail_url = NULL,
                status = 'draft', updated_at = NOW()
            WHERE video_id = %s
        """, (video_id,))
        return jsonify({'success': True})
    except Exception as e:
        app.logger.exception('Bunny video deletion failed')
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# PUBLIC PAGES (with caching for the hot queries)
# ============================================================

@app.route("/")
def index():
    # Cache the four "same for everyone" queries for 60-300 seconds.
    # Personalisation (is_locked / is_purchased) is applied after the cache.
    featured = cache.get('home_featured') if cache else None
    if featured is None:
        featured = fetch_all("""
            SELECT
                v.*, c.channel_name, c.slug AS channel_slug, c.channel_logo
            FROM videos v
            LEFT JOIN channels c ON v.channel_id = c.channel_id
            WHERE v.status = 'published'
              AND v.visibility IN ('public', 'unlisted')
            ORDER BY v.created_at DESC
            LIMIT 6
        """)
        if cache:
            cache.set('home_featured', featured, timeout=60)

    latest = cache.get('home_latest') if cache else None
    if latest is None:
        latest = fetch_all("""
            SELECT
                v.*, c.channel_name, c.slug AS channel_slug, c.channel_logo
            FROM videos v
            LEFT JOIN channels c ON v.channel_id = c.channel_id
            WHERE v.status = 'published'
              AND v.visibility IN ('public', 'unlisted')
            ORDER BY RAND()
            LIMIT 18
        """)
        if cache:
            # Shorter TTL for the randomised list
            cache.set('home_latest', latest, timeout=120)

    categories = cache.get('home_categories') if cache else None
    if categories is None:
        categories = fetch_all("""
            SELECT category_id, category_name, slug, description
            FROM categories
            WHERE is_active = 1
            ORDER BY category_name ASC
        """)
        if cache:
            cache.set('home_categories', categories, timeout=300)

    popular_channels = cache.get('home_popular_channels') if cache else None
    if popular_channels is None:
        popular_channels = fetch_all("""
            SELECT
                c.channel_id, c.channel_name, c.slug,
                c.channel_logo, c.channel_banner,
                c.subscriber_count, c.total_views,
                COUNT(v.video_id) AS total_videos
            FROM channels c
            LEFT JOIN videos v
                ON v.channel_id = c.channel_id
                AND v.status = 'published'
                AND v.visibility IN ('public', 'unlisted')
            WHERE c.is_active = 1
            GROUP BY
                c.channel_id, c.channel_name, c.slug,
                c.channel_logo, c.channel_banner,
                c.subscriber_count, c.total_views
            ORDER BY c.subscriber_count DESC, c.total_views DESC
            LIMIT 8
        """)
        if cache:
            cache.set('home_popular_channels', popular_channels, timeout=300)

    # The cache returns dicts (pickled), not mysql Row objects.
    # Copy them so per-user annotations don't leak between requests.
    featured = [dict(r) for r in featured]
    latest = [dict(r) for r in latest]

    user_id = session.get('user_id')
    annotate_video_lock_status(featured, user_id)
    annotate_video_lock_status(latest, user_id)
    annotate_purchase_status(featured, user_id)
    annotate_purchase_status(latest, user_id)

    return render_template(
        "index.html",
        featured=featured,
        videos=latest,
        categories=categories,
        popular_channels=popular_channels,
    )


@app.route('/register', methods=['GET', 'POST'])
def register():
    if is_logged_in():
        return redirect(url_for('index'))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not full_name:
            flash('Full name is required.', 'danger')
            return render_template('register.html')
        if len(full_name) > 150:
            flash('Full name is too long.', 'danger')
            return render_template('register.html')
        if not username:
            flash('Username is required.', 'danger')
            return render_template('register.html')
        if len(username) < 3:
            flash('Username must be at least 3 characters long.', 'danger')
            return render_template('register.html')
        if len(username) > 80:
            flash('Username is too long.', 'danger')
            return render_template('register.html')
        if not re.match(r'^[A-Za-z0-9_.]+$', username):
            flash('Username can only contain letters, numbers, underscores and dots.',
                  'danger')
            return render_template('register.html')
        if not email:
            flash('Email address is required.', 'danger')
            return render_template('register.html')
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash('Please enter a valid email address.', 'danger')
            return render_template('register.html')
        if len(email) > 255:
            flash('Email address is too long.', 'danger')
            return render_template('register.html')
        if password == '':
            flash('Password is required.', 'danger')
            return render_template('register.html')
        if len(password) < 8:
            flash('Password must be at least 8 characters long.', 'danger')
            return render_template('register.html')
        if len(password) > 128:
            flash('Password is too long.', 'danger')
            return render_template('register.html')
        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return render_template('register.html')

        existing = fetch_one("""
            SELECT user_id, email, username, phone
            FROM users
            WHERE email = %s
               OR username = %s
               OR (%s IS NOT NULL AND phone = %s)
            LIMIT 1
        """, (email, username, phone or None, phone or None))

        if existing:
            if existing.get('email') and existing['email'].lower() == email:
                flash('An account with that email already exists.', 'danger')
            elif existing.get('username') and existing['username'].lower() == username.lower():
                flash('That username is already taken. Please choose another.', 'danger')
            elif phone and existing.get('phone') == phone:
                flash('An account with that phone number already exists.', 'danger')
            else:
                flash('An account with some of these details already exists.', 'danger')
            return render_template('register.html')

        password_hash = hash_password(password)

        user_id = execute_query("""
            INSERT INTO users
                (full_name, username, email, phone, password_hash,
                 role, is_active, email_verified, phone_verified)
            VALUES (%s, %s, %s, %s, %s, 'user', TRUE, FALSE, FALSE)
        """, (full_name, username, email, phone or None, password_hash),
            return_lastrowid=True)

        if not user_id:
            flash('Unable to create your account. Please try again.', 'danger')
            return render_template('register.html')

        user = fetch_one('SELECT * FROM users WHERE user_id = %s LIMIT 1', (user_id,))
        if not user:
            flash('Your account was created, but we could not complete the login.',
                  'warning')
            return redirect(url_for('login'))

        login_user(user)
        flash('Account created successfully. Welcome to KSS!', 'success')
        return redirect(url_for('index'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_logged_in():
        return redirect(url_for('index'))

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')

        if not identifier or not password:
            flash('Enter your email/username and password.', 'danger')
            return render_template('login.html')

        user = fetch_one("""
            SELECT * FROM users
            WHERE email = %s OR username = %s
            LIMIT 1
        """, (identifier.lower(), identifier))

        if not user:
            flash('Invalid login details.', 'danger')
            return render_template('login.html')
        if not user['is_active']:
            flash('Your account is inactive.', 'danger')
            return render_template('login.html')
        if not verify_password(user['password_hash'], password):
            flash('Invalid login details.', 'danger')
            return render_template('login.html')

        # Clean up stale rows so dead sessions don't count against them
        purge_stale_sessions(user['user_id'])

        active = get_active_sessions(user['user_id'])

        if len(active) >= MAX_CONCURRENT_SESSIONS:
            flash(
                f'This account is already signed in on '
                f'{MAX_CONCURRENT_SESSIONS} devices. Sign out on one of them '
                f'before signing in here.',
                'danger'
            )
            return render_template(
                'login.html',
                active_sessions=[
                    {
                        'device': _describe_device(s['user_agent']),
                        'last_seen': s['last_seen_at'],
                    }
                    for s in active
                ],
                max_sessions=MAX_CONCURRENT_SESSIONS,
            )

        execute_query(
            'UPDATE users SET last_login_at = NOW() WHERE user_id = %s',
            (user['user_id'],)
        )

        login_user(user)

        next_url = request.args.get('next')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('index'))

    return render_template('login.html')




@app.route('/account/sessions')
@login_required
def account_sessions():
    user = get_current_user()
    current_token = session.get('session_token')

    rows = get_active_sessions(user['user_id'])
    sessions = []
    for row in rows:
        sessions.append({
            'token': row['session_token'],
            'device': _describe_device(row['user_agent']),
            'created_at': row['created_at'],
            'last_seen_at': row['last_seen_at'],
            'is_current': row['session_token'] == current_token,
        })

    return render_template(
        'account_sessions.html',
        sessions=sessions,
        max_sessions=MAX_CONCURRENT_SESSIONS,
    )


@app.route('/account/sessions/<token>/revoke', methods=['POST'])
@login_required
def revoke_session(token):
    user = get_current_user()

    row = fetch_one("""
        SELECT session_token FROM user_sessions
        WHERE session_token = %s AND user_id = %s
        LIMIT 1
    """, (token, user['user_id']))

    if not row:
        flash('Session not found.', 'danger')
        return redirect(url_for('account_sessions'))

    delete_session_row(token)

    if token == session.get('session_token'):
        session.clear()
        flash('You signed out of this device.', 'success')
        return redirect(url_for('login'))

    flash('Device signed out.', 'success')
    return redirect(url_for('account_sessions'))


@app.route('/account/sessions/revoke-all', methods=['POST'])
@login_required
def revoke_all_sessions():
    user = get_current_user()
    delete_all_sessions_for_user(user['user_id'])
    session.clear()
    flash('Signed out of every device.', 'success')
    return redirect(url_for('login'))




@app.route('/logout')
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))


@app.route('/profile')
@login_required
def profile():
    user = get_current_user()
    return render_template('profile.html', user=user)


@app.route("/videos")
def videos():
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 24
    search_query = request.args.get("q", "", type=str).strip()
    category_id = request.args.get("category", "", type=str).strip()
    deal_type = request.args.get("type", "", type=str).strip()
    offset = (page - 1) * per_page

    conditions = ["v.status = 'published'",
                  "v.visibility IN ('public', 'unlisted')"]
    params = []

    if search_query:
        terms = [t.strip() for t in search_query.split() if t.strip()]
        for term in terms:
            conditions.append("""
                (
                    v.title LIKE %s
                    OR v.description LIKE %s
                    OR c.channel_name LIKE %s
                    OR cat.category_name LIKE %s
                    OR EXISTS (
                        SELECT 1 FROM video_tags vt2
                        JOIN tags t2 ON vt2.tag_id = t2.tag_id
                        WHERE vt2.video_id = v.video_id
                          AND t2.tag_name LIKE %s
                    )
                )
            """)
            pattern = f"%{term}%"
            params.extend([pattern, pattern, pattern, pattern, pattern])

    if category_id:
        conditions.append("v.category_id = %s")
        params.append(category_id)

    if deal_type == "free":
        conditions.append("v.is_free = 1")
    elif deal_type == "purchase":
        conditions.append("v.purchase_enabled = 1")
    elif deal_type == "rental":
        conditions.append("v.rental_enabled = 1")

    where_sql = " AND ".join(conditions)

    total_row = fetch_one(f"""
        SELECT COUNT(*) AS total
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        LEFT JOIN categories cat ON v.category_id = cat.category_id
        WHERE {where_sql}
    """, tuple(params))
    total = total_row["total"] if total_row else 0

    rows = fetch_all(f"""
        SELECT
            v.*, c.channel_name, c.slug AS channel_slug,
            c.channel_logo, cat.category_name
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        LEFT JOIN categories cat ON v.category_id = cat.category_id
        WHERE {where_sql}
        ORDER BY v.created_at DESC, v.total_views DESC
        LIMIT %s OFFSET %s
    """, tuple(params + [per_page, offset]))

    categories = fetch_all("""
        SELECT category_id, category_name, slug
        FROM categories WHERE is_active = 1
        ORDER BY category_name
    """)

    total_pages = ((total + per_page - 1) // per_page) if total else 1
    annotate_video_lock_status(rows, session.get('user_id'))

    return render_template(
        "videos.html",
        videos=rows, categories=categories,
        page=page, total_pages=total_pages, total=total,
        search_query=search_query,
        selected_category=category_id,
        selected_type=deal_type,
    )


@app.route("/video/<slug>")
def video_details(slug):
    # This row is identical for every viewer of this video — cache it by
    # slug for a short window. For a viral video this collapses thousands
    # of identical lookups into one DB hit every ~15s.
    video = cached_fetch(f'video:by_slug:{slug}', 15, lambda: fetch_one("""
        SELECT
            v.*, c.channel_id, c.channel_name, c.slug AS channel_slug,
            c.channel_logo, c.channel_banner, c.subscriber_count,
            c.total_videos,
            cat.category_name, cat.slug AS category_slug
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        LEFT JOIN categories cat ON v.category_id = cat.category_id
        WHERE v.slug = %s
        LIMIT 1
    """, (slug,)))

    if not video:
        abort(404)
    video = dict(video)  # cache may return a shared object; copy before per-user edits
    if video["status"] != "published" and not is_admin():
        abort(404)

    user_id = session.get("user_id")

    access_type = (get_video_access_type(user_id, video) if user_id
                   else ("free" if video["is_free"] else None))

    is_following = False
    has_subscription = False

    if user_id:
        follower = fetch_one("""
            SELECT channel_id FROM channel_followers
            WHERE user_id = %s AND channel_id = %s LIMIT 1
        """, (user_id, video["channel_id"]))
        is_following = bool(follower)

        subscription = fetch_one("""
            SELECT subscription_id FROM channel_subscriptions
            WHERE user_id = %s AND channel_id = %s
              AND status = 'active' AND expires_at > NOW()
            LIMIT 1
        """, (user_id, video["channel_id"]))
        has_subscription = bool(subscription)

    related = [dict(r) for r in cached_fetch(
        f'video:related:{video["video_id"]}', 30, lambda: fetch_all("""
        SELECT
            v.video_id, v.title, v.slug, v.thumbnail_url,
            v.duration_seconds, v.is_free, v.purchase_enabled,
            v.purchase_price, v.rental_enabled, v.rental_price,
            c.channel_name, c.slug AS channel_slug
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE v.status = 'published'
          AND v.video_id != %s
          AND (v.category_id = %s OR v.channel_id = %s)
        ORDER BY v.total_views DESC
        LIMIT 8
    """, (video["video_id"], video["category_id"], video["channel_id"])))]

    annotate_video_lock_status(related, user_id)

    return render_template(
        "video_details.html",
        video=video, access_type=access_type,
        is_following=is_following, has_subscription=has_subscription,
        related=related,
        subscription_price=CHANNEL_SUBSCRIPTION_PRICE,
        subscription_days=CHANNEL_SUBSCRIPTION_DAYS,
    )


@app.route("/watch/<slug>")
def watch(slug):
    # Same idea as video_details: this is the page 3,000 concurrent viewers
    # of one launch video will all load. Cache the video row by slug.
    video = cached_fetch(f'video:by_slug:{slug}', 15, lambda: fetch_one("""
        SELECT v.*, c.channel_name, c.slug AS channel_slug, c.channel_logo
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE v.slug = %s
        LIMIT 1
    """, (slug,)))

    if not video:
        abort(404)
    video = dict(video)
    if video["status"] != "published" and not is_admin():
        abort(404)

    user_id = session.get("user_id")
    access_type = (get_video_access_type(user_id, video) if user_id
                   else ("free" if video["is_free"] else None))

    if not access_type:
        if not user_id:
            flash("Please sign in to access this video.", "info")
            return redirect(url_for("login", next=request.url))
        flash("You do not have access to this video.", "error")
        return redirect(url_for("video_details", slug=video["slug"]))

    suggested_videos = [dict(r) for r in cached_fetch(
        f'video:suggested:{video["video_id"]}', 30, lambda: fetch_all("""
        SELECT
            v.video_id, v.title, v.slug, v.thumbnail_url,
            v.duration_seconds, v.is_free, v.purchase_enabled,
            v.purchase_price, c.channel_name
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE v.status = 'published'
          AND v.video_id != %s
          AND (v.category_id = %s OR v.channel_id = %s)
        ORDER BY v.total_views DESC
        LIMIT 8
    """, (video["video_id"], video["category_id"], video["channel_id"])))]

    # Comments change more often, so a shorter TTL — still turns thousands
    # of identical reads into one DB hit every few seconds instead of one
    # per pageview.
    comments = [dict(r) for r in cached_fetch(
        f'video:comments:{video["video_id"]}', 8, lambda: fetch_all("""
        SELECT
            cm.comment_id, cm.comment_text, cm.created_at,
            u.user_id, u.full_name, u.username, u.profile_image
        FROM comments cm
        JOIN users u ON cm.user_id = u.user_id
        WHERE cm.video_id = %s
          AND cm.is_deleted = 0
          AND cm.parent_comment_id IS NULL
        ORDER BY cm.created_at DESC
        LIMIT 50
    """, (video["video_id"],)))]

    user_reaction = None
    if user_id:
        reaction = fetch_one("""
            SELECT reaction FROM video_reactions
            WHERE user_id = %s AND video_id = %s LIMIT 1
        """, (user_id, video["video_id"]))
        if reaction:
            user_reaction = reaction["reaction"]

    return render_template(
        "watch.html",
        video=video, access_type=access_type,
        suggested_videos=suggested_videos,
        comments=comments, user_reaction=user_reaction,
        bunny_cdn_hostname=BUNNY_CDN_HOSTNAME,
    )


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    results = []
    if query:
        search_term = f'%{query}%'
        results = fetch_all("""
            SELECT v.*, c.channel_name, c.slug AS channel_slug, c.channel_logo
            FROM videos v
            JOIN channels c ON c.channel_id = v.channel_id
            WHERE v.status = 'published'
              AND v.visibility = 'public'
              AND c.is_active = TRUE
              AND (
                    v.title LIKE %s
                    OR v.description LIKE %s
                    OR c.channel_name LIKE %s
                  )
            ORDER BY v.total_views DESC, v.published_at DESC
            LIMIT 50
        """, (search_term, search_term, search_term))
    return render_template('search.html', query=query, videos=results)


@app.route("/channel/<slug>")
def channel(slug):
    channel_data = fetch_one("""
        SELECT * FROM channels
        WHERE slug = %s AND is_active = 1
        LIMIT 1
    """, (slug,))
    if not channel_data:
        abort(404)

    sort = request.args.get("sort", "latest")
    sort_map = {
        "latest": "v.published_at DESC",
        "popular": "v.total_views DESC",
        "oldest": "v.published_at ASC",
    }
    order_sql = sort_map.get(sort, sort_map["latest"])

    channel_videos = fetch_all(f"""
        SELECT v.*, cat.category_name
        FROM videos v
        LEFT JOIN categories cat ON v.category_id = cat.category_id
        WHERE v.channel_id = %s
          AND v.status = 'published'
          AND v.visibility IN ('public', 'unlisted')
        ORDER BY {order_sql}
        LIMIT 60
    """, (channel_data["channel_id"],))

    video_count_row = fetch_one("""
        SELECT COUNT(*) AS total FROM videos
        WHERE channel_id = %s
          AND status = 'published'
          AND visibility IN ('public', 'unlisted')
    """, (channel_data["channel_id"],))
    video_count = video_count_row["total"] if video_count_row else 0

    annotate_video_lock_status(channel_videos, session.get('user_id'))

    follower_row = fetch_one(
        "SELECT COUNT(*) AS total FROM channel_followers WHERE channel_id = %s",
        (channel_data["channel_id"],)
    )
    follower_count = follower_row["total"] if follower_row else 0

    is_following = False
    has_subscription = False
    if session.get("user_id"):
        following = fetch_one("""
            SELECT channel_id FROM channel_followers
            WHERE user_id = %s AND channel_id = %s LIMIT 1
        """, (session["user_id"], channel_data["channel_id"]))
        is_following = bool(following)

        subscription = fetch_one("""
            SELECT subscription_id FROM channel_subscriptions
            WHERE user_id = %s AND channel_id = %s
              AND status = 'active' AND expires_at > NOW()
            LIMIT 1
        """, (session["user_id"], channel_data["channel_id"]))
        has_subscription = bool(subscription)

    return render_template(
        "channel.html",
        channel=channel_data, videos=channel_videos,
        video_count=video_count, follower_count=follower_count,
        is_following=is_following, has_subscription=has_subscription,
        sort=sort,
        subscription_price=CHANNEL_SUBSCRIPTION_PRICE,
        subscription_days=CHANNEL_SUBSCRIPTION_DAYS,
    )


# ============================================================
# API — reactions, follows, comments, progress, views
# ============================================================

@app.route('/api/channel/<int:channel_id>/follow', methods=['POST'])
@login_required
def toggle_channel_follow(channel_id):
    user_id = session['user_id']
    channel = fetch_one(
        'SELECT channel_id FROM channels WHERE channel_id = %s AND is_active = 1 LIMIT 1',
        (channel_id,)
    )
    if not channel:
        return jsonify({'success': False, 'message': 'Channel not found.'}), 404

    existing = fetch_one("""
        SELECT * FROM channel_followers
        WHERE user_id = %s AND channel_id = %s
        LIMIT 1
    """, (user_id, channel_id))

    if existing:
        execute_query(
            'DELETE FROM channel_followers WHERE user_id = %s AND channel_id = %s',
            (user_id, channel_id)
        )
        following = False
    else:
        execute_query("""
            INSERT INTO channel_followers (user_id, channel_id)
            VALUES (%s, %s)
        """, (user_id, channel_id))
        following = True

    count_row = fetch_one(
        'SELECT COUNT(*) AS total FROM channel_followers WHERE channel_id = %s',
        (channel_id,)
    )
    return jsonify({
        'success': True, 'following': following,
        'followers': count_row['total'] if count_row else 0,
    })


@app.route('/api/video/<int:video_id>/reaction', methods=['POST'])
@login_required
def video_reaction(video_id):
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    reaction = data.get('reaction')
    if reaction not in ('like', 'dislike'):
        return jsonify({'success': False, 'message': 'Invalid reaction.'}), 400

    video = fetch_one(
        'SELECT video_id FROM videos WHERE video_id = %s LIMIT 1', (video_id,)
    )
    if not video:
        return jsonify({'success': False, 'message': 'Video not found.'}), 404

    existing = fetch_one("""
        SELECT reaction_id, reaction FROM video_reactions
        WHERE user_id = %s AND video_id = %s LIMIT 1
    """, (user_id, video_id))

    # Instead of recomputing SUM(reaction='like') over every row this video
    # has ever received (a full table scan that gets slower the more
    # popular the video is, right when it's getting hammered), apply the
    # delta directly to the counter columns with a single atomic UPDATE.
    # This is O(1) per click and safe under concurrency: MySQL serializes
    # the += / -= on that row rather than us doing a racy read-modify-write
    # in Python.
    like_delta = dislike_delta = 0

    if existing:
        if existing['reaction'] == reaction:
            execute_query(
                'DELETE FROM video_reactions WHERE reaction_id = %s',
                (existing['reaction_id'],)
            )
            current_reaction = None
            if reaction == 'like':
                like_delta = -1
            else:
                dislike_delta = -1
        else:
            execute_query(
                'UPDATE video_reactions SET reaction = %s WHERE reaction_id = %s',
                (reaction, existing['reaction_id'])
            )
            current_reaction = reaction
            if reaction == 'like':
                like_delta, dislike_delta = 1, -1
            else:
                like_delta, dislike_delta = -1, 1
    else:
        execute_query("""
            INSERT INTO video_reactions (user_id, video_id, reaction)
            VALUES (%s, %s, %s)
        """, (user_id, video_id, reaction))
        current_reaction = reaction
        if reaction == 'like':
            like_delta = 1
        else:
            dislike_delta = 1

    execute_query("""
        UPDATE videos
        SET total_likes = GREATEST(0, total_likes + %s),
            total_dislikes = GREATEST(0, total_dislikes + %s)
        WHERE video_id = %s
    """, (like_delta, dislike_delta, video_id))

    counts = fetch_one(
        'SELECT total_likes, total_dislikes FROM videos WHERE video_id = %s',
        (video_id,)
    ) or {'total_likes': 0, 'total_dislikes': 0}

    return jsonify({
        'success': True, 'reaction': current_reaction,
        'likes': int(counts['total_likes'] or 0),
        'dislikes': int(counts['total_dislikes'] or 0),
    })


@app.route("/api/video/<int:video_id>/comment", methods=["POST"])
def add_comment(video_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(success=False, message="Please sign in to comment."), 401

    data = request.get_json(silent=True) or {}
    text = (data.get("comment_text") or "").strip()
    if not text:
        return jsonify(success=False, message="comment cannot be empty"), 400

    video = fetch_one(
        "SELECT video_id FROM videos WHERE video_id = %s LIMIT 1", (video_id,)
    )
    if not video:
        return jsonify(success=False, message="Video not found"), 404

    new_comment_id = execute_query("""
        INSERT INTO comments
            (user_id, video_id, parent_comment_id, comment_text,
             is_edited, is_deleted, is_pinned, like_count,
             created_at, updated_at)
        VALUES (%s, %s, NULL, %s, 0, 0, 0, 0, NOW(), NOW())
    """, (user_id, video_id, text), return_lastrowid=True)

    if not new_comment_id:
        return jsonify(success=False, message="Unable to save comment"), 500

    execute_query("""
        UPDATE videos SET total_comments = COALESCE(total_comments, 0) + 1
        WHERE video_id = %s
    """, (video_id,))

    user = fetch_one("""
        SELECT user_id, full_name, username, profile_image
        FROM users WHERE user_id = %s LIMIT 1
    """, (user_id,))

    return jsonify(success=True, comment={
        "comment_id": new_comment_id,
        "comment_text": text,
        "full_name": user["full_name"],
        "username": user["username"],
        "profile_image": user["profile_image"],
    })


@app.route("/api/video/<int:video_id>/progress", methods=["POST"])
@login_required
def save_watch_progress(video_id):
    user_id = session["user_id"]
    video = fetch_one("SELECT * FROM videos WHERE video_id = %s LIMIT 1", (video_id,))
    if not video:
        return jsonify({"success": False, "message": "Video not found."}), 404

    if not has_video_access(user_id, video):
        return jsonify({"success": False, "message": "You do not have access to this video."}), 403

    data = request.get_json(silent=True) or {}
    try:
        progress_seconds = max(0, int(float(data.get("progress_seconds", 0))))
        completion_percentage = max(0, min(100, float(data.get("completion_percentage", 0))))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid progress data."}), 400

    execute_query("""
        INSERT INTO watch_history
            (user_id, video_id, progress_seconds, completion_percentage, last_watched_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            progress_seconds = VALUES(progress_seconds),
            completion_percentage = VALUES(completion_percentage),
            last_watched_at = NOW()
    """, (user_id, video_id, progress_seconds, completion_percentage))

    return jsonify({"success": True})


@app.route("/api/video/<int:video_id>/view", methods=["POST"])
def record_video_view(video_id):
    video = fetch_one("""
        SELECT * FROM videos
        WHERE video_id = %s AND status = 'published'
        LIMIT 1
    """, (video_id,))
    if not video:
        return jsonify({"success": False}), 404

    user_id = session.get("user_id")
    if not video["is_free"]:
        if not user_id:
            return jsonify({"success": False, "message": "Login required."}), 401
        if not has_video_access(user_id, video):
            return jsonify({"success": False, "message": "No access."}), 403

    session_id = session.get("view_session_id")
    if not session_id:
        session_id = uuid.uuid4().hex
        session["view_session_id"] = session_id

    ip_address = request.remote_addr or ""
    ip_hash = hashlib.sha256(ip_address.encode("utf-8")).hexdigest()
    user_agent = request.headers.get("User-Agent", "")

    recent_view = fetch_one("""
        SELECT view_id FROM video_views
        WHERE video_id = %s AND session_id = %s
          AND started_at >= NOW() - INTERVAL 30 MINUTE
        LIMIT 1
    """, (video_id, session_id))
    if recent_view:
        return jsonify({"success": True, "counted": False})

    execute_query("""
        INSERT INTO video_views
            (video_id, user_id, session_id, ip_hash, user_agent,
             started_at, last_activity_at, is_valid_view)
        VALUES (%s, %s, %s, %s, %s, NOW(), NOW(), 1)
    """, (video_id, user_id, session_id, ip_hash, user_agent))

    execute_query("""
        UPDATE videos
        SET total_views = total_views + 1, updated_at = NOW()
        WHERE video_id = %s
    """, (video_id,))

    return jsonify({"success": True, "counted": True})


# ============================================================
# PAYMENTS — purchase, rent, subscribe, callback, webhook
# ============================================================

@app.route("/video/<int:video_id>/purchase")
@login_required
def purchase_video(video_id):
    video = fetch_one("""
        SELECT v.*, c.channel_name, c.slug AS channel_slug
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE v.video_id = %s LIMIT 1
    """, (video_id,))
    if not video:
        abort(404)
    if video["status"] != "published":
        flash("This video is not available for purchase.", "error")
        return redirect(url_for("index"))
    if not video["purchase_enabled"]:
        flash("Purchase is not available for this video.", "error")
        return redirect(url_for("video_details", slug=video["slug"]))
    if has_video_access(session["user_id"], video):
        return redirect(url_for("watch", slug=video["slug"]))

    return render_template(
        "payment.html",
        payment_type="video_purchase",
        video=video, amount=video["purchase_price"],
        currency=video["currency"] or "GHS",
        paystack_public_key=PAYSTACK_PUBLIC_KEY,
    )


@app.route("/video/<int:video_id>/rent")
@login_required
def rent_video(video_id):
    video = fetch_one("""
        SELECT v.*, c.channel_name, c.slug AS channel_slug
        FROM videos v
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE v.video_id = %s LIMIT 1
    """, (video_id,))
    if not video:
        abort(404)
    if video["status"] != "published":
        flash("This video is not available for rental.", "error")
        return redirect(url_for("index"))
    if not video["rental_enabled"]:
        flash("Rental is not available for this video.", "error")
        return redirect(url_for("video_details", slug=video["slug"]))

    access_type = get_video_access_type(session["user_id"], video)
    if access_type in ("free", "admin", "purchase", "rental", "subscription"):
        return redirect(url_for("watch", slug=video["slug"]))

    return render_template(
        "payment.html",
        payment_type="video_rental",
        video=video, amount=video["rental_price"],
        rental_duration_hours=VIDEO_RENTAL_DURATION_HOURS,
        currency=video["currency"] or "GHS",
        paystack_public_key=PAYSTACK_PUBLIC_KEY,
    )


@app.route("/channel/<int:channel_id>/subscribe")
@login_required
def subscribe_channel(channel_id):
    channel = fetch_one("""
        SELECT * FROM channels
        WHERE channel_id = %s AND is_active = 1
        LIMIT 1
    """, (channel_id,))
    if not channel:
        abort(404)

    existing = fetch_one("""
        SELECT * FROM channel_subscriptions
        WHERE user_id = %s AND channel_id = %s
          AND status = 'active' AND expires_at > NOW()
        ORDER BY expires_at DESC LIMIT 1
    """, (session["user_id"], channel_id))

    if existing:
        flash("You already have an active subscription to this channel.", "info")
        return redirect(url_for("channel", slug=channel["slug"]))

    return render_template(
        "payment.html",
        payment_type="channel_subscription",
        channel=channel, amount=CHANNEL_SUBSCRIPTION_PRICE,
        subscription_days=CHANNEL_SUBSCRIPTION_DAYS,
        currency="GHS",
        paystack_public_key=PAYSTACK_PUBLIC_KEY,
    )


@app.route('/purchases')
@login_required
def purchases():
    user_id = session['user_id']
    purchases_list = fetch_all("""
        SELECT p.*, v.title, v.slug, v.thumbnail_url
        FROM video_purchases p
        JOIN videos v ON v.video_id = p.video_id
        WHERE p.user_id = %s AND p.status = 'paid'
        ORDER BY p.purchased_at DESC
    """, (user_id,))
    return render_template('purchases.html', purchases=purchases_list)


@app.route('/history')
@login_required
def history():
    user_id = session['user_id']
    history_list = fetch_all("""
        SELECT h.*, v.title, v.slug, v.thumbnail_url,
               v.duration_seconds, c.channel_name,
               c.slug AS channel_slug
        FROM watch_history h
        JOIN videos v ON v.video_id = h.video_id
        JOIN channels c ON c.channel_id = v.channel_id
        WHERE h.user_id = %s
        ORDER BY h.last_watched_at DESC
        LIMIT 100
    """, (user_id,))
    return render_template('watch_history.html', history=history_list)


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]

    purchases = fetch_all("""
        SELECT
            vp.purchase_id, vp.amount, vp.currency, vp.purchased_at,
            v.video_id, v.title, v.slug, v.thumbnail_url,
            c.channel_name
        FROM video_purchases vp
        JOIN videos v ON vp.video_id = v.video_id
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE vp.user_id = %s AND vp.status = 'paid'
        ORDER BY vp.purchased_at DESC
    """, (user_id,))

    rentals = fetch_all("""
        SELECT
            vr.rental_id, vr.amount, vr.currency, vr.rented_at,
            vr.expires_at, vr.status,
            v.video_id, v.title, v.slug, v.thumbnail_url,
            c.channel_name
        FROM video_rentals vr
        JOIN videos v ON vr.video_id = v.video_id
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE vr.user_id = %s
          AND vr.status = 'active' AND vr.expires_at > NOW()
        ORDER BY vr.expires_at ASC
    """, (user_id,))

    subscriptions = fetch_all("""
        SELECT
            cs.subscription_id, cs.amount, cs.currency,
            cs.started_at, cs.expires_at, cs.status,
            c.channel_id, c.channel_name, c.slug, c.channel_logo
        FROM channel_subscriptions cs
        JOIN channels c ON cs.channel_id = c.channel_id
        WHERE cs.user_id = %s
          AND cs.status = 'active' AND cs.expires_at > NOW()
        ORDER BY cs.expires_at ASC
    """, (user_id,))

    history = fetch_all("""
        SELECT
            wh.progress_seconds, wh.completion_percentage,
            wh.last_watched_at,
            v.video_id, v.title, v.slug, v.thumbnail_url,
            v.duration_seconds, c.channel_name
        FROM watch_history wh
        JOIN videos v ON wh.video_id = v.video_id
        LEFT JOIN channels c ON v.channel_id = c.channel_id
        WHERE wh.user_id = %s
        ORDER BY wh.last_watched_at DESC
        LIMIT 12
    """, (user_id,))

    return render_template(
        "dashboard.html",
        purchases=purchases, rentals=rentals,
        subscriptions=subscriptions, history=history,
    )


# ============================================================
# ADMIN — dashboard, users, channels, videos, payments, analytics
# ============================================================

@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    stats = {
        'total_users': (fetch_one('SELECT COUNT(*) AS total FROM users') or {}).get('total', 0),
        'total_channels': (fetch_one('SELECT COUNT(*) AS total FROM channels WHERE is_active = TRUE') or {}).get('total', 0),
        'total_videos': (fetch_one("SELECT COUNT(*) AS total FROM videos WHERE status <> 'deleted'") or {}).get('total', 0),
        'total_views': (fetch_one("SELECT COALESCE(SUM(total_views), 0) AS total FROM videos WHERE status <> 'deleted'") or {}).get('total', 0),
    }
    recent_payments = fetch_all("""
        SELECT p.payment_id, p.payment_type, p.amount, p.currency,
               p.paid_at, u.full_name
        FROM payments p
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE p.status = 'successful'
        ORDER BY p.paid_at DESC
        LIMIT 6
    """)
    recent_videos = fetch_all("""
        SELECT v.video_id, v.title, v.thumbnail_url, v.status,
               v.is_free, v.rental_enabled, v.purchase_enabled,
               v.total_views, c.channel_name
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.status <> 'deleted'
        ORDER BY v.created_at DESC
        LIMIT 6
    """)
    top_channels = fetch_all("""
        SELECT channel_id, channel_name, channel_logo,
               subscriber_count, total_views
        FROM channels
        WHERE is_active = TRUE
        ORDER BY total_views DESC
        LIMIT 6
    """)

    chart_labels, chart_views, chart_users = [], [], []
    for i in range(6, -1, -1):
        day = datetime.now().date() - timedelta(days=i)
        chart_labels.append(day.strftime('%a'))
        view_result = fetch_one("""
            SELECT COUNT(*) AS total FROM video_views
            WHERE DATE(started_at) = %s AND is_valid_view = TRUE
        """, (day,))
        chart_views.append(view_result['total'] if view_result else 0)
        user_result = fetch_one(
            'SELECT COUNT(*) AS total FROM users WHERE DATE(created_at) = %s',
            (day,)
        )
        chart_users.append(user_result['total'] if user_result else 0)

    return render_template(
        'admin_dashboard.html',
        stats=stats, recent_payments=recent_payments,
        recent_videos=recent_videos, top_channels=top_channels,
        chart_labels=chart_labels, chart_views=chart_views,
        chart_users=chart_users,
    )


@app.route('/admin/users')
@admin_required
def admin_users():
    users = fetch_all("""
        SELECT user_id, full_name, username, email, phone,
               profile_image, role, is_active,
               email_verified, created_at, last_login_at
        FROM users
        ORDER BY created_at DESC
        LIMIT 500
    """)
    user_stats = fetch_one("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(is_active = TRUE), 0) AS active,
            COALESCE(SUM(role = 'admin'), 0) AS admins,
            COALESCE(SUM(created_at >= DATE_FORMAT(CURDATE(), '%Y-%m-01')), 0)
                AS new_this_month
        FROM users
    """) or {'total': 0, 'active': 0, 'admins': 0, 'new_this_month': 0}
    return render_template('admin_users.html', users=users, user_stats=user_stats)


@app.route('/admin/users/<int:user_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_user_status(user_id):
    if user_id == session['user_id']:
        flash('You cannot deactivate your own account.', 'warning')
        return redirect(url_for('admin_users'))
    user = fetch_one(
        'SELECT user_id, is_active FROM users WHERE user_id = %s LIMIT 1',
        (user_id,)
    )
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_users'))
    new_status = not bool(user['is_active'])
    execute_query(
        'UPDATE users SET is_active = %s WHERE user_id = %s',
        (new_status, user_id)
    )
    flash('User status updated successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/channels')
@admin_required
def admin_channels():
    channel_stats = {
        'total': (fetch_one('SELECT COUNT(*) AS total FROM channels') or {}).get('total', 0),
        'active': (fetch_one('SELECT COUNT(*) AS total FROM channels WHERE is_active = TRUE') or {}).get('total', 0),
        'verified': (fetch_one('SELECT COUNT(*) AS total FROM channels WHERE is_verified = TRUE') or {}).get('total', 0),
        'subscribers': (fetch_one('SELECT COALESCE(SUM(subscriber_count), 0) AS total FROM channels WHERE is_active = TRUE') or {}).get('total', 0),
    }
    channels = fetch_all("""
        SELECT
            c.channel_id, c.owner_user_id, c.channel_name, c.slug,
            c.description, c.channel_logo, c.channel_banner,
            c.subscriber_count, c.total_views, c.total_videos,
            c.is_active, c.is_verified, c.created_at,
            u.full_name AS owner_name, u.email AS owner_email
        FROM channels c
        LEFT JOIN users u ON u.user_id = c.owner_user_id
        ORDER BY c.created_at DESC
    """)
    return render_template(
        'admin_channels.html',
        channels=channels, channel_stats=channel_stats,
    )


@app.route('/admin/channels/create', methods=['GET', 'POST'])
@app.route('/admin/channels/<int:channel_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_create_channel(channel_id=None):
    channel = None
    if channel_id:
        channel = fetch_one("""
            SELECT
                channel_id, owner_user_id, channel_name, slug,
                description, channel_logo, channel_banner,
                subscriber_count, total_views, total_videos,
                is_active, is_verified, created_at
            FROM channels WHERE channel_id = %s
        """, (channel_id,))
        if not channel:
            return ('Channel not found.', 404)

    users = fetch_all("""
        SELECT user_id, full_name, email FROM users
        WHERE is_active = TRUE ORDER BY full_name ASC
    """)

    if request.method == 'POST':
        channel_name = request.form.get('channel_name', '').strip()
        slug = request.form.get('slug', '').strip().lower()
        description = request.form.get('description', '').strip()
        owner_user_id = request.form.get('owner_user_id')
        is_active = 1 if request.form.get('is_active') else 0
        is_verified = 1 if request.form.get('is_verified') else 0

        if not channel_name:
            flash('Channel name is required.', 'error')
            return render_template('admin_create_channel.html',
                                   channel=channel, users=users)
        if not slug:
            flash('Channel URL is required.', 'error')
            return render_template('admin_create_channel.html',
                                   channel=channel, users=users)
        if not owner_user_id:
            flash('Please select a channel owner.', 'error')
            return render_template('admin_create_channel.html',
                                   channel=channel, users=users)

        owner = fetch_one("""
            SELECT user_id FROM users
            WHERE user_id = %s AND is_active = TRUE
        """, (owner_user_id,))
        if not owner:
            flash('The selected channel owner is invalid.', 'error')
            return render_template('admin_create_channel.html',
                                   channel=channel, users=users)

        existing_slug = fetch_one(
            'SELECT channel_id FROM channels WHERE slug = %s', (slug,)
        )
        if existing_slug:
            if not channel_id or existing_slug['channel_id'] != channel_id:
                flash('That channel URL is already in use.', 'error')
                return render_template('admin_create_channel.html',
                                       channel=channel, users=users)

        channel_logo = channel['channel_logo'] if channel else None
        channel_banner = channel['channel_banner'] if channel else None

        logo_file = request.files.get('channel_logo')
        if logo_file and logo_file.filename:
            try:
                logo_result = cloudinary.uploader.upload(
                    logo_file, folder="kss/channels/logos",
                    resource_type="image"
                )
                channel_logo = logo_result.get('secure_url')
            except Exception as e:
                print("CHANNEL LOGO UPLOAD ERROR:", e)
                flash('Unable to upload the channel logo. Please try again.', 'error')
                return render_template('admin_create_channel.html',
                                       channel=channel, users=users)

        banner_file = request.files.get('channel_banner')
        if banner_file and banner_file.filename:
            try:
                banner_result = cloudinary.uploader.upload(
                    banner_file, folder="kss/channels/banners",
                    resource_type="image"
                )
                channel_banner = banner_result.get('secure_url')
            except Exception as e:
                print("CHANNEL BANNER UPLOAD ERROR:", e)
                flash('Unable to upload the channel banner. Please try again.', 'error')
                return render_template('admin_create_channel.html',
                                       channel=channel, users=users)

        if channel_id:
            execute_query("""
                UPDATE channels
                SET owner_user_id = %s, channel_name = %s, slug = %s,
                    description = %s, channel_logo = %s,
                    channel_banner = %s, is_active = %s, is_verified = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = %s
            """, (owner_user_id, channel_name, slug, description,
                  channel_logo, channel_banner, is_active, is_verified,
                  channel_id))
            flash('Channel updated successfully.', 'success')
        else:
            new_channel_id = execute_query("""
                INSERT INTO channels
                    (owner_user_id, channel_name, slug, description,
                     channel_logo, channel_banner,
                     subscriber_count, total_views, total_videos,
                     is_active, is_verified)
                VALUES (%s, %s, %s, %s, %s, %s, 0, 0, 0, %s, %s)
            """, (owner_user_id, channel_name, slug, description,
                  channel_logo, channel_banner, is_active, is_verified),
                return_lastrowid=True)

            if not new_channel_id:
                flash('Unable to create the channel.', 'error')
                return render_template('admin_create_channel.html',
                                       channel=None, users=users)
            flash('Channel created successfully.', 'success')

        return redirect(url_for('admin_channels'))

    return render_template('admin_create_channel.html',
                           channel=channel, users=users)


app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'thumbnails')
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


@app.route('/admin/videos')
@admin_required
def admin_videos():
    video_stats = {
        'total': (fetch_one("SELECT COUNT(*) AS total FROM videos WHERE status <> 'deleted'") or {}).get('total', 0),
        'published': (fetch_one("SELECT COUNT(*) AS total FROM videos WHERE status = 'published'") or {}).get('total', 0),
        'processing': (fetch_one("SELECT COUNT(*) AS total FROM videos WHERE status = 'processing'") or {}).get('total', 0),
        'drafts': (fetch_one("SELECT COUNT(*) AS total FROM videos WHERE status = 'draft'") or {}).get('total', 0),
        'paid': (fetch_one("""
            SELECT COUNT(*) AS total FROM videos
            WHERE status <> 'deleted'
              AND (rental_enabled = TRUE
                   OR purchase_enabled = TRUE
                   OR subscription_enabled = TRUE)
        """) or {}).get('total', 0),
    }
    channels = fetch_all("""
        SELECT channel_id, channel_name FROM channels
        WHERE is_active = TRUE ORDER BY channel_name ASC
    """)
    videos = fetch_all("""
        SELECT
            v.video_id, v.channel_id, v.category_id,
            v.title, v.slug, v.thumbnail_url, v.bunny_video_id,
            v.duration_seconds, v.status, v.visibility,
            v.is_free,
            v.rental_enabled, v.rental_price, v.rental_duration_hours,
            v.purchase_enabled, v.purchase_price,
            v.subscription_enabled, v.currency,
            v.total_views, v.total_likes, v.total_dislikes,
            v.total_comments,
            v.published_at, v.created_at,
            c.channel_name, cat.category_name
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN categories cat ON cat.category_id = v.category_id
        WHERE v.status <> 'deleted'
        ORDER BY v.created_at DESC
        LIMIT 200
    """)
    return render_template('admin_videos.html',
                           videos=videos, channels=channels,
                           video_stats=video_stats)


@app.route('/admin/videos/create', methods=['GET', 'POST'])
@app.route('/admin/videos/<int:video_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_create_video(video_id=None):
    video = None
    selected_tags = []

    if video_id:
        video = fetch_one("""
            SELECT * FROM videos
            WHERE video_id = %s AND status <> 'deleted'
            LIMIT 1
        """, (video_id,))
        if not video:
            flash('Video not found.', 'error')
            return redirect(url_for('admin_videos'))
        tag_rows = fetch_all("""
            SELECT t.tag_name FROM tags t
            INNER JOIN video_tags vt ON vt.tag_id = t.tag_id
            WHERE vt.video_id = %s
            ORDER BY t.tag_name ASC
        """, (video_id,))
        selected_tags = [row['tag_name'] for row in tag_rows]

    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        try:
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            slug = request.form.get('slug', '').strip().lower()
            channel_id = request.form.get('channel_id')
            category_id = request.form.get('category_id')
            tags_string = request.form.get('tags', '').strip()
            bunny_video_id = request.form.get('bunny_video_id', '').strip()
            duration_seconds = request.form.get('duration_seconds')
            visibility = request.form.get('visibility', 'public')
            currency = request.form.get('currency', 'GHS').upper()
            action = request.form.get('action', 'save')

            is_free = request.form.get('is_free') == '1'
            rental_enabled = request.form.get('rental_enabled') == '1'
            purchase_enabled = request.form.get('purchase_enabled') == '1'
            subscription_enabled = request.form.get('subscription_enabled') == '1'
            rental_price = request.form.get('rental_price')
            purchase_price = request.form.get('purchase_price')

            # =========================================================
            # THUMBNAIL UPLOAD -> CLOUDINARY
            # =========================================================
            thumbnail_file = request.files.get('thumbnail_file')
            thumbnail_url = video['thumbnail_url'] if video else ''

            if thumbnail_file and thumbnail_file.filename:
                if allowed_file(thumbnail_file.filename):
                    try:
                        upload_result = cloudinary.uploader.upload(
                            thumbnail_file,
                            folder="kss/thumbnails",
                            resource_type="image"
                        )
                        thumbnail_url = upload_result.get('secure_url')

                        if not thumbnail_url:
                            raise RuntimeError(
                                'Cloudinary did not return a secure URL.'
                            )

                    except Exception as upload_error:
                        app.logger.exception('Thumbnail upload failed')
                        message = 'Unable to upload the thumbnail. Please try again.'
                        if is_ajax:
                            return jsonify({'success': False, 'message': message}), 500
                        flash(message, 'error')
                        return redirect(request.url)
                else:
                    message = 'Invalid thumbnail file type. Allowed: png, jpg, jpeg, gif, webp.'
                    if is_ajax:
                        return jsonify({'success': False, 'message': message}), 400
                    flash(message, 'error')
                    return redirect(request.url)

            # =================================================
            # VALIDATE TITLE
            # =================================================
            if not title:
                message = 'Video title is required.'
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)

            if len(title) > 255:
                message = 'Video title cannot exceed 255 characters.'
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)

            # =================================================
            # VALIDATE CHANNEL
            # =================================================
            if not channel_id:
                message = 'Please select a channel.'
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)

            try:
                channel_id = int(channel_id)
            except (TypeError, ValueError):
                message = 'Invalid channel.'
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)

            channel = fetch_one("""
                SELECT channel_id, channel_name, is_active
                FROM channels WHERE channel_id = %s LIMIT 1
            """, (channel_id,))
            if not channel:
                message = 'Selected channel does not exist.'
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)
            if not channel['is_active']:
                message = 'The selected channel is inactive.'
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)

            # =================================================
            # CATEGORY
            # =================================================
            if category_id:
                try:
                    category_id = int(category_id)
                except (TypeError, ValueError):
                    category_id = None
            else:
                category_id = None

            # =================================================
            # VISIBILITY
            # =================================================
            allowed_visibility = {'public', 'private', 'unlisted'}
            if visibility not in allowed_visibility:
                visibility = 'public'

            # =================================================
            # CURRENCY
            # =================================================
            allowed_currencies = {'GHS', 'USD'}
            if currency not in allowed_currencies:
                currency = 'GHS'

            # =================================================
            # DURATION
            # =================================================
            if duration_seconds:
                try:
                    duration_seconds = int(duration_seconds)
                    if duration_seconds < 0:
                        duration_seconds = 0
                except (TypeError, ValueError):
                    duration_seconds = None
            else:
                duration_seconds = None

            # =================================================
            # RENTAL
            # =================================================
            if rental_enabled:
                try:
                    rental_price = float(rental_price or 0)
                    if rental_price <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    message = 'Enter a valid rental price greater than 0.'
                    if is_ajax:
                        return jsonify({'success': False, 'message': message}), 400
                    flash(message, 'error')
                    return redirect(request.url)
                rental_duration_hours = VIDEO_RENTAL_DURATION_HOURS
            else:
                rental_price = 0.00
                rental_duration_hours = VIDEO_RENTAL_DURATION_HOURS

            # =================================================
            # PURCHASE
            # =================================================
            if purchase_enabled:
                try:
                    purchase_price = float(purchase_price or 0)
                    if purchase_price <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    message = 'Enter a valid purchase price greater than 0.'
                    if is_ajax:
                        return jsonify({'success': False, 'message': message}), 400
                    flash(message, 'error')
                    return redirect(request.url)
            else:
                purchase_price = 0.00

            # =================================================
            # ACCESS LOGIC
            # =================================================
            if is_free:
                rental_enabled = False
                purchase_enabled = False
                rental_price = 0.00
                purchase_price = 0.00
                rental_duration_hours = VIDEO_RENTAL_DURATION_HOURS
            elif not (rental_enabled or purchase_enabled or subscription_enabled):
                message = ('A paid video must offer rental, permanent purchase, '
                           'or channel subscription access.')
                if is_ajax:
                    return jsonify({'success': False, 'message': message}), 400
                flash(message, 'error')
                return redirect(request.url)

            # =================================================
            # SLUG
            # =================================================
            if slug:
                slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
            if not slug:
                slug = generate_video_slug(title, video_id)
            else:
                if video_id:
                    duplicate = fetch_one("""
                        SELECT video_id FROM videos
                        WHERE slug = %s AND video_id <> %s LIMIT 1
                    """, (slug, video_id))
                else:
                    duplicate = fetch_one(
                        'SELECT video_id FROM videos WHERE slug = %s LIMIT 1',
                        (slug,)
                    )
                if duplicate:
                    slug = generate_video_slug(title, video_id)

            # =================================================
            # DATABASE
            # =================================================
            connection = get_db_connection()
            if not connection:
                raise RuntimeError('Unable to connect to database.')

            cursor = connection.cursor(dictionary=True)

            try:
                # ---------- UPDATE EXISTING ----------
                if video_id:
                    existing = fetch_one("""
                        SELECT video_id, bunny_video_id,
                               bunny_library_id, status
                        FROM videos WHERE video_id = %s LIMIT 1
                    """, (video_id,))
                    if not existing:
                        raise RuntimeError('Video no longer exists.')

                    current_bunny_video_id = existing['bunny_video_id']
                    if not bunny_video_id:
                        bunny_video_id = current_bunny_video_id

                    current_status = existing['status']
                    new_status = current_status
                    if action == 'save':
                        if current_status == 'published':
                            new_status = 'published'
                        elif bunny_video_id:
                            if current_status in {'ready', 'unpublished'}:
                                new_status = current_status
                            else:
                                new_status = 'draft'
                        else:
                            new_status = 'draft'
                    elif action == 'save_publish':
                        if not bunny_video_id:
                            raise RuntimeError(
                                'This video cannot be published until it has '
                                'been uploaded to Bunny Stream.'
                            )
                        if current_status not in {'ready', 'published'}:
                            raise RuntimeError(
                                'This video is not ready for publishing. '
                                'Bunny Stream must finish processing it first.'
                            )
                        new_status = 'published'

                    cursor.execute("""
                        UPDATE videos
                        SET
                            channel_id = %s, category_id = %s, title = %s,
                            slug = %s, description = %s, thumbnail_url = %s,
                            bunny_video_id = %s, bunny_library_id = %s,
                            duration_seconds = %s, status = %s, visibility = %s,
                            is_free = %s, rental_enabled = %s,
                            rental_price = %s, rental_duration_hours = %s,
                            purchase_enabled = %s, purchase_price = %s,
                            subscription_enabled = %s, currency = %s,
                            published_at = CASE
                                WHEN %s = 'published' AND status <> 'published' THEN NOW()
                                WHEN %s <> 'published' THEN NULL
                                ELSE published_at
                            END,
                            updated_at = NOW()
                        WHERE video_id = %s
                    """, (
                        channel_id, category_id, title, slug, description,
                        thumbnail_url or None, bunny_video_id or None,
                        BUNNY_LIBRARY_ID if bunny_video_id else None,
                        duration_seconds, new_status, visibility,
                        is_free, rental_enabled, rental_price,
                        rental_duration_hours, purchase_enabled,
                        purchase_price, subscription_enabled, currency,
                        new_status, new_status, video_id,
                    ))

                    save_video_tags(video_id, tags_string, connection)
                    connection.commit()

                    if bunny_video_id:
                        try:
                            bunny_update_video(bunny_video_id, title,
                                               existing['bunny_library_id'])
                        except Exception as bunny_error:
                            app.logger.warning(
                                'KSS video updated, but Bunny title update failed: %s',
                                bunny_error
                            )

                    if is_ajax:
                        return jsonify({
                            'success': True,
                            'video_id': video_id,
                            'edit_url': url_for('admin_create_video',
                                                video_id=video_id),
                            'status': new_status,
                        })

                    if new_status == 'published':
                        flash('Video updated and published.', 'success')
                    else:
                        flash('Video updated successfully.', 'success')
                    return redirect(url_for('admin_create_video',
                                            video_id=video_id))

                # ---------- CREATE NEW ----------
                else:
                    new_status = 'draft'
                    if action == 'save_publish':
                        raise RuntimeError(
                            'Save the video first, upload it to Bunny Stream, '
                            'wait for processing, then publish it.'
                        )

                    cursor.execute("""
                        INSERT INTO videos
                            (channel_id, category_id, title, slug,
                             description, thumbnail_url, bunny_video_id,
                             bunny_library_id, duration_seconds,
                             status, visibility, is_free,
                             rental_enabled, rental_price,
                             rental_duration_hours, purchase_enabled,
                             purchase_price, subscription_enabled,
                             currency, total_views, total_likes,
                             total_dislikes, total_comments,
                             total_watch_seconds, created_at, updated_at)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             0, 0, 0, 0, 0, NOW(), NOW())
                    """, (
                        channel_id, category_id, title, slug, description,
                        thumbnail_url or None, bunny_video_id or None,
                        BUNNY_LIBRARY_ID if bunny_video_id else None,
                        duration_seconds, new_status, visibility, is_free,
                        rental_enabled, rental_price, rental_duration_hours,
                        purchase_enabled, purchase_price,
                        subscription_enabled, currency,
                    ))

                    new_video_id = cursor.lastrowid
                    save_video_tags(new_video_id, tags_string, connection)
                    connection.commit()

                    if is_ajax:
                        return jsonify({
                            'success': True,
                            'video_id': new_video_id,
                            'edit_url': url_for('admin_create_video',
                                                video_id=new_video_id),
                            'status': new_status,
                        })

                    flash('Video draft created successfully.', 'success')
                    return redirect(url_for('admin_create_video',
                                            video_id=new_video_id))

            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.close()

        except Exception as e:
            app.logger.exception('Error saving admin video')
            if is_ajax:
                return jsonify({'success': False, 'message': str(e)}), 500
            flash(str(e), 'error')
            return redirect(request.url)

    channels = fetch_all("""
        SELECT channel_id, channel_name FROM channels
        WHERE is_active = TRUE ORDER BY channel_name ASC
    """)
    categories = fetch_all("""
        SELECT category_id, category_name FROM categories
        WHERE is_active = TRUE ORDER BY category_name ASC
    """)

    return render_template(
        'admin_create_video.html',
        video=video, channels=channels,
        categories=categories, selected_tags=selected_tags,
    )














@app.route('/admin/videos/<int:video_id>/publish', methods=['POST'])
@admin_required
def admin_publish_video(video_id):
    video = fetch_one("""
        SELECT video_id, title, status, bunny_video_id
        FROM videos WHERE video_id = %s AND status <> 'deleted' LIMIT 1
    """, (video_id,))
    if not video:
        flash('Video not found.', 'error')
        return redirect(url_for('admin_videos'))
    if not video['bunny_video_id']:
        flash('This video has not been uploaded to Bunny Stream.', 'error')
        return redirect(url_for('admin_videos'))
    if video['status'] not in {'ready', 'published'}:
        flash('This video is not ready for publishing. Wait for Bunny Stream processing to finish.', 'error')
        return redirect(url_for('admin_videos'))
    execute_query("""
        UPDATE videos
        SET status = 'published', published_at = NOW(), updated_at = NOW()
        WHERE video_id = %s
    """, (video_id,))
    flash('Video published successfully.', 'success')
    return redirect(url_for('admin_videos'))


@app.route('/admin/videos/<int:video_id>/unpublish', methods=['POST'])
@admin_required
def admin_unpublish_video(video_id):
    video = fetch_one("""
        SELECT video_id, status FROM videos
        WHERE video_id = %s AND status <> 'deleted' LIMIT 1
    """, (video_id,))
    if not video:
        flash('Video not found.', 'error')
        return redirect(url_for('admin_videos'))
    execute_query("""
        UPDATE videos
        SET status = 'unpublished', updated_at = NOW()
        WHERE video_id = %s
    """, (video_id,))
    flash('Video unpublished successfully.', 'success')
    return redirect(url_for('admin_videos'))


@app.route('/admin/payments')
@admin_required
def admin_payments():
    payments = fetch_all("""
        SELECT p.*, u.username, u.full_name, u.email
        FROM payments p
        JOIN users u ON u.user_id = p.user_id
        ORDER BY p.created_at DESC
        LIMIT 500
    """)
    return render_template('admin_payments.html', payments=payments)


@app.route('/admin/analytics')
@admin_required
def admin_analytics():
    views_by_day = fetch_all("""
        SELECT DATE(started_at) AS view_date, COUNT(*) AS total_views
        FROM video_views
        WHERE is_valid_view = TRUE
        GROUP BY DATE(started_at)
        ORDER BY view_date DESC
        LIMIT 30
    """)
    revenue_by_day = fetch_all("""
        SELECT DATE(COALESCE(paid_at, created_at)) AS revenue_date,
               COALESCE(SUM(amount), 0) AS revenue
        FROM payments
        WHERE status = 'successful'
        GROUP BY DATE(COALESCE(paid_at, created_at))
        ORDER BY revenue_date DESC
        LIMIT 30
    """)
    top_videos = fetch_all("""
        SELECT v.video_id, v.title, v.total_views,
               v.total_likes, v.total_comments, c.channel_name
        FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        ORDER BY v.total_views DESC
        LIMIT 20
    """)
    video_earnings = fetch_all("""
        SELECT
            v.video_id, v.title, v.slug, v.currency,
            v.purchase_price, v.rental_price, c.channel_name,
            COALESCE(SUM(CASE WHEN p.payment_type = 'video_purchase'
                              THEN p.amount END), 0) AS purchase_revenue,
            COALESCE(SUM(CASE WHEN p.payment_type = 'video_rental'
                              THEN p.amount END), 0) AS rental_revenue,
            COALESCE(SUM(p.amount), 0) AS total_revenue,
            COUNT(DISTINCT CASE WHEN p.payment_type = 'video_purchase'
                                THEN p.payment_id END) AS purchase_count,
            COUNT(DISTINCT CASE WHEN p.payment_type = 'video_rental'
                                THEN p.payment_id END) AS rental_count
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        INNER JOIN payments p
            ON p.related_id = v.video_id
            AND p.payment_type IN ('video_purchase', 'video_rental')
            AND p.status = 'successful'
        GROUP BY v.video_id, v.title, v.slug, v.currency,
                 v.purchase_price, v.rental_price, c.channel_name
        HAVING total_revenue > 0
        ORDER BY total_revenue DESC
        LIMIT 100
    """)
    earnings_summary = fetch_one("""
        SELECT
            COALESCE(SUM(CASE WHEN payment_type = 'video_purchase'
                              THEN amount END), 0) AS purchase_total,
            COALESCE(SUM(CASE WHEN payment_type = 'video_rental'
                              THEN amount END), 0) AS rental_total,
            COUNT(DISTINCT CASE WHEN payment_type = 'video_purchase'
                                THEN payment_id END) AS purchase_count,
            COUNT(DISTINCT CASE WHEN payment_type = 'video_rental'
                                THEN payment_id END) AS rental_count
        FROM payments
        WHERE status = 'successful'
          AND payment_type IN ('video_purchase', 'video_rental')
    """) or {
        'purchase_total': 0, 'rental_total': 0,
        'purchase_count': 0, 'rental_count': 0,
    }
    return render_template(
        'admin_analytics.html',
        views_by_day=views_by_day,
        revenue_by_day=revenue_by_day,
        top_videos=top_videos,
        video_earnings=video_earnings,
        earnings_summary=earnings_summary,
    )


@app.route('/admin/channels/<int:channel_id>/toggle-status', methods=['POST'])
@admin_required
def toggle_channel_status(channel_id):
    channel = fetch_one("""
        SELECT channel_id, channel_name, is_active
        FROM channels WHERE channel_id = %s
    """, (channel_id,))
    if not channel:
        return ('Channel not found.', 404)
    new_status = not bool(channel['is_active'])
    execute_query("""
        UPDATE channels
        SET is_active = %s, updated_at = CURRENT_TIMESTAMP
        WHERE channel_id = %s
    """, (new_status, channel_id))
    return redirect(url_for('admin_channels'))


# ============================================================
# PAYMENT INIT / CALLBACK / WEBHOOK
# ============================================================

@app.route("/api/payment/initialize", methods=["POST"])
@login_required
def initialize_payment():
    if not PAYSTACK_SECRET_KEY:
        return jsonify({
            "success": False,
            "message": "Payments are not configured yet. Add PAYSTACK_SECRET_KEY to .env."
        }), 503

    user_id = session["user_id"]
    user = fetch_one("SELECT * FROM users WHERE user_id = %s LIMIT 1", (user_id,))
    if not user:
        return jsonify({"success": False, "message": "User account not found."}), 404

    data = request.get_json(silent=True) or {}
    payment_type = data.get("payment_type")
    video_id = data.get("video_id")
    channel_id = data.get("channel_id")

    allowed_types = {"video_purchase", "video_rental", "channel_subscription"}
    if payment_type not in allowed_types:
        return jsonify({"success": False, "message": "Invalid payment type."}), 400

    amount = None
    currency = "GHS"
    related_id = None
    metadata = {}

    if payment_type == "video_purchase":
        if not video_id:
            return jsonify({"success": False, "message": "Video ID is required."}), 400
        video = fetch_one("""
            SELECT * FROM videos
            WHERE video_id = %s AND status = 'published' LIMIT 1
        """, (video_id,))
        if not video:
            return jsonify({"success": False, "message": "Video not found."}), 404
        if not video["purchase_enabled"]:
            return jsonify({"success": False, "message": "Purchase is not available."}), 400
        if has_video_access(user_id, video):
            return jsonify({
                "success": True, "already_has_access": True,
                "redirect_url": url_for("watch", slug=video["slug"]),
            })
        amount = float(video["purchase_price"])
        currency = video["currency"] or "GHS"
        related_id = video["video_id"]
        metadata = {"payment_type": "video_purchase", "user_id": user_id,
                    "video_id": video["video_id"]}

    elif payment_type == "video_rental":
        if not video_id:
            return jsonify({"success": False, "message": "Video ID is required."}), 400
        video = fetch_one("""
            SELECT * FROM videos
            WHERE video_id = %s AND status = 'published' LIMIT 1
        """, (video_id,))
        if not video:
            return jsonify({"success": False, "message": "Video not found."}), 404
        if not video["rental_enabled"]:
            return jsonify({"success": False, "message": "Rental is not available."}), 400
        access_type = get_video_access_type(user_id, video)
        if access_type in ("free", "admin", "purchase", "rental", "subscription"):
            return jsonify({
                "success": True, "already_has_access": True,
                "redirect_url": url_for("watch", slug=video["slug"]),
            })
        amount = float(video["rental_price"])
        currency = video["currency"] or "GHS"
        related_id = video["video_id"]
        metadata = {"payment_type": "video_rental", "user_id": user_id,
                    "video_id": video["video_id"]}

    elif payment_type == "channel_subscription":
        if not channel_id:
            return jsonify({"success": False, "message": "Channel ID is required."}), 400
        channel = fetch_one("""
            SELECT * FROM channels
            WHERE channel_id = %s AND is_active = 1 LIMIT 1
        """, (channel_id,))
        if not channel:
            return jsonify({"success": False, "message": "Channel not found."}), 404
        existing = fetch_one("""
            SELECT subscription_id FROM channel_subscriptions
            WHERE user_id = %s AND channel_id = %s
              AND status = 'active' AND expires_at > NOW()
            LIMIT 1
        """, (user_id, channel_id))
        if existing:
            return jsonify({
                "success": True, "already_subscribed": True,
                "redirect_url": url_for("channel", slug=channel["slug"]),
            })
        amount = CHANNEL_SUBSCRIPTION_PRICE
        currency = "GHS"
        related_id = channel["channel_id"]
        metadata = {"payment_type": "channel_subscription",
                    "user_id": user_id, "channel_id": channel["channel_id"]}

    payment_reference = generate_payment_reference(payment_type, user_id)

    try:
        execute_query("""
            INSERT INTO payments
                (user_id, payment_reference, payment_type, related_id,
                 amount, currency, provider, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'paystack', 'pending')
        """, (user_id, payment_reference, payment_type,
              related_id, amount, currency))

        if payment_type == "video_purchase":
            execute_query("""
                INSERT INTO video_purchases
                    (user_id, video_id, amount, currency,
                     payment_reference, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
            """, (user_id, video_id, amount, currency, payment_reference))

        elif payment_type == "video_rental":
            execute_query("""
                INSERT INTO video_rentals
                    (user_id, video_id, amount, currency,
                     rental_duration_hours, payment_reference, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            """, (user_id, video_id, amount, currency,
                  VIDEO_RENTAL_DURATION_HOURS, payment_reference))

        elif payment_type == "channel_subscription":
            execute_query("""
                INSERT INTO channel_subscriptions
                    (user_id, channel_id, amount, currency,
                     payment_reference, status, auto_renew)
                VALUES (%s, %s, %s, %s, %s, 'pending', 0)
            """, (user_id, channel_id, amount, currency, payment_reference))

        paystack_data = paystack_initialize(
            email=user["email"], amount=amount, currency=currency,
            reference=payment_reference, metadata=metadata
        )
        paystack_reference = paystack_data.get("reference", payment_reference)
        if paystack_reference != payment_reference:
            raise RuntimeError('Paystack returned an unexpected payment reference.')

        execute_query("""
            UPDATE payments SET paystack_reference = %s, updated_at = NOW()
            WHERE payment_reference = %s
        """, (paystack_reference, payment_reference))

        return jsonify({
            "success": True,
            "authorization_url": paystack_data["authorization_url"],
            "access_code": paystack_data.get("access_code"),
            "reference": paystack_reference,
        })

    except Exception as e:
        app.logger.exception("Payment initialization failed")
        execute_query("""
            UPDATE payments SET status = 'failed',
                gateway_response = %s, updated_at = NOW()
            WHERE payment_reference = %s
        """, (str(e), payment_reference))

        if payment_type == "video_purchase":
            execute_query("""
                UPDATE video_purchases SET status = 'failed', updated_at = NOW()
                WHERE payment_reference = %s
            """, (payment_reference,))
        elif payment_type == "video_rental":
            execute_query("""
                UPDATE video_rentals SET status = 'failed', updated_at = NOW()
                WHERE payment_reference = %s
            """, (payment_reference,))
        elif payment_type == "channel_subscription":
            execute_query("""
                UPDATE channel_subscriptions SET status = 'failed'
                WHERE payment_reference = %s
            """, (payment_reference,))

        return jsonify({
            "success": False,
            "message": "Unable to initialize payment. Please try again."
        }), 500


def payment_success_destination(payment):
    if payment['payment_type'] in ('video_purchase', 'video_rental'):
        video = fetch_one(
            'SELECT slug FROM videos WHERE video_id = %s LIMIT 1',
            (payment['related_id'],)
        )
        if video:
            return url_for('watch', slug=video['slug'])
    elif payment['payment_type'] == 'channel_subscription':
        channel = fetch_one(
            'SELECT slug FROM channels WHERE channel_id = %s LIMIT 1',
            (payment['related_id'],)
        )
        if channel:
            return url_for('channel', slug=channel['slug'])
    return url_for('dashboard')


def validate_paystack_transaction(payment, transaction):
    if transaction.get('status') != 'success':
        raise ValueError('Paystack did not report a successful transaction.')
    if transaction.get('reference') != payment['payment_reference']:
        raise ValueError('Paystack reference does not match the pending payment.')
    if int(transaction.get('amount') or 0) != money_to_subunit(payment['amount']):
        raise ValueError('Paystack amount does not match the pending payment.')
    currency = (transaction.get('currency') or '').upper()
    if currency != (payment['currency'] or '').upper():
        raise ValueError('Paystack currency does not match the pending payment.')


def confirm_paystack_payment(reference):
    payment = fetch_one(
        'SELECT * FROM payments WHERE payment_reference = %s LIMIT 1',
        (reference,)
    )
    if not payment:
        raise LookupError('Unknown payment reference.')
    if payment['status'] == 'successful':
        return payment
    if payment['status'] != 'pending':
        raise ValueError('This payment is no longer pending.')

    verification = paystack_verify(reference)
    if not verification.get('status'):
        raise ValueError(verification.get('message', 'Payment verification failed.'))
    transaction = verification.get('data') or {}
    validate_paystack_transaction(payment, transaction)

    connection = get_db_connection()
    if not connection:
        raise RuntimeError('Unable to connect to the payment database.')
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            'SELECT * FROM payments WHERE payment_reference = %s FOR UPDATE',
            (reference,)
        )
        locked_payment = cursor.fetchone()
        if not locked_payment:
            raise LookupError('Unknown payment reference.')
        if locked_payment['status'] == 'successful':
            connection.commit()
            return locked_payment
        if locked_payment['status'] != 'pending':
            raise ValueError('This payment is no longer pending.')

        cursor.execute("""
            UPDATE payments
            SET status = 'successful', paystack_reference = %s, channel = %s,
                gateway_response = %s, paid_at = NOW(), updated_at = NOW()
            WHERE payment_reference = %s AND status = 'pending'
        """, (transaction['reference'], transaction.get('channel'),
              json.dumps(transaction, default=str), reference))

        if locked_payment['payment_type'] == 'video_purchase':
            cursor.execute("""
                UPDATE video_purchases
                SET status = 'paid',
                    purchased_at = COALESCE(purchased_at, NOW()),
                    updated_at = NOW()
                WHERE payment_reference = %s AND status = 'pending'
            """, (reference,))
        elif locked_payment['payment_type'] == 'video_rental':
            cursor.execute("""
                UPDATE video_rentals
                SET status = 'active', rented_at = NOW(),
                    expires_at = DATE_ADD(NOW(), INTERVAL %s HOUR),
                    updated_at = NOW()
                WHERE payment_reference = %s AND status = 'pending'
            """, (VIDEO_RENTAL_DURATION_HOURS, reference))
        elif locked_payment['payment_type'] == 'channel_subscription':
            cursor.execute("""
                UPDATE channel_subscriptions
                SET status = 'active', started_at = NOW(),
                    expires_at = DATE_ADD(NOW(), INTERVAL %s DAY)
                WHERE payment_reference = %s AND status = 'pending'
            """, (CHANNEL_SUBSCRIPTION_DAYS, reference))
            if cursor.rowcount:
                cursor.execute("""
                    UPDATE channels
                    SET subscriber_count = subscriber_count + 1,
                        updated_at = NOW()
                    WHERE channel_id = %s
                """, (locked_payment['related_id'],))
        else:
            raise ValueError('Unsupported payment type.')

        connection.commit()
        locked_payment['status'] = 'successful'
        return locked_payment
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


@app.route('/payment/webhook', methods=['POST'])
def paystack_webhook():
    if not is_valid_paystack_webhook():
        return jsonify({'success': False, 'message': 'Invalid Paystack signature.'}), 401
    event = request.get_json(silent=True) or {}
    if event.get('event') != 'charge.success':
        return jsonify({'success': True}), 200
    reference = (event.get('data') or {}).get('reference')
    if not reference:
        return jsonify({'success': False, 'message': 'Missing payment reference.'}), 400
    try:
        confirm_paystack_payment(reference)
    except Exception:
        app.logger.exception('Paystack webhook confirmation failed for %s', reference)
        return jsonify({'success': False}), 500
    return jsonify({'success': True}), 200


@app.route("/payment/callback")
def payment_callback():
    reference = request.args.get("reference")
    if not reference:
        return redirect(url_for("payment_failed"))

    payment = fetch_one(
        'SELECT * FROM payments WHERE payment_reference = %s LIMIT 1',
        (reference,)
    )
    if not payment:
        return redirect(url_for("payment_failed"))

    if payment["status"] == "successful":
        return redirect(payment_success_destination(payment))

    try:
        confirmed_payment = confirm_paystack_payment(reference)
        return redirect(payment_success_destination(confirmed_payment))
    except Exception:
        app.logger.exception('Payment callback confirmation failed for %s', reference)
        return redirect(url_for('payment_failed', reference=reference))


@app.route("/payment/failed")
def payment_failed():
    reference = request.args.get("reference")
    return render_template("payment_failed.html", reference=reference)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route('/health')
def health():
    connection = get_db_connection()
    if connection:
        try:
            connection.close()
            return jsonify({'status': 'ok', 'database': 'connected'})
        except Exception:
            pass
    return jsonify({'status': 'error', 'database': 'unavailable'}), 500


# ============================================================
# ENTRY POINT (dev only — use gunicorn in production)
# ============================================================


# Warm the pool at import time (gunicorn imports this module once per worker).
init_pool()


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', '5003')),
        debug=False,
    )



if __name__ == '__main__':
    # NEVER use debug=True in production.
    # Run with gunicorn instead. This app is I/O-bound (waiting on MySQL and
    # on Bunny/Paystack HTTP calls), so threads help a lot per worker:
    #   gunicorn -w <2*CPU_CORES+1> -k gthread --threads 8 --timeout 60 \
    #            --max-requests 1000 --max-requests-jitter 100 \
    #            -b 0.0.0.0:5003 app:app
    #
    # --max-requests recycles workers periodically, which caps the damage
    # from any slow memory growth over a long high-traffic session.
    #
    # Remember: DB_POOL_SIZE is PER WORKER. Check that
    # DB_POOL_SIZE * worker_count stays comfortably under your MySQL plan's
    # max_connections before you raise worker count for a launch.
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', '5003')),
        debug=False,
    )