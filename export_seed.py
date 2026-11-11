# ==============================================================================
# Copyright (c) 2026 Orgif Solutions LLP. All rights reserved. For info visit ewebgen.com
#
# This file is part of the Editable Website Developer AI Agent project and Owned by Orgif Solutions LLP.
# ==============================================================================

import os
import json
import re
from urllib.parse import unquote
from database_setup import app, TemplateCategory, TemplateItem, HelpArticle

# The name of the file that the Client Updater looks for
EXPORT_FILENAME = 'update_seed.json'

def resolve_file_pointer(json_payload, uploads_root):
    """
    If a template's JSON was offloaded to disk as a file pointer (FILE:slug/file.json),
    we open the file and inline the raw JSON so the exported seed artifact is 100% self-contained.
    """
    if not json_payload or not isinstance(json_payload, str):
        return "{}"
        
    if json_payload.startswith('FILE:'):
        pointer_path = json_payload.split('FILE:')[1].strip()
        target_path = os.path.join(uploads_root, 'system_templates', pointer_path)
        
        if os.path.exists(target_path) and os.path.isfile(target_path):
            try:
                with open(target_path, 'r', encoding='utf-8') as pf:
                    return pf.read()
            except Exception as e:
                print(f"   [!] Warning: Failed to read pointer file {target_path}: {e}")
        else:
            print(f"   [!] Warning: Broken file pointer in DB pointing to missing disk file: {target_path}")
        return "{}"
        
    return json_payload

def make_assets_portable(content_str):
    """
    Surgically cleanses poisoned Smart Thumbnail proxy URLs (/api/thumbnail?url=...) 
    sitting inside legacy database records, unquotes their inner targets to pure paths (/uploads/...),
    and strips absolute host domains to ensure 100% portable client ingestion.
    """
    if not content_str or not isinstance(content_str, str):
        return content_str
        
    # 1. Surgical Proxy Unwrapper & Unquoter
    def proxy_replacer(match):
        try:
            raw_encoded = match.group(1)
            clean_path = unquote(raw_encoded)
            if not clean_path.startswith('/'):
                clean_path = '/' + clean_path
            return clean_path
        except:
            return match.group(0)

    content_str = re.sub(r'https?://[^/]+/api/thumbnail\?url=([^"\'\s>\}&]+)', proxy_replacer, content_str, flags=re.IGNORECASE)
    content_str = re.sub(r'/api/thumbnail\?url=([^"\'\s>\}&]+)', proxy_replacer, content_str, flags=re.IGNORECASE)
    content_str = re.sub(r'thumbnail\?url=([^"\'\s>\}&]+)', proxy_replacer, content_str, flags=re.IGNORECASE)

    # 2. Convert absolute app origin URLs to pure relative paths
    content_str = re.sub(r'https?://[^/]+/uploads/system_templates/', '/uploads/system_templates/', content_str, flags=re.IGNORECASE)
    content_str = re.sub(r'https?://[^/]+/uploads/', '/uploads/', content_str, flags=re.IGNORECASE)
    
    return content_str

def export_seed_data():
    print("\n" + "="*50)
    print(f" 🚀 STARTING TEMPLATE & HELP Q&A EXPORT TO {EXPORT_FILENAME}")
    print("="*50)
    
    base_dir = os.path.abspath(os.path.dirname(__file__))
    uploads_folder = os.path.join(base_dir, 'uploads')
    
    with app.app_context():
        uploads_folder = app.config.get('UPLOAD_FOLDER', uploads_folder)
        
        try:
            # 1. Extract Categories
            categories = TemplateCategory.query.all()
            cat_list = []
            cat_map = {} 
            
            for cat in categories:
                if "saved section" not in cat.name.lower() and "my save" not in cat.name.lower():
                    cat_list.append({"name": cat.name})
                    cat_map[cat.id] = cat.name
                
            print(f"📦 Found {len(cat_list)} portable Categories.")

            # 2. Extract Templates
            templates = TemplateItem.query.all()
            tpl_list = []
            
            for tpl in templates:
                cat_name = cat_map.get(tpl.category_id, "Uncategorized")
                raw_project_json = resolve_file_pointer(tpl.project_data_json, uploads_folder)
                
                clean_html = make_assets_portable(tpl.content_html or "")
                clean_json = make_assets_portable(raw_project_json)
                clean_thumb = make_assets_portable(tpl.thumbnail_url or "")
                
                tpl_list.append({
                    "name": tpl.name,
                    "category_name": cat_name,
                    "description": getattr(tpl, 'description', ""),
                    "type": getattr(tpl, 'type', "template"),
                    "content_html": clean_html,
                    "project_data_json": clean_json,
                    "thumbnail_url": clean_thumb
                })
                
            print(f"📄 Successfully processed {len(tpl_list)} Templates.")

            # ========================================================
            # --- NEW: EXTRACT HELP ARTICLES FOR SEEDING ---
            # ========================================================
            articles = HelpArticle.query.all()
            art_list = []
            for a in articles:
                art_list.append({
                    "question": a.question,
                    "answer": a.answer,
                    "category": a.category,
                    "is_active": a.is_active
                })
            print(f"❓ Successfully processed {len(art_list)} Knowledge Base Q&A Articles.")

            # 3. Assemble the JSON payload
            export_data = {
                "categories": cat_list,
                "templates": tpl_list,
                "help_articles": art_list # Injected into the application seed dictionary
            }

            # 4. Write to file
            export_path = os.path.join(base_dir, EXPORT_FILENAME)
            
            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=4)
                
            print("\n✅ EXPORT SUCCESSFUL!")
            print(f"📂 File saved to: {export_path}")
            print("\nNext Steps for publishing an update:")
            print("1. Copy 'update_seed.json' into your update .zip file.")
            print("2. Upload the .zip to your Owner server.\n")

        except Exception as e:
            print(f"\n❌ EXPORT FAILED: {str(e)}")

if __name__ == "__main__":
    export_seed_data()