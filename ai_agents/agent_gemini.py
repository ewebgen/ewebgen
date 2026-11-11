import os
import json
import uuid
import base64
import time
import requests

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    print("⚠️ Google GenAI SDK not installed. Gemini features may be limited.")

from database_setup import app
from services_core import get_current_uid
from ai_agents.base_agent import BaseAIAgent

class GeminiAgent:
    """
    Handles all API interactions specifically for Google's Gemini and Imagen models,
    with robust connection retry and error-recovery logic to prevent [Errno 104] Connection Reset issues.
    """

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
             yield f"data: {json.dumps({'type': 'error', 'message': 'Gemini API Key missing.'})}\n\n"
             return
             
        if not genai:
             yield f"data: {json.dumps({'type': 'error', 'message': 'Google GenAI SDK is not installed on the server.'})}\n\n"
             return

        # --- THE ULTIMATE DESIGN FORCING MANDATE (UPGRADED) ---
        enhanced_system = system_instruction + (
            "\n\n[GEMINI ULTIMATE DESIGN MANDATE]: You are an elite, award-winning frontend architect. "
            "You are strictly FORBIDDEN from generating plain, boring, or basic HTML. "
            "\n1. CSS VOLUME: You MUST write extensive, verbose CSS within the <style> tag. "
            "\n2. PREMIUM COLOR SCHEME: You MUST use a highly attractive, modern, and professional color scheme. STRICTLY AVOID dull brown or beige color combinations. Use clean, premium palettes (e.g., Deep Navy, Slate, Crisp Whites, vibrant Tech Blue, Emerald, or Royal Purple accents). Ensure high contrast. NEVER use a white background with white text, or a black background with black text. "
            "\n3. MODERN UI & ANIMATIONS: You MUST use CSS variables, Flexbox/Grid, beautiful linear-gradients, glassmorphism (backdrop-filter), rounded corners (border-radius: 16px), deep layered box-shadows, and include CSS @keyframes animations (e.g., fade-in, slide-up, hover scale). "
            "\n4. ALIGNMENT & SPACING: Ensure generous padding (e.g., 'padding: 5rem 2rem') and use 'margin: 0 auto' with 'max-width: 1200px' for all main containers so the site is perfectly centered. "
            "\n5. CONTENT DENSITY & STRUCTURE (ANTI-LAZY): Every single page MUST contain a MINIMUM of 4 to 6 fully fleshed-out, distinct `<section>` blocks. You will fail if you return a page with only 1 or 2 sections. Do not use Lorem Ipsum."
            "\n6. BEAUTIFUL HERO IMAGE: Every page MUST start with a breathtaking, high-impact Hero Section. Depending on the layout provided, use either a striking side-by-side layout with an `<img>` tag (`src='/images/placeholder.jpg'`), OR a majestic full-width CSS `background-image: url('/images/placeholder.jpg')` with a dark overlay and white text. Make the hero visually striking."
            "\n7. PERFECT IMAGE ALIGNMENT: All images MUST be perfectly aligned. When placing text next to an image, you MUST wrap them in a Flexbox container (`display: flex; align-items: center; justify-content: space-between; gap: 3rem; flex-wrap: wrap;`). All images must explicitly have `max-width: 100%; height: auto; object-fit: cover; display: block; border-radius: 12px;`."
            "\n8. STRICT DROPDOWN / SUBMENU RULES: If you create navigation dropdowns or submenus, DO NOT add inline styles like `display: block;` or `opacity: 1;` to the `<ul>` or submenu containers. Submenus MUST be hidden by default and only revealed via CSS hover states. Never force them to be open in the HTML."
            "\nFailure to provide advanced CSS, multiple sections, and perfectly aligned images will result in immediate rejection."
        )

        max_retries = 3
        retry_delay = 2
        last_exception = None

        for attempt in range(1, max_retries + 1):
            try:
                client = genai.Client(api_key=api_key)
                gemini_history = []
                
                for i, item in enumerate(history):
                    role = item.get('role', 'user')
                    parts = item.get('parts', [])
                    text_content = parts[0].get('text', '') if parts else ""
                    
                    if i == len(history) - 1 and role == 'user':
                        text_content += (
                            "\n\n[STRICT EXECUTION]: Generate the complete, breathtakingly designed website. "
                            "Output ONLY a valid JSON object including complete styles.css and all HTML pages. Do not wrap code in markdown blocks."
                        )
                        
                    gemini_history.append(types.Content(role=role, parts=[types.Part(text=text_content)]))

                if max_tokens:
                    config = types.GenerateContentConfig(
                        system_instruction=enhanced_system,
                        max_output_tokens=max_tokens,
                        temperature=0.7,
                        response_mime_type="application/json"
                    )
                else:
                    config = types.GenerateContentConfig(
                        system_instruction=enhanced_system,
                        temperature=0.7,
                        response_mime_type="application/json"
                    )

                response_stream = client.models.generate_content_stream(
                    model=model_name, 
                    contents=gemini_history,
                    config=config
                )

                full_text = ""
                for chunk in response_stream:
                    if chunk.text:
                        full_text += chunk.text
                        yield f"data: {json.dumps({'type': 'chunk', 'content': chunk.text})}\n\n"
                
                if full_text.strip():
                    # Successfully completed stream without connection reset
                    yield BaseAIAgent.finalize_response(full_text, user_id, is_new_generation)
                    return
                else:
                    raise Exception("Gemini returned an empty response stream.")

            except (ConnectionError, TimeoutError, OSError) as ce:
                last_exception = ce
                print(f"⚠️ [Gemini Connection Warning] Attempt {attempt}/{max_retries} failed due to connection issue: {ce}", flush=True)
                if attempt < max_retries:
                    time.sleep(retry_delay * attempt)
                    continue
            except Exception as e:
                err_str = str(e)
                if "104" in err_str or "connection reset" in err_str.lower() or "broken pipe" in err_str.lower():
                    last_exception = e
                    print(f"⚠️ [Gemini Connection Reset] Attempt {attempt}/{max_retries}: {e}", flush=True)
                    if attempt < max_retries:
                        time.sleep(retry_delay * attempt)
                        continue
                else:
                    # Non-connection error (e.g. invalid API key, safety block, invalid JSON config)
                    print(f"❌ Gemini Error: {e}", flush=True)
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                    return

        # If all retries failed due to connection drops
        print(f"❌ Gemini Connection Failed after {max_retries} attempts: {last_exception}", flush=True)
        yield f"data: {json.dumps({'type': 'error', 'message': f'Connection lost to Gemini API ([Errno 104] Connection reset by peer). Please check your network or try again.'})}\n\n"

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        """
        Primary handler for Imagen 3 generation via Google's REST endpoints.
        """
        if not api_key:
            print("❌ [Image Gen] Gemini API key is missing.", flush=True)
            return None

        print(f"🎨 [Image Gen] Attempting Gemini/Imagen 3 via REST...", flush=True)
        uid = get_current_uid()
        
        req_prompt = prompt
        if img_type == 'logo':
            req_prompt = f"minimalist vector logo, icon, {prompt}, white background, flat style"
            if text_overlay: req_prompt += f", with text '{text_overlay}'"

        models_to_try = [
            model_name if model_name else "imagen-3.0-generate-002",
            "imagen-3.0-generate-002",
            "imagen-3.0-generate-001",
            "imagen-3.0-fast-generate-001"
        ]
        
        headers = { "Content-Type": "application/json" }
        payload = {
            "prompt": req_prompt,
            "number_of_images": 1,
            "aspect_ratio": "1:1",
            "safety_filter_level": "BLOCK_ONLY_HIGH",
            "person_generation": "ALLOW_ADULT",
            "outputOptions": { "mimeType": "image/jpeg" }
        }

        def try_gen(u, p):
            try:
                r = requests.post(u, headers=headers, json=p, timeout=40)
                if r.status_code == 200:
                    d = r.json()
                    b64 = None
                    if 'generatedImages' in d and len(d['generatedImages']) > 0:
                        b64 = d['generatedImages'][0].get('image', {}).get('imageBytes')
                    elif 'predictions' in d and len(d['predictions']) > 0:
                        b64 = d['predictions'][0].get('bytesBase64Encoded')
                        if not b64: b64 = d['predictions'][0].get('image', {}).get('bytesBase64Encoded')
                    
                    if b64:
                        img_data = base64.b64decode(b64)
                        filename = f"user_{uid}_gallery_ai_gen_{uuid.uuid4().hex[:8]}.jpg"
                        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        
                        if not os.path.exists(app.config['UPLOAD_FOLDER']): 
                            os.makedirs(app.config['UPLOAD_FOLDER'])
                            
                        with open(filepath, "wb") as f: 
                            f.write(img_data)
                            
                        print(f"✅ [Image Gen] Success with Gemini REST", flush=True)
                        return [f"/uploads/{filename}"]
                    else:
                        print(f"⚠️ [Image Gen] Response OK but no imageBytes", flush=True)
                        return None
                else:
                    print(f"❌ [Image Gen] REST Error {r.status_code}: {r.text}", flush=True)
                    return None
            except Exception as ex: 
                print(f"❌ [Image Gen] Request Error: {ex}", flush=True)
                return None

        for m_name in models_to_try:
            print(f"🔄 [Image Gen] Trying model: {m_name}...", flush=True)
            
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateImages?key={api_key}"
            res = try_gen(url, payload)
            if res: return res
            
            url_alpha = f"https://generativelanguage.googleapis.com/v1alpha/models/{m_name}:generateImages?key={api_key}"
            res = try_gen(url_alpha, payload)
            if res: return res

            url_pred = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:predict?key={api_key}"
            payload_pred = {"instances": [{"prompt": req_prompt}], "parameters": {"sampleCount": 1, "aspectRatio": "1:1"}}
            res = try_gen(url_pred, payload_pred)
            if res: return res
            
        print("❌ [Image Gen] All Gemini models failed.", flush=True)
        return None