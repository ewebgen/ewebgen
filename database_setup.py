import os
import io
import zipfile
import re
import json
import random
import requests
import string
import logging
import smtplib
import threading
import time
import uuid
import shutil
# --- FIX: Import quote_plus for safe password encoding ---
from urllib.parse import quote_plus
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

# Load environment variables
from dotenv import load_dotenv
# Compiled (Nuitka) builds: .env lives in the data folder / working directory, not next to the binary
if os.getenv('EWEBGEN_DATA_DIR'):
    load_dotenv(os.path.join(os.getenv('EWEBGEN_DATA_DIR'), '.env'))
load_dotenv(os.path.join(os.getcwd(), '.env'))
load_dotenv()

# SQLAlchemy OR logic
from sqlalchemy import or_, text, desc, func

from flask import Flask, request, jsonify, send_file, render_template, Response, stream_with_context, redirect, url_for, render_template_string, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Desktop app keeps user data (projects, uploads, DB) in a writable per-user folder.
DATA_DIR = os.path.abspath(os.getenv('EWEBGEN_DATA_DIR') or BASE_DIR)
PROJECTS_DIR = os.path.join(DATA_DIR, 'projects')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
UPDATES_FOLDER = os.path.join(DATA_DIR, 'updates_repo')
BACKUPS_FOLDER = os.path.join(DATA_DIR, 'backups') 
UNSAVED_DIR = os.path.join(DATA_DIR, 'unsaved_projects')

# Auto-create necessary directories
for folder in [PROJECTS_DIR, UPLOAD_FOLDER, UPDATES_FOLDER, BACKUPS_FOLDER, UNSAVED_DIR]:
    os.makedirs(folder, exist_ok=True)

# --- GOAL 3 & 5 FIX: ALLOW VIDEO EXTENSIONS GLOBALLY ---
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'txt', 'doc', 'docx', 'zip', 'mp4', 'webm', 'ogg', 'mov'}

# --- FLASK APP SETUP ---
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# SECURITY FIX: Remove hardcoded fallback secret key to prevent Session Hijacking.
# If no key is provided in the environment, generate a cryptographically secure random key for this runtime.
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY') or os.urandom(24).hex()

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['UPDATES_FOLDER'] = UPDATES_FOLDER
app.config['UNSAVED_DIR'] = UNSAVED_DIR
# FIX: INCREASE LIMIT TO 500MB TO ALLOW LARGE TEMPLATE AND VIDEO UPLOADS
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

# --- DATABASE SETUP (MYSQL MIGRATION) ---
db_user = os.getenv('DB_USER', 'root')
db_pass = os.getenv('DB_PASS', '')
db_host = os.getenv('DB_HOST', 'localhost')
db_port = os.getenv('DB_PORT', '3306')
db_name = os.getenv('DB_NAME', 'database_name')

if db_pass:
    encoded_pass = quote_plus(db_pass)
else:
    encoded_pass = ""

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

def _is_desktop_edition():
    if str(os.getenv('EWEBGEN_DESKTOP', '')).strip().lower() in ('1', 'true', 'yes'):
        return True
    try:
        with open(os.path.join(BASE_DIR, 'edition.txt'), 'r', encoding='utf-8') as _f:
            return _f.read().strip().lower().startswith('desktop')
    except OSError:
        return False

# Desktop edition always uses SQLite (no MySQL); EWEBGEN_DB=sqlite also forces it
if os.getenv('EWEBGEN_DB', '').strip().lower() == 'sqlite' or (not os.getenv('EWEBGEN_DB') and _is_desktop_edition()):
    # Standalone desktop app: zero-setup local database file
    _sqlite_path = os.path.join(DATA_DIR, 'ewebgen.db').replace('\\', '/')
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{_sqlite_path}"
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'check_same_thread': False, 'timeout': 30}}
else:
    # Server install: MySQL
    app.config['SQLALCHEMY_DATABASE_URI'] = f"mysql+pymysql://{db_user}:{encoded_pass}@{db_host}:{db_port}/{db_name}?charset=utf8mb4"
    # MySQL Optimization: Recycle connections to prevent timeouts
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_recycle': 280,
        'pool_pre_ping': True
    }

# --- DATABASE INSTANCE ---
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ==========================================
# FIX: PREVENT FLASK-LOGIN REDIRECT LOOPS
# ==========================================
@login_manager.unauthorized_handler
def unauthorized_handler():
    if request.path.startswith('/api/'):
        return jsonify({"error": "Unauthorized access. Please log in.", "authenticated": False}), 401
    return redirect('/')

# --- MODELS ---

class UserType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    project_limit = db.Column(db.Integer, default=3, nullable=False)
    is_default = db.Column(db.Boolean, default=False) 
    image_limit = db.Column(db.Integer, default=-1) # -1 means unlimited
    storage_limit_mb = db.Column(db.Integer, default=500)
    # NEW: Allow users on this tier to use their own AI API Keys
    allow_own_api_keys = db.Column(db.Boolean, default=False)
    # NEW: Allow downloading of zip files (Goal 2)
    allow_download = db.Column(db.Boolean, default=False)

class SubscriptionPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default='USD')
    # GOAL 3: Added billing cycle column
    billing_cycle = db.Column(db.String(20), default='monthly') 
    features = db.Column(db.Text, nullable=True) 
    
    user_type_id = db.Column(db.Integer, db.ForeignKey('user_type.id'), nullable=False)
    user_type = db.relationship('UserType', backref='plans')
    
    is_active = db.Column(db.Boolean, default=True)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    
    password_hash = db.Column(db.String(255), nullable=False)
    
    role = db.Column(db.String(20), default='client') 
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(6), nullable=True)
    
    full_name = db.Column(db.String(150), nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=True)
    phone_number = db.Column(db.String(20), unique=True, nullable=True)
    country = db.Column(db.String(50), nullable=True) # Added for country requirement
    language = db.Column(db.String(20), default='en') # Added for multi-language support
    
    force_password_reset = db.Column(db.Boolean, default=False)

    is_blocked = db.Column(db.Boolean, default=False)
    is_muted = db.Column(db.Boolean, default=False)
    
    images_generated = db.Column(db.Integer, default=0) # Tracker for generated images
    
    # --- GOAL 4 FIX: TRACK WEBSITES GENERATED FOR SUBSCRIPTION LIMITS ---
    websites_generated = db.Column(db.Integer, default=0)
    
    # --- NEW: Additional limit carried over from previous plans (Goal 15) ---
    extra_project_limit = db.Column(db.Integer, default=0)
    # --- NEW: Additional limit for images ---
    extra_image_limit = db.Column(db.Integer, default=0)

    user_type_id = db.Column(db.Integer, db.ForeignKey('user_type.id'), nullable=True)
    user_type = db.relationship('UserType', backref='users')
    
    # NEW: Override/Individual setting for own API keys
    allow_own_api_keys = db.Column(db.Boolean, default=False)
    # NEW: JSON store for user's personal Google Analytics, SMTP, and AI Keys
    user_settings_json = db.Column(db.Text, default='{}')

    # SINGLE ACTIVE SESSION TRACKER
    session_token = db.Column(db.String(255), nullable=True)

    # NEW: Store encrypted FTP credentials for easy 1-click deployments/imports
    ftp_credentials = db.Column(db.Text, default='{}')

class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    
    ai_provider = db.Column(db.String(50), default='gemini') 
    
    # --- API KEY OVERFLOW FIX: Changed from String(200) to Text to support long keys ---
    gemini_api_key = db.Column(db.Text, nullable=True) 
    openai_api_key = db.Column(db.Text, nullable=True)
    xai_api_key = db.Column(db.Text, nullable=True) 
    meta_api_key = db.Column(db.Text, nullable=True) 
    perplexity_api_key = db.Column(db.Text, nullable=True)
    
    # NEW AI PROVIDERS (Claude & DeepSeek)
    claude_api_key = db.Column(db.Text, nullable=True)
    deepseek_api_key = db.Column(db.Text, nullable=True)
    
    pexels_api_key = db.Column(db.Text, nullable=True)
    
    default_chat_state = db.Column(db.String(20), default='default') # Floating/Medium default

    provider_keys_json = db.Column(db.Text, default='{}')

    concurrent_limit_enabled = db.Column(db.Boolean, default=False)
    concurrent_limit = db.Column(db.Integer, default=5)
    
    top_menu_color = db.Column(db.String(20), default='#212529')
    
    voice_silence_duration = db.Column(db.Integer, default=2000)
    
    store_images_locally = db.Column(db.Boolean, default=True) 

    gemini_keys_json = db.Column(db.Text, default='[]') 
    max_users_per_agent = db.Column(db.Integer, default=5) 
    max_concurrent_agents = db.Column(db.Integer, default=10) 

    image_providers_json = db.Column(db.Text, default='{}')

    email_method = db.Column(db.String(20), default='smtp')
    
    smtp_host = db.Column(db.String(120), nullable=True)
    smtp_port = db.Column(db.Integer, default=587)
    smtp_user = db.Column(db.String(120), nullable=True)
    smtp_pass = db.Column(db.String(120), nullable=True)
    smtp_from_email = db.Column(db.String(120), nullable=True)
    
    email_api_url = db.Column(db.String(255), nullable=True)
    email_api_key = db.Column(db.String(255), nullable=True)
    
    image_search_limit = db.Column(db.Integer, default=10) 
    
    default_currency = db.Column(db.String(10), default='USD') 

    # --- NEW FIELDS ADDED FOR NEW FEATURES ---
    google_client_id = db.Column(db.String(255), nullable=True)
    google_client_secret = db.Column(db.String(255), nullable=True)
    
    logo_url = db.Column(db.String(255), default='https://ewebgen.com/images/logo.png')
    logo_width = db.Column(db.String(20), default='120px')
    logo_height = db.Column(db.String(20), default='auto')
    
    login_left_title = db.Column(db.String(255), default='Welcome to Ewebgen')
    login_left_content = db.Column(db.Text, default='<p>Ewebgen is your premier AI website builder.<br><br>Main website: <a href="https://ewebgen.com" target="_blank">ewebgen.com</a><br>Promotions: <a href="https://ordersell.com" target="_blank">ordersell.com</a><br>Company: <strong>Orgif Solutions LLP</strong></p><p><strong>Upcoming Features:</strong><br>- Blogging Website Builder<br>- Shopping Website Builder<br>- And much more...</p>')
    terms_content = db.Column(db.Text, default='<h2>Terms and Conditions</h2><p>Please read these terms and conditions carefully...</p>')
    privacy_content = db.Column(db.Text, default='<h2>Privacy Policy</h2><p>Your privacy is important to us...</p>')
    gdpr_cookie_msg = db.Column(db.Text, default='We use cookies to ensure you get the best experience on our website.')
    show_powered_by = db.Column(db.Boolean, default=True) # Option to toggle "Powered by EWebGen"
    show_live_preview = db.Column(db.Boolean, default=False) # Option to show website generation live preview

    # --- NEW FEATURE: Option to hide Country/Language popup on first login ---
    hide_onboarding_popup = db.Column(db.Boolean, default=False)

    # --- FEATURE 3 FIX: INDIVIDUAL AI CONFIGURATION FIELDS ---
    model_gemini = db.Column(db.String(100), default='gemini-2.0-flash')
    image_model_gemini = db.Column(db.String(100), default='imagen-3.0-generate-002') # NEW IMAGE MODEL
    token_limit_gemini = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_gemini = db.Column(db.Text, nullable=True)
    
    model_openai = db.Column(db.String(100), default='gpt-4o')
    image_model_openai = db.Column(db.String(100), default='dall-e-3') # NEW IMAGE MODEL
    token_limit_openai = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_openai = db.Column(db.Text, nullable=True)
    
    model_grok = db.Column(db.String(100), default='grok-beta')
    image_model_grok = db.Column(db.String(100), default='') # NEW IMAGE MODEL
    token_limit_grok = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_grok = db.Column(db.Text, nullable=True)
    
    model_meta = db.Column(db.String(100), default='llama3-70b-8192')
    image_model_meta = db.Column(db.String(100), default='') # NEW IMAGE MODEL
    token_limit_meta = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_meta = db.Column(db.Text, nullable=True)
    
    model_perplexity = db.Column(db.String(100), default='llama-3.1-sonar-large-128k-online')
    image_model_perplexity = db.Column(db.String(100), default='') # NEW IMAGE MODEL
    token_limit_perplexity = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_perplexity = db.Column(db.Text, nullable=True)
    
    # NEW MODELS FOR CLAUDE & DEEPSEEK
    model_claude = db.Column(db.String(100), default='claude-3-5-sonnet-20241022')
    image_model_claude = db.Column(db.String(100), default='') # NEW IMAGE MODEL
    token_limit_claude = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_claude = db.Column(db.Text, nullable=True)
    
    model_deepseek = db.Column(db.String(100), default='deepseek-chat')
    image_model_deepseek = db.Column(db.String(100), default='') # NEW IMAGE MODEL
    token_limit_deepseek = db.Column(db.String(50), default='') # NEW: TOKEN LIMIT
    inst_deepseek = db.Column(db.Text, nullable=True)
    
    design_guidelines = db.Column(db.Text, nullable=True)

    # --- GOAL 3 FIX: Dynamic Default Pages Storage ---
    default_pages_json = db.Column(db.Text, nullable=True, default='[]')
    
    # NEW: Tax Settings
    tax_enabled = db.Column(db.Boolean, default=False)
    tax_percentage = db.Column(db.Float, default=0.0)

    # --- GOAL 1 FIX: Webhook Forwarding for API Keys ---
    forward_api_keys_to_owner = db.Column(db.Boolean, default=False)


class UserClipboard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False) 
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class GenerationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    content = db.Column(db.Text, nullable=False)
    
    # FIX: Explicit User relationship fixes the "Network Error" on Admin CMS Logs
    user = db.relationship('User', backref='generation_logs')

# ========================================================
# --- NEW: USER ACTIVITY LOG (Goal 1 & 2 Implementation) ---
# ========================================================
class UserActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action_type = db.Column(db.String(50), nullable=False) # 'website_generated', 'image_generated', 'project_edited', 'order_placed', 'project_saved'
    description = db.Column(db.Text, nullable=False)
    reference_id = db.Column(db.String(100), nullable=True) # Unsaved Project ID or other references
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=True) # For unsaved websites, +30 days
    user = db.relationship('User', backref='activity_logs')

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sender = db.Column(db.String(50), default='System')
    subject = db.Column(db.String(150), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    thread_id = db.Column(db.String(50), nullable=True) 
    attachment = db.Column(db.String(255), nullable=True) 

class UserMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject = db.Column(db.String(150), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False) 
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    thread_id = db.Column(db.String(50), nullable=True) 
    attachment = db.Column(db.String(255), nullable=True) 

class PaymentGateway(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False) 
    code = db.Column(db.String(50), unique=True, nullable=False) 
    is_active = db.Column(db.Boolean, default=False)
    
    config_json = db.Column(db.Text, default='{}') 
    
    mode = db.Column(db.String(20), default='sandbox') 

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    gateway_id = db.Column(db.Integer, db.ForeignKey('payment_gateway.id'), nullable=False)
    
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='USD')
    
    status = db.Column(db.String(20), default='pending') 
    
    transaction_id = db.Column(db.String(100), nullable=True) 
    order_id = db.Column(db.String(100), nullable=True) 
    
    response_json = db.Column(db.Text, nullable=True) 
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', backref='transactions')
    gateway = db.relationship('PaymentGateway', backref='transactions')

class SystemUpdate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    version = db.Column(db.String(20), unique=True, nullable=False) 
    release_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    description = db.Column(db.Text, nullable=True) 
    zip_filename = db.Column(db.String(255), nullable=False) 
    
    download_count = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True) 
    is_latest = db.Column(db.Boolean, default=False) 

# ========================================================
# --- NEW: TEMPLATES AND CATEGORIES MODELS ---
# ========================================================

class TemplateCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    items = db.relationship('TemplateItem', backref='category', cascade='all, delete-orphan')

class TemplateItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('template_category.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    thumbnail_url = db.Column(db.String(255), nullable=True)
    
    # HTML Content is now optional (for snippets)
    content_html = db.Column(db.Text, nullable=True)
    
    # FULL PROJECT DATA (For Multi-page templates) - Using massive text size for MySQL LONGTEXT
    project_data_json = db.Column(db.Text(4294967295), nullable=True)
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

# ========================================================
# --- NEW: USER SAVED SECTIONS (Goal 1 Implementation) ---
# ========================================================
class UserSavedSection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    content_html = db.Column(db.Text(4294967295), nullable=False) # Longtext support for large sections
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ========================================================
# --- EXTERNAL LICENSE SYSTEM MODELS (RESTORED) ---
# ========================================================

class LicensePlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    plan_type = db.Column(db.String(20), default='lifetime') # monthly, yearly, lifetime
    price = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default='USD')
    features = db.Column(db.Text, nullable=True) # Newline separated
    url_slug = db.Column(db.String(100), unique=True, nullable=False) # For unique checkout URLs
    is_active = db.Column(db.Boolean, default=True)

class IssuedLicense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey('license_plan.id'), nullable=True)
    
    # Identifiers (Domain for Web, Hardware ID for Desktop)
    domain_name = db.Column(db.String(255), nullable=True)
    hardware_id = db.Column(db.String(255), nullable=True)
    
    license_key = db.Column(db.String(100), unique=True, nullable=False)
    target_type = db.Column(db.String(20), default='website') # website or app
    plan_type = db.Column(db.String(20), default='lifetime') # Inherited from plan for safety
    
    status = db.Column(db.String(20), default='active') # active, suspended, expired
    suspend_reason = db.Column(db.Text, nullable=True)
    
    # Security & Verification Fields
    failed_attempts = db.Column(db.Integer, default=0)
    lockout_until = db.Column(db.DateTime, nullable=True)
    last_verified_date = db.Column(db.DateTime, nullable=True)
    
    issue_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expiry_date = db.Column(db.DateTime, nullable=True) # Null for lifetime
    
    user = db.relationship('User', backref='issued_licenses')
    plan = db.relationship('LicensePlan', backref='issued_licenses')

class BannedDomain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain_name = db.Column(db.String(255), unique=True, nullable=False)
    reason = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class UnauthorizedAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    license_key = db.Column(db.String(100), nullable=True)
    domain_name = db.Column(db.String(255), nullable=True)
    hardware_id = db.Column(db.String(255), nullable=True)
    ip_address = db.Column(db.String(50), nullable=True)
    reason = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    status = db.Column(db.String(20), default='pending_review') # pending_review, ignored, banned

# ========================================================
# --- NEW: HELP & KNOWLEDGE BASE, HIRE REQUEST, FEEDBACK ---
# ========================================================
class HelpArticle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(255), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100), default='General')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class HireRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    country = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default='Pending') # Pending, Contacted, Completed
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    user = db.relationship('User', backref='hire_requests')

class UserFeedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    rating = db.Column(db.Integer, default=5) # 1 to 5 rating
    message = db.Column(db.Text, nullable=True)
    admin_reply = db.Column(db.Text, nullable=True)
    is_replied = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    replied_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User', backref='feedbacks')

# ========================================================
# --- NEW: API KEY PURCHASE REQUEST MODEL ---
# ========================================================
class ApiKeyRequest(db.Model):
    __tablename__ = 'api_key_request'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    provider = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(30), default='Pending') # Pending, Completed, Cancelled
    assigned_key = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    user = db.relationship('User', backref='api_key_purchase_requests')

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- DB INIT, MIGRATION & DEFAULT ADMIN ---

def init_db():
    with app.app_context():
        # Create tables if they don't exist
        db.create_all()
        
        inspector = db.inspect(db.engine)
        user_columns = [c['name'] for c in inspector.get_columns('user')]
        settings_columns = [c['name'] for c in inspector.get_columns('system_settings')]
        notif_columns = [c['name'] for c in inspector.get_columns('notification')]
        msg_columns = [c['name'] for c in inspector.get_columns('user_message')]
        
        ut_columns = [c['name'] for c in inspector.get_columns('user_type')]
        pg_columns = [c['name'] for c in inspector.get_columns('payment_gateway')]
        
        tables = inspector.get_table_names()

        with db.engine.connect() as conn:
            
	
            # --- MIGRATIONS FOR GOALS 2 & 15 ---
            if 'allow_download' not in ut_columns:
                conn.execute(text("ALTER TABLE user_type ADD COLUMN allow_download BOOLEAN DEFAULT FALSE"))
                # Optionally auto-enable for Pro if it existed
                try: conn.execute(text("UPDATE user_type SET allow_download = TRUE WHERE name = 'Pro'"))
                except: pass
                
            if 'extra_project_limit' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN extra_project_limit INTEGER DEFAULT 0"))
            
            if 'extra_image_limit' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN extra_image_limit INTEGER DEFAULT 0"))
            
            # --- NEW MIGRATIONS FOR USER SETTINGS & AI KEYS (Feature 2 & 3 & 4) ---
            if 'allow_own_api_keys' not in ut_columns:
                conn.execute(text("ALTER TABLE user_type ADD COLUMN allow_own_api_keys BOOLEAN DEFAULT FALSE"))
            if 'allow_own_api_keys' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN allow_own_api_keys BOOLEAN DEFAULT FALSE"))
            if 'user_settings_json' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN user_settings_json TEXT"))

            # --- SINGLE SESSION TRACKER MIGRATION ---
            if 'session_token' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN session_token VARCHAR(255)"))
                
            # --- NEW: MIGRATION FOR FTP CREDENTIALS ---
            if 'ftp_credentials' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN ftp_credentials TEXT"))

            # --- GOAL 3: MIGRATION FOR BILLING CYCLE ---
            if 'subscription_plan' in tables:
                sp_columns = [c['name'] for c in inspector.get_columns('subscription_plan')]
                if 'billing_cycle' not in sp_columns:
                    conn.execute(text("ALTER TABLE subscription_plan ADD COLUMN billing_cycle VARCHAR(20) DEFAULT 'monthly'"))

            # --- NEW MIGRATIONS FOR FULL PROJECT TEMPLATES ---
            if 'template_item' in tables:
                ti_columns = [c['name'] for c in inspector.get_columns('template_item')]
                if 'project_data_json' not in ti_columns:
                    print("Migrating TemplateItem: Adding 'project_data_json' for full templates...")
                    try:
                        conn.execute(text("ALTER TABLE template_item ADD COLUMN project_data_json LONGTEXT"))
                    except:
                        pass # Fallback if dialect doesn't support LONGTEXT directly in raw SQL
                
                try:
                    conn.execute(text("ALTER TABLE template_item MODIFY content_html TEXT NULL"))
                except:
                    pass


            # --- MIGRATIONS FOR LICENSE SYSTEM (RESTORED) ---
            if 'license_plan' in tables:
                lp_columns = [c['name'] for c in inspector.get_columns('license_plan')]
                if 'plan_type' not in lp_columns:
                    conn.execute(text("ALTER TABLE license_plan ADD COLUMN plan_type VARCHAR(20) DEFAULT 'lifetime'"))
                if 'url_slug' not in lp_columns:
                    conn.execute(text("ALTER TABLE license_plan ADD COLUMN url_slug VARCHAR(100)"))
                    
            if 'issued_license' in tables:
                il_columns = [c['name'] for c in inspector.get_columns('issued_license')]
                if 'target_type' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN target_type VARCHAR(20) DEFAULT 'website'"))
                if 'plan_type' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN plan_type VARCHAR(20) DEFAULT 'lifetime'"))
                if 'suspend_reason' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN suspend_reason TEXT"))
                if 'issue_date' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN issue_date DATETIME"))
                if 'expiry_date' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN expiry_date DATETIME"))
                
                # --- NEW HARDWARE ID & SECURITY MIGRATIONS ---
                if 'hardware_id' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN hardware_id VARCHAR(255)"))
                if 'failed_attempts' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN failed_attempts INTEGER DEFAULT 0"))
                if 'lockout_until' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN lockout_until DATETIME"))
                if 'last_verified_date' not in il_columns:
                    conn.execute(text("ALTER TABLE issued_license ADD COLUMN last_verified_date DATETIME"))
                
                try:
                    conn.execute(text("ALTER TABLE issued_license MODIFY domain_name VARCHAR(255) NULL"))
                except:
                    pass
                    
            if 'banned_domain' in tables:
                bd_columns = [c['name'] for c in inspector.get_columns('banned_domain')]
                if 'timestamp' not in bd_columns:
                    conn.execute(text("ALTER TABLE banned_domain ADD COLUMN timestamp DATETIME"))


            # --- MIGRATION FOR PAYMENT GATEWAY ---
            if 'mode' not in pg_columns:
                conn.execute(text("ALTER TABLE payment_gateway ADD COLUMN mode VARCHAR(20) DEFAULT 'sandbox'"))
            if 'config_json' not in pg_columns:
                conn.execute(text("ALTER TABLE payment_gateway ADD COLUMN config_json TEXT"))

            # --- MIGRATION FOR USER TYPE DEFAULT ---
            if 'is_default' not in ut_columns:
                conn.execute(text("ALTER TABLE user_type ADD COLUMN is_default BOOLEAN DEFAULT FALSE"))
                conn.execute(text("UPDATE user_type SET is_default = TRUE WHERE name = 'Free'"))
                
            # --- MIGRATION FOR LIMITS ---
            if 'image_limit' not in ut_columns:
                conn.execute(text("ALTER TABLE user_type ADD COLUMN image_limit INTEGER DEFAULT -1"))
            if 'storage_limit_mb' not in ut_columns:
                conn.execute(text("ALTER TABLE user_type ADD COLUMN storage_limit_mb INTEGER DEFAULT 500"))
            
            # --- MIGRATIONS FOR USER TABLE ---
            if 'full_name' not in user_columns:
                conn.execute(text('ALTER TABLE user ADD COLUMN full_name VARCHAR(150)'))
            if 'username' not in user_columns:
                conn.execute(text('ALTER TABLE user ADD COLUMN username VARCHAR(80)'))
            if 'phone_number' not in user_columns:
                conn.execute(text('ALTER TABLE user ADD COLUMN phone_number VARCHAR(20)'))
            if 'country' not in user_columns:
                conn.execute(text('ALTER TABLE user ADD COLUMN country VARCHAR(50)')) # Added Country
            if 'language' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN language VARCHAR(20) DEFAULT 'en'")) # Added Language
            if 'force_password_reset' not in user_columns: 
                conn.execute(text("ALTER TABLE user ADD COLUMN force_password_reset BOOLEAN DEFAULT FALSE"))
            if 'images_generated' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN images_generated INTEGER DEFAULT 0"))
                
            # --- FIX GOAL 4: Migration to track generated websites ---
            if 'websites_generated' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN websites_generated INTEGER DEFAULT 0"))
            
            # Spam Control Migration
            if 'is_blocked' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN is_blocked BOOLEAN DEFAULT FALSE"))
            if 'is_muted' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN is_muted BOOLEAN DEFAULT FALSE"))

            # User Type Migration
            if 'user_type_id' not in user_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN user_type_id INTEGER DEFAULT NULL"))
                conn.execute(text("ALTER TABLE user ADD CONSTRAINT fk_user_type FOREIGN KEY (user_type_id) REFERENCES user_type(id)"))
            
            # --- MIGRATIONS FOR MESSAGING (Threading & Attachments) ---
            if 'thread_id' not in notif_columns:
                conn.execute(text("ALTER TABLE notification ADD COLUMN thread_id VARCHAR(50)"))
            if 'attachment' not in notif_columns:
                conn.execute(text("ALTER TABLE notification ADD COLUMN attachment VARCHAR(255)"))
            
            if 'thread_id' not in msg_columns:
                conn.execute(text("ALTER TABLE user_message ADD COLUMN thread_id VARCHAR(50)"))
            if 'attachment' not in msg_columns:
                conn.execute(text("ALTER TABLE user_message ADD COLUMN attachment VARCHAR(255)"))

            # --- MIGRATIONS FOR SETTINGS TABLE ---
            if 'email_method' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN email_method VARCHAR(20) DEFAULT 'smtp'"))
            if 'email_api_url' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN email_api_url VARCHAR(255)"))
            if 'email_api_key' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN email_api_key VARCHAR(255)"))
            
            if 'gemini_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN gemini_api_key TEXT"))
            if 'ai_provider' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN ai_provider VARCHAR(50) DEFAULT 'gemini'"))
            if 'openai_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN openai_api_key TEXT"))
            if 'xai_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN xai_api_key TEXT"))
            if 'meta_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN meta_api_key TEXT"))
            if 'perplexity_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN perplexity_api_key TEXT"))

            # --- NEW API KEYS FOR CLAUDE & DEEPSEEK ---
            if 'claude_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN claude_api_key TEXT"))
            if 'deepseek_api_key' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN deepseek_api_key TEXT"))

            # --- FIX: ALWAYS FORCE EXISTING API KEY COLUMNS TO TEXT TO PREVENT 1406 DATA TOO LONG ERRORS ---
            try:
                conn.execute(text("ALTER TABLE system_settings MODIFY gemini_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY openai_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY xai_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY meta_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY perplexity_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY claude_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY deepseek_api_key TEXT"))
                conn.execute(text("ALTER TABLE system_settings MODIFY pexels_api_key TEXT"))
            except Exception as e:
                print(f"API Key Migration Info: {e}")

            if 'default_chat_state' not in settings_columns:
                 conn.execute(text("ALTER TABLE system_settings ADD COLUMN default_chat_state VARCHAR(20) DEFAULT 'default'"))
            
            if 'provider_keys_json' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN provider_keys_json TEXT"))
            if 'concurrent_limit_enabled' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN concurrent_limit_enabled BOOLEAN DEFAULT FALSE"))
            if 'concurrent_limit' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN concurrent_limit INTEGER DEFAULT 5"))
            if 'top_menu_color' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN top_menu_color VARCHAR(20) DEFAULT '#212529'"))
            
            if 'voice_silence_duration' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN voice_silence_duration INTEGER DEFAULT 2000"))
            
            if 'store_images_locally' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN store_images_locally BOOLEAN DEFAULT TRUE"))

            if 'image_search_limit' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_search_limit INTEGER DEFAULT 10"))
                
            if 'default_currency' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN default_currency VARCHAR(10) DEFAULT 'USD'"))

            # --- NEW SETTINGS MIGRATIONS ---
            if 'google_client_id' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN google_client_id VARCHAR(255)"))
            if 'google_client_secret' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN google_client_secret VARCHAR(255)"))
            if 'logo_url' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN logo_url VARCHAR(255)"))
            if 'logo_width' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN logo_width VARCHAR(20) DEFAULT '120px'"))
            if 'logo_height' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN logo_height VARCHAR(20) DEFAULT 'auto'"))
            if 'login_left_title' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN login_left_title VARCHAR(255)"))
            if 'login_left_content' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN login_left_content TEXT"))
            if 'terms_content' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN terms_content TEXT"))
            if 'privacy_content' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN privacy_content TEXT"))
            if 'gdpr_cookie_msg' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN gdpr_cookie_msg TEXT"))
            if 'show_powered_by' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN show_powered_by BOOLEAN DEFAULT TRUE"))
            
            if 'show_live_preview' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN show_live_preview BOOLEAN DEFAULT FALSE"))
                
            # --- NEW FEATURE: Option to hide Country/Language popup on first login ---
            if 'hide_onboarding_popup' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN hide_onboarding_popup BOOLEAN DEFAULT FALSE"))

            # --- APPLY MIGRATIONS FOR GOAL 3: Dynamic Pages ---
            if 'default_pages_json' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN default_pages_json TEXT"))

            # --- APPLY MIGRATIONS FOR NEW AI CMS FIELDS ---
            if 'model_gemini' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_gemini VARCHAR(100) DEFAULT 'gemini-2.0-flash'"))
            if 'inst_gemini' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_gemini TEXT"))
            if 'model_openai' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_openai VARCHAR(100) DEFAULT 'gpt-4o'"))
            if 'inst_openai' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_openai TEXT"))
            if 'model_grok' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_grok VARCHAR(100) DEFAULT 'grok-beta'"))
            if 'inst_grok' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_grok TEXT"))
            if 'model_meta' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_meta VARCHAR(100) DEFAULT 'llama3-70b-8192'"))
            if 'inst_meta' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_meta TEXT"))
            if 'model_perplexity' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_perplexity VARCHAR(100) DEFAULT 'llama-3.1-sonar-large-128k-online'"))
            if 'inst_perplexity' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_perplexity TEXT"))
                
            # --- NEW CLAUDE & DEEPSEEK SETTINGS MIGRATIONS ---
            if 'model_claude' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_claude VARCHAR(100) DEFAULT 'claude-3-5-sonnet-20241022'"))
            if 'inst_claude' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_claude TEXT"))
            if 'model_deepseek' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN model_deepseek VARCHAR(100) DEFAULT 'deepseek-chat'"))
            if 'inst_deepseek' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN inst_deepseek TEXT"))
                
            # --- IMAGE MODEL MIGRATIONS ---
            if 'image_model_gemini' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_gemini VARCHAR(100) DEFAULT 'imagen-3.0-generate-002'"))
            if 'image_model_openai' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_openai VARCHAR(100) DEFAULT 'dall-e-3'"))
            if 'image_model_grok' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_grok VARCHAR(100) DEFAULT ''"))
            if 'image_model_meta' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_meta VARCHAR(100) DEFAULT ''"))
            if 'image_model_perplexity' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_perplexity VARCHAR(100) DEFAULT ''"))
            if 'image_model_claude' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_claude VARCHAR(100) DEFAULT ''"))
            if 'image_model_deepseek' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN image_model_deepseek VARCHAR(100) DEFAULT ''"))
                
            # --- NEW: TOKEN LIMIT MIGRATIONS ---
            if 'token_limit_gemini' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_gemini VARCHAR(50) DEFAULT ''"))
            if 'token_limit_openai' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_openai VARCHAR(50) DEFAULT ''"))
            if 'token_limit_grok' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_grok VARCHAR(50) DEFAULT ''"))
            if 'token_limit_meta' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_meta VARCHAR(50) DEFAULT ''"))
            if 'token_limit_perplexity' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_perplexity VARCHAR(50) DEFAULT ''"))
            if 'token_limit_claude' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_claude VARCHAR(50) DEFAULT ''"))
            if 'token_limit_deepseek' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN token_limit_deepseek VARCHAR(50) DEFAULT ''"))
                
            if 'design_guidelines' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN design_guidelines TEXT"))
                
            if 'tax_enabled' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN tax_enabled BOOLEAN DEFAULT FALSE"))
            if 'tax_percentage' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN tax_percentage FLOAT DEFAULT 0.0"))

            # --- GOAL 1 FIX: Webhook Forwarding for API Keys ---
            if 'forward_api_keys_to_owner' not in settings_columns:
                conn.execute(text("ALTER TABLE system_settings ADD COLUMN forward_api_keys_to_owner BOOLEAN DEFAULT FALSE"))

            # --- NEW API KEY REQUEST MIGRATION ---
            if 'api_key_request' in tables:
                akr_columns = [c['name'] for c in inspector.get_columns('api_key_request')]
                if 'assigned_key' not in akr_columns:
                    try:
                        conn.execute(text("ALTER TABLE api_key_request ADD COLUMN assigned_key VARCHAR(255)"))
                    except:
                        pass

            conn.commit()

        # SECURITY FIX: Parameterize default admin password to prevent hardcoded credential usage
        if not User.query.filter_by(role='admin').first():
            print("Creating default Admin user...")
            admin_default_pass = os.getenv('ADMIN_DEFAULT_PASS', 'admin123')
            hashed_pw = generate_password_hash(admin_default_pass)
            admin = User(
                email="admin@example.com", 
                username="admin", 
                full_name="System Admin",
                phone_number="0000000000",
                country="", # Empty initially so they get prompted
                language="en", # Default Language
                password_hash=hashed_pw, 
                role="admin", 
                is_verified=True,
                allow_own_api_keys=True
            )
            db.session.add(admin)
            db.session.commit()

        # Initialize Default User Types if none exist
        if not UserType.query.first():
            print("Initializing Default User Types...")
            free_tier = UserType(name="Free", project_limit=3, is_default=True, image_limit=10, storage_limit_mb=100, allow_own_api_keys=False, allow_download=False)
            pro_tier = UserType(name="Pro", project_limit=50, is_default=False, image_limit=-1, storage_limit_mb=5000, allow_own_api_keys=True, allow_download=True)
            db.session.add(free_tier)
            db.session.add(pro_tier)
            db.session.commit()
            
        # --- GOAL 2 FIX: INITIALIZE CASHFREE AND CCAVENUE ---
        if not PaymentGateway.query.filter_by(code='cashfree').first():
            print("Initializing Cashfree Gateway...")
            cashfree_config = json.dumps({'app_id': '', 'secret_key': ''})
            cf = PaymentGateway(name="Cashfree", code="cashfree", config_json=cashfree_config, is_active=False)
            db.session.add(cf)
            db.session.commit()
            
        if not PaymentGateway.query.filter_by(code='ccavenue').first():
            print("Initializing CCAvenue Gateway...")
            cc_config = json.dumps({'merchant_id': '', 'access_code': '', 'working_key': ''})
            cc = PaymentGateway(name="CCAvenue", code="ccavenue", config_json=cc_config, is_active=False)
            db.session.add(cc)
            db.session.commit()

        if not PaymentGateway.query.filter_by(code='paypal').first():
            print("Initializing PayPal Gateway...")
            paypal_config = json.dumps({'client_id': '', 'client_secret': ''})
            ppl = PaymentGateway(name="PayPal", code="paypal", config_json=paypal_config, is_active=False)
            db.session.add(ppl)
            db.session.commit()

        # Clean up legacy gateways (Razorpay and 2Checkout) if they exist
        for old_code in ['razorpay', '2checkout']:
            old_gw = PaymentGateway.query.filter_by(code=old_code).first()
            if old_gw:
                db.session.delete(old_gw)
                db.session.commit()

        if not SystemSettings.query.first():
            print("Initializing System Settings from database...")
            default_imgs = json.dumps({
                "pexels": {"key": "", "enabled": True, "type": "free"},
                "unsplash": {"key": "", "enabled": False, "type": "paid"},
                "pixabay": {"key": "", "enabled": False, "type": "paid"},
                "freepik": {"key": "", "enabled": False, "type": "paid"},
                "shutterstock": {"key": "", "enabled": False, "type": "paid"},
                "mighty": {"key": "", "enabled": False, "type": "paid"}
            })
            
            # --- FIX: Initialize dynamic pages safely ---
            default_pages_val = json.dumps([
                {"name": "index.html", "description": "Homepage"},
                {"name": "about.html", "description": "About Us"},
                {"name": "contact.html", "description": "Contact Us"},
                {"name": "services.html", "description": "Products or Services"},
                {"name": "terms.html", "description": "Terms and Conditions"},
                {"name": "policy.html", "description": "Privacy Policy"},
                {"name": "faq.html", "description": "FAQ or Portfolio"}
            ])

            settings = SystemSettings(
                ai_provider="gemini", 
                gemini_api_key="", 
                pexels_api_key="", 
                image_providers_json=default_imgs,
                default_pages_json=default_pages_val, # INJECT HERE
                gemini_keys_json='[]',
                email_method='smtp',
                default_chat_state='default', # Changed to default as requested
                provider_keys_json='{}',
                voice_silence_duration=2000,
                store_images_locally=True,
                image_search_limit=10, 
                default_currency='USD',
                login_left_title="Welcome to Ewebgen",
                login_left_content="""<div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #ffffff;"><p style="color: #ffffff; font-size: 1.2em; font-weight: 300; line-height: 1.6; margin-bottom: 30px;">Ewebgen is your premier AI-powered website builder. Create, edit, and deploy stunning websites in minutes without writing a single line of code.</p><div style="background: rgba(255, 255, 255, 0.1); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 12px; padding: 20px; margin-bottom: 30px; backdrop-filter: blur(10px); box-shadow: 0 4px 15px rgba(0,0,0,0.1);"><h3 style="color: #ffffff; margin-top: 0; font-size: 1.05em; letter-spacing: 1px; text-transform: uppercase; border-bottom: 1px solid rgba(255,255,255,0.2); padding-bottom: 10px; margin-bottom: 15px;">Our Ecosystem</h3><ul style="color: #ffffff; list-style: none; padding: 0; margin: 0; font-size: 1.1em; line-height: 1.8;"><li style="color: #ffffff; margin-bottom: 8px;"><span style="color: #ffffff; opacity: 0.9;">Main Website :</span> <a href="https://ewebgen.com" target="_blank" style="color: #82caff; text-decoration: none; font-weight: bold; margin-left: 5px;">ewebgen.com →</a></li></ul></div><div style="margin-bottom: 35px;"><h3 style="color: #ffffff; font-size: 1.25em; margin-bottom: 15px; font-weight: 600;">Upcoming Features</h3><ul style="color: #ffffff; list-style: none; padding: 0; margin: 0; font-size: 1.1em; line-height: 1.6;"><li style="color: #ffffff; display: flex; align-items: center; margin-bottom: 12px;"><span style="background: #28a745; color: white; border-radius: 50%; width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; margin-right: 12px; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.2);">✓</span> AI Blogging Website Builder</li><li style="color: #ffffff; display: flex; align-items: center; margin-bottom: 12px;"><span style="background: #28a745; color: white; border-radius: 50%; width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; margin-right: 12px; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.2);">✓</span> E-commerce &amp; Shopping Integration</li><li style="color: #ffffff; display: flex; align-items: center; margin-bottom: 12px;"><span style="background: #28a745; color: white; border-radius: 50%; width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; margin-right: 12px; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.2);">✓</span>Many more Widgets</li></ul></div> </div>""",
                terms_content="""<h2>Terms and Conditions</h2>\n<p><strong>Last Updated:</strong> February 2026</p>\n<p>Welcome,  By accessing or using our website building platform, you agree to be bound by these Terms and Conditions.</p>\n\n<h3>1. Use of the Service</h3>\n<p>Ewebgen provides AI-powered website generation, editing, and deployment tools. You agree to use the service only for lawful purposes. You are solely responsible for the content, text, and images you generate, upload, or deploy using our platform.</p>\n\n<h3>2. Account Registration</h3>\n<p>To use certain features, you must register for an account (via email or Google Sign-in). You are responsible for maintaining the confidentiality of your account credentials and for all activities under your account.</p>\n\n<h3>3. AI Generation Disclaimer</h3>\n<p>Our platform utilizes artificial intelligence to generate code, text, and images. While we strive for high quality, we do not guarantee the accuracy, completeness, or uniqueness of the AI-generated output. You are responsible for reviewing and verifying all generated content before deploying it to a live server.</p>\n\n<h3>4. Subscriptions and Payments</h3>\n<p>Certain features, including increased project limits and storage, require a paid subscription. Payments are processed securely via third-party gateways (e.g., PayPal, 2Checkout). Subscriptions auto-renew unless cancelled prior to the billing cycle.</p>\n\n<h3>5. Deployment &amp; File Overrides</h3>\n<p>By using our FTP deployment feature, you authorize Ewebgen to transfer files to your remote server. <strong>You acknowledge that this action will override existing files.</strong> Orgif Solutions LLP is not liable for any data loss, server downtime, or damages resulting from FTP deployments.</p>\n\n<h3>6. Intellectual Property</h3>\n<p>You retain ownership of the content you create. However, the Ewebgen platform, its source code, UI, and proprietary AI workflows remain the exclusive property of Orgif Solutions LLP.</p>\n\n<h3>7. Limitation of Liability</h3>\n<p>In no event shall Orgif Solutions LLP be liable for any indirect, incidental, or consequential damages arising from the use or inability to use the Ewebgen platform.</p>""",
                privacy_content="""<h2>Privacy Policy</h2>\n<p><strong>Last Updated:</strong> February 2026</p>\n<p>At <strong>Ewebgen</strong>, your privacy is our priority. This Privacy Policy explains how we collect, use, and protect your information.</p>\n\n<h3>1. Information We Collect</h3>\n<ul>\n    <li><strong>Account Information:</strong> Name, email address, phone number, and password when you register or use Google Sign-in.</li>\n    <li><strong>Project Data:</strong> HTML/CSS/JS files, prompts, and images you upload or generate within our platform.</li>\n    <li><strong>Payment Information:</strong> Processed directly by our secure payment partners (PayPal, 2Checkout). We do not store your full credit card details.</li>\n    <li><strong>Usage Data:</strong> System logs, API usage, and interaction data to improve our AI models and service reliability.</li>\n</ul>\n\n<h3>2. How We Use Your Information</h3>\n<p>We use your data to provide, maintain, and improve the Ewebgen platform, process transactions, send system notifications, and provide customer support. Your prompts may be processed by third-party AI providers (e.g., OpenAI, Google Gemini) to generate your websites.</p>\n\n<h3>3. Third-Party Services</h3>\n<p>We may share necessary data with trusted third parties, including AI API providers, image stock libraries (e.g., Pexels), and email delivery services, strictly for fulfilling your platform requests.</p>\n\n<h3>4. Data Security</h3>\n<p>We implement industry-standard security measures to protect your account and project data. However, no internet transmission is 100% secure, and we cannot guarantee absolute security.</p>\n\n<h3>5. Your Rights (GDPR)</h3>\n<p>You have the right to access, correct, or delete your personal data. You may delete your account and associated project files directly from your profile settings or by contacting our support team. We use cookies to store session data and user preferences; you can manage these via our Cookie Banner.</p>\n\n<h3>6. Contact Us</h3>\n<p>For any privacy-related concerns, please contact us via the support portal or visit our main websites at <a href="https://ewebgen.com" target="_blank">ewebgen.com</a>.</p>""",
                gdpr_cookie_msg="""We use cookies to securely authenticate your session, save your editor preferences, and improve your AI building experience. By clicking "Accept &amp; Continue", you agree to our use of cookies as outlined in our <a href="#" onclick="showPrivacy()" style="color: #4dabf5; text-decoration: underline;">Privacy Policy</a>.""",
                forward_api_keys_to_owner=False,
                show_live_preview=False,
                
                # --- INCORPORATED FIX: Ensure elite premium design guidelines default on setup ---
                design_guidelines="""[MASTER FRONTEND ARCHITECT MANDATE - NON-NEGOTIABLE DESIGN RULES]:
You are an elite, award-winning Lead Frontend Architect and UI/UX Designer. You are strictly FORBIDDEN from generating plain, boring, dated, or basic HTML layouts. Every website you build MUST look like a $10,000 bespoke production web application.

1. PURE VANILLA CSS EXCELLENCE: Write extensive, professional, verbose CSS directly inside styles.css. Use CSS Variables, CSS Grid, and Flexbox exclusively.
2. PREMIUM DIVERSE COLOR SCHEME & VIBRANT SECTION BACKGROUNDS: You MUST use a highly attractive, modern, and professional color scheme. STRICTLY AVOID dull brown, beige, or sticking to just basic white and light grey. CRITICAL: You MUST use diverse, premium background colors for different `<section>` elements to clearly separate them visually (e.g., Hero: Deep Navy, Section 2: Soft Light Blue, Section 3: Crisp White, Section 4: Elegant Charcoal). Do not just alternate between two colors. Give each section a distinctly beautiful and professional background color that fits the brand. NEVER use a white background with white text. Ensure high contrast.
3. MODERN VISUAL AESTHETICS: Apply glassmorphism (backdrop-filter: blur(12px)), rounded geometry (border-radius: 12px), dynamic gradients, and soft layered box-shadows.
4. MICRO-INTERACTIONS: Include CSS @keyframes animations and hover states for cards and buttons.
5. PERFECT IMAGE ALIGNMENT (CRITICAL): All images MUST be perfectly aligned. Wrap text next to images in a Flexbox container (`display: flex; align-items: center; justify-content: space-between; gap: 3rem; flex-wrap: wrap;`) so they never misalign. Use `max-width: 100%; height: auto; object-fit: cover; border-radius: 12px; display: block;`.
6. MULTIPLE SECTIONS REQUIRED (ANTI-LAZY): EVERY single page MUST contain a MINIMUM of 4 to 6 fully fleshed-out, distinct `<section>` blocks. You will fail if you return a page with only 1 or 2 sections. Do not use Lorem Ipsum.
7. BEAUTIFUL HERO IMAGE: Every page MUST start with a breathtaking, high-impact Hero Section. Depending on the layout provided, use either a striking side-by-side layout with an `<img>` tag (src='/images/placeholder.jpg'), OR a majestic full-width CSS `background-image: url('/images/placeholder.jpg')` with a dark overlay and white text. Make the hero visually striking."""
            )
            db.session.add(settings)
            db.session.commit()

        # --- SEED DEFAULT KNOWLEDGE BASE Q&As ---
        if not HelpArticle.query.first():
            print("Seeding default Knowledge Base articles...")
            default_qas = [
                HelpArticle(
                    question="How do I create a new website from scratch?",
                    answer="In the left chat panel, type a description of the website you want (e.g., 'Create a new modern dental bakery website with 5 pages') and click Generate. The AI will build the complete layout and structure for you.",
                    category="Getting Started"
                ),
                HelpArticle(
                    question="How do I edit text or replace images on my page?",
                    answer="Double-click any text directly inside the live preview window to edit it inline. To replace an image, hover over it and click the 'Edit Image' badge, or right-click any element to open context options.",
                    category="Editing & Customization"
                ),
                HelpArticle(
                    question="How do I save or export my completed project?",
                    answer="Click the 'Save' button in the top navigation bar to save your edits to history. To export files, click ' Download' to receive a self-contained ZIP archive of your HTML, CSS, and images.",
                    category="Export & Deployment"
                ),
                HelpArticle(
                    question="Can I use my own personal AI API keys?",
                    answer="Yes! Click on 'Settings' in the left sidebar or top profile menu. Under 'AI Provider Keys', select your preferred AI (OpenAI, Gemini, Claude, DeepSeek, etc.) and enter your API key.",
                    category="AI & Settings"
                )
            ]
            db.session.bulk_save_objects(default_qas)
            db.session.commit()

init_db()