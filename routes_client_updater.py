import os
import io
import json
import re
import requests
import threading
import zipfile
import shutil
import sys
import time
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

# Import shared resources (Added HelpArticle model)
from database_setup import app, db, SystemSettings, BASE_DIR, TemplateCategory, TemplateItem, SystemUpdate, HelpArticle

# --- CONFIGURATION ---
OWNER_UPDATE_URL = "https://app.ewebgen.com/api/updates" 

# Safe headers to prevent "Connection Error" on multiple clicks
REQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Connection': 'close',
    'Cache-Control': 'no-cache'
}

def get_backups_folder():
    folder = app.config.get('BACKUPS_FOLDER', os.path.join(BASE_DIR, 'backups'))
    if not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    return folder

def get_updates_repo_folder():
    folder = app.config.get('UPDATES_FOLDER', os.path.join(BASE_DIR, 'updates_repo'))
    if not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    return folder

def slugify(text):
    """Creates a filesystem-safe directory name from a template title."""
    return re.sub(r'[\W_]+', '_', text.lower()).strip('_')

def get_current_version():
    """Reads the active system version from a local version.txt file."""
    v_path = os.path.join(BASE_DIR, 'version.txt')
    if os.path.exists(v_path):
        try:
            with open(v_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except:
            pass
    return "1.0.0"

def set_current_version(version):
    """Writes the updated system version back to version.txt."""
    v_path = os.path.join(BASE_DIR, 'version.txt')
    try:
        with open(v_path, 'w', encoding='utf-8') as f:
            f.write(version)
    except Exception as e:
        print(f"Warning: Failed to update version file: {e}")

# =====================================================================
# AUTO-SYNC DATA UPON UPDATE (GOAL 1 FAIL-SAFE ENGINE + HELP Q&A)
# =====================================================================
def sync_seed_data():
    """
    Looks for an 'update_seed.json' file after an update or clean install.
    If found, it safely injects new categories, templates, and Help Q&A articles 
    into the client's database without overwriting their existing custom data.
    Crucially features automatic disk offloading to prevent MySQL packet overflows.
    """
    seed_filename = 'update_seed.json'
    
    possible_dirs = [
        os.getcwd(),
        BASE_DIR,
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(BASE_DIR, 'uploads')
    ]
    
    seed_path = None
    for d in possible_dirs:
        p = os.path.join(d, seed_filename)
        if os.path.exists(p) and os.path.isfile(p):
            seed_path = p
            break
            
    if not seed_path:
        print("ℹ️ [Updater] No 'update_seed.json' found. Skipping template sync.")
        return

    print(f"📦 [Updater] Found template seed artifact at: {seed_path}")
    uploads_dir = app.config.get('UPLOAD_FOLDER', os.path.join(BASE_DIR, 'uploads'))
    system_templates_dir = os.path.join(uploads_dir, 'system_templates')
    os.makedirs(system_templates_dir, exist_ok=True)

    try:
        with open(seed_path, 'r', encoding='utf-8') as sf:
            seed_data = json.load(sf)
            
        categories = seed_data.get('categories', [])
        templates = seed_data.get('templates', [])
        articles_data = seed_data.get('help_articles', [])
        
        if not categories and not templates and not articles_data:
            print("⚠️ [Updater] Seed file is empty or malformed.")
            return

        # 1. Synchronize Categories (Upsert)
        cat_map = {} 
        for cat_info in categories:
            c_name = cat_info.get('name')
            if not c_name: continue
            
            existing_cat = TemplateCategory.query.filter(TemplateCategory.name.ilike(c_name)).first()
            if existing_cat:
                cat_map[c_name.lower()] = existing_cat.id
            else:
                new_cat = TemplateCategory(name=c_name)
                db.session.add(new_cat)
                db.session.flush() 
                cat_map[c_name.lower()] = new_cat.id
                
        db.session.commit()
        print(f"✅ [Updater] Synchronized {len(categories)} template categories.")

        # 2. Synchronize Templates with Automatic MySQL Packet Overflow Protection
        default_cat_id = TemplateCategory.query.first().id if TemplateCategory.query.first() else 1
        
        for tpl_info in templates:
            t_name = tpl_info.get('name')
            if not t_name: continue
            
            cat_name = tpl_info.get('category_name', '').lower()
            target_cat_id = cat_map.get(cat_name, default_cat_id)
            
            content_html = tpl_info.get('content_html', '')
            project_json = tpl_info.get('project_data_json', '{}')
            thumbnail_url = tpl_info.get('thumbnail_url', '')
            
            # --- GOAL 1 FAIL-SAFE: Automatic Disk Offloading for massive JSON structures ---
            template_slug = slugify(t_name)
            tpl_folder = os.path.join(system_templates_dir, template_slug)
            os.makedirs(tpl_folder, exist_ok=True)
            
            if project_json and not project_json.startswith('FILE:') and len(project_json.encode('utf-8')) > 250 * 1024:
                disk_json_path = os.path.join(tpl_folder, 'data_structure.json')
                try:
                    with open(disk_json_path, 'w', encoding='utf-8') as df:
                        df.write(project_json)
                    project_json = f"FILE:{template_slug}/data_structure.json"
                    print(f"   💾 Offloaded massive JSON structure to disk for template: '{t_name}'")
                except Exception as e:
                    print(f"   [!] Warning: Failed to offload JSON to disk for {t_name}: {e}")

            existing_item = TemplateItem.query.filter(TemplateItem.name.ilike(t_name)).first()
            
            if existing_item:
                existing_item.category_id = target_cat_id
                existing_item.content_html = content_html
                existing_item.thumbnail_url = thumbnail_url
                
                if existing_item.project_data_json and existing_item.project_data_json.startswith('FILE:') and project_json.startswith('FILE:'):
                    if existing_item.project_data_json != project_json:
                        old_rel = existing_item.project_data_json.split('FILE:')[1]
                        old_p = os.path.join(system_templates_dir, old_rel)
                        try:
                            if os.path.isfile(old_p): os.remove(old_p)
                        except: pass
                        
                existing_item.project_data_json = project_json
            else:
                new_item = TemplateItem(
                    name=t_name,
                    category_id=target_cat_id,
                    content_html=content_html,
                    project_data_json=project_json,
                    thumbnail_url=thumbnail_url
                )
                db.session.add(new_item)
                
        db.session.commit()
        print(f"✅ [Updater] Synchronized {len(templates)} template items successfully.")

        # ========================================================
        # 3. NEW: SYNC KNOWLEDGE BASE Q&A ARTICLES
        # ========================================================
        art_count = 0
        for a_data in articles_data:
            question = a_data.get('question', '').strip()
            answer = a_data.get('answer', '').strip()
            if not question or not answer: continue

            category = a_data.get('category', 'General')
            is_active = a_data.get('is_active', True)

            art = HelpArticle.query.filter_by(question=question).first()
            if not art:
                art = HelpArticle(question=question, answer=answer, category=category, is_active=is_active)
                db.session.add(art)
            else:
                art.answer = answer
                art.category = category
                art.is_active = is_active
            art_count += 1

        db.session.commit()
        print(f"✅ [Updater] Synchronized {art_count} Help Q&A articles successfully.")
        
        try:
            processed_path = seed_path + '.installed'
            if os.path.exists(processed_path): os.remove(processed_path)
            os.rename(seed_path, processed_path)
        except: pass

    except Exception as e:
        db.session.rollback()
        print(f"❌ [Updater] CRITICAL ERROR during seed synchronization: {e}")


# =====================================================================
# SYSTEM BACKUP & RESTORATION ENDPOINTS
# =====================================================================

@app.route('/api/system/list_local_backups', methods=['GET'])
@login_required
def list_local_backups():
    """Populates the 'Restore Previous Version' dropdown in the local CMS."""
    folder = get_backups_folder()
    try:
        files = [f for f in os.listdir(folder) if f.endswith('.zip')]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(folder, x)), reverse=True)
        return jsonify(files)
    except Exception as e:
        print(f"Error listing backups: {e}")
        return jsonify([])

@app.route('/api/system/create_backup', methods=['POST'])
@login_required
def create_system_backup():
    """Manually creates an instant snapshot backup archive of the current working directory."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    try:
        backups_dir = get_backups_folder()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"manual_backup_{timestamp}.zip"
        backup_path = os.path.join(backups_dir, backup_filename)
        
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(BASE_DIR):
                if any(excluded in root for excluded in ['backups', 'updates_repo', '.git', '__pycache__', '.thumbnails', 'projects', 'unsaved']):
                    continue
                for f in files:
                    if f.endswith(('.zip', '.log', '.lic', '.lock', '.installed')): continue
                    file_full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(file_full_path, BASE_DIR)
                    zf.write(file_full_path, rel_path)
                    
        return jsonify({"success": True, "message": f"Backup successfully created: {backup_filename}", "filename": backup_filename})
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to generate backup archive: {str(e)}"}), 500

@app.route('/api/system/download_backup/<filename>', methods=['GET'])
@login_required
def download_system_backup(filename):
    """Allows admins to download a specific backup archive directly to their local machine."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    backups_dir = get_backups_folder()
    safe_fname = secure_filename(filename)
    file_path = os.path.join(backups_dir, safe_fname)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return jsonify({"success": False, "error": "Requested backup file not found on server disk."}), 404

@app.route('/api/system/delete_backup', methods=['POST'])
@login_required
def delete_backup():
    """Deletes a local ZIP backup file from the dropdown list."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    filename = request.json.get('filename')
    if not filename: return jsonify({"success": False, "error": "Missing filename"}), 400
    
    folder = get_backups_folder()
    file_path = os.path.join(folder, secure_filename(filename))
    
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            return jsonify({"success": True, "message": "Backup deleted successfully."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
            
    return jsonify({"success": False, "error": "File not found."}), 404

@app.route('/api/system/restore_backup', methods=['POST'])
@login_required
def restore_backup():
    """Restores the active application code from a selected ZIP backup."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    filename = request.json.get('filename')
    if not filename: return jsonify({"success": False, "error": "No backup specified."}), 400

    folder = get_backups_folder()
    backup_path = os.path.join(folder, secure_filename(filename))
    
    if not os.path.exists(backup_path):
        return jsonify({"success": False, "error": "Backup file not found on disk."}), 404

    try:
        with zipfile.ZipFile(backup_path, 'r') as zip_ref:
            # SECURITY FIX: Protect against Zip Slip vulnerabilities during restore
            for member in zip_ref.namelist():
                target_path = os.path.join(BASE_DIR, member)
                # Ensure the resolved target path is strictly within the BASE_DIR
                if os.path.commonpath([BASE_DIR, os.path.abspath(target_path)]) != BASE_DIR:
                    print(f"⚠️ [Security] Blocked path traversal attempt in backup restore: {member}")
                    continue
                zip_ref.extract(member, BASE_DIR)

        def restart_server():
            time.sleep(2)
            os._exit(0) 
            
        threading.Thread(target=restart_server).start()

        return jsonify({"success": True, "message": "System restored successfully. Application restarting..."})
    except Exception as e:
        return jsonify({"success": False, "error": f"Restoration failed: {str(e)}"}), 500


# =====================================================================
# MANUAL UPLOADS & PERMISSION CHECKING
# =====================================================================

@app.route('/api/system/upload_update', methods=['POST'])
@login_required
def manual_upload_update():
    """Allows admins to manually upload an update .zip package via the CMS interface."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file payload attached."}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "No file selected."}), 400
        
    if not file.filename.lower().endswith('.zip'):
        return jsonify({"success": False, "error": "Only .zip update archives are permitted."}), 400
        
    try:
        repo_dir = get_updates_repo_folder()
        safe_name = secure_filename(file.filename)
        file_path = os.path.join(repo_dir, safe_name)
        file.save(file_path)
        
        new_up = SystemUpdate(
            version=safe_name.replace('.zip', '').replace('update_', ''),
            release_date=datetime.now(timezone.utc),
            description="Manually uploaded system update archive.",
            zip_filename=safe_name,
            is_active=True
        )
        db.session.add(new_up)
        db.session.commit()
        
        return jsonify({"success": True, "message": "Update package uploaded and indexed successfully!"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": f"Package upload failed: {str(e)}"}), 500

@app.route('/api/system/upload_seed', methods=['POST'])
@login_required
def manual_upload_seed():
    """Allows admins to upload a raw 'update_seed.json' directly to trigger instant template ingestion."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No seed file attached."}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "No file selected."}), 400
        
    if not file.filename.lower().endswith('.json'):
        return jsonify({"success": False, "error": "Only 'update_seed.json' formatting is supported."}), 400
        
    try:
        target_path = os.path.join(BASE_DIR, 'update_seed.json')
        file.save(target_path)
        
        sync_seed_data()
        
        return jsonify({"success": True, "message": "Template seed artifact uploaded and successfully synchronized!"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Seed processing failed: {str(e)}"}), 500

@app.route('/api/system/check_permissions', methods=['GET'])
@login_required
def check_system_permissions():
    """Diagnostic check to ensure server environment allows dynamic file creation."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    status = {}
    paths_to_test = [
        ('BASE_DIR', BASE_DIR),
        ('UPLOAD_FOLDER', app.config.get('UPLOAD_FOLDER', os.path.join(BASE_DIR, 'uploads'))),
        ('BACKUPS_FOLDER', get_backups_folder()),
        ('UPDATES_FOLDER', get_updates_repo_folder()),
        ('PROJECTS_DIR', os.path.join(BASE_DIR, 'projects'))
    ]
    
    all_writable = True
    for name, p in paths_to_test:
        if not os.path.exists(p):
            try: os.makedirs(p, exist_ok=True)
            except: pass
        
        is_writable = os.access(p, os.W_OK)
        status[name] = {
            "path": p,
            "exists": os.path.exists(p),
            "writable": is_writable
        }
        if not is_writable: all_writable = False
        
    return jsonify({
        "success": True,
        "all_writable": all_writable,
        "status": status
    })


# =====================================================================
# CLIENT UPDATE POLLING & EXECUTION ENDPOINTS
# =====================================================================

@app.route('/api/system/check_remote_updates', methods=['GET'])
@login_required
def check_remote_updates():
    """Polls the remote master server to check if a newer update is available."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    current_version = get_current_version()
    
    try:
        # SECURITY FIX: Enforce SSL verification (verify=True is default, removing verify=False)
        resp = requests.get(f"{OWNER_UPDATE_URL}/check", headers=REQ_HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                latest = data.get('latest_version')
                # If the versions do not perfectly match, an update is available
                update_available = False
                if latest and latest != current_version:
                    update_available = True
                
                return jsonify({
                    "success": True,
                    "update_available": update_available,
                    "current_version": current_version,
                    "latest_version": latest,
                    "release_notes": data.get('description', '')
                })
            return jsonify(data)
        return jsonify({"success": False, "error": f"Master server returned HTTP {resp.status_code}"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not connect to master server: {str(e)}"})

@app.route('/api/system/list_remote_versions', methods=['GET'])
@login_required
def list_remote_versions():
    """Fetches a list of all available remote update versions from the master server."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    try:
        # SECURITY FIX: Enforce SSL verification
        resp = requests.get(f"{OWNER_UPDATE_URL}/list_all", headers=REQ_HEADERS, timeout=10)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"success": False, "error": f"Master server returned HTTP {resp.status_code}"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not connect to master server: {str(e)}"})

@app.route('/api/system/perform_update', methods=['POST'])
@login_required
def perform_update():
    """Downloads and executes an application update from the master repository."""
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    version = request.json.get('version')
    if not version: return jsonify({"success": False, "error": "Missing version parameter."}), 400
    
    download_url = f"https://app.ewebgen.com/api/updates/download/{version}"

    try:
        # Create safety backup before extraction
        folder = get_backups_folder()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_zip_path = os.path.join(folder, f"autobackup_{timestamp}.zip")
        
        print(f"📦 [Updater] Creating pre-update safety backup: {backup_zip_path}")
        with zipfile.ZipFile(backup_zip_path, 'w', zipfile.ZIP_DEFLATED) as bzip:
            for root, dirs, files in os.walk(BASE_DIR):
                if any(excluded in root for excluded in ['backups', '.git', '__pycache__', 'projects', 'updates_repo', '.thumbnails']):
                    continue
                for f in files:
                    if f.endswith(('.zip', '.log', '.lic', '.lock')): continue
                    file_p = os.path.join(root, f)
                    rel_p = os.path.relpath(file_p, BASE_DIR)
                    bzip.write(file_p, rel_p)

        print(f"⬇️ [Updater] Downloading update package from: {download_url}")
        # SECURITY FIX: Enforce SSL verification
        resp = requests.get(download_url, headers=REQ_HEADERS, timeout=45, stream=True)
        
        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}"
            try: err = resp.json().get('error', err)
            except: pass
            return jsonify({"success": False, "error": f"Failed to download update package: {err}"})
            
        update_zip_buffer = io.BytesIO(resp.content)
        
        print("📂 [Updater] Extracting update package over working environment...")
        with zipfile.ZipFile(update_zip_buffer, 'r') as uzip:
            # SECURITY FIX: Protect against Zip Slip vulnerabilities during extraction
            for member in uzip.namelist():
                target_path = os.path.join(BASE_DIR, member)
                # Ensure the resolved target path is strictly within the BASE_DIR
                if os.path.commonpath([BASE_DIR, os.path.abspath(target_path)]) != BASE_DIR:
                    print(f"⚠️ [Security] Blocked path traversal attempt in update package: {member}")
                    continue
                uzip.extract(member, BASE_DIR)
            
        # Officially update the active version marker
        set_current_version(version)

        print("🔄 [Updater] Executing post-update template synchronization...")
        sync_seed_data()

        # Hard restart trigger
        def restart_server():
            time.sleep(2)
            os._exit(0)
            
        threading.Thread(target=restart_server).start()

        return jsonify({"success": True, "message": "System updated/restored successfully! Restarting..."})

    except Exception as e:
        print(f"❌ [Updater] Update execution failed: {e}")
        return jsonify({"success": False, "error": f"Failed to complete update: {str(e)}"}), 500