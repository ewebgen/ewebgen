import os
import io
import json
import re
import zipfile
import uuid
import shutil
import base64
import tempfile
import requests
import urllib3
from urllib.parse import unquote
from werkzeug.utils import secure_filename

from datetime import datetime, timezone, timedelta
from flask import request, jsonify, send_file, Response
from flask_login import login_required, current_user

# Disable harmless SSL warnings when fetching local/external images
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import Shared Resources
from database_setup import app, db, User, SystemSettings, PROJECTS_DIR, UserClipboard, UserActivityLog, UNSAVED_DIR

# Import core dependencies
from services_core import (
    get_current_files, 
    save_new_state, 
    get_user_session_data, 
    ensure_links_are_proxied, 
    remove_proxy_links, 
    get_user_project_dir, 
    localize_html_images, 
    download_image_server_side, 
    DeploymentService,
    PHPGeneratorService,
    apply_google_analytics,
    FTPEncryptionService,
    is_safe_url
)

# =======================================================
# SECURITY HELPER: SAFE PROJECT NAMES
# =======================================================
def sanitize_project_name(name):
    """
    Safely sanitizes a project name to prevent path traversal 
    while preserving spaces and standard characters for legacy projects.
    """
    if not name or not isinstance(name, str): 
        return "Unnamed_Project"
    
    # Strip out directory traversal dots and dangerous path characters
    name = name.replace('..', '')
    name = re.sub(r'[\\/:\*\?"<>\|]', '', name)
    return name.strip()

# =======================================================
# SECURITY HELPER: DECRYPTION FOR SYSTEM SETTINGS
# =======================================================
def decrypt_sensitive_data(val):
    if not val: return val
    try:
        dec = FTPEncryptionService.decrypt_credentials(val)
        if dec and 'sec_val' in dec: return dec['sec_val']
    except: pass
    return val # Fallback to plain text if not encrypted or legacy

# ========================================================
# GOAL 1 & 2: UI STERILIZATION & MOBILE FIX ENGINE
# ========================================================

def strip_ui_elements(content):
    """
    Surgically removes editor-only UI artifacts (like '+' buttons) 
    and injects mobile overflow protection.
    """
    if not content or not isinstance(content, str): return content
    
    # Remove UI artifacts
    patterns = [
        r'<div[^>]*class=["\'][^"\']*eweb-add-section-btn[^"\']*["\'][^>]*>.*?</div>',
        r'<div[^>]*class=["\'][^"\']*section-action-bar[^"\']*["\'][^>]*>.*?</div>',
        r'<button[^>]*class=["\'][^"\']*inline-add-widget-btn[^"\']*["\'][^>]*>.*?</button>',
        r'<div[^>]*class=["\'][^"\']*bg-control-container[^"\']*["\'][^>]*>.*?</div>',
        r'<div[^>]*class=["\'][^"\']*eweb-drop-indicator[^"\']*["\'][^>]*>.*?</div>'
    ]
    for p in patterns:
        content = re.sub(p, '', content, flags=re.IGNORECASE | re.DOTALL)
    
    # Inject Mobile Scrollbar Fix (Goal 2)
    if '<head>' in content and 'overflow-x: hidden' not in content:
        mobile_fix = '<style>body, html { overflow-x: hidden !important; width: 100% !important; }</style>'
        content = content.replace('<head>', '<head>' + mobile_fix)
        
    return content

# ========================================================
# STORAGE & PROJECT UTILS
# ========================================================

def check_storage_limit(user_id):
    user = db.session.get(User, user_id)
    if not user or not user.user_type: 
        return True 
        
    limit_mb = user.user_type.storage_limit_mb
    if limit_mb == -1: 
        return True 
        
    total_size = 0
    user_dir = os.path.join(PROJECTS_DIR, str(user_id))
    if os.path.exists(user_dir):
        for dirpath, dirnames, filenames in os.walk(user_dir):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
                    
    current_mb = total_size / (1024 * 1024)
    return current_mb < limit_mb

# ========================================================
# PROJECT MANAGEMENT ENDPOINTS
# ========================================================

@app.route('/create_page', methods=['POST'])
@login_required
def create_page():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": f"Storage limit reached. Please upgrade your plan."}), 403

    req_data = request.json
    base_page = req_data.get('base_page')
    new_page_name = req_data.get('new_page_name')

    if not base_page or not new_page_name: return jsonify({"success": False, "error": "Missing base page or new page name."}), 400
    current_files = get_current_files()
    if not current_files: return jsonify({"success": False, "error": "No project loaded."}), 400
    if base_page not in current_files: return jsonify({"success": False, "error": "Base page not found."}), 404

    clean_name = new_page_name.strip().lower()
    if clean_name.endswith('.html'): clean_name = clean_name[:-5]
    clean_name = re.sub(r'[^a-z0-9\-]', '-', clean_name)
    clean_name = f"{clean_name}.html"
    
    if clean_name in current_files: return jsonify({"success": False, "error": f"Page '{clean_name}' already exists."}), 400

    new_content = current_files[base_page]
    if isinstance(new_content, str) and not new_content.startswith('data:'):
        title_match = re.search(r'<title>(.*?)</title>', new_content, re.IGNORECASE)
        if title_match:
             new_title = clean_name.replace('.html', '').replace('-', ' ').title()
             new_content = new_content.replace(title_match.group(0), f"<title>{new_title}</title>")
    
    new_state = current_files.copy()
    new_state[clean_name] = new_content
    save_new_state(new_state)
    
    session_data = get_user_session_data()
    active_project = session_data.get('active_project') if session_data else None
    proj_name = active_project if active_project else 'Unsaved Project'

    try:
        log = UserActivityLog(
            user_id=current_user.id,
            action_type='project_edited',
            description=f"Created a new page: '{clean_name}' in project '{proj_name}'"
        )
        db.session.add(log)
        db.session.commit()
    except: pass
    
    return jsonify({"success": True, "message": f"Page '{clean_name}' created successfully.", "new_filename": clean_name})

@app.route('/delete_page', methods=['POST'])
@login_required
def delete_page():
    req_data = request.json
    page_to_delete = req_data.get('page_name')
    
    if not page_to_delete: return jsonify({"success": False, "error": "Missing page name."}), 400
    
    page_to_delete = os.path.basename(str(page_to_delete))
    current_files = get_current_files() or {}

    # FIX: The browser may hold pages the server session lost (e.g. after a restart).
    # Only refuse when we can positively see this is the last page.
    html_pages = [f for f in current_files.keys() if f.endswith(('.html', '.php'))]
    if page_to_delete in current_files and len(html_pages) <= 1:
        return jsonify({"success": False, "error": "Cannot delete the only page in the project."}), 400

    if page_to_delete in current_files:
        new_state = current_files.copy()
        del new_state[page_to_delete]
        save_new_state(new_state)
    
    session_data = get_user_session_data()
    active_project = session_data.get('active_project') if session_data else None
    proj_name = active_project if active_project else 'Unsaved Project'

    # FIX: Also remove the page from the saved project file so it does not come back on reload.
    if active_project:
        try:
            proj_file = os.path.join(get_user_project_dir(), f"{sanitize_project_name(active_project)}.json")
            if os.path.exists(proj_file):
                with open(proj_file, 'r', encoding='utf-8') as f: proj_data = json.load(f)
                if page_to_delete in proj_data.get('files', {}):
                    del proj_data['files'][page_to_delete]
                    with open(proj_file, 'w', encoding='utf-8') as f: json.dump(proj_data, f, indent=4)
        except Exception as e:
            print(f"delete_page: could not update saved project: {e}", flush=True)

    try:
        log = UserActivityLog(
            user_id=current_user.id,
            action_type='project_edited',
            description=f"Deleted page: '{page_to_delete}' from project '{proj_name}'"
        )
        db.session.add(log)
        db.session.commit()
    except: pass
    
    return jsonify({"success": True, "message": f"Page '{page_to_delete}' deleted successfully."})

@app.route('/duplicate_page', methods=['POST'])
@login_required
def duplicate_page():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": "Storage limit reached. Please upgrade your plan."}), 403

    req_data = request.json
    page_to_duplicate = req_data.get('page_name')
    new_page_name = req_data.get('new_page_name')

    if not page_to_duplicate or not new_page_name:
        return jsonify({"success": False, "error": "Missing target page or new page name."}), 400

    current_files = get_current_files()
    if not current_files or page_to_duplicate not in current_files:
        return jsonify({"success": False, "error": "Source page not found."}), 404

    clean_name = new_page_name.strip().lower()
    if clean_name.endswith('.html'): clean_name = clean_name[:-5]
    clean_name = re.sub(r'[^a-z0-9\-]', '-', clean_name) + ".html"

    if clean_name in current_files:
        return jsonify({"success": False, "error": f"Page '{clean_name}' already exists."}), 400

    content = current_files[page_to_duplicate]
    if isinstance(content, str) and not content.startswith('data:'):
        title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
        if title_match:
            new_title = clean_name.replace('.html', '').replace('-', ' ').title()
            content = content.replace(title_match.group(0), f"<title>{new_title}</title>")

    new_state = current_files.copy()
    new_state[clean_name] = content
    save_new_state(new_state)

    return jsonify({"success": True, "message": f"Page duplicated as '{clean_name}'", "new_filename": clean_name})

@app.route('/get_page_code', methods=['POST'])
@login_required
def get_page_code():
    req_data = request.json
    page_name = req_data.get('page_name')
    if not page_name: return jsonify({"success": False, "error": "Missing page name"}), 400
    
    current_files = get_current_files()
    if not current_files or page_name not in current_files:
        return jsonify({"success": False, "error": "Page not found in active workspace"}), 404
        
    return jsonify({"success": True, "content": current_files[page_name]})

@app.route('/api/check_project_limit', methods=['GET'])
@login_required
def check_project_limit():
    user_dir = get_user_project_dir()
    current_count = len([f for f in os.listdir(user_dir) if f.endswith('.json')])
    
    limit = 1000 
    if current_user.user_type:
        limit = current_user.user_type.project_limit
    
    is_allowed = True if limit == -1 else current_count < limit
    
    return jsonify({
        "allowed": is_allowed,
        "current": current_count,
        "limit": limit
    })

@app.route('/list_projects', methods=['GET'])
@login_required
def list_projects():
    try:
        user_dir = get_user_project_dir()
        if not os.path.exists(user_dir):
            return jsonify({"projects": []})
            
        files = [f for f in os.listdir(user_dir) if f.endswith('.json')]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(user_dir, x)), reverse=True)
        
        projects = [f.replace('.json', '') for f in files]
        return jsonify({"projects": projects})
    except Exception as e:
        print(f"Error listing projects: {e}")
        return jsonify({"projects": []})

@app.route('/save_project', methods=['POST'])
@login_required
def save_project():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": f"Storage limit reached. Please upgrade your plan."}), 403

    req_data = request.json
    raw_project_name = req_data.get('project_name')
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    if not raw_project_name: return jsonify({"success": False, "error": "Missing project_name"}), 400
    project_name = sanitize_project_name(raw_project_name)
    if not project_name: return jsonify({"success": False, "error": "Invalid project name format"}), 400
    
    user_dir = get_user_project_dir()
    file_path = os.path.join(user_dir, f"{project_name}.json")
    
    if not os.path.exists(file_path):
        current_count = len([f for f in os.listdir(user_dir) if f.endswith('.json')])
        limit = 1000
        if current_user.user_type: limit = current_user.user_type.project_limit
        if limit != -1 and current_count >= limit:
            return jsonify({"success": False, "error": f"Project limit reached ({limit}). Upgrade your plan."}), 403

    current_files = get_current_files()
    if not current_files: return jsonify({"success": False, "error": "No website to save."}), 400
    
    try:
        sanitized_files = {k: strip_ui_elements(v) if isinstance(v, str) else v for k, v in current_files.items()}
        
        store_locally = False
        with app.app_context():
            settings = SystemSettings.query.first()
            if settings and settings.store_images_locally:
                store_locally = True

        if store_locally:
            localized_files = sanitized_files.copy()
            for fname, content in localized_files.items():
                if fname.endswith(('.html', '.php')) and isinstance(content, str) and not content.startswith('data:'):
                    localized_files[fname] = localize_html_images(content)
            
            sanitized_files = localized_files
            save_new_state(sanitized_files) 
        
        with open(file_path, 'w', encoding='utf-8') as f: json.dump({"files": sanitized_files}, f, indent=4)
        
        session_data = get_user_session_data()
        if session_data:
            session_data['active_project'] = project_name
            
        try:
            log = UserActivityLog(
                user_id=current_user.id,
                action_type='project_saved',
                description=f"Saved project: '{project_name}'"
            )
            db.session.add(log)
            db.session.commit()
        except: pass
            
        return jsonify({"success": True, "message": f"Project '{project_name}' saved successfully."})
    except Exception as e: 
        return jsonify({"success": False, "error": "Failed to save project due to a server error."}), 500

@app.route('/rename_project', methods=['POST'])
@login_required
def rename_project():
    req_data = request.json
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    old_name = sanitize_project_name(req_data.get('old_name', ''))
    new_name = sanitize_project_name(req_data.get('new_name', ''))

    if not old_name or not new_name:
        return jsonify({"success": False, "error": "Missing or invalid old/new name"}), 400

    user_dir = get_user_project_dir()
    old_path = os.path.join(user_dir, f"{old_name}.json")
    new_path = os.path.join(user_dir, f"{new_name}.json")

    if not os.path.exists(old_path):
        return jsonify({"success": False, "error": "Project not found"}), 404

    if os.path.exists(new_path):
        return jsonify({"success": False, "error": "A project with the new name already exists"}), 400

    try:
        os.rename(old_path, new_path)
        session_data = get_user_session_data()
        if session_data and session_data.get('active_project') == old_name:
            session_data['active_project'] = new_name
        return jsonify({"success": True, "message": f"Project renamed to '{new_name}'"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Rename failed: {str(e)}"}), 500

@app.route('/load_project', methods=['POST'])
@login_required
def load_project():
    req_data = request.json
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    project_name = sanitize_project_name(req_data.get('project_name', ''))
    if not project_name: return jsonify({"success": False, "error": "Missing or invalid project_name"}), 400
    
    user_dir = get_user_project_dir()
    file_path = os.path.join(user_dir, f"{project_name}.json")
    
    if not os.path.exists(file_path): 
        return jsonify({"success": False, "error": f"Project '{project_name}' not found."}), 404
        
    try:
        with open(file_path, 'r', encoding='utf-8') as f: save_data = json.load(f)
        loaded_files = save_data.get('files', {})
        if not loaded_files: return jsonify({"success": False, "error": "Loaded project file is empty or corrupted."}), 500

        session_data = get_user_session_data()
        session_data['history_stack'] = [loaded_files]
        session_data['redo_stack'] = []
        session_data['active_project'] = project_name 
        
        current_page = "index.html"
        if "index.html" not in loaded_files:
            if "index.php" in loaded_files:
                current_page = "index.php"
            else:
                for f in loaded_files:
                    if f.endswith(('.html', '.php')):
                        current_page = f
                        break

        return jsonify({"success": True, "message": f"Project '{project_name}' loaded.", "files": loaded_files, "current_page": current_page})
    except Exception as e: 
        return jsonify({"success": False, "error": "Failed to load project."}), 500

@app.route('/delete_project', methods=['POST'])
@login_required
def delete_project():
    req_data = request.json
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    project_name = sanitize_project_name(req_data.get('project_name', ''))
    if not project_name: return jsonify({"success": False, "error": "Missing or invalid project_name"}), 400
    
    user_dir = get_user_project_dir()
    file_path = os.path.join(user_dir, f"{project_name}.json")
    if not os.path.exists(file_path): return jsonify({"success": False, "error": "Project not found."}), 404
    try:
        os.remove(file_path)
        session_data = get_user_session_data()
        if session_data and session_data.get('active_project') == project_name:
            session_data['active_project'] = None
        return jsonify({"success": True, "message": f"Project '{project_name}' deleted."})
    except Exception as e: return jsonify({"success": False, "error": "Failed to delete project."}), 500

@app.route('/save', methods=['POST'])
@login_required
def save_edits():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": f"Storage limit reached. Please upgrade your plan."}), 403

    req_data = request.json
    filename = req_data.get('filename')
    new_body_content = strip_ui_elements(req_data.get('new_body_content', ''))
    
    if not filename or not new_body_content: return jsonify({"success": False, "error": "Missing filename or new_body_content."}), 400
    current_files = get_current_files()
    if not current_files: return jsonify({"success": False, "error": "No website currently loaded."}), 400
    original_html = current_files.get(filename)
    if not original_html: return jsonify({"success": False, "error": f"File {filename} not found."}), 404
        
    if filename.endswith('.php') or re.search(r'<!DOCTYPE html>', new_body_content, re.IGNORECASE) or re.search(r'<html', new_body_content, re.IGNORECASE):
        updated_html = new_body_content
    else:
        head_match = re.search(r'(<head>[\s\S]*?<\/head>)', original_html, re.IGNORECASE | re.DOTALL)
        body_open_match = re.search(r'(<body[^>]*>)', original_html, re.IGNORECASE | re.DOTALL)
        original_head = head_match.group(1) if head_match else '<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>AI Website</title><link rel="stylesheet" href="styles.css"></head>'
        original_body_tag = body_open_match.group(1) if body_open_match else '<body>'
        updated_html = f"<!DOCTYPE html><html lang=\"en\">\n{original_head}\n{original_body_tag}\n{new_body_content}\n</body>\n</html>"
    
    new_state = current_files.copy()
    new_state[filename] = ensure_links_are_proxied(updated_html, list(current_files.keys()))
    save_new_state(new_state)
    
    session_data = get_user_session_data()
    active_project = session_data.get('active_project')
    
    if active_project:
        # SECURITY FIX: Safely retrieve active project from session
        active_project = sanitize_project_name(active_project)
        user_dir = get_user_project_dir()
        file_path = os.path.join(user_dir, f"{active_project}.json")
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump({"files": new_state}, f, indent=4)
        except Exception: pass

    proj_name = active_project if active_project else 'Unsaved Project'
    try:
        log = UserActivityLog(
            user_id=current_user.id,
            action_type='project_edited',
            description=f"Manually edited file: '{filename}' in project '{proj_name}'"
        )
        db.session.add(log)
        db.session.commit()
    except: pass

    return jsonify({"success": True, "saved_content": new_state[filename]})

@app.route('/save_css', methods=['POST'])
@login_required
def save_css_edits():
    req_data = request.json
    new_css_content = req_data.get('new_css_content')
    if new_css_content is None: return jsonify({"success": False, "error": "Missing new_css_content."}), 400
    current_files = get_current_files()
    if not current_files: return jsonify({"success": False, "error": "No website currently loaded."}), 400
    new_state = current_files.copy()
    new_state['styles.css'] = new_css_content
    save_new_state(new_state)
    
    session_data = get_user_session_data()
    active_project = session_data.get('active_project')
    if active_project:
        # SECURITY FIX: Safely retrieve active project from session
        active_project = sanitize_project_name(active_project)
        user_dir = get_user_project_dir()
        file_path = os.path.join(user_dir, f"{active_project}.json")
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump({"files": new_state}, f, indent=4)
        except: pass
        
    return jsonify({"success": True})

@app.route('/api/clipboard/save', methods=['POST'])
@login_required
def clipboard_save():
    req_data = request.json
    content = req_data.get('content')
    if not content: return jsonify({"success": False, "error": "No content provided."}), 400
    UserClipboard.query.filter_by(user_id=current_user.id).delete()
    new_clip = UserClipboard(user_id=current_user.id, content=content)
    db.session.add(new_clip)
    db.session.commit()
    return jsonify({"success": True, "message": "Section copied to clipboard."})

@app.route('/api/clipboard/read', methods=['GET'])
@login_required
def clipboard_read():
    clip = UserClipboard.query.filter_by(user_id=current_user.id).first()
    if not clip: return jsonify({"success": False, "error": "Clipboard is empty."}), 404
    return jsonify({"success": True, "content": clip.content})

@app.route('/undo', methods=['POST'])
@login_required
def undo_action():
    session_data = get_user_session_data()
    if not session_data or len(session_data['history_stack']) < 2: return jsonify({"success": False, "error": "No previous state to undo."}), 400
    current_state = session_data['history_stack'].pop()
    session_data['redo_stack'].append(current_state)
    reverted_files = get_current_files()
    current_page = next((f for f in reverted_files if f.endswith('.html')), "index.html")
    return jsonify({"success": True, "pages": reverted_files, "current_page": current_page})

@app.route('/redo', methods=['POST'])
@login_required
def redo_action():
    session_data = get_user_session_data()
    if not session_data or not session_data['redo_stack']: return jsonify({"success": False, "error": "No state to redo."}), 400
    state_to_redo = session_data['redo_stack'].pop()
    session_data['history_stack'].append(state_to_redo)
    redone_files = get_current_files()
    current_page = next((f for f in redone_files if f.endswith('.html')), "index.html")
    return jsonify({"success": True, "pages": redone_files, "current_page": current_page})

# =====================================================================
# HISTORY & UNSAVED SITES ENGINE
# =====================================================================

@app.route('/api/user/history', methods=['GET'])
@login_required
def get_user_history():
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    try:
        old_logs = UserActivityLog.query.filter(
            UserActivityLog.user_id == current_user.id,
            UserActivityLog.timestamp < thirty_days_ago
        ).all()
        for log in old_logs:
            if log.reference_id:
                unsaved_filename = f"unsaved_{current_user.id}_{log.reference_id}.zip"
                unsaved_path = os.path.join(app.config.get('UNSAVED_DIR', UNSAVED_DIR), unsaved_filename)
                if os.path.exists(unsaved_path):
                    try: os.remove(unsaved_path)
                    except: pass
            db.session.delete(log)
        if old_logs: db.session.commit()
    except Exception as e:
        db.session.rollback()

    logs = UserActivityLog.query.filter_by(user_id=current_user.id).order_by(UserActivityLog.timestamp.desc()).limit(100).all()
    history = []
    for log in logs:
        history.append({
            "id": log.id,
            "action_type": log.action_type,
            "description": log.description,
            "reference_id": log.reference_id,
            "date": log.timestamp.strftime("%Y-%m-%d %H:%M")
        })
    return jsonify({"success": True, "history": history})

@app.route('/api/user/history/load/<reference_id>', methods=['POST'])
@login_required
def load_history_project(reference_id):
    # Retrieve reference from database ensuring it belongs to current user
    log = UserActivityLog.query.filter_by(user_id=current_user.id, reference_id=reference_id).first()
    if not log: return jsonify({"success": False, "error": "History record not found."}), 404
        
    unsaved_filename = f"unsaved_{current_user.id}_{reference_id}.zip"
    unsaved_path = os.path.join(app.config.get('UNSAVED_DIR', UNSAVED_DIR), unsaved_filename)
    if not os.path.exists(unsaved_path):
        return jsonify({"success": False, "error": "Website files expired (older than 30 days)."}), 404
        
    try:
        loaded_files = {}
        with zipfile.ZipFile(unsaved_path, 'r') as zipf:
            for filename in zipf.namelist():
                if filename.startswith('images/'): continue 
                content = zipf.read(filename)
                try: loaded_files[filename] = content.decode('utf-8')
                except UnicodeDecodeError:
                    encoded = base64.b64encode(content).decode('utf-8')
                    mime_type = 'application/octet-stream'
                    if filename.endswith('.png'): mime_type = 'image/png'
                    elif filename.endswith(('.jpg', '.jpeg')): mime_type = 'image/jpeg'
                    loaded_files[filename] = f"data:{mime_type};base64,{encoded}"

        session_data = get_user_session_data()
        session_data['history_stack'] = [loaded_files]
        session_data['redo_stack'] = []
        session_data['active_project'] = None 
        
        current_page = "index.html"
        if "index.html" not in loaded_files:
            for f in loaded_files:
                if f.endswith(('.html', '.php')):
                    current_page = f
                    break
        return jsonify({"success": True, "message": "Historical site loaded.", "files": loaded_files, "current_page": current_page})
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500

# =====================================================================
# ZIP PROJECT IMPORTER & JSON EXPORT/IMPORT
# =====================================================================

@app.route('/import_zip', methods=['POST'])
@login_required
def import_zip_project():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": "Storage limit reached. Please upgrade your plan."}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No ZIP archive attached."}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "No file selected."}), 400
        
    if not file.filename.lower().endswith('.zip'):
        return jsonify({"success": False, "error": "Only .zip project files are permitted."}), 400

    project_name = request.form.get('project_name', 'Imported_Website')
    project_name = sanitize_project_name(re.sub(r'[^a-zA-Z0-9_\-\s]', '_', project_name))

    temp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(temp_dir, secure_filename(file.filename))
        file.save(zip_path)
        
        extracted_files = {}
        with zipfile.ZipFile(zip_path, 'r') as zipf:
            for item in zipf.namelist():
                if '__MACOSX' in item or item.endswith('/'): continue
                data = zipf.read(item)
                filename = os.path.basename(item)
                if not filename: continue
                
                if item.startswith('images/') or filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')):
                    ext = filename.split('.')[-1] if '.' in filename else 'png'
                    new_media_name = f"user_{current_user.id}_import_{uuid.uuid4().hex[:8]}.{ext}"
                    pub_path = os.path.join(app.config['UPLOAD_FOLDER'], new_media_name)
                    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                    with open(pub_path, 'wb') as mf: mf.write(data)
                    
                    extracted_files[f"__MEDIA_MAP__:{item}"] = f"/uploads/{new_media_name}"
                    extracted_files[f"__MEDIA_MAP__:images/{filename}"] = f"/uploads/{new_media_name}"
                    extracted_files[f"__MEDIA_MAP__:{filename}"] = f"/uploads/{new_media_name}"
                else:
                    try: extracted_files[filename] = data.decode('utf-8')
                    except UnicodeDecodeError:
                        encoded = base64.b64encode(data).decode('utf-8')
                        extracted_files[filename] = f"data:application/octet-stream;base64,{encoded}"

        cleaned_project = {}
        for fname, content in extracted_files.items():
            if fname.startswith('__MEDIA_MAP__:'): continue
            if isinstance(content, str) and fname.endswith(('.html', '.php', '.css')):
                for map_key, new_url in extracted_files.items():
                    if map_key.startswith('__MEDIA_MAP__:'):
                        orig_ref = map_key.split('__MEDIA_MAP__:')[1]
                        content = content.replace(orig_ref, new_url).replace(f"./{orig_ref}", new_url).replace(f"/{orig_ref}", new_url)
                cleaned_project[fname] = content
            else: cleaned_project[fname] = content

        if not cleaned_project: return jsonify({"success": False, "error": "Archive contained no readable web files."}), 400

        user_dir = get_user_project_dir()
        save_path = os.path.join(user_dir, f"{project_name}.json")
        counter, orig_name = 1, project_name
        while os.path.exists(save_path):
            project_name = f"{orig_name}_{counter}"
            save_path = os.path.join(user_dir, f"{project_name}.json")
            counter += 1

        with open(save_path, 'w', encoding='utf-8') as f: json.dump({"files": cleaned_project}, f, indent=4)

        session_data = get_user_session_data()
        session_data['history_stack'] = [cleaned_project]
        session_data['redo_stack'] = []
        session_data['active_project'] = project_name

        try:
            log = UserActivityLog(user_id=current_user.id, action_type='project_imported', description=f"Imported ZIP: '{project_name}'")
            db.session.add(log)
            db.session.commit()
        except: pass

        current_page = next((k for k in cleaned_project if k.endswith(('.html', '.php'))), "index.html")
        return jsonify({"success": True, "message": f"Project '{project_name}' imported!", "files": cleaned_project, "current_page": current_page})

    except Exception as e: return jsonify({"success": False, "error": f"Extraction failed: {str(e)}"}), 500
    finally: shutil.rmtree(temp_dir, ignore_errors=True)

@app.route('/export_project_json', methods=['GET'])
@login_required
def export_project_json():
    current_files = get_current_files()
    if not current_files: return jsonify({"error": "No project active"}), 400
    session_data = get_user_session_data()
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    p_name = sanitize_project_name(session_data.get('active_project', 'project_export'))
    if not p_name: p_name = "project_export"
    
    resp = jsonify({"project_name": p_name, "version": "3.9", "files": current_files})
    resp.headers['Content-Disposition'] = f'attachment; filename={p_name}.json'
    return resp

@app.route('/import_project_json', methods=['POST'])
@login_required
def import_project_json():
    if 'file' not in request.files: return jsonify({"success": False, "error": "No JSON file attached"}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.json'): return jsonify({"success": False, "error": "Only .json permitted"}), 400
    
    try:
        data = json.load(file)
        files = data.get('files')
        if not files or not isinstance(files, dict): return jsonify({"success": False, "error": "Invalid structure"}), 400
        
        # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
        p_name = sanitize_project_name(data.get('project_name', file.filename.replace('.json', '')))
        if not p_name: p_name = "Imported_JSON_Project"
        
        session_data = get_user_session_data()
        session_data['history_stack'] = [files]
        session_data['redo_stack'] = []
        session_data['active_project'] = p_name
        return jsonify({"success": True, "message": "JSON workspace loaded successfully!", "files": files})
    except Exception as e: return jsonify({"success": False, "error": "Malformed JSON payload"}), 400

# ========================================================
# EXPORT, DEPLOYMENT & FTP IMPORT ENDPOINTS 
# ========================================================

AJAX_FORM_SCRIPT = """
<script>
document.addEventListener('DOMContentLoaded', function() {
    const forms = document.querySelectorAll('form[action*="send_mail.php"]');
    forms.forEach(form => {
        form.addEventListener('submit', function(e) {
            e.preventDefault();
            const btn = form.querySelector('button[type="submit"]') || form.querySelector('input[type="submit"]');
            const origText = btn ? (btn.tagName === 'INPUT' ? btn.value : btn.textContent) : 'Submit';
            if (btn) {
                if (btn.tagName === 'INPUT') btn.value = 'Sending...';
                else btn.textContent = 'Sending...';
                btn.disabled = true;
            }
            
            let msgDiv = form.querySelector('.form-message');
            if (!msgDiv) {
                msgDiv = document.createElement('div');
                msgDiv.className = 'form-message';
                msgDiv.style.marginTop = '10px';
                msgDiv.style.fontWeight = 'bold';
                form.appendChild(msgDiv);
            }
            msgDiv.textContent = '';
            
            const searchParams = new URLSearchParams();
            const inputs = form.querySelectorAll('input, textarea, select');
            let hasData = false;
            
            inputs.forEach((input, index) => {
                if (input.type === 'submit' || input.type === 'button') return;
                let key = input.name || input.id || ('field_' + index);
                let val = input.value;
                if(val && val.trim().length > 0) {
                    searchParams.append(key, val);
                    hasData = true;
                }
            });
            
            if(!hasData) {
                msgDiv.textContent = 'Please fill out all required fields.';
                msgDiv.style.color = 'red';
                if (btn) {
                    if (btn.tagName === 'INPUT') btn.value = origText;
                    else btn.textContent = origText;
                    btn.disabled = false;
                }
                return;
            }
            
            fetch(form.action, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: searchParams.toString()
            })
            .then(async r => {
                const text = await r.text();
                let data = null;
                try {
                    const jsonStart = text.indexOf('{');
                    const jsonEnd = text.lastIndexOf('}');
                    if (jsonStart !== -1 && jsonEnd !== -1) {
                        data = JSON.parse(text.substring(jsonStart, jsonEnd + 1));
                    } else {
                        data = JSON.parse(text);
                    }
                } catch(err) { 
                    if (r.ok) data = {success: true, message: "Message sent successfully!"};
                    else throw new Error('Server returned invalid JSON.'); 
                }
                if (btn) {
                    if (btn.tagName === 'INPUT') btn.value = origText;
                    else btn.textContent = origText;
                    btn.disabled = false;
                }
                msgDiv.textContent = data.message || (data.success ? 'Message sent successfully!' : 'Failed to send.');
                msgDiv.style.color = data.success ? 'green' : 'red';
                if (data.success) form.reset();
            })
            .catch(err => {
                if (btn) {
                    if (btn.tagName === 'INPUT') btn.value = origText;
                    else btn.textContent = origText;
                    btn.disabled = false;
                }
                msgDiv.textContent = 'Error sending message. Please try again later.';
                msgDiv.style.color = 'red';
            });
        });
    });
});
</script>
"""

# --- SURGICAL THUMBNAIL PROXY UNWRAPPER ---
def get_local_upload_path(url_val):
    if not url_val: return None
    if 'url=' in url_val and ('/api/thumbnail' in url_val or 'thumbnail' in url_val):
        try:
            raw_encoded = url_val.split('url=')[1].split('&')[0]
            url_val = unquote(raw_encoded)
        except: pass

    clean_url = unquote(url_val.split('?')[0].split('#')[0])
    fname = None
    if '/uploads/' in clean_url: fname = clean_url.split('/uploads/')[-1]
    elif 'uploads/' in clean_url: fname = clean_url.split('uploads/')[-1]
    elif '/system_templates/' in clean_url: fname = os.path.join('system_templates', clean_url.split('/system_templates/')[-1])
    else: fname = os.path.basename(clean_url)

    if not fname: return None
    fname = fname.lstrip('\\/')
    base_app_dir = os.path.dirname(os.path.abspath(__file__))
    
    paths_to_try = [
        os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), fname),
        os.path.join(app.root_path, app.config.get('UPLOAD_FOLDER', 'uploads'), fname),
        os.path.join(base_app_dir, 'uploads', fname),
        os.path.join(os.path.dirname(base_app_dir), 'uploads', fname),
        os.path.join(os.getcwd(), 'uploads', fname),
        os.path.join(os.getcwd(), fname),
        fname 
    ]
    for p in paths_to_try:
        if os.path.exists(p) and os.path.isfile(p): return p
    return None

def process_and_localize_assets(content, downloaded_cache, fetch_and_save_callback):
    if not content or not isinstance(content, str): return content
    def replace_src_poster(match):
        try:
            original = match.group(0)
            attr = match.group(1).strip() 
            url_val = match.group(2).strip()
            new_filename = fetch_and_save_callback(url_val, downloaded_cache)
            if new_filename: return f'{attr}="images/{new_filename}"'
            return original
        except Exception: return match.group(0)

    def replace_css_url(match):
        try:
            original = match.group(0)
            quote_char = match.group(1)
            url_val = match.group(2).strip()
            new_filename = fetch_and_save_callback(url_val, downloaded_cache)
            if new_filename: return f'url({quote_char}images/{new_filename}{quote_char})'
            return original
        except Exception: return match.group(0)

    try:
        content = re.sub(r'(src|poster)\s*=\s*["\']([^"\']+)["\']', replace_src_poster, content, flags=re.IGNORECASE)
        content = re.sub(r'url\(([\'"]?)([^\'"\)]+)\1\)', replace_css_url, content, flags=re.IGNORECASE)
        return content
    except Exception: return content

def fix_form_actions(html_content):
    if not html_content or not isinstance(html_content, str): return html_content
    def replace_form(match):
        try:
            form_tag = match.group(0)
            if 'action=' not in form_tag.lower() or 'action="#"' in form_tag or "action='#'" in form_tag:
                form_tag = re.sub(r'action=["\'][^"\']*["\']', '', form_tag, flags=re.IGNORECASE)
                return form_tag.replace('<form', '<form action="send_mail.php"', 1)
            return form_tag
        except Exception: return match.group(0)
    return re.sub(r'<form[^>]*>', replace_form, html_content, flags=re.IGNORECASE)

def fix_form_scripts(html_content):
    if not html_content or not isinstance(html_content, str): return html_content
    def script_replacer(match):
        script_body = match.group(0)
        if ('send_mail.php' in script_body or 'php-contact-form' in script_body) and 'URLSearchParams' not in script_body:
            return '' 
        return script_body
    return re.sub(r'<script[^>]*>.*?<\/script>', script_replacer, html_content, flags=re.IGNORECASE | re.DOTALL)


@app.route('/download', methods=['POST'])
@login_required
def download_zip():
    if current_user.role != 'admin':
        if not current_user.user_type or not current_user.user_type.allow_download:
            return jsonify({
                "error": "Upgrade Required", 
                "needs_upgrade": True, 
                "message": "Downloading source code is not included in your plan."
            }), 403

    current_files = get_current_files()
    if not current_files: return jsonify({"error": "No website generated to download"}), 400
    
    ga_id = ""
    contact_email = current_user.email
    smtp_host_cfg, smtp_port_cfg, smtp_user_cfg, smtp_pass_cfg = "", "587", "", ""
    
    try:
        user_settings = json.loads(current_user.user_settings_json or '{}')
        ga_id = user_settings.get('google_analytics', '').strip()
        contact_email = user_settings.get('contact_email', contact_email).strip()
        smtp_host_cfg = user_settings.get('smtp_host', '').strip()
        smtp_port_cfg = str(user_settings.get('smtp_port', '587')).strip()
        smtp_user_cfg = user_settings.get('smtp_user', '').strip()
        # SECURITY FIX: Decrypt the SMTP password for the generated PHP script
        smtp_pass_cfg = decrypt_sensitive_data(user_settings.get('smtp_pass', '').strip())
    except: pass
    
    current_files['send_mail.php'] = PHPGeneratorService.get_contact_form_script(
        owner_email=contact_email, smtp_host=smtp_host_cfg, smtp_port=smtp_port_cfg,
        smtp_user=smtp_user_cfg, smtp_pass=smtp_pass_cfg
    )
    
    zip_name = "website.zip"
    session_data = get_user_session_data()
    active_project = session_data.get('active_project')
    
    if active_project:
        # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
        clean_proj = sanitize_project_name(active_project)
        if clean_proj: zip_name = f"{clean_proj}.zip"

    zip_buffer = io.BytesIO()
    downloaded_images = {} 

    def zip_fetcher(url_val, cache, zf_obj):
        if not url_val or url_val.startswith('images/') or url_val.startswith('data:'): return None
        if url_val in cache: return cache[url_val]
        
        if 'url=' in url_val and ('/api/thumbnail' in url_val or 'thumbnail' in url_val):
            try: url_val = unquote(url_val.split('url=')[1].split('&')[0])
            except: pass

        filename, file_content = None, None
        local_path = get_local_upload_path(url_val)
        
        if local_path:
            try:
                with open(local_path, 'rb') as f: file_content = f.read()
                filename = os.path.basename(local_path)
            except Exception as e: print(f"Error reading local file: {e}")
                
        if not file_content and ('/uploads/' in url_val or request.host in url_val):
            try:
                uri = '/' + url_val.split('/', 3)[-1] if url_val.startswith('http') else url_val
                if not uri.startswith('/'): uri = '/' + uri
                with app.test_client() as internal_cl:
                    resp = internal_cl.get(uri)
                    if resp.status_code == 200 and len(resp.data) > 0:
                        file_content = resp.data
                        clean_name = unquote(url_val.split('?')[0].split('/')[-1])
                        ext = clean_name.split('.')[-1] if '.' in clean_name else 'png'
                        filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
            except: pass
                
        elif not file_content and url_val.startswith('http'):
            try:
                # SECURITY FIX: Enforce SSL verification and SSRF check
                if is_safe_url(url_val):
                    resp = requests.get(url_val, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=10)
                    if resp.status_code == 200:
                        file_content = resp.content
                        ext = 'png' if '.png' in url_val.lower() else 'jpg'
                        filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
            except: pass
            
        if filename and file_content:
            filename = secure_filename(filename)
            zf_obj.writestr(f"images/{filename}", file_content)
            cache[url_val] = filename
            return filename
        return None

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in current_files.items():
            if isinstance(content, str) and content.startswith('data:'):
                try:
                    header, encoded = content.split(',', 1)
                    zf.writestr(filename, base64.b64decode(encoded))
                except: pass
            elif filename.endswith(('.html', '.php')):
                static_content = remove_proxy_links(content)
                static_content = process_and_localize_assets(static_content, downloaded_images, lambda url, c: zip_fetcher(url, c, zf))
                static_content = fix_form_actions(static_content)
                static_content = fix_form_scripts(static_content)
                
                if 'styles.css' in current_files and not re.search(r'<link[^>]*href=["\']styles\.css["\']', static_content, re.IGNORECASE):
                     if '</head>' in static_content: static_content = static_content.replace('</head>', '<link rel="stylesheet" href="styles.css"></head>')
                         
                if 'action="send_mail.php"' in static_content or "action='send_mail.php'" in static_content:
                    if '</body>' in static_content: static_content = static_content.replace('</body>', f"{AJAX_FORM_SCRIPT}</body>")
                    else: content += AJAX_FORM_SCRIPT
                        
                zf.writestr(filename, apply_google_analytics(static_content, ga_id).encode('utf-8'))
            elif filename.endswith('.css'):
                zf.writestr(filename, process_and_localize_assets(content, downloaded_images, lambda url, c: zip_fetcher(url, c, zf)).encode('utf-8'))
            else:
                zf.writestr(filename, content.encode('utf-8'))
                
    zip_buffer.seek(0)
    response = send_file(zip_buffer, as_attachment=True, download_name=zip_name, mimetype='application/zip')
    response.headers['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return response

# ========================================================
# ENCRYPTED CREDENTIALS API
# ========================================================
@app.route('/api/user/ftp_credentials', methods=['GET'])
@login_required
def get_ftp_credentials():
    creds = FTPEncryptionService.decrypt_credentials(current_user.ftp_credentials)
    return jsonify({
        "success": True, 
        "host": creds.get('host', ''),
        "user": creds.get('user', ''),
        "target_dir": creds.get('target_dir', '/public_html'),
        "has_saved_password": bool(creds.get('password'))
    })

# ========================================================
# DEPLOYMENT API (UPDATED WITH SAVED CREDS)
# ========================================================
@app.route('/api/deploy_website', methods=['POST'])
@login_required
def deploy_website():
    req_data = request.json
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    project_name = sanitize_project_name(req_data.get('project_name', ''))
    
    ftp_host = req_data.get('ftp_host')
    ftp_user = req_data.get('ftp_user')
    ftp_pass = req_data.get('ftp_pass')
    target_dir = req_data.get('target_dir', '/public_html')
    save_creds = req_data.get('save_credentials', False)

    # Use saved password if the field was left empty by the UI
    if not ftp_pass:
        saved_creds = FTPEncryptionService.decrypt_credentials(current_user.ftp_credentials)
        if saved_creds and saved_creds.get('host') == ftp_host and saved_creds.get('user') == ftp_user:
            ftp_pass = saved_creds.get('password')

    if not all([project_name, ftp_host, ftp_user, ftp_pass]):
        return jsonify({"success": False, "error": "Missing FTP credentials or Project Name."}), 400

    # Save credentials if requested
    if save_creds:
        new_creds = {'host': ftp_host, 'user': ftp_user, 'password': ftp_pass, 'target_dir': target_dir}
        current_user.ftp_credentials = FTPEncryptionService.encrypt_credentials(new_creds)
        db.session.commit()

    user_dir = get_user_project_dir()
    project_file_path = os.path.join(user_dir, f"{project_name}.json")

    # FIX: Deploy the LIVE editor state (latest edits) instead of only the last saved JSON,
    # otherwise recent changes never reached the hosting path.
    live_files = {}
    try:
        session_data = get_user_session_data()
        active_proj = sanitize_project_name(session_data.get('active_project') or '') if session_data else ''
        if active_proj == project_name or not os.path.exists(project_file_path):
            live_files = {k: (strip_ui_elements(v) if isinstance(v, str) and k.endswith(('.html', '.php')) else v)
                          for k, v in (get_current_files() or {}).items()}
    except Exception as e:
        print(f"deploy: could not read live session files: {e}", flush=True)

    if not live_files and not os.path.exists(project_file_path): return jsonify({"success": False, "error": "Project not found."}), 404

    temp_dir = tempfile.mkdtemp()
    downloaded_images_cache = {}
    images_dir = os.path.join(temp_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    
    def deploy_fetcher(url_val, cache):
        if not url_val or url_val.startswith('images/') or url_val.startswith('data:'): return None
        if url_val in cache: return cache[url_val]
        
        if 'url=' in url_val and ('/api/thumbnail' in url_val or 'thumbnail' in url_val):
            try: url_val = unquote(url_val.split('url=')[1].split('&')[0])
            except: pass

        filename, file_content = None, None
        local_path = get_local_upload_path(url_val)
        
        if local_path:
            try:
                with open(local_path, 'rb') as f: file_content = f.read()
                filename = os.path.basename(local_path)
            except: pass
                
        if not file_content and ('/uploads/' in url_val or request.host in url_val):
            try:
                uri = '/' + url_val.split('/', 3)[-1] if url_val.startswith('http') else url_val
                if not uri.startswith('/'): uri = '/' + uri
                with app.test_client() as internal_cl:
                    resp = internal_cl.get(uri)
                    if resp.status_code == 200 and len(resp.data) > 0:
                        file_content = resp.data
                        clean_name = unquote(url_val.split('?')[0].split('/')[-1])
                        ext = clean_name.split('.')[-1] if '.' in clean_name else 'png'
                        filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
            except: pass
                    
        elif not file_content and url_val.startswith('http'):
            try:
                # SECURITY FIX: Enforce SSL verification and SSRF check
                if is_safe_url(url_val):
                    resp = requests.get(url_val, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=10)
                    if resp.status_code == 200:
                        file_content = resp.content
                        ext = 'png' if '.png' in url_val.lower() else 'jpg'
                        filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
            except: pass
        
        if filename and file_content:
            filename = secure_filename(filename)
            with open(os.path.join(images_dir, filename), 'wb') as img_f: img_f.write(file_content)
            cache[url_val] = filename
            return filename
        return None
    
    try:
        if live_files:
            files = live_files
            # Keep the saved project in sync with what was deployed
            try:
                with open(project_file_path, 'w', encoding='utf-8') as f: json.dump({"files": live_files}, f, indent=4)
            except Exception: pass
        else:
            with open(project_file_path, 'r', encoding='utf-8') as f: project_data = json.load(f)
            files = project_data.get('files', {})
        if not files: return jsonify({"success": False, "error": "Project is empty."}), 400

        normalized_files = {}
        has_index = False
        for fname, fcontent in files.items():
            if fname.lower() in ['index.html', 'index.php']:
                normalized_files[fname.lower()] = fcontent
                has_index = True
            else: normalized_files[fname] = fcontent

        if not has_index:
            html_files = [f for f in normalized_files.keys() if f.lower().endswith('.html')]
            if html_files:
                target = [f for f in html_files if f.lower() in ['home.html', 'main.html']]
                target = target[0] if target else html_files[0]
                normalized_files['index.html'] = normalized_files.pop(target)

        ga_id, contact_email = "", current_user.email
        smtp_host_cfg, smtp_port_cfg, smtp_user_cfg, smtp_pass_cfg = "", "587", "", ""
        try:
            user_settings = json.loads(current_user.user_settings_json or '{}')
            ga_id = user_settings.get('google_analytics', '').strip()
            contact_email = user_settings.get('contact_email', contact_email).strip()
            smtp_host_cfg = user_settings.get('smtp_host', '').strip()
            smtp_port_cfg = str(user_settings.get('smtp_port', '587')).strip()
            smtp_user_cfg = user_settings.get('smtp_user', '').strip()
            # SECURITY FIX: Decrypt the SMTP password for the generated PHP script
            smtp_pass_cfg = decrypt_sensitive_data(user_settings.get('smtp_pass', '').strip())
        except: pass
        
        normalized_files['send_mail.php'] = PHPGeneratorService.get_contact_form_script(
            owner_email=contact_email, smtp_host=smtp_host_cfg, smtp_port=smtp_port_cfg,
            smtp_user=smtp_user_cfg, smtp_pass=smtp_pass_cfg
        )

        for filename, content in normalized_files.items():
            file_path = os.path.join(temp_dir, filename)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            if isinstance(content, str) and content.startswith('data:'):
                try:
                    header, encoded = content.split(',', 1)
                    with open(file_path, 'wb') as bin_f: bin_f.write(base64.b64decode(encoded))
                except: pass
            else:
                if filename.endswith(('.html', '.php', '.css')):
                    if filename.endswith(('.html', '.php')):
                        content = remove_proxy_links(content)
                        content = process_and_localize_assets(content, downloaded_images_cache, deploy_fetcher)
                        content = fix_form_actions(content)
                        content = fix_form_scripts(content)
                        
                        if 'styles.css' in normalized_files and not re.search(r'<link[^>]*href=["\']styles\.css["\']', content, re.IGNORECASE):
                            if '</head>' in content: content = content.replace('</head>', '<link rel="stylesheet" href="styles.css"></head>')
                                
                        if 'action="send_mail.php"' in content or "action='send_mail.php'" in content:
                            if '</body>' in content: content = content.replace('</body>', f"{AJAX_FORM_SCRIPT}</body>")
                            else: content += AJAX_FORM_SCRIPT
                                
                        content = apply_google_analytics(content, ga_id)
                        # FIX: pages saved before the mobile fix carry "overflow-x: hidden" on main/section,
                        # which collapses <main> on phones in full-height layouts. Upgrade it to "clip".
                        content = content.replace("max-width: 100vw !important; overflow-x: hidden !important; }",
                                                  "max-width: 100vw !important; overflow-x: clip !important; }")
                        # FIX: Cache-bust local CSS/JS so browsers/hosting caches pick up the new version
                        deploy_ver = datetime.now().strftime('%Y%m%d%H%M%S')
                        content = re.sub(r'((?:href|src)=["\'])(?!https?:|//|data:)([^"\'?#]+\.(?:css|js))(["\'])',
                                         lambda m: f"{m.group(1)}{m.group(2)}?v={deploy_ver}{m.group(3)}", content, flags=re.IGNORECASE)
                    elif filename.endswith('.css'):
                        content = process_and_localize_assets(content, downloaded_images_cache, deploy_fetcher)

                    with open(file_path, 'w', encoding='utf-8') as txt_f: txt_f.write(content)
                else:
                    with open(file_path, 'w', encoding='utf-8') as txt_f: txt_f.write(content)

        success, logs = DeploymentService.deploy_via_ftp(ftp_host, ftp_user, ftp_pass, temp_dir, target_dir)

    except Exception as e:
        return jsonify({"success": False, "error": f"Deploy Error: {str(e)}"}), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    return jsonify({"success": success, "logs": logs, "message": "Deployed!" if success else "Failed."})


# ========================================================
# IMPORT VIA FTP API
# ========================================================
@app.route('/api/import_ftp', methods=['POST'])
@login_required
def import_ftp_project():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": "Storage limit reached. Please upgrade your plan."}), 403

    req_data = request.json
    
    # SECURITY FIX: Path Traversal prevention (Preserving Spaces)
    project_name = sanitize_project_name(req_data.get('project_name', 'FTP_Import'))
    
    ftp_host = req_data.get('ftp_host')
    ftp_user = req_data.get('ftp_user')
    ftp_pass = req_data.get('ftp_pass')
    target_dir = req_data.get('target_dir', '/public_html')
    save_creds = req_data.get('save_credentials', False)

    # Use saved password if empty
    if not ftp_pass:
        saved_creds = FTPEncryptionService.decrypt_credentials(current_user.ftp_credentials)
        if saved_creds and saved_creds.get('host') == ftp_host and saved_creds.get('user') == ftp_user:
            ftp_pass = saved_creds.get('password')

    if not all([ftp_host, ftp_user, ftp_pass]):
        return jsonify({"success": False, "error": "Missing FTP credentials."}), 400

    # Save credentials if requested
    if save_creds:
        new_creds = {'host': ftp_host, 'user': ftp_user, 'password': ftp_pass, 'target_dir': target_dir}
        current_user.ftp_credentials = FTPEncryptionService.encrypt_credentials(new_creds)
        db.session.commit()

    success, files_dict, logs = DeploymentService.import_via_ftp(ftp_host, ftp_user, ftp_pass, target_dir)

    if not success or not files_dict:
        return jsonify({"success": False, "error": "\n".join(logs)}), 400

    # Format the project name
    if not project_name: project_name = "FTP_Import"
    
    user_dir = get_user_project_dir()
    save_path = os.path.join(user_dir, f"{project_name}.json")
    counter, orig_name = 1, project_name
    while os.path.exists(save_path):
        project_name = f"{orig_name}_{counter}"
        save_path = os.path.join(user_dir, f"{project_name}.json")
        counter += 1

    # Format links so the builder's live preview works perfectly
    cleaned_project = {}
    for fname, content in files_dict.items():
        if isinstance(content, str) and fname.endswith(('.html', '.php', '.css')):
            cleaned_project[fname] = ensure_links_are_proxied(content, list(files_dict.keys()))
        else:
            cleaned_project[fname] = content

    with open(save_path, 'w', encoding='utf-8') as f:
        json.dump({"files": cleaned_project}, f, indent=4)

    # Set as active session
    session_data = get_user_session_data()
    session_data['history_stack'] = [cleaned_project]
    session_data['redo_stack'] = []
    session_data['active_project'] = project_name

    try:
        log = UserActivityLog(user_id=current_user.id, action_type='project_imported', description=f"Imported FTP Project: '{project_name}'")
        db.session.add(log)
        db.session.commit()
    except: pass

    current_page = next((k for k in cleaned_project if k.endswith(('.html', '.php'))), "index.html")
    
    # --- GOAL 3 FIX: ADD PROJECT NAME TO RESPONSE ---
    return jsonify({"success": True, "message": f"Project '{project_name}' imported successfully!", "project_name": project_name, "files": cleaned_project, "current_page": current_page, "logs": logs})