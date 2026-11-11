import os
import random
import string
import uuid
import shutil
import json
import base64
from datetime import datetime, timezone
from sqlalchemy import or_, desc, func, text
from flask import request, jsonify
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# Import Shared Resources
from database_setup import (
    app, db, User, UserType, Notification, UserMessage, 
    SystemSettings, GenerationLog, UserClipboard, PROJECTS_DIR,
    PaymentGateway, Transaction, SubscriptionPlan, IssuedLicense,
    UserActivityLog, UserSavedSection, HelpArticle, HireRequest, UserFeedback, ApiKeyRequest
)

# Import from our new services structure
from services_core import save_attachment, EmailService, FTPEncryptionService

# =======================================================
# SECURITY HELPER: ENCRYPTION FOR SYSTEM SETTINGS
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

# -------------------- ADMIN API Endpoints --------------------

@app.route('/api/admin/messages', methods=['GET'])
@login_required
def admin_get_messages():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    msgs = db.session.query(UserMessage, User).join(User, UserMessage.user_id == User.id).order_by(desc(UserMessage.timestamp)).limit(50).all()
    
    result = []
    for m, u in msgs:
        result.append({
            "id": m.id,
            "thread_id": m.thread_id,
            "user_id": u.id,
            "from_user": u.username or "Unknown",
            "email": u.email,
            "subject": m.subject,
            "body": m.body,
            "read": m.is_read,
            "timestamp": m.timestamp.strftime("%Y-%m-%d %H:%M"),
            "attachment": m.attachment,
            "has_attachment": bool(m.attachment)
        })
    return jsonify({"messages": result})

@app.route('/api/admin/mark_message_read', methods=['POST'])
@login_required
def admin_mark_read():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    msg_id = data.get('message_id')
    msg = UserMessage.query.get(msg_id)
    if msg:
        msg.is_read = True
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Message not found"}), 404

@app.route('/api/admin/send_notification', methods=['POST'])
@login_required
def admin_send_message():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    target_id = request.form.get('target_user_id')
    subject = request.form.get('subject')
    body = request.form.get('body')
    existing_thread_id = request.form.get('thread_id')
    file = request.files.get('attachment')
    
    if not subject or not body: return jsonify({"success": False, "error": "Missing fields"}), 400
    
    attachment_path = save_attachment(file)
    
    if target_id == 'all':
        users = User.query.all()
        for u in users:
            if u.id == current_user.id: continue 
            t_id = str(uuid.uuid4())
            n = Notification(user_id=u.id, sender="Admin", subject=subject, body=body, thread_id=t_id, attachment=attachment_path)
            db.session.add(n)
    else:
        try:
            uid = int(target_id)
            t_id = existing_thread_id if existing_thread_id else str(uuid.uuid4())
            n = Notification(user_id=uid, sender="Admin", subject=subject, body=body, thread_id=t_id, attachment=attachment_path)
            db.session.add(n)
        except:
            return jsonify({"success": False, "error": "Invalid user ID"}), 400
            
    db.session.commit()
    return jsonify({"success": True, "message": "Message sent."})

@app.route('/api/admin/users', methods=['GET'])
@login_required
def admin_get_users():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    results = db.session.query(User, UserType).outerjoin(UserType, User.user_type_id == UserType.id).all()
    
    output = []
    for u, ut in results:
        # Calculate Remaining Limits
        rem_sites = "Unlimited"
        if ut and ut.project_limit != -1:
            rem_sites = max(0, ut.project_limit + (u.extra_project_limit or 0) - (u.websites_generated or 0))
            
        rem_images = "Unlimited"
        if ut and ut.image_limit != -1:
            rem_images = max(0, ut.image_limit + (u.extra_image_limit or 0) - (u.images_generated or 0))
            
        output.append({ 
            "id": u.id, 
            "email": u.email, 
            "username": u.username, 
            "full_name": u.full_name,
            "phone": u.phone_number,
            "country": u.country, 
            "role": u.role, 
            "verified": u.is_verified,
            "is_blocked": u.is_blocked,
            "is_muted": u.is_muted,
            "user_type_id": u.user_type_id,
            "user_type_name": ut.name if ut else None,
            "images_generated": u.images_generated,
            "websites_generated": u.websites_generated,
            "remaining_websites": rem_sites,
            "remaining_images": rem_images
        })
    return jsonify(output)

@app.route('/api/admin/update_user', methods=['POST'])
@login_required
def admin_update_user():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user_id = data.get('user_id')
    user = User.query.get(user_id)
    if not user: return jsonify({"success": False, "error": "User not found"}), 404

    new_username = data.get('username')
    new_email = data.get('email')
    new_phone = data.get('phone_number')
    new_country = data.get('country') 
    new_fullname = data.get('full_name')
    new_role = data.get('role')
    new_verified = data.get('verified')
    new_type_id = data.get('user_type_id')

    if new_username: user.username = new_username
    if new_email: user.email = new_email
    if new_phone is not None: user.phone_number = new_phone
    if new_country is not None: user.country = new_country 
    if new_fullname: user.full_name = new_fullname
    if new_role: user.role = new_role
    if new_verified is not None: user.is_verified = bool(new_verified)
    
    if new_type_id: user.user_type_id = int(new_type_id)
    else: user.user_type_id = None 

    # --- GOAL 2 FIX: Add to limits ---
    add_proj = data.get('add_project_limit')
    if add_proj and str(add_proj).strip().isdigit():
        user.extra_project_limit = (user.extra_project_limit or 0) + int(add_proj)
        
    add_img = data.get('add_image_limit')
    if add_img and str(add_img).strip().isdigit():
        user.extra_image_limit = (user.extra_image_limit or 0) + int(add_img)

    db.session.commit()
    return jsonify({"success": True, "message": "User updated successfully."})

@app.route('/api/admin/toggle_verify', methods=['POST'])
@login_required
def admin_toggle_verify():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user = User.query.get(data.get('user_id'))
    if user:
        user.is_verified = not user.is_verified
        db.session.commit()
        return jsonify({"success": True, "message": "Updated verify status."})
    return jsonify({"success": False, "error": "User not found"}), 404

@app.route('/api/admin/toggle_status', methods=['POST'])
@login_required
def admin_toggle_status():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user = User.query.get(data.get('user_id'))
    status_type = data.get('type')
    
    if not user: return jsonify({"success": False, "error": "User not found"}), 404
    
    if status_type == 'mute': user.is_muted = not user.is_muted
    elif status_type == 'block': user.is_blocked = not user.is_blocked
    
    db.session.commit()
    return jsonify({"success": True, "message": "Status updated."})

@app.route('/api/admin/delete_user', methods=['POST'])
@login_required
def admin_delete_user():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user_id = data.get('user_id')
    if str(user_id) == str(current_user.id): return jsonify({"success": False, "error": "Cannot delete yourself."}), 400
    
    user = User.query.get(user_id)
    if user:
        try:
            # FIX: 1. Delete Orders (Foreign Key Constraint Fix)
            db.session.execute(text("DELETE FROM user_order WHERE user_id = :uid"), {"uid": user.id})

            # FIX: 2. Delete Activity Logs & Saved Sections & Feedbacks & Hire Requests
            UserActivityLog.query.filter_by(user_id=user.id).delete()
            UserSavedSection.query.filter_by(user_id=user.id).delete()
            UserFeedback.query.filter_by(user_id=user.id).delete()
            HireRequest.query.filter_by(user_id=user.id).delete()
            ApiKeyRequest.query.filter_by(user_id=user.id).delete()

            # Delete dependencies that have nullable=False first
            Transaction.query.filter_by(user_id=user.id).delete()
            IssuedLicense.query.filter_by(user_id=user.id).delete()
            
            Notification.query.filter_by(user_id=user.id).delete()
            UserMessage.query.filter_by(user_id=user.id).delete()
            GenerationLog.query.filter_by(user_id=user.id).delete()
            UserClipboard.query.filter_by(user_id=user.id).delete()
            
            user_projects_dir = os.path.join(PROJECTS_DIR, str(user.id))
            if os.path.exists(user_projects_dir): shutil.rmtree(user_projects_dir)

            db.session.delete(user)
            db.session.commit()
            return jsonify({"success": True, "message": "User deleted."})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": f"Failed to delete constraints: {str(e)}"}), 500
            
    return jsonify({"success": False, "error": "User not found"}), 404

# --- USER TYPE MANAGEMENT ---

@app.route('/api/admin/user_types', methods=['GET'])
@login_required
def admin_get_user_types():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    results = db.session.query(UserType, func.count(User.id)).outerjoin(User, UserType.id == User.user_type_id).group_by(UserType.id).all()
    output = []
    for ut, count in results:
        output.append({
            "id": ut.id,
            "name": ut.name,
            "project_limit": ut.project_limit,
            "image_limit": ut.image_limit,
            "storage_limit_mb": ut.storage_limit_mb,
            "user_count": count,
            "is_default": ut.is_default,
            "allow_own_api_keys": ut.allow_own_api_keys, 
            "allow_download": ut.allow_download          
        })
    return jsonify(output)

@app.route('/api/admin/create_user_type', methods=['POST'])
@login_required
def admin_create_user_type():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    name = data.get('name')
    limit = data.get('project_limit')
    image_limit = data.get('image_limit', -1)
    storage_limit_mb = data.get('storage_limit_mb', 500)
    
    try:
        new_type = UserType(
            name=name, 
            project_limit=int(limit),
            image_limit=int(image_limit),
            storage_limit_mb=int(storage_limit_mb),
            allow_own_api_keys=bool(data.get('allow_own_api_keys', False)), 
            allow_download=bool(data.get('allow_download', False))          
        )
        db.session.add(new_type)
        db.session.commit()
        return jsonify({"success": True, "message": "User Type created."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/update_user_type', methods=['POST'])
@login_required
def admin_update_user_type():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    ut = UserType.query.get(data.get('id'))
    if not ut: return jsonify({"success": False, "error": "Type not found"}), 404
    
    if 'name' in data: ut.name = data['name']
    if 'project_limit' in data: ut.project_limit = int(data['project_limit'])
    if 'image_limit' in data: ut.image_limit = int(data['image_limit'])
    if 'storage_limit_mb' in data: ut.storage_limit_mb = int(data['storage_limit_mb'])
    if 'allow_own_api_keys' in data: ut.allow_own_api_keys = bool(data['allow_own_api_keys']) 
    if 'allow_download' in data: ut.allow_download = bool(data['allow_download'])             
    
    db.session.commit()
    return jsonify({"success": True, "message": "User Type updated."})

@app.route('/api/admin/delete_user_type', methods=['POST'])
@login_required
def admin_delete_user_type():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    ut = UserType.query.get(data.get('id'))
    if not ut: return jsonify({"success": False, "error": "Type not found"}), 404
    
    try:
        SubscriptionPlan.query.filter_by(user_type_id=ut.id).delete()
        User.query.filter_by(user_type_id=ut.id).update({User.user_type_id: None})
        
        db.session.delete(ut)
        db.session.commit()
        return jsonify({"success": True, "message": "User Type and its linked Subscription Plans deleted successfully."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/set_default_user_type', methods=['POST'])
@login_required
def admin_set_default_user_type():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    type_id = request.json.get('user_type_id')
    
    UserType.query.update({UserType.is_default: False})
    ut = UserType.query.get(type_id)
    if ut:
        ut.is_default = True
        db.session.commit()
        return jsonify({"success": True, "message": f"Default type set to {ut.name}"})
    return jsonify({"success": False, "error": "Type not found"}), 404

# --- SUBSCRIPTION PLANS MANAGEMENT ---

@app.route('/api/plans', methods=['GET'])
def get_public_plans():
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    results = []
    
    current_plan_id = None
    current_price = -1.0

    if current_user and current_user.is_authenticated:
        try:
            res = db.session.execute(
                text("SELECT product_name FROM user_order WHERE user_id = :uid AND status = 'active' ORDER BY created_at DESC LIMIT 1"),
                {"uid": current_user.id}
            ).fetchone()
            if res and res[0]:
                active_plan_name = str(res[0]).strip().lower()
                for p in plans:
                    if p.name and p.name.strip().lower() == active_plan_name:
                        current_plan_id = p.id
                        break
        except Exception as e:
            print(f"Order check error: {e}")

        if not current_plan_id and hasattr(current_user, 'user_type_id') and current_user.user_type_id:
            for p in plans:
                if p.user_type_id == current_user.user_type_id:
                    current_plan_id = p.id
                    break
        
        if not current_plan_id and hasattr(current_user, 'plan_id') and current_user.plan_id:
            current_plan_id = current_user.plan_id

        if not current_plan_id and hasattr(current_user, 'plan_name') and current_user.plan_name:
            for p in plans:
                if p.name and p.name.strip().lower() == current_user.plan_name.strip().lower():
                    current_plan_id = p.id
                    break

    if current_plan_id:
        for p in plans:
            if p.id == current_plan_id:
                current_price = float(p.price)
                break

    plans = sorted(plans, key=lambda x: float(x.price))

    for p in plans:
        is_current = (p.id == current_plan_id)
        if current_price >= 0 and float(p.price) < current_price and not is_current:
            continue

        results.append({
            "id": p.id,
            "name": p.name,
            "price": p.price,
            "currency": p.currency,
            "billing_cycle": p.billing_cycle, 
            "features": p.features.split('\n') if p.features else [],
            "user_type_name": p.user_type.name if p.user_type else "Unknown",
            "is_current": is_current
        })
        
    return jsonify(results)

@app.route('/api/admin/plans', methods=['GET'])
@login_required
def admin_get_plans():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    plans = SubscriptionPlan.query.all()
    results = []
    for p in plans:
        results.append({
            "id": p.id,
            "name": p.name,
            "price": p.price,
            "currency": p.currency,
            "billing_cycle": p.billing_cycle, 
            "features": p.features, 
            "user_type_id": p.user_type_id,
            "user_type_name": p.user_type.name if p.user_type else "Unknown",
            "is_active": p.is_active
        })
    return jsonify(results)

@app.route('/api/admin/create_plan', methods=['POST'])
@login_required
def admin_create_plan():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    
    name = data.get('name')
    price = data.get('price')
    currency = data.get('currency', 'USD')
    billing_cycle = data.get('billing_cycle', 'monthly') 
    user_type_id = data.get('user_type_id')
    features = data.get('features') 
    
    if not name or price is None or not user_type_id:
        return jsonify({"success": False, "error": "Missing required fields"}), 400
        
    try:
        new_plan = SubscriptionPlan(
            name=name,
            price=float(price),
            currency=currency,
            billing_cycle=billing_cycle, 
            user_type_id=int(user_type_id),
            features=features,
            is_active=True
        )
        db.session.add(new_plan)
        db.session.commit()
        return jsonify({"success": True, "message": "Plan created."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/update_plan', methods=['POST'])
@login_required
def admin_update_plan():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    
    plan = SubscriptionPlan.query.get(data.get('id'))
    if not plan: return jsonify({"success": False, "error": "Plan not found"}), 404
    
    if 'name' in data: plan.name = data['name']
    if 'price' in data: plan.price = float(data['price'])
    if 'currency' in data: plan.currency = data['currency']
    if 'billing_cycle' in data: plan.billing_cycle = data['billing_cycle'] 
    if 'user_type_id' in data: plan.user_type_id = int(data['user_type_id'])
    if 'features' in data: plan.features = data['features']
    if 'is_active' in data: plan.is_active = data['is_active']
    
    db.session.commit()
    return jsonify({"success": True, "message": "Plan updated."})

@app.route('/api/admin/delete_plan', methods=['POST'])
@login_required
def admin_delete_plan():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    plan = SubscriptionPlan.query.get(request.json.get('id'))
    if not plan: return jsonify({"success": False, "error": "Plan not found"}), 404
    
    db.session.delete(plan)
    db.session.commit()
    return jsonify({"success": True, "message": "Plan deleted."})

# --- SYSTEM SETTINGS ---

@app.route('/api/admin/logs', methods=['GET'])
@login_required
def get_logs():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    user_id = request.args.get('user_id')
    logs = GenerationLog.query.filter_by(user_id=user_id).order_by(desc(GenerationLog.timestamp)).limit(3).all()
    return jsonify({"logs": [{"timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S"), "content": l.content} for l in logs]})

@app.route('/api/admin/generation_logs', methods=['GET'])
@login_required
def admin_get_generation_logs():
    if current_user.role != 'admin': 
        return jsonify({"error": "Unauthorized"}), 403
        
    user_id = request.args.get('user_id')
    query = GenerationLog.query
    
    if user_id:
        query = query.filter_by(user_id=user_id)
        
    logs = query.order_by(GenerationLog.timestamp.desc()).limit(200).all()
    
    result = []
    for log in logs:
        result.append({
            "id": log.id,
            "user_email": log.user.email if log.user else "Unknown User",
            "timestamp": log.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "content": log.content
        })
        
    return jsonify({"success": True, "logs": result})

@app.route('/api/admin/get_settings', methods=['GET'])
@login_required
def admin_get_settings():
    s = SystemSettings.query.first()
    if not s: return jsonify({})
    
    data = {
        "default_chat_state": s.default_chat_state,
        "top_menu_color": s.top_menu_color,
        "concurrent_limit_enabled": s.concurrent_limit_enabled,
        "concurrent_limit": s.concurrent_limit,
        "voice_silence_duration": s.voice_silence_duration,
        "store_images_locally": s.store_images_locally,
        "image_search_limit": s.image_search_limit or 10,
        "default_currency": s.default_currency,
        "show_powered_by": s.show_powered_by,
        "show_live_preview": getattr(s, 'show_live_preview', False),
        "tax_enabled": getattr(s, 'tax_enabled', False),
        "tax_percentage": getattr(s, 'tax_percentage', 0.0),
        "hide_onboarding_popup": getattr(s, 'hide_onboarding_popup', False)
    }

    if current_user.role == 'admin':
        # SECURITY FIX: Decrypt API Keys and SMTP configurations for UI injection
        data.update({
            "ai_provider": s.ai_provider,
            "gemini_api_key": decrypt_sensitive_data(s.gemini_api_key),
            "openai_api_key": decrypt_sensitive_data(s.openai_api_key),
            "claude_api_key": decrypt_sensitive_data(s.claude_api_key),       
            "deepseek_api_key": decrypt_sensitive_data(s.deepseek_api_key),   
            "xai_api_key": decrypt_sensitive_data(s.xai_api_key),
            "meta_api_key": decrypt_sensitive_data(s.meta_api_key),
            "perplexity_api_key": decrypt_sensitive_data(s.perplexity_api_key),
            "provider_keys_json": decrypt_sensitive_data(s.provider_keys_json),
            
            "image_providers_json": s.image_providers_json,
            
            "email_method": s.email_method,
            "smtp_host": s.smtp_host,
            "smtp_port": s.smtp_port,
            "smtp_user": s.smtp_user,
            "smtp_from_email": s.smtp_from_email,
            "email_api_url": s.email_api_url,
            "email_api_key": decrypt_sensitive_data(s.email_api_key),
            
            "google_client_id": s.google_client_id,
            "google_client_secret": s.google_client_secret,
            "logo_url": s.logo_url,
            "logo_width": s.logo_width,
            "logo_height": s.logo_height,
            "login_left_title": s.login_left_title,
            "login_left_content": s.login_left_content,
            "terms_content": s.terms_content,
            "privacy_content": s.privacy_content,
            "gdpr_cookie_msg": s.gdpr_cookie_msg,
            
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
            "inst_perplexity": s.inst_perplexity,

            "image_model_gemini": getattr(s, 'image_model_gemini', ''),
            "image_model_openai": getattr(s, 'image_model_openai', ''),
            "image_model_claude": getattr(s, 'image_model_claude', ''),
            "image_model_deepseek": getattr(s, 'image_model_deepseek', ''),
            "image_model_grok": getattr(s, 'image_model_grok', ''),
            "image_model_meta": getattr(s, 'image_model_meta', ''),
            "image_model_perplexity": getattr(s, 'image_model_perplexity', ''),
            
            "token_limit_gemini": getattr(s, 'token_limit_gemini', ''),
            "token_limit_openai": getattr(s, 'token_limit_openai', ''),
            "token_limit_claude": getattr(s, 'token_limit_claude', ''),
            "token_limit_deepseek": getattr(s, 'token_limit_deepseek', ''),
            "token_limit_grok": getattr(s, 'token_limit_grok', ''),
            "token_limit_meta": getattr(s, 'token_limit_meta', ''),
            "token_limit_perplexity": getattr(s, 'token_limit_perplexity', ''),

            "design_guidelines": s.design_guidelines,
            "default_pages_json": s.default_pages_json
        })
        
        # Obfuscate the SMTP password for the UI
        if s.smtp_pass:
            data["smtp_pass"] = "********"
    
    return jsonify(data)

@app.route('/api/admin/update_settings', methods=['POST'])
@login_required
def admin_update_settings():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    settings = SystemSettings.query.first()
    if not settings: settings = SystemSettings(); db.session.add(settings)
    
    if 'ai_provider' in data: settings.ai_provider = data['ai_provider']
    
    # SECURITY FIX: Encrypt all Global AI API Keys
    if 'gemini_api_key' in data and data['gemini_api_key'] is not None: settings.gemini_api_key = encrypt_sensitive_data(str(data['gemini_api_key']).strip())
    if 'openai_api_key' in data and data['openai_api_key'] is not None: settings.openai_api_key = encrypt_sensitive_data(str(data['openai_api_key']).strip())
    if 'claude_api_key' in data and data['claude_api_key'] is not None: settings.claude_api_key = encrypt_sensitive_data(str(data['claude_api_key']).strip())     
    if 'deepseek_api_key' in data and data['deepseek_api_key'] is not None: settings.deepseek_api_key = encrypt_sensitive_data(str(data['deepseek_api_key']).strip()) 
    if 'xai_api_key' in data and data['xai_api_key'] is not None: settings.xai_api_key = encrypt_sensitive_data(str(data['xai_api_key']).strip())
    if 'meta_api_key' in data and data['meta_api_key'] is not None: settings.meta_api_key = encrypt_sensitive_data(str(data['meta_api_key']).strip())
    if 'perplexity_api_key' in data and data['perplexity_api_key'] is not None: settings.perplexity_api_key = encrypt_sensitive_data(str(data['perplexity_api_key']).strip())
    
    if 'provider_keys_json' in data: settings.provider_keys_json = encrypt_sensitive_data(data['provider_keys_json'])
    
    if 'concurrent_limit_enabled' in data: settings.concurrent_limit_enabled = data['concurrent_limit_enabled']
    if 'concurrent_limit' in data and str(data['concurrent_limit']).strip().isdigit(): 
        settings.concurrent_limit = int(str(data['concurrent_limit']).strip())
        
    if 'top_menu_color' in data: settings.top_menu_color = data['top_menu_color']
    if 'default_chat_state' in data: settings.default_chat_state = data['default_chat_state']
    
    if 'voice_silence_duration' in data and str(data['voice_silence_duration']).strip().isdigit(): 
        settings.voice_silence_duration = int(str(data['voice_silence_duration']).strip())
        
    if 'store_images_locally' in data: settings.store_images_locally = data['store_images_locally']
    
    if 'image_search_limit' in data and str(data['image_search_limit']).strip().isdigit(): 
        settings.image_search_limit = int(str(data['image_search_limit']).strip())
        
    if 'default_currency' in data: settings.default_currency = data['default_currency'] 
    
    if 'tax_enabled' in data: settings.tax_enabled = bool(data['tax_enabled'])
    if 'tax_percentage' in data: settings.tax_percentage = float(data['tax_percentage'])

    if 'hide_onboarding_popup' in data:
        val = data['hide_onboarding_popup']
        settings.hide_onboarding_popup = val if isinstance(val, bool) else str(val).lower() in ['true', '1', 'yes']

    if 'image_providers_json' in data: settings.image_providers_json = data['image_providers_json']
    if 'email_method' in data: settings.email_method = data['email_method']
    if 'smtp_host' in data: settings.smtp_host = data['smtp_host']
    
    if 'smtp_port' in data and str(data['smtp_port']).strip().isdigit(): 
        settings.smtp_port = int(str(data['smtp_port']).strip())
        
    if 'smtp_user' in data: settings.smtp_user = data['smtp_user']
    
    # SECURITY FIX: Encrypt SMTP Passwords
    if 'smtp_pass' in data and data['smtp_pass'] and data['smtp_pass'] != '********': 
        settings.smtp_pass = encrypt_sensitive_data(data['smtp_pass']) 
        
    if 'smtp_from_email' in data: settings.smtp_from_email = data['smtp_from_email']
    if 'email_api_url' in data: settings.email_api_url = data['email_api_url']
    if 'email_api_key' in data and data['email_api_key']: settings.email_api_key = encrypt_sensitive_data(data['email_api_key'])

    if 'image_providers_json' in data:
         try:
             providers = json.loads(data['image_providers_json'])
             if 'pexels' in providers and providers['pexels'].get('key'):
                 settings.pexels_api_key = encrypt_sensitive_data(providers['pexels']['key'])
         except: pass

    if 'google_client_id' in data and data['google_client_id'] is not None: settings.google_client_id = str(data['google_client_id']).strip()
    if 'google_client_secret' in data and data['google_client_secret'] is not None: settings.google_client_secret = str(data['google_client_secret']).strip()
    if 'logo_url' in data and data['logo_url'] is not None: settings.logo_url = str(data['logo_url']).strip()
    if 'logo_width' in data and data['logo_width'] is not None: settings.logo_width = str(data['logo_width']).strip()
    if 'logo_height' in data and data['logo_height'] is not None: settings.logo_height = str(data['logo_height']).strip()
    if 'login_left_title' in data: settings.login_left_title = data['login_left_title']
    if 'login_left_content' in data: settings.login_left_content = data['login_left_content']
    if 'terms_content' in data: settings.terms_content = data['terms_content']
    if 'privacy_content' in data: settings.privacy_content = data['privacy_content']
    if 'gdpr_cookie_msg' in data: settings.gdpr_cookie_msg = data['gdpr_cookie_msg']
    
    if 'show_powered_by' in data:
        val = data['show_powered_by']
        if isinstance(val, bool):
            settings.show_powered_by = val
        else:
            settings.show_powered_by = str(val).lower() in ['true', '1', 'yes']
    elif any(k in data for k in ['logo_url', 'login_left_title', 'gdpr_cookie_msg']):
        settings.show_powered_by = False
        
    if 'show_live_preview' in data:
        val = data['show_live_preview']
        if isinstance(val, bool):
            settings.show_live_preview = val
        else:
            settings.show_live_preview = str(val).lower() in ['true', '1', 'yes']

    if 'model_gemini' in data and data['model_gemini'] is not None: settings.model_gemini = str(data['model_gemini']).strip()
    if 'inst_gemini' in data: settings.inst_gemini = data['inst_gemini']
    if 'model_openai' in data and data['model_openai'] is not None: settings.model_openai = str(data['model_openai']).strip()
    if 'inst_openai' in data: settings.inst_openai = data['inst_openai']
    
    if 'model_claude' in data and data['model_claude'] is not None: settings.model_claude = str(data['model_claude']).strip()       
    if 'inst_claude' in data: settings.inst_claude = data['inst_claude']                   
    if 'model_deepseek' in data and data['model_deepseek'] is not None: settings.model_deepseek = str(data['model_deepseek']).strip()   
    if 'inst_deepseek' in data: settings.inst_deepseek = data['inst_deepseek']             
    
    if 'model_grok' in data and data['model_grok'] is not None: settings.model_grok = str(data['model_grok']).strip()
    if 'inst_grok' in data: settings.inst_grok = data['inst_grok']
    if 'model_meta' in data and data['model_meta'] is not None: settings.model_meta = str(data['model_meta']).strip()
    if 'inst_meta' in data: settings.inst_meta = data['inst_meta']
    if 'model_perplexity' in data and data['model_perplexity'] is not None: settings.model_perplexity = str(data['model_perplexity']).strip()
    if 'inst_perplexity' in data: settings.inst_perplexity = data['inst_perplexity']

    if 'image_model_gemini' in data and data['image_model_gemini'] is not None: settings.image_model_gemini = str(data['image_model_gemini']).strip()
    if 'image_model_openai' in data and data['image_model_openai'] is not None: settings.image_model_openai = str(data['image_model_openai']).strip()
    if 'image_model_claude' in data and data['image_model_claude'] is not None: settings.image_model_claude = str(data['image_model_claude']).strip()
    if 'image_model_deepseek' in data and data['image_model_deepseek'] is not None: settings.image_model_deepseek = str(data['image_model_deepseek']).strip()
    if 'image_model_grok' in data and data['image_model_grok'] is not None: settings.image_model_grok = str(data['image_model_grok']).strip()
    if 'image_model_meta' in data and data['image_model_meta'] is not None: settings.image_model_meta = str(data['image_model_meta']).strip()
    if 'image_model_perplexity' in data and data['image_model_perplexity'] is not None: settings.image_model_perplexity = str(data['image_model_perplexity']).strip()

    if 'token_limit_gemini' in data and data['token_limit_gemini'] is not None: settings.token_limit_gemini = str(data['token_limit_gemini']).strip()
    if 'token_limit_openai' in data and data['token_limit_openai'] is not None: settings.token_limit_openai = str(data['token_limit_openai']).strip()
    if 'token_limit_claude' in data and data['token_limit_claude'] is not None: settings.token_limit_claude = str(data['token_limit_claude']).strip()
    if 'token_limit_deepseek' in data and data['token_limit_deepseek'] is not None: settings.token_limit_deepseek = str(data['token_limit_deepseek']).strip()
    if 'token_limit_grok' in data and data['token_limit_grok'] is not None: settings.token_limit_grok = str(data['token_limit_grok']).strip()
    if 'token_limit_meta' in data and data['token_limit_meta'] is not None: settings.token_limit_meta = str(data['token_limit_meta']).strip()
    if 'token_limit_perplexity' in data and data['token_limit_perplexity'] is not None: settings.token_limit_perplexity = str(data['token_limit_perplexity']).strip()

    if 'design_guidelines' in data: settings.design_guidelines = data['design_guidelines']

    if 'default_pages_json' in data:
        settings.default_pages_json = data['default_pages_json']

    try:
        db.session.commit()
        return jsonify({"success": True, "message": "Settings updated."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/reset_user_pass', methods=['POST'])
@login_required
def admin_reset_pass():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user = User.query.get(data.get('user_id'))
    if user:
        new_pass = "temp1234" 
        user.password_hash = generate_password_hash(new_pass)
        user.force_password_reset = True
        db.session.commit()
        return jsonify({"success": True, "message": f"Password reset to '{new_pass}'"})
    return jsonify({"success": False, "error": "User not found"}), 404

# --- BACKGROUND REMOVAL ENDPOINT ---
@app.route('/api/admin/remove_background', methods=['POST'])
@login_required
def admin_remove_background():
    data = request.json
    image_data = data.get('image_data')
    
    if not image_data:
        return jsonify({"success": False, "error": "No image data provided"}), 400
        
    try:
        try:
            from rembg import remove
            from PIL import Image
            import io
        except ImportError:
            return jsonify({
                "success": False, 
                "error": "Background removal module (rembg) is not installed. Run 'pip install rembg Pillow'"
            })
            
        if "," in image_data:
            header, encoded = image_data.split(",", 1)
        else:
            encoded = image_data
            
        img_bytes = base64.b64decode(encoded)
        input_image = Image.open(io.BytesIO(img_bytes))
        output_image = remove(input_image)
        
        buffered = io.BytesIO()
        output_image.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        new_image_data = f"data:image/png;base64,{img_str}"
        return jsonify({"success": True, "image": new_image_data})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ========================================================
# --- NEW: KNOWLEDGE BASE Q&A (ADMIN CMS MANAGEMENT) ---
# ========================================================

@app.route('/api/admin/help/articles', methods=['GET'])
@login_required
def admin_get_help_articles():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    articles = HelpArticle.query.order_by(HelpArticle.id.asc()).all()
    return jsonify([
        {
            "id": a.id,
            "question": a.question,
            "answer": a.answer,
            "category": a.category,
            "is_active": a.is_active,
            "created_at": a.created_at.strftime("%Y-%m-%d") if a.created_at else ""
        } for a in articles
    ])

@app.route('/api/admin/help/articles', methods=['POST'])
@login_required
def admin_save_help_article():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    article_id = data.get('id')
    question = data.get('question')
    answer = data.get('answer')
    category = data.get('category', 'General')

    if not question or not answer:
        return jsonify({"success": False, "error": "Question and Answer are required."}), 400

    if article_id:
        article = HelpArticle.query.get(article_id)
        if not article: return jsonify({"success": False, "error": "Article not found."}), 404
        article.question = question
        article.answer = answer
        article.category = category
        article.is_active = data.get('is_active', True)
    else:
        article = HelpArticle(
            question=question,
            answer=answer,
            category=category,
            is_active=data.get('is_active', True)
        )
        db.session.add(article)

    db.session.commit()
    return jsonify({"success": True, "message": "Q&A saved successfully."})

@app.route('/api/admin/help/articles/<int:aid>', methods=['DELETE'])
@login_required
def admin_delete_help_article(aid):
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    article = HelpArticle.query.get(aid)
    if not article: return jsonify({"success": False, "error": "Article not found."}), 404
    db.session.delete(article)
    db.session.commit()
    return jsonify({"success": True, "message": "Article deleted."})

@app.route('/api/admin/help/export', methods=['GET'])
@login_required
def admin_export_help_articles():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    articles = HelpArticle.query.all()
    export_data = [
        {
            "question": a.question,
            "answer": a.answer,
            "category": a.category,
            "is_active": a.is_active
        } for a in articles
    ]
    return jsonify({"success": True, "articles": export_data})

@app.route('/api/admin/help/import', methods=['POST'])
@login_required
def admin_import_help_articles():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    articles_data = data.get('articles', [])
    if not articles_data:
        return jsonify({"success": False, "error": "No Q&A articles found to import."}), 400

    count = 0
    for item in articles_data:
        if item.get('question') and item.get('answer'):
            art = HelpArticle(
                question=item['question'],
                answer=item['answer'],
                category=item.get('category', 'General'),
                is_active=item.get('is_active', True)
            )
            db.session.add(art)
            count += 1

    db.session.commit()
    return jsonify({"success": True, "message": f"Successfully imported {count} Q&A articles."})

# ========================================================
# --- NEW: HIRE DEVELOPER REQUESTS MANAGEMENT ---
# ========================================================

@app.route('/api/admin/hire_requests', methods=['GET'])
@login_required
def admin_get_hire_requests():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    requests_list = HireRequest.query.order_by(desc(HireRequest.created_at)).all()
    return jsonify([
        {
            "id": r.id,
            "user_id": r.user_id,
            "user_email": r.user.email if r.user else "Anonymous",
            "name": r.name,
            "email": r.email,
            "phone": r.phone or "",
            "country": r.country or "",
            "description": r.description,
            "status": r.status,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""
        } for r in requests_list
    ])

@app.route('/api/admin/hire_requests/status', methods=['POST'])
@login_required
def admin_update_hire_status():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    req_id = data.get('id')
    status = data.get('status', 'Contacted')
    
    req = HireRequest.query.get(req_id)
    if not req: return jsonify({"success": False, "error": "Request not found."}), 404
    
    req.status = status
    db.session.commit()
    return jsonify({"success": True, "message": "Hire request status updated."})

# ========================================================
# --- NEW: FEEDBACK & RATINGS MANAGEMENT ---
# ========================================================

@app.route('/api/admin/feedback', methods=['GET'])
@login_required
def admin_get_feedbacks():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    feedbacks = UserFeedback.query.order_by(desc(UserFeedback.created_at)).all()
    return jsonify([
        {
            "id": f.id,
            "user_id": f.user_id,
            "username": f.user.username if f.user else "Anonymous",
            "email": f.user.email if f.user else "Anonymous",
            "rating": f.rating,
            "message": f.message or "",
            "admin_reply": f.admin_reply or "",
            "is_replied": f.is_replied,
            "created_at": f.created_at.strftime("%Y-%m-%d %H:%M") if f.created_at else "",
            "replied_at": f.replied_at.strftime("%Y-%m-%d %H:%M") if f.replied_at else ""
        } for f in feedbacks
    ])

@app.route('/api/admin/feedback/reply', methods=['POST'])
@login_required
def admin_reply_feedback():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    fb_id = data.get('id')
    reply_text = data.get('reply')

    if not reply_text: return jsonify({"success": False, "error": "Reply message cannot be empty."}), 400

    fb = UserFeedback.query.get(fb_id)
    if not fb: return jsonify({"success": False, "error": "Feedback not found."}), 404

    fb.admin_reply = reply_text
    fb.is_replied = True
    fb.replied_at = datetime.now(timezone.utc)

    # Automatically notify the user inside their Messages area!
    if fb.user_id:
        thread_id = str(uuid.uuid4())
        notification = Notification(
            user_id=fb.user_id,
            sender="Admin CMS",
            subject="Reply to your Feedback/Suggestion",
            body=f"Hello,\n\nRegarding your feedback: '{fb.message}'\n\nAdmin Reply:\n{reply_text}",
            thread_id=thread_id
        )
        msg = UserMessage(
            user_id=fb.user_id,
            subject="Reply to your Feedback/Suggestion",
            body=f"Hello,\n\nRegarding your feedback: '{fb.message}'\n\nAdmin Reply:\n{reply_text}",
            thread_id=thread_id
        )
        db.session.add(notification)
        db.session.add(msg)

    db.session.commit()
    return jsonify({"success": True, "message": "Reply sent to user's messages area successfully!"})

# ========================================================
# --- NEW: API KEY PURCHASE REQUESTS MANAGEMENT ---
# ========================================================

@app.route('/api/admin/api_key_requests', methods=['GET'])
@login_required
def admin_get_api_key_requests():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    requests_list = ApiKeyRequest.query.order_by(desc(ApiKeyRequest.created_at)).all()
    return jsonify([
        {
            "id": r.id,
            "user_id": r.user_id,
            "user_email": r.user.email if r.user else "Unknown",
            "provider": r.provider,
            "amount": r.amount,
            "status": r.status,
            "assigned_key": r.assigned_key or "",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            "updated_at": r.updated_at.strftime("%Y-%m-%d %H:%M") if r.updated_at else ""
        } for r in requests_list
    ])

@app.route('/api/admin/api_key_requests/update', methods=['POST'])
@login_required
def admin_update_api_key_request():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    req_id = data.get('id')
    status = data.get('status')
    assigned_key = data.get('assigned_key', '').strip()
    
    req = ApiKeyRequest.query.get(req_id)
    if not req: return jsonify({"success": False, "error": "Request not found."}), 404
    
    req.status = status
    if assigned_key:
        req.assigned_key = assigned_key
        
    db.session.commit()
    
    # If completed and key assigned, send email and system notification
    if status.lower() == 'completed' and req.user:
        subject = f"Your {req.provider} API Key Request is Completed"
        body = f"Hello,\n\nYour request for a {req.provider} API Key (${req.amount}) has been processed.\n\nYour API Key: {req.assigned_key}\n\nYou can now copy this from your My Orders section and paste it into your AI Settings."
        
        # 1. Send Internal Notification
        thread_id = str(uuid.uuid4())
        n = Notification(user_id=req.user_id, sender="Admin", subject=subject, body=body, thread_id=thread_id)
        m = UserMessage(user_id=req.user_id, subject=subject, body=body, thread_id=thread_id)
        db.session.add(n)
        db.session.add(m)
        db.session.commit()
        
        # 2. Send Real Email
        try:
            EmailService.send_email(req.user.email, subject, body.replace('\n', '<br>'))
        except Exception as e:
            print(f"Failed to send email for API Key completion: {e}")

    return jsonify({"success": True, "message": "API Key request updated successfully."})

# ==========================================================================
# FIRST-LOGIN ADMIN SETUP (AI key + Image key + SMTP reminder)
# ==========================================================================
_AI_KEY_COLUMNS = {
    'gemini': 'gemini_api_key', 'openai': 'openai_api_key', 'claude': 'claude_api_key',
    'deepseek': 'deepseek_api_key', 'grok': 'xai_api_key', 'meta': 'meta_api_key',
    'perplexity': 'perplexity_api_key'
}

def _admin_setup_status(s):
    has_ai = False
    if s:
        for col in _AI_KEY_COLUMNS.values():
            val = decrypt_sensitive_data(getattr(s, col, None))
            if val and len(str(val).strip()) > 5:
                has_ai = True; break
        if not has_ai:
            try:
                pk = json.loads(decrypt_sensitive_data(s.provider_keys_json) or '{}')
                has_ai = any(any(k and len(str(k).strip()) > 5 for k in (v or [])) for v in pk.values() if isinstance(v, list))
            except Exception:
                pass

    has_img = False
    if s:
        try:
            provs = json.loads(s.image_providers_json or '{}')
            for cfg in provs.values():
                if isinstance(cfg, dict) and cfg.get('enabled') and (str(cfg.get('key') or '').strip() or cfg.get('keys')):
                    has_img = True; break
        except Exception:
            pass
        if not has_img and decrypt_sensitive_data(getattr(s, 'pexels_api_key', None)):
            has_img = True

    has_smtp = bool(s and (
        (s.smtp_host and s.smtp_user and s.smtp_pass) or
        (getattr(s, 'email_method', '') == 'api' and s.email_api_url and s.email_api_key)
    ))
    return {"has_ai_key": has_ai, "has_image_key": has_img, "has_smtp": has_smtp,
            "ai_provider": (s.ai_provider if s and s.ai_provider else 'gemini')}


@app.route('/api/admin/setup_status', methods=['GET'])
@login_required
def admin_setup_status():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    s = SystemSettings.query.first()
    status = _admin_setup_status(s)
    status["can_generate"] = status["has_ai_key"]
    status["success"] = True
    return jsonify(status)


@app.route('/api/admin/quick_setup', methods=['POST'])
@login_required
def admin_quick_setup():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json or {}
    s = SystemSettings.query.first()
    if not s:
        s = SystemSettings(); db.session.add(s)

    ai_provider = str(data.get('ai_provider') or '').strip().lower()
    ai_key = str(data.get('ai_api_key') or '').strip()
    img_provider = str(data.get('image_provider') or '').strip().lower()
    img_key = str(data.get('image_api_key') or '').strip()

    if not ai_key and not img_key and not _admin_setup_status(s)['has_ai_key']:
        return jsonify({"success": False, "error": "Please enter at least one API key."}), 400

    if ai_key:
        if ai_provider not in _AI_KEY_COLUMNS:
            return jsonify({"success": False, "error": "Please choose a valid AI provider."}), 400
        setattr(s, _AI_KEY_COLUMNS[ai_provider], encrypt_sensitive_data(ai_key))
        s.ai_provider = ai_provider
        # Also add to the rotation pool so every provider (incl. Grok) picks it up
        try:
            pk = json.loads(decrypt_sensitive_data(s.provider_keys_json) or '{}')
        except Exception:
            pk = {}
        pool = [k for k in (pk.get(ai_provider) or []) if k and k != ai_key]
        pk[ai_provider] = [ai_key] + pool
        s.provider_keys_json = encrypt_sensitive_data(json.dumps(pk))

    if img_key:
        valid_img = ['pexels', 'unsplash', 'pixabay', 'freepik', 'shutterstock', 'mighty']
        if img_provider not in valid_img:
            return jsonify({"success": False, "error": "Please choose a valid image provider."}), 400
        try:
            provs = json.loads(s.image_providers_json or '{}')
        except Exception:
            provs = {}
        cfg = provs.get(img_provider) or {"type": "free" if img_provider == 'pexels' else "paid"}
        cfg['key'] = img_key
        cfg['enabled'] = True
        provs[img_provider] = cfg
        s.image_providers_json = json.dumps(provs)
        if img_provider == 'pexels':
            s.pexels_api_key = encrypt_sensitive_data(img_key)

    # Optional SMTP details
    if str(data.get('smtp_host') or '').strip():
        s.email_method = 'smtp'
        s.smtp_host = str(data.get('smtp_host')).strip()
        port = str(data.get('smtp_port') or '587').strip()
        if port.isdigit(): s.smtp_port = int(port)
        s.smtp_user = str(data.get('smtp_user') or '').strip()
        if data.get('smtp_pass') and data.get('smtp_pass') != '********':
            s.smtp_pass = encrypt_sensitive_data(data.get('smtp_pass'))
        s.smtp_from_email = str(data.get('smtp_from_email') or s.smtp_user or '').strip()

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    status = _admin_setup_status(s)
    status["can_generate"] = status["has_ai_key"]
    status["success"] = True
    return jsonify(status)
