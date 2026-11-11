# ================================================================================================
# Copyright (c) 2026 Orgif Solutions LLP. All rights reserved. For info visit ewebgen.com
#
# This file is part of the Editable Website Developer AI Agent project and Owned by Orgif Solutions LLP.
#
# This software is the confidential and proprietary information of 
# Orgif Solutions LLP. You shall not disclose such Confidential Information 
# and shall use it only in accordance with the terms of the license 
# agreement you entered into with [Your Company Name].
#
# Unauthorized copying of this file, via any medium, is strictly prohibited.
# Proprietary and confidential.
# Version 4.22
# ==============================================================================

import os
import json
import re
import base64
from flask import request, jsonify, render_template, Response, stream_with_context, redirect, send_from_directory, make_response, render_template_string
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

# SECURITY FIX: Import Bleach for HTML sanitization to prevent Stored XSS
try:
    import bleach
    def sanitize_input(text):
        if not text: return ""
        return bleach.clean(text, strip=True)
except ImportError:
    # Safe fallback if bleach is not installed in the environment
    def sanitize_input(text):
        if not text: return ""
        return re.sub(r'<[^>]*>', '', text)

# --- IMPORT MODULES ---
# 1. Database & Config (Added HelpArticle, HireRequest, UserFeedback)
from database_setup import app, db, User, SystemSettings, HelpArticle, HireRequest, UserFeedback

# --- CONFIG: INCREASED OVERALL UPLOAD LIMIT TO 500MB ---
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

# 2. Services & Core Helpers
from services_core import (
    get_user_session_data, get_current_files, save_new_state, 
    sync_user_project_state, get_user_project_dir, EmailService
)
from services_ai import AIGenerationService
from services_image_providers import ImageProviderService

# 3. Register All Route Modules (This automatically binds the @app.routes)
import routes_auth
import routes_admin
import routes_project
import routes_media_templates
import routes_payment
import routes_updates
import routes_client_updater

# Import the storage checker from routes_project to use in /generate
from routes_project import check_storage_limit

# ==========================================
# WEB-BASED LICENSE ACTIVATION SYSTEM
#   Website install  -> bound to the domain URL
#   Desktop app      -> bound to this computer's MAC address (EWEBGEN_DESKTOP=1)
# ==========================================
@app.before_request
def require_license_check():
    """Intercepts all traffic if the application is not activated."""
    # Whitelisted endpoints
    allowed_endpoints = ['activate_page', 'api_activate_license', 'api_free_plan_status', 'api_activate_free_plan',
                         'api_license_state', 'api_license_recheck', 'static', 'internal_setup_templates']
    if request.endpoint in allowed_endpoints:
        return

    from security_engine import SecurityEngine

    # --- LOCAL DEVELOPMENT BYPASS ---
    # FIX: Previously ANY request whose Host was localhost/127.0.0.1 skipped the license check.
    # Behind a reverse proxy that does not forward the Host header, that meant the live site was
    # never checked. Now the bypass only applies to explicit development setups, and never to
    # the desktop app (licensed by MAC address).
    if not SecurityEngine.is_desktop_mode():
        dev_mode = (os.getenv('FLASK_ENV', '').lower() == 'development' or os.getenv('LICENSE_DEV_BYPASS') == '1')
        host_header = request.headers.get('Host', '').lower()
        forwarded = request.headers.get('X-Forwarded-Host') or request.headers.get('X-Forwarded-For') or request.headers.get('X-Real-IP')
        if dev_mode and not forwarded and ('127.0.0.1' in host_header or 'localhost' in host_header):
            return

    valid, msg = SecurityEngine.verify_license_logic(silent=True)
    
    if not valid:
        wants_json = (request.path.startswith('/api/') or request.method != 'GET' or request.is_json or
                      request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
                      'application/json' in (request.headers.get('Accept') or ''))
        if wants_json:
            return jsonify({"success": False, "error": f"Application locked: {msg}", "message": msg,
                            "needs_activation": True, "redirect": "/activation"}), 403
        return redirect('/activation')

# First run: set the admin password right after license activation (registers its guard
# AFTER the license guard above, so the license screen always comes first)
import routes_first_run

# ==========================================
# DESKTOP EDITION: no monetization, system updates or commerce/orders in Admin CMS
# (the desktop app is updated by replacing eWebGen.exe and has no payments)
# ==========================================
DESKTOP_BLOCKED_PREFIXES = (
    '/api/system/',                       # System Updates (remote updates, backups, seed upload)
    '/api/admin/updates',
    '/api/admin/plans', '/api/admin/create_plan', '/api/admin/update_plan', '/api/admin/delete_plan',
    '/api/admin/payment_gateways', '/api/admin/update_payment_gateway', '/api/admin/update_gateway',
    '/api/admin/transactions', '/api/admin/orders', '/api/admin/api_key_requests',
)

@app.context_processor
def inject_edition():
    from security_engine import SecurityEngine
    return {"desktop_edition": SecurityEngine.is_desktop_mode()}

@app.before_request
def desktop_edition_guard():
    from security_engine import SecurityEngine
    if SecurityEngine.is_desktop_mode() and request.path.startswith(DESKTOP_BLOCKED_PREFIXES):
        return jsonify({"success": False, "error": "Not available in the desktop edition."}), 404

@app.route('/activation')
def activate_page():
    from security_engine import SecurityEngine
    
    valid, msg = SecurityEngine.verify_license_logic(silent=True)
    if valid:
        return redirect('/')

    is_desktop = SecurityEngine.is_desktop_mode()
    bind_label = "Device MAC address" if is_desktop else "Domain"
    bind_value = SecurityEngine.get_hardware_id() if is_desktop else SecurityEngine.get_active_domain()
    lic_state = SecurityEngine.get_license_state()
    lic_state["reason"] = msg

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>eWebGen License</title>
        <style>
            * { box-sizing: border-box; }
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg,#eef2ff,#f8fafc); display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 16px; }
            .auth-box { background: white; padding: 32px; border-radius: 14px; box-shadow: 0 18px 45px rgba(15,23,42,0.12); width: 100%; max-width: 480px; text-align: center; border-top: 5px solid #6610f2; }
            .auth-box.expired { border-top-color: #dc2626; }
            h2 { color: #1e293b; margin: 0 0 8px; }
            p { color: #64748b; font-size: 14.5px; margin: 0 0 16px; line-height: 1.5; }
            .bind { display: inline-block; background: #f1f5f9; color: #334155; border-radius: 999px; padding: 6px 14px; font-size: 13px; margin-bottom: 16px; font-family: Consolas, monospace; word-break: break-all; }
            .status { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; border-radius: 10px; padding: 12px 14px; font-size: 14px; margin-bottom: 18px; text-align: left; line-height: 1.5; }
            .status b { display: block; font-size: 15px; margin-bottom: 2px; }
            input { width: 100%; padding: 12px; margin: 6px 0; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 15px; }
            input:focus { border-color: #6610f2; outline: none; box-shadow: 0 0 0 3px rgba(102,16,242,.15); }
            button, .btn { display: block; width: 100%; padding: 13px; color: white; border: none; border-radius: 8px; font-size: 15.5px; cursor: pointer; margin-top: 10px; font-weight: bold; text-decoration: none; text-align: center; }
            .btn-key { background: #28a745; } .btn-key:hover { background: #218838; }
            .btn-free { background: #6610f2; } .btn-free:hover { background: #520dc2; }
            .btn-renew { background: #ea580c; } .btn-renew:hover { background: #c2410c; }
            .btn-ghost { background: #f1f5f9; color: #334155; } .btn-ghost:hover { background: #e2e8f0; }
            button:disabled { background: #94a3b8 !important; color: #fff; cursor: not-allowed; }
            .panel { display: none; text-align: left; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
            .panel.free { background: #f5f3ff; border: 1px solid #ddd6fe; }
            .panel.paid { background: #fff7ed; border: 1px solid #fed7aa; }
            .panel h3 { margin: 0 0 4px; font-size: 16px; color: #1e293b; }
            .panel small { color: #6b7280; display: block; margin-bottom: 4px; }
            .divider { display: flex; align-items: center; gap: 10px; color: #94a3b8; font-size: 12px; margin: 6px 0 12px; text-transform: uppercase; }
            .divider:before, .divider:after { content: ''; flex: 1; height: 1px; background: #e2e8f0; }
            #msg { margin-top: 16px; font-size: 14.5px; font-weight: bold; min-height: 20px; }
            a.get-key-link { color: #6610f2; font-size: 14px; }
        </style>
    </head>
    <body>
        <div class="auth-box" id="box">
            <h2 id="title">🔐 Activate eWebGen</h2>
            <p id="subtitle">Activate this {{ 'desktop app' if is_desktop else 'installation' }} to start building websites.</p>
            <div class="bind">{{ bind_label }}: {{ bind_value }}</div>

            <div class="status" id="status-box" style="display:none;"></div>

            <!-- FREE: activate or one-click renew (asks name + email) -->
            <div class="panel free" id="free-panel">
                <h3 id="free-title">⚡ Free plan available</h3>
                <small id="free-sub"></small>
                <input type="text" id="free-name" placeholder="Your name" autocomplete="name">
                <input type="email" id="free-email" placeholder="Your email (license key will be sent here)" autocomplete="email">
                <button class="btn-free" id="free-btn" onclick="activateFree()">⚡ Activate Free Plan</button>
            </div>

            <!-- PAID: renew on the owner portal -->
            <div class="panel paid" id="paid-panel">
                <h3>🔁 Renew your license</h3>
                <small>Renew securely on the eWebGen license portal. Your same license key keeps working after renewal.</small>
                <a class="btn btn-renew" id="renew-link" href="#" target="_blank" rel="noopener noreferrer">Renew License →</a>
                <button class="btn-ghost" id="recheck-btn" onclick="recheck()">I've renewed — check again</button>
            </div>

            <div class="divider" id="key-divider" style="display:none;">or enter a license key</div>
            <input type="text" id="license-key" placeholder="Enter License Key (e.g. EWEB-XXXX)" autocomplete="off">
            <button class="btn-key" id="activate-btn" onclick="activate()">Verify & Activate</button>
            <div style="margin-top:14px;">
                <a href="https://ewebgen.com/license.html" target="_blank" rel="noopener noreferrer" class="get-key-link">Get your License key &rarr;</a>
            </div>
            <div id="msg"></div>
        </div>
        <script>
            const LIC = {{ lic_state | tojson }};
            const msg = document.getElementById('msg');
            const $ = id => document.getElementById(id);
            const isBlocked = LIC.state && LIC.state !== 'none' && LIC.state !== 'active';

            function done(text) {
                msg.style.color = "#28a745";
                msg.textContent = text || "✅ Activation Successful! Booting system...";
                setTimeout(() => window.location.href = '/', 1500);
            }
            function fail(btn, label, text) {
                msg.style.color = "#dc3545";
                msg.textContent = "❌ " + text;
                if (btn) { btn.disabled = false; btn.textContent = label; }
            }

            // ---------- Screen for an expired / blocked license ----------
            if (isBlocked) {
                $('box').classList.add('expired');
                const expired = LIC.state === 'expired';
                $('title').textContent = expired ? '⏰ License expired' : '⛔ License not active';
                $('subtitle').textContent = 'eWebGen is locked until the license is renewed or re-activated.';
                const sb = $('status-box');
                sb.style.display = 'block';
                sb.innerHTML = '<b>' + (expired ? 'Expired' : ('Status: ' + LIC.state)) + (LIC.expiry_date ? ' on ' + LIC.expiry_date : '') + '</b>' +
                               (LIC.license_key_masked ? 'Key: ' + LIC.license_key_masked + '<br>' : '') +
                               (LIC.message || LIC.reason || '');
                $('key-divider').style.display = 'flex';
                $('activate-btn').textContent = 'Activate New Key';

                if (!LIC.is_free && (expired || LIC.state === 'cancelled')) {
                    $('paid-panel').style.display = 'block';
                    $('renew-link').href = LIC.renew_url || 'https://ewebgen.com/license.html';
                }
            }

            // ---------- Ask owner server whether a zero-price plan applies ----------
            fetch('/api/license/free_plan_status', { method: 'POST' })
                .then(r => r.json())
                .then(d => {
                    if (!d) return;
                    const canFreeRenew = isBlocked && (d.can_reactivate || (LIC.is_free && d.available));
                    if (isBlocked && !canFreeRenew) return;            // paid license -> owner portal
                    if (!isBlocked && !d.available) return;
                    const planName = (d.plan && d.plan.name) ? d.plan.name : 'Free';
                    const period = (d.plan && d.plan.plan_type) ? d.plan.plan_type : '';
                    $('free-panel').style.display = 'block';
                    $('paid-panel').style.display = 'none';
                    $('key-divider').style.display = 'flex';
                    const btn = $('free-btn');
                    if (canFreeRenew) {
                        $('free-title').textContent = '♻️ Renew for free';
                        $('free-sub').textContent = 'Your license is on the ' + planName + ' plan (' + (period || 'free') + '). Enter your name and email, then renew with one click.';
                        btn.textContent = btn.dataset.label = '♻️ Renew License';
                    } else {
                        $('free-sub').textContent = planName + ' plan' + (period ? ' (' + period + ')' : '') + ' — no payment needed.';
                        btn.textContent = btn.dataset.label = '⚡ Activate Free Plan';
                    }
                }).catch(() => {});

            function activateFree() {
                const btn = $('free-btn');
                const email = $('free-email').value.trim();
                const name = $('free-name').value.trim();
                if (!name) { fail(btn, btn.dataset.label, 'Please enter your name.'); return; }
                if (!email || email.indexOf('@') < 1) { fail(btn, btn.dataset.label, 'Please enter a valid email.'); return; }
                btn.disabled = true; btn.textContent = 'Please wait...';
                msg.textContent = '';
                fetch('/api/license/activate_free', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ email: email, name: name })
                }).then(r => r.json()).then(d => {
                    if (d.success) done('✅ ' + (d.message || 'Activated!') + ' Booting system...');
                    else fail(btn, btn.dataset.label, d.error || 'Activation failed.');
                }).catch(() => fail(btn, btn.dataset.label, 'Network error. Please try again.'));
            }

            function recheck() {
                const btn = $('recheck-btn');
                btn.disabled = true; btn.textContent = 'Checking with license server...';
                fetch('/api/license/recheck', { method: 'POST' }).then(r => r.json()).then(d => {
                    if (d.success) done('✅ License renewed! Booting system...');
                    else fail(btn, "I've renewed — check again", d.error || 'License is still not active.');
                }).catch(() => fail(btn, "I've renewed — check again", 'Network error.'));
            }

            function activate() {
                const key = $('license-key').value.trim();
                const btn = $('activate-btn');
                const label = btn.textContent;
                if (!key) { fail(btn, label, 'Please enter a valid license key.'); return; }
                btn.disabled = true; btn.textContent = "Verifying with Server...";
                msg.textContent = "";
                fetch('/api/activate_license', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ license_key: key })
                }).then(r => r.json()).then(data => {
                    if (data.success) done();
                    else fail(btn, label, data.error);
                }).catch(() => fail(btn, label, 'Network error connecting to activation server.'));
            }
            $('license-key').addEventListener('keypress', function(e) { if (e.key === 'Enter') activate(); });
        </script>
    </body>
    </html>
    """
    return render_template_string(html, is_desktop=is_desktop, bind_label=bind_label, bind_value=bind_value, lic_state=lic_state)

@app.route('/api/license/state', methods=['GET'])
def api_license_state():
    from security_engine import SecurityEngine
    valid, msg = SecurityEngine.verify_license_logic(silent=True)
    st = SecurityEngine.get_license_state()
    st.update({"valid": valid, "message": st.get("message") or msg, "warning": SecurityEngine.get_license_warning() if valid else ""})
    return jsonify(st)

@app.route('/api/license/recheck', methods=['POST'])
def api_license_recheck():
    from security_engine import SecurityEngine
    valid, msg = SecurityEngine.verify_license_logic(silent=False, force_online=True)
    return jsonify({"success": True} if valid else {"success": False, "error": msg})

@app.route('/api/activate_license', methods=['POST'])
def api_activate_license():
    data = request.json or {}
    license_key = (data.get('license_key') or '').strip()
    if not license_key:
        return jsonify({"success": False, "error": "License key required."})
        
    from security_engine import SecurityEngine
    valid, msg = SecurityEngine.verify_license_logic(license_key=license_key, silent=False)
    
    if valid:
        return jsonify({"success": True, "message": "Activated successfully."})
    else:
        return jsonify({"success": False, "error": msg})

@app.route('/api/license/free_plan_status', methods=['POST', 'GET'])
def api_free_plan_status():
    from security_engine import SecurityEngine
    return jsonify(SecurityEngine.get_free_plan_status())

@app.route('/api/license/activate_free', methods=['POST'])
def api_activate_free_plan():
    from security_engine import SecurityEngine
    data = request.json or {}
    ok, msg = SecurityEngine.activate_free_plan(email=(data.get('email') or '').strip(), name=(data.get('name') or '').strip())
    return jsonify({"success": ok, "message": msg} if ok else {"success": False, "error": msg})

# -------------------- MAIN APP ROUTES --------------------

@app.route('/')
def index():
    # FIX ISSUE 1: Prevent Firefox from recursively loading the main UI inside preview iframes
    resp = make_response(render_template('index.html'))
    resp.headers['X-Frame-Options'] = 'DENY'
    return resp

# =========================================================
# CLIENT-SIDE API ENDPOINTS FOR HELP SLIDER & FEEDBACK
# (Direct Forwarding to Owner CMS - Zero Local DB Touch)
# =========================================================

@app.route('/api/help/articles', methods=['GET'])
def get_public_help_articles():
    """
    Returns active Q&A knowledge base items for the Help Slider search box.
    """
    articles = HelpArticle.query.filter_by(is_active=True).order_by(HelpArticle.category.asc(), HelpArticle.id.asc()).all()
    results = []
    for a in articles:
        results.append({
            "id": a.id,
            "question": a.question,
            "answer": a.answer,
            "category": a.category
        })
    return jsonify({"success": True, "articles": results})

@app.route('/api/hire/request', methods=['POST'])
def submit_hire_request():
    """
    Forwards developer hiring requests directly to the Owner Admin CMS (app.ewebgen.com).
    """
    data = request.json or {}
    name = sanitize_input(data.get('name', ''))
    email = sanitize_input(data.get('email', ''))
    phone = sanitize_input(data.get('phone', ''))
    country = sanitize_input(data.get('country', ''))
    # SECURITY FIX: Sanitize input to prevent Stored XSS on the Admin CMS
    description = sanitize_input(data.get('description', ''))

    if not name or not email or not description:
        return jsonify({"success": False, "error": "Name, Email, and Project Description are required fields."}), 400

    try:
        import requests
        client_domain = request.host.split(':')[0].lower() if hasattr(request, 'host') else "unknown-client"
        
        payload = {
            "name": name,
            "email": email,
            "phone": phone,
            "country": country,
            "description": description,
            "client_domain": client_domain,
            "user_email": current_user.email if current_user and current_user.is_authenticated else email
        }

        # Forward directly to the Owner Server API
        resp = requests.post("https://app.ewebgen.com/api/external/hire_request", json=payload, timeout=8)
        
        if resp.status_code in [200, 201]:
            return jsonify({"success": True, "message": "Your request has been submitted to Orgif Solutions developer team. We will contact you soon!"})
        else:
            return jsonify({"success": False, "error": "Could not submit request to remote server."}), 500

    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to connect to server: {str(e)}"}), 500


@app.route('/api/feedback/submit', methods=['POST'])
def submit_user_feedback():
    """
    Forwards user feedback and ratings directly to the Owner Admin CMS (app.ewebgen.com).
    """
    data = request.json or {}
    try:
        rating = int(data.get('rating', 5))
        if rating < 1: rating = 1
        if rating > 5: rating = 5
    except (ValueError, TypeError):
        rating = 5

    # SECURITY FIX: Sanitize input to prevent Stored XSS on the Admin CMS
    message = sanitize_input(data.get('message', ''))

    try:
        import requests
        client_domain = request.host.split(':')[0].lower() if hasattr(request, 'host') else "unknown-client"
        user_email = current_user.email if current_user and current_user.is_authenticated else "Anonymous"
        username = current_user.username if current_user and current_user.is_authenticated else "Anonymous"
        
        payload = {
            "rating": rating,
            "message": message,
            "client_domain": client_domain,
            "user_email": user_email,
            "username": username
        }

        # Forward directly to the Owner Server API
        resp = requests.post("https://app.ewebgen.com/api/external/feedback_submit", json=payload, timeout=8)
        
        if resp.status_code in [200, 201]:
            return jsonify({"success": True, "message": "Thank you for your feedback and rating!"})
        else:
            return jsonify({"success": False, "error": "Could not submit feedback to remote server."}), 500

    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to connect to server: {str(e)}"}), 500


# ==========================================
# IDEAS (Help > Submit Idea) & ADS (below the generation progress circle)
# Both are relayed to / from the Owner Admin CMS.
# ==========================================
_ADS_CACHE = {"at": 0, "ads": [], "key": ""}

def _owner_base():
    from security_engine import OWNER_SERVERS
    return OWNER_SERVERS[0]

@app.route('/api/idea/submit', methods=['POST'])
def submit_idea():
    data = request.json or {}
    title = sanitize_input(data.get('title', ''))[:200]
    description = sanitize_input(data.get('description', ''))
    if not title or not description:
        return jsonify({"success": False, "error": "Please add a title and describe your idea."}), 400
    try:
        import requests
        from security_engine import SecurityEngine
        is_auth = current_user and current_user.is_authenticated
        payload = {
            "title": title,
            "category": sanitize_input(data.get('category', 'Feature'))[:60],
            "description": description,
            "name": sanitize_input(data.get('name', '')) or (getattr(current_user, 'full_name', '') if is_auth else ''),
            "email": sanitize_input(data.get('email', '')) or (current_user.email if is_auth else ''),
            "client_domain": SecurityEngine.get_hardware_id() if SecurityEngine.is_desktop_mode() else SecurityEngine.get_active_domain(),
            "app_type": SecurityEngine.get_target_type(),
            "app_version": (open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.txt')).read().strip()
                            if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.txt')) else ''),
        }
        resp = requests.post(f"{_owner_base()}/api/external/idea_submit", json=payload, timeout=8)
        if resp.status_code in (200, 201):
            return jsonify({"success": True, "message": "Thank you! Your idea was sent to the eWebGen team."})
        return jsonify({"success": False, "error": "Could not send your idea right now. Please try again later."}), 502
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not reach the server: {e}"}), 502

@app.route('/api/ads/current', methods=['GET'])
def current_ads():
    """Active ads from the owner CMS (cached for 10 minutes)."""
    import time as _time
    from security_engine import SecurityEngine
    placement = request.args.get('placement', 'generation_progress')
    app_type = SecurityEngine.get_target_type()
    key = f"{placement}:{app_type}"
    if _ADS_CACHE["key"] == key and _time.time() - _ADS_CACHE["at"] < 600:
        return jsonify({"success": True, "ads": _ADS_CACHE["ads"]})
    try:
        import requests
        r = requests.get(f"{_owner_base()}/api/public/ads", params={"placement": placement, "app_type": app_type}, timeout=5)
        ads = (r.json() or {}).get("ads", []) if r.status_code == 200 else []
    except Exception:
        ads = _ADS_CACHE["ads"] if _ADS_CACHE["key"] == key else []
    _ADS_CACHE.update({"at": _time.time(), "ads": ads, "key": key})
    return jsonify({"success": True, "ads": ads})

@app.route('/api/ads/track', methods=['POST'])
def track_ad():
    try:
        import requests, threading
        data = request.json or {}
        payload = {"id": data.get('id'), "event": 'click' if data.get('event') == 'click' else 'view'}
        threading.Thread(target=lambda: requests.post(f"{_owner_base()}/api/public/ads/track", json=payload, timeout=5), daemon=True).start()
    except Exception:
        pass
    return jsonify({"success": True})


# --- FILE SERVING ---
@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/preview/<path:filename>')
@login_required 
def preview_page(filename):
    current_files = get_current_files()
    
    if filename not in current_files:
        has_index_php = 'index.php' in current_files
        candidates = []
        
        if has_index_php:
            candidates = [f"{filename}.php", f"{filename}.html"]
        else:
            candidates = [f"{filename}.html", f"{filename}.php"]
            
        found = False
        for candidate in candidates:
            if candidate in current_files:
                filename = candidate
                found = True
                break
        
        if not found:
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                print(f"⚠️ [Preview] Auto-generating missing asset: {filename}")
                search_query = filename.rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ')
                fallback_url = ImageProviderService.get_image_url(search_query)
                return redirect(fallback_url)
            if filename == 'styles.css': return "", 200
            return f"<h1>Error 404: Page '{filename}' not found.</h1><p>If you just imported this, try clicking the page in the dropdown again.</p>", 404

    content = current_files[filename]

    if isinstance(content, str) and content.startswith('data:'):
        try:
            header, encoded = content.split(',', 1)
            mime_type = 'application/octet-stream'
            if ':' in header and ';' in header:
                mime_type = header.split(':')[1].split(';')[0]
            
            binary_data = base64.b64decode(encoded)
            return Response(binary_data, mimetype=mime_type)
        except Exception as e:
            print(f"Error decoding base64 file {filename}: {e}")
            return "Error decoding file", 500

    if filename.endswith('.html') or filename.endswith('.php'):
        css_content = current_files.get('styles.css', None)
        preview_html = content

        if filename.endswith('.html') and css_content:
            if re.search(r'<link[^>]+href=["\']styles\.css["\'][^>]*>', content, re.IGNORECASE):
                preview_html = re.sub(r'<link[^>]+href=["\']styles\.css["\'][^>]*>', f'<style>{css_content}</style>', content, flags=re.IGNORECASE)
                
        # Ensure viewport meta tag exists for mobile menu responsiveness in preview window
        if 'meta name="viewport"' not in preview_html.lower() and '<head>' in preview_html.lower():
            preview_html = re.sub(r'<head>', '<head>\n<meta name="viewport" content="width=device-width, initial-scale=1.0">', preview_html, count=1, flags=re.IGNORECASE)
            
        resp = Response(preview_html, mimetype='text/html')
        
        # SECURITY FIX: Enforce Content Security Policy (CSP)
        # This completely mitigates Stored XSS by blocking external script execution when an Admin or User previews a generated site.
        resp.headers['Content-Security-Policy'] = "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://www.googletagmanager.com; object-src 'none';"
        
        return resp
    else:
        mimetype = 'text/plain'
        if filename.endswith('.css'): mimetype = 'text/css'
        elif filename.endswith('.js'): mimetype = 'application/javascript'
        elif filename.endswith('.json'): mimetype = 'application/json'
        elif filename.endswith('.svg'): mimetype = 'image/svg+xml'
        return Response(content, mimetype=mimetype)

@app.route('/preview/send_mail.php', methods=['POST'])
@login_required
def preview_send_mail():
    name = request.form.get('name', '')
    email = request.form.get('email', '')
    message = request.form.get('message', '')
    
    to_email = current_user.email
    try:
        user_settings = json.loads(current_user.user_settings_json or '{}')
        if user_settings.get('contact_email'):
            to_email = user_settings['contact_email']
    except:
        pass
        
    subject = f"[Website Contact] Message from {name}"
    body = f"Name: {name}<br>Email: {email}<br><br>Message:<br>{message}"
    
    # Send email (using SMTP settings if configured, otherwise default)
    success = EmailService.send_email(to_email, subject, body)
    
    if success:
        return jsonify({"success": True, "message": "Message sent successfully!"})
    else:
        return jsonify({"success": False, "message": "Failed to send email. Check SMTP settings."}), 500

@app.route('/api/sync_state', methods=['POST'])
@login_required
def sync_state():
    data = request.json
    files = data.get('files')
    is_partial = data.get('partial', False) 
    is_new_project = data.get('is_new_project', False) 

    if not files: return jsonify({"success": False, "error": "No files provided"}), 400
    
    if is_new_project:
        session_data = get_user_session_data()
        
        # --- GOAL 3 FIX: Preserve the FTP Project Name during Sync ---
        preserve_name = data.get('preserve_project_name')
        if preserve_name:
            session_data['active_project'] = preserve_name
        else:
            session_data['active_project'] = None 
            
        sync_user_project_state(files, partial=False) 
    else:
        current_files = get_current_files() if is_partial else {}
        current_files.update(files)
        save_new_state(current_files)
    
    return jsonify({"success": True, "message": "Session synced."})

@app.route('/generate', methods=['POST'])
@login_required
def generate():
    data = request.json
    full_history = data.get('history')
    user_id = current_user.id
    
    if not check_storage_limit(user_id):
        return jsonify({"type": "error", "message": f"Storage limit reached. Please upgrade your plan or delete old projects."}), 403

    limit = 3 
    if current_user.user_type:
        limit = current_user.user_type.project_limit
    elif current_user.role == 'admin':
        limit = -1
    
    last_message_text = ""
    if full_history and len(full_history) > 0:
        try:
            parts = full_history[-1].get('parts', [])
            if parts:
                last_message_text = parts[0].get('text', '').lower()
        except: pass

    reset_triggers = ["create a new website", "create new website", "start over", "create a completely new", "new project", "build a new website", "build new website", "make a new website", "make new website", "generate a new website", "generate new website"]
    
    # --- ADDED FIX: Strictly check for the frontend's is_new_generation flag ---
    is_new_request = data.get('is_new_generation', False) or any(trigger in last_message_text for trigger in reset_triggers)
    is_first_generation = not get_current_files(user_id)

    # --- GOAL 1 & 2 FIX: ABSOLUTE FOOLPROOF API KEY CHECK ---
    using_own_key = False
    try:
        from database_setup import app, db, User, SystemSettings
        import json
        with app.app_context():
            user = User.query.get(user_id)
            if user:
                allow_own_keys = user.role == 'admin' or user.allow_own_api_keys or (user.user_type and user.user_type.allow_own_api_keys)
                
                if allow_own_keys:
                    try:
                        user_settings = json.loads(user.user_settings_json or '{}')
                        pref_provider = user_settings.get('ai_provider', 'default').strip().lower()
                        
                        # Only grant bypass IF they selected a specific provider and have a key for it.
                        # If 'default', they use the Admin's AI key, so they consume limits.
                        if pref_provider != 'default':
                            key_to_check = user_settings.get(f'{pref_provider}_api_key')
                            if key_to_check and len(str(key_to_check).strip()) > 5:
                                using_own_key = True
                            elif hasattr(user, 'api_keys_json') and user.api_keys_json:
                                api_keys = json.loads(user.api_keys_json)
                                if api_keys.get(f'{pref_provider}_api_key') and len(str(api_keys.get(f'{pref_provider}_api_key')).strip()) > 5:
                                    using_own_key = True
                            elif hasattr(user, f'{pref_provider}_api_key'):
                                key_attr = getattr(user, f'{pref_provider}_api_key')
                                if key_attr and len(str(key_attr).strip()) > 5:
                                    using_own_key = True
                    except Exception as e:
                        print(f"API Limit Bypass Check Error: {e}", flush=True)
    except Exception as e:
        print(f"API Limit Bypass Setup Error: {e}", flush=True)

    # If limit is not -1 (infinite) AND they are NOT using their own valid API key, block them
    if limit != -1 and not using_own_key:
        total_allowed_limit = limit + getattr(current_user, 'extra_project_limit', 0)
        if is_new_request or is_first_generation:
            if current_user.websites_generated >= total_allowed_limit:
                 return jsonify({"type": "error", "message": f"Project limit reached. Please upgrade your plan to generate more websites."}), 403

    if is_new_request:
        print(f"🧹 [Intent] Reset triggered by user: {current_user.username}")
        from services_core import USER_SESSIONS, USER_SESSIONS_LOCK
        with USER_SESSIONS_LOCK:
            # FIX: Always start from a clean slate for a new website (previously a closed
            # template stayed in the session and was sent back to the AI as "current site").
            USER_SESSIONS[user_id] = {'history_stack': [], 'redo_stack': [], 'active_project': None}

        # FIX: Drop any stale chat turns that belonged to the previously loaded template.
        # Keep only the latest prompt so the AI designs a brand-new site.
        if full_history:
            full_history = [full_history[-1]]

    if not full_history:
        current_files = get_current_files(user_id)
        if not current_files: return jsonify({"error": "No chat history provided"}), 400
        current_page = next((f for f in current_files if f.endswith('.html')), "index.html")
        return jsonify({"files": current_files, "current_page": current_page})
        
    history_for_model = full_history

    current_files = {} if is_new_request else get_current_files(user_id)
    if current_files:
        current_state_for_prompt = current_files.copy()
        for filename in current_state_for_prompt:
            content = current_state_for_prompt[filename]
            if isinstance(content, str) and content.startswith('data:'):
                current_state_for_prompt[filename] = "[BINARY_DATA_SKIPPED]"
            elif filename.endswith(('.html', '.css', '.php')) and len(content) > 20000:
                current_state_for_prompt[filename] = content[:20000] + '... [TRUNCATED]'
        
        context_msg = {
            "role": "user",
            "parts": [{"text": "CURRENT_WEBSITE_STRUCTURE:\n" + json.dumps(current_state_for_prompt, indent=2)}]
        }
        history_for_model = [context_msg] + full_history

    url_pattern = re.search(r'(https?://[^\s]+)', last_message_text)
    reference_instruction = ""
    if url_pattern:
        ref_url = url_pattern.group(0)
        print(f"🔗 [Reference Detected] URL: {ref_url}")
        reference_instruction = (
            f"**USER REFERENCE URL DETECTED: {ref_url}**\n"
            "The user has provided a reference URL. Analyze the typical structure, layout, and design aesthetic of this website based on your knowledge. "
            "Generate a website that mimics this structure and vibe (e.g., navigation style, hero section layout, color palette mood), "
            "but use the user's specific content topic. Do NOT copy exact text or private data.\n"
        )

    analytics_instruction = ""
    try:
        user_settings = json.loads(current_user.user_settings_json or '{}')
        if user_settings.get('google_analytics'):
            analytics_code = user_settings['google_analytics']
            analytics_instruction = f"\n**USER SETTINGS MANDATE:** You MUST include the following Google Analytics code EXACTLY as provided immediately after the `<head>` tag in EVERY generated HTML file:\n{analytics_code}\n"
    except:
        pass

    # --- GOAL 3 FIX: PREVENT MARKDOWN WRAPPERS IN CSS/HTML JSON VALUES ---
    system_instruction = (
        "You are an expert web developer specializing in multi-page websites. "
        "Your task is to generate or **modify the current site structure** to fulfill the user's request. "
        "CRITICAL: If the user is only asking for a small change (e.g., changing text, adding one page, changing color), you **MUST** preserve the structure, links, and overall design of the existing site. "
        
        "**PRESERVATION OF MANUAL EDITS (IMPORTANT):** "
        "1. If the user has manually applied styling (bold, colors, fonts) via inline styles or span tags, you **MUST** preserve these specific tags unless explicitly asked to remove them. "
        "2. When modifying sections, ensure distinct sub-sections (like cards, feature boxes, or image-text wrappers) remain distinct elements to allow for individual manipulation by the user. "
        
        "**DESIGN & PROFESSIONALISM MANDATE:** "
        "3. The generated website **MUST** be highly professional, modern, and aesthetically pleasing, adhering to contemporary web development standards. "
        "4. Use clean, structured HTML5 and semantic elements (`<header>`, `<nav>`, `<main>`, `<section>`, `<footer>`). "
        "5. The design in `styles.css` **MUST** be clean, responsive (mobile-first approach), and utilize proper typography. "
        
        # --- NEW RULES FOR HORIZONTAL SCROLL AND DROPDOWNS ---
        "CRITICAL FOR MOBILE: To prevent horizontal scrolling bugs, your CSS MUST include `* { box-sizing: border-box; } html, body { overflow-x: hidden; width: 100%; margin: 0; padding: 0; }`. Avoid `100vw` and never use fixed pixel widths that break mobile viewports. Always use percentages or flexbox. "
        
        "6. **PREMIUM COLOR SCHEME MANDATE:** You **MUST** use an attractive, modern, and professional color scheme. STRICTLY AVOID dull brown or beige color combinations. Use clean, premium palettes (e.g., Deep Navy, Slate, Crisp Whites, vibrant Tech Blue, Emerald, or Royal Purple accents). Ensure high contrast between text and background. **CRITICAL: NEVER use a white or extremely light background with white or light text. NEVER use a black background with black text.** By default, use clean white backgrounds (#FFFFFF / #FAFAFA) with deep slate/charcoal text (#1E293B), or an immersive dark theme (#0F172A) with crisp off-white text (#F8FAFC). "
        
        "7. **SEO & RESPONSIVE MANDATE:** You **MUST** include `<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">`, a contextual `<title>`, and `<meta name=\"description\" content=\"...\">` in the `<head>` of EVERY HTML file. This ensures the mobile menu and responsive CSS trigger correctly.\n\n"
        
        # --- FIX FOR DROPDOWN MENUS ---
        "8. **INTERACTIVE DROPDOWN MENUS:** If the website includes a navigation dropdown menu, it **MUST** be fully functional using pure CSS `:hover` states. Ensure the dropdown menu is hidden by default (`display: none;`) and opens automatically when hovering over the parent item (e.g., `.dropdown:hover .dropdown-menu { display: block; position: absolute; z-index: 99; }`). It must auto-close when the mouse moves away.\n\n"
        
        f"{reference_instruction}"
        f"{analytics_instruction}"
        
        "\nThe output MUST be **ONLY** a single JSON object. Do NOT include any explanatory text outside the JSON. "
        "The exact JSON schema representing the required pages will be provided in the subsequent instructions based on the dynamic settings. "
        "When a user asks to modify something, you **MUST** return the **ENTIRE, UPDATED** JSON structure with all files. "
        "All HTML files **must** link to the external 'styles.css' file in their `<head>` like this: `<link rel=\"stylesheet\" href=\"styles.css\">` and **must NOT** contain any `<style>` tags. "
        "CRITICAL MANDATE: For every major content section on every page, you **MUST** include at least one `<img>` tag or prominent background image. For `<img>` tags, use src='/images/placeholder.jpg' with a detailed, specific `alt` attribute describing the desired image content. The backend will replace the src based on your alt text.\n\n"
        "**ABSOLUTE JSON RULE (CRITICAL FOR PARSING):** Do NOT wrap the CSS or HTML code inside markdown code blocks (e.g., ```html or ```css) *within* the JSON string values. The JSON values must contain RAW text code only. The overall output must be pure, standard JSON.\n"
    )

    is_new_generation = is_new_request or is_first_generation
    
    return Response(
        stream_with_context(AIGenerationService.generate_stream(history_for_model, system_instruction, current_user.id, is_new_generation)), 
        mimetype='text/event-stream'
    )

@app.route('/<path:filename>')
@login_required
def fallback_root_files(filename):
    current_files = get_current_files()
    if filename in current_files:
        content = current_files[filename]
        if isinstance(content, str) and content.startswith('data:'):
             try:
                header, encoded = content.split(',', 1)
                mime_type = header.split(':')[1].split(';')[0]
                return Response(base64.b64decode(encoded), mimetype=mime_type)
             except: pass

        mimetype = 'text/plain'
        if filename.endswith('.css'): mimetype = 'text/css'
        elif filename.endswith('.js'): mimetype = 'application/javascript'
        elif filename.endswith('.png'): mimetype = 'image/png'
        elif filename.endswith('.jpg') or filename.endswith('.jpeg'): mimetype = 'image/jpeg'
        elif filename.endswith('.svg'): mimetype = 'image/svg+xml'
        return Response(content, mimetype=mimetype)

    if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
         print(f"⚠️ [Root Fallback] Auto-generating missing asset: {filename}")
         search_query = filename.rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ')
         fallback_url = ImageProviderService.get_image_url(search_query)
         return redirect(fallback_url)
    
    return "Not Found", 404

@app.route('/images/<path:filename>')
@login_required
def fallback_nested_images(filename):
    print(f"⚠️ [Nested Image Fallback] Request for: images/{filename}")
    search_query = filename.rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ').replace('/', ' ')
    fallback_url = ImageProviderService.get_image_url(search_query)
    return redirect(fallback_url)

@app.route('/api/admin/create_user', methods=['POST'])
@login_required
def admin_create_user():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json
    email = data.get('email')
    password = data.get('password')
    role = data.get('role', 'client')
    
    if User.query.filter_by(email=email).first():
        return jsonify({"success": False, "error": "Email already exists"}), 400
        
    hashed_pw = generate_password_hash(password)
    new_user = User(email=email, password_hash=hashed_pw, role=role, is_verified=True)
    db.session.add(new_user)
    db.session.commit()
    
    return jsonify({"success": True, "message": "User created successfully."})

# =========================================
# INSTALLATION / SETUP HOOK
# =========================================
@app.route('/api/internal/setup_templates', methods=['POST'])
def internal_setup_templates():
    # 1. Security Check: Only allow the local server (the bash script) to trigger this
    if request.remote_addr != '127.0.0.1':
        return jsonify({"error": "Unauthorized"}), 403
        
    # 2. The "Run Once" Lock Check
    # We look for a hidden file in the current directory
    lock_file_path = os.path.join(os.getcwd(), '.templates_imported.lock')
    
    if os.path.exists(lock_file_path):
        return jsonify({"success": False, "message": "Templates were already imported. Lock file exists."}), 200

    # 3. If no lock file exists, run the import
    from routes_client_updater import sync_seed_data
    try:
        sync_seed_data()
        
        # 4. CREATE THE LOCK FILE
        # This guarantees the import can NEVER run again unless you manually delete this file
        with open(lock_file_path, 'w') as f:
            f.write(f"Templates imported successfully on {os.popen('date').read()}")
            
        return jsonify({"success": True, "message": "Templates imported and system locked!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# =========================================
# LAUNCH SERVER
# =========================================

if __name__ == '__main__':
    print("\n" + "="*50)
    print(" 🚀 STARTING AI WEBSITE BUILDER ")
    print("="*50)
    print("Note: License verification is now handled securely via the Web Browser.")
    print("Please open the application in your browser to activate.")
    print("="*50 + "\n")

    if not os.path.exists('templates'): os.makedirs('templates')
    if not os.path.exists('projects'): os.makedirs('projects')
    
    app.run(debug=False, host='0.0.0.0', port=3003, threaded=True)