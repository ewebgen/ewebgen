import os
import json
import re
import uuid
import traceback
import shutil
import requests
import io
import urllib3
from urllib.parse import unquote
from PIL import Image
from flask import request, jsonify, redirect, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

# Disable harmless SSL warnings during local/external image localization
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import Shared Resources
from database_setup import (
    app, db, SystemSettings, PROJECTS_DIR, 
    TemplateCategory, TemplateItem, UserSavedSection
)

# Import core dependencies from our new services structure
# SECURITY FIX: Added is_safe_url to block SSRF
from services_core import (
    allowed_file,
    save_attachment,
    download_image_server_side,
    get_user_project_dir,
    localize_html_images,
    purge_ga_and_smtp_from_html, 
    is_safe_url 
)

# Import AI specific dependencies
from services_ai import (
    ImageProviderService,
    AIGenerationService
)

# Import the storage limit checker from the project routes
from routes_project import check_storage_limit

# Helper function to create a clean folder name from the template name
def slugify(text):
    text = text.lower()
    return re.sub(r'[\W_]+', '_', text).strip('_')

# ========================================================
# FULLY RESTORED: MEDIA & GALLERY ENDPOINTS
# ========================================================

@app.route('/api/thumbnail')
@login_required
def serve_thumbnail():
    """
    Smart Thumbnail Generator:
    Takes an image URL, generates a compressed 200px thumbnail on-the-fly, 
    caches it, and serves it. Massive performance boost for the Media Manager.
    """
    file_url = request.args.get('url')
    if not file_url:
        return jsonify({"error": "No URL provided"}), 400
        
    # Extract filename from url (e.g. /uploads/user_123_abc.png)
    filename = file_url.split('/')[-1].split('?')[0]
    upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads')
    original_path = os.path.join(upload_folder, filename)
    
    # If the file isn't local, just redirect to the original URL
    if not os.path.exists(original_path):
        return redirect(file_url)

    # Setup hidden thumbnails directory
    thumb_dir = os.path.join(upload_folder, '.thumbnails')
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, f"thumb_{filename}")

    # If thumbnail already exists, serve it instantly
    if os.path.exists(thumb_path):
        return send_file(thumb_path)

    # If thumbnail does not exist, create it, save it, and serve it
    try:
        with Image.open(original_path) as img:
            # Convert RGBA (transparent PNGs) to RGB to safely save as JPEG for maximum compression
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            # Create a lightweight 200x200 thumbnail
            img.thumbnail((200, 200))
            img.save(thumb_path, format="JPEG", quality=75)
            
        return send_file(thumb_path)
    except Exception as e:
        print(f"Thumbnail error for {filename}: {e}")
        return redirect(file_url) # Fallback to original image if conversion fails

@app.route('/api/gallery')
@login_required
def get_gallery_images():
    images = []
    videos = [] 
    
    uid = str(current_user.id)
    user_prefix = f"user_{uid}_"
    
    if os.path.exists(app.config['UPLOAD_FOLDER']):
        try:
            files = sorted(
                os.listdir(app.config['UPLOAD_FOLDER']), 
                key=lambda x: os.path.getmtime(os.path.join(app.config['UPLOAD_FOLDER'], x)), 
                reverse=True
            )
            for f in files:
                lower_f = f.lower()
                if f.startswith(user_prefix) and '_gal_' in f: # Match the new prefix from services_core
                    if lower_f.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
                        images.append(f"/uploads/{f}")
                    elif lower_f.endswith(('.mp4', '.webm', '.ogg', '.mov')):
                        videos.append(f"/uploads/{f}")
                # Fallback for old prefix if needed
                elif f.startswith(user_prefix) and '_gallery_' in f:
                    if lower_f.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
                        images.append(f"/uploads/{f}")
                    elif lower_f.endswith(('.mp4', '.webm', '.ogg', '.mov')):
                        videos.append(f"/uploads/{f}")
        except Exception as e:
            print(f"Error reading gallery: {e}")
    
    return jsonify({"success": True, "images": images, "videos": videos})

@app.route('/api/project_images')
@login_required
def get_project_images():
    page = int(request.args.get('page', 1))
    per_page = 20
    
    user_dir = get_user_project_dir()
    all_images = []
    seen_images = set()
    
    if os.path.exists(user_dir):
        try:
            project_files = sorted(
                [f for f in os.listdir(user_dir) if f.endswith('.json')],
                key=lambda x: os.path.getmtime(os.path.join(user_dir, x)),
                reverse=True
            )
            
            for proj_file in project_files:
                try:
                    with open(os.path.join(user_dir, proj_file), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        files = data.get('files', {})
                        for fname, content in files.items():
                            if fname.endswith(('.html', '.php')) and isinstance(content, str):
                                matches = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
                                for src in matches:
                                    if src not in seen_images and not src.startswith('data:image'):
                                        seen_images.add(src)
                                        all_images.append(src)
                except Exception as e:
                    pass
        except Exception as e:
            print(f"Error reading project directory: {e}")
            
    total = len(all_images)
    start = (page - 1) * per_page
    end = start + per_page
    paginated = all_images[start:end]
    
    return jsonify({
        "success": True, 
        "images": paginated, 
        "has_more": end < total,
        "page": page
    })

@app.route('/api/search_images')
@login_required
def api_search_images():
    query = request.args.get('q', '')
    if not query:
        return jsonify({"success": False, "error": "No query provided"}), 400
    
    try:
        results = ImageProviderService.search_images(query)
        return jsonify({"success": True, "images": results})
    except Exception as e:
        print(f"Search Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/generate_image', methods=['POST'])
@login_required
def api_generate_image():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": f"Storage limit reached. Please upgrade your plan."}), 403

    data = request.json
    prompt = data.get('prompt')
    img_type = data.get('type', 'image') 
    text_overlay = data.get('text', '') 
    
    if not prompt:
        return jsonify({"success": False, "error": "No prompt provided"}), 400
    
    try:
        result_urls = AIGenerationService.generate_image(prompt, img_type, text_overlay)
        if result_urls:
            return jsonify({"success": True, "images": result_urls})
        else:
            return jsonify({"success": False, "error": "Generation returned no result"}), 500
    except Exception as e:
        print(f"Generation Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/upload_image', methods=['POST'])
@login_required
def api_upload_image():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": f"Storage limit reached. Please upgrade your plan."}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "No selected file"}), 400
        
    is_video = file.filename.lower().endswith(('.mp4', '.webm', '.ogg', '.mov'))
    is_allowed = allowed_file(file.filename) or is_video
        
    if file and is_allowed:
        url = save_attachment(file)
        if url:
            return jsonify({"success": True, "url": url})
            
    return jsonify({"success": False, "error": "Upload failed or unsupported file type."}), 500

@app.route('/api/admin/upload_logo', methods=['POST'])
@login_required
def api_admin_upload_logo():
    if current_user.role != 'admin':
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    if 'image' not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400
        
    file = request.files['image']
    if file.filename == '':
        return jsonify({"success": False, "error": "No selected file"}), 400
        
    if file and allowed_file(file.filename):
        url = save_attachment(file)
        if url:
            return jsonify({"success": True, "url": url})
            
    return jsonify({"success": False, "error": "Upload failed"}), 500

@app.route('/api/delete_image', methods=['POST'])
@login_required
def delete_image():
    data = request.json
    filename = data.get('filename')
    if not filename: return jsonify({"success": False, "error": "No filename provided"}), 400
    
    filename = secure_filename(filename)
    
    uid = str(current_user.id)
    user_prefix = f"user_{uid}_"
    
    if not filename.startswith(user_prefix) and current_user.role != 'admin':
        return jsonify({"success": False, "error": "Unauthorized to delete this media file."}), 403
        
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            return jsonify({"success": True, "message": "Media deleted."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": False, "error": "File not found"}), 404

@app.route('/api/save_web_image', methods=['POST'])
@login_required
def save_web_image():
    if not check_storage_limit(current_user.id):
        return jsonify({"success": False, "error": f"Storage limit reached. Please upgrade your plan."}), 403

    url = request.json.get('url')
    if not url: return jsonify({"success": False, "error": "No URL provided"}), 400
    
    try:
        local_url = download_image_server_side(url, filename_prefix="gallery_web_")
        if local_url and local_url.startswith('/uploads/'):
            return jsonify({"success": True, "url": local_url})
        else:
            return jsonify({"success": False, "error": "Failed to download image."}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ========================================================
# FULLY RESTORED: USER SAVED SECTIONS API
# ========================================================

@app.route('/api/user/saved_sections', methods=['GET'])
@login_required
def get_user_saved_sections():
    sections = UserSavedSection.query.filter_by(user_id=current_user.id).order_by(UserSavedSection.created_at.desc()).all()
    result = []
    for s in sections:
        result.append({
            "id": s.id,
            "name": s.name,
            "content_html": s.content_html,
            "date": s.created_at.strftime("%Y-%m-%d")
        })
    return jsonify({"success": True, "sections": result})

@app.route('/api/user/saved_sections', methods=['POST'])
@login_required
def save_user_section():
    data = request.json
    name = data.get('name')
    content_html = data.get('content_html')

    if not name or not content_html:
        return jsonify({"success": False, "error": "Name and content are required."}), 400

    new_sec = UserSavedSection(
        user_id=current_user.id,
        name=name,
        content_html=content_html
    )
    db.session.add(new_sec)
    db.session.commit()
    
    return jsonify({"success": True, "message": "Section saved to your account!", "id": new_sec.id})

@app.route('/api/user/saved_sections/<int:sec_id>', methods=['DELETE'])
@login_required
def delete_user_section(sec_id):
    sec = UserSavedSection.query.filter_by(id=sec_id, user_id=current_user.id).first()
    if sec:
        db.session.delete(sec)
        db.session.commit()
        return jsonify({"success": True, "message": "Section deleted."})
    return jsonify({"success": False, "error": "Section not found or unauthorized."}), 404


# ========================================================
# FULLY RESTORED: ADMIN TEMPLATES & CATEGORIES API
# ========================================================

@app.route('/api/templates/categories', methods=['GET'])
@login_required
def get_template_categories():
    # Filters out "Saved Sections" entirely from category dropdowns
    categories = TemplateCategory.query.order_by(TemplateCategory.name).all()
    filtered_categories = []
    for c in categories:
        name_lower = c.name.lower()
        if "saved section" not in name_lower and "my save" not in name_lower:
            filtered_categories.append({"id": c.id, "name": c.name})
            
    return jsonify(filtered_categories)

@app.route('/api/admin/templates/categories', methods=['POST'])
@login_required
def create_template_category():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    name = data.get('name')
    if not name: return jsonify({"success": False, "error": "Name is required"}), 400
    
    new_cat = TemplateCategory(name=name)
    db.session.add(new_cat)
    db.session.commit()
    return jsonify({"success": True, "message": "Category created", "category": {"id": new_cat.id, "name": new_cat.name}})

@app.route('/api/admin/templates/categories/<int:cat_id>', methods=['PUT'])
@login_required
def rename_template_category(cat_id):
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    name = data.get('name')
    if not name: return jsonify({"success": False, "error": "Name is required"}), 400
    
    cat = TemplateCategory.query.get(cat_id)
    if cat:
        cat.name = name
        db.session.commit()
        return jsonify({"success": True, "message": "Category renamed successfully!", "category": {"id": cat.id, "name": cat.name}})
    return jsonify({"success": False, "error": "Category not found."}), 404

@app.route('/api/admin/templates/categories/<int:cat_id>', methods=['DELETE'])
@login_required
def delete_template_category(cat_id):
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    cat = TemplateCategory.query.get(cat_id)
    if cat:
        try:
            # Explicitly delete all templates inside the category first to clear foreign keys
            items = TemplateItem.query.filter_by(category_id=cat.id).all()
            for item in items:
                # Clean up new individual slug template folder before deleting item
                template_slug = slugify(item.name)
                template_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', template_slug)
                if os.path.exists(template_folder):
                    try:
                        shutil.rmtree(template_folder)
                    except Exception as e:
                        print(f"Error removing template folder during category deletion: {e}")
                
                # Fallback: Remove old flat-file zip/json payload if stored locally
                if item.project_data_json and item.project_data_json.startswith('FILE:'):
                    file_name = item.project_data_json.split('FILE:')[1]
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', file_name)
                    try: 
                        if os.path.exists(file_path):
                            if os.path.isfile(file_path):
                                os.remove(file_path)
                    except Exception as e:
                        print(f"Error removing old template file during category deletion: {e}")
                db.session.delete(item)
            
            # Commit the template item deletions
            db.session.commit()

            # Now safely delete the category
            db.session.delete(cat)
            db.session.commit()
            return jsonify({"success": True, "message": "Category and all its templates were deleted successfully."})
            
        except Exception as e:
            db.session.rollback()
            print(f"Error deleting category: {e}")
            return jsonify({"success": False, "error": f"Database error: {str(e)}"}), 500
            
    return jsonify({"success": False, "error": "Category not found."}), 404

@app.route('/api/templates/items', methods=['GET'])
@login_required
def get_template_items():
    cat_id = request.args.get('category_id')
    item_type = request.args.get('type', 'all') # Can be 'all', 'templates', or 'elements'
    
    # 1. Identify "Saved Sections" categories to exclude them entirely
    excluded_cat_ids = []
    try:
        categories = TemplateCategory.query.all()
        for c in categories:
            name_lower = c.name.lower()
            if "saved section" in name_lower or "my save" in name_lower:
                excluded_cat_ids.append(c.id)
    except Exception:
        pass
        
    # 2. Fetch items based on category or fetch All
    if cat_id and str(cat_id).lower() != 'all':
        items = TemplateItem.query.filter_by(category_id=cat_id).order_by(TemplateItem.id.desc()).all()
    else:
        if excluded_cat_ids:
            items = TemplateItem.query.filter(~TemplateItem.category_id.in_(excluded_cat_ids)).order_by(TemplateItem.id.desc()).all()
        else:
            items = TemplateItem.query.order_by(TemplateItem.id.desc()).all()
        
    result = []
    for i in items:
        # EXCLUDE elements belonging to 'Saved Sections' category just in case
        if i.category_id in excluded_cat_ids:
            continue
            
        is_full = bool(i.project_data_json and i.project_data_json.strip())
        
        # --- FIX: Filter by requested item type ---
        if item_type == 'templates' and not is_full:
            continue
        if item_type == 'elements' and is_full:
            continue
            
        result.append({
            "id": i.id,
            "category_id": i.category_id,
            "name": i.name,
            "thumbnail_url": i.thumbnail_url,
            "content_html": i.content_html,
            "is_full_project": is_full 
        })
    return jsonify(result)

# --- FIX: Clean Universal GET alias for template loading ---
@app.route('/api/templates/items/<int:item_id>', methods=['GET'])
@app.route('/api/admin/templates/items/<int:item_id>', methods=['GET'])
@login_required
def get_template_item(item_id):
    item = TemplateItem.query.get(item_id)
    if item:
        project_json = item.project_data_json
        
        if project_json and project_json.startswith('FILE:'):
            file_name = project_json.split('FILE:')[1]
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', file_name)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    project_json = f.read()
            except Exception as e:
                print(f"Error reading large template file: {e}")
                project_json = "{}"
                
        return jsonify({
            "success": True,
            "item": {
                "id": item.id,
                "name": item.name,
                "category_id": item.category_id,
                "content_html": item.content_html,
                "project_data_json": project_json
            }
        })
    return jsonify({"success": False, "error": "Template not found."}), 404

def purge_ga_and_smtp(html_str):
    """Aggressively strips Google Analytics and SMTP logic so it doesn't get baked into global templates."""
    if not html_str or not isinstance(html_str, str): 
        return html_str
    return purge_ga_and_smtp_from_html(html_str)

# --- SURGICAL THUMBNAIL PROXY UNWRAPPER FOR TEMPLATE ASSETS ---
def get_tpl_local_path(url_val):
    """
    Safely unquotes and hunts down local media files across active upload directories.
    Surgically intercepts smart thumbnail proxies (/api/thumbnail?url=...) and 
    extracts the raw target file sitting inside before hunting the disk.
    """
    if not url_val: return None
    
    # Surgical Proxy Interceptor
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
    paths = [
        os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), fname),
        os.path.join(app.root_path, app.config.get('UPLOAD_FOLDER', 'uploads'), fname),
        os.path.join(base_app_dir, 'uploads', fname),
        os.path.join(os.getcwd(), 'uploads', fname),
        os.path.join(os.getcwd(), fname),
        fname
    ]
    for p in paths:
        if os.path.exists(p) and os.path.isfile(p): return p
    return None

@app.route('/api/admin/templates/items', methods=['POST'])
@login_required
def create_template_item():
    try:
        if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
        
        item_id = request.form.get('id') 
        name = request.form.get('name')
        category_id = request.form.get('category_id')
        content_html = request.form.get('content_html')
        
        if not name:
            return jsonify({"success": False, "error": "Name is required."}), 400
            
        # DEFINE TEMPLATE-SPECIFIC DIRECTORY
        template_slug = slugify(name)
        template_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', template_slug)
        os.makedirs(template_dir, exist_ok=True)

        # --- FIX: Specifically search for "Global Sections" instead of just taking the first category ---
        if not category_id or str(category_id).lower() == 'null':
            default_cat = TemplateCategory.query.filter_by(name="Global Sections").first()
            if not default_cat:
                default_cat = TemplateCategory(name="Global Sections")
                db.session.add(default_cat)
                db.session.commit()
            category_id = default_cat.id

        project_data_json = request.form.get('project_data_json')
        project_data_file = request.files.get('project_data_file')
        
        if not content_html and not project_data_json and not project_data_file:
            return jsonify({"success": False, "error": "You must provide either HTML content or upload a Full Project zip/folder."}), 400

        if project_data_file:
            project_data_json = project_data_file.read().decode('utf-8')
            
        # LOCALIZER: Handles local copying, internal WSGI memory loopback, and external downloading
        def localize_template_assets(html_str):
            if not html_str or not isinstance(html_str, str): return html_str
            
            def asset_replacer(match):
                try:
                    full_match = match.group(0)
                    attr = match.group(1).strip() # src or poster
                    url_val = match.group(2).strip()
                    
                    if not url_val or url_val.startswith('data:') or f'/system_templates/{template_slug}/' in url_val:
                        return full_match

                    # --- PROXY UNWRAPPER ---
                    if 'url=' in url_val and ('/api/thumbnail' in url_val or 'thumbnail' in url_val):
                        try:
                            url_val = unquote(url_val.split('url=')[1].split('&')[0])
                        except: pass
                        
                    file_content = None
                    filename = None
                    
                    local_p = get_tpl_local_path(url_val)
                    if local_p:
                        try:
                            with open(local_p, 'rb') as f: file_content = f.read()
                            filename = os.path.basename(local_p)
                        except: pass
                        
                    # WSGI internal loopback fallback for app-hosted media
                    if not file_content and ('/uploads/' in url_val or '/api/thumbnail' in url_val or request.host in url_val):
                        try:
                            uri = '/' + url_val.split('/', 3)[-1] if url_val.startswith('http') else url_val
                            if not uri.startswith('/'): uri = '/' + uri
                            with app.test_client() as cl:
                                r = cl.get(uri)
                                if r.status_code == 200 and len(r.data) > 0:
                                    file_content = r.data
                                    clean_n = unquote(url_val.split('?')[0].split('/')[-1])
                                    ext = clean_n.split('.')[-1] if '.' in clean_n else 'png'
                                    filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
                        except: pass
                        
                    # External fetch
                    elif not file_content and url_val.startswith('http'):
                        # --- SECURITY FIX: SSRF Protection ---
                        if not is_safe_url(url_val):
                            print(f"⚠️ [Security] Blocked SSRF attempt in template asset: {url_val}")
                            return full_match
                            
                        try:
                            headers = {'User-Agent': 'Mozilla/5.0'}
                            # --- SECURITY FIX: Enforce SSL Verification ---
                            r = requests.get(url_val, headers=headers, timeout=10, stream=True)
                            if r.status_code == 200:
                                file_content = r.content
                                ext = 'jpg'
                                lower_u = url_val.lower().split('?')[0]
                                if '.png' in lower_u: ext = 'png'
                                elif '.webp' in lower_u: ext = 'webp'
                                elif '.svg' in lower_u: ext = 'svg'
                                elif '.gif' in lower_u: ext = 'gif'
                                filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
                        except: pass
                        
                    if file_content and filename:
                        filename = secure_filename(filename)
                        target_file_path = os.path.join(template_dir, filename)
                        with open(target_file_path, 'wb') as out_f:
                            out_f.write(file_content)
                            
                        new_rel_url = f"/uploads/system_templates/{template_slug}/{filename}"
                        return f'{attr}="{new_rel_url}"'
                            
                    return full_match
                except Exception as e:
                    print(f"Asset localizer error: {e}")
                    return match.group(0)

            def css_replacer(match):
                try:
                    full_m = match.group(0)
                    q = match.group(1)
                    u = match.group(2).strip()
                    if not u or u.startswith('data:') or f'/system_templates/{template_slug}/' in u: return full_m

                    # --- PROXY UNWRAPPER ---
                    if 'url=' in u and ('/api/thumbnail' in u or 'thumbnail' in u):
                        try:
                            u = unquote(u.split('url=')[1].split('&')[0])
                        except: pass
                    
                    file_content = None
                    filename = None
                    local_p = get_tpl_local_path(u)
                    if local_p:
                        try:
                            with open(local_p, 'rb') as f: file_content = f.read()
                            filename = os.path.basename(local_p)
                        except: pass
                    if not file_content and ('/uploads/' in u or request.host in u):
                        try:
                            uri = '/' + u.split('/', 3)[-1] if u.startswith('http') else u
                            if not uri.startswith('/'): uri = '/' + uri
                            with app.test_client() as cl:
                                r = cl.get(uri)
                                if r.status_code == 200 and len(r.data) > 0:
                                    file_content = r.data
                                    clean_n = unquote(u.split('?')[0].split('/')[-1])
                                    ext = clean_n.split('.')[-1] if '.' in clean_n else 'png'
                                    filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
                        except: pass
                    elif not file_content and u.startswith('http'):
                        # --- SECURITY FIX: SSRF Protection ---
                        if not is_safe_url(u):
                            print(f"⚠️ [Security] Blocked SSRF attempt in template CSS asset: {u}")
                            return full_m
                            
                        try:
                            # --- SECURITY FIX: Enforce SSL Verification ---
                            r = requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                            if r.status_code == 200:
                                file_content = r.content
                                ext = 'jpg'
                                if '.png' in u.lower(): ext = 'png'
                                elif '.webp' in u.lower(): ext = 'webp'
                                elif '.svg' in u.lower(): ext = 'svg'
                                filename = f"media_{uuid.uuid4().hex[:8]}.{ext}"
                        except: pass
                    if file_content and filename:
                        filename = secure_filename(filename)
                        with open(os.path.join(template_dir, filename), 'wb') as out_f: out_f.write(file_content)
                        return f"url({q}/uploads/system_templates/{template_slug}/{filename}{q})"
                    return full_m
                except: return match.group(0)

            html_str = re.sub(r'(src|poster)\s*=\s*["\']([^"\']+)["\']', asset_replacer, html_str, flags=re.IGNORECASE)
            html_str = re.sub(r'url\(([\'"]?)([^\'"\)]+)\1\)', css_replacer, html_str, flags=re.IGNORECASE)
            return html_str

        # APPLY GA/SMTP PURGE & LOCALIZATION
        if project_data_json or content_html:
            try:
                settings = SystemSettings.query.first()
                if settings and getattr(settings, 'store_images_locally', True):
                    if content_html:
                        content_html = localize_template_assets(content_html)
                        content_html = purge_ga_and_smtp(content_html)
                        
                    if project_data_json:
                        files_dict = json.loads(project_data_json)
                        localized_files = {}
                        for fname, content in files_dict.items():
                            if fname == 'send_mail.php':
                                continue 
                                
                            if fname.endswith(('.html', '.php', '.css')) and isinstance(content, str) and not content.startswith('data:'):
                                clean_content = localize_template_assets(content)
                                clean_content = purge_ga_and_smtp(clean_content)
                                localized_files[fname] = clean_content
                            else:
                                localized_files[fname] = content
                        project_data_json = json.dumps(localized_files)
            except Exception as e:
                print(f"Error localizing/cleaning template images: {e}")
        
        file = request.files.get('thumbnail') or request.files.get('image')
        thumbnail_url = request.form.get('existing_thumbnail', None)
        
        if file and file.filename != '' and allowed_file(file.filename):
            safe_name = secure_filename(file.filename)
            thumb_name, ext = os.path.splitext(safe_name)
            if len(thumb_name) > 60:
                thumb_name = thumb_name[:60]
            unique_filename = f"thumb_{thumb_name}{ext}"
            if len(unique_filename) > 120:
                unique_filename = unique_filename[:120-len(ext)] + ext
            
            file_path = os.path.join(template_dir, unique_filename)
            file.save(file_path)
            thumbnail_url = f"/uploads/system_templates/{template_slug}/{unique_filename}"
            
        if project_data_json and len(project_data_json.encode('utf-8')) > 500 * 1024:
            file_name = f"data_structure.json"
            file_path = os.path.join(template_dir, file_name)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(project_data_json)
            project_data_json = f"FILE:{template_slug}/{file_name}"
            
        if item_id:
            item = TemplateItem.query.get(item_id)
            if not item: return jsonify({"success": False, "error": "Template not found."}), 404
            item.name = name 
            item.category_id = category_id
            item.content_html = content_html
            if project_data_json:
                if item.project_data_json and item.project_data_json.startswith('FILE:'):
                    old_file = item.project_data_json.split('FILE:')[1]
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', old_file)
                    try: 
                        if os.path.isfile(old_path): os.remove(old_path)
                    except: pass
                item.project_data_json = project_data_json
            if file: item.thumbnail_url = thumbnail_url
        else:
            item = TemplateItem(
                name=name,
                category_id=category_id,
                content_html=content_html,
                project_data_json=project_data_json,
                thumbnail_url=thumbnail_url
            )
            db.session.add(item)
            
        db.session.commit()
        return jsonify({"success": True, "message": "Template saved successfully!"})
        
    except Exception as e:
        db.session.rollback()
        traceback.print_exc()
        error_msg = str(e)
        if "max_allowed_packet" in error_msg.lower():
            return jsonify({"success": False, "error": "Database packet limit reached. Fix applied, please try again."}), 500
        return jsonify({"success": False, "error": f"Server Error: {error_msg}"}), 500

@app.route('/api/admin/templates/items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_template_item(item_id):
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    item = TemplateItem.query.get(item_id)
    if item:
        template_slug = slugify(item.name)
        template_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', template_slug)
        if os.path.exists(template_folder):
            try: shutil.rmtree(template_folder)
            except: pass
            
        if item.project_data_json and item.project_data_json.startswith('FILE:'):
            file_name = item.project_data_json.split('FILE:')[1]
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], 'system_templates', file_name)
            try: 
                if os.path.isfile(file_path): os.remove(file_path)
            except: pass
            
        db.session.delete(item)
        db.session.commit()
        return jsonify({"success": True, "message": "Template deleted."})
    return jsonify({"success": False, "error": "Template not found."}), 404