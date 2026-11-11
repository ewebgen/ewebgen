import os
import json
import uuid
import random
from datetime import datetime, timezone, timedelta
from flask_login import current_user

# Import app context and models from database_setup
from database_setup import app, db, SystemSettings, User, UserActivityLog
from services_core import ConnectionManager, save_user_log, get_current_uid, FTPEncryptionService

# Import the isolated Image Providers
from services_image_providers import ImageProviderService

# Import our dedicated AI Agents (Strategy Pattern) & Base Guardrails
from ai_agents.base_agent import BaseAIAgent
from ai_agents.agent_gemini import GeminiAgent
from ai_agents.agent_openai import OpenAIAgent
from ai_agents.agent_claude import ClaudeAgent
from ai_agents.agent_deepseek import DeepSeekAgent
from ai_agents.agent_grok import GrokAgent
from ai_agents.agent_meta import MetaAgent
from ai_agents.agent_perplexity import PerplexityAgent

# =======================================================
# SECURITY HELPER: DECRYPTION FOR AI KEYS
# =======================================================
def decrypt_sensitive_data(val):
    if not val: return val
    try:
        dec = FTPEncryptionService.decrypt_credentials(val)
        if dec and 'sec_val' in dec: return dec['sec_val']
    except: pass
    return val # Fallback to plain text if not encrypted or legacy

# =======================================================
# UNIFIED AI GENERATION SERVICE (TRAFFIC CONTROLLER)
# =======================================================

class AIGenerationService:
    
    @staticmethod
    def get_settings():
        with app.app_context():
            return SystemSettings.query.first()

    @staticmethod
    def get_rotated_key(settings, provider, current_active_count):
        keys = []
        try:
            # SECURITY FIX: Decrypt the master provider keys JSON
            decrypted_json = decrypt_sensitive_data(settings.provider_keys_json)
            key_data = json.loads(decrypted_json or '{}')
            keys = key_data.get(provider, [])
        except: keys = []
        
        # SECURITY FIX: Decrypt legacy individual keys
        legacy_key = decrypt_sensitive_data(getattr(settings, f"{provider}_api_key", None))
        if legacy_key and legacy_key not in keys:
            keys.insert(0, legacy_key)
            
        if not keys: return None
        
        limit = settings.concurrent_limit if settings.concurrent_limit_enabled else 1000
        if limit < 1: limit = 5
        
        if current_active_count < 1:
            return keys[0]
            
        agent_index = (current_active_count - 1) // limit
        key_to_use = keys[agent_index % len(keys)]
        return key_to_use

    @staticmethod
    def generate_stream(history_messages, system_instruction, user_id=None, is_new_generation=False):
        """Builds the prompt and routes the stream request to the correct AI agent with Queue support."""
        user = None
        using_own_key = False
        
        if user_id:
            with app.app_context():
                user = User.query.get(user_id)
                if user:
                    allow_own_keys = user.role == 'admin' or user.allow_own_api_keys or (user.user_type and user.user_type.allow_own_api_keys)
                    if allow_own_keys:
                        try:
                            sys_settings = SystemSettings.query.first()
                            sys_provider = sys_settings.ai_provider.strip().lower() if sys_settings and sys_settings.ai_provider else 'gemini'
                            
                            user_settings = {}
                            if hasattr(user, 'user_settings_json') and user.user_settings_json:
                                user_settings = json.loads(user.user_settings_json)
                                
                            pref_provider = user_settings.get('ai_provider', 'default').strip().lower()
                            
                            if pref_provider != 'default':
                                # SECURITY FIX: Decrypt user's personal API keys
                                key1 = decrypt_sensitive_data(user_settings.get(f'{pref_provider}_api_key'))
                                if key1 and len(str(key1).strip()) > 5: 
                                    using_own_key = True
                                    
                                if not using_own_key and hasattr(user, 'api_keys_json') and user.api_keys_json:
                                    api_keys = json.loads(user.api_keys_json)
                                    key2 = decrypt_sensitive_data(api_keys.get(f'{pref_provider}_api_key') or api_keys.get(pref_provider))
                                    if key2 and len(str(key2).strip()) > 5: 
                                        using_own_key = True
                                        
                                if not using_own_key and hasattr(user, f'{pref_provider}_api_key'):
                                    key3 = decrypt_sensitive_data(getattr(user, f'{pref_provider}_api_key'))
                                    if key3 and len(str(key3).strip()) > 5: 
                                        using_own_key = True
                                        
                                if not using_own_key and pref_provider == 'openai':
                                    if user_settings.get('openai_api_key') or (hasattr(user, 'openai_api_key') and user.openai_api_key):
                                        using_own_key = True
                        except: pass
                
                    if user.user_type and user.user_type.project_limit != -1 and not using_own_key:
                        total_allowed = user.user_type.project_limit + getattr(user, 'extra_project_limit', 0)
                        if is_new_generation and user.websites_generated >= total_allowed:
                            yield f"data: {json.dumps({'type': 'error', 'message': 'Website Generation Limit Reached. Please upgrade your plan.'})}\n\n"
                            return

        settings = AIGenerationService.get_settings()
        provider = settings.ai_provider.strip().lower() if settings and settings.ai_provider else 'gemini'
        
        allow_own_keys = False
        user_api_key = None
        user_default_pages = None
        user_model_val = None
        user_token_val = None
        
        if user:
            allow_own_keys = user.role == 'admin' or user.allow_own_api_keys or (user.user_type and user.user_type.allow_own_api_keys)
            
        if allow_own_keys and user and user.user_settings_json:
            try:
                user_settings = json.loads(user.user_settings_json)
                pref_provider = user_settings.get('ai_provider', 'default').strip().lower()
                
                if pref_provider and pref_provider != 'default':
                    provider = pref_provider
                
                    # SECURITY FIX: Decrypt user's personal API keys for injection
                    user_key = decrypt_sensitive_data(user_settings.get(f'{provider}_api_key'))
                    if user_key:
                        user_api_key = user_key
                        
                    user_model_val = user_settings.get(f'user_model_{provider}')
                    user_token_val = user_settings.get(f'user_token_{provider}')
                    
                if user_settings.get('default_pages_mode') == 'manual':
                    pages_json_str = user_settings.get('default_pages_json')
                    if pages_json_str:
                        user_default_pages = json.loads(pages_json_str)
            except Exception as e:
                print(f"Error parsing user personal AI settings: {e}")

        custom_inst = getattr(settings, f"inst_{provider}", None)
        design_rules = getattr(settings, "design_guidelines", None)
        
        # Determine specific Model
        if user_model_val and str(user_model_val).strip().lower() not in ['default', '']:
            custom_model = user_model_val
        else:
            custom_model = getattr(settings, f"model_{provider}", None)

        # Determine specific Token Limit
        if user_token_val and str(user_token_val).strip().lower() not in ['default', '']:
            token_limit_raw = user_token_val
        else:
            token_limit_raw = getattr(settings, f"token_limit_{provider}", None)

        final_max_tokens = None
        if token_limit_raw and str(token_limit_raw).strip().lower() not in ['maximum', 'default', '']:
            try:
                final_max_tokens = int(str(token_limit_raw).strip())
            except ValueError:
                pass

        # ========================================================
        # 1. BASE INSTRUCTION (Website Context & Analytics)
        # ========================================================
        final_instruction = system_instruction
        
        # ========================================================
        # 2. CORE DEVELOPER MANDATES & ANTI-UGLY / ANTI-LAZY FIXES
        # ========================================================
        base_developer_rules = (
            "You are an elite, award-winning Lead Frontend Architect and Senior Full-Stack Web Developer. "
            "Your task is to generate or **modify the current site structure** to fulfill the user's request. "
            "CRITICAL: If the user is only asking for a small change, you **MUST** preserve the structure, links, and overall design of the existing site. "
            
            "**PRESERVATION OF MANUAL EDITS (IMPORTANT):** "
            "1. If the user has manually applied styling via inline styles or span tags, you **MUST** preserve these specific tags. "
            "2. When modifying sections, ensure distinct sub-sections (cards, feature boxes, image-text wrappers) remain distinct elements. "
            
            "**DESIGN, LAYOUT & BEAUTY MANDATE (ANTI-UGLY FIX):** "
            "3. Every website you build MUST look like a $10,000 bespoke production agency web application with stunning modern aesthetics. "
            "4. Use clean, structured HTML5 and semantic elements (`<header>`, `<nav>`, `<main>`, `<section>`, `<footer>`). "
            "5. The design in `styles.css` **MUST** be clean, responsive (mobile-first approach), utilizing CSS Grid and Flexbox exclusively. NEVER use floating elements or crude table layouts. "
            
            "6. **PREMIUM DIVERSE COLOR CONTRAST & ALTERNATING SECTIONS MANDATE:** You MUST use a highly attractive, modern, and professional color scheme. STRICTLY AVOID dull brown or sticking to just basic white and grey. Use rich, beautiful, and diverse professional palettes (e.g., Deep Navy, Soft Pastel Blues, Warm Sand, Forest Green, Elegant Charcoal, Vibrant Tech Blues). **CRITICAL: You MUST alternate the background colors of adjacent `<section>` elements to visually distinguish them. DO NOT use only 2 colors. Give each section a distinctly beautiful and professional background color that fits the brand, including the Hero section.** Ensure high contrast between text and background. NEVER use a white background with white text, or a black background with black text."
            
            "7. **SEO & RESPONSIVE MANDATE:** Include `<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">`, a contextual `<title>`, and `<meta name=\"description\" content=\"...\">` in the `<head>` of EVERY HTML file. "
            
            "8. **CONTACT FORM MANDATE (CRITICAL):** Whenever you generate a form, set its action to 'send_mail.php' (`<form action=\"send_mail.php\" method=\"POST\">`). NEVER use `action=\"#\"`. "
            
            "9. **STRICT MULTI-SECTION MANDATE (ANTI-LAZY FIX FOR 1-SECTION BUG):** "
            "Apart from the `<header>` and `<footer>`, EVERY single HTML page (including About, Services, Contact, FAQ, etc.) **MUST** contain a **MINIMUM of 4 to 6 distinct content `<section>` elements** in its `<body>`. "
            "Generating pages with only 1 section is strictly prohibited and results in system failure. Each section must be fully fleshed out with rich headings, subtitles, descriptive text cards, interactive buttons, and high-impact images. "
            "The ONLY exception to the 4-section minimum rule is Terms and Privacy pages (`terms.html`, `policy.html`), which may have 2 comprehensive text sections.\n\n"
            
            "**GUARANTEED HERO IMAGE MANDATE (CRITICAL):** "
            "10. Every homepage/landing page (`index.html`) **MUST** feature a breathtaking, high-impact Hero Section at the top. Depending on the layout blueprint provided, the Hero section MUST include a prominent, beautifully styled Hero Image—either as an `<img>` tag integrated side-by-side with text, OR as a stunning full-width CSS `background-image: url('/images/placeholder.jpg')` with a dark overlay and centered typography. "
            "Every other page must also include at least one prominent feature or header banner image.\n\n"
            
            "**BULLETPROOF IMAGE ALIGNMENT MANDATE (CRITICAL FIX FOR MISALIGNED IMAGES):** "
            "11. All images (`<img>` tags) across all sections **MUST** be properly wrapped and aligned. Every image must be assigned CSS classes or inline styling guaranteeing proper proportions: `max-width: 100%; height: auto; object-fit: cover; border-radius: 12px; display: block;`. "
            "For side-by-side image-and-text layouts, you MUST wrap them in a Flexbox container (`display: flex; align-items: center; justify-content: space-between; gap: 3rem; flex-wrap: wrap;`) so images never overflow, distort, or misalign on mobile screens.\n\n"
            
            "**ABSOLUTE JSON RULE (CRITICAL FOR PARSING):** Do NOT wrap the CSS or HTML code inside markdown code blocks (e.g., ```html or ```css) *within* the JSON string values. The JSON values must contain RAW text code only. The overall output must be pure, standard JSON.\n\n"
            
            "\nThe output MUST be **ONLY** a single JSON object. Do NOT include any explanatory text outside the JSON. "
            "The exact JSON schema representing the required pages will be provided in the subsequent instructions based on dynamic settings. "
            "When a user asks to modify something, you **MUST** return the **ENTIRE, UPDATED** JSON structure with all files. "
            "All HTML files **must** link to the external 'styles.css' file in their `<head>` like this: `<link rel=\"stylesheet\" href=\"styles.css\">` and **must NOT** contain any `<style>` tags. "
            "CRITICAL MANDATE: For every major content section on every page, you **MUST** include at least one `<img>` tag with a **detailed and specific** `alt` attribute describing the desired image content. Use src='/images/placeholder.jpg'."
        )
        final_instruction += f"\n\n{base_developer_rules}"

        # ========================================================
        # 3. ADMIN SPECIFIC OVERRIDES
        # ========================================================
        if custom_inst and custom_inst.strip():
            final_instruction += f"\n\n[ADDITIONAL ADMIN INSTRUCTIONS - YOU MUST FOLLOW THESE IN ADDITION TO ALL THE RULES ABOVE]:\n{custom_inst.strip()}"
            
        if design_rules and design_rules.strip():
            final_instruction += f"\n\n[ADDITIONAL GLOBAL DESIGN GUIDELINES - YOU MUST FOLLOW THESE IN ADDITION TO ALL THE RULES ABOVE]:\n{design_rules.strip()}"
            
        # ========================================================
        # 4. PAGE GENERATION RULES
        # ========================================================
        default_pages = []
        if user_default_pages:
            default_pages = user_default_pages
        elif hasattr(settings, 'default_pages_json') and settings.default_pages_json:
            try:
                default_pages = json.loads(settings.default_pages_json)
            except: pass
            
        if not default_pages:
            default_pages = [
                {"name": "index.html", "description": "Homepage with arresting hero banner, feature highlights, testimonials, and CTA", "location": "menu"},
                {"name": "about.html", "description": "About Us with company story, mission, team cards, and stats", "location": "menu"},
                {"name": "services.html", "description": "Products or Services with detailed pricing cards and feature breakdown", "location": "menu"},
                {"name": "contact.html", "description": "Contact Us with interactive contact form, map placeholder, and support info", "location": "menu"},
                {"name": "faq.html", "description": "FAQ accordion and customer help resources", "location": "menu"},
                {"name": "terms.html", "description": "Terms and Conditions", "location": "footer"},
                {"name": "policy.html", "description": "Privacy Policy", "location": "footer"}
            ]

        page_list_lines = []
        page_schema_lines = []
        current_page_fallback = "index.html"

        for i, p in enumerate(default_pages):
            p_name = p.get("name", "").strip()
            p_desc = p.get("description", "").strip()
            p_loc = str(p.get("location", "menu")).upper()
            
            is_placeholder = False
            
            if not p_name:
                p_name = f"<AI_GENERATED_FILENAME_{i+1}.html>"
                is_placeholder = True
            
            if not p_desc:
                p_desc = "AI MUST DECIDE APPROPRIATE TOPIC/CONTENT BASED ON OVERALL CONTEXT"
                
            if i == 0 and not is_placeholder:
                current_page_fallback = p_name
                
            page_list_lines.append(f"{i+1}. {p_name} ({p_desc}) - Placement: {p_loc}")
            
            if is_placeholder:
                page_schema_lines.append(f'"<REPLACE_WITH_REAL_FILENAME_{i+1}.html>": "... (full HTML content) ..."')
            else:
                page_schema_lines.append(f'"{p_name}": "... (full HTML content for {p_name}) ..."')

        page_list_str = "\n".join(page_list_lines)
        page_schema_example = ',\n            '.join(page_schema_lines)

        mandatory_page_rules = f"""
        CRITICAL PAGE REQUIREMENT: 
        You MUST generate exactly {len(default_pages)} distinct HTML pages. 
        DO NOT BE LAZY. DO NOT omit pages or collapse pages into a single section. Every single page must have 4-6 robust content sections.
        You must create the following pages:
        {page_list_str}
        
        IMPORTANT NAMING RULE (CRITICAL): If any page filename above is a placeholder like "<AI_GENERATED_FILENAME_1.html>", you MUST replace it with a highly relevant, lowercase, hyphenated filename ending in .html (e.g., "portfolio.html", "pricing.html") based on the website context. Do NOT literally output the placeholder text as the JSON key.
        
        CRITICAL HEADER & MOBILE MENU RULES (MUST FOLLOW STRICTLY):
        1. FULL NAVBAR ON EVERY PAGE: You MUST include the completely coded, fully functional top navigation bar (`<nav>`) and `<header>` on EVERY single page.
        2. NO DOUBLE MENU ICONS: For the mobile view, use EXACTLY ONE standard hamburger menu toggle button.
        3. DROPDOWN OVERLAYS (CRITICAL): All navigation dropdown menus MUST have `position: absolute;` and a high `z-index`.
        
        ANTI-TRUNCATION MANDATE (CRITICAL):
        You MUST provide the FULL, UNCOMPRESSED HTML code for every single page. NEVER use comments like "".
        
        CRITICAL SEO METADATA RULE:
        You MUST include highly optimized SEO metadata inside the <head> tag of EVERY page you generate.
        
        CRITICAL OUTPUT FORMAT (JSON SCHEMA ENFORCEMENT): 
        Your response MUST match this structure exactly:
        {{
            {page_schema_example},
            "styles.css": "... (all shared CSS content) ...",
            "current_page": "{current_page_fallback}"
        }}
        """
        final_instruction += f"\n\n{mandatory_page_rules}"

        # ========================================================
        # 5. RIGID HTML & CSS BLUEPRINT MANDATE (ALIGNMENT & HERO FIX)
        # ========================================================
        
        hero_layout_choice = random.choice(['side_by_side', 'full_width_bg'])

        if hero_layout_choice == 'side_by_side':
            hero_skeleton = """
        1. HERO SECTION SKELETON (Use this exact HTML layout for the top of the homepage):
        <section class="hero-section" style="padding: 100px 0; background: linear-gradient(135deg, var(--hero-bg-color-1, #f8fafc) 0%, var(--hero-bg-color-2, #e2e8f0) 100%);">
            <div class="container" style="display: flex; align-items: center; justify-content: space-between; gap: 4rem; flex-wrap: wrap;">
                <div class="hero-content" style="flex: 1; min-width: 300px; text-align: left;">
                    <h1 style="font-size: 3.5rem; font-weight: 800; color: var(--hero-text-color, #0f172a); margin-bottom: 1.5rem; line-height: 1.2;">Stunning Headline For Your Business</h1>
                    <p style="font-size: 1.15rem; color: var(--hero-subtext-color, #475569); margin-bottom: 2rem; line-height: 1.7;">Engaging value proposition and description that hooks visitors immediately upon arrival.</p>
                    <div style="display: flex; gap: 1rem; flex-wrap: wrap;">
                        <a href="contact.html" class="btn-primary" style="background: var(--primary-btn-bg, #0284c7); color: white; padding: 1rem 2rem; border-radius: 999px; text-decoration: none; font-weight: 600; box-shadow: 0 10px 20px rgba(0,0,0,0.25);">Get Started</a>
                        <a href="services.html" class="btn-secondary" style="background: transparent; color: var(--hero-text-color, #0f172a); border: 2px solid var(--hero-text-color, #0f172a); padding: 1rem 2rem; border-radius: 999px; text-decoration: none; font-weight: 600;">Learn More</a>
                    </div>
                </div>
                <div class="hero-image-wrapper" style="flex: 1; min-width: 300px;">
                    <img src="/images/placeholder.jpg" alt="Hero illustration showcasing professional services" style="width: 100%; height: auto; border-radius: 20px; object-fit: cover; box-shadow: 0 20px 40px rgba(0,0,0,0.1);">
                </div>
            </div>
        </section>
        """
        else:
            hero_skeleton = """
        1. HERO SECTION SKELETON (Use this exact HTML layout for the top of the homepage - Wide Image Background):
        <section class="hero-section" style="position: relative; padding: 140px 0; background-color: var(--hero-bg-color-1, #1e293b); background-image: url('/images/placeholder.jpg'); background-size: cover; background-position: center; text-align: center; display: flex; align-items: center; justify-content: center; min-height: 70vh;">
            <div class="hero-overlay" style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(15, 23, 42, 0.65); z-index: 1;"></div>
            <div class="container" style="position: relative; z-index: 2; max-width: 900px; color: #ffffff;">
                <h1 style="font-size: 4rem; font-weight: 800; color: #ffffff; margin-bottom: 1.5rem; line-height: 1.1; text-shadow: 0 4px 10px rgba(0,0,0,0.3);">Engineered for the World's Toughest Jobs</h1>
                <p style="font-size: 1.25rem; color: #f1f5f9; margin-bottom: 2.5rem; line-height: 1.6; text-shadow: 0 2px 5px rgba(0,0,0,0.3);">Manufacturing premium solutions for global shipping, secure storage, and innovative custom architectures.</p>
                <div style="display: flex; gap: 1rem; flex-wrap: wrap; justify-content: center;">
                    <a href="services.html" class="btn-primary" style="background: var(--primary-btn-bg, #ea580c); color: white; padding: 1rem 2.5rem; border-radius: 8px; text-decoration: none; font-weight: bold; box-shadow: 0 10px 20px rgba(0,0,0,0.2);">View Products</a>
                    <a href="contact.html" class="btn-secondary" style="background: transparent; color: white; border: 2px solid #ffffff; padding: 1rem 2.5rem; border-radius: 8px; text-decoration: none; font-weight: bold;">Request Quote</a>
                </div>
            </div>
        </section>
        """

        premium_html_css_blueprint = """
        ======================================================================
        CRITICAL ALIGNMENT & PREMIUM LAYOUT RULES (YOU MUST FOLLOW THIS EXACTLY)
        ======================================================================
        AI models frequently fail at CSS alignment for Hero sections, Images, and Forms. You MUST strictly use the exact CSS classes and structure provided below.
        """ + hero_skeleton + """
        2. SECTION IMAGE-TEXT WRAPPER SKELETON (Use for alternating content sections):
        <section class="content-section" style="padding: 90px 0; background-color: var(--section-bg-color, #ffffff);">
            <div class="container" style="display: flex; align-items: center; justify-content: space-between; gap: 4rem; flex-wrap: wrap;">
                <div style="flex: 1; min-width: 300px;">
                    <img src="/images/placeholder.jpg" alt="Detailed feature showcase" style="width: 100%; height: auto; border-radius: 16px; object-fit: cover; box-shadow: 0 15px 35px rgba(0,0,0,0.08);">
                </div>
                <div style="flex: 1; min-width: 300px; text-align: left;">
                    <h2 style="font-size: 2.5rem; font-weight: 700; color: var(--heading-color, #0f172a); margin-bottom: 1.2rem;">Built For Maximum Performance</h2>
                    <p style="font-size: 1.1rem; color: var(--text-color, #475569); line-height: 1.7; margin-bottom: 1.5rem;">Detailed explanatory text demonstrating benefits, key features, and professional quality.</p>
                    <ul style="list-style: none; padding: 0; display: flex; flex-direction: column; gap: 0.8rem; color: var(--text-color, #334155); font-weight: 500;">
                        <li><i class="fas fa-check-circle" style="color: var(--accent-color, #0284c7); margin-right: 8px;"></i> Advanced professional workflow</li>
                        <li><i class="fas fa-check-circle" style="color: var(--accent-color, #0284c7); margin-right: 8px;"></i> Fully responsive mobile architecture</li>
                        <li><i class="fas fa-check-circle" style="color: var(--accent-color, #0284c7); margin-right: 8px;"></i> 24/7 dedicated customer assistance</li>
                    </ul>
                </div>
            </div>
        </section>

        3. MANDATORY CSS FOR `styles.css` (Include these exact global rules):
        body { display: flex; flex-direction: column; min-height: 100vh; margin: 0; font-family: 'Inter', system-ui, sans-serif; background: var(--bg-color, #ffffff); color: var(--text-color, #333); line-height: 1.6; }
        main { flex: 1; display: flex; flex-direction: column; width: 100%; }
        .container { width: 100%; max-width: 1200px; margin: 0 auto; padding: 0 5%; box-sizing: border-box; }
        .section-title { text-align: center; margin-bottom: 3rem; }
        
        /* NOTE: Ensure you assign highly attractive, DIVERSE background colors to each section natively to break up the design! */
        section { padding: 90px 0; }
        .hero-section { padding: 0; }
        
        img { max-width: 100%; height: auto; border-radius: 8px; object-fit: cover; display: block; }
        
        /* Premium Form CSS */
        .premium-form { display: flex; flex-direction: column; gap: 1.5rem; max-width: 600px; margin: 0 auto; background: var(--form-bg, #fff); padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.05); box-sizing: border-box; width: 100%; }
        .form-group { display: flex; flex-direction: column; text-align: left; gap: 0.5rem; width: 100%; }
        .form-group label { font-weight: 600; font-size: 0.95rem; color: var(--form-text, #444); }
        .form-group input, .form-group textarea { width: 100%; padding: 0.85rem 1rem; border: 1px solid #ddd; border-radius: 8px; font-family: inherit; font-size: 1rem; box-sizing: border-box; background: var(--input-bg, #fdfdfd); transition: 0.3s; }
        .form-group input:focus, .form-group textarea:focus { border-color: var(--accent-color, #007bff); outline: none; box-shadow: 0 0 0 3px rgba(0,123,255,0.15); }
        .btn-primary { background: var(--accent-color, #007bff); color: white; border: none; padding: 1rem 2rem; border-radius: 8px; font-weight: 600; cursor: pointer; transition: 0.3s; font-size: 1rem; align-self: flex-start; }
        .btn-primary:hover { filter: brightness(0.9); transform: translateY(-2px); }
        
        /* Premium Footer CSS */
        .premium-footer { background: var(--footer-bg, #111); color: var(--footer-text, #f0f0f0); padding: 4rem 0 1rem; width: 100%; margin-top: auto; }
        .footer-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 3rem; align-items: start; justify-content: space-between; }
        .footer-col { display: flex; flex-direction: column; gap: 1.5rem; text-align: left; }
        .footer-col h4 { color: var(--footer-heading, #fff); margin: 0; font-size: 1.2rem; font-weight: 700; }
        .footer-col ul { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.8rem; }
        .footer-col ul li a { color: var(--footer-link, #aaa); text-decoration: none; transition: 0.3s; font-size: 0.95rem; }
        .footer-col ul li a:hover { color: var(--footer-link-hover, #fff); }
        .footer-bottom { text-align: center; padding-top: 2rem; margin-top: 3rem; border-top: 1px solid rgba(255,255,255,0.1); color: var(--footer-text, #777); font-size: 0.9rem; }
        ======================================================================
        """
        final_instruction += f"\n\n{premium_html_css_blueprint}"

        # ========================================================
        # 6. CRITICAL DESIGN GUARDRAIL ENFORCEMENT (MASTER MANDATE)
        # ========================================================
        try:
            final_instruction = BaseAIAgent.inject_master_design_mandate(
                base_instruction=final_instruction,
                provider_name=provider
            )
        except AttributeError:
            master_design_mandate = f"""
        [MASTER FRONTEND ARCHITECT MANDATE - NON-NEGOTIABLE DESIGN RULES FOR {provider.upper()}]:
        You are an elite, award-winning Lead Frontend Architect and UI/UX Designer. You are strictly FORBIDDEN from generating plain, boring, dated, or basic HTML layouts. Every website you build MUST look like a $10,000 bespoke production web application.
        1. PURE VANILLA CSS EXCELLENCE (NO TAILWIND/BOOTSTRAP):
           - Write extensive, professional, verbose CSS directly inside styles.css.
           - Use CSS Custom Properties (:root variables) for a cohesive color palette (Primary, Secondary, Accent, Dark Neutral, Light Neutral).
           - Use CSS Grid and Flexbox exclusively for modern, responsive layouts. Never use table layouts or float-based grids.
        2. MODERN VISUAL AESTHETICS (MANDATORY):
           - GLASSMORPHISM: Apply subtle frosted-glass effects to navigation bars, cards, and floating badges using `background: rgba(255, 255, 255, 0.7); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.3);`.
           - DEPTH & ELEVATION: Use layered, soft box-shadows (e.g., `box-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.08), 0 4px 6px -2px rgba(0, 0, 0, 0.04);`). Never use harsh black borders or solid drop shadows.
           - ROUNDED GEOMETRY: Enforce modern border-radius on all elements (Buttons: 8px to 999px pill; Cards: 16px to 24px; Images: 12px to 20px).
           - DYNAMIC GRADIENTS: Use rich, multi-stop linear and radial backgrounds for hero sections, badges, and accent buttons.
        3. MICRO-INTERACTIONS & MOTION:
           - Include smooth CSS transitions (`transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);`) on all clickable elements.
           - Design interactive hover states for cards (slight upward transform `-translateY(6px)`, enhanced shadow) and buttons (subtle glow, background shift).
           - Define CSS `@keyframes` animations (e.g., smooth fade-in-up for hero titles, floating animations for hero imagery).
        4. TYPOGRAPHY & ICONOGRAPHY:
           - Structure typography with clear visual hierarchy (Hero headings: 3.5rem - 4.5rem font-weight 800; Section titles: 2.2rem - 2.8rem font-weight 700; Body: 1.05rem - 1.15rem line-height 1.7).
           - Use rich visual cards with FontAwesome icons (`<i class="fas fa-icon"></i>`) instead of plain text lists.
        5. CONTENT DENSITY & STRUCTURE:
           - Include complete, realistic, industry-specific copy. NEVER use "Lorem Ipsum" or placeholder text like "[Insert Name Here]".
           - Ensure spacious, breathable layout padding (`padding: 80px 0;` to `100px 0;` for main sections).
        """
            final_instruction = f"{final_instruction.strip()}\n\n{master_design_mandate}"

        model_map = {
            'gemini': custom_model or 'gemini-3.1-pro-preview',
            'openai': custom_model or 'gpt-4.1',
            'claude': custom_model or 'claude-sonnet-4.6',
            'deepseek': custom_model or 'deepseek-chat',
            'grok': custom_model or 'grok-4.3',
            'meta': custom_model or '',          # MetaAgent picks the best model for Groq / OpenRouter / Llama API
            'perplexity': custom_model or 'sonar-pro'
        }
        active_model = model_map.get(provider, 'gemini-2.5-flash')
        
        # --- INSTANT VALIDATION (No Queue Wait if missing key) ---
        if user_api_key:
            api_key = user_api_key
        else:
            api_key = AIGenerationService.get_rotated_key(settings, provider, 1)

        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'message': f'{provider.upper()} API Key missing or invalid. Please configure it in Settings.'})}\n\n"
            return
            
        # ========================================================
        # CALL CENTER WAITING QUEUE LOGIC
        # ========================================================
        req_id = str(uuid.uuid4())
        ConnectionManager.enter_queue(req_id)
        
        limit = settings.concurrent_limit if settings and settings.concurrent_limit_enabled else 1000
        if limit < 1: limit = 5
        
        acquired = False
        log_buffer = [f"🚀 Generation Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"]
        
        try:
            # 1. Loop and ping the frontend while we wait for an open slot
            while True:
                acquired, current_pos = ConnectionManager.try_acquire(req_id, limit)
                if acquired:
                    break # We have a slot! Proceed to generate.
                if current_pos == -1:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Removed from queue. Please try again.'})}\n\n"
                    return
                    
                # Yield queue position to the frontend
                yield f"data: {json.dumps({'type': 'queue', 'position': current_pos})}\n\n"

            # 2. Proceed with generation now that slot is acquired
            log_str = f"🤖 AI Provider: {provider.upper()} | Model: {active_model}"
            if final_max_tokens:
                log_str += f" | Tokens: {final_max_tokens}"
            log_buffer.append(log_str)
            
            if user_api_key:
                log_buffer.append(f"🔑 Key Assigned: Personal User API Key")
            else:
                masked_key = f"...{api_key[-4:]}" if api_key and len(api_key) > 4 else "None"
                log_buffer.append(f"🔑 Key Assigned: {masked_key}")
            
            user_prompt = "Unknown request"
            if history_messages:
                for msg in reversed(history_messages):
                    if msg.get('role') == 'user':
                        try:
                            txt = msg['parts'][0]['text']
                            if "CURRENT_WEBSITE_STRUCTURE:" in txt: continue
                            user_prompt = txt
                            break
                        except: pass
                        
            log_buffer.append(f"📝 Prompt: {user_prompt[:300]}...")

            stream = None
            
            if provider == 'openai':
                stream = OpenAIAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            elif provider == 'deepseek':
                stream = DeepSeekAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            elif provider == 'claude':
                stream = ClaudeAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            elif provider == 'grok':
                stream = GrokAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            elif provider == 'perplexity':
                stream = PerplexityAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            elif provider == 'meta':
                stream = MetaAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            else:
                stream = GeminiAgent.generate_stream(api_key, active_model, history_messages, final_instruction, user_id, is_new_generation, max_tokens=final_max_tokens)
            
            for chunk in stream:
                yield chunk
            
            log_buffer.append("✅ UI Code Generation Successfully Completed.")

        except Exception as e:
            log_buffer.append(f"❌ Error during generation: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # 3. Clean up the queue and slot
            ConnectionManager.leave_queue(req_id)
            if acquired:
                ConnectionManager.release_slot()
            if user_id:
                save_user_log(user_id, "\n".join(log_buffer))

    @staticmethod
    def generate_image(prompt, img_type='image', text_overlay=''):
        """Routes image generation requests and handles cross-provider fallbacks."""
        user = None
        using_own_key = False
        with app.app_context():
            if current_user and current_user.is_authenticated:
                user = User.query.get(current_user.id)
                if user:
                    allow_own_keys = user.role == 'admin' or user.allow_own_api_keys or (user.user_type and user.user_type.allow_own_api_keys)
                    if allow_own_keys:
                        try:
                            sys_settings = SystemSettings.query.first()
                            sys_provider = sys_settings.ai_provider.strip().lower() if sys_settings and sys_settings.ai_provider else 'gemini'
                            
                            user_settings = {}
                            if hasattr(user, 'user_settings_json') and user.user_settings_json:
                                user_settings = json.loads(user.user_settings_json)
                                
                            pref_provider = user_settings.get('ai_provider', 'default').strip().lower()
                            if pref_provider == 'default': pref_provider = sys_provider
                            
                            # SECURITY FIX: Decrypt user's personal keys during image gen
                            key1 = decrypt_sensitive_data(user_settings.get(f'{pref_provider}_api_key'))
                            if key1 and len(str(key1).strip()) > 5: 
                                using_own_key = True
                                
                            if not using_own_key and hasattr(user, 'api_keys_json') and user.api_keys_json:
                                api_keys = json.loads(user.api_keys_json)
                                key2 = decrypt_sensitive_data(api_keys.get(f'{pref_provider}_api_key') or api_keys.get(pref_provider))
                                if key2 and len(str(key2).strip()) > 5: 
                                    using_own_key = True
                                    
                            if not using_own_key and hasattr(user, f'{pref_provider}_api_key'):
                                key3 = decrypt_sensitive_data(getattr(user, f'{pref_provider}_api_key'))
                                if key3 and len(str(key3).strip()) > 5: 
                                    using_own_key = True
                                    
                            if not using_own_key and pref_provider == 'openai':
                                openai_key_attr = decrypt_sensitive_data(user_settings.get('openai_api_key'))
                                if not openai_key_attr and hasattr(user, 'openai_api_key'):
                                    openai_key_attr = decrypt_sensitive_data(user.openai_api_key)
                                if openai_key_attr:
                                    using_own_key = True
                        except: pass

                    if user.user_type and user.user_type.project_limit != -1 and not using_own_key:
                        if user.images_generated >= user.user_type.image_limit:
                            raise Exception("Image generation limit reached. Please upgrade.")

        generated_urls = []
        
        final_prompt = prompt.replace('\n', ' ').strip()
        if len(final_prompt) > 800:
            final_prompt = final_prompt[:800]
        
        with app.app_context():
            settings = SystemSettings.query.first()
            if not settings: return []
            
            provider = settings.ai_provider.strip().lower()
            print(f"🎨 [Image Gen Traffic Controller] Provider: '{provider}'", flush=True)

            api_key = None
            if using_own_key and user:
                 try:
                     user_settings = json.loads(user.user_settings_json or '{}')
                     
                     pref_prov = user_settings.get('ai_provider', 'default').strip().lower()
                     if pref_prov != 'default': provider = pref_prov
                     
                     # SECURITY FIX: Decrypt user's personal keys during image gen
                     api_key = decrypt_sensitive_data(user_settings.get(f'{provider}_api_key'))
                     if not api_key and hasattr(user, 'api_keys_json') and user.api_keys_json:
                         api_keys = json.loads(user.api_keys_json)
                         api_key = decrypt_sensitive_data(api_keys.get(f'{provider}_api_key') or api_keys.get(provider))
                     if not api_key and hasattr(user, f'{provider}_api_key'):
                         api_key = decrypt_sensitive_data(getattr(user, f'{provider}_api_key'))
                 except: pass

            if not api_key:
                api_key = AIGenerationService.get_rotated_key(settings, provider, 1)
            
            model_to_use = getattr(settings, f'image_model_{provider}', '')
            result = None

            if provider == 'gemini':
                result = GeminiAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)
            elif provider == 'openai':
                result = OpenAIAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)
            elif provider == 'claude':
                result = ClaudeAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)
            elif provider == 'deepseek':
                result = DeepSeekAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)
            elif provider == 'grok':
                result = GrokAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)
            elif provider == 'meta':
                result = MetaAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)
            elif provider == 'perplexity':
                result = PerplexityAgent.generate_image(api_key, model_to_use, final_prompt, img_type, text_overlay)

            if result:
                generated_urls.extend(result)

            if not generated_urls and provider != 'openai':
                print(f"⚠️ {provider.upper()} image generation returned None. Routing to DALL-E fallback...", flush=True)
                
                openai_key = None
                if using_own_key and user:
                     try:
                         user_settings = json.loads(user.user_settings_json or '{}')
                         # SECURITY FIX: Decrypt user's personal keys during fallback image gen
                         openai_key = decrypt_sensitive_data(user_settings.get('openai_api_key'))
                         if not openai_key and hasattr(user, 'openai_api_key'):
                             openai_key = decrypt_sensitive_data(user.openai_api_key)
                     except: pass
                
                if not openai_key:
                    openai_key = AIGenerationService.get_rotated_key(settings, 'openai', 1)
                    
                fallback_result = OpenAIAgent.generate_image(openai_key, 'dall-e-3', final_prompt, img_type, text_overlay)
                if fallback_result:
                    generated_urls.extend(fallback_result)
            
            if generated_urls and user and not using_own_key:
                try:
                    user.images_generated += 1
                    log = UserActivityLog(
                        user_id=user.id,
                        action_type='image_generated',
                        description=f"Generated an AI {img_type}."
                    )
                    db.session.add(log)
                    db.session.commit()
                except: pass
            
            if not generated_urls:
                print("❌ All AI Models Failed. Falling back to stock image provider...", flush=True)
                fallback_stock_url = ImageProviderService.get_image_url(final_prompt)
                if fallback_stock_url:
                    generated_urls.append(fallback_stock_url)

        if not generated_urls:
            print("⚠️ [Image Gen] Returning EWebGen default fallback.", flush=True)
            generated_urls.append("https://placehold.co/800x600?text=Fallback")

        return generated_urls

def get_pexels_image_url(query, width=800, height=600):
    return ImageProviderService.get_image_url(query, width, height)