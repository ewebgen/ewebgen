import os
import json
import uuid
import base64
import requests
from database_setup import app
from ai_agents.base_agent import BaseAIAgent
from services_core import download_image_server_side, get_current_uid

class OpenAIAgent:
    """
    Handles all API interactions specifically for OpenAI models.
    """

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
             yield f"data: {json.dumps({'type': 'error', 'message': 'OpenAI API Key missing.'})}\n\n"
             return

        # --- THE ULTIMATE DESIGN FORCING MANDATE (UPGRADED) ---
        enhanced_system = system_instruction + (
            "\n\n[OPENAI ULTIMATE DESIGN MANDATE]: You are an elite, award-winning frontend architect. "
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

        messages = [{"role": "system", "content": enhanced_system}]
        
        for i, item in enumerate(history):
            role = item.get('role', 'user')
            if role == 'model': role = 'assistant'
            content = item.get('parts', [{}])[0].get('text', '')
            
            # Reinforce the output format at the very end of the prompt
            if i == len(history) - 1 and role == 'user':
                content += (
                    "\n\n[STRICT EXECUTION]: Generate the complete, breathtakingly designed website. "
                    "Output ONLY a valid JSON object. Do not wrap code in markdown blocks."
                )
                
            messages.append({"role": role, "content": content})

        # Added OpenRouter specific headers
        headers = { 
            "Authorization": f"Bearer {api_key}", 
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ewebgen.com", 
            "X-Title": "Editable Website Builder"  
        }
        
        # --- ENABLE STRICT JSON MODE & REMOVE TOKEN LIMITS ---
        payload = { 
            "model": model_name, 
            "messages": messages, 
            "stream": True, 
            "temperature": 1, 
            "response_format": {"type": "json_object"} 
        } 
        
        # --- GOAL: APPLY CUSTOM TOKEN LIMIT IF SPECIFIED ---
        if max_tokens:
            payload["max_tokens"] = 50000

        try:
            # Routing to OpenRouter's endpoint
            resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, stream=True, timeout=150)
            
            if resp.status_code != 200:
                err_msg = f"OpenAI API Error {resp.status_code}: {resp.text}"
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
                                    yield f"data: {json.dumps({'type': 'chunk', 'content': content})}\n\n"
                        except: pass
            
            # Pass the final string to the BaseAgent for JSON extraction, HTML formatting, and saving
            yield BaseAIAgent.finalize_response(full_text, user_id, is_new_generation)

        except Exception as e:
            print(f"OpenAI Error: {e}", flush=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        """
        Attempts to generate an image using standard OpenAI DALL-E/GPT models.
        Includes robust support for decoding b64_json responses directly to the server.
        """
        if not api_key:
            return None
        
        # Priority list of models
        models_to_try = ["gpt-image-1", "dall-e-3", "dall-e-2"]
        
        # Determine the target model
        target_model = model_name if model_name in models_to_try else "gpt-image-1"
        ordered_models = [target_model] + [m for m in models_to_try if m != target_model]
            
        print(f"🎨 [Image Gen] Attempting OPENAI Image Generation...", flush=True)
        
        def call_openai_image_api(model):
            headers = { "Authorization": f"Bearer {api_key}", "Content-Type": "application/json" }
            req_prompt = prompt
            if img_type == 'logo': 
                req_prompt = f"minimalist vector logo, icon, {prompt}, white background, flat style"
                
            payload = { 
                "model": model, 
                "prompt": req_prompt, 
                "n": 1, 
                "size": "1024x1024" 
                # Not forcing response_format='b64_json' so we can naturally handle whichever it provides
            }
            return requests.post('https://api.openai.com/v1/images/generations', headers=headers, json=payload, timeout=150)

        for model in ordered_models:
            print(f"🔄 Trying model: {model}...", flush=True)
            try:
                resp = call_openai_image_api(model)
                
                if resp.status_code == 200:
                    data = resp.json()
                    
                    # LOGGING: Print the first 2000 chars of the successful response for debugging
                    print(f"✅ OpenAI Success Response ({model}):\n{json.dumps(data, indent=2)[:2000]}", flush=True)
                    
                    if data.get('data') and len(data['data']) > 0:
                        image_obj = data['data'][0]
                        url = image_obj.get('url')
                        b64 = image_obj.get('b64_json')
                        
                        # Case 1: OpenAI returns a standard URL
                        if url:
                            local_path = download_image_server_side(url, filename_prefix="gallery_ai_openai_")
                            if local_path: return [local_path]
                            
                        # Case 2: OpenAI returns Base64 Encoded Image Data
                        elif b64:
                            try:
                                img_data = base64.b64decode(b64)
                                uid = get_current_uid()
                                unique_id = uuid.uuid4().hex[:10]
                                filename = f"user_{uid}_gallery_ai_openai_{unique_id}.png"
                                
                                # Access context to find exact upload path
                                with app.app_context():
                                    upload_folder = app.config.get('UPLOAD_FOLDER')
                                    if not upload_folder:
                                        # Fallback directly alongside the code if config fails
                                        upload_folder = os.path.join(os.path.abspath(os.path.dirname(__file__)), '..', 'uploads')
                                    
                                    if not os.path.exists(upload_folder):
                                        os.makedirs(upload_folder)
                                        
                                    filepath = os.path.join(upload_folder, filename)
                                    with open(filepath, "wb") as f:
                                        f.write(img_data)
                                        
                                    # Return exactly what the frontend requires: /uploads/filename.ext
                                    print(f"✅ Decoded and saved b64_json successfully to {filepath}", flush=True)
                                    return [f"/uploads/{filename}"]
                                    
                            except Exception as e:
                                print(f"❌ Error decoding/saving base64 image: {e}", flush=True)
                        else:
                            print("❌ Response was OK, but contained neither 'url' nor 'b64_json'.", flush=True)
                else:
                    print(f"❌ Model {model} failed (Status {resp.status_code}): {resp.text}", flush=True)
            except Exception as ex:
                print(f"❌ Error with model {model}: {ex}", flush=True)
            
        return None