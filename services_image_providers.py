import re
import json
import requests
from urllib.parse import quote

# Import app context and models
from database_setup import app, SystemSettings

# Import Decryption Service
try:
    from services_core import FTPEncryptionService
    def decrypt_sensitive_data(val):
        if not val: return val
        try:
            dec = FTPEncryptionService.decrypt_credentials(val)
            if dec and 'sec_val' in dec: return dec['sec_val']
        except: pass
        return val
except ImportError:
    def decrypt_sensitive_data(val):
        return val

# =======================================================
# IMAGE PROVIDER SERVICE (STOCK PHOTOS)
# =======================================================

class ImageProviderService:
    generation_counter = 0

    @staticmethod
    def get_image_url(query, width=800, height=600):
        ImageProviderService.generation_counter += 1
        clean_query = re.sub(r'[^a-zA-Z0-9 ]', '', query)

        with app.app_context():
            settings = SystemSettings.query.first()
            providers = {}
            if settings and settings.image_providers_json:
                try: 
                    providers = json.loads(settings.image_providers_json)
                except: 
                    providers = {}
            
            # SECURITY FIX: Decrypt the master Pexels key if it exists
            db_pexels_key = decrypt_sensitive_data(settings.pexels_api_key) if settings else ""
            
            if 'pexels' not in providers:
                 providers['pexels'] = {"key": db_pexels_key, "enabled": True, "type": "free"}
            else:
                 if not providers['pexels'].get('key') and db_pexels_key:
                     providers['pexels']['key'] = db_pexels_key

            candidates = []
            for name, config in providers.items():
                if config.get('enabled', False):
                    if name.lower() == 'pollinations':
                        continue
                        
                    single_key = config.get('key', '').strip()
                    keys_list = config.get('keys', [])
                    provider_type = config.get('type')
                    
                    if name == 'pexels' and not single_key and not keys_list: continue 
                    if provider_type == 'paid' and not single_key and not keys_list: continue
                    
                    candidates.append(name)
            
            if not candidates:
                # Safe Fallback to Placehold.co if no providers are configured
                return ImageProviderService.get_fallback_image(clean_query, width, height)

            rotation_index = (ImageProviderService.generation_counter // 5) % len(candidates)
            priority_list = candidates[rotation_index:] + candidates[:rotation_index]
            
            for provider in priority_list:
                config = providers[provider]
                key_pool = config.get('keys', [])
                if not key_pool and config.get('key'): 
                    key_pool = [config.get('key')]
                    
                key_to_use = ""
                if key_pool:
                    k_idx = ImageProviderService.generation_counter % len(key_pool)
                    key_to_use = key_pool[k_idx]
                
                # SECURITY FIX: Decrypt the key right before injecting it into the header
                actual_key = decrypt_sensitive_data(key_to_use)

                try:
                    url = None
                    if provider == 'pexels': url = ImageProviderService.fetch_pexels(query, actual_key)
                    elif provider == 'unsplash': url = ImageProviderService.fetch_unsplash(query, actual_key)
                    elif provider == 'pixabay': url = ImageProviderService.fetch_pixabay(query, actual_key)
                    elif provider == 'freepik': url = ImageProviderService.fetch_freepik(query, actual_key)
                    elif provider == 'shutterstock': url = ImageProviderService.fetch_shutterstock(query, actual_key)
                    elif provider == 'mighty': url = ImageProviderService.fetch_mighty(query, actual_key)
                    
                    if url and url.startswith('http'): 
                        print(f"✅ [Image Provider] Successfully fetched image from {provider}", flush=True)
                        return url
                    else:
                        print(f"⚠️ [Image Provider] {provider} returned empty URL. Trying next...", flush=True)
                except Exception as e:
                    print(f"⚠️ [Image Provider Error] {provider} failed: {e}. Trying next available provider...", flush=True)
                    continue 

            # Safe Fallback to Placehold.co if external APIs reject the concurrent requests (Rate Limiting)
            print("⚠️ [Image Fallback] All configured image providers failed or rate-limited.", flush=True)
            return ImageProviderService.get_fallback_image(clean_query, width, height)

    @staticmethod
    def search_images(query, limit=10):
        images = []
        with app.app_context():
            settings = SystemSettings.query.first()
            if not settings: return []
            search_limit = settings.image_search_limit or limit

            # SECURITY FIX: Decrypt API keys before making remote requests
            decrypted_pexels = decrypt_sensitive_data(settings.pexels_api_key)
            if decrypted_pexels:
                try:
                    headers = { "Authorization": decrypted_pexels }
                    url = "https://api.pexels.com/v1/search"
                    # Enforce SSL validation implicitly by not passing verify=False
                    resp = requests.get(url, headers=headers, params={"query": query, "per_page": search_limit}, timeout=5)
                    if resp.status_code == 200:
                        for p in resp.json().get('photos', []): 
                            images.append(p['src']['medium'])
                except: pass

            unsplash_key = decrypt_sensitive_data(getattr(settings, 'unsplash_api_key', None)) or ''
            if not unsplash_key:
                try:
                    provs = json.loads(settings.image_providers_json or '{}')
                    if 'unsplash' in provs:
                        unsplash_key = decrypt_sensitive_data(provs['unsplash'].get('key', ''))
                except: pass

            if len(images) < search_limit and unsplash_key:
                try:
                    url = "https://api.unsplash.com/search/photos"
                    resp = requests.get(url, params={"query": query, "client_id": unsplash_key, "per_page": search_limit}, timeout=5)
                    if resp.status_code == 200:
                        for p in resp.json().get('results', []): 
                            images.append(p['urls']['regular'])
                except: pass

        if not images:
            for i in range(5): 
                base_safe = "https://placehold.co"
                images.append(f"{base_safe}/800x600?text=Image")
                
        return images[:search_limit]

    @staticmethod
    def get_fallback_image(query, width, height):
        safe_query = quote(query[:20] if query else "Image")
        base_url = "https://placehold.co"
        print("⚠️ [Image Fallback] Ultimate safe fallback triggered.", flush=True)
        return f"{base_url}/{width}x{height}?text={safe_query}"

    @staticmethod
    def fetch_pexels(query, key):
        if not key: return None
        headers = { "Authorization": key }
        params = { "query": query, "orientation": "landscape", "size": "medium", "per_page": 1 }
        url = "https://api.pexels.com/v1/search"
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('photos'): return data['photos'][0]['src']['medium']
        return None

    @staticmethod
    def fetch_unsplash(query, key):
        if not key: return None
        headers = { "Authorization": f"Client-ID {key}" }
        params = { "query": query, "orientation": "landscape", "per_page": 1 }
        url = "https://api.unsplash.com/search/photos"
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('results'): return data['results'][0]['urls']['regular']
        return None
        
    @staticmethod
    def fetch_pixabay(query, key):
        if not key: return None
        params = { "key": key, "q": query, "image_type": "photo", "per_page": 3 }
        url = "https://pixabay.com/api/"
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('hits'): return data['hits'][0]['webformatURL']
        return None

    @staticmethod
    def fetch_freepik(query, key):
        if not key: return None
        headers = { "X-Freepik-API-Key": key }
        params = { "term": query, "limit": 1 }
        url = "https://api.freepik.com/v1/resources"
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('data') and len(data['data']) > 0:
                return data['data'][0]['image']['source']['url']
        return None

    @staticmethod
    def fetch_shutterstock(query, key):
        if not key: return None
        auth_header = key if "Basic" in key or "Bearer" in key else f"Bearer {key}"
        headers = { "Authorization": auth_header }
        params = { "query": query, "per_page": 1, "view": "minimal" }
        url = "https://api.shutterstock.com/v2/images/search"
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('data') and len(data['data']) > 0:
                return data['data'][0]['assets']['preview']['url']
        return None

    @staticmethod
    def fetch_mighty(query, key):
        if not key: return None
        headers = { "Authorization": key }
        params = { "q": query, "limit": 1 }
        try:
            url = "https://api.mighty-images.com/v1/search"
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('url'): return data['url']
        except: pass
        return None

def get_pexels_image_url(query, width=800, height=600):
    return ImageProviderService.get_image_url(query, width, height)