import os
import json
from datetime import datetime, timezone
from flask import request, jsonify, send_from_directory, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from database_setup import app, db, SystemUpdate

# Ensure updates directory exists
def get_updates_folder():
    folder = app.config.get('UPDATES_FOLDER', 'updates_repo')
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder

# ==========================================
# PUBLIC API (For Clients to Check Updates)
# ==========================================

@app.route('/api/updates/check', methods=['GET'])
def check_updates():
    """
    Returns the latest active version.
    Clients poll this to see if they are behind.
    """
    # In a real scenario, you might verify an API key from the client here
    latest = SystemUpdate.query.filter_by(is_active=True).order_by(SystemUpdate.id.desc()).first()
    
    if latest:
        return jsonify({
            "success": True,
            "latest_version": latest.version,
            "release_date": latest.release_date.strftime("%Y-%m-%d"),
            "description": latest.description,
            "download_url": f"/api/updates/download/{latest.version}"
        })
    else:
        return jsonify({
            "success": True,
            "latest_version": None,
            "message": "No updates available."
        })

@app.route('/api/updates/list_all', methods=['GET'])
def list_all_updates():
    """
    Returns all active versions (for Restore functionality on Client side).
    """
    updates = SystemUpdate.query.filter_by(is_active=True).order_by(SystemUpdate.id.desc()).all()
    results = []
    for u in updates:
        results.append({
            "version": u.version,
            "release_date": u.release_date.strftime("%Y-%m-%d"),
            "description": u.description,
            "is_latest": u.is_latest,
            "download_url": f"/api/updates/download/{u.version}"
        })
    
    return jsonify({"success": True, "updates": results})

@app.route('/api/updates/download/<version>', methods=['GET'])
def download_update(version):
    """
    Serves the zip file for a specific version.
    """
    # --- GOAL 2 FIX: Secure source code by preventing direct downloads from any user ---
    return jsonify({
        "success": False, 
        "error": "Source code download is restricted for security purposes."
    }), 403

    # Original logic is preserved below to ensure no code deletion:
    update = SystemUpdate.query.filter_by(version=version, is_active=True).first()
    if not update:
        return jsonify({"success": False, "error": "Version not found."}), 404
        
    folder = get_updates_folder()
    
    # Track download count (optional)
    try:
        update.download_count += 1
        db.session.commit()
    except: pass
    
    return send_from_directory(folder, update.zip_filename, as_attachment=True)

# ==========================================
# ADMIN API (Owner Management)
# ==========================================

@app.route('/api/admin/updates/upload', methods=['POST'])
@login_required
def admin_upload_update():
    """
    Admin uploads a new zip file representing a system update.
    """
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
        
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400
        
    file = request.files['file']
    version = request.form.get('version')
    description = request.form.get('description', '')
    
    if file.filename == '' or not version:
        return jsonify({"success": False, "error": "Missing file or version number."}), 400
        
    if not file.filename.endswith('.zip'):
        return jsonify({"success": False, "error": "Only .zip files are allowed."}), 400

    # Check if version exists
    if SystemUpdate.query.filter_by(version=version).first():
        return jsonify({"success": False, "error": f"Version {version} already exists."}), 400

    folder = get_updates_folder()
    
    # --- WINDOWS PATH FIX: Truncate long ZIP filenames to prevent saving errors ---
    safe_base = secure_filename(f"update_{version}_{file.filename}")
    name, ext = os.path.splitext(safe_base)
    
    # Enforce hard 120 character limit
    if len(name) > 110:
        name = name[:110]
        
    filename = f"{name}{ext}"
    if len(filename) > 120:
        filename = filename[:120-len(ext)] + ext
    # -------------------------------------------------------------------------
        
    file.save(os.path.join(folder, filename))
    
    # Manage "Latest" flag
    # Set all others to is_latest=False
    SystemUpdate.query.update({SystemUpdate.is_latest: False})
    
    new_update = SystemUpdate(
        version=version,
        description=description,
        zip_filename=filename,
        is_active=True,
        is_latest=True
    )
    
    db.session.add(new_update)
    db.session.commit()
    
    return jsonify({"success": True, "message": f"Version {version} uploaded successfully."})

@app.route('/api/admin/updates/history', methods=['GET'])
@login_required
def admin_updates_history():
    """
    Admin view of all uploaded versions.
    """
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    updates = SystemUpdate.query.order_by(SystemUpdate.id.desc()).all()
    results = []
    for u in updates:
        results.append({
            "id": u.id,
            "version": u.version,
            "date": u.release_date.strftime("%Y-%m-%d %H:%M"),
            "description": u.description,
            "filename": u.zip_filename,
            "downloads": u.download_count,
            "is_latest": u.is_latest,
            "is_active": u.is_active
        })
    return jsonify(results)

@app.route('/api/admin/updates/delete', methods=['POST'])
@login_required
def admin_delete_update():
    """
    Admin deletes a version.
    """
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json
    u_id = data.get('id')
    update = SystemUpdate.query.get(u_id)
    
    if not update:
        return jsonify({"success": False, "error": "Update not found."}), 404
        
    # Delete file
    folder = get_updates_folder()
    path = os.path.join(folder, update.zip_filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except: pass
        
    db.session.delete(update)
    db.session.commit()
    
    # Re-calculate latest
    latest = SystemUpdate.query.filter_by(is_active=True).order_by(SystemUpdate.id.desc()).first()
    if latest:
        latest.is_latest = True
        db.session.commit()
        
    return jsonify({"success": True, "message": "Update deleted."})

@app.route('/api/admin/updates/toggle_active', methods=['POST'])
@login_required
def admin_toggle_update():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json
    update = SystemUpdate.query.get(data.get('id'))
    if update:
        update.is_active = not update.is_active
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404