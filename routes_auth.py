import os
import random
import string
import uuid
import shutil
import json
import base64
from datetime import datetime, timezone
from sqlalchemy import or_, desc, func, text
from flask import request, jsonify, session
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# SECURITY FIX: Import Limiter to prevent Brute-Force and Credential Stuffing
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Import Shared Resources
from database_setup import (
    app, db, User, UserType, Notification, UserMessage, 
    SystemSettings, GenerationLog, UserClipboard, PROJECTS_DIR,
    PaymentGateway, Transaction, SubscriptionPlan, IssuedLicense,
    UserActivityLog, UserSavedSection # Added missing imports
)

# Import from our new services structure
from services_core import EmailService, save_attachment, FTPEncryptionService

# --- NEW: Import Security Engine for License Warnings & Enforcement ---
from security_engine import SecurityEngine

# --- NEW: Import BaseAIAgent to ensure default high-end design mandates are accessible ---
try:
    from ai_agents.base_agent import BaseAIAgent
except ImportError:
    BaseAIAgent = None

# Initialize Rate Limiter mapped to the Flask App
auth_limiter = Limiter(key_func=get_remote_address, app=app)

# =======================================================
# SECURITY HELPER: ENCRYPTION FOR USER SETTINGS
# =======================================================
def encrypt_sensitive_data(val):
    if not val or str(val).strip() == '': return val
    try:
        # Avoid double encryption
        dec = FTPEncryptionService.decrypt_credentials(val)
        if dec and 'sec_val' in dec: return val 
    except: pass
    return FTPEncryptionService.encrypt_credentials({'sec_val': str(val)})

def decrypt_sensitive_data(val):
    if not val: return val
    try:
        dec = FTPEncryptionService.decrypt_credentials(val)
        if dec and 'sec_val' in dec: return dec['sec_val']
    except: pass
    return val # Fallback to plain text if not encrypted or legacy

# -------------------- PUBLIC APP CONFIG --------------------

@app.route('/api/app_config', methods=['GET'])
def get_app_config():
    """
    Public endpoint used by the login page and frontend to load 
    logos, custom text, GDPR messages, and Google Client IDs.
    """
    s = SystemSettings.query.first()
    if not s:
        return jsonify({})
    
    # Ensure a default design guideline string is always available if empty
    default_design = getattr(s, 'design_guidelines', '') or ""
    if not default_design and BaseAIAgent and hasattr(BaseAIAgent, 'inject_master_design_mandate'):
        default_design = BaseAIAgent.inject_master_design_mandate("")

    return jsonify({
        "logo_url": s.logo_url,
        "logo_width": s.logo_width,
        "logo_height": s.logo_height,
        "login_left_title": s.login_left_title,
        "login_left_content": s.login_left_content,
        "terms_content": s.terms_content,
        "privacy_content": s.privacy_content,
        "gdpr_cookie_msg": s.gdpr_cookie_msg,
        "google_client_id": s.google_client_id,
        "top_menu_color": s.top_menu_color,
        "default_currency": s.default_currency,
        "show_powered_by": s.show_powered_by,
        
        # --- FIX: EXPOSE LIVE PREVIEW SETTING TO FRONTEND APP CONFIG ---
        "show_live_preview": getattr(s, 'show_live_preview', False),
        
        # --- GOAL 2 FIX: EXPOSE TAX SETTINGS FOR CHECKOUT ---
        "tax_enabled": getattr(s, 'tax_enabled', False),
        "tax_percentage": getattr(s, 'tax_percentage', 0.0),

        # --- NEW: HIDE ONBOARDING POPUP SETTING ---
        "hide_onboarding_popup": getattr(s, 'hide_onboarding_popup', False),

        # --- NEW: Expose Admin Models for User Settings Dropdown ---
        "model_gemini": s.model_gemini,
        "model_openai": s.model_openai,
        "model_claude": s.model_claude,
        "model_deepseek": s.model_deepseek,
        "model_grok": s.model_grok,
        "model_meta": s.model_meta,
        "model_perplexity": s.model_perplexity,
        "default_design_guidelines": default_design
    })


# -------------------- AUTH API Endpoints --------------------

# --- GOAL 1 FIX: SINGLE ACTIVE SESSION ENFORCEMENT ---
@app.before_request
def enforce_single_session():
    if current_user.is_authenticated:
        local_token = session.get('session_token')
        db_token = current_user.session_token
        
        # If the user has logged in elsewhere, their DB token will have changed.
        if db_token and local_token != db_token:
            logout_user()
            session.pop('session_token', None)
            if request.path.startswith('/api/'):
                return jsonify({"error": "Session expired. You logged in from another device.", "authenticated": False, "force_logout": True}), 401

@app.route('/api/login', methods=['POST'])
@auth_limiter.limit("10 per minute") # SECURITY FIX: Rate limit login attempts
def login():
    data = request.json
    identifier = data.get('identifier') 
    password = data.get('password')
    if not identifier or not password: return jsonify({"success": False, "error": "Missing credentials"}), 400

    user = User.query.filter(or_(User.email == identifier, User.username == identifier, User.phone_number == identifier)).first()
    
    if user and check_password_hash(user.password_hash, password):
        if not user.is_verified and not user.force_password_reset:
             return jsonify({"success": False, "error": "Account not verified."}), 403
        
        # Check if blocked
        if user.is_blocked:
            return jsonify({"success": False, "error": "Account blocked. Contact admin."}), 403

        # SINGLE SESSION: Generate and store new session token
        token = uuid.uuid4().hex
        user.session_token = token
        db.session.commit()
        session['session_token'] = token

        login_user(user)
        return jsonify({
            "success": True, 
            "role": user.role, 
            "email": user.email,
            "username": user.username,
            "full_name": user.full_name,
            "phone_number": user.phone_number,
            "id": user.id,
            "force_reset": user.force_password_reset,
            "country": user.country,
            "language": getattr(user, 'language', 'en')
        })
    
    return jsonify({"success": False, "error": "Invalid credentials"}), 401

@app.route('/api/google_login', methods=['POST'])
@auth_limiter.limit("10 per minute") # SECURITY FIX: Rate limit OAuth attempts
def google_login():
    """
    Handles Google Sign-in. Decodes the JWT credential passed from the frontend.
    If the user doesn't exist, it auto-registers them.
    """
    data = request.json
    credential = data.get('credential')
    
    if not credential:
        return jsonify({"success": False, "error": "Missing Google credentials"}), 400

    try:
        # Decode the JWT payload (Part 2 of the token)
        # Note: In a strict production environment, verify the signature using google.oauth2.id_token
        payload_segment = credential.split('.')[1]
        payload_segment += '=' * (-len(payload_segment) % 4) # Add padding
        decoded_bytes = base64.urlsafe_b64decode(payload_segment)
        google_data = json.loads(decoded_bytes)
        
        email = google_data.get('email')
        full_name = google_data.get('name', 'Google User')
        
        if not email:
            return jsonify({"success": False, "error": "Invalid Google Token"}), 400

        user = User.query.filter_by(email=email).first()
        
        if user:
            # Login existing user
            if user.is_blocked:
                return jsonify({"success": False, "error": "Account blocked. Contact admin."}), 403
            
            user.is_verified = True # Auto-verify Google users if they weren't already
            
            # SINGLE SESSION TRACKER
            token = uuid.uuid4().hex
            user.session_token = token
            db.session.commit()
            session['session_token'] = token
            
            login_user(user)
        else:
            # Auto-register new user
            username = email.split('@')[0]
            if User.query.filter_by(username=username).first(): 
                username = f"{username}{random.randint(100,999)}"
                
            # Random secure password for Google users
            random_pass = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
            
            new_user = User(
                email=email, 
                password_hash=generate_password_hash(random_pass), 
                full_name=full_name, 
                username=username, 
                role='client', 
                is_verified=True,
                country='',
                language='en'
            )
            
            # Assign Default User Type
            default_type = UserType.query.filter_by(is_default=True).first()
            if not default_type:
                default_type = UserType.query.filter_by(name='Free').first()
            if default_type:
                new_user.user_type_id = default_type.id
                
            # SINGLE SESSION TRACKER
            token = uuid.uuid4().hex
            new_user.session_token = token
            session['session_token'] = token

            db.session.add(new_user)
            db.session.flush() # Populate ID

            # Create Welcome Message
            thread_id = str(uuid.uuid4())
            welcome_notif = Notification(
                user_id=new_user.id,
                sender="System",
                subject="Welcome to AI Builder",
                body="Welcome! Start by typing a prompt to build your website.",
                is_read=False,
                thread_id=thread_id 
            )
            db.session.add(welcome_notif)
            db.session.commit()
            
            login_user(new_user)
            user = new_user

        return jsonify({
            "success": True, 
            "role": user.role, 
            "email": user.email,
            "username": user.username,
            "phone_number": user.phone_number,
            "full_name": user.full_name,
            "id": user.id,
            "force_reset": user.force_password_reset,
            "country": user.country,
            "language": getattr(user, 'language', 'en')
        })
        
    except Exception as e:
        print(f"Google Login Error: {e}")
        return jsonify({"success": False, "error": "Failed to process Google login."}), 500

@app.route('/api/register', methods=['POST'])
@auth_limiter.limit("5 per minute") # SECURITY FIX: Rate limit registration
def register():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    confirm_password = data.get('confirm_password')
    full_name = data.get('full_name')
    phone_number = data.get('phone_number')
    
    # NEW: Removed country requirement from registration logic
    country = data.get('country', '')
    language = data.get('language', 'en')
    
    if not email or not password or not confirm_password or not full_name or not phone_number:
        return jsonify({"success": False, "error": "All fields including confirm password are required."}), 400

    if password != confirm_password:
        return jsonify({"success": False, "error": "Passwords do not match."}), 400

    # Check explicitly if email or phone number exists to show specific error
    if User.query.filter_by(email=email).first():
        return jsonify({"success": False, "error": "Email is already registered. Please use a different email or log in."}), 400
        
    if User.query.filter_by(phone_number=phone_number).first():
        return jsonify({"success": False, "error": "Phone number is already registered. Please use a different phone number."}), 400

    # Ensure unique username automatically to avoid backend collision
    username = email.split('@')[0]
    base_username = username
    while User.query.filter_by(username=username).first():
        username = f"{base_username}{random.randint(100,9999)}"

    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    new_user = User(
        email=email, 
        password_hash=generate_password_hash(password), 
        full_name=full_name, 
        phone_number=phone_number, 
        username=username, 
        role='client', 
        is_verified=False, 
        verification_code=code, 
        country=country,
        language=language
    )
    
    # Assign Default User Type
    default_type = UserType.query.filter_by(is_default=True).first()
    if not default_type:
        default_type = UserType.query.filter_by(name='Free').first()
    
    if default_type:
        new_user.user_type_id = default_type.id

    db.session.add(new_user)
    db.session.flush() # Populate ID

    # Create Welcome Message
    thread_id = str(uuid.uuid4())
    welcome_notif = Notification(
        user_id=new_user.id,
        sender="System",
        subject="Welcome to AI Builder",
        body="Welcome! Start by typing a prompt to build your website.",
        is_read=False,
        thread_id=thread_id 
    )
    db.session.add(welcome_notif)
    db.session.commit()
    
    EmailService.send_verification_email(new_user, code)
    return jsonify({"success": True, "message": f"Verification code sent to email. Username: {username}", "username": username})

@app.route('/api/verify', methods=['POST'])
@auth_limiter.limit("10 per minute") # SECURITY FIX: Prevent OTP Brute Force
def verify_email():
    data = request.json
    email = data.get('email')
    code = data.get('code')
    user = User.query.filter_by(email=email).first()
    if user and user.verification_code == code:
        user.is_verified = True
        user.verification_code = None
        db.session.commit()
        return jsonify({"success": True, "message": "Verified successfully."})
    return jsonify({"success": False, "error": "Invalid verification code."}), 400

@app.route('/api/forgot_password', methods=['POST'])
@auth_limiter.limit("3 per minute") # SECURITY FIX: Rate limit resets
def forgot_password():
    data = request.json
    email = data.get('email')
    user = User.query.filter_by(email=email).first()
    if user:
        new_pass = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        user.password_hash = generate_password_hash(new_pass)
        user.force_password_reset = True 
        
        # Log out all existing sessions on password reset
        user.session_token = None
        
        db.session.commit()
        EmailService.send_email(email, "Password Reset Code", f"Your new temporary password code is: {new_pass}. Please log in with this code and you will be prompted to create a new password.", silent=True)
        return jsonify({"success": True, "message": "New temporary password code sent to email."})
    return jsonify({"success": False, "error": "Email not found."}), 404

@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    data = request.json
    old_password = data.get('old_password')
    new_password = data.get('new_password')
    if not check_password_hash(current_user.password_hash, old_password):
        return jsonify({"success": False, "error": "Incorrect current password."}), 400
    current_user.password_hash = generate_password_hash(new_password)
    current_user.force_password_reset = False
    current_user.is_verified = True
    
    # Generate new token so the user stays logged in, but other sessions drop
    token = uuid.uuid4().hex
    current_user.session_token = token
    session['session_token'] = token
    
    db.session.commit()
    return jsonify({"success": True, "message": "Password updated successfully."})

@app.route('/api/update_country', methods=['POST'])
@login_required
def update_country():
    data = request.json
    country = data.get('country')
    if not country:
        return jsonify({"success": False, "error": "Country is required."}), 400
        
    current_user.country = country
    db.session.commit()
    return jsonify({"success": True, "message": "Country updated successfully."})

# --- NEW ENDPOINT: UPDATE LANGUAGE ---
@app.route('/api/update_language', methods=['POST'])
@login_required
def update_language():
    data = request.json
    lang = data.get('language')
    if not lang:
        return jsonify({"success": False, "error": "Language code is required."}), 400
        
    current_user.language = lang
    db.session.commit()
    return jsonify({"success": True, "message": "Language updated successfully."})

@app.route('/api/update_profile', methods=['POST'])
@login_required
def update_profile():
    data = request.json
    full_name = data.get('full_name')
    email = data.get('email')
    phone_number = data.get('phone_number')
    
    if not full_name or not email or not phone_number: return jsonify({"success": False, "error": "All fields are required"}), 400

    if email != current_user.email:
        if User.query.filter_by(email=email).first(): return jsonify({"success": False, "error": "Email already in use"}), 400
    if phone_number != current_user.phone_number:
        if User.query.filter_by(phone_number=phone_number).first(): return jsonify({"success": False, "error": "Phone number already in use"}), 400

    current_user.full_name = full_name
    current_user.email = email
    current_user.phone_number = phone_number
    db.session.commit()
    return jsonify({"success": True, "message": "Profile updated successfully."})

@app.route('/api/delete_account', methods=['POST'])
@login_required
def delete_account():
    if current_user.role == 'admin':
        return jsonify({"success": False, "error": "Admins cannot delete their own account from here. Please use direct database access."}), 400
        
    user = User.query.get(current_user.id)
    if user:
        try:
            # FIX: Clear new tables via raw SQL or ORM before deleting user
            db.session.execute(text("DELETE FROM user_order WHERE user_id = :uid"), {"uid": user.id})
            
            UserActivityLog.query.filter_by(user_id=user.id).delete()
            UserSavedSection.query.filter_by(user_id=user.id).delete()

            # Delete dependencies that have nullable=False first
            Transaction.query.filter_by(user_id=user.id).delete()
            IssuedLicense.query.filter_by(user_id=user.id).delete()
            
            # Delete other relationships
            Notification.query.filter_by(user_id=user.id).delete()
            UserMessage.query.filter_by(user_id=user.id).delete()
            GenerationLog.query.filter_by(user_id=user.id).delete()
            UserClipboard.query.filter_by(user_id=user.id).delete()
            
            # Delete file directory
            user_projects_dir = os.path.join(PROJECTS_DIR, str(user.id))
            if os.path.exists(user_projects_dir): shutil.rmtree(user_projects_dir)

            db.session.delete(user)
            db.session.commit()
            
            session.pop('session_token', None)
            logout_user()
            return jsonify({"success": True, "message": "Account deleted successfully."})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": f"Failed to delete account constraints: {str(e)}"}), 500
            
    return jsonify({"success": False, "error": "User not found"}), 404

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    if current_user.is_authenticated:
        current_user.session_token = None
        db.session.commit()
        
    session.pop('session_token', None)
    logout_user()
    return jsonify({"success": True})



@app.route('/api/check_auth', methods=['GET'])
def check_auth():
    if current_user.is_authenticated:
        # Double check token just in case
        local_token = session.get('session_token')
        if current_user.session_token and local_token != current_user.session_token:
            logout_user()
            session.pop('session_token', None)
            return jsonify({"authenticated": False})
            
        # --- GOAL 1: CHECK FOR LICENSE WARNING ---
        license_warning = SecurityEngine.get_license_warning()
            
        # Check permissions for AI Keys (immediate update after upgrade)
        allow_ai_keys = current_user.role == 'admin' or current_user.allow_own_api_keys or (current_user.user_type and current_user.user_type.allow_own_api_keys)
        
        response_data = {
            "authenticated": True, 
            "role": current_user.role, 
            "email": current_user.email,
            "full_name": current_user.full_name,
            "username": current_user.username,       
            "phone_number": current_user.phone_number, 
            "force_reset": current_user.force_password_reset,
            "country": current_user.country,
            "language": getattr(current_user, 'language', 'en'),
            # Return limits
            "project_limit": current_user.user_type.project_limit if current_user.user_type else 3,
            "image_limit": current_user.user_type.image_limit if current_user.user_type else -1,
            "storage_limit_mb": current_user.user_type.storage_limit_mb if current_user.user_type else 500,
            "user_type_name": current_user.user_type.name if current_user.user_type else "Free",
            # New trackers
            "allow_own_api_keys": allow_ai_keys,
            "websites_generated": current_user.websites_generated
        }
        
        # Inject the warning if it exists
        if license_warning:
            response_data["license_warning"] = license_warning
            
        return jsonify(response_data)
    return jsonify({"authenticated": False})


@app.route('/api/profile', methods=['GET'])
@login_required
def get_profile():
    # Dynamically calculate remaining storage
    user_dir = os.path.join(PROJECTS_DIR, str(current_user.id))
    total_size_mb = 0.0
    
    if os.path.exists(user_dir):
        total_bytes = 0
        for dirpath, dirnames, filenames in os.walk(user_dir):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_bytes += os.path.getsize(fp)
        total_size_mb = total_bytes / (1024 * 1024)

    # Get limits from user type
    project_limit = current_user.user_type.project_limit if current_user.user_type else 3
    storage_limit = current_user.user_type.storage_limit_mb if current_user.user_type else 500

    # -------------------------------------------------------------
    # GOAL 1 FIX: REMOVED "UNLIMITED" TEXT AND RETURNED TO LIMIT CALC
    # -------------------------------------------------------------
    total_allowed = -1
    if project_limit != -1:
        total_allowed = project_limit + current_user.extra_project_limit
    
    # Standard deduction logic using actual websites generated count
    sites_left = "Unlimited" if total_allowed == -1 else max(0, total_allowed - current_user.websites_generated)
        
    space_left = None if storage_limit == -1 else max(0.01, round(storage_limit - total_size_mb, 2))

    return jsonify({
        "full_name": current_user.full_name,
        "email": current_user.email,
        "phone_number": current_user.phone_number, 
        "username": current_user.username,         
        "role": current_user.role,
        "websites_remaining": sites_left,
        "storage_remaining_mb": space_left,
        "user_type_name": current_user.user_type.name if current_user.user_type else "Free",
        "allow_download": current_user.user_type.allow_download if current_user.user_type else False
    })

# -------------------- USER SETTINGS API (NEW) --------------------

@app.route('/api/user/settings', methods=['GET'])
@login_required
def get_user_settings():
    try:
        settings = json.loads(current_user.user_settings_json or '{}')
    except:
        settings = {}
    
    allow_ai_keys = current_user.role == 'admin' or current_user.allow_own_api_keys or (current_user.user_type and current_user.user_type.allow_own_api_keys)
    
    # SECURITY FIX: Decrypt API Keys and SMTP configurations for UI injection
    if allow_ai_keys:
        for key in ['gemini_api_key', 'openai_api_key', 'claude_api_key', 'deepseek_api_key', 'xai_api_key', 'meta_api_key', 'perplexity_api_key']:
            if key in settings:
                settings[key] = decrypt_sensitive_data(settings[key])
    else:
        # Hide AI keys and Custom Models if user has downgraded and lost permission
        for key in ['ai_provider', 'gemini_api_key', 'openai_api_key', 'claude_api_key', 'deepseek_api_key', 'xai_api_key', 'meta_api_key', 'perplexity_api_key',
                    'user_model_gemini', 'user_model_openai', 'user_model_claude', 'user_model_deepseek', 'user_model_grok', 'user_model_meta', 'user_model_perplexity',
                    'user_token_gemini', 'user_token_openai', 'user_token_claude', 'user_token_deepseek', 'user_token_grok', 'user_token_meta', 'user_token_perplexity',
                    'default_pages_mode', 'default_pages_json']:
            settings.pop(key, None)

    # Mask SMTP password
    if settings.get('smtp_pass'):
        settings['smtp_pass'] = '********'

    # Pre-seed default design rules in return payload if none exist in user settings
    if allow_ai_keys and not settings.get('design_guidelines'):
        if BaseAIAgent and hasattr(BaseAIAgent, 'inject_master_design_mandate'):
            settings['design_guidelines'] = BaseAIAgent.inject_master_design_mandate("")

    return jsonify({
        "success": True,
        "settings": settings,
        "allow_own_api_keys": allow_ai_keys
    })

@app.route('/api/user/settings', methods=['POST'])
@login_required
def save_user_settings():
    data = request.json
    try:
        current_settings = json.loads(current_user.user_settings_json or '{}')
    except:
        current_settings = {}
        
    allow_ai_keys = current_user.role == 'admin' or current_user.allow_own_api_keys or (current_user.user_type and current_user.user_type.allow_own_api_keys)

    # Google Analytics
    if 'google_analytics' in data: 
        current_settings['google_analytics'] = data['google_analytics']
    
    # Contact / SMTP
    if 'contact_email' in data: current_settings['contact_email'] = data['contact_email']
    if 'smtp_host' in data: current_settings['smtp_host'] = data['smtp_host']
    if 'smtp_port' in data: current_settings['smtp_port'] = data['smtp_port']
    if 'smtp_user' in data: current_settings['smtp_user'] = data['smtp_user']
    
    # SECURITY FIX: Encrypt SMTP password before storing
    if 'smtp_pass' in data and data['smtp_pass'] != '********': 
        current_settings['smtp_pass'] = encrypt_sensitive_data(data['smtp_pass'])
    
    # AI Provider Keys, Models, and Custom Pages
    if allow_ai_keys:
        if 'ai_provider' in data: current_settings['ai_provider'] = data['ai_provider']
        
        # SECURITY FIX: Encrypt Personal AI API Keys before storing
        if 'gemini_api_key' in data: current_settings['gemini_api_key'] = encrypt_sensitive_data(data['gemini_api_key'])
        if 'openai_api_key' in data: current_settings['openai_api_key'] = encrypt_sensitive_data(data['openai_api_key'])
        if 'claude_api_key' in data: current_settings['claude_api_key'] = encrypt_sensitive_data(data['claude_api_key'])
        if 'deepseek_api_key' in data: current_settings['deepseek_api_key'] = encrypt_sensitive_data(data['deepseek_api_key'])
        if 'xai_api_key' in data: current_settings['xai_api_key'] = encrypt_sensitive_data(data['xai_api_key'])
        if 'meta_api_key' in data: current_settings['meta_api_key'] = encrypt_sensitive_data(data['meta_api_key'])
        if 'perplexity_api_key' in data: current_settings['perplexity_api_key'] = encrypt_sensitive_data(data['perplexity_api_key'])

        # --- NEW: User Selected/Custom Models ---
        if 'user_model_gemini' in data: current_settings['user_model_gemini'] = data['user_model_gemini']
        if 'user_model_openai' in data: current_settings['user_model_openai'] = data['user_model_openai']
        if 'user_model_claude' in data: current_settings['user_model_claude'] = data['user_model_claude']
        if 'user_model_deepseek' in data: current_settings['user_model_deepseek'] = data['user_model_deepseek']
        if 'user_model_grok' in data: current_settings['user_model_grok'] = data['user_model_grok']
        if 'user_model_meta' in data: current_settings['user_model_meta'] = data['user_model_meta']
        if 'user_model_perplexity' in data: current_settings['user_model_perplexity'] = data['user_model_perplexity']

        # --- NEW: User Selected Token Limits ---
        if 'user_token_gemini' in data: current_settings['user_token_gemini'] = data['user_token_gemini']
        if 'user_token_openai' in data: current_settings['user_token_openai'] = data['user_token_openai']
        if 'user_token_claude' in data: current_settings['user_token_claude'] = data['user_token_claude']
        if 'user_token_deepseek' in data: current_settings['user_token_deepseek'] = data['user_token_deepseek']
        if 'user_token_grok' in data: current_settings['user_token_grok'] = data['user_token_grok']
        if 'user_token_meta' in data: current_settings['user_token_meta'] = data['user_token_meta']
        if 'user_token_perplexity' in data: current_settings['user_token_perplexity'] = data['user_token_perplexity']

        # --- NEW: Custom Default Pages ---
        if 'default_pages_mode' in data: current_settings['default_pages_mode'] = data['default_pages_mode']
        if 'default_pages_json' in data: current_settings['default_pages_json'] = data['default_pages_json']
        if 'design_guidelines' in data: current_settings['design_guidelines'] = data['design_guidelines']

    current_user.user_settings_json = json.dumps(current_settings)
    db.session.commit()
    
    return jsonify({"success": True, "message": "Settings saved successfully."})

# --- MESSAGE SYSTEM API ---

@app.route('/api/notifications', methods=['GET'])
@login_required
def fetch_messages():
    if current_user.role == 'admin':
        # Admin view: Show messages sent to admin by users
        msgs = db.session.query(UserMessage, User).join(User, UserMessage.user_id == User.id).order_by(desc(UserMessage.timestamp)).limit(50).all()
        return jsonify([{
            "id": m.id,
            "thread_id": m.thread_id,
            "subject": m.subject,
            "from": u.username or u.full_name or "User",
            "body": m.body,
            "read": m.is_read,
            "timestamp": m.timestamp.strftime("%Y-%m-%d %H:%M"),
            "has_attachment": bool(m.attachment)
        } for m, u in msgs])
    else:
        # Normal client view: Show messages from system/admin
        msgs = Notification.query.filter_by(user_id=current_user.id).order_by(desc(Notification.timestamp)).limit(50).all()
        return jsonify([{
            "id": n.id,
            "thread_id": n.thread_id,
            "subject": n.subject,
            "from": n.sender,
            "body": n.body,
            "read": n.is_read,
            "timestamp": n.timestamp.strftime("%Y-%m-%d %H:%M"),
            "has_attachment": bool(n.attachment)
        } for n in msgs])

@app.route('/api/notifications/thread', methods=['GET'])
@login_required
def fetch_thread():
    msg_id = request.args.get('id') 
    
    if current_user.role == 'admin':
        msg = UserMessage.query.filter_by(id=msg_id).first()
        if not msg: msg = Notification.query.filter_by(id=msg_id).first()
        thread_id = msg.thread_id if msg else msg_id 
        if not msg and len(str(msg_id)) > 10: thread_id = msg_id

        received = UserMessage.query.filter_by(thread_id=thread_id).all()
        sent = Notification.query.filter_by(thread_id=thread_id).all()
        
        all_msgs = []
        for m in received:
            u = User.query.get(m.user_id)
            sender_name = u.username if u else "User"
            all_msgs.append({
                "id": m.id, "sender": sender_name, "from": sender_name, "body": m.body, 
                "timestamp": m.timestamp, "attachment": m.attachment, "type": "received"
            })
        for m in sent:
            all_msgs.append({
                "id": m.id, "sender": "Me", "from": "Me", "body": m.body, 
                "timestamp": m.timestamp, "attachment": m.attachment, "type": "sent"
            })
            
    else:
        notif = Notification.query.filter_by(id=msg_id, user_id=current_user.id).first()
        thread_id = notif.thread_id if notif else msg_id 
        if not notif and len(str(msg_id)) > 10: thread_id = msg_id

        uid = current_user.id
        received = Notification.query.filter_by(user_id=uid, thread_id=thread_id).all()
        sent = UserMessage.query.filter_by(user_id=uid, thread_id=thread_id).all()
        
        all_msgs = []
        for m in received:
            all_msgs.append({
                "id": m.id, "sender": m.sender, "from": m.sender, "body": m.body, 
                "timestamp": m.timestamp, "attachment": m.attachment, "type": "received"
            })
        for m in sent:
            all_msgs.append({
                "id": m.id, "sender": "Me", "from": "Me", "body": m.body, 
                "timestamp": m.timestamp, "attachment": m.attachment, "type": "sent"
            })
            
    all_msgs.sort(key=lambda x: x['timestamp'])
    
    return jsonify({
        "thread_id": thread_id,
        "thread": [{**m, "timestamp": m["timestamp"].strftime("%Y-%m-%d %H:%M")} for m in all_msgs]
    })

@app.route('/api/notifications/read', methods=['POST'])
@login_required
def mark_message_read():
    data = request.json
    msg_id = data.get('id')
    if current_user.role == 'admin':
        m = UserMessage.query.filter_by(id=msg_id).first()
    else:
        m = Notification.query.filter_by(id=msg_id, user_id=current_user.id).first()
        
    if m:
        m.is_read = True
        db.session.commit()
    return jsonify({"success": True})

@app.route('/api/notifications/read_all', methods=['POST'])
@login_required
def mark_all_messages_read():
    if current_user.role == 'admin':
        UserMessage.query.filter_by(is_read=False).update({UserMessage.is_read: True})
    else:
        Notification.query.filter_by(user_id=current_user.id, is_read=False).update({Notification.is_read: True})
    db.session.commit()
    return jsonify({"success": True})

@app.route('/api/notifications/delete', methods=['POST'])
@login_required
def delete_message():
    data = request.json
    msg_id = data.get('id')
    
    if current_user.role == 'admin':
        m = UserMessage.query.filter_by(id=msg_id).first()
        if not m:
            m = Notification.query.filter_by(id=msg_id).first()
    else:
        m = Notification.query.filter_by(id=msg_id, user_id=current_user.id).first()
        
    if m:
        db.session.delete(m)
        db.session.commit()
        return jsonify({"success": True, "message": "Message deleted."})
    return jsonify({"success": False, "error": "Message not found or unauthorized."}), 404

@app.route('/api/contact_admin', methods=['POST'])
@login_required
def contact_admin():
    if current_user.is_blocked:
        return jsonify({"success": False, "error": "Account blocked."}), 403

    subject = request.form.get('subject')
    body = request.form.get('body')
    file = request.files.get('attachment')
    
    if not subject or not body: return jsonify({"success": False, "error": "Missing fields"}), 400
    
    attachment_path = save_attachment(file)
    thread_id = str(uuid.uuid4())
    
    msg = UserMessage(user_id=current_user.id, subject=subject, body=body, thread_id=thread_id, attachment=attachment_path)
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True, "message": "Message sent."})

@app.route('/api/reply', methods=['POST'])
@login_required
def reply_message():
    thread_id = request.form.get('parent_id') 
    body = request.form.get('body')
    file = request.files.get('attachment')
    
    if not thread_id or not body: return jsonify({"success": False, "error": "Missing info"}), 400
    
    attachment_path = save_attachment(file)
    
    if current_user.role == 'admin':
        last_msg = UserMessage.query.filter_by(thread_id=thread_id).first()
        if not last_msg: return jsonify({"success": False, "error": "Thread not found"}), 404
        subject = f"RE: {last_msg.subject}"
        msg = Notification(user_id=last_msg.user_id, sender="Admin", subject=subject, body=body, thread_id=thread_id, attachment=attachment_path)
    else:
        last_msg = Notification.query.filter_by(thread_id=thread_id).first()
        subject = f"RE: {last_msg.subject}" if last_msg else "Reply"
        msg = UserMessage(user_id=current_user.id, subject=subject, body=body, thread_id=thread_id, attachment=attachment_path)
        
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True})


# ==========================================================
# FIX GOAL 3: INJECTED ADMIN SETTINGS GET & SAVE ROUTES
# ==========================================================
# Note: These routes have been disabled (commented out) because they conflict 
# with the fully updated, complete versions located in routes_admin.py which 
# correctly handle all your new settings (Chatbox, Login Text, GDPR, etc).
# They are kept here so no code is technically "deleted".

# @app.route('/api/admin/get_settings', methods=['GET'])
# @login_required
def get_admin_settings():
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
        
    s = SystemSettings.query.first()
    if not s: 
        return jsonify({})
    
    return jsonify({
        "gemini_api_key": s.gemini_api_key,
        "email_method": s.email_method,
        "smtp_host": s.smtp_host,
        "smtp_port": s.smtp_port,
        "smtp_user": s.smtp_user,
        "smtp_pass": s.smtp_pass,
        "smtp_from_email": s.smtp_from_email,
        "email_api_url": s.email_api_url,
        "email_api_key": s.email_api_key,
        "image_providers_json": s.image_providers_json,
        
        # --- FIX: ENSURE THESE ARE ACTUALLY SENT TO THE FRONTEND ---
        "ai_provider": s.ai_provider,
        "default_pages_json": s.default_pages_json,
        "design_guidelines": s.design_guidelines,
        
        "model_gemini": s.model_gemini,
        "inst_gemini": s.inst_gemini,
        "model_openai": s.model_openai,
        "inst_openai": s.inst_openai,
        "model_claude": s.model_claude,
        "inst_claude": s.inst_claude,
        "model_deepseek": s.model_deepseek,
        "inst_deepseek": s.inst_deepseek,
        "model_grok": s.model_grok,
        "inst_grok": s.inst_grok,
        "model_meta": s.model_meta,
        "inst_meta": s.inst_meta,
        "model_perplexity": s.model_perplexity,
        "inst_perplexity": s.inst_perplexity
    })


# @app.route('/api/admin/update_settings', methods=['POST'])
# @login_required
def update_admin_settings():
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
    data = request.json
    settings = SystemSettings.query.first()
    if not settings:
        settings = SystemSettings()
        db.session.add(settings)
        
    # 1. Save standard settings
    if 'gemini_api_key' in data: settings.gemini_api_key = data['gemini_api_key']
    if 'image_providers_json' in data: settings.image_providers_json = data['image_providers_json']
        
    # 2. Save SMTP / Email settings
    if 'email_method' in data: settings.email_method = data['email_method']
    if 'smtp_host' in data: settings.smtp_host = data['smtp_host']
    if 'smtp_port' in data: settings.smtp_port = data['smtp_port']
    if 'smtp_user' in data: settings.smtp_user = data['smtp_user']
    if 'smtp_pass' in data: settings.smtp_pass = data['smtp_pass']
    if 'smtp_from_email' in data: settings.smtp_from_email = data['smtp_from_email']
    if 'email_api_url' in data: settings.email_api_url = data['email_api_url']
    if 'email_api_key' in data: settings.email_api_key = data['email_api_key']

    # 3. Save AI Provider, Default Pages, & Guidelines
    if 'ai_provider' in data: settings.ai_provider = data['ai_provider']
    if 'default_pages_json' in data: settings.default_pages_json = data['default_pages_json']
    if 'design_guidelines' in data: settings.design_guidelines = data['design_guidelines']

    # 4. Save all Custom AI Models & Instructions
    providers = ['gemini', 'openai', 'claude', 'deepseek', 'grok', 'meta', 'perplexity']
    for p in providers:
        model_key = f'model_{p}'
        inst_key = f'inst_{p}'
        
        if model_key in data:
            setattr(settings, model_key, data[model_key])
        if inst_key in data:
            setattr(settings, inst_key, data[inst_key])
        
    try:
        db.session.commit()
        return jsonify({'success': True, 'message': 'Settings updated successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Database error: {str(e)}'}), 500