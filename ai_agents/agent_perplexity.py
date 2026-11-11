import json
import requests
from ai_agents.base_agent import BaseAIAgent
from ai_agents.openai_compat import stream_chat
from services_core import download_image_server_side

class PerplexityAgent:
    """
    Handles all API interactions specifically for Perplexity AI models.
    """

    RETIRED_PREFIXES = ("llama-3.1-sonar", "llama-3-sonar", "pplx-")

    @staticmethod
    def generate_stream(api_key, model_name, history, system_instruction, user_id=None, is_new_generation=False, max_tokens=None):
        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Perplexity API Key missing.'})}\n\n"
            return
        # Perplexity's Sonar models are search models: switch web search off so the answer is pure code
        # (no citations / prose), use sonar-pro (largest output), and plan for its ~8K-token output limit.
        custom = model_name if model_name and not model_name.startswith(PerplexityAgent.RETIRED_PREFIXES) else None
        out_tokens = min(max_tokens, 8000) if max_tokens else 8000
        yield from stream_chat(
            provider_label="Perplexity",
            url="https://api.perplexity.ai/chat/completions",
            api_key=api_key,
            models=[custom, "sonar-pro", "sonar"],
            history=history, system_instruction=system_instruction,
            out_tokens=out_tokens,
            user_id=user_id, is_new_generation=is_new_generation,
            extra_payload={"disable_search": True},
            json_mode=False, temperature=0.3, read_timeout=600,
        )

    @staticmethod
    def generate_image(api_key, model_name, prompt, img_type='image', text_overlay=''):
        """
        Perplexity does not natively support an image generation API endpoint for public use in the same way.
        This function gracefully falls back or attempts a pass-through if they ever add one.
        """
        print(f"🎨 [Image Gen] Perplexity does not currently support native image generation. Falling back.", flush=True)
        return None