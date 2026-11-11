"""
Shared streaming engine for OpenAI-compatible chat APIs (xAI Grok, Meta Llama via Groq /
OpenRouter / Meta Llama API, Perplexity).

Why these providers produced weak designs before:
  * their extra "design mandate" told the model to put ALL CSS in a <style> tag, while the main
    system prompt says CSS must go in styles.css and pages must NOT contain <style> tags.
    Smaller models followed one or the other at random -> thin / missing styling;
  * retired default models (llama3-70b-8192 = 8K context, llama-3.1-sonar-*-online) and output
    limits too small for a full multi-page site -> truncated JSON -> broken or bare pages;
  * Perplexity's web search added citations and prose; reasoning models emit <think> blocks
    that confuse the JSON extractor; short read timeouts cut off slow reasoning models.

This module fixes those for all three: one consistent design brief, per-model output budgets
(with a compact plan when the budget is small), JSON mode when supported, model fallbacks,
long read timeouts and cleanup of <think> blocks / citations before parsing.
"""
import json
import re
import requests

from ai_agents.base_agent import BaseAIAgent


DESIGN_BRIEF = (
    "\n\n[DESIGN QUALITY BRIEF - FOLLOW EXACTLY]\n"
    "You are a senior UI designer and front-end engineer. The result must look like a premium, modern, "
    "professionally designed website - never plain, bare or default-browser looking.\n"
    "CSS LOCATION (VERY IMPORTANT): put ALL CSS in the \"styles.css\" file of the JSON. Every HTML page links it with "
    "<link rel=\"stylesheet\" href=\"styles.css\">. Do NOT put <style> blocks in the HTML pages. No Tailwind or Bootstrap.\n"
    "styles.css MUST be complete and rich, and contain at least:\n"
    "  1. :root design tokens - primary, secondary, accent, dark, light, text and muted colours; radius; shadows; spacing; "
    "a premium palette (deep navy/slate + one vivid accent such as blue, emerald, violet or coral). Never brown/beige. High contrast.\n"
    "  2. Base styles: *, html, body, typography scale (h1 3.2-4.2rem/800, h2 2.2-2.8rem/700, body 1.05rem, line-height 1.7), links, images "
    "(max-width:100%; height:auto; display:block; object-fit:cover; border-radius:12px).\n"
    "  3. Layout: .container {max-width:1200px; margin:0 auto; padding:0 24px}; sections padding 80-110px 0; CSS Grid and Flexbox.\n"
    "  4. Header/nav: sticky, glass effect (backdrop-filter), logo, links with hover underline, CTA button, dropdowns hidden by default "
    "(position:absolute; z-index) and shown on :hover, a single hamburger button for mobile.\n"
    "  5. Components: buttons (primary/outline, pill or 10px radius, hover lift), cards (16-24px radius, layered soft shadows, hover translateY(-6px)), "
    "icon circles for FontAwesome icons, badges, stats, testimonials, pricing or feature grid, CTA band with gradient, contact form "
    "with styled inputs, multi-column dark footer.\n"
    "  6. Hero: striking gradient or image background with overlay, big headline, sub-text, two buttons, image or visual.\n"
    "  7. Motion: transitions on interactive elements and at least two @keyframes (fadeInUp, float) applied to hero/cards.\n"
    "  8. Responsive: media queries at 992px and 768px (stack grids, show hamburger, reduce font sizes).\n"
    "HTML: every page has the same full header/nav and footer, and 4-6 distinct, content-rich <section> blocks with realistic, "
    "industry-specific copy (no Lorem Ipsum, no [placeholders]). Use FontAwesome icons (<i class=\"fas fa-...\"></i>). "
    "Images use src=\"/images/placeholder.jpg\" with a descriptive alt text.\n"
)

COMPACT_PLAN = (
    "\n[OUTPUT BUDGET]: Your output limit is about {tokens} tokens. A truncated answer is worthless, so plan to FINISH: "
    "write index.html completely and richly, then the other requested pages more briefly (3-4 sections each, max {pages} extra pages), "
    "and keep styles.css focused (about 300-500 lines, no repetition). Close every string and the JSON object.\n"
)

FINAL_REMINDER = (
    "\n\n[OUTPUT]: Reply with ONLY one valid JSON object - no markdown fences, no explanations, no citations. "
    "Start with {{ and end with }}. Keys are file names (\"index.html\", other pages, \"styles.css\") plus \"current_page\". "
    "All CSS goes in \"styles.css\".{extra}"
)


def build_messages(history, system_instruction, out_tokens):
    compact = out_tokens and out_tokens < 24000
    system = system_instruction + DESIGN_BRIEF
    if compact:
        system += COMPACT_PLAN.format(tokens=out_tokens, pages=2 if out_tokens < 12000 else 3)
    messages = [{"role": "system", "content": system}]
    for i, item in enumerate(history):
        role = item.get('role', 'user')
        if role == 'model':
            role = 'assistant'
        try:
            content = item.get('parts', [{}])[0].get('text', '')
        except (AttributeError, IndexError):
            content = str(item.get('content', ''))
        if i == len(history) - 1 and role == 'user':
            content += FINAL_REMINDER.format(extra=" Finish the JSON completely." if compact else "")
        messages.append({"role": role, "content": content})
    return messages


def clean_model_text(text):
    """Remove reasoning blocks and stray citations so the JSON extractor sees only the answer."""
    if not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S | re.I)
    if '<think>' in text.lower() and '</think>' not in text.lower():
        text = text[text.lower().index('<think>') + 7:]
    # Prose before the JSON (e.g. "Here is your website:") is dropped by the JSON extractor,
    # but citation markers like [1][2] right before it can contain no braces, so they are harmless.
    return text.strip()


def _is_model_error(status, body):
    b = (body or '').lower()
    return status in (400, 404) and any(w in b for w in ('model', 'not found', 'does not exist', 'decommission', 'deprecat', 'invalid_model'))


def _is_param_error(status, body):
    b = (body or '').lower()
    return status in (400, 422) and any(w in b for w in ('response_format', 'json_object', 'disable_search', 'unrecognized',
                                                         'unknown', 'not supported', 'unsupported', 'extra', 'max_tokens', 'max_completion'))


def stream_chat(*, provider_label, url, api_key, models, history, system_instruction, out_tokens,
                user_id=None, is_new_generation=False, extra_headers=None, extra_payload=None,
                json_mode=True, temperature=0.5, read_timeout=600):
    """
    Streams an OpenAI-compatible chat completion as SSE chunks for the editor, trying each model in
    `models` until one is accepted, and dropping optional parameters the API rejects.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    messages = build_messages(history, system_instruction, out_tokens)

    optional = {}
    if json_mode:
        optional["response_format"] = {"type": "json_object"}
    optional.update(extra_payload or {})

    last_error = ""
    tried = []
    for model in list(dict.fromkeys(m for m in models if m)):
        params = dict(optional)
        tokens = out_tokens
        for attempt in range(4):
            payload = {"model": model, "messages": messages, "stream": True, "temperature": temperature}
            if tokens:
                payload["max_tokens"] = tokens
            payload.update(params)
            tried.append(model)
            try:
                resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=(20, read_timeout))
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                break
            if resp.status_code == 200:
                yield from _relay(resp, provider_label, model, user_id, is_new_generation)
                return
            body = resp.text[:800]
            last_error = f"{provider_label} API Error {resp.status_code}: {body}"
            print(f"⚠️ [{provider_label}] {model}: {last_error}", flush=True)
            if _is_param_error(resp.status_code, body):
                low = body.lower()
                if 'max_tokens' in low or 'max_completion' in low:
                    # Ask for less output (keep the compact plan in the prompt)
                    tokens = max(4000, int((tokens or 8000) * 0.5)) if tokens else None
                    continue
                dropped = False
                for k in list(params):
                    if k.replace('_', '') in low.replace('_', '') or k in ('response_format', 'disable_search'):
                        params.pop(k, None)
                        dropped = True
                        break
                if dropped:
                    continue
            if _is_model_error(resp.status_code, body) or resp.status_code in (404,):
                break       # try the next model
            yield f"data: {json.dumps({'type': 'error', 'message': last_error})}\n\n"
            return

    msg = last_error or f"{provider_label}: no model accepted the request."
    if tried:
        msg += f" (models tried: {', '.join(dict.fromkeys(tried))})"
    yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"


def _relay(resp, provider_label, model, user_id, is_new_generation):
    print(f"🤖 [{provider_label}] streaming with model {model}", flush=True)
    full_text = ""
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            decoded = line.decode('utf-8', errors='ignore')
            if not decoded.startswith("data: "):
                continue
            data_str = decoded[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk_json = json.loads(data_str)
            except ValueError:
                continue
            choices = chunk_json.get('choices') or []
            if not choices:
                continue
            delta = choices[0].get('delta', {}) or {}
            content = delta.get('content') or ''
            if content:
                full_text += content
                yield f"data: {json.dumps({'type': 'chunk', 'content': content})}\n\n"
    except requests.exceptions.RequestException as e:
        if not full_text:
            yield f"data: {json.dumps({'type': 'error', 'message': f'{provider_label} connection lost: {e}'})}\n\n"
            return
        print(f"⚠️ [{provider_label}] stream interrupted, using partial output: {e}", flush=True)
    yield BaseAIAgent.finalize_response(clean_model_text(full_text), user_id, is_new_generation)
