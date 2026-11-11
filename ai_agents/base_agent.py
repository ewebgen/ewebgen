# ==============================================================================
# Copyright (c) 2026 Orgif Solutions LLP. All rights reserved. For info visit ewebgen.com
#
# This file is part of the Editable Website Developer AI Agent project and Owned by Orgif Solutions LLP.
# Proprietary and confidential.
# ==============================================================================

import re
import json
import uuid
import zipfile
import os
import base64
from datetime import datetime, timezone, timedelta

# Import models and core services
from database_setup import app, db, User, SystemSettings, UserActivityLog, UNSAVED_DIR
from services_core import (
    enrich_html_content, 
    ensure_links_are_proxied,
    PHPGeneratorService, 
    DEFAULT_HTACCESS, 
    localize_html_images, 
    save_new_state
)
from services_image_providers import ImageProviderService

class BaseAIAgent:
    """
    Holds shared processing logic, JSON repair, and Universal Design Guardrails for all AI agents.
    """
    
    @staticmethod
    def inject_master_design_mandate(base_instruction, provider_name="AI"):
        """
        CRITICAL DESIGN ENFORCER:
        Superimposes the 'Old Version' award-winning design guardrails onto any system prompt.
        This guarantees that even if database settings are generic, output designs are elite.
        """
        master_mandate = f"""

[MASTER FRONTEND ARCHITECT MANDATE - NON-NEGOTIABLE DESIGN RULES FOR {provider_name.upper()}]:
You are an elite, award-winning Lead Frontend Architect and UI/UX Designer. You are strictly FORBIDDEN from generating plain, boring, dated, or basic HTML layouts. Every website you build MUST look like a $10,000 bespoke production web application.

1. PURE VANILLA CSS EXCELLENCE (NO TAILWIND/BOOTSTRAP):
   - Write extensive, professional, verbose CSS directly inside the <style> tag.
   - Use CSS Custom Properties (:root variables) for a cohesive color palette (Primary, Secondary, Accent, Dark Neutral, Light Neutral).
   - Use CSS Grid and Flexbox exclusively for modern, responsive layouts. Never use table layouts or float-based grids.

2. PREMIUM COLOR SCHEME, VISUAL AESTHETICS & ALTERNATING SECTIONS (MANDATORY FIXES):
   - COLOR PALETTE & ALTERNATING SECTIONS: You MUST use a highly attractive, modern, and professional color scheme. STRICTLY AVOID dull brown or beige color combinations. Use clean, premium palettes (e.g., Deep Navy/Slate, Crisp White/Off-White backgrounds, vibrant modern accents like Tech Blue, Emerald, or Royal Purple). CRITICAL: You MUST alternate the background colors of adjacent `<section>` elements to visually distinguish them (e.g., Section 1: Pure White #FFFFFF, Section 2: Very Light Gray/Blue #F8FAFC, Section 3: Pure White). Never make all adjacent sections the exact same background color.
   - GLASSMORPHISM: Apply subtle frosted-glass effects to navigation bars, cards, and floating badges using `background: rgba(255, 255, 255, 0.7); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.3);`.
   - DEPTH & ELEVATION: Use layered, soft box-shadows (e.g., `box-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.08), 0 4px 6px -2px rgba(0, 0, 0, 0.04);`). Never use harsh black borders or solid drop shadows.
   - ROUNDED GEOMETRY: Enforce modern border-radius on all elements (Buttons: 8px to 999px pill; Cards: 16px to 24px; Images: 12px to 20px).
   - STRICT IMAGE ALIGNMENT: All images MUST be perfectly aligned. When placing text next to an image, you MUST wrap them in a Flexbox container (`display: flex; align-items: center; justify-content: space-between; gap: 3rem; flex-wrap: wrap;`) to ensure images never break the layout or misalign. All images must explicitly have `max-width: 100%; height: auto; object-fit: cover; display: block; border-radius: 12px;`.

3. MICRO-INTERACTIONS & MOTION:
   - Include smooth CSS transitions (`transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);`) on all clickable elements.
   - Design interactive hover states for cards (slight upward transform `-translateY(6px)`, enhanced shadow) and buttons (subtle glow, background shift).
   - Define CSS `@keyframes` animations (e.g., smooth fade-in-up for hero titles, floating animations for hero imagery).

4. TYPOGRAPHY & ICONOGRAPHY:
   - Structure typography with clear visual hierarchy (Hero headings: 3.5rem - 4.5rem font-weight 800; Section titles: 2.2rem - 2.8rem font-weight 700; Body: 1.05rem - 1.15rem line-height 1.7).
   - Use rich visual cards with FontAwesome icons (`<i class="fas fa-icon"></i>`) instead of plain text lists.

5. CONTENT DENSITY, HERO IMAGES & STRUCTURE (ANTI-LAZY MANDATE):
   - BEAUTIFUL HERO IMAGE: Every page MUST start with a breathtaking, high-impact Hero Section. Depending on the layout provided, use either a striking side-by-side layout with an `<img>` tag (`src='/images/placeholder.jpg'`), OR a majestic full-width CSS `background-image: url('/images/placeholder.jpg')` with a dark overlay and white text. Make the hero visually striking.
   - MULTIPLE SECTIONS REQUIRED: LAZY GENERATION IS STRICTLY FORBIDDEN. Every single page MUST contain a MINIMUM of 4 to 6 fully fleshed-out, distinct `<section>` blocks. You will fail if you return a page with only 1 or 2 sections.
   - Include complete, realistic, industry-specific copy. NEVER use "Lorem Ipsum" or placeholder text like "[Insert Name Here]".
   - Ensure spacious, breathable layout padding (`padding: 80px 0;` to `100px 0;` for main sections).
"""
        if not base_instruction:
            return master_mandate
        if "[MASTER FRONTEND ARCHITECT MANDATE" in base_instruction:
            return base_instruction
            
        return f"{base_instruction.strip()}\n\n{master_mandate}"

    @staticmethod
    def enforce_typography_and_icons(html_content):
        """
        POST-PROCESSING DESIGN ENFORCER:
        Checks if the LLM forgot to include premium Google Fonts or FontAwesome.
        Always injects the Global AI Dropdown Fix to prevent broken submenus.
        """
        if not html_content or not isinstance(html_content, str):
            return html_content
            
        # 1. Font injection (only if missing)
        fonts = """
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
"""
        if "plus jakarta" not in html_content.lower():
            if "</head>" in html_content.lower():
                html_content = re.sub(r'</head>', f"{fonts}\n</head>", html_content, flags=re.IGNORECASE)
            elif "<body" in html_content.lower():
                html_content = f"{fonts}\n{html_content}"

        # 2. Mandatory Structural CSS Fixes (ALWAYS INJECTED)
        structural_css = """
    <style>
        /* --- GLOBAL DESIGN ENFORCER --- */
        :root {
            --font-primary: 'Plus Jakarta Sans', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            --font-heading: 'Outfit', 'Plus Jakarta Sans', sans-serif;
        }
        body {
            font-family: var(--font-primary) !important;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }
        h1, h2, h3, h4, h5, h6, .navbar-brand, .hero-title {
            font-family: var(--font-heading) !important;
            letter-spacing: -0.02em;
        }
        /* --- AI DROPDOWN MENU FIX --- */
        nav ul { list-style: none; padding: 0; margin: 0; }
        nav li { position: relative; }
        nav li > ul, .dropdown-menu, .nav-item > .dropdown-menu, .sub-menu, .submenu, [class*="dropdown-menu"], nav li > div[class*="dropdown"] {
            position: absolute !important;
            top: 100% !important;
            left: 0 !important;
            min-width: 200px;
            z-index: 10000 !important;
            display: none !important;
            background: #fff;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            border: 1px solid #eee;
            border-radius: 4px;
        }
        nav li:hover > ul, .dropdown:hover > .dropdown-menu, .nav-item:hover > .dropdown-menu, li:hover > .sub-menu, li:hover > .submenu, [class*="dropdown"]:hover > [class*="dropdown-menu"], nav li:hover > div[class*="dropdown"], .dropdown-menu.show {
            display: block !important;
        }
    </style>
"""
        if "</head>" in html_content.lower():
            html_content = re.sub(r'</head>', f"{structural_css}\n</head>", html_content, flags=re.IGNORECASE)
        elif "<body" in html_content.lower():
            html_content = f"{structural_css}\n{html_content}"
            
        return html_content

    @staticmethod
    def robust_json_repair(text):
        """
        Aggressively extracts and repairs truncated or malformed JSON from LLM outputs.
        Specifically designed to protect massive CSS style blocks from being corrupted.
        """
        if not text or not isinstance(text, str):
            raise Exception("Empty AI response received.")

        # 1. Surgical Extraction: Find the first '{' and ignore everything before it
        start_idx = text.find('{')
        if start_idx == -1:
            raise Exception("No JSON object found in AI response.")
            
        json_str = text[start_idx:].strip()
        
        # 2. Scrubbing: Remove trailing markdown markers or explanatory notes
        json_str = re.sub(r'```.*$', '', json_str, flags=re.DOTALL | re.MULTILINE).strip()
        if json_str.endswith('`'):
            json_str = json_str.rstrip('`').strip()

        # 3. Direct Parse Attempt
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # 4. Bracket & Quote Balancing (Handles token truncation during long CSS generation)
        open_braces = 0
        open_brackets = 0
        in_string = False
        escape_next = False
        cleaned_chars = []

        for char in json_str:
            cleaned_chars.append(char)
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == '{': open_braces += 1
                elif char == '}': open_braces = max(0, open_braces - 1)
                elif char == '[': open_brackets += 1
                elif char == ']': open_brackets = max(0, open_brackets - 1)

        repaired = "".join(cleaned_chars)

        # Close unclosed strings
        if in_string:
            repaired += '"'
            
        # Strip trailing commas before closing braces/brackets
        repaired = re.sub(r',\s*$', '', repaired)
        repaired = re.sub(r',\s*(\}|\])', r'\1', repaired)

        # Balance structural brackets
        while open_brackets > 0:
            repaired += ']'
            open_brackets -= 1
        while open_braces > 0:
            repaired += '}'
            open_braces -= 1

        try:
            return json.loads(repaired)
        except Exception as final_e:
            # Emergency fallback regex extraction of files dictionary if JSON is deeply malformed
            files_match = re.findall(r'"([^"]+\.(?:html|css|js|php))"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if files_match:
                fallback_dict = {}
                for fname, fcontent in files_match:
                    try:
                        fallback_dict[fname] = bytes(fcontent, "utf-8").decode("unicode_escape")
                    except:
                        fallback_dict[fname] = fcontent
                if fallback_dict:
                    return {"files": fallback_dict}
            raise Exception(f"Failed to repair JSON output: {str(final_e)}")

    @staticmethod
    def replace_image_placeholders(html_content, prompt_query="business modern"):
        """
        Replaces missing, empty, or broken image placeholders with dynamic high-res stock imagery.
        Guarantees that generated websites look visually rich immediately upon rendering.
        """
        if not html_content or not isinstance(html_content, str):
            return html_content
            
        def img_replacer(match):
            full_tag = match.group(0)
            src_val = match.group(2)
            
            # If src is empty, placeholder, or broken, fetch a high quality stock image
            if not src_val or 'placehold' in src_val or 'example.com' in src_val or src_val.strip() == '':
                # Extract alt text to use as intelligent stock photo search keyword
                alt_match = re.search(r'alt\s*=\s*[\'"]([^\'"]+)[\'"]', full_tag, re.IGNORECASE)
                search_query = alt_match.group(1) if alt_match and len(alt_match.group(1).strip()) > 2 else prompt_query
                
                new_url = ImageProviderService.get_image_url(search_query, width=1000, height=700)
                if new_url:
                    return re.sub(r'src\s*=\s*([\'"])[^\'"]*(\1)', f'src="{new_url}"', full_tag, flags=re.IGNORECASE)
            return full_tag

        try:
            return re.sub(r'<img[^>]+src\s*=\s*([\'"])(.*?)\1[^>]*>', img_replacer, html_content, flags=re.IGNORECASE)
        except Exception as e:
            print(f"⚠️ Image replacement warning: {e}")
            return html_content

    @staticmethod
    def finalize_response(full_text, user_id=None, is_new_generation=False):
        """
        Standardized finalizer pipeline executed across all AI agents.
        Applies JSON repair, markdown stripping, asset localization, typography enforcement, and database logging.
        """
        try:
            parsed = BaseAIAgent.robust_json_repair(full_text)
            
            final_files = parsed.get('files', {})
            current_page = parsed.get('current_page', 'index.html')
            
            if not final_files and isinstance(parsed, dict):
                # Handle cases where model returns raw keys as filenames
                for k, v in parsed.items():
                    if str(k).endswith(('.html', '.css', '.js', '.php')):
                        final_files[k] = str(v)

            if not final_files:
                raise Exception("AI did not return any valid HTML/CSS files in the JSON response.")

            # --- PROCESS AND ENRICH EVERY GENERATED FILE ---
            for filename, content in list(final_files.items()):
                if isinstance(content, str):
                    
                    # --- CSS FIX: Strip Markdown from within JSON values ---
                    # Gemini often wraps its string values in ```css ... ``` inside the JSON.
                    # This removes those wrappers so the CSS/HTML is clean and valid.
                    content = re.sub(r'^```[a-zA-Z]*\s*', '', content)
                    content = re.sub(r'\s*```$', '', content)
                    content = content.strip()
                    
                    if filename.endswith('.html'):
                        # 1. Enforce Premium Typography & FontAwesome
                        content = BaseAIAgent.enforce_typography_and_icons(content)
                        # 2. Replace broken/empty image placeholders with dynamic high-res stock photos
                        content = BaseAIAgent.replace_image_placeholders(content)
                        # 3. Apply standard system enrichment (scripts, editable classes, proxying)
                        content = enrich_html_content(content)
                        content = ensure_links_are_proxied(content, list(final_files.items()))
                        content = localize_html_images(content)
                        
                    final_files[filename] = content

            # --- SAVE STATE TO USER WORKSPACE ---
            if user_id:
                try:
                    save_new_state(final_files, user_id=user_id)
                except Exception as save_err:
                    print(f"⚠️ Workspace save warning: {save_err}")

            # --- USER ACTIVITY & LIMIT LOGGING ---
            if user_id:
                with app.app_context():
                    user = db.session.get(User, user_id)
                    if user:
                        if is_new_generation:
                            allow_ai_keys = user.role == 'admin' or user.allow_own_api_keys or (user.user_type and user.user_type.allow_own_api_keys)
                            using_own_key = False
                            if allow_ai_keys:
                                try:
                                    settings_json = json.loads(user.user_settings_json or '{}')
                                    if any(settings_json.get(k) for k in ['gemini_api_key', 'openai_api_key', 'claude_api_key', 'deepseek_api_key', 'xai_api_key', 'meta_api_key', 'perplexity_api_key']):
                                        using_own_key = True
                                except: pass
                            
                            if not using_own_key:
                                user.websites_generated = (user.websites_generated or 0) + 1
                                
                            # --- GOAL 1 FIX: Generate reference_id and save zip for history loader ---
                            ref_id = uuid.uuid4().hex
                            try:
                                unsaved_filename = f"unsaved_{user_id}_{ref_id}.zip"
                                unsaved_path = os.path.join(app.config.get('UNSAVED_DIR', UNSAVED_DIR), unsaved_filename)
                                with zipfile.ZipFile(unsaved_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                                    for fname, content in final_files.items():
                                        if isinstance(content, str):
                                            if content.startswith('data:'):
                                                try:
                                                    header, encoded = content.split(',', 1)
                                                    zf.writestr(fname, base64.b64decode(encoded))
                                                except: pass
                                            else:
                                                zf.writestr(fname, content.encode('utf-8'))
                            except Exception as zip_err:
                                print(f"⚠️ Unsaved project zip error: {zip_err}")
                                ref_id = None
                                
                            # Name the entry after the generated site so it is easy to find in My History
                            site_title = ""
                            try:
                                idx_html = final_files.get('index.html') or next((v for k, v in final_files.items() if k.endswith('.html') and isinstance(v, str)), '')
                                m = re.search(r'<title[^>]*>(.*?)</title>', idx_html or '', re.I | re.S)
                                site_title = re.sub(r'\s+', ' ', m.group(1)).strip()[:120] if m else ""
                            except Exception:
                                site_title = ""
                            page_count = len([k for k in final_files if k.endswith(('.html', '.php'))])
                            db.session.add(UserActivityLog(
                                user_id=user_id,
                                action_type='website_generated',
                                description=(f"New website: {site_title}" if site_title else "Generated a new website via AI prompt") + f" ({page_count} page{'s' if page_count != 1 else ''})",
                                reference_id=ref_id
                            ))
                        else:
                            db.session.add(UserActivityLog(
                                user_id=user_id,
                                action_type='project_edited',
                                description="Updated website structure via AI Prompt."
                            ))
                        db.session.commit()
            
            return f"data: {json.dumps({'type': 'complete', 'files': final_files, 'current_page': current_page})}\n\n"
            
        except Exception as e:
            print(f"❌ [BaseAIAgent Finalize Error]: {str(e)}", flush=True)
            return f"data: {json.dumps({'type': 'error', 'message': f'Generation Error: {str(e)}'})}\n\n"