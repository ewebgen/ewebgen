import os, socket, hashlib, requests, json, time, hmac, re
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Force Python to load .env variables using absolute path
# (compiled builds keep .env / data in EWEBGEN_DATA_DIR, the original app folder)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if os.getenv('EWEBGEN_DATA_DIR'):
    load_dotenv(os.path.join(os.getenv('EWEBGEN_DATA_DIR'), '.env'))
load_dotenv(os.path.join(BASE_DIR, '.env'))
load_dotenv()

# Redundant Owner Servers for Failover Logic (OWNER_SERVER_URL in .env overrides / adds a server)
OWNER_SERVERS = [u.strip().rstrip('/') for u in os.getenv("OWNER_SERVER_URL", "").split(',') if u.strip()] or ["https://app.ewebgen.com"]

# --- License timing rules ---
GRACE_PERIOD_DAYS = 7          # kept for backward compatibility
RETRY_PERIOD_DAYS = 7
ONLINE_CHECK_HOURS = int(os.getenv("LICENSE_CHECK_HOURS", "1"))      # re-verify with owner server (hours)
OFFLINE_GRACE_DAYS = int(os.getenv("LICENSE_OFFLINE_GRACE_DAYS", "7"))  # only if owner unreachable
BLOCKED_RECHECK_MINUTES = 3     # expired/blocked: re-ask owner (detect renewal) at most every 3 min
UNREACHABLE_RETRY_MINUTES = 15  # owner down: retry at most every 15 min

class SecurityEngine:

    # ------------------------------------------------------------------
    # ACTIVATION MODE
    #   website  -> license bound to the DOMAIN URL
    #   desktop  -> license bound to this computer's MAC address
    # The desktop launcher sets EWEBGEN_DESKTOP=1.
    # ------------------------------------------------------------------
    @staticmethod
    def is_desktop_mode():
        """Desktop edition: EWEBGEN_DESKTOP=1 (set by the launcher) or edition.txt containing 'desktop'."""
        if str(os.getenv("EWEBGEN_DESKTOP", "")).strip().lower() in ("1", "true", "yes"):
            return True
        try:
            with open(os.path.join(BASE_DIR, 'edition.txt'), 'r', encoding='utf-8') as f:
                return f.read().strip().lower().startswith('desktop')
        except OSError:
            return False

    @staticmethod
    def get_target_type():
        return "app" if SecurityEngine.is_desktop_mode() else "website"

    @staticmethod
    def get_hardware_id():
        """Primary network adapter MAC address, formatted AA:BB:CC:DD:EE:FF."""
        override = os.getenv("EWEBGEN_HARDWARE_ID")
        if override:
            return override.strip().upper()
        import uuid as _uuid
        node = _uuid.getnode()
        # If the multicast bit is set Python generated a random number (no NIC found):
        # fall back to a stable machine fingerprint instead so the binding never drifts.
        if (node >> 40) & 0x01:
            raw = f"{socket.gethostname()}-{os.getenv('COMPUTERNAME', '')}-{os.getenv('PROCESSOR_IDENTIFIER', '')}"
            node = int(hashlib.sha256(raw.encode()).hexdigest()[:12], 16) & 0xFEFFFFFFFFFF
        return ':'.join(f"{(node >> s) & 0xFF:02X}" for s in range(40, -1, -8))

    @staticmethod
    def get_identity():
        """Value stored in the local lease and checked for tampering/copying."""
        if SecurityEngine.is_desktop_mode():
            return "desktop-" + SecurityEngine.get_hardware_id().replace(':', '')
        return SecurityEngine.get_active_domain()

    @staticmethod
    def build_owner_payload(license_key=None):
        if SecurityEngine.is_desktop_mode():
            return {"domain_name": "", "hardware_id": SecurityEngine.get_hardware_id(),
                    "license_key": license_key or "", "target_type": "app"}
        return {"domain_name": SecurityEngine.get_active_domain(), "hardware_id": "",
                "license_key": license_key or "", "target_type": "website"}

    @staticmethod
    def get_free_plan_status():
        payload = SecurityEngine.build_owner_payload()
        res = SecurityEngine.call_owner_server("/api/license/free_plan", payload)
        if not isinstance(res, dict) or 'available' not in res:
            return {"success": False, "available": False, "error": (res or {}).get("message", "Owner server unreachable")}
        return res

    @staticmethod
    def activate_free_plan(email="", name=""):
        payload = SecurityEngine.build_owner_payload()
        payload.update({"email": email, "name": name})
        res = SecurityEngine.call_owner_server("/api/license/activate_free", payload, accept_errors=True)
        if not res or not res.get("success") or not res.get("license_key"):
            return False, (res or {}).get("error") or (res or {}).get("message") or "Free activation failed."
        valid, msg = SecurityEngine.verify_license_logic(license_key=res["license_key"], silent=False)
        if not valid:
            return False, msg
        return True, res.get("message") or "Activated."

    @staticmethod
    def get_active_domain():
        """Extracts the active domain cleanly to prevent multi-tenant file conflicts."""
        flask_domain = None
        try:
            from flask import request
            if request and hasattr(request, 'host'):
                fwd = (request.headers.get('X-Forwarded-Host') or '').split(',')[0].strip()
                flask_domain = (fwd or request.host).split(':')[0].lower()
        except Exception:
            pass

        app_domain = os.getenv("APP_DOMAIN")
        if app_domain:
            return app_domain.replace("https://", "").replace("http://", "").split('/')[0].lower()
        elif flask_domain and not flask_domain.startswith(("192.168.", "10.", "172.", "127.")):
            return flask_domain
        return "localhost"

    @staticmethod
    def get_lease_filename(domain_name):
        """
        Generates a unique lease filename per domain to prevent race conditions 
        when multiple subdomains (e.g., demo.ewebgen.com and web.ewebgen.com) 
        share the same physical application directory.
        """
        safe_domain = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', domain_name)
        lease_dir = os.path.abspath(os.getenv('EWEBGEN_DATA_DIR') or BASE_DIR)
        specific_file = os.path.join(lease_dir, f"app_auth_{safe_domain}.lic")
        legacy_file = os.path.join(lease_dir, "app_auth.lic")
        
        # If the domain-specific file exists, always use it
        if os.path.exists(specific_file):
            return specific_file
            
        # Auto-migrate legacy license file if it belongs to this exact domain
        if os.path.exists(legacy_file):
            try:
                with open(legacy_file, 'r') as f:
                    data = json.loads(f.read())
                if data.get('domain_name') == domain_name:
                    os.rename(legacy_file, specific_file)
                    return specific_file
            except Exception:
                pass
                
        # Default to specific file path for new creations
        return specific_file

    @staticmethod
    def get_current_environment():
        """Detects if running on localhost or a public domain."""
        if os.getenv("APP_DOMAIN"):
            return "server"

        hostname = socket.gethostname()
        try:
            ip = socket.gethostbyname(hostname)
            is_local = ip in ["127.0.0.1", "localhost", "0.0.0.0"] or ".local" in hostname
        except:
            is_local = True
            
        return "local" if is_local else "server"

    @staticmethod
    def call_owner_server(endpoint, payload, accept_errors=False):
        """Redundancy Logic: Tries Site A, then Site B if A is down."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        last_error = ""
        for server in OWNER_SERVERS:
            try:
                url = f"{server}{endpoint}"
                resp = requests.post(url, json=payload, headers=headers, timeout=15)
                
                if resp.status_code == 200:
                    return resp.json()
                elif accept_errors and resp.headers.get('Content-Type', '').startswith('application/json'):
                    return resp.json()
                else:
                    last_error = f"HTTP {resp.status_code}"
            except Exception as e:
                last_error = type(e).__name__
                continue 
                
        return {"valid": False, "unreachable": True, "message": f"Firewall/Connection Blocked: {last_error}"}

    @staticmethod
    def generate_signature(data_dict):
        """Generates a tamper-proof HMAC signature for the local lease file."""
        data_str = json.dumps(data_dict, sort_keys=True)
        secret_key = os.getenv("SECRET_KEY", "fallback-secure-hmac-key-change-me")
        return hmac.new(secret_key.encode(), data_str.encode(), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # LOCAL LEASE (signed cache of the owner server's last answer)
    # ------------------------------------------------------------------
    @staticmethod
    def _write_lease(identity, lease):
        lease = {k: v for k, v in lease.items() if k != 'signature'}
        lease['domain_name'] = identity
        lease['signature'] = SecurityEngine.generate_signature(lease)
        with open(SecurityEngine.get_lease_filename(identity), 'w') as f:
            f.write(json.dumps(lease, indent=4))

    @staticmethod
    def save_lease(domain_name, license_key, current_time, warning_msg="", info=None):
        """Saves a lease after a SUCCESSFUL online verification."""
        info = info or {}
        SecurityEngine._write_lease(domain_name, {
            "hardware_id": SecurityEngine.get_hardware_id() if SecurityEngine.is_desktop_mode() else "",
            "license_key": license_key,
            "last_verified": current_time.isoformat(),
            "last_attempt": current_time.isoformat(),
            "last_retry": "",
            "warning": warning_msg or "",
            "status": "active",
            "message": "",
            "expiry_date": info.get("expiry_date") or "",
            "is_free": bool(info.get("is_free")),
            "renew_url": info.get("renew_url") or "",
            "plan_type": info.get("plan_type") or "",
        })

    @staticmethod
    def read_lease():
        """Returns (identity, lease_dict_or_None). Removes tampered / copied lease files."""
        identity = SecurityEngine.get_identity()
        lease_file = SecurityEngine.get_lease_filename(identity)
        if not os.path.exists(lease_file):
            return identity, None
        try:
            with open(lease_file, 'r') as f:
                lease = json.loads(f.read())
        except Exception:
            return identity, None
        sig = lease.pop('signature', '')
        if sig != SecurityEngine.generate_signature(lease):
            SecurityEngine.log_error_to_owner(identity, SecurityEngine._env_label(identity), lease.get('license_key', ''),
                                              "Tampered license file signature detected.")
            try: os.remove(lease_file)
            except Exception: pass
            return identity, None
        if lease.get('domain_name') != identity:
            SecurityEngine.log_error_to_owner(identity, SecurityEngine._env_label(identity), lease.get('license_key', ''),
                                              f"Domain Mismatch! Bound: {lease.get('domain_name')} | Requested: {identity}")
            try: os.remove(lease_file)
            except Exception: pass
            return identity, None
        return identity, lease

    @staticmethod
    def get_license_warning():
        """Reads the local lease file to retrieve any active warnings."""
        _, lease = SecurityEngine.read_lease()
        return (lease or {}).get('warning', '') if lease and lease.get('status', 'active') == 'active' else ""

    @staticmethod
    def _env_label(identity):
        if SecurityEngine.is_desktop_mode(): return "desktop"
        if identity == "localhost" and not os.getenv("APP_DOMAIN"): return "local"
        return "server"

    @staticmethod
    def _parse_dt(val):
        if not val: return None
        try:
            dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
        except ValueError:
            try: dt = datetime.strptime(str(val)[:10], "%Y-%m-%d")
            except ValueError: return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def get_license_state():
        """Offline summary for the activation / renewal screen."""
        identity, lease = SecurityEngine.read_lease()
        if not lease or not lease.get('license_key'):
            return {"state": "none", "target_type": SecurityEngine.get_target_type()}
        now = datetime.now(timezone.utc)
        exp = SecurityEngine._parse_dt(lease.get('expiry_date'))
        status = lease.get('status') or 'active'
        if status == 'active' and exp and now >= exp:
            status = 'expired'
        key = lease.get('license_key', '')
        return {
            "state": status,
            "target_type": SecurityEngine.get_target_type(),
            "license_key_masked": (key[:5] + "••••" + key[-4:]) if len(key) > 9 else key,
            "expiry_date": exp.strftime("%Y-%m-%d") if exp else "",
            "is_free": bool(lease.get('is_free')),
            "renew_url": lease.get('renew_url') or f"{OWNER_SERVERS[0]}/license/renew?key={key}",
            "message": lease.get('message') or "",
            "plan_type": lease.get('plan_type') or "",
        }

    @staticmethod
    def _online_verify(license_key):
        """Returns (outcome, result): outcome in 'valid' | 'invalid' | 'unreachable'."""
        payload = SecurityEngine.build_owner_payload(license_key)
        try:
            result = SecurityEngine.call_owner_server("/api/license/verify", payload, accept_errors=True) or {}
        except Exception as e:
            return 'unreachable', {"message": str(e)}
        if result.get('valid'):
            return 'valid', result
        # Only a clear answer from the owner counts as a refusal
        if result.get('status') and result.get('status') not in ('rate_limited', 'error'):
            return 'invalid', result
        return 'unreachable', result

    @staticmethod
    def verify_license_logic(license_key=None, silent=False, force_online=False):
        """
        License rules
          * expiry date from the owner server is enforced locally (even offline)
          * online re-check every LICENSE_CHECK_HOURS (default 1h)
          * an explicit refusal (expired / suspended / invalid ...) blocks immediately
          * offline grace (OFFLINE_GRACE_DAYS) ONLY when the owner server can't be reached,
            and never beyond the expiry date
        """
        identity, lease = SecurityEngine.read_lease()
        env = SecurityEngine._env_label(identity)
        now = datetime.now(timezone.utc)

        new_key = (license_key or '').strip()
        lease = lease or {}
        key = new_key or lease.get('license_key', '')
        if not key:
            return False, "No valid license key found. Please activate."

        exp = SecurityEngine._parse_dt(lease.get('expiry_date')) if not new_key else None
        last_verified = SecurityEngine._parse_dt(lease.get('last_verified')) or (now - timedelta(days=999))
        last_attempt = SecurityEngine._parse_dt(lease.get('last_attempt'))
        status = lease.get('status') or 'active'
        expired_locally = bool(exp and now >= exp)

        if not new_key and not force_online:
            # Healthy lease, checked recently -> no network call
            if status == 'active' and not expired_locally and (now - last_verified) < timedelta(hours=ONLINE_CHECK_HOURS):
                return True, "Lease valid"
            # Blocked / expired lease: re-ask the owner at most every few minutes (detects renewals)
            if (status != 'active' or expired_locally) and last_attempt and (now - last_attempt) < timedelta(minutes=BLOCKED_RECHECK_MINUTES):
                return False, lease.get('message') or (f"License expired on {exp.strftime('%Y-%m-%d')}." if exp else "License is not active.")
            # Owner was unreachable a moment ago: don't block every request on the network
            if status == 'active' and not expired_locally and last_attempt and (now - last_attempt) < timedelta(minutes=UNREACHABLE_RETRY_MINUTES):
                if (now - last_verified) < timedelta(days=OFFLINE_GRACE_DAYS):
                    return True, "Lease valid (offline grace)"

        outcome, result = SecurityEngine._online_verify(key)

        if outcome == 'valid':
            SecurityEngine.save_lease(identity, key, now, result.get('warning', ''), result)
            return True, "Verified Online"

        if outcome == 'invalid':
            msg = result.get('message') or 'License is not active.'
            st = result.get('status') or 'invalid'
            if new_key:
                # Key typed on the activation screen was refused: keep the existing lease untouched
                if not silent or "Mismatch" in msg or "Invalid" in msg:
                    SecurityEngine.log_error_to_owner(identity, env, key, f"Hard verification failed: {msg}")
                return False, msg
            lease.update({
                "license_key": key, "status": st, "message": msg, "last_attempt": now.isoformat(),
                "expiry_date": result.get("expiry_date") or lease.get("expiry_date", ""),
                "is_free": bool(result.get("is_free", lease.get("is_free", False))),
                "renew_url": result.get("renew_url") or lease.get("renew_url", ""),
                "plan_type": result.get("plan_type") or lease.get("plan_type", ""),
                "warning": "",
            })
            lease.setdefault("last_verified", "")
            SecurityEngine._write_lease(identity, lease)
            if not silent or "Mismatch" in msg or "Invalid" in msg:
                SecurityEngine.log_error_to_owner(identity, env, key, f"Hard verification failed: {msg}")
            return False, msg

        # Owner server unreachable
        if new_key:
            return False, "Unable to reach the license server. Please check your internet connection."
        if lease:
            lease["last_attempt"] = now.isoformat()
            SecurityEngine._write_lease(identity, lease)
        if expired_locally:
            return False, f"License expired on {exp.strftime('%Y-%m-%d')}. Please renew."
        if status != 'active':
            return False, lease.get('message') or "License is not active."
        if (now - last_verified) < timedelta(days=OFFLINE_GRACE_DAYS):
            return True, "Lease valid (owner server unreachable - offline grace)"
        return False, f"Could not reach the license server for {OFFLINE_GRACE_DAYS} days. Please connect to the internet."

    @staticmethod
    def log_error_to_owner(domain, env, key, reason):
        """
        Silently reports errors/tampering to the owner CMS without accidentally banning the user.
        """
        payload = SecurityEngine.build_owner_payload(key)
        payload["reason"] = f"[{env.upper()}] {reason}"
        SecurityEngine.call_owner_server("/api/license/log_error", payload)