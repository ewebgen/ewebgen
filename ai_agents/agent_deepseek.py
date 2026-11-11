import json
import requests
from ai_agents.base_agent import BaseAIAgent
from services_core import download_image_server_side

class DeepSeekAgent:
    """
    Handles all API interactions specifically for DeepSeek models.
    """

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
             yield f"data: {json.dumps({'type': 'error', 'message': 'DeepSeek API Key missing.'})}\n\n"
             return

        messages = [{"role": "system", "content": system_instruction}]
        
        for i, item in enumerate(history):
            role = item.get('role', 'user')
            if role == 'model': role = 'assistant'
            content = item.get('parts', [{}])[0].get('text', '')
            
            # =========================================================================
            # DYNAMIC INJECTION OVERRIDE: PREMIUM ARCHITECTURE & CORE DEVELOPER MANDATES
            # =========================================================================
            if i == len(history) - 1 and role == 'user':
                content += (
                    "\n\n[DEEPSEEK ARCHITECTURAL EXCELLENCE & CORE DEVELOPER MANDATES]:"
                    "\nYou are a Master Frontend Engineer specializing in top-tier bespoke, PREMIUM multi-page web applications. "
                    "The code you output must bypass basic web semantics and directly yield highly polished layouts that follow these core mandates to guarantee a PREMIUM WEBSITE DESIGN:\n"
                    
                    "\n1. CONTENT DENSITY & STRUCTURE (ANTI-LAZY MANDATE):"
                    "\nEvery single HTML page (including About, Services, Contact, etc.) MUST contain a MINIMUM of 4 to 6 fully fleshed-out, distinct `<section>` blocks. "
                    "Generating pages with only 1 or 2 sections is strictly forbidden. You must build out rich, comprehensive layouts with genuine copy. Do not use Lorem Ipsum."
                    
                    "\n2. BREATHTAKING HERO WRAPPERS (WITH HERO IMAGE MANDATE):"
                    "\nThe Hero area MUST feel distinct and premium. EVERY page MUST start with a breathtaking, high-impact Hero Section at the top. Depending on the layout provided in the instructions, use either a striking side-by-side layout with an `<img>` tag (`src='/images/placeholder.jpg'`), OR a majestic full-width CSS `background-image: url('/images/placeholder.jpg')` with a dark overlay and white text. Make the hero visually striking."

                    "\n3. PERFECT IMAGE ALIGNMENT & RESPONSIVE FLEXBOX:"
                    "\nAll images MUST be perfectly aligned. When placing text next to an image (like in a feature or about section), you MUST wrap them in a Flexbox container (`display: flex; align-items: center; justify-content: space-between; gap: 3rem; flex-wrap: wrap;`). "
                    "All images must explicitly have `max-width: 100%; height: auto; object-fit: cover; display: block; border-radius: 12px;`. Never let images overflow their containers."
                    
                    "\n4. CONTAINER MAPPING & ALIGNMENT:"
                    "\nYou MUST wrap all structural sections inside an explicit container utilizing max-width (e.g., 1200px) and 'margin: 0 auto;' to ensure elements center cleanly on wider desktop screens."
                    
                    "\n5. MODERN VISUAL DEPTH & PREMIUM CSS LAYOUTS:"
                    "\nExtensively maximize modern Vanilla CSS within 'styles.css'. Incorporate subtle linear gradients, rounded edges ('border-radius: 12px' or greater), premium drop-shadow variations, and glassmorphism styling parameters ('backdrop-filter: blur(10px); background: rgba(255,255,255,0.08);')."
                    
                    "\n6. PREMIUM COLOR SCHEME & ACCESSIBILITY GUARDRAILS:"
                    "\nYou MUST use a highly attractive, modern, and professional color scheme. STRICTLY AVOID dull brown or beige color combinations. Use clean, premium palettes (e.g., Deep Navy, Slate, Crisp Whites, vibrant Tech Blue, Emerald, or Royal Purple accents). Ensure high contrast between text and background. NEVER use a white/light background with white/light font elements. NEVER pair a black background with black text layers."
                    
                    "\n7. OUTPUT COMPLIANCE STRATEGY:"
                    "\nDo NOT wrap your raw JSON return inside markdown block enclosures (e.g., do not envelope with ```json or ```html markers). "
                    "The payload output must solely consist of a standard, parseable JSON object starting exactly with '{' and terminating with '}'."

                    "\n8. STRICT DROPDOWN / SUBMENU RULES:"
                    "\nIf you create navigation dropdowns or submenus, DO NOT add inline styles like `display: block;` or `opacity: 1;` to the `<ul>` or submenu containers. Submenus MUST be hidden by default and only revealed via CSS hover states. Never force them to be open in the HTML."
                )
                
            messages.append({"role": role, "content": content})

        headers = { 
            "Authorization": f"Bearer {api_key}", 
            "Content-Type": "application/json" 
        }
        
        # --- GOAL: APPLY CUSTOM TOKEN LIMIT IF SPECIFIED ---
        max_out = max_tokens if max_tokens else 50000 
        
        payload = { 
            "model": model_name, 
            "messages": messages, 
            "stream": True, 
            "temperature": 0.7, 
            "max_tokens": max_out, 
            "response_format": {"type": "json_object"}
        } 

        try:
            # Strictly routing to DeepSeek's endpoint
            resp = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload, stream=True, timeout=60)
            
            if resp.status_code != 200:
                err_msg = f"DeepSeek API Error {resp.status_code}: {resp.text}"
                yield f"data: {json.dumps({'type': 'error', 'message': err_msg})}\n\n"
                return

            full_text = ""
            for line in resp.iter_lines():
                if line:
                    decoded = line.decode('utf-8')
                    if decoded.startswith("data: "):
                        data_str = decoded[6:]
                        if data_str.strip() == "[DONE]": break
                        try:
                            chunk_json = json.loads(data_str)
                            if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                                delta = chunk_json['choices'][0].get('delta', {})
                                content = delta.get('content', '')
                                if content:
                                    full_text += content
                                    # Proper SSE formatting for chunks so the frontend UI actually streams
                                    yield f"data: {json.dumps({'type': 'chunk', 'content': content})}\n\n"
                        except: pass
            
            # Pass the final string to the BaseAgent for JSON extraction, HTML formatting, and saving
            if full_text.strip():
                yield BaseAIAgent.finalize_response(full_text, user_id, is_new_generation)

        except Exception as e:
            print(f"DeepSeek Error: {str(e)}", flush=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        """
        Attempts to generate an image if a custom image model is specified for DeepSeek.
        Returns the local URL list on success, or None on failure (triggering DALL-E fallback).
        """
        if not api_key or not model_name:
            return None
            
        print(f"🎨 [Image Gen] Attempting DEEPSEEK using custom Image Model: {model_name}...", flush=True)
        try:
            headers = { "Authorization": f"Bearer {api_key}", "Content-Type": "application/json" }
            req_prompt = prompt
            if img_type == 'logo': 
                req_prompt = f"minimalist vector logo, icon, {prompt}, white background, flat style"
                
            payload = { "model": model_name, "prompt": req_prompt, "n": 1, "size": "1024x1024" }
            url_post = 'https://api.deepseek.com/images/generations'
            
            resp = requests.post(url_post, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data'):
                    url = data['data'][0]['url']
                    local_path = download_image_server_side(url, filename_prefix=f"gallery_ai_deepseek_")
                    if local_path: 
                        return [local_path]
        except Exception as ex:
            print(f"❌ DEEPSEEK Custom Image Gen Failed: {ex}", flush=True)
            
        return None