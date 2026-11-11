import json
import requests
from ai_agents.base_agent import BaseAIAgent
from services_core import download_image_server_side

class ClaudeAgent:
    """
    Handles all API interactions specifically for Anthropic's Claude models.
    """

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Claude API Key missing.'})}\n\n"
            return

        # --- THE ULTIMATE DESIGN FORCING MANDATE (UPGRADED) ---
        enhanced_system = system_instruction + (
            "\n\n[CLAUDE ULTIMATE DESIGN MANDATE - NON-NEGOTIABLE]: You are an elite, award-winning frontend architect. "
            "You are strictly FORBIDDEN from generating plain, boring, or basic HTML. "
            "\n1. PURE VANILLA CSS ONLY: You MUST write extensive, verbose CSS within the <style> tag. ABSOLUTELY NO TAILWIND OR EXTERNAL CSS FRAMEWORKS."
            "\n2. PREMIUM COLOR SCHEME: You MUST use a highly attractive, modern, and professional color scheme. STRICTLY AVOID dull brown or beige color combinations. Use clean, premium palettes (e.g., Deep Navy, Slate, Crisp Whites, vibrant Tech Blue, Emerald, or Royal Purple accents). Ensure high contrast. NEVER use a white background with white text, or a black background with black text. "
            "\n3. MODERN UI & ANIMATIONS: You MUST use CSS variables, Flexbox/Grid, beautiful linear-gradients, glassmorphism (backdrop-filter), rounded corners (border-radius: 16px), deep layered box-shadows, and include CSS @keyframes animations (e.g., fade-in, slide-up, hover scale). "
            "\n4. ALIGNMENT & SPACING: Ensure generous padding (e.g., 'padding: 5rem 2rem') and use 'margin: 0 auto' with 'max-width: 1200px' for all main containers so the site is perfectly centered. "
            "\n5. CONTENT DENSITY & STRUCTURE (ANTI-LAZY): EVERY single page (including About, Services, Contact, etc.) MUST contain a MINIMUM of 4 to 6 fully fleshed-out, distinct `<section>` blocks. You will fail if you return a page with only 1 or 2 sections. Do not use Lorem Ipsum."
            "\n6. BEAUTIFUL HERO IMAGE: Every page MUST start with a breathtaking, high-impact Hero Section. Depending on the layout provided, use either a striking side-by-side layout with an `<img>` tag (`src='/images/placeholder.jpg'`), OR a majestic full-width CSS `background-image: url('/images/placeholder.jpg')` with a dark overlay and white text. Make the hero visually striking."
            "\n7. PERFECT IMAGE ALIGNMENT: All images MUST be perfectly aligned. When placing text next to an image, you MUST wrap them in a Flexbox container (`display: flex; align-items: center; justify-content: space-between; gap: 3rem; flex-wrap: wrap;`). All images must explicitly have `max-width: 100%; height: auto; object-fit: cover; display: block; border-radius: 12px;`."
            "\n8. STRICT DROPDOWN / SUBMENU RULES: If you create navigation dropdowns or submenus, DO NOT add inline styles like `display: block;` or `opacity: 1;` to the `<ul>` or submenu containers. Submenus MUST be hidden by default and only revealed via CSS hover states. Never force them to be open in the HTML."
            "\nFailure to provide advanced CSS, multiple sections, and perfectly aligned images will result in immediate rejection."
        )

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15", 
            "content-type": "application/json"
        }
        
        anthropic_messages = []
        for i, msg in enumerate(history):
            role = "user" if msg.get('role') == "user" else "assistant"
            try:
                content = msg['parts'][0]['text']
            except (KeyError, IndexError):
                content = str(msg.get('content', ''))
            
            # --- STRICT EXECUTION PROTOCOL IN USER PROMPT ---
            if i == len(history) - 1 and role == 'user':
                content += (
                    "\n\n[FINAL INSTRUCTION]: I will reject this generation if it looks basic, flat, left-aligned, or lacks sections. "
                    "You MUST output a highly styled, modern, multi-section website using ONLY Vanilla CSS. DO NOT USE TAILWIND. "
                    "Output ONLY a valid JSON object. Do NOT wrap it in ```json or markdown blocks. Start directly with { and end with }."
                )
                
            anthropic_messages.append({"role": role, "content": content})

        # --- GOAL: APPLY CUSTOM TOKEN LIMIT IF SPECIFIED ---
        final_max_tokens = max_tokens if max_tokens else 30000

        payload = {
            "model": model_name,
            "messages": anthropic_messages,
            "system": enhanced_system,
            "max_tokens": final_max_tokens, 
            "temperature": 0.2,
            "stream": True
        }

        try:
            url_post = 'https://api.anthropic.com/v1/messages'
            resp = requests.post(url_post, headers=headers, json=payload, stream=True, timeout=60)
            
            if resp.status_code != 200:
                error_msg = resp.text
                try: error_msg = resp.json().get('error', {}).get('message', resp.text)
                except: pass
                yield f"data: {json.dumps({'type': 'error', 'message': f'Claude API Error: {error_msg}'})}\n\n"
                return

            full_response = ""
            for line in resp.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith('data: '):
                        data_str = decoded_line[6:]
                        if data_str.strip() == '[DONE]': continue
                        try:
                            chunk = json.loads(data_str)
                            if chunk.get('type') == 'content_block_delta':
                                text = chunk['delta'].get('text', '')
                                if text:
                                    full_response += text
                                    # Proper SSE formatting for chunks
                                    yield f"data: {json.dumps({'type': 'chunk', 'content': text})}\n\n"
                            elif chunk.get('type') == 'error':
                                err = chunk.get('error', {}).get('message', 'Unknown streaming error')
                                yield f"data: {json.dumps({'type': 'error', 'message': f'Claude Stream Error: {err}'})}\n\n"
                        except: pass

            if full_response.strip():
                # Process the accumulated JSON and salvage if truncated
                yield BaseAIAgent.finalize_response(full_response, user_id, is_new_generation)

        except Exception as e:
            print(f"Claude Error: {e}", flush=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        if not api_key or not model_name:
            return None
            
        print(f"🎨 [Image Gen] Attempting CLAUDE using custom Image Model: {model_name}...", flush=True)
        try:
            headers = { "Authorization": f"Bearer {api_key}", "Content-Type": "application/json" }
            req_prompt = prompt
            if img_type == 'logo': 
                req_prompt = f"minimalist vector logo, icon, {prompt}, white background, flat style"
                
            payload = { "model": model_name, "prompt": req_prompt, "n": 1, "size": "1024x1024" }
            url_post = 'https://api.anthropic.com/v1/images/generations'
            
            resp = requests.post(url_post, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data'):
                    url = data['data'][0]['url']
                    local_path = download_image_server_side(url, filename_prefix=f"gallery_ai_claude_")
                    if local_path: 
                        return [local_path]
        except Exception as e:
            print(f"Claude Image Error: {e}", flush=True)
            
        return None