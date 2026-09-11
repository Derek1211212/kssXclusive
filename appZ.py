import os

import hashlib
import hmac

from datetime import datetime, timedelta

from functools import wraps

import time

import re

import mysql.connector

from mysql.connector import Error

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort

from werkzeug.security import generate_password_hash, check_password_hash

from dotenv import load_dotenv

import requests
import certifi
import json
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import cloudinary
import cloudinary.uploader

load_dotenv()

app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-secret-key-in-production')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

DB_CONFIG = {'host': os.getenv('DB_HOST', 'localhost'), 'port': int(os.getenv('DB_PORT', '3306')), 'user': os.getenv('DB_USER', 'root'), 'password': os.getenv('DB_PASSWORD', ''), 'database': os.getenv('DB_NAME', 'kss')}

BUNNY_API_BASE = 'https://video.bunnycdn.com'

BUNNY_TUS_ENDPOINT = 'https://video.bunnycdn.com/tusupload'

BUNNY_API_KEY = os.getenv('BUNNY_API_KEY', '')

BUNNY_LIBRARY_ID = os.getenv('BUNNY_LIBRARY_ID', '')

BUNNY_CDN_HOSTNAME = os.getenv('BUNNY_CDN_HOSTNAME', '')

PAYSTACK_SECRET_KEY = os.getenv('PAYSTACK_SECRET_KEY', '')

PAYSTACK_PUBLIC_KEY = os.getenv('PAYSTACK_PUBLIC_KEY', '')

# Every paid video rental lasts exactly 48 hours.  Prices remain per-video
# settings managed by an administrator.
VIDEO_RENTAL_DURATION_HOURS = 48

# Customer channel subscription settings
CHANNEL_SUBSCRIPTION_PRICE = float(
    os.getenv('KSS_CHANNEL_SUBSCRIPTION_PRICE', '50.00')
)

CHANNEL_SUBSCRIPTION_DAYS = int(
    os.getenv('KSS_CHANNEL_SUBSCRIPTION_DAYS', '30')
)

def bunny_headers():
    return {'AccessKey': BUNNY_API_KEY, 'Content-Type': 'application/json'}

def bunny_create_video(title):
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not BUNNY_LIBRARY_ID:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')

    url = f'{BUNNY_API_BASE}/library/{BUNNY_LIBRARY_ID}/videos'

    session = requests.Session()
    session.trust_env = False

    response = session.post(
        url,
        headers=bunny_headers(),
        json={'title': title},
        timeout=30,
        verify=certifi.where()
    )

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
    response = requests.get(url, headers={'AccessKey': BUNNY_API_KEY}, timeout=30, verify=certifi.where())
    if not response.ok:
        raise RuntimeError(f'Bunny video lookup failed: {response.status_code} {response.text}')
    return response.json()

def bunny_update_video(video_id, title, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not library_id:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')
    url = f'{BUNNY_API_BASE}/library/{library_id}/videos/{video_id}'
    response = requests.post(url, headers=bunny_headers(), json={'title': title}, timeout=30, verify=certifi.where())
    if not response.ok:
        raise RuntimeError(f'Bunny video update failed: {response.status_code} {response.text}')
    return response.json()

def bunny_delete_video(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    url = f'{BUNNY_API_BASE}/library/{library_id}/videos/{video_id}'
    response = requests.delete(url, headers={'AccessKey': BUNNY_API_KEY}, timeout=30, verify=certifi.where())
    if not response.ok:
        raise RuntimeError(f'Bunny video deletion failed: {response.status_code} {response.text}')
    return True

def bunny_create_upload_signature(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    if not BUNNY_API_KEY:
        raise RuntimeError('BUNNY_API_KEY is not configured.')
    if not library_id:
        raise RuntimeError('BUNNY_LIBRARY_ID is not configured.')
    expiration_time = int(time.time()) + 3600
    signature_string = str(library_id) + str(BUNNY_API_KEY) + str(expiration_time) + str(video_id)
    signature = hashlib.sha256(signature_string.encode('utf-8')).hexdigest()
    # Key names below match what admin/create_video.html's getBunnyUploadAuth()
    # actually reads (tus_endpoint / bunny_video_id) - the old names
    # ("endpoint" / "video_id") silently produced undefined values client-side.
    return {'tus_endpoint': BUNNY_TUS_ENDPOINT, 'library_id': str(library_id), 'bunny_video_id': str(video_id), 'signature': signature, 'expiration': expiration_time}

def bunny_status_to_kss_status(bunny_status):
    status_map = {0: 'processing', 1: 'processing', 2: 'processing', 3: 'processing', 4: 'ready', 5: 'failed', 6: 'failed'}
    return status_map.get(bunny_status, 'processing')




# ============================================================
# PAYSTACK HELPERS
# ============================================================

def paystack_headers():
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }


def money_to_subunit(amount):
    """
    Paystack expects amounts in the smallest currency unit.
    For GHS 50.00 -> 5000 pesewas.
    """
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
        "channel_subscription": "SUB"
    }.get(payment_type, "PAY")

    return f"KSS-{short_type}-{user_id}-{uuid.uuid4().hex[:12].upper()}"


def paystack_initialize(
    email,
    amount,
    currency,
    reference,
    metadata
):
    """
    Initialize a Paystack transaction.
    """

    if not PAYSTACK_SECRET_KEY:
        raise Exception("PAYSTACK_SECRET_KEY is not configured.")

    payload = {
        "email": email,
        "amount": money_to_subunit(amount),
        "currency": currency,
        "reference": reference,
        "callback_url": url_for(
            "payment_callback",
            _external=True
        ),
        "metadata": metadata
    }

    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers=paystack_headers(),
        json=payload,
        timeout=30
    )

    if not response.ok:
        raise Exception(
            f"Paystack initialization failed: "
            f"{response.status_code} {response.text}"
        )

    result = response.json()

    if not result.get("status"):
        raise Exception(
            result.get("message", "Unable to initialize payment.")
        )

    return result["data"]


def is_valid_paystack_webhook():
    """Validate Paystack's SHA-512 signature against the unmodified body."""
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
    """
    Verify a transaction directly with Paystack.
    """

    if not PAYSTACK_SECRET_KEY:
        raise Exception("PAYSTACK_SECRET_KEY is not configured.")

    response = requests.get(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers=paystack_headers(),
        timeout=30
    )

    if not response.ok:
        raise Exception(
            f"Paystack verification failed: "
            f"{response.status_code} {response.text}"
        )

    result = response.json()

    return result









def get_db_connection():
    """
    Create and return a new MySQL connection.
    """
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f'Database connection error: {e}')
    return None

def fetch_one(query, params=None):
    """
    Execute SELECT query and return one row as dictionary.
    """
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
        if cursor:
            cursor.close()
        connection.close()

def fetch_all(query, params=None):
    """
    Execute SELECT query and return all rows as dictionaries.
    """
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
        if cursor:
            cursor.close()
        connection.close()

def execute_query(query, params=None, return_lastrowid=False):
    """
    Execute INSERT / UPDATE / DELETE query.
    """
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
        connection.rollback()
        print(f'Database execute error: {e}')
        return None
    finally:
        if cursor:
            cursor.close()
        connection.close()

def hash_password(password):
    return generate_password_hash(password)

def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)

def get_current_user():
    """
    Return currently logged-in user.
    """
    user_id = session.get('user_id')
    if not user_id:
        return None
    return fetch_one('\n        SELECT\n            user_id,\n            full_name,\n            username,\n            email,\n            phone,\n            profile_image,\n            role,\n            is_active,\n            email_verified,\n            phone_verified,\n            last_login_at,\n            created_at\n        FROM users\n        WHERE user_id = %s\n        LIMIT 1\n        ', (user_id,))

def login_user(user):
    """
    Store user information in session.
    """
    session.clear()
    session.permanent = True
    session['user_id'] = user['user_id']
    session['role'] = user['role']
    session['username'] = user['username']
    session['email'] = user['email']

def logout_user():
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

def has_video_access(user_id, video):
    """
    Determine whether a user is allowed to watch a video.

    Access types:

    1. Free video
    2. Purchased video
    3. Active rental
    4. Active paid channel subscription

    Subscription rule:
    The subscription grants access to subscription-enabled videos
    published during the active subscription period.
    """
    if not video:
        return False
    if video.get('is_free'):
        return True
    if not user_id:
        return False
    user = fetch_one('\n        SELECT role\n        FROM users\n        WHERE user_id = %s\n        LIMIT 1\n        ', (user_id,))
    if user and user['role'] == 'admin':
        return True
    purchase = fetch_one("\n        SELECT purchase_id\n        FROM video_purchases\n        WHERE user_id = %s\n          AND video_id = %s\n          AND status = 'paid'\n        LIMIT 1\n        ", (user_id, video['video_id']))
    if purchase:
        return True
    rental = fetch_one("\n        SELECT rental_id\n        FROM video_rentals\n        WHERE user_id = %s\n          AND video_id = %s\n          AND status = 'active'\n          AND expires_at > NOW()\n        LIMIT 1\n        ", (user_id, video['video_id']))
    if rental:
        return True
    if video.get('subscription_enabled'):
        subscription = fetch_one("\n            SELECT subscription_id\n            FROM channel_subscriptions\n            WHERE user_id = %s\n              AND channel_id = %s\n              AND status = 'active'\n              AND NOW() <= expires_at\n              AND started_at <= %s\n              AND expires_at >= %s\n            LIMIT 1\n            ", (user_id, video['channel_id'], video['published_at'], video['published_at']))
        if subscription:
            return True
    return False

def get_video_access_type(user_id, video):
    """
    Return the reason the user has access to a video.
    """
    if not video:
        return None
    if video.get('is_free'):
        return 'free'
    if not user_id:
        return None
    user = fetch_one('\n        SELECT role\n        FROM users\n        WHERE user_id = %s\n        LIMIT 1\n        ', (user_id,))
    if user and user['role'] == 'admin':
        return 'admin'
    purchase = fetch_one("\n        SELECT purchase_id\n        FROM video_purchases\n        WHERE user_id = %s\n          AND video_id = %s\n          AND status = 'paid'\n        LIMIT 1\n        ", (user_id, video['video_id']))
    if purchase:
        return 'purchase'
    rental = fetch_one("\n        SELECT rental_id, expires_at\n        FROM video_rentals\n        WHERE user_id = %s\n          AND video_id = %s\n          AND status = 'active'\n          AND expires_at > NOW()\n        ORDER BY expires_at DESC\n        LIMIT 1\n        ", (user_id, video['video_id']))
    if rental:
        return 'rental'
    if video.get('subscription_enabled'):
        subscription = fetch_one("\n            SELECT subscription_id\n            FROM channel_subscriptions\n            WHERE user_id = %s\n              AND channel_id = %s\n              AND status = 'active'\n              AND NOW() <= expires_at\n              AND started_at <= %s\n              AND expires_at >= %s\n            LIMIT 1\n            ", (user_id, video['channel_id'], video['published_at'], video['published_at']))
        if subscription:
            return 'subscription'
    return None


def annotate_video_lock_status(videos, user_id):
    """Add an ``is_locked`` field to video cards without doing one query/card."""
    if not videos:
        return videos

    paid_ids = set()
    protected_ids = [
        video['video_id'] for video in videos if not video.get('is_free')
    ]

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

def generate_video_slug(title, video_id=None):
    """
    Generate a unique URL slug for a video.
    """
    base_slug = re.sub('[^a-z0-9]+', '-', title.lower().strip()).strip('-')
    if not base_slug:
        base_slug = 'video'
    slug = base_slug
    counter = 2
    while True:
        if video_id:
            existing = fetch_one('\n                SELECT video_id\n                FROM videos\n                WHERE slug = %s\n                  AND video_id <> %s\n                LIMIT 1\n                ', (slug, video_id))
        else:
            existing = fetch_one('\n                SELECT video_id\n                FROM videos\n                WHERE slug = %s\n                LIMIT 1\n                ', (slug,))
        if not existing:
            return slug
        slug = f'{base_slug}-{counter}'
        counter += 1

def save_video_tags(video_id, tags_string, connection):
    """
    Saves tags and video_tags relationships.

    tags_string example:
        Ghana, finance, cocoa, education
    """
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute('\n            DELETE FROM video_tags\n            WHERE video_id = %s\n            ', (video_id,))
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
            cursor.execute('\n                SELECT tag_id\n                FROM tags\n                WHERE slug = %s\n                LIMIT 1\n                ', (tag_slug,))
            tag_row = cursor.fetchone()
            if tag_row:
                tag_id = tag_row['tag_id']
            else:
                cursor.execute('\n                    INSERT INTO tags\n                    (\n                        tag_name,\n                        slug,\n                        created_at\n                    )\n                    VALUES\n                    (\n                        %s,\n                        %s,\n                        NOW()\n                    )\n                    ', (tag_name, tag_slug))
                tag_id = cursor.lastrowid
            cursor.execute('\n                INSERT IGNORE INTO video_tags\n                (\n                    video_id,\n                    tag_id\n                )\n                VALUES\n                (\n                    %s,\n                    %s\n                )\n                ', (video_id, tag_id))
    finally:
        cursor.close()

@app.errorhandler(404)
def page_not_found(error):
    return (render_template('404.html'), 404)

@app.errorhandler(500)
def internal_server_error(error):
    return (render_template('500.html'), 500)

@app.before_request
def load_logged_in_user():
    user_id = session.get('user_id')
    if user_id:
        user = fetch_one('\n            SELECT\n                user_id,\n                full_name,\n                username,\n                email,\n                profile_image,\n                role,\n                is_active\n            FROM users\n            WHERE user_id = %s\n            LIMIT 1\n            ', (user_id,))
        if user and user['is_active']:
            pass
        else:
            session.clear()

@app.context_processor
def inject_global_variables():
    user = get_current_user()
    return {'current_user': user, 'logged_in': bool(user), 'current_year': datetime.now().year}




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






@app.route('/admin/api/bunny/create-video', methods=['POST'])
@admin_required
def admin_bunny_create_video():
    data = request.get_json(silent=True) or {}
    title = data.get('title', '').strip()
    video_id = data.get('video_id')
    if not title:
        return (jsonify({'success': False, 'message': 'Video title is required.'}), 400)
    if not video_id:
        return (jsonify({'success': False, 'message': 'KSS video ID is required.'}), 400)
    try:
        video_id = int(video_id)
    except (TypeError, ValueError):
        return (jsonify({'success': False, 'message': 'Invalid KSS video ID.'}), 400)
    video = fetch_one("\n        SELECT\n            video_id,\n            title,\n            bunny_video_id\n        FROM videos\n        WHERE video_id = %s\n          AND status <> 'deleted'\n        LIMIT 1\n        ", (video_id,))
    if not video:
        return (jsonify({'success': False, 'message': 'KSS video not found.'}), 404)
    if video['bunny_video_id']:
        return jsonify({'success': True, 'already_exists': True, 'bunny_video_id': video['bunny_video_id']})
    try:
        bunny_video = bunny_create_video(title)
        bunny_video_id = bunny_video.get('guid') or bunny_video.get('videoId')
        if not bunny_video_id:
            raise RuntimeError('Bunny did not return a Video ID.')
        execute_query("\n            UPDATE videos\n            SET\n                bunny_video_id = %s,\n                bunny_library_id = %s,\n                status = 'processing',\n                updated_at = NOW()\n            WHERE video_id = %s\n            ", (bunny_video_id, BUNNY_LIBRARY_ID, video_id))
        return jsonify({'success': True, 'already_exists': False, 'bunny_video_id': bunny_video_id})
    except Exception as e:
        app.logger.exception('Bunny video creation failed')
        return (jsonify({'success': False, 'message': str(e)}), 500)

@app.route('/admin/api/bunny/upload-auth', methods=['POST'])
@admin_required
def admin_bunny_upload_auth():
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    if not video_id:
        return (jsonify({'success': False, 'message': 'Video ID is required.'}), 400)
    try:
        video_id = int(video_id)
    except (TypeError, ValueError):
        return (jsonify({'success': False, 'message': 'Invalid video ID.'}), 400)
    video = fetch_one("\n        SELECT\n            video_id,\n            bunny_video_id,\n            bunny_library_id\n        FROM videos\n        WHERE video_id = %s\n          AND status <> 'deleted'\n        LIMIT 1\n        ", (video_id,))
    if not video:
        return (jsonify({'success': False, 'message': 'Video not found.'}), 404)
    if not video['bunny_video_id']:
        return (jsonify({'success': False, 'message': 'A Bunny video has not been created yet.'}), 400)
    try:
        upload = bunny_create_upload_signature(
            video['bunny_video_id'],
            video.get('bunny_library_id')
        )
        return jsonify({'success': True, **upload})
    except Exception as e:
        app.logger.exception('Bunny upload authorization failed')
        return (jsonify({'success': False, 'message': str(e)}), 500)

@app.route('/admin/api/bunny/status/<int:video_id>')
@admin_required
def admin_bunny_status(video_id):
    video = fetch_one("\n        SELECT\n            video_id,\n            bunny_video_id,\n            bunny_library_id,\n            status\n        FROM videos\n        WHERE video_id = %s\n          AND status <> 'deleted'\n        LIMIT 1\n        ", (video_id,))
    if not video:
        return (jsonify({'success': False, 'message': 'Video not found.'}), 404)
    if not video['bunny_video_id']:
        return jsonify({'success': True, 'connected': False, 'kss_status': video['status']})
    try:
        bunny = bunny_get_video(video['bunny_video_id'], video.get('bunny_library_id'))
        bunny_status = bunny.get('status')
        kss_status = bunny_status_to_kss_status(bunny_status)
        duration = bunny.get('length') or 0
        encode_progress = bunny.get('encodeProgress') or 0
        if video['status'] != 'published':
            execute_query('\n                UPDATE videos\n                SET\n                    status = %s,\n                    duration_seconds = %s,\n                    updated_at = NOW()\n                WHERE video_id = %s\n                ', (kss_status, duration, video_id))
        thumbnail_url = None
        if BUNNY_CDN_HOSTNAME:
            thumbnail_url = 'https://' + BUNNY_CDN_HOSTNAME + '/' + video['bunny_video_id'] + '/thumbnail.jpg'
        return jsonify({'success': True, 'connected': True, 'bunny_video_id': video['bunny_video_id'], 'bunny_status': bunny_status, 'encode_progress': encode_progress, 'duration_seconds': duration, 'kss_status': kss_status, 'thumbnail_url': thumbnail_url})
    except Exception as e:
        app.logger.exception('Bunny status check failed')
        return (jsonify({'success': False, 'message': str(e)}), 500)

@app.route('/admin/api/bunny/delete/<int:video_id>', methods=['POST'])
@admin_required
def admin_bunny_delete(video_id):
    video = fetch_one('\n        SELECT\n            video_id,\n            bunny_video_id,\n            bunny_library_id\n        FROM videos\n        WHERE video_id = %s\n        LIMIT 1\n        ', (video_id,))
    if not video:
        return (jsonify({'success': False, 'message': 'Video not found.'}), 404)
    if not video['bunny_video_id']:
        return jsonify({'success': True})
    try:
        bunny_delete_video(video['bunny_video_id'], video.get('bunny_library_id'))
        execute_query("\n            UPDATE videos\n            SET\n                bunny_video_id = NULL,\n                bunny_library_id = NULL,\n                duration_seconds = NULL,\n                thumbnail_url = NULL,\n                status = 'draft',\n                updated_at = NOW()\n            WHERE video_id = %s\n            ", (video_id,))
        return jsonify({'success': True})
    except Exception as e:
        app.logger.exception('Bunny video deletion failed')
        return (jsonify({'success': False, 'message': str(e)}), 500)

@app.route("/")
def index():

    featured = fetch_all("""
        SELECT
            v.*,
            c.channel_name,
            c.slug AS channel_slug,
            c.channel_logo
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.status = 'published'
          AND v.visibility IN ('public', 'unlisted')
        ORDER BY v.created_at DESC
        LIMIT 6
    """)

    latest = fetch_all("""
        SELECT
            v.*,
            c.channel_name,
            c.slug AS channel_slug,
            c.channel_logo
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.status = 'published'
          AND v.visibility IN ('public', 'unlisted')
        ORDER BY RAND()
        LIMIT 18
    """)

    categories = fetch_all("""
        SELECT
            category_id,
            category_name,
            slug,
            description
        FROM categories
        WHERE is_active = 1
        ORDER BY category_name ASC
    """)

    popular_channels = fetch_all("""
        SELECT
            channel_id,
            channel_name,
            slug,
            channel_logo,
            channel_banner,
            subscriber_count,
            total_views,
            total_videos
        FROM channels
        WHERE is_active = 1
        ORDER BY subscriber_count DESC, total_views DESC
        LIMIT 8
    """)

    user_id = session.get('user_id')
    annotate_video_lock_status(featured, user_id)
    annotate_video_lock_status(latest, user_id)

    return render_template(
        "index.html",
        featured=featured,
        videos=latest,
        categories=categories,
        popular_channels=popular_channels
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

        # Basic validation
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

        # Allow letters, numbers, underscores and dots
        if not re.match(r'^[A-Za-z0-9_.]+$', username):
            flash(
                'Username can only contain letters, numbers, underscores and dots.',
                'danger'
            )
            return render_template('register.html')

        if not email:
            flash('Email address is required.', 'danger')
            return render_template('register.html')

        # Basic email validation
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

        # Check duplicate email, username or phone
        existing = fetch_one(
            '''
            SELECT
                user_id,
                email,
                username,
                phone
            FROM users
            WHERE email = %s
               OR username = %s
               OR (%s IS NOT NULL AND phone = %s)
            LIMIT 1
            ''',
            (
                email,
                username,
                phone or None,
                phone or None
            )
        )

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

        # Hash password
        password_hash = hash_password(password)

        # Create account
        user_id = execute_query(
            '''
            INSERT INTO users
            (
                full_name,
                username,
                email,
                phone,
                password_hash,
                role,
                is_active,
                email_verified,
                phone_verified
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                'user',
                TRUE,
                FALSE,
                FALSE
            )
            ''',
            (
                full_name,
                username,
                email,
                phone or None,
                password_hash
            ),
            return_lastrowid=True
        )

        if not user_id:
            flash(
                'Unable to create your account. Please try again.',
                'danger'
            )
            return render_template('register.html')

        # Get newly created user
        user = fetch_one(
            '''
            SELECT *
            FROM users
            WHERE user_id = %s
            LIMIT 1
            ''',
            (user_id,)
        )

        if not user:
            flash(
                'Your account was created, but we could not complete the login.',
                'warning'
            )
            return redirect(url_for('login'))

        # Log the new user in
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
        user = fetch_one('\n            SELECT *\n            FROM users\n            WHERE email = %s\n               OR username = %s\n            LIMIT 1\n            ', (identifier.lower(), identifier))
        if not user:
            flash('Invalid login details.', 'danger')
            return render_template('login.html')
        if not user['is_active']:
            flash('Your account is inactive.', 'danger')
            return render_template('login.html')
        if not verify_password(user['password_hash'], password):
            flash('Invalid login details.', 'danger')
            return render_template('login.html')
        execute_query('\n            UPDATE users\n            SET last_login_at = NOW()\n            WHERE user_id = %s\n            ', (user['user_id'],))
        login_user(user)
        next_url = request.args.get('next')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('index'))
    return render_template('login.html')

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

    page = request.args.get(
        "page",
        1,
        type=int
    )

    page = max(page, 1)

    per_page = 24

    search_query = request.args.get(
        "q",
        "",
        type=str
    ).strip()

    category_id = request.args.get(
        "category",
        "",
        type=str
    ).strip()

    deal_type = request.args.get(
        "type",
        "",
        type=str
    ).strip()

    offset = (page - 1) * per_page

    conditions = [
        "v.status = 'published'",
        "v.visibility IN ('public', 'unlisted')"
    ]

    params = []

    if search_query:

        terms = [
            term.strip()
            for term in search_query.split()
            if term.strip()
        ]

        for term in terms:

            conditions.append("""
                (
                    v.title LIKE %s
                    OR v.description LIKE %s
                    OR c.channel_name LIKE %s
                    OR cat.category_name LIKE %s
                    OR EXISTS (
                        SELECT 1
                        FROM video_tags vt2
                        JOIN tags t2
                            ON vt2.tag_id = t2.tag_id
                        WHERE vt2.video_id = v.video_id
                          AND t2.tag_name LIKE %s
                    )
                )
            """)

            pattern = f"%{term}%"

            params.extend([
                pattern,
                pattern,
                pattern,
                pattern,
                pattern
            ])

    if category_id:

        conditions.append(
            "v.category_id = %s"
        )

        params.append(category_id)

    if deal_type == "free":

        conditions.append(
            "v.is_free = 1"
        )

    elif deal_type == "purchase":

        conditions.append(
            "v.purchase_enabled = 1"
        )

    elif deal_type == "rental":

        conditions.append(
            "v.rental_enabled = 1"
        )

    where_sql = " AND ".join(conditions)

    total_row = fetch_one(
        f"""
        SELECT COUNT(*) AS total
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        LEFT JOIN categories cat
            ON v.category_id = cat.category_id
        WHERE {where_sql}
        """,
        tuple(params)
    )

    total = total_row["total"] if total_row else 0

    rows = fetch_all(
        f"""
        SELECT
            v.*,
            c.channel_name,
            c.slug AS channel_slug,
            c.channel_logo,
            cat.category_name
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        LEFT JOIN categories cat
            ON v.category_id = cat.category_id
        WHERE {where_sql}
        ORDER BY
            v.created_at DESC,
            v.total_views DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [per_page, offset])
    )

    categories = fetch_all("""
        SELECT
            category_id,
            category_name,
            slug
        FROM categories
        WHERE is_active = 1
        ORDER BY category_name
    """)

    total_pages = (
        (total + per_page - 1) // per_page
        if total
        else 1
    )

    annotate_video_lock_status(rows, session.get('user_id'))

    return render_template(
        "videos.html",
        videos=rows,
        categories=categories,
        page=page,
        total_pages=total_pages,
        total=total,
        search_query=search_query,
        selected_category=category_id,
        selected_type=deal_type
    )



@app.route("/video/<slug>")
def video_details(slug):

    video = fetch_one("""
        SELECT
            v.*,
            c.channel_id,
            c.channel_name,
            c.slug AS channel_slug,
            c.channel_logo,
            c.channel_banner,
            c.subscriber_count,
            c.total_videos,
            cat.category_name,
            cat.slug AS category_slug
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        LEFT JOIN categories cat
            ON v.category_id = cat.category_id
        WHERE v.slug = %s
        LIMIT 1
    """, (slug,))

    if not video:
        abort(404)

    if video["status"] != "published":

        if not is_admin():
            abort(404)

    user_id = session.get("user_id")

    access_type = (
        get_video_access_type(
            user_id,
            video
        )
        if user_id
        else (
            "free"
            if video["is_free"]
            else None
        )
    )

    is_following = False
    has_subscription = False

    if user_id:

        follower = fetch_one("""
            SELECT channel_id
            FROM channel_followers
            WHERE user_id = %s
              AND channel_id = %s
            LIMIT 1
        """, (
            user_id,
            video["channel_id"]
        ))

        is_following = bool(follower)

        subscription = fetch_one("""
            SELECT subscription_id
            FROM channel_subscriptions
            WHERE user_id = %s
              AND channel_id = %s
              AND status = 'active'
              AND expires_at > NOW()
            LIMIT 1
        """, (
            user_id,
            video["channel_id"]
        ))

        has_subscription = bool(subscription)

    related = fetch_all("""
        SELECT
            v.video_id,
            v.title,
            v.slug,
            v.thumbnail_url,
            v.duration_seconds,
            v.is_free,
            v.purchase_enabled,
            v.purchase_price,
            v.rental_enabled,
            v.rental_price,
            c.channel_name,
            c.slug AS channel_slug
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.status = 'published'
          AND v.video_id != %s
          AND (
                v.category_id = %s
                OR v.channel_id = %s
          )
        ORDER BY v.total_views DESC
        LIMIT 8
    """, (
        video["video_id"],
        video["category_id"],
        video["channel_id"]
    ))

    annotate_video_lock_status(related, user_id)

    return render_template(
        "video_details.html",
        video=video,
        access_type=access_type,
        is_following=is_following,
        has_subscription=has_subscription,
        related=related,
        subscription_price=CHANNEL_SUBSCRIPTION_PRICE,
        subscription_days=CHANNEL_SUBSCRIPTION_DAYS
    )



@app.route("/watch/<slug>")
def watch(slug):

    video = fetch_one("""
        SELECT
            v.*,
            c.channel_name,
            c.slug AS channel_slug,
            c.channel_logo
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.slug = %s
        LIMIT 1
    """, (slug,))

    if not video:
        abort(404)

    if video["status"] != "published":
        if not is_admin():
            abort(404)

    user_id = session.get("user_id")

    access_type = get_video_access_type(
        user_id,
        video
    ) if user_id else (
        "free" if video["is_free"] else None
    )

    # --------------------------------------------------------
    # PAID VIDEO WITHOUT ACCESS
    # --------------------------------------------------------

    if not access_type:

        if not user_id:
            flash(
                "Please sign in to access this video.",
                "info"
            )

            return redirect(
                url_for(
                    "login",
                    next=request.url
                )
            )

        flash(
            "You do not have access to this video.",
            "error"
        )

        return redirect(
            url_for(
                "video_details",
                slug=video["slug"]
            )
        )

    suggested_videos = fetch_all("""
        SELECT
            v.video_id,
            v.title,
            v.slug,
            v.thumbnail_url,
            v.duration_seconds,
            v.is_free,
            v.purchase_enabled,
            v.purchase_price,
            c.channel_name
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.status = 'published'
          AND v.video_id != %s
          AND (
              v.category_id = %s
              OR v.channel_id = %s
          )
        ORDER BY v.total_views DESC
        LIMIT 8
    """, (
        video["video_id"],
        video["category_id"],
        video["channel_id"]
    ))

    comments = fetch_all("""
        SELECT
            cm.comment_id,
            cm.comment_text,
            cm.created_at,
            u.user_id,
            u.full_name,
            u.username,
            u.profile_image
        FROM comments cm
        JOIN users u
            ON cm.user_id = u.user_id
        WHERE cm.video_id = %s
          AND cm.is_deleted = 0
          AND cm.parent_comment_id IS NULL
        ORDER BY cm.created_at DESC
        LIMIT 50
    """, (video["video_id"],))

    user_reaction = None

    if user_id:
        reaction = fetch_one("""
            SELECT reaction
            FROM video_reactions
            WHERE user_id = %s
              AND video_id = %s
            LIMIT 1
        """, (
            user_id,
            video["video_id"]
        ))

        if reaction:
            user_reaction = reaction["reaction"]

    return render_template(
        "watch.html",
        video=video,
        access_type=access_type,
        suggested_videos=suggested_videos,
        comments=comments,
        user_reaction=user_reaction,
        bunny_cdn_hostname=BUNNY_CDN_HOSTNAME
    )
@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    results = []
    if query:
        search_term = f'%{query}%'
        results = fetch_all("\n            SELECT\n                v.*,\n                c.channel_name,\n                c.slug AS channel_slug,\n                c.channel_logo\n            FROM videos v\n            JOIN channels c\n                ON c.channel_id = v.channel_id\n            WHERE v.status = 'published'\n              AND v.visibility = 'public'\n              AND c.is_active = TRUE\n              AND (\n                    v.title LIKE %s\n                    OR v.description LIKE %s\n                    OR c.channel_name LIKE %s\n                  )\n            ORDER BY v.total_views DESC, v.published_at DESC\n            LIMIT 50\n            ", (search_term, search_term, search_term))
    return render_template('search.html', query=query, videos=results)

@app.route("/channel/<slug>")
def channel(slug):

    channel_data = fetch_one("""
        SELECT *
        FROM channels
        WHERE slug = %s
          AND is_active = 1
        LIMIT 1
    """, (slug,))

    if not channel_data:
        abort(404)

    sort = request.args.get(
        "sort",
        "latest"
    )

    sort_map = {
        "latest": "v.published_at DESC",
        "popular": "v.total_views DESC",
        "oldest": "v.published_at ASC"
    }

    order_sql = sort_map.get(
        sort,
        sort_map["latest"]
    )

    channel_videos = fetch_all(
        f"""
        SELECT
            v.*,
            cat.category_name
        FROM videos v
        LEFT JOIN categories cat
            ON v.category_id = cat.category_id
        WHERE v.channel_id = %s
          AND v.status = 'published'
          AND v.visibility IN ('public', 'unlisted')
        ORDER BY {order_sql}
        LIMIT 60
        """,
        (channel_data["channel_id"],)
    )

    # ``channels.total_videos`` is a denormalized value and is not updated
    # when a video is created or published. Count the same videos visitors
    # can see so the header always matches the channel grid.
    video_count_row = fetch_one("""
        SELECT COUNT(*) AS total
        FROM videos
        WHERE channel_id = %s
          AND status = 'published'
          AND visibility IN ('public', 'unlisted')
    """, (channel_data["channel_id"],))
    video_count = video_count_row["total"] if video_count_row else 0

    annotate_video_lock_status(channel_videos, session.get('user_id'))

    follower_row = fetch_one("""
        SELECT COUNT(*) AS total
        FROM channel_followers
        WHERE channel_id = %s
    """, (
        channel_data["channel_id"],
    ))

    follower_count = (
        follower_row["total"]
        if follower_row
        else 0
    )

    is_following = False
    has_subscription = False

    if session.get("user_id"):

        following = fetch_one("""
            SELECT channel_id
            FROM channel_followers
            WHERE user_id = %s
              AND channel_id = %s
            LIMIT 1
        """, (
            session["user_id"],
            channel_data["channel_id"]
        ))

        is_following = bool(following)

        subscription = fetch_one("""
            SELECT subscription_id
            FROM channel_subscriptions
            WHERE user_id = %s
              AND channel_id = %s
              AND status = 'active'
              AND expires_at > NOW()
            LIMIT 1
        """, (
            session["user_id"],
            channel_data["channel_id"]
        ))

        has_subscription = bool(subscription)

    return render_template(
        "channel.html",
        channel=channel_data,
        videos=channel_videos,
        video_count=video_count,
        follower_count=follower_count,
        is_following=is_following,
        has_subscription=has_subscription,
        sort=sort,
        subscription_price=CHANNEL_SUBSCRIPTION_PRICE,
        subscription_days=CHANNEL_SUBSCRIPTION_DAYS
    )




@app.route('/api/channel/<int:channel_id>/follow', methods=['POST'])
@login_required
def follow_channel(channel_id):
    user_id = session['user_id']
    channel_data = fetch_one('\n        SELECT channel_id\n        FROM channels\n        WHERE channel_id = %s\n          AND is_active = TRUE\n        LIMIT 1\n        ', (channel_id,))
    if not channel_data:
        return (jsonify({'success': False, 'message': 'Channel not found.'}), 404)
    existing = fetch_one('\n        SELECT follower_id\n        FROM channel_followers\n        WHERE user_id = %s\n          AND channel_id = %s\n        LIMIT 1\n        ', (user_id, channel_id))
    if existing:
        execute_query('\n            DELETE FROM channel_followers\n            WHERE follower_id = %s\n            ', (existing['follower_id'],))
        action = 'unfollowed'
    else:
        execute_query('\n            INSERT INTO channel_followers\n            (\n                user_id,\n                channel_id\n            )\n            VALUES\n            (\n                %s,\n                %s\n            )\n            ', (user_id, channel_id))
        action = 'followed'
    follower_count = fetch_one('\n        SELECT COUNT(*) AS total\n        FROM channel_followers\n        WHERE channel_id = %s\n        ', (channel_id,))
    return jsonify({'success': True, 'action': action, 'followers': follower_count['total']})

@app.route('/api/video/<int:video_id>/reaction', methods=['POST'])
@login_required
def video_reaction(video_id):
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    reaction = data.get('reaction')
    if reaction not in ('like', 'dislike'):
        return (jsonify({'success': False, 'message': 'Invalid reaction.'}), 400)
    video = fetch_one('\n        SELECT video_id\n        FROM videos\n        WHERE video_id = %s\n        LIMIT 1\n        ', (video_id,))
    if not video:
        return (jsonify({'success': False, 'message': 'Video not found.'}), 404)
    existing = fetch_one('\n        SELECT reaction_id, reaction\n        FROM video_reactions\n        WHERE user_id = %s\n          AND video_id = %s\n        LIMIT 1\n        ', (user_id, video_id))
    if existing:
        if existing['reaction'] == reaction:
            execute_query('\n                DELETE FROM video_reactions\n                WHERE reaction_id = %s\n                ', (existing['reaction_id'],))
            current_reaction = None
        else:
            execute_query('\n                UPDATE video_reactions\n                SET reaction = %s\n                WHERE reaction_id = %s\n                ', (reaction, existing['reaction_id']))
            current_reaction = reaction
    else:
        execute_query('\n            INSERT INTO video_reactions\n            (\n                user_id,\n                video_id,\n                reaction\n            )\n            VALUES\n            (\n                %s,\n                %s,\n                %s\n            )\n            ', (user_id, video_id, reaction))
        current_reaction = reaction
    counts = fetch_one("\n        SELECT\n            SUM(reaction = 'like') AS likes,\n            SUM(reaction = 'dislike') AS dislikes\n        FROM video_reactions\n        WHERE video_id = %s\n        ", (video_id,))
    likes = int(counts['likes'] or 0)
    dislikes = int(counts['dislikes'] or 0)
    execute_query('\n        UPDATE videos\n        SET\n            total_likes = %s,\n            total_dislikes = %s\n        WHERE video_id = %s\n        ', (likes, dislikes, video_id))
    return jsonify({'success': True, 'reaction': current_reaction, 'likes': likes, 'dislikes': dislikes})

from flask import jsonify, request, session

from flask import jsonify, request, session

from flask import jsonify, request, session

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
        "SELECT video_id FROM videos WHERE video_id = %s LIMIT 1",
        (video_id,)
    )
    if not video:
        return jsonify(success=False, message="Video not found"), 404

    # execute_query commits and returns cursor.lastrowid
    new_comment_id = execute_query("""
        INSERT INTO comments
            (user_id, video_id, parent_comment_id, comment_text,
             is_edited, is_deleted, is_pinned, like_count,
             created_at, updated_at)
        VALUES
            (%s, %s, NULL, %s,
             0, 0, 0, 0,
             NOW(), NOW())
    """, (user_id, video_id, text), return_lastrowid=True)

    if not new_comment_id:
        return jsonify(success=False, message="Unable to save comment"), 500

    execute_query("""
        UPDATE videos
        SET total_comments = COALESCE(total_comments, 0) + 1
        WHERE video_id = %s
    """, (video_id,))

    user = fetch_one("""
        SELECT user_id, full_name, username, profile_image
        FROM users WHERE user_id = %s LIMIT 1
    """, (user_id,))

    return jsonify(
        success=True,
        comment={
            "comment_id": new_comment_id,
            "comment_text": text,
            "full_name": user["full_name"],
            "username": user["username"],
            "profile_image": user["profile_image"],
        }
    )

@app.route("/api/video/<int:video_id>/progress", methods=["POST"])
@login_required
def save_watch_progress(video_id):

    user_id = session["user_id"]

    video = fetch_one("""
        SELECT *
        FROM videos
        WHERE video_id = %s
        LIMIT 1
    """, (video_id,))

    if not video:
        return jsonify({
            "success": False,
            "message": "Video not found."
        }), 404

    if not has_video_access(user_id, video):
        return jsonify({
            "success": False,
            "message": "You do not have access to this video."
        }), 403

    data = request.get_json(silent=True) or {}

    try:
        progress_seconds = max(
            0,
            int(float(data.get(
                "progress_seconds",
                0
            )))
        )

        completion_percentage = max(
            0,
            min(
                100,
                float(data.get(
                    "completion_percentage",
                    0
                ))
            )
        )

    except (TypeError, ValueError):

        return jsonify({
            "success": False,
            "message": "Invalid progress data."
        }), 400

    execute_query("""
        INSERT INTO watch_history (
            user_id,
            video_id,
            progress_seconds,
            completion_percentage,
            last_watched_at
        )
        VALUES (
            %s, %s, %s, %s, NOW()
        )
        ON DUPLICATE KEY UPDATE
            progress_seconds = VALUES(progress_seconds),
            completion_percentage =
                VALUES(completion_percentage),
            last_watched_at = NOW()
    """, (
        user_id,
        video_id,
        progress_seconds,
        completion_percentage
    ))

    return jsonify({
        "success": True
    })



@app.route("/api/video/<int:video_id>/view", methods=["POST"])
def record_video_view(video_id):

    video = fetch_one("""
        SELECT *
        FROM videos
        WHERE video_id = %s
          AND status = 'published'
        LIMIT 1
    """, (video_id,))

    if not video:
        return jsonify({
            "success": False
        }), 404

    user_id = session.get("user_id")

    # Free videos can be viewed without login.
    # Paid videos require access.
    if not video["is_free"]:

        if not user_id:
            return jsonify({
                "success": False,
                "message": "Login required."
            }), 401

        if not has_video_access(
            user_id,
            video
        ):
            return jsonify({
                "success": False,
                "message": "No access."
            }), 403

    session_id = session.get("view_session_id")

    if not session_id:

        session_id = uuid.uuid4().hex

        session["view_session_id"] = session_id

    ip_address = request.remote_addr or ""

    ip_hash = hashlib.sha256(
        ip_address.encode("utf-8")
    ).hexdigest()

    user_agent = request.headers.get(
        "User-Agent",
        ""
    )

    # Basic duplicate protection:
    # don't create another view for the same
    # session/video within a short period.
    recent_view = fetch_one("""
        SELECT view_id
        FROM video_views
        WHERE video_id = %s
          AND session_id = %s
          AND started_at >= NOW() - INTERVAL 30 MINUTE
        LIMIT 1
    """, (
        video_id,
        session_id
    ))

    if recent_view:

        return jsonify({
            "success": True,
            "counted": False
        })

    execute_query("""
        INSERT INTO video_views (
            video_id,
            user_id,
            session_id,
            ip_hash,
            user_agent,
            started_at,
            last_activity_at,
            is_valid_view
        )
        VALUES (
            %s, %s, %s, %s, %s,
            NOW(), NOW(), 1
        )
    """, (
        video_id,
        user_id,
        session_id,
        ip_hash,
        user_agent
    ))

    execute_query("""
        UPDATE videos
        SET total_views = total_views + 1,
            updated_at = NOW()
        WHERE video_id = %s
    """, (video_id,))

    return jsonify({
        "success": True,
        "counted": True
    })




@app.route("/video/<int:video_id>/purchase")
@login_required
def purchase_video(video_id):

    video = fetch_one("""
        SELECT
            v.*,
            c.channel_name,
            c.slug AS channel_slug
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.video_id = %s
        LIMIT 1
    """, (video_id,))

    if not video:
        abort(404)

    if video["status"] != "published":
        flash("This video is not available for purchase.", "error")
        return redirect(url_for("index"))

    if not video["purchase_enabled"]:
        flash("Purchase is not available for this video.", "error")
        return redirect(
            url_for("video_details", slug=video["slug"])
        )

    if has_video_access(session["user_id"], video):
        return redirect(
            url_for("watch", slug=video["slug"])
        )

    return render_template(
        "payment.html",
        payment_type="video_purchase",
        video=video,
        amount=video["purchase_price"],
        currency=video["currency"] or "GHS",
        paystack_public_key=PAYSTACK_PUBLIC_KEY
    )

@app.route("/video/<int:video_id>/rent")
@login_required
def rent_video(video_id):

    video = fetch_one("""
        SELECT
            v.*,
            c.channel_name,
            c.slug AS channel_slug
        FROM videos v
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE v.video_id = %s
        LIMIT 1
    """, (video_id,))

    if not video:
        abort(404)

    if video["status"] != "published":
        flash("This video is not available for rental.", "error")
        return redirect(url_for("index"))

    if not video["rental_enabled"]:
        flash("Rental is not available for this video.", "error")
        return redirect(
            url_for("video_details", slug=video["slug"])
        )

    access_type = get_video_access_type(
        session["user_id"],
        video
    )

    if access_type in ("free", "admin", "purchase", "rental", "subscription"):
        return redirect(
            url_for("watch", slug=video["slug"])
        )

    return render_template(
        "payment.html",
        payment_type="video_rental",
        video=video,
        amount=video["rental_price"],
        rental_duration_hours=VIDEO_RENTAL_DURATION_HOURS,
        currency=video["currency"] or "GHS",
        paystack_public_key=PAYSTACK_PUBLIC_KEY
    )



@app.route("/channel/<int:channel_id>/subscribe")
@login_required
def subscribe_channel(channel_id):

    channel = fetch_one("""
        SELECT *
        FROM channels
        WHERE channel_id = %s
          AND is_active = 1
        LIMIT 1
    """, (channel_id,))

    if not channel:
        abort(404)

    existing = fetch_one("""
        SELECT *
        FROM channel_subscriptions
        WHERE user_id = %s
          AND channel_id = %s
          AND status = 'active'
          AND expires_at > NOW()
        ORDER BY expires_at DESC
        LIMIT 1
    """, (
        session["user_id"],
        channel_id
    ))

    if existing:
        flash("You already have an active subscription to this channel.", "info")
        return redirect(
            url_for(
                "channel",
                slug=channel["slug"]
            )
        )

    return render_template(
        "payment.html",
        payment_type="channel_subscription",
        channel=channel,
        amount=CHANNEL_SUBSCRIPTION_PRICE,
        subscription_days=CHANNEL_SUBSCRIPTION_DAYS,
        currency="GHS",
        paystack_public_key=PAYSTACK_PUBLIC_KEY
    )

@app.route('/purchases')
@login_required
def purchases():
    user_id = session['user_id']
    purchases_list = fetch_all("\n        SELECT\n            p.*,\n            v.title,\n            v.slug,\n            v.thumbnail_url\n        FROM video_purchases p\n        JOIN videos v\n            ON v.video_id = p.video_id\n        WHERE p.user_id = %s\n          AND p.status = 'paid'\n        ORDER BY p.purchased_at DESC\n        ", (user_id,))
    return render_template('purchases.html', purchases=purchases_list)

@app.route('/history')
@login_required
def history():
    user_id = session['user_id']
    history_list = fetch_all('\n        SELECT\n            h.*,\n            v.title,\n            v.slug,\n            v.thumbnail_url,\n            v.duration_seconds,\n            c.channel_name,\n            c.slug AS channel_slug\n        FROM watch_history h\n        JOIN videos v\n            ON v.video_id = h.video_id\n        JOIN channels c\n            ON c.channel_id = v.channel_id\n        WHERE h.user_id = %s\n        ORDER BY h.last_watched_at DESC\n        LIMIT 100\n        ', (user_id,))
    return render_template('watch_history.html', history=history_list)

@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    stats = {'total_users': fetch_one('SELECT COUNT(*) AS total FROM users')['total'], 'total_channels': fetch_one('SELECT COUNT(*) AS total FROM channels WHERE is_active = TRUE')['total'], 'total_videos': fetch_one("\n            SELECT COUNT(*) AS total\n            FROM videos\n            WHERE status <> 'deleted'\n            ")['total'], 'total_views': fetch_one("\n            SELECT COALESCE(SUM(total_views), 0) AS total\n            FROM videos\n            WHERE status <> 'deleted'\n            ")['total']}
    recent_payments = fetch_all("\n        SELECT\n            p.payment_id,\n            p.payment_type,\n            p.amount,\n            p.currency,\n            p.paid_at,\n            u.full_name\n        FROM payments p\n        LEFT JOIN users u\n            ON u.user_id = p.user_id\n        WHERE p.status = 'successful'\n        ORDER BY p.paid_at DESC\n        LIMIT 6\n        ")
    recent_videos = fetch_all("\n        SELECT\n            v.video_id,\n            v.title,\n            v.thumbnail_url,\n            v.status,\n            v.is_free,\n            v.rental_enabled,\n            v.purchase_enabled,\n            v.total_views,\n            c.channel_name\n        FROM videos v\n        LEFT JOIN channels c\n            ON c.channel_id = v.channel_id\n        WHERE v.status <> 'deleted'\n        ORDER BY v.created_at DESC\n        LIMIT 6\n        ")
    top_channels = fetch_all('\n        SELECT\n            channel_id,\n            channel_name,\n            channel_logo,\n            subscriber_count,\n            total_views\n        FROM channels\n        WHERE is_active = TRUE\n        ORDER BY total_views DESC\n        LIMIT 6\n        ')
    chart_labels = []
    chart_views = []
    chart_users = []
    for i in range(6, -1, -1):
        day = datetime.now().date() - timedelta(days=i)
        chart_labels.append(day.strftime('%a'))
        view_result = fetch_one('\n            SELECT COUNT(*) AS total\n            FROM video_views\n            WHERE DATE(started_at) = %s\n              AND is_valid_view = TRUE\n            ', (day,))
        chart_views.append(view_result['total'] if view_result else 0)
        user_result = fetch_one('\n            SELECT COUNT(*) AS total\n            FROM users\n            WHERE DATE(created_at) = %s\n            ', (day,))
        chart_users.append(user_result['total'] if user_result else 0)
    return render_template('admin_dashboard.html', stats=stats, recent_payments=recent_payments, recent_videos=recent_videos, top_channels=top_channels, chart_labels=chart_labels, chart_views=chart_views, chart_users=chart_users)

@app.route('/admin/users')
@admin_required
def admin_users():
    users = fetch_all('\n        SELECT\n            user_id,\n            full_name,\n            username,\n            email,\n            phone,\n            profile_image,\n            role,\n            is_active,\n            email_verified,\n            created_at,\n            last_login_at\n        FROM users\n        ORDER BY created_at DESC\n        LIMIT 500\n        ')
    user_stats = fetch_one("\n        SELECT\n            COUNT(*) AS total,\n            COALESCE(SUM(is_active = TRUE), 0) AS active,\n            COALESCE(SUM(role = 'admin'), 0) AS admins,\n            COALESCE(SUM(created_at >= DATE_FORMAT(CURDATE(), '%Y-%m-01')), 0)\n                AS new_this_month\n        FROM users\n        ") or {
            'total': 0,
            'active': 0,
            'admins': 0,
            'new_this_month': 0,
        }
    return render_template(
        'admin_users.html', users=users, user_stats=user_stats
    )

@app.route('/admin/users/<int:user_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_user_status(user_id):
    if user_id == session['user_id']:
        flash('You cannot deactivate your own account.', 'warning')
        return redirect(url_for('admin_users'))
    user = fetch_one('\n        SELECT user_id, is_active\n        FROM users\n        WHERE user_id = %s\n        LIMIT 1\n        ', (user_id,))
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_users'))
    new_status = not bool(user['is_active'])
    execute_query('\n        UPDATE users\n        SET is_active = %s\n        WHERE user_id = %s\n        ', (new_status, user_id))
    flash('User status updated successfully.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/channels')
@admin_required
def admin_channels():
    result = fetch_one('\n        SELECT COUNT(*) AS total\n        FROM channels\n        ')
    total_channels = result['total'] if result else 0
    result = fetch_one('\n        SELECT COUNT(*) AS total\n        FROM channels\n        WHERE is_active = TRUE\n        ')
    active_channels = result['total'] if result else 0
    result = fetch_one('\n        SELECT COUNT(*) AS total\n        FROM channels\n        WHERE is_verified = TRUE\n        ')
    verified_channels = result['total'] if result else 0
    result = fetch_one('\n        SELECT COALESCE(SUM(subscriber_count), 0) AS total\n        FROM channels\n        WHERE is_active = TRUE\n        ')
    total_subscribers = result['total'] if result else 0
    channel_stats = {'total': total_channels, 'active': active_channels, 'verified': verified_channels, 'subscribers': total_subscribers}
    channels = fetch_all('\n        SELECT\n            c.channel_id,\n            c.owner_user_id,\n            c.channel_name,\n            c.slug,\n            c.description,\n            c.channel_logo,\n            c.channel_banner,\n            c.subscriber_count,\n            c.total_views,\n            c.total_videos,\n            c.is_active,\n            c.is_verified,\n            c.created_at,\n\n            u.full_name AS owner_name,\n            u.email AS owner_email\n\n        FROM channels c\n\n        LEFT JOIN users u\n            ON u.user_id = c.owner_user_id\n\n        ORDER BY c.created_at DESC\n        ')
    return render_template('admin_channels.html', channels=channels, channel_stats=channel_stats)

@app.route('/admin/channels/create', methods=['GET', 'POST'])
@app.route('/admin/channels/<int:channel_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_create_channel(channel_id=None):

    channel = None

    # =========================================================
    # LOAD EXISTING CHANNEL
    # =========================================================

    if channel_id:
        channel = fetch_one("""
            SELECT
                channel_id,
                owner_user_id,
                channel_name,
                slug,
                description,
                channel_logo,
                channel_banner,
                subscriber_count,
                total_views,
                total_videos,
                is_active,
                is_verified,
                created_at
            FROM channels
            WHERE channel_id = %s
        """, (channel_id,))

        if not channel:
            return ('Channel not found.', 404)

    # =========================================================
    # LOAD USERS
    # =========================================================

    users = fetch_all("""
        SELECT
            user_id,
            full_name,
            email
        FROM users
        WHERE is_active = TRUE
        ORDER BY full_name ASC
    """)

    # =========================================================
    # HANDLE FORM SUBMISSION
    # =========================================================

    if request.method == 'POST':

        channel_name = request.form.get('channel_name', '').strip()
        slug = request.form.get('slug', '').strip().lower()
        description = request.form.get('description', '').strip()
        owner_user_id = request.form.get('owner_user_id')

        is_active = 1 if request.form.get('is_active') else 0
        is_verified = 1 if request.form.get('is_verified') else 0

        # =====================================================
        # VALIDATION
        # =====================================================

        if not channel_name:
            flash('Channel name is required.', 'error')
            return render_template(
                'admin_create_channel.html',
                channel=channel,
                users=users
            )

        if not slug:
            flash('Channel URL is required.', 'error')
            return render_template(
                'admin_create_channel.html',
                channel=channel,
                users=users
            )

        if not owner_user_id:
            flash('Please select a channel owner.', 'error')
            return render_template(
                'admin_create_channel.html',
                channel=channel,
                users=users
            )

        # =====================================================
        # VALIDATE OWNER
        # =====================================================

        owner = fetch_one("""
            SELECT user_id
            FROM users
            WHERE user_id = %s
              AND is_active = TRUE
        """, (owner_user_id,))

        if not owner:
            flash('The selected channel owner is invalid.', 'error')
            return render_template(
                'admin_create_channel.html',
                channel=channel,
                users=users
            )

        # =====================================================
        # CHECK SLUG
        # =====================================================

        existing_slug = fetch_one("""
            SELECT channel_id
            FROM channels
            WHERE slug = %s
        """, (slug,))

        if existing_slug:
            if not channel_id or existing_slug['channel_id'] != channel_id:
                flash('That channel URL is already in use.', 'error')
                return render_template(
                    'admin_create_channel.html',
                    channel=channel,
                    users=users
                )

        # =====================================================
        # KEEP EXISTING IMAGES BY DEFAULT
        # =====================================================

        channel_logo = channel['channel_logo'] if channel else None
        channel_banner = channel['channel_banner'] if channel else None

        # =====================================================
        # CHANNEL LOGO UPLOAD
        # =====================================================

        logo_file = request.files.get('channel_logo')

        if logo_file and logo_file.filename:

            try:
                logo_result = cloudinary.uploader.upload(
                    logo_file,
                    folder="kss/channels/logos",
                    resource_type="image"
                )

                channel_logo = logo_result.get('secure_url')

            except Exception as e:
                print("CHANNEL LOGO UPLOAD ERROR:", e)

                flash(
                    'Unable to upload the channel logo. Please try again.',
                    'error'
                )

                return render_template(
                    'admin_create_channel.html',
                    channel=channel,
                    users=users
                )

        # =====================================================
        # CHANNEL BANNER UPLOAD
        # =====================================================

        banner_file = request.files.get('channel_banner')

        if banner_file and banner_file.filename:

            try:
                banner_result = cloudinary.uploader.upload(
                    banner_file,
                    folder="kss/channels/banners",
                    resource_type="image"
                )

                channel_banner = banner_result.get('secure_url')

            except Exception as e:
                print("CHANNEL BANNER UPLOAD ERROR:", e)

                flash(
                    'Unable to upload the channel banner. Please try again.',
                    'error'
                )

                return render_template(
                    'admin_create_channel.html',
                    channel=channel,
                    users=users
                )

        # =====================================================
        # UPDATE EXISTING CHANNEL
        # =====================================================

        if channel_id:

            execute_query("""
                UPDATE channels
                SET
                    owner_user_id = %s,
                    channel_name = %s,
                    slug = %s,
                    description = %s,
                    channel_logo = %s,
                    channel_banner = %s,
                    is_active = %s,
                    is_verified = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = %s
            """, (
                owner_user_id,
                channel_name,
                slug,
                description,
                channel_logo,
                channel_banner,
                is_active,
                is_verified,
                channel_id
            ))

            flash('Channel updated successfully.', 'success')

        # =====================================================
        # CREATE NEW CHANNEL
        # =====================================================

        else:

            new_channel_id = execute_query("""
                INSERT INTO channels
                (
                    owner_user_id,
                    channel_name,
                    slug,
                    description,
                    channel_logo,
                    channel_banner,
                    subscriber_count,
                    total_views,
                    total_videos,
                    is_active,
                    is_verified
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    0,
                    0,
                    0,
                    %s,
                    %s
                )
            """, (
                owner_user_id,
                channel_name,
                slug,
                description,
                channel_logo,
                channel_banner,
                is_active,
                is_verified
            ), return_lastrowid=True)

            if not new_channel_id:
                flash('Unable to create the channel.', 'error')

                return render_template(
                    'admin_create_channel.html',
                    channel=None,
                    users=users
                )

            flash('Channel created successfully.', 'success')

        return redirect(url_for('admin_channels'))

    # =========================================================
    # GET REQUEST
    # =========================================================

    return render_template(
        'admin_create_channel.html',
        channel=channel,
        users=users
    )



import os
import time
from werkzeug.utils import secure_filename

# Add to your Flask app config (e.g., in __init__.py or config)
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'thumbnails')
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']





@app.route('/admin/videos')
@admin_required
def admin_videos():
    result = fetch_one("\n        SELECT COUNT(*) AS total\n        FROM videos\n        WHERE status <> 'deleted'\n        ")
    total_videos = result['total'] if result else 0
    result = fetch_one("\n        SELECT COUNT(*) AS total\n        FROM videos\n        WHERE status = 'published'\n        ")
    published_videos = result['total'] if result else 0
    result = fetch_one("\n        SELECT COUNT(*) AS total\n        FROM videos\n        WHERE status = 'processing'\n        ")
    processing_videos = result['total'] if result else 0
    result = fetch_one("\n        SELECT COUNT(*) AS total\n        FROM videos\n        WHERE status = 'draft'\n        ")
    draft_videos = result['total'] if result else 0
    result = fetch_one("\n        SELECT COUNT(*) AS total\n        FROM videos\n        WHERE status <> 'deleted'\n          AND (\n                rental_enabled = TRUE\n                OR purchase_enabled = TRUE\n                OR subscription_enabled = TRUE\n              )\n        ")
    paid_videos = result['total'] if result else 0
    video_stats = {'total': total_videos, 'published': published_videos, 'processing': processing_videos, 'drafts': draft_videos, 'paid': paid_videos}
    channels = fetch_all('\n        SELECT\n            channel_id,\n            channel_name\n        FROM channels\n        WHERE is_active = TRUE\n        ORDER BY channel_name ASC\n        ')
    videos = fetch_all("\n        SELECT\n\n            v.video_id,\n            v.channel_id,\n            v.category_id,\n\n            v.title,\n            v.slug,\n            v.thumbnail_url,\n\n            v.bunny_video_id,\n\n            v.duration_seconds,\n\n            v.status,\n            v.visibility,\n\n            v.is_free,\n\n            v.rental_enabled,\n            v.rental_price,\n            v.rental_duration_hours,\n\n            v.purchase_enabled,\n            v.purchase_price,\n\n            v.subscription_enabled,\n\n            v.currency,\n\n            v.total_views,\n            v.total_likes,\n            v.total_dislikes,\n            v.total_comments,\n\n            v.published_at,\n            v.created_at,\n\n            c.channel_name,\n\n            cat.category_name\n\n        FROM videos v\n\n        LEFT JOIN channels c\n            ON c.channel_id = v.channel_id\n\n        LEFT JOIN categories cat\n            ON cat.category_id = v.category_id\n\n        WHERE v.status <> 'deleted'\n\n        ORDER BY v.created_at DESC\n\n        LIMIT 200\n        ")
    return render_template('admin_videos.html', videos=videos, channels=channels, video_stats=video_stats)

@app.route('/admin/videos/create', methods=['GET', 'POST'])
@app.route('/admin/videos/<int:video_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_create_video(video_id=None):
    video = None
    selected_tags = []

    # LOAD EXISTING VIDEO
    if video_id:
        video = fetch_one("""
            SELECT *
            FROM videos
            WHERE video_id = %s
              AND status <> 'deleted'
            LIMIT 1
        """, (video_id,))

        if not video:
            flash('Video not found.', 'error')
            return redirect(url_for('admin_videos'))

        tag_rows = fetch_all("""
            SELECT t.tag_name
            FROM tags t
            INNER JOIN video_tags vt ON vt.tag_id = t.tag_id
            WHERE vt.video_id = %s
            ORDER BY t.tag_name ASC
        """, (video_id,))
        selected_tags = [row['tag_name'] for row in tag_rows]

    # POST - SAVE VIDEO
    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        try:
            # BASIC FIELDS
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

            # ACCESS SETTINGS
            is_free = request.form.get('is_free') == '1'
            rental_enabled = request.form.get('rental_enabled') == '1'
            purchase_enabled = request.form.get('purchase_enabled') == '1'
            subscription_enabled = request.form.get('subscription_enabled') == '1'
            rental_price = request.form.get('rental_price')
            purchase_price = request.form.get('purchase_price')

            # =========================================================
            # THUMBNAIL FILE UPLOAD
            # =========================================================
            thumbnail_file = request.files.get('thumbnail_file')
            thumbnail_url = video['thumbnail_url'] if video else ''  # keep existing by default

            if thumbnail_file and thumbnail_file.filename:
                if allowed_file(thumbnail_file.filename):
                    filename = secure_filename(thumbnail_file.filename)
                    filename = f"{int(time.time())}_{filename}"
                    upload_dir = app.config['UPLOAD_FOLDER']
                    os.makedirs(upload_dir, exist_ok=True)
                    filepath = os.path.join(upload_dir, filename)
                    thumbnail_file.save(filepath)
                    thumbnail_url = url_for('static', filename=f'thumbnails/{filename}')
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
                FROM channels
                WHERE channel_id = %s
                LIMIT 1
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

                # Rental length is a product rule, not a client-provided value.
                # This prevents a tampered form from creating a shorter/longer
                # entitlement than the 48-hour offer shown to customers.
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
                message = (
                    'A paid video must offer rental, permanent purchase, '
                    'or channel subscription access.'
                )
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
                        SELECT video_id
                        FROM videos
                        WHERE slug = %s
                          AND video_id <> %s
                        LIMIT 1
                    """, (slug, video_id))
                else:
                    duplicate = fetch_one("""
                        SELECT video_id
                        FROM videos
                        WHERE slug = %s
                        LIMIT 1
                    """, (slug,))

                if duplicate:
                    slug = generate_video_slug(title, video_id)

            # =================================================
            # DATABASE CONNECTION
            # =================================================
            connection = get_db_connection()
            if not connection:
                raise RuntimeError('Unable to connect to database.')

            cursor = connection.cursor(dictionary=True)

            try:
                # UPDATE EXISTING VIDEO
                if video_id:
                    existing = fetch_one("""
                        SELECT video_id, bunny_video_id, bunny_library_id, status
                        FROM videos
                        WHERE video_id = %s
                        LIMIT 1
                    """, (video_id,))

                    if not existing:
                        raise RuntimeError('Video no longer exists.')

                    current_bunny_video_id = existing['bunny_video_id']
                    if not bunny_video_id:
                        bunny_video_id = current_bunny_video_id

                    current_status = existing['status']

                    # Determine new status
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
                            raise RuntimeError('This video cannot be published until it has been uploaded to Bunny Stream.')
                        # A published video is already known to have completed
                        # Bunny processing.  Editing its metadata must not
                        # force an unnecessary re-encoding cycle.
                        if current_status not in {'ready', 'published'}:
                            raise RuntimeError('This video is not ready for publishing. Bunny Stream must finish processing it first.')
                        new_status = 'published'

                    # UPDATE
                    cursor.execute("""
                        UPDATE videos
                        SET
                            channel_id = %s,
                            category_id = %s,
                            title = %s,
                            slug = %s,
                            description = %s,
                            thumbnail_url = %s,
                            bunny_video_id = %s,
                            bunny_library_id = %s,
                            duration_seconds = %s,
                            status = %s,
                            visibility = %s,
                            is_free = %s,
                            rental_enabled = %s,
                            rental_price = %s,
                            rental_duration_hours = %s,
                            purchase_enabled = %s,
                            purchase_price = %s,
                            subscription_enabled = %s,
                            currency = %s,
                            published_at = CASE
                                WHEN %s = 'published' AND status <> 'published' THEN NOW()
                                WHEN %s <> 'published' THEN NULL
                                ELSE published_at
                            END,
                            updated_at = NOW()
                        WHERE video_id = %s
                    """, (
                        channel_id,
                        category_id,
                        title,
                        slug,
                        description,
                        thumbnail_url or None,
                        bunny_video_id or None,
                        BUNNY_LIBRARY_ID if bunny_video_id else None,
                        duration_seconds,
                        new_status,
                        visibility,
                        is_free,
                        rental_enabled,
                        rental_price,
                        rental_duration_hours,
                        purchase_enabled,
                        purchase_price,
                        subscription_enabled,
                        currency,
                        new_status,
                        new_status,
                        video_id
                    ))

                    # Save tags
                    save_video_tags(video_id, tags_string, connection)

                    connection.commit()

                    # Update Bunny title
                    if bunny_video_id:
                        try:
                            bunny_update_video(bunny_video_id, title, existing['bunny_library_id'])
                        except Exception as bunny_error:
                            app.logger.warning('KSS video updated, but Bunny title update failed: %s', bunny_error)

                    if is_ajax:
                        return jsonify({
                            'success': True,
                            'video_id': video_id,
                            'edit_url': url_for('admin_create_video', video_id=video_id),
                            'status': new_status
                        })

                    if new_status == 'published':
                        flash('Video updated and published.', 'success')
                    else:
                        flash('Video updated successfully.', 'success')

                    return redirect(url_for('admin_create_video', video_id=video_id))

                # CREATE NEW VIDEO
                else:
                    new_status = 'draft'
                    if action == 'save_publish':
                        raise RuntimeError('Save the video first, upload it to Bunny Stream, wait for processing, then publish it.')

                    cursor.execute("""
                        INSERT INTO videos
                        (
                            channel_id,
                            category_id,
                            title,
                            slug,
                            description,
                            thumbnail_url,
                            bunny_video_id,
                            bunny_library_id,
                            duration_seconds,
                            status,
                            visibility,
                            is_free,
                            rental_enabled,
                            rental_price,
                            rental_duration_hours,
                            purchase_enabled,
                            purchase_price,
                            subscription_enabled,
                            currency,
                            total_views,
                            total_likes,
                            total_dislikes,
                            total_comments,
                            total_watch_seconds,
                            created_at,
                            updated_at
                        )
                        VALUES
                        (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            0,
                            0,
                            0,
                            0,
                            0,
                            NOW(),
                            NOW()
                        )
                    """, (
                        channel_id,
                        category_id,
                        title,
                        slug,
                        description,
                        thumbnail_url or None,
                        bunny_video_id or None,
                        BUNNY_LIBRARY_ID if bunny_video_id else None,
                        duration_seconds,
                        new_status,
                        visibility,
                        is_free,
                        rental_enabled,
                        rental_price,
                        rental_duration_hours,
                        purchase_enabled,
                        purchase_price,
                        subscription_enabled,
                        currency
                    ))

                    new_video_id = cursor.lastrowid
                    save_video_tags(new_video_id, tags_string, connection)
                    connection.commit()

                    if is_ajax:
                        return jsonify({
                            'success': True,
                            'video_id': new_video_id,
                            'edit_url': url_for('admin_create_video', video_id=new_video_id),
                            'status': new_status
                        })

                    flash('Video draft created successfully.', 'success')
                    return redirect(url_for('admin_create_video', video_id=new_video_id))

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

    # GET - LOAD CHANNELS AND CATEGORIES
    channels = fetch_all("""
        SELECT channel_id, channel_name
        FROM channels
        WHERE is_active = TRUE
        ORDER BY channel_name ASC
    """)
    categories = fetch_all("""
        SELECT category_id, category_name
        FROM categories
        WHERE is_active = TRUE
        ORDER BY category_name ASC
    """)

    return render_template(
        'admin_create_video.html',
        video=video,
        channels=channels,
        categories=categories,
        selected_tags=selected_tags
    )

    



@app.route("/dashboard")
@login_required
def dashboard():

    user_id = session["user_id"]

    purchases = fetch_all("""
        SELECT
            vp.purchase_id,
            vp.amount,
            vp.currency,
            vp.purchased_at,
            v.video_id,
            v.title,
            v.slug,
            v.thumbnail_url,
            c.channel_name
        FROM video_purchases vp
        JOIN videos v
            ON vp.video_id = v.video_id
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE vp.user_id = %s
          AND vp.status = 'paid'
        ORDER BY vp.purchased_at DESC
    """, (user_id,))

    rentals = fetch_all("""
        SELECT
            vr.rental_id,
            vr.amount,
            vr.currency,
            vr.rented_at,
            vr.expires_at,
            vr.status,
            v.video_id,
            v.title,
            v.slug,
            v.thumbnail_url,
            c.channel_name
        FROM video_rentals vr
        JOIN videos v
            ON vr.video_id = v.video_id
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE vr.user_id = %s
          AND vr.status = 'active'
          AND vr.expires_at > NOW()
        ORDER BY vr.expires_at ASC
    """, (user_id,))

    subscriptions = fetch_all("""
        SELECT
            cs.subscription_id,
            cs.amount,
            cs.currency,
            cs.started_at,
            cs.expires_at,
            cs.status,
            c.channel_id,
            c.channel_name,
            c.slug,
            c.channel_logo
        FROM channel_subscriptions cs
        JOIN channels c
            ON cs.channel_id = c.channel_id
        WHERE cs.user_id = %s
          AND cs.status = 'active'
          AND cs.expires_at > NOW()
        ORDER BY cs.expires_at ASC
    """, (user_id,))

    history = fetch_all("""
        SELECT
            wh.progress_seconds,
            wh.completion_percentage,
            wh.last_watched_at,
            v.video_id,
            v.title,
            v.slug,
            v.thumbnail_url,
            v.duration_seconds,
            c.channel_name
        FROM watch_history wh
        JOIN videos v
            ON wh.video_id = v.video_id
        LEFT JOIN channels c
            ON v.channel_id = c.channel_id
        WHERE wh.user_id = %s
        ORDER BY wh.last_watched_at DESC
        LIMIT 12
    """, (user_id,))

    return render_template(
        "dashboard.html",
        purchases=purchases,
        rentals=rentals,
        subscriptions=subscriptions,
        history=history
    )





@app.route('/admin/videos/<int:video_id>/publish', methods=['POST'])
@admin_required
def admin_publish_video(video_id):
    video = fetch_one("\n        SELECT\n            video_id,\n            title,\n            status,\n            bunny_video_id\n        FROM videos\n        WHERE video_id = %s\n          AND status <> 'deleted'\n        LIMIT 1\n        ", (video_id,))
    if not video:
        flash('Video not found.', 'error')
        return redirect(url_for('admin_videos'))
    if not video['bunny_video_id']:
        flash('This video has not been uploaded to Bunny Stream.', 'error')
        return redirect(url_for('admin_videos'))
    if video['status'] not in {'ready', 'published'}:
        flash('This video is not ready for publishing. Wait for Bunny Stream processing to finish.', 'error')
        return redirect(url_for('admin_videos'))
    execute_query("\n        UPDATE videos\n        SET\n            status = 'published',\n            published_at = NOW(),\n            updated_at = NOW()\n        WHERE video_id = %s\n        ", (video_id,))
    flash('Video published successfully.', 'success')
    return redirect(url_for('admin_videos'))

@app.route('/admin/videos/<int:video_id>/unpublish', methods=['POST'])
@admin_required
def admin_unpublish_video(video_id):
    video = fetch_one("\n        SELECT\n            video_id,\n            status\n        FROM videos\n        WHERE video_id = %s\n          AND status <> 'deleted'\n        LIMIT 1\n        ", (video_id,))
    if not video:
        flash('Video not found.', 'error')
        return redirect(url_for('admin_videos'))
    execute_query("\n        UPDATE videos\n        SET\n            status = 'unpublished',\n            updated_at = NOW()\n        WHERE video_id = %s\n        ", (video_id,))
    flash('Video unpublished successfully.', 'success')
    return redirect(url_for('admin_videos'))

@app.route('/admin/payments')
@admin_required
def admin_payments():
    payments = fetch_all('\n        SELECT\n            p.*,\n            u.username,\n            u.full_name,\n            u.email\n        FROM payments p\n        JOIN users u\n            ON u.user_id = p.user_id\n        ORDER BY p.created_at DESC\n        LIMIT 500\n        ')
    return render_template('admin_payments.html', payments=payments)

@app.route('/admin/analytics')
@admin_required
def admin_analytics():

    views_by_day = fetch_all('''
        SELECT
            DATE(started_at) AS view_date,
            COUNT(*) AS total_views
        FROM video_views
        WHERE is_valid_view = TRUE
        GROUP BY DATE(started_at)
        ORDER BY view_date DESC
        LIMIT 30
    ''')

    revenue_by_day = fetch_all('''
        SELECT
            DATE(COALESCE(paid_at, created_at)) AS revenue_date,
            COALESCE(SUM(amount), 0) AS revenue
        FROM payments
        WHERE status = 'successful'
        GROUP BY DATE(COALESCE(paid_at, created_at))
        ORDER BY revenue_date DESC
        LIMIT 30
    ''')

    top_videos = fetch_all('''
        SELECT
            v.video_id,
            v.title,
            v.total_views,
            v.total_likes,
            v.total_comments,
            c.channel_name
        FROM videos v
        JOIN channels c
            ON c.channel_id = v.channel_id
        ORDER BY v.total_views DESC
        LIMIT 20
    ''')

    # ---- Per-video earnings from purchases + rentals ----
    video_earnings = fetch_all('''
        SELECT
            v.video_id,
            v.title,
            v.slug,
            v.currency,
            v.purchase_price,
            v.rental_price,
            c.channel_name,

            COALESCE(SUM(
                CASE WHEN p.payment_type = 'video_purchase'
                     THEN p.amount END
            ), 0) AS purchase_revenue,
            COALESCE(SUM(
                CASE WHEN p.payment_type = 'video_rental'
                     THEN p.amount END
            ), 0) AS rental_revenue,
            COALESCE(SUM(p.amount), 0) AS total_revenue,

            COUNT(DISTINCT CASE
                WHEN p.payment_type = 'video_purchase'
                THEN p.payment_id END
            ) AS purchase_count,
            COUNT(DISTINCT CASE
                WHEN p.payment_type = 'video_rental'
                THEN p.payment_id END
            ) AS rental_count

        FROM videos v
        LEFT JOIN channels c
            ON c.channel_id = v.channel_id
        INNER JOIN payments p
            ON p.related_id = v.video_id
            AND p.payment_type IN ('video_purchase', 'video_rental')
            AND p.status = 'successful'
        GROUP BY
            v.video_id,
            v.title,
            v.slug,
            v.currency,
            v.purchase_price,
            v.rental_price,
            c.channel_name
        HAVING total_revenue > 0
        ORDER BY total_revenue DESC
        LIMIT 100
    ''')

    # Totals for the summary cards
    earnings_summary = fetch_one('''
        SELECT
            COALESCE(SUM(CASE
                WHEN payment_type = 'video_purchase'
                THEN amount END), 0) AS purchase_total,
            COALESCE(SUM(CASE
                WHEN payment_type = 'video_rental'
                THEN amount END), 0) AS rental_total,
            COUNT(DISTINCT CASE
                WHEN payment_type = 'video_purchase'
                THEN payment_id END) AS purchase_count,
            COUNT(DISTINCT CASE
                WHEN payment_type = 'video_rental'
                THEN payment_id END) AS rental_count
        FROM payments
        WHERE status = 'successful'
          AND payment_type IN ('video_purchase', 'video_rental')
    ''') or {
        'purchase_total': 0,
        'rental_total': 0,
        'purchase_count': 0,
        'rental_count': 0,
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
    channel = fetch_one('\n        SELECT\n            channel_id,\n            channel_name,\n            is_active\n        FROM channels\n        WHERE channel_id = %s\n        ', (channel_id,))
    if not channel:
        return ('Channel not found.', 404)
    new_status = not bool(channel['is_active'])
    execute_query('\n        UPDATE channels\n        SET\n            is_active = %s,\n            updated_at = CURRENT_TIMESTAMP\n        WHERE channel_id = %s\n        ', (new_status, channel_id))
    return redirect(url_for('admin_channels'))





@app.route("/api/payment/initialize", methods=["POST"])
@login_required
def initialize_payment():

    if not PAYSTACK_SECRET_KEY:
        return jsonify({
            "success": False,
            "message": "Payments are not configured yet. Add PAYSTACK_SECRET_KEY to .env."
        }), 503

    user_id = session["user_id"]
    user = fetch_one("""
        SELECT *
        FROM users
        WHERE user_id = %s
        LIMIT 1
    """, (user_id,))

    if not user:
        return jsonify({
            "success": False,
            "message": "User account not found."
        }), 404

    data = request.get_json(silent=True) or {}

    payment_type = data.get("payment_type")
    video_id = data.get("video_id")
    channel_id = data.get("channel_id")

    allowed_types = {
        "video_purchase",
        "video_rental",
        "channel_subscription"
    }

    if payment_type not in allowed_types:
        return jsonify({
            "success": False,
            "message": "Invalid payment type."
        }), 400

    amount = None
    currency = "GHS"
    related_id = None
    metadata = {}

    # --------------------------------------------------------
    # VIDEO PURCHASE
    # --------------------------------------------------------

    if payment_type == "video_purchase":

        if not video_id:
            return jsonify({
                "success": False,
                "message": "Video ID is required."
            }), 400

        video = fetch_one("""
            SELECT *
            FROM videos
            WHERE video_id = %s
              AND status = 'published'
            LIMIT 1
        """, (video_id,))

        if not video:
            return jsonify({
                "success": False,
                "message": "Video not found."
            }), 404

        if not video["purchase_enabled"]:
            return jsonify({
                "success": False,
                "message": "Purchase is not available."
            }), 400

        if has_video_access(user_id, video):
            return jsonify({
                "success": True,
                "already_has_access": True,
                "redirect_url": url_for(
                    "watch",
                    slug=video["slug"]
                )
            })

        amount = float(video["purchase_price"])
        currency = video["currency"] or "GHS"
        related_id = video["video_id"]

        metadata = {
            "payment_type": "video_purchase",
            "user_id": user_id,
            "video_id": video["video_id"]
        }

    # --------------------------------------------------------
    # VIDEO RENTAL
    # --------------------------------------------------------

    elif payment_type == "video_rental":

        if not video_id:
            return jsonify({
                "success": False,
                "message": "Video ID is required."
            }), 400

        video = fetch_one("""
            SELECT *
            FROM videos
            WHERE video_id = %s
              AND status = 'published'
            LIMIT 1
        """, (video_id,))

        if not video:
            return jsonify({
                "success": False,
                "message": "Video not found."
            }), 404

        if not video["rental_enabled"]:
            return jsonify({
                "success": False,
                "message": "Rental is not available."
            }), 400

        access_type = get_video_access_type(
            user_id,
            video
        )

        if access_type in (
            "free",
            "admin",
            "purchase",
            "rental",
            "subscription"
        ):
            return jsonify({
                "success": True,
                "already_has_access": True,
                "redirect_url": url_for(
                    "watch",
                    slug=video["slug"]
                )
            })

        amount = float(video["rental_price"])
        currency = video["currency"] or "GHS"
        related_id = video["video_id"]

        metadata = {
            "payment_type": "video_rental",
            "user_id": user_id,
            "video_id": video["video_id"]
        }

    # --------------------------------------------------------
    # CHANNEL SUBSCRIPTION
    # --------------------------------------------------------

    elif payment_type == "channel_subscription":

        if not channel_id:
            return jsonify({
                "success": False,
                "message": "Channel ID is required."
            }), 400

        channel = fetch_one("""
            SELECT *
            FROM channels
            WHERE channel_id = %s
              AND is_active = 1
            LIMIT 1
        """, (channel_id,))

        if not channel:
            return jsonify({
                "success": False,
                "message": "Channel not found."
            }), 404

        existing = fetch_one("""
            SELECT subscription_id
            FROM channel_subscriptions
            WHERE user_id = %s
              AND channel_id = %s
              AND status = 'active'
              AND expires_at > NOW()
            LIMIT 1
        """, (
            user_id,
            channel_id
        ))

        if existing:
            return jsonify({
                "success": True,
                "already_subscribed": True,
                "redirect_url": url_for(
                    "channel",
                    slug=channel["slug"]
                )
            })

        amount = CHANNEL_SUBSCRIPTION_PRICE
        currency = "GHS"
        related_id = channel["channel_id"]

        metadata = {
            "payment_type": "channel_subscription",
            "user_id": user_id,
            "channel_id": channel["channel_id"]
        }

    # --------------------------------------------------------
    # CREATE OUR INTERNAL PAYMENT REFERENCE
    # --------------------------------------------------------

    payment_reference = generate_payment_reference(
        payment_type,
        user_id
    )

    try:

        # Internal payment record
        execute_query("""
            INSERT INTO payments (
                user_id,
                payment_reference,
                payment_type,
                related_id,
                amount,
                currency,
                provider,
                status
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, 'paystack', 'pending'
            )
        """, (
            user_id,
            payment_reference,
            payment_type,
            related_id,
            amount,
            currency
        ))

        # ----------------------------------------------------
        # CREATE PENDING ENTITLEMENT
        # ----------------------------------------------------

        if payment_type == "video_purchase":

            execute_query("""
                INSERT INTO video_purchases (
                    user_id,
                    video_id,
                    amount,
                    currency,
                    payment_reference,
                    status
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'pending'
                )
            """, (
                user_id,
                video_id,
                amount,
                currency,
                payment_reference
            ))

        elif payment_type == "video_rental":

            execute_query("""
                INSERT INTO video_rentals (
                    user_id,
                    video_id,
                    amount,
                    currency,
                    rental_duration_hours,
                    payment_reference,
                    status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'pending'
                )
            """, (
                user_id,
                video_id,
                amount,
                currency,
                VIDEO_RENTAL_DURATION_HOURS,
                payment_reference
            ))

        elif payment_type == "channel_subscription":

            execute_query("""
                INSERT INTO channel_subscriptions (
                    user_id,
                    channel_id,
                    amount,
                    currency,
                    payment_reference,
                    status,
                    auto_renew
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'pending', 0
                )
            """, (
                user_id,
                channel_id,
                amount,
                currency,
                payment_reference
            ))

        # ----------------------------------------------------
        # INITIALIZE PAYSTACK
        # ----------------------------------------------------

        paystack_data = paystack_initialize(
            email=user["email"],
            amount=amount,
            currency=currency,
            reference=payment_reference,
            metadata=metadata
        )

        paystack_reference = paystack_data.get(
            "reference",
            payment_reference
        )

        if paystack_reference != payment_reference:
            raise RuntimeError('Paystack returned an unexpected payment reference.')

        execute_query("""
            UPDATE payments
            SET paystack_reference = %s,
                updated_at = NOW()
            WHERE payment_reference = %s
        """, (
            paystack_reference,
            payment_reference
        ))

        return jsonify({
            "success": True,
            "authorization_url": paystack_data["authorization_url"],
            "access_code": paystack_data.get("access_code"),
            "reference": paystack_reference
        })

    except Exception as e:

        app.logger.exception(
            "Payment initialization failed"
        )

        execute_query("""
            UPDATE payments
            SET status = 'failed',
                gateway_response = %s,
                updated_at = NOW()
            WHERE payment_reference = %s
        """, (
            str(e),
            payment_reference
        ))

        if payment_type == "video_purchase":
            execute_query("""
                UPDATE video_purchases
                SET status = 'failed',
                    updated_at = NOW()
                WHERE payment_reference = %s
            """, (payment_reference,))

        elif payment_type == "video_rental":
            execute_query("""
                UPDATE video_rentals
                SET status = 'failed',
                    updated_at = NOW()
                WHERE payment_reference = %s
            """, (payment_reference,))

        elif payment_type == "channel_subscription":
            execute_query("""
                UPDATE channel_subscriptions
                SET status = 'failed'
                WHERE payment_reference = %s
            """, (payment_reference,))

        return jsonify({
            "success": False,
            "message": "Unable to initialize payment. Please try again."
        }), 500





def payment_success_destination(payment):
    """Return the page a confirmed payer should see next."""
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
    """Never grant access based solely on a browser redirect or webhook body."""
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
    """Verify a Paystack payment and atomically create its entitlement.

    This is used by both the browser callback and the signed webhook, so a
    customer receives access even if they close the checkout tab before being
    redirected back to KSS.
    """
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
        # The row lock makes the callback and a webhook safe to receive in
        # either order; only the first request grants the entitlement.
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

        cursor.execute('''
            UPDATE payments
            SET status = 'successful', paystack_reference = %s, channel = %s,
                gateway_response = %s, paid_at = NOW(), updated_at = NOW()
            WHERE payment_reference = %s AND status = 'pending'
        ''', (
            transaction['reference'], transaction.get('channel'),
            json.dumps(transaction, default=str), reference
        ))

        if locked_payment['payment_type'] == 'video_purchase':
            cursor.execute('''
                UPDATE video_purchases
                SET status = 'paid', purchased_at = COALESCE(purchased_at, NOW()),
                    updated_at = NOW()
                WHERE payment_reference = %s AND status = 'pending'
            ''', (reference,))
        elif locked_payment['payment_type'] == 'video_rental':
            cursor.execute('''
                UPDATE video_rentals
                SET status = 'active', rented_at = NOW(),
                    expires_at = DATE_ADD(NOW(), INTERVAL %s HOUR),
                    updated_at = NOW()
                WHERE payment_reference = %s AND status = 'pending'
            ''', (VIDEO_RENTAL_DURATION_HOURS, reference))
        elif locked_payment['payment_type'] == 'channel_subscription':
            cursor.execute('''
                UPDATE channel_subscriptions
                SET status = 'active', started_at = NOW(),
                    expires_at = DATE_ADD(NOW(), INTERVAL %s DAY)
                WHERE payment_reference = %s AND status = 'pending'
            ''', (CHANNEL_SUBSCRIPTION_DAYS, reference))
            if cursor.rowcount:
                cursor.execute('''
                    UPDATE channels
                    SET subscriber_count = subscriber_count + 1, updated_at = NOW()
                    WHERE channel_id = %s
                ''', (locked_payment['related_id'],))
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
        # Paystack retries non-2xx responses; this is preferable to losing a
        # legitimate entitlement due to a temporary DB/API problem.
        return jsonify({'success': False}), 500
    return jsonify({'success': True}), 200


@app.route("/payment/callback")
def payment_callback():

    reference = request.args.get("reference")

    if not reference:
        return redirect(
            url_for("payment_failed")
        )

    payment = fetch_one("""
        SELECT *
        FROM payments
        WHERE payment_reference = %s
        LIMIT 1
    """, (reference,))

    if not payment:
        return redirect(
            url_for("payment_failed")
        )

    # Already processed
    if payment["status"] == "successful":

        if payment["payment_type"] == "video_purchase":

            video = fetch_one("""
                SELECT slug
                FROM videos
                WHERE video_id = %s
                LIMIT 1
            """, (payment["related_id"],))

            if video:
                return redirect(
                    url_for(
                        "watch",
                        slug=video["slug"]
                    )
                )

        elif payment["payment_type"] == "video_rental":

            video = fetch_one("""
                SELECT slug
                FROM videos
                WHERE video_id = %s
                LIMIT 1
            """, (payment["related_id"],))

            if video:
                return redirect(
                    url_for(
                        "watch",
                        slug=video["slug"]
                    )
                )

        elif payment["payment_type"] == "channel_subscription":

            channel = fetch_one("""
                SELECT slug
                FROM channels
                WHERE channel_id = %s
                LIMIT 1
            """, (payment["related_id"],))

            if channel:
                return redirect(
                    url_for(
                        "channel",
                        slug=channel["slug"]
                    )
                )

    # The shared confirmation path also makes a successful browser callback
    # idempotent with a webhook that may have arrived moments earlier.
    try:
        confirmed_payment = confirm_paystack_payment(reference)
        return redirect(payment_success_destination(confirmed_payment))
    except Exception:
        app.logger.exception('Payment callback confirmation failed for %s', reference)
        return redirect(url_for('payment_failed', reference=reference))

    try:

        verification = paystack_verify(reference)

        if not verification.get("status"):
            raise Exception(
                verification.get(
                    "message",
                    "Payment verification failed."
                )
            )

        transaction = verification.get("data", {})

        transaction_status = transaction.get("status")
        transaction_amount = transaction.get("amount")
        transaction_currency = transaction.get("currency")

        expected_amount = money_to_subunit(
            payment["amount"]
        )

        expected_currency = payment["currency"]

        # ----------------------------------------------------
        # IMPORTANT PAYMENT VALIDATION
        # ----------------------------------------------------

        if transaction_status != "success":
            raise Exception(
                f"Transaction status: {transaction_status}"
            )

        if int(transaction_amount or 0) != expected_amount:
            raise Exception(
                "Payment amount does not match."
            )

        if (
            transaction_currency
            and transaction_currency.upper()
            != expected_currency.upper()
        ):
            raise Exception(
                "Payment currency does not match."
            )

        # ----------------------------------------------------
        # MARK PAYMENT SUCCESSFUL
        # ----------------------------------------------------

        gateway_response = json.dumps(
            transaction,
            default=str
        )

        execute_query("""
            UPDATE payments
            SET status = 'successful',
                paystack_reference = %s,
                channel = %s,
                gateway_response = %s,
                paid_at = NOW(),
                updated_at = NOW()
            WHERE payment_reference = %s
        """, (
            transaction.get("reference"),
            transaction.get("channel"),
            gateway_response,
            reference
        ))

        # ----------------------------------------------------
        # VIDEO PURCHASE
        # ----------------------------------------------------

        if payment["payment_type"] == "video_purchase":

            execute_query("""
                UPDATE video_purchases
                SET status = 'paid',
                    purchased_at = COALESCE(
                        purchased_at,
                        NOW()
                    ),
                    updated_at = NOW()
                WHERE payment_reference = %s
            """, (reference,))

            video = fetch_one("""
                SELECT
                    video_id,
                    slug,
                    title
                FROM videos
                WHERE video_id = %s
                LIMIT 1
            """, (payment["related_id"],))

            if video:

                execute_query("""
                    INSERT INTO notifications (
                        user_id,
                        notification_type,
                        title,
                        message,
                        related_video_id
                    )
                    VALUES (
                        %s,
                        'payment',
                        %s,
                        %s,
                        %s
                    )
                """, (
                    payment["user_id"],
                    "Video purchase successful",
                    f"You now have permanent access to "
                    f"\"{video['title']}\".",
                    video["video_id"]
                ))

                return redirect(
                    url_for(
                        "watch",
                        slug=video["slug"]
                    )
                )

        # ----------------------------------------------------
        # VIDEO RENTAL
        # ----------------------------------------------------

        elif payment["payment_type"] == "video_rental":

            rental = fetch_one("""
                SELECT rental_duration_hours
                FROM video_rentals
                WHERE payment_reference = %s
                LIMIT 1
            """, (reference,))

            if not rental:
                raise Exception(
                    "Rental record not found."
                )

            hours = int(
                rental["rental_duration_hours"]
            )

            expires_at = (
                datetime.now()
                + timedelta(hours=hours)
            )

            execute_query("""
                UPDATE video_rentals
                SET status = 'active',
                    rented_at = NOW(),
                    expires_at = %s,
                    updated_at = NOW()
                WHERE payment_reference = %s
            """, (
                expires_at,
                reference
            ))

            video = fetch_one("""
                SELECT
                    video_id,
                    slug,
                    title
                FROM videos
                WHERE video_id = %s
                LIMIT 1
            """, (payment["related_id"],))

            if video:

                execute_query("""
                    INSERT INTO notifications (
                        user_id,
                        notification_type,
                        title,
                        message,
                        related_video_id
                    )
                    VALUES (
                        %s,
                        'rental_expiry',
                        %s,
                        %s,
                        %s
                    )
                """, (
                    payment["user_id"],
                    "Video rental activated",
                    f"Your rental of \"{video['title']}\" "
                    f"is active for {hours} hours.",
                    video["video_id"]
                ))

                return redirect(
                    url_for(
                        "watch",
                        slug=video["slug"]
                    )
                )

        # ----------------------------------------------------
        # CHANNEL SUBSCRIPTION
        # ----------------------------------------------------

        elif payment["payment_type"] == "channel_subscription":

            expires_at = (
                datetime.now()
                + timedelta(
                    days=CHANNEL_SUBSCRIPTION_DAYS
                )
            )

            execute_query("""
                UPDATE channel_subscriptions
                SET status = 'active',
                    started_at = NOW(),
                    expires_at = %s
                WHERE payment_reference = %s
            """, (
                expires_at,
                reference
            ))

            execute_query("""
                UPDATE channels
                SET subscriber_count =
                    subscriber_count + 1,
                    updated_at = NOW()
                WHERE channel_id = %s
            """, (
                payment["related_id"],
            ))

            channel = fetch_one("""
                SELECT
                    channel_id,
                    slug,
                    channel_name
                FROM channels
                WHERE channel_id = %s
                LIMIT 1
            """, (payment["related_id"],))

            if channel:

                execute_query("""
                    INSERT INTO notifications (
                        user_id,
                        notification_type,
                        title,
                        message,
                        related_channel_id
                    )
                    VALUES (
                        %s,
                        'subscription',
                        %s,
                        %s,
                        %s
                    )
                """, (
                    payment["user_id"],
                    "Subscription successful",
                    f"You are now subscribed to "
                    f"{channel['channel_name']}.",
                    channel["channel_id"]
                ))

                return redirect(
                    url_for(
                        "channel",
                        slug=channel["slug"]
                    )
                )

        return redirect(
            url_for("dashboard")
        )

    except Exception as e:

        app.logger.exception(
            "Payment verification failed"
        )

        execute_query("""
            UPDATE payments
            SET status = 'failed',
                gateway_response = %s,
                updated_at = NOW()
            WHERE payment_reference = %s
        """, (
            str(e),
            reference
        ))

        if payment["payment_type"] == "video_purchase":

            execute_query("""
                UPDATE video_purchases
                SET status = 'failed',
                    updated_at = NOW()
                WHERE payment_reference = %s
            """, (reference,))

        elif payment["payment_type"] == "video_rental":

            execute_query("""
                UPDATE video_rentals
                SET status = 'failed',
                    updated_at = NOW()
                WHERE payment_reference = %s
            """, (reference,))

        elif payment["payment_type"] == "channel_subscription":

            execute_query("""
                UPDATE channel_subscriptions
                SET status = 'failed'
                WHERE payment_reference = %s
            """, (reference,))

        return redirect(
            url_for(
                "payment_failed",
                reference=reference
            )
        )




@app.route("/payment/failed")
def payment_failed():

    reference = request.args.get("reference")

    return render_template(
        "payment_failed.html",
        reference=reference
    )



@app.route(
    "/api/channel/<int:channel_id>/follow",
    methods=["POST"]
)
@login_required
def toggle_channel_follow(channel_id):

    user_id = session["user_id"]

    channel = fetch_one("""
        SELECT channel_id
        FROM channels
        WHERE channel_id = %s
          AND is_active = 1
        LIMIT 1
    """, (channel_id,))

    if not channel:
        return jsonify({
            "success": False,
            "message": "Channel not found."
        }), 404

    existing = fetch_one("""
        SELECT *
        FROM channel_followers
        WHERE user_id = %s
          AND channel_id = %s
        LIMIT 1
    """, (
        user_id,
        channel_id
    ))

    if existing:

        execute_query("""
            DELETE FROM channel_followers
            WHERE user_id = %s
              AND channel_id = %s
        """, (
            user_id,
            channel_id
        ))

        following = False

    else:

        execute_query("""
            INSERT INTO channel_followers (
                user_id,
                channel_id
            )
            VALUES (%s, %s)
        """, (
            user_id,
            channel_id
        ))

        following = True

    count_row = fetch_one("""
        SELECT COUNT(*) AS total
        FROM channel_followers
        WHERE channel_id = %s
    """, (
        channel_id,
    ))

    return jsonify({
        "success": True,
        "following": following,
        "followers": (
            count_row["total"]
            if count_row
            else 0
        )
    })













@app.route('/health')
def health():
    connection = get_db_connection()
    if connection:
        try:
            connection.close()
            return jsonify({'status': 'ok', 'database': 'connected'})
        except Exception:
            pass
    return (jsonify({'status': 'error', 'database': 'unavailable'}), 500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5003')), debug=True)