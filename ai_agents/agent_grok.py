import json
import requests
from ai_agents.base_agent import BaseAIAgent
from ai_agents.openai_compat import stream_chat
from services_core import download_image_server_side

class GrokAgent:
    """
    Handles all API interactions specifically for xAI's Grok models.
    """

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Grok API Key missing.'})}\n\n"
            return
        # Grok 4 models are reasoning models: they can think for minutes before the first token,
        # so a long read timeout is required. Fall back to other Grok models if one is not available.
        yield from stream_chat(
            provider_label="Grok",
            url="https://api.x.ai/v1/chat/completions",
            api_key=api_key,
            models=[model_name, "grok-4.3", "grok-4", "grok-4-fast", "grok-3"],
            history=history, system_instruction=system_instruction,
            out_tokens=max_tokens or 40000,
            user_id=user_id, is_new_generation=is_new_generation,
            json_mode=True, temperature=0.4, read_timeout=900,
        )

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        """
        Attempts to generate an image if a custom image model is specified for Grok.
        Returns the local URL list on success, or None on failure (triggering DALL-E fallback).
        """
        if not api_key or not model_name:
            return None
            
        print(f"🎨 [Image Gen] Attempting GROK using custom Image Model: {model_name}...", flush=True)
        try:
            headers = { "Authorization": f"Bearer {api_key}", "Content-Type": "application/json" }
            req_prompt = prompt
            if img_type == 'logo': 
                req_prompt = f"minimalist vector logo, icon, {prompt}, white background, flat style"
                
            payload = { "model": model_name, "prompt": req_prompt, "n": 1, "size": "1024x1024" }
            
            # FIX: Safely assign the URL string
            grok_img_url = "https://api.x.ai/v1/images/generations"
            resp = requests.post(grok_img_url, headers=headers, json=payload, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data'):
                    url = data['data'][0]['url']
                    local_path = download_image_server_side(url, filename_prefix=f"gallery_ai_grok_")
                    if local_path: 
                        return [local_path]
        except Exception as ex:
            print(f"❌ GROK Custom Image Gen Failed: {ex}", flush=True)
            
        return None