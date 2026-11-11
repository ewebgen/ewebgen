# ================================================================================================
# FIRST RUN: set the admin password
#
# Right after the license is activated (web or desktop), the app does NOT show a login screen.
# Instead the owner sets the admin password (entered twice) and is signed in straight away.
#
# "First run" = the built-in admin still has the factory password (ADMIN_DEFAULT_PASS / admin123).
# Installs where the admin already changed it are marked done automatically, and the setup page
# and API refuse to work once setup is done (so they can never be used to reset the password).
# ================================================================================================
import os
import uuid
import threading

from flask import request, jsonify, redirect, render_template_string, session
from flask_login import login_user
from werkzeug.security import generate_password_hash, check_password_hash

from database_setup import app, db, User, DATA_DIR

MARKER = os.path.join(DATA_DIR, '.admin_setup_done')
MIN_LENGTH = 8
_lock = threading.Lock()
_state = {"pending": None}   # cached: None = unknown, True = setup needed, False = done


def _default_passwords():
    return {os.getenv('ADMIN_DEFAULT_PASS', 'admin123'), 'admin123'}


def _primary_admin():
    return User.query.filter_by(role='admin').order_by(User.id.asc()).first()


def _mark_done():
    _state["pending"] = False
    try:
        with open(MARKER, 'w', encoding='utf-8') as f:
            f.write('admin password set\n')
    except OSError:
        pass


def admin_setup_pending():
    """True while the primary admin still uses the factory password (checked once, then cached)."""
    if _state["pending"] is False or os.path.exists(MARKER):
        _state["pending"] = False
        return False
    if _state["pending"] is True:
        return True
    with _lock:
        if _state["pending"] is not None:
            return _state["pending"]
        admin = _primary_admin()
        if not admin:
            _state["pending"] = False
            return False
        if any(check_password_hash(admin.password_hash, p) for p in _default_passwords()):
            _state["pending"] = True
            return True
        _mark_done()           # admin already has their own password: never ask
        return False


ALLOWED_DURING_SETUP = {
    'first_run_setup_page', 'first_run_setup_api',
    'activate_page', 'api_activate_license', 'api_free_plan_status', 'api_activate_free_plan',
    'api_license_state', 'api_license_recheck', 'static', 'internal_setup_templates',
}


@app.before_request
def require_admin_password_setup():
    """Runs after the license guard: until the admin password is set, everything goes to /setup-admin."""
    if request.endpoint in ALLOWED_DURING_SETUP or request.endpoint is None:
        return
    try:
        pending = admin_setup_pending()
    except Exception:
        return
    if not pending:
        return
    wants_json = (request.path.startswith('/api/') or request.method != 'GET' or request.is_json or
                  'application/json' in (request.headers.get('Accept') or ''))
    if wants_json:
        return jsonify({"success": False, "authenticated": False, "needs_admin_setup": True,
                        "error": "Please set the admin password first.", "redirect": "/setup-admin"}), 403
    return redirect('/setup-admin')


SETUP_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Set Admin Password - eWebGen</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg,#eef2ff,#f8fafc); display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 16px; }
        .box { background: #fff; padding: 32px; border-radius: 14px; box-shadow: 0 18px 45px rgba(15,23,42,0.12); width: 100%; max-width: 440px; border-top: 5px solid #6610f2; }
        h2 { color: #1e293b; margin: 0 0 6px; text-align: center; }
        p.sub { color: #64748b; font-size: 14.5px; margin: 0 0 20px; line-height: 1.5; text-align: center; }
        .user { display: flex; justify-content: center; margin-bottom: 18px; }
        .user span { background: #f1f5f9; color: #334155; border-radius: 999px; padding: 6px 14px; font-size: 13px; }
        label { display: block; font-size: 13px; font-weight: 700; color: #334155; margin: 12px 0 5px; }
        .pw { position: relative; }
        input { width: 100%; padding: 12px 44px 12px 12px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 15px; }
        input:focus { border-color: #6610f2; outline: none; box-shadow: 0 0 0 3px rgba(102,16,242,.15); }
        .eye { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); background: none; border: none; cursor: pointer; font-size: 16px; color: #64748b; padding: 6px; }
        .meter { height: 6px; background: #e2e8f0; border-radius: 4px; margin-top: 8px; overflow: hidden; }
        .meter i { display: block; height: 100%; width: 0; background: #dc2626; transition: width .2s, background .2s; }
        .hint { font-size: 12px; color: #64748b; margin-top: 6px; min-height: 16px; }
        .match { font-size: 12px; margin-top: 6px; min-height: 16px; }
        button.go { width: 100%; padding: 13px; color: #fff; border: none; border-radius: 8px; font-size: 15.5px; cursor: pointer; margin-top: 20px; font-weight: bold; background: #6610f2; }
        button.go:hover { background: #520dc2; }
        button.go:disabled { background: #94a3b8; cursor: not-allowed; }
        #msg { margin-top: 14px; font-size: 14px; font-weight: bold; min-height: 20px; text-align: center; }
    </style>
</head>
<body>
    <form class="box" id="f" autocomplete="on" onsubmit="event.preventDefault(); save();">
        <h2>🔐 Set your admin password</h2>
        <p class="sub">Welcome to eWebGen! Create the password for the administrator account. You'll use it to sign in next time.</p>
        <div class="user"><span>Admin login: <b>{{ username }}</b>{% if email %} &nbsp;·&nbsp; {{ email }}{% endif %}</span></div>
        <input type="text" name="username" value="{{ username }}" autocomplete="username" hidden>

        <label for="p1">New password</label>
        <div class="pw">
            <input type="password" id="p1" autocomplete="new-password" minlength="{{ min_len }}" required autofocus>
            <button type="button" class="eye" onclick="toggle('p1', this)" aria-label="Show password">👁</button>
        </div>
        <div class="meter"><i id="bar"></i></div>
        <div class="hint" id="hint">At least {{ min_len }} characters. Mix letters, numbers and symbols.</div>

        <label for="p2">Confirm password</label>
        <div class="pw">
            <input type="password" id="p2" autocomplete="new-password" required>
            <button type="button" class="eye" onclick="toggle('p2', this)" aria-label="Show password">👁</button>
        </div>
        <div class="match" id="match"></div>

        <button class="go" id="btn" type="submit">Save password &amp; continue</button>
        <div id="msg"></div>
    </form>
    <script>
        const MIN = {{ min_len }};
        const $ = id => document.getElementById(id);
        function toggle(id, b) { const i = $(id); i.type = i.type === 'password' ? 'text' : 'password'; b.textContent = i.type === 'password' ? '👁' : '🙈'; }
        function score(p) {
            let s = 0; if (p.length >= MIN) s++; if (p.length >= 12) s++;
            if (/[a-z]/.test(p) && /[A-Z]/.test(p)) s++; if (/\\d/.test(p)) s++; if (/[^A-Za-z0-9]/.test(p)) s++;
            return s;
        }
        function update() {
            const p = $('p1').value, q = $('p2').value, s = score(p);
            const colors = ['#dc2626', '#dc2626', '#f59e0b', '#f59e0b', '#16a34a', '#16a34a'];
            const words = ['Too short', 'Weak', 'Fair', 'Good', 'Strong', 'Very strong'];
            $('bar').style.width = (p ? Math.max(10, s * 20) : 0) + '%';
            $('bar').style.background = colors[s];
            $('hint').textContent = p ? (p.length < MIN ? `At least ${MIN} characters (${p.length}/${MIN})` : words[s]) : `At least ${MIN} characters. Mix letters, numbers and symbols.`;
            if (!q) { $('match').textContent = ''; }
            else if (p === q) { $('match').textContent = '✓ Passwords match'; $('match').style.color = '#16a34a'; }
            else { $('match').textContent = '✗ Passwords do not match'; $('match').style.color = '#dc2626'; }
        }
        ['p1', 'p2'].forEach(id => $(id).addEventListener('input', () => { $('msg').textContent = ''; update(); }));
        function fail(t) { $('msg').style.color = '#dc2626'; $('msg').textContent = '❌ ' + t; $('btn').disabled = false; $('btn').textContent = 'Save password & continue'; }
        function save() {
            const p = $('p1').value, q = $('p2').value;
            if (p.length < MIN) return fail(`Password must be at least ${MIN} characters.`);
            if (p !== q) return fail('The two passwords do not match.');
            $('btn').disabled = true; $('btn').textContent = 'Saving...'; $('msg').textContent = '';
            fetch('/api/setup/admin_password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: p, confirm: q }) })
                .then(r => r.json()).then(d => {
                    if (!d.success) return fail(d.error || 'Could not save the password.');
                    try { sessionStorage.setItem('eweb_just_logged_in', '1'); sessionStorage.removeItem('eweb_welcome_prompt_done'); } catch (e) {}
                    $('msg').style.color = '#16a34a'; $('msg').textContent = '✅ Password saved. Opening eWebGen...';
                    setTimeout(() => location.href = '/', 700);
                }).catch(() => fail('Network error. Please try again.'));
        }
    </script>
</body>
</html>
"""


@app.route('/setup-admin', methods=['GET'])
def first_run_setup_page():
    if not admin_setup_pending():
        return redirect('/')
    admin = _primary_admin()
    email = admin.email if admin and admin.email and admin.email != 'admin@example.com' else ''
    return render_template_string(SETUP_HTML, username=(admin.username if admin else 'admin'),
                                  email=email, min_len=MIN_LENGTH)


@app.route('/api/setup/admin_password', methods=['POST'])
def first_run_setup_api():
    if not admin_setup_pending():
        return jsonify({"success": False, "error": "The admin password has already been set."}), 403
    data = request.json or {}
    pw = str(data.get('password') or '')
    confirm = str(data.get('confirm') or '')
    if len(pw) < MIN_LENGTH:
        return jsonify({"success": False, "error": f"Password must be at least {MIN_LENGTH} characters."}), 400
    if pw != confirm:
        return jsonify({"success": False, "error": "The two passwords do not match."}), 400
    if pw in _default_passwords():
        return jsonify({"success": False, "error": "Please choose a different password than the default one."}), 400

    with _lock:
        admin = _primary_admin()
        if not admin:
            return jsonify({"success": False, "error": "No admin account found."}), 500
        admin.password_hash = generate_password_hash(pw)
        if hasattr(admin, 'force_password_reset'):
            admin.force_password_reset = False
        admin.is_verified = True
        # Sign the admin in right away (same single-session token as a normal login)
        token = uuid.uuid4().hex
        admin.session_token = token
        db.session.commit()
        session['session_token'] = token
        login_user(admin, remember=True)
        _mark_done()
    return jsonify({"success": True})
