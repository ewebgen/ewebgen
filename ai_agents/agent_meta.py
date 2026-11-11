import json
import requests
from ai_agents.base_agent import BaseAIAgent
from ai_agents.openai_compat import stream_chat
from services_core import download_image_server_side

class MetaAgent:
    """
    Handles API interactions specifically for Meta's Llama models, routed via OpenRouter.
    """

    # Meta Llama is reached through one of three OpenAI-compatible services, detected from the key:
    #   gsk_...  -> Groq            (default)
    #   sk-or-.. -> OpenRouter
    #   LLM|...  -> Meta Llama API  (api.llama.com)
    ROUTES = {
        'groq': {
            'url': "https://api.groq.com/openai/v1/chat/completions",
            'models': ["llama-3.3-70b-versatile", "meta-llama/llama-4-maverick-17b-128e-instruct", "meta-llama/llama-4-scout-17b-16e-instruct"],
            'max_out': 32000, 'json_mode': True,
        },
        'openrouter': {
            'url': "https://openrouter.ai/api/v1/chat/completions",
            'models': ["meta-llama/llama-4-maverick", "meta-llama/llama-3.3-70b-instruct"],
            'max_out': 32000, 'json_mode': True,
        },
        'llama_api': {
            'url': "https://api.llama.com/compat/v1/chat/completions",
            'models': ["Llama-4-Maverick-17B-128E-Instruct-FP8", "Llama-3.3-70B-Instruct"],
            'max_out': 16000, 'json_mode': False,
        },
    }
    RETIRED = {"llama3-70b-8192", "llama3-8b-8192", "llama2-70b-4096", "llama-3.1-70b-versatile", "llama-3.2-90b-text-preview",
               "meta-llama/llama-3-70b-chat", "meta-llama/Llama-3-8b-chat-hf"}

    @staticmethod
    def route_for_key(api_key):
        k = (api_key or '').strip()
        if k.startswith('sk-or-'):
            return 'openrouter'
        if k.startswith('LLM|'):
            return 'llama_api'
        return 'groq'

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Meta (Groq / OpenRouter / Llama API) key missing.'})}\n\n"
            return
        route = MetaAgent.route_for_key(api_key)
        cfg = MetaAgent.ROUTES[route]
        custom = model_name if model_name and model_name not in MetaAgent.RETIRED else None
        out_tokens = min(max_tokens, cfg['max_out']) if max_tokens else cfg['max_out']
        extra_headers = {"HTTP-Referer": "https://ewebgen.com", "X-Title": "eWebGen Website Builder"} if route == 'openrouter' else {}
        yield from stream_chat(
            provider_label="Meta Llama",
            url=cfg['url'], api_key=api_key,
            models=[custom] + cfg['models'],
            history=history, system_instruction=system_instruction,
            out_tokens=out_tokens,
            user_id=user_id, is_new_generation=is_new_generation,
            extra_headers=extra_headers, json_mode=cfg['json_mode'], temperature=0.5, read_timeout=600,
        )

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        """
        Attempts to generate an image if a custom image model is specified for Meta.
        """
        if not api_key or not model_name:
            return None
            
        print(f"🎨 [Image Gen] Attempting META using custom Image Model: {model_name}...", flush=True)
        try:
            headers = { "Authorization": f"Bearer {api_key}", "Content-Type": "application/json" }
            req_prompt = prompt
            if img_type == 'logo': 
                req_prompt = f"minimalist vector logo, icon, {prompt}, white background, flat style"
                
            payload = { "model": model_name, "prompt": req_prompt, "n": 1, "size": "1024x1024" }
            url_post = 'https://api.together.xyz/v1/images/generations' 
            
            # FIX: Increased image generation timeout from 30 to 60 seconds
            resp = requests.post(url_post, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data'):
                    url = data['data'][0]['url']
                    local_path = download_image_server_side(url, filename_prefix=f"gallery_ai_meta_")
                    if local_path: 
                        return [local_path]
        except Exception as ex:
            print(f"❌ META Custom Image Gen Failed: {ex}", flush=True)
            
        return None