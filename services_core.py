import os
import io
import zipfile
import re
import json
import random
import requests
import string
import logging
import smtplib
import threading
import time
import uuid
import shutil
import base64
import ftplib
import hmac
import hashlib
import socket
import ipaddress
from urllib.parse import parse_qs, urlencode, quote, unquote, urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from sqlalchemy import desc
from flask_login import current_user
from flask import url_for, request

# Import app context and models from database_setup
from database_setup import (
    app, db, SystemSettings, GenerationLog, User, ALLOWED_EXTENSIONS,
    PaymentGateway, Transaction, UserClipboard
)

# =======================================================
# SECURITY HELPER: SSRF PREVENTION
# =======================================================
def is_safe_url(url):
    """
    Validates that a given URL points to a public, external IP address.
    Prevents Server-Side Request Forgery (SSRF) against internal networks and cloud metadata.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ['http', 'https']:
            return False
            
        hostname = parsed.hostname
        if not hostname:
            return False
            
        # Resolve the hostname to an IP address
        ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(ip)
        
        # Strictly block private, loopback, link-local, and multicast IP ranges
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast:
            return False
            
        return True
    except Exception as e:
        print(f"⚠️ [Security] URL Validation Error: {e}")
        return False

# =======================================================
# GLOBAL HELPER FOR USER ISOLATION
# =======================================================
def get_current_uid():
    """Safely fetch current user ID for file prefixing."""
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            return str(current_user.id)
    except:
        pass
    return "anon"

# =======================================================
# 1. FILE HELPERS & CONSTANTS
# =======================================================

# --- DEFAULT HTACCESS PAYLOAD FIX (Added -MultiViews for clean URL fix) ---
DEFAULT_HTACCESS = """# BEGIN Custom HTACCESS
Options -Indexes +SymLinksIfOwnerMatch -MultiViews
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteBase /
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]

# Remove trailing slash
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule ^(.*)/$ /$1 [L,R=301]

# Extensionless mapping for .html
RewriteCond %{REQUEST_FILENAME} !-d
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME}.html -f
RewriteRule ^(.*)$ $1.html [L]

# Extensionless mapping for .php
RewriteCond %{REQUEST_FILENAME} !-d
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME}.php -f
RewriteRule ^(.*)$ $1.php [L]

# Rewrite rules to block out some common exploits.
RewriteCond %{QUERY_STRING} base64_encode[^(]*\([^)]*\) [OR]
RewriteCond %{QUERY_STRING} (<|%3C)([^s]*s)+cript.*(>|%3E) [NC,OR]
RewriteCond %{QUERY_STRING} GLOBALS(=|\[|\%[0-9A-Z]{0,2}) [OR]
RewriteCond %{QUERY_STRING} _REQUEST(=|\[|\%[0-9A-Z]{0,2})
RewriteRule .* index.php [F]

# Block direct access to sensitive files.
RewriteCond %{REQUEST_URI} /?.+\.(_|htaccess|htpasswd|bak|conf|inc|ini|lock|log|old|sh|sql|twig|yaml) [NC,OR]
RewriteCond %{REQUEST_URI} /?.*(config.php|di.php|load.php)
RewriteRule ^(.*)$ index.php?_url=/$1&_errcode=404 [L]
</IfModule>

# Always serve the latest deployed pages (prevents stale cached HTML after re-deploy)
<IfModule mod_headers.c>
<FilesMatch "\.(html|htm|php)$">
Header set Cache-Control "no-cache, no-store, must-revalidate"
Header set Pragma "no-cache"
Header set Expires "0"
</FilesMatch>
</IfModule>
<IfModule LiteSpeed>
CacheLookup off
</IfModule>
# END Custom HTACCESS
"""

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_attachment(file):
    if file and allowed_file(file.filename):
        # --- GOAL 1 FIX: Safely unquote filenames containing spaces or encoding ---
        raw_filename = unquote(file.filename)
        filename = secure_filename(raw_filename)
        
        # --- WINDOWS PATH FIX: Truncate long filenames to prevent saving errors ---
        name, ext = os.path.splitext(filename)
        if len(name) > 60: 
            name = name[:60] # Keep base name reasonable
            
        ts = int(time.time())
        uid = get_current_uid()
        filename = f"user_{uid}_gal_{ts}_{name}{ext}"
        
        # Enforce hard 120 character limit
        if len(filename) > 120:
            filename = filename[:120-len(ext)] + ext
        # -------------------------------------------------------------------------
            
        if not os.path.exists(app.config['UPLOAD_FOLDER']):
            os.makedirs(app.config['UPLOAD_FOLDER'])
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return f"/uploads/{filename}"
    return None

def download_image_server_side(url, filename_prefix="local_media_"):
    """
    Downloads an image or video from a remote URL or internal WSGI memory and saves it to the uploads folder.
    Returns the local path string suitable for src/poster attributes.
    """
    if not url or url.startswith('data:'):
        return url
        
    # --- SURGICAL PROXY UNWRAPPER ---
    if 'url=' in url and ('/api/thumbnail' in url or 'thumbnail' in url):
        try:
            raw_encoded = url.split('url=')[1].split('&')[0]
            url = unquote(raw_encoded)
        except: pass
        
    # --- GOAL 1 FIX: Unquote URL paths to safely handle filenames with spaces ---
    clean_url = unquote(url.split('?')[0].split('#')[0])
    
    # If already a standard relative upload URL, return directly
    if clean_url.startswith('/uploads/'):
        return clean_url
    elif clean_url.startswith('uploads/'):
        return f"/{clean_url}"
        
    try:
        if not os.path.exists(app.config['UPLOAD_FOLDER']):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            
        file_content = None
        ext = 'jpg'
        lower_url = clean_url.lower()
        if '.png' in lower_url: ext = 'png'
        elif '.gif' in lower_url: ext = 'gif'
        elif '.webp' in lower_url: ext = 'webp'
        elif '.svg' in lower_url: ext = 'svg'
        elif '.mp4' in lower_url: ext = 'mp4'
        elif '.webm' in lower_url: ext = 'webm'
        elif '.ogg' in lower_url: ext = 'ogg'
        elif '.mov' in lower_url: ext = 'mov'
        
        uid = get_current_uid()
        
        # --- WINDOWS PATH FIX: Ensure prefix isn't too long ---
        if len(filename_prefix) > 30: 
            filename_prefix = filename_prefix[:30]
            
        filename = f"user_{uid}_{filename_prefix}{uuid.uuid4().hex[:10]}.{ext}"
        
        # Enforce hard 120 character limit
        if len(filename) > 120:
            filename = filename[:120-len(ext)] + ext
        # ------------------------------------------------------
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        # --- GOAL 1 FIX: Try Internal WSGI Loopback if URL points to the app itself ---
        if not file_content and ('/uploads/' in url or request.host in url):
            try:
                uri = '/' + url.split('/', 3)[-1] if url.startswith('http') else url
                if not uri.startswith('/'): uri = '/' + uri
                with app.test_client() as cl:
                    resp = cl.get(uri)
                    if resp.status_code == 200 and len(resp.data) > 0:
                        file_content = resp.data
            except Exception:
                pass

        # --- GOAL 1 FIX: Try Standard External Request if WSGI wasn't applicable ---
        if not file_content and url.startswith('http'):
            # SECURITY FIX: Block SSRF attacks and enforce strict domain boundaries
            if not is_safe_url(url):
                print(f"⚠️ [Security] Blocked unsafe URL fetch attempt: {url}")
                return url
                
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            # SECURITY FIX: Enforce SSL verification (removed verify=False)
            response = requests.get(url, headers=headers, timeout=15, stream=True)
            if response.status_code == 200:
                file_content = response.content

        if file_content:
            with open(filepath, 'wb') as f:
                f.write(file_content)
            return f"/uploads/{filename}"
            
        return url
    except Exception:
        return url

# =======================================================
# 1.5 SHARED TRACKING & CLEANING UTILITIES (NEW)
# =======================================================

def purge_ga_and_smtp_from_html(html_str):
    """
    Aggressively strips Google Analytics snippets and SMTP logic.
    Used for creating clean templates and master project backups.
    """
    if not html_str or not isinstance(html_str, str):
        return html_str
    
    # 1. Robust Google Analytics stripping (covers multiple block styles)
    # Strips the async script tag
    html_str = re.sub(r'<script async src=["\']https://www\.googletagmanager\.com/gtag/js\?id=[^"\']+["\']></script>', '', html_str, flags=re.IGNORECASE)
    # Strips the initialization script block
    html_str = re.sub(r'<script>\s*window\.dataLayer.*?gtag\(\'config\'.*?</script>', '', html_str, flags=re.IGNORECASE | re.DOTALL)
    # Strips any standard HTML comment wrappers around GA
    html_str = re.sub(r'', '', html_str, flags=re.IGNORECASE)
    
    return html_str

def apply_google_analytics(html_content, ga_id):
    """
    Safely strips old GA code and injects the new one (if provided) into the <head>.
    Used during download and deployment to ensure the user's latest settings are used.
    """
    # First, purge any existing mismatched code
    html_content = purge_ga_and_smtp_from_html(html_content)
    
    if not ga_id or not str(ga_id).strip():
        return html_content
        
    clean_ga = str(ga_id).strip()
    
    # --- GOAL 3 FIX: PREVENT DOM BREAKAGE ("> ") ---
    # If the user provided a raw ID (e.g. G-XXXXX), wrap it in a script tag.
    # If they pasted the full script tag (as instructed by the UI), use it directly.
    if clean_ga.upper().startswith('G-') and '<script' not in clean_ga.lower():
        ga_snippet = f"""
<script async src="https://www.googletagmanager.com/gtag/js?id={clean_ga}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', '{clean_ga}');
</script>
"""
    else:
        ga_snippet = f"\n{clean_ga}\n"

    # Inject before </head> or at the top
    if '</head>' in html_content:
        return html_content.replace('</head>', ga_snippet + '\n</head>')
    return ga_snippet + '\n' + html_content

# =======================================================
# 2. CONCURRENCY & LOGGING (NEW QUEUE SYSTEM)
# =======================================================

class ConnectionManager:
    active_requests = 0
    waiting_list = []
    lock = threading.Lock()
    condition = threading.Condition(lock)

    @classmethod
    def enter_queue(cls, req_id):
        """Adds a request to the waiting list and returns its initial position."""
        with cls.condition:
            if req_id not in cls.waiting_list:
                cls.waiting_list.append(req_id)
            return cls.waiting_list.index(req_id) + 1

    @classmethod
    def try_acquire(cls, req_id, limit):
        """
        Attempts to acquire a slot. If not available or not first in line,
        it waits briefly and returns the current queue position.
        Returns: (acquired_boolean, current_position)
        """
        with cls.condition:
            # If you are first in line and there is a slot available
            if cls.waiting_list and cls.waiting_list[0] == req_id:
                if cls.active_requests < limit:
                    cls.waiting_list.pop(0) # Remove from queue
                    cls.active_requests += 1 # Take the slot
                    return True, 0
            
            # Wait 1.5 seconds. This pause acts as the "tick" for the frontend update loop.
            cls.condition.wait(timeout=1.5)
            
            # Return current position in line
            if req_id in cls.waiting_list:
                return False, cls.waiting_list.index(req_id) + 1
            return False, -1

    @classmethod
    def release_slot(cls):
        """Frees an active slot and wakes up the queue."""
        with cls.condition:
            if cls.active_requests > 0:
                cls.active_requests -= 1
            cls.condition.notify_all()
            
    @classmethod
    def leave_queue(cls, req_id):
        """Removes a user from the queue if they cancel or disconnect."""
        with cls.condition:
            if req_id in cls.waiting_list:
                cls.waiting_list.remove(req_id)
            cls.condition.notify_all()

def save_user_log(user_id, log_text):
    with app.app_context():
        try:
            new_log = GenerationLog(user_id=user_id, content=log_text)
            db.session.add(new_log)
            db.session.commit()
        except Exception:
            db.session.rollback()
            try:
                clean_text = log_text.encode('ascii', 'ignore').decode('ascii')
                new_log = GenerationLog(user_id=user_id, content=clean_text)
                db.session.add(new_log)
                db.session.commit()
            except Exception:
                pass
        
        try:
            logs = GenerationLog.query.filter_by(user_id=user_id).order_by(desc(GenerationLog.timestamp)).all()
            if len(logs) > 100:
                for old_log in logs[100:]:
                    db.session.delete(old_log)
                db.session.commit()
        except Exception:
            pass

# =======================================================
# 3. PHP GENERATOR SERVICE
# =======================================================

class PHPGeneratorService:
    @staticmethod
    def get_contact_form_script(owner_email, smtp_host="", smtp_port="587", smtp_user="", smtp_pass=""):
        """Returns the content for send_mail.php with robust SMTP socket and mail() fallbacks."""
        
        # Safely escape strings for PHP injection
        owner_email = str(owner_email).replace('"', '\\"')
        smtp_host = str(smtp_host).replace('"', '\\"')
        smtp_port = str(smtp_port) if str(smtp_port).isdigit() else "587"
        smtp_user = str(smtp_user).replace('"', '\\"')
        smtp_pass = str(smtp_pass).replace('"', '\\"')
        
        php_template = """<?php
header('Content-Type: application/json');
error_reporting(0); // Prevent PHP warnings from breaking JSON response

// ALWAYS return HTTP 200 to prevent shared hosts from intercepting 4xx/5xx and returning HTML error pages
http_response_code(200);

$to_email = "__OWNER_EMAIL__";
$smtp_host = "__SMTP_HOST__";
$smtp_port = __SMTP_PORT__;
$smtp_user = "__SMTP_USER__";
$smtp_pass = "__SMTP_PASS__";
$subject_prefix = "[Website Contact] ";

if ($_SERVER["REQUEST_METHOD"] !== "POST") {
    echo json_encode(["success" => false, "message" => "Method not allowed."]);
    exit;
}

// FIX: Fully Dynamic AI-Proof Input Parsing. 
// Captures everything submitted, bypassing strict variable names.
$post_data = $_POST;
if (empty($post_data)) {
    $raw_input = file_get_contents('php://input');
    $json = json_decode($raw_input, true);
    if (is_array($json)) {
        $post_data = $json;
    } else {
        parse_str($raw_input, $post_data);
    }
}

$name = '';
$email = '';
$all_fields = "";

// Dynamically extract whatever fields the AI generated into the form
foreach ($post_data as $key => $value) {
    if (is_array($value)) { $value = implode(', ', $value); }
    $k = strtolower((string)$key);
    $val = strip_tags(trim((string)$value));
    if (empty($val)) continue;

    if (strpos($k, 'email') !== false && empty($email)) {
        $email = filter_var($val, FILTER_SANITIZE_EMAIL);
    } elseif ((strpos($k, 'name') !== false || strpos($k, 'first') !== false) && empty($name)) {
        $name = $val;
    } else {
        $clean_key = ucfirst(str_replace(['_', '-'], ' ', $key));
        $all_fields .= $clean_key . ": " . $val . "\\r\\n";
    }
}

if (empty($name)) $name = "Website Visitor";

if (empty($email)) {
    // If no explicit email field found, search the entire payload for one
    preg_match("/[a-z0-9_\-\+]+@[a-z0-9\-]+\.([a-z]{2,3})(?:\.[a-z]{2})?/i", $all_fields, $matches);
    if(isset($matches[0])) {
        $email = $matches[0];
    } else {
        $email = "no-reply@website.com";
    }
}

if (empty($all_fields) && $name === "Website Visitor") {
    echo json_encode(["success" => false, "message" => "Invalid input. Please complete all fields."]);
    exit;
}

$subject = $subject_prefix . "Message from " . $name;
$body = "Name: $name\\r\\nEmail: $email\\r\\n\\r\\nMessage Details:\\r\\n$all_fields\\r\\n";

// ANTI-SPOOFING FIX: 
// Always use a domain-matched email for the From header in the native mail fallback.
$host_domain = isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : 'website.com';
$host_domain = preg_replace('/^www\./', '', $host_domain);
$native_headers = "From: Website Form <no-reply@" . $host_domain . ">\\r\\n";
$native_headers .= "Reply-To: $email\\r\\n";
$native_headers .= "MIME-Version: 1.0\\r\\n";
$native_headers .= "Content-Type: text/plain; charset=UTF-8\\r\\n";

if (!empty($smtp_host) && !empty($smtp_user) && !empty($smtp_pass)) {
    try {
        $newline = "\\r\\n";
        $secure = ($smtp_port == 465) ? 'ssl://' : '';
        
        $context = stream_context_create([
            'ssl' => [
                'verify_peer' => false,
                'verify_peer_name' => false,
                'allow_self_signed' => true
            ]
        ]);

        // Reduced timeout to 5 seconds
        $socket = @stream_socket_client($secure . $smtp_host . ':' . $smtp_port, $errno, $errstr, 5, STREAM_CLIENT_CONNECT, $context);
        
        if (!$socket) {
            throw new Exception("SMTP Connection failed");
        }

        function server_parse($socket, $expected_response) {
            $server_response = '';
            while (substr($server_response, 3, 1) != ' ') {
                if (!($server_response = fgets($socket, 256))) return false;
            }
            return (substr($server_response, 0, 3) == $expected_response);
        }

        server_parse($socket, "220");
        fwrite($socket, "EHLO " . $host_domain . $newline);
        server_parse($socket, "250");

        if ($smtp_port == 587) {
            fwrite($socket, "STARTTLS" . $newline);
            server_parse($socket, "220");
            stream_socket_enable_crypto($socket, true, STREAM_CRYPTO_METHOD_TLS_CLIENT);
            fwrite($socket, "EHLO " . $host_domain . $newline);
            server_parse($socket, "250");
        }

        fwrite($socket, "AUTH LOGIN" . $newline);
        server_parse($socket, "334");
        fwrite($socket, base64_encode($smtp_user) . $newline);
        server_parse($socket, "334");
        fwrite($socket, base64_encode($smtp_pass) . $newline);
        if (!server_parse($socket, "235")) {
            throw new Exception("SMTP Auth failed");
        }

        fwrite($socket, "MAIL FROM: <$smtp_user>" . $newline);
        server_parse($socket, "250");
        fwrite($socket, "RCPT TO: <$to_email>" . $newline);
        server_parse($socket, "250");
        
        fwrite($socket, "DATA" . $newline);
        server_parse($socket, "354");

        $smtp_headers = "Date: " . date("r") . $newline;
        $smtp_headers .= "To: $to_email" . $newline;
        $smtp_headers .= "From: Website Form <$smtp_user>" . $newline;
        $smtp_headers .= "Reply-To: $email" . $newline;
        $smtp_headers .= "Subject: $subject" . $newline;
        $smtp_headers .= "MIME-Version: 1.0" . $newline;
        $smtp_headers .= "Content-Type: text/plain; charset=UTF-8" . $newline . $newline;

        fwrite($socket, $smtp_headers . $body . $newline . "." . $newline);
        server_parse($socket, "250");
        
        fwrite($socket, "QUIT" . $newline);
        fclose($socket);

        echo json_encode(["success" => true, "message" => "Message sent successfully!"]);
    } catch (Exception $e) {
        // Silent Fallback to PHP native mail()
        if (mail($to_email, $subject, $body, $native_headers)) {
            echo json_encode(["success" => true, "message" => "Message sent successfully!"]);
        } else {
            echo json_encode(["success" => false, "message" => "Connection to mail server failed, and local mail is disabled."]);
        }
    }
} else {
    // Fallback to PHP native mail()
    if (mail($to_email, $subject, $body, $native_headers)) {
        echo json_encode(["success" => true, "message" => "Message sent successfully!"]);
    } else {
        echo json_encode(["success" => false, "message" => "Failed to send email. Server mail() disabled or misconfigured."]);
    }
}
?>"""
        
        php_template = php_template.replace('__OWNER_EMAIL__', owner_email)
        php_template = php_template.replace('__SMTP_HOST__', smtp_host)
        php_template = php_template.replace('__SMTP_PORT__', smtp_port)
        php_template = php_template.replace('__SMTP_USER__', smtp_user)
        php_template = php_template.replace('__SMTP_PASS__', smtp_pass)
        
        return php_template

    @staticmethod
    def get_form_html():
        return """
        <div class="contact-form-container" style="padding:20px; background:#f9f9f9; border-radius:8px; max-width:600px; margin:20px auto;">
            <h3>Contact Us</h3>
            <form id="php-contact-form" action="send_mail.php" method="POST">
                <div style="margin-bottom:15px;">
                    <label style="display:block; margin-bottom:5px; font-weight:bold;">Name</label>
                    <input type="text" name="name" required style="width:100%; padding:10px; border:1px solid #ddd; border-radius:4px;">
                </div>
                <div style="margin-bottom:15px;">
                    <label style="display:block; margin-bottom:5px; font-weight:bold;">Email</label>
                    <input type="email" name="email" required style="width:100%; padding:10px; border:1px solid #ddd; border-radius:4px;">
                </div>
                <div style="margin-bottom:15px;">
                    <label style="display:block; margin-bottom:5px; font-weight:bold;">Message</label>
                    <textarea name="message" rows="5" required style="width:100%; padding:10px; border:1px solid #ddd; border-radius:4px;"></textarea>
                </div>
                <button type="submit" style="background:#007bff; color:white; padding:12px 25px; border:none; border-radius:4px; cursor:pointer; font-size:16px;">Send Message</button>
                <div id="form-response" style="margin-top:10px; font-weight:bold; min-height: 20px;"></div>
            </form>
            <script>
            document.getElementById('php-contact-form').addEventListener('submit', function(e) {
                e.preventDefault();
                const respDiv = document.getElementById('form-response');
                respDiv.textContent = 'Sending...';
                respDiv.style.color = '#333';

                const searchParams = new URLSearchParams();
                const inputs = this.querySelectorAll('input, textarea, select');
                inputs.forEach((input, index) => {
                    if (input.type === 'submit' || input.type === 'button') return;
                    let key = input.name || input.id || ('field_' + index);
                    let val = input.value;
                    if(val) searchParams.append(key, val);
                });
                
                fetch('send_mail.php', { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: searchParams.toString() 
                })
                .then(async r => {
                    const text = await r.text();
                    let data;
                    try {
                        const jsonStart = text.indexOf('{');
                        const jsonEnd = text.lastIndexOf('}');
                        if (jsonStart !== -1 && jsonEnd !== -1) {
                            data = JSON.parse(text.substring(jsonStart, jsonEnd + 1));
                        } else {
                            data = JSON.parse(text);
                        }
                    } catch(err) {
                        console.error('PHP Output:', text);
                        throw new Error('Server returned invalid response.');
                    }
                    respDiv.textContent = data.message || 'Error occurred';
                    respDiv.style.color = data.success ? 'green' : 'red';
                    if(data.success) this.reset();
                })
                .catch(err => {
                    respDiv.textContent = 'Server error. Email configuration failed.';
                    respDiv.style.color = 'red';
                });
            });
            </script>
        </div>
        """

# =======================================================
# 4. EMAIL SERVICE
# =======================================================

class EmailService:
    @staticmethod
    def send_email(to_email, subject, body, silent=False):
        with app.app_context():
            settings = SystemSettings.query.first()
            if not settings:
                return False
                
            method = settings.email_method or 'smtp'
            
            if method == 'smtp':
                if settings.smtp_host and settings.smtp_user and settings.smtp_pass:
                    try:
                        msg = MIMEMultipart()
                        msg['From'] = settings.smtp_from_email or settings.smtp_user
                        msg['To'] = to_email
                        msg['Subject'] = subject
                        msg.attach(MIMEText(body, 'html')) 

                        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
                        server.starttls()
                        server.login(settings.smtp_user, settings.smtp_pass)
                        text_content = msg.as_string()
                        server.sendmail(settings.smtp_user, to_email, text_content)
                        server.quit()
                        return True
                    except Exception:
                        return False
            
            elif method == 'api':
                if settings.email_api_url:
                    try:
                        payload = {
                            "to": to_email,
                            "subject": subject,
                            "text": body,
                            "from": settings.smtp_from_email or "noreply@ai-builder.com"
                        }
                        headers = { "Content-Type": "application/json" }
                        if settings.email_api_key:
                            headers["Authorization"] = f"Bearer {settings.email_api_key}"
                            headers["api-key"] = settings.email_api_key 

                        # SECURITY FIX: Enforce SSL verification (removed verify=False)
                        resp = requests.post(settings.email_api_url, json=payload, headers=headers, timeout=10)
                        
                        if resp.status_code in [200, 201, 202]:
                            return True
                        else:
                            return False
                    except Exception:
                        return False

            return True

    @staticmethod
    def send_verification_email(user, token):
        subject = "Verify your Account - Verification Code"
        html = f"""
        <h3>Welcome to AI Website Builder!</h3>
        <p>Your verification code is: <strong style="font-size: 24px; color: #007bff;">{token}</strong></p>
        <p>Please enter this code in the application to activate your account.</p>
        <p>If you did not request this, please ignore this email.</p>
        """
        threading.Thread(target=EmailService.send_email, args=(user.email, subject, html)).start()

    @staticmethod
    def send_password_reset(user, token):
        subject = "Password Reset Request"
        html = f"""
        <h3>Password Reset</h3>
        <p>Your temporary password / reset code is: <strong style="font-size: 24px; color: #dc3545;">{token}</strong></p>
        <p>Please log in using this temporary password. You will then be prompted to create a new, secure password.</p>
        <p>If you did not request a password reset, please contact support immediately.</p>
        """
        threading.Thread(target=EmailService.send_email, args=(user.email, subject, html)).start()

# =======================================================
# 5. FTP ENCRYPTION SERVICE & DEPLOYMENT/IMPORT LOGIC
# =======================================================

class FTPEncryptionService:
    @staticmethod
    def _get_key():
        """Generates a secure 32-byte encryption key derived from the Flask SECRET_KEY."""
        secret = app.config.get('SECRET_KEY', 'default_secret')
        return hashlib.sha256(secret.encode()).digest()

    @staticmethod
    def encrypt_credentials(creds_dict):
        """Encrypts the FTP credentials dictionary into a secure AES base64 string."""
        try:
            from Crypto.Cipher import AES
            data = json.dumps(creds_dict)
            key = FTPEncryptionService._get_key()
            iv = os.urandom(16)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            
            pad_len = 16 - len(data) % 16
            data += chr(pad_len) * pad_len
            
            encrypted = cipher.encrypt(data.encode('utf-8'))
            return base64.b64encode(iv + encrypted).decode('utf-8')
        except Exception as e:
            print("Encryption error:", e)
            return "{}"

    @staticmethod
    def decrypt_credentials(encrypted_str):
        """Decrypts the AES base64 string back into the FTP credentials dictionary."""
        try:
            if not encrypted_str or encrypted_str == '{}': 
                return {}
            from Crypto.Cipher import AES
            enc_bytes = base64.b64decode(encrypted_str)
            iv = enc_bytes[:16]
            encrypted = enc_bytes[16:]
            key = FTPEncryptionService._get_key()
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(encrypted).decode('utf-8')
            
            pad_len = ord(decrypted[-1])
            if 1 <= pad_len <= 16:
                decrypted = decrypted[:-pad_len]
            
            return json.loads(decrypted)
        except Exception as e:
            print("Decryption error:", e)
            return {}


class DeploymentService:
    @staticmethod
    def deploy_via_ftp(host, user, password, local_project_path, target_dir='/public_html'):
        log = []
        
        def make_remote_dirs(ftp_conn, dir_path):
            parts = [p for p in dir_path.split('/') if p]
            current = ""
            for part in parts:
                current = f"{current}/{part}" if current else part
                try:
                    ftp_conn.mkd(current)
                except Exception:
                    pass 

        htaccess_path = os.path.join(local_project_path, '.htaccess')
        if not os.path.exists(htaccess_path):
            with open(htaccess_path, 'w', encoding='utf-8') as f:
                f.write(DEFAULT_HTACCESS)

        try:
            ftp = ftplib.FTP(host, timeout=60)
            ftp.login(user, password)
            ftp.set_pasv(True)
            log.append(f"✅ Connected to {host}")

            try:
                ftp.cwd(target_dir)
            except Exception:
                try:
                    make_remote_dirs(ftp, target_dir)
                    ftp.cwd(target_dir)
                except Exception:
                    log.append(f"⚠️ Could not change to {target_dir}, uploading to root.")

            files_count = 0
            for root, dirs, files in os.walk(local_project_path):
                for filename in files:
                    local_file = os.path.join(root, filename)
                    
                    relative_path = os.path.relpath(local_file, local_project_path)
                    remote_file = relative_path.replace("\\", "/") 

                    if "/" in remote_file:
                        sub_dir = remote_file.rsplit('/', 1)[0]
                        make_remote_dirs(ftp, sub_dir)

                    # FIX: Force-overwrite existing files on the hosting path.
                    # Some FTP servers refuse STOR over an existing file (553/550) or keep the old
                    # copy; delete first, upload, and verify the size on the server.
                    local_size = os.path.getsize(local_file)
                    uploaded = False
                    last_err = None
                    for attempt in range(2):
                        try:
                            if attempt == 1:
                                try: ftp.delete(remote_file)
                                except Exception: pass
                            with open(local_file, 'rb') as f:
                                ftp.storbinary(f'STOR {remote_file}', f)
                            try:
                                ftp.voidcmd('TYPE I')
                                remote_size = ftp.size(remote_file)
                                if remote_size is not None and int(remote_size) != local_size:
                                    raise Exception(f"size mismatch (server {remote_size} vs local {local_size})")
                            except ftplib.error_perm:
                                pass  # SIZE not supported by server; trust STOR
                            uploaded = True
                            break
                        except Exception as e:
                            last_err = e
                    if uploaded:
                        files_count += 1
                    else:
                        log.append(f"❌ Failed to overwrite {remote_file}: {last_err}")
            
            ftp.quit()
            log.append(f"✅ Successfully uploaded {files_count} files.")
            return True, log

        except Exception as e:
            return False, [f"❌ Error: {str(e)}"]

    @staticmethod
    def import_via_ftp(host, user, password, target_dir='/public_html'):
        """Connects to remote server, walks dir recursively, and retrieves files directly into memory."""
        log = []
        files_dict = {}
        
        try:
            ftp = ftplib.FTP(host)
            ftp.login(user, password)
            log.append(f"✅ Connected to {host}")
            
            try:
                ftp.cwd(target_dir)
                log.append(f"📂 Changed directory to {target_dir}")
            except Exception:
                log.append(f"⚠️ Could not change to {target_dir}, importing from root.")
                
            def download_dir(current_path, current_dict_prefix):
                items = []
                ftp.retrlines('LIST', items.append)
                for item in items:
                    parts = item.split(None, 8)
                    if len(parts) < 9: continue
                    name = parts[-1]
                    if name in ['.', '..']: continue
                    
                    is_dir = item.startswith('d')
                    if not is_dir and '<DIR>' in item: is_dir = True
                    
                    remote_file_path = f"{current_path}/{name}" if current_path else name
                    dict_key = f"{current_dict_prefix}{name}" if current_dict_prefix else name
                    
                    if is_dir:
                        # Prevent endless recursion on common unneeded directories
                        if name.lower() not in ['cgi-bin', '.well-known']:
                            try:
                                ftp.cwd(name)
                                download_dir(remote_file_path, f"{dict_key}/")
                                ftp.cwd('..')
                            except Exception: pass
                    else:
                        # Extract web files directly into RAM for the builder
                        if name.lower().endswith(('.html', '.php', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')):
                            buffer = io.BytesIO()
                            try:
                                ftp.retrbinary(f"RETR {name}", buffer.write)
                                buffer.seek(0)
                                if name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')):
                                    encoded = base64.b64encode(buffer.read()).decode('utf-8')
                                    mime_type = 'application/octet-stream'
                                    if name.lower().endswith('.png'): mime_type = 'image/png'
                                    elif name.lower().endswith(('.jpg', '.jpeg')): mime_type = 'image/jpeg'
                                    elif name.lower().endswith('.gif'): mime_type = 'image/gif'
                                    elif name.lower().endswith('.svg'): mime_type = 'image/svg+xml'
                                    elif name.lower().endswith('.webp'): mime_type = 'image/webp'
                                    files_dict[dict_key] = f"data:{mime_type};base64,{encoded}"
                                else:
                                    files_dict[dict_key] = buffer.read().decode('utf-8', errors='ignore')
                            except Exception as e:
                                log.append(f"❌ Failed to download {dict_key}: {e}")

            download_dir("", "")
            ftp.quit()
            log.append(f"✅ Successfully downloaded {len(files_dict)} files from FTP.")
            return True, files_dict, log

        except Exception as e:
            return False, {}, [f"❌ FTP Connection Error: {str(e)}"]

# =======================================================
# 8. GLOBAL UTILS (WITH BULLETPROOF ERROR HANDLING)
# =======================================================

USER_SESSIONS = {}
USER_SESSIONS_LOCK = threading.Lock() 

def get_user_session_data(user_id=None):
    try:
        uid = user_id or (current_user.id if current_user and current_user.is_authenticated else None)
        if uid is None: return None
        with USER_SESSIONS_LOCK:
            if uid not in USER_SESSIONS: 
                USER_SESSIONS[uid] = {'history_stack': [], 'redo_stack': [], 'active_project': None}
        return USER_SESSIONS[uid]
    except Exception:
        return None

def get_current_files(user_id=None):
    try:
        data = get_user_session_data(user_id)
        if not data: return {}
        files = data.get('history_stack', [{}])[-1] if data.get('history_stack') else {}
        if files and '.htaccess' not in files:
            files['.htaccess'] = DEFAULT_HTACCESS
        return files
    except Exception:
        return {}

def enrich_html_content(content):
    if not content or not isinstance(content, str): return content
    
    # 1. Replace <img> tags
    def replace_img(match):
        try:
            attrs = match.group(1)
            if 'src="/uploads/' in attrs or 'src="data:' in attrs or 'src="http' in attrs and 'placehold.co' not in attrs:
                return match.group(0)
            
            alt_match = re.search(r'alt=["\'](.*?)["\']', attrs, re.IGNORECASE)
            alt_text = alt_match.group(1) if alt_match else "generic image"
            
            if len(alt_text) < 3: return match.group(0)

            # Fixed safe indirect import to avoid 500 crashes
            from services_image_providers import ImageProviderService 
            real_url = ImageProviderService.get_image_url(alt_text)
            new_attrs = re.sub(r'src=["\'][^"\']*["\']', '', attrs) 
            return f'<img src="{real_url}" {new_attrs.strip()}>'
        except Exception:
            return match.group(0)

    try:
        content = re.sub(r'<img\s+([^>]*?)>', replace_img, content, flags=re.IGNORECASE)
    except Exception:
        pass

    # --- NEW: Catch CSS background-image placeholders for the Wide Hero Layout ---
    def replace_bg(match):
        try:
            quote_char = match.group(1)
            url_val = match.group(2)
            
            if 'placeholder' in url_val.lower() or 'placehold.co' in url_val.lower():
                title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
                search_query = title_match.group(1) if title_match else "modern professional background"
                
                from services_image_providers import ImageProviderService 
                real_url = ImageProviderService.get_image_url(search_query + " abstract background", width=1920, height=1080)
                if real_url:
                    return f'url({quote_char}{real_url}{quote_char})'
            return match.group(0)
        except Exception:
            return match.group(0)

    try:
        content = re.sub(r'url\(([\'"]?)(.*?)\1\)', replace_bg, content, flags=re.IGNORECASE)
    except Exception:
        pass

    return content

def ensure_images_are_placeholders(content):
    if not content or not isinstance(content, str): return content
    
    def replace_img(match):
        try:
            attributes = match.group(1)
            src_match = re.search(r'src=["\']([^"\']+)["\']', attributes, re.IGNORECASE)
            
            if src_match:
                src = src_match.group(1)
                if 'pollinations' in src: pass
                elif '/uploads/' in src or 'data:image' in src or src.startswith('http') and 'placehold.co' not in src: 
                    return match.group(0)

            alt_match = re.search(r'alt=["\'](.*?)["\']', attributes, re.IGNORECASE)
            alt_text = alt_match.group(1) if alt_match else "Image"
            
            search_query = quote(re.sub(r'[^a-zA-Z0-9 ]+', '', alt_text).strip()[:30])
            image_url = f"https://placehold.co/600x400?text={search_query}"
            
            new_attributes = re.sub(r'src\s*=\s*["\'].*?["\']', '', attributes, count=1, flags=re.IGNORECASE)
            return f'<img src="{image_url}" {new_attributes.strip()}>'
        except Exception:
            return match.group(0)
        
    try:
        return re.sub(r'<img\s+([^>]*?)>', replace_img, content, flags=re.IGNORECASE)
    except Exception:
        return content

def ensure_links_are_proxied(content, all_files):
    if not content or not isinstance(content, str): return content
    try:
        for filename in [f for f in all_files if f.endswith('.html')]:
            content = re.sub(rf'(href=["\'])(?:/|){re.escape(filename)}(["\'])', rf'\1/preview/{filename}\2', content, flags=re.IGNORECASE)
        return content
    except Exception:
        return content

def remove_proxy_links(content):
    if not content or not isinstance(content, str): return content
    try:
        return re.sub(r'(href=["\'])/preview/([^"\']*?\.html)(["\'])', r'\1\2\3', content, flags=re.IGNORECASE)
    except Exception:
        return content

def _increment_website_count_if_applicable(user_id):
    """
    CRITICAL LIMIT CHECK:
    Reduces the user's limit (increments the generated count) ONLY IF the user is using the Admin's AI key.
    If the user has provided their OWN valid API key for the selected AI Provider, it bypasses the limit.
    """
    if not user_id: return
    try:
        from database_setup import app, db, User, SystemSettings
        import json
        with app.app_context():
            user = User.query.get(user_id)
            if user:
                using_own_key = False
                
                allow_own_keys = getattr(user, 'role', '') == 'admin' or getattr(user, 'allow_own_api_keys', False) or (user.user_type and getattr(user.user_type, 'allow_own_api_keys', False))
                
                if allow_own_keys:
                    try:
                        user_settings = {}
                        if hasattr(user, 'user_settings_json') and user.user_settings_json:
                            user_settings = json.loads(user.user_settings_json)
                            
                        pref_provider = user_settings.get('ai_provider', 'default').strip().lower()
                        
                        if pref_provider != 'default':
                            key_to_check = user_settings.get(f'{pref_provider}_api_key')
                            if key_to_check and len(str(key_to_check).strip()) > 5:
                                using_own_key = True
                            elif hasattr(user, 'api_keys_json') and user.api_keys_json:
                                api_keys = json.loads(user.api_keys_json)
                                if api_keys.get(f'{pref_provider}_api_key') and len(str(api_keys.get(f'{pref_provider}_api_key')).strip()) > 5:
                                    using_own_key = True
                            elif hasattr(user, f'{pref_provider}_api_key'):
                                key_attr = getattr(user, f'{pref_provider}_api_key')
                                if key_attr and len(str(key_attr).strip()) > 5:
                                    using_own_key = True
                    except Exception:
                        pass
                    
                if not using_own_key:
                    user.websites_generated = getattr(user, 'websites_generated', 0) + 1
                    db.session.commit()
    except Exception:
        pass

def save_new_state(new_files, user_id=None):
    try:
        data = get_user_session_data(user_id)
        if not data or not new_files: return
        
        data['history_stack'] = [new_files]
        data['redo_stack'] = [] 
        
    except Exception:
        pass

def sync_user_project_state(new_files, partial=True, user_id=None):
    try:
        data = get_user_session_data(user_id)
        if not data: return
        
        current_state = {}
        if data['history_stack']:
            current_state = data['history_stack'][-1].copy()
        
        if new_files:
            current_state.update(new_files)
        
        data['history_stack'] = [current_state]
        data['redo_stack'] = []
        
    except Exception:
        pass

def extract_json_from_response(text):
    if not text: return "{}"
    try:
        match = re.search(r'```json\s*(\{.*\})\s*```', text, re.DOTALL)
        if match: return match.group(1).strip()
        if text.strip().startswith('{'): return text.strip()
        return text.strip()
    except Exception:
        return "{}"

def get_user_project_dir():
    from database_setup import PROJECTS_DIR
    if not current_user.is_authenticated: return PROJECTS_DIR 
    user_dir = os.path.join(PROJECTS_DIR, str(current_user.id))
    if not os.path.exists(user_dir): os.makedirs(user_dir)
    return user_dir

def localize_html_images(html_content):
    """Replaces external absolute image URLs in HTML with locally downloaded relative URLs."""
    if not html_content or not isinstance(html_content, str): return html_content
    
    def replace_match(match):
        try:
            attr = match.group(1) # src, poster
            quote_char = match.group(2)
            url = match.group(3)
            if url.startswith('/uploads/') or url.startswith('data:'): return match.group(0)
            
            # --- PROXY UNWRAPPER ---
            if 'url=' in url and ('/api/thumbnail' in url or 'thumbnail' in url):
                try: url = unquote(url.split('url=')[1].split('&')[0])
                except: pass
                
            local_path = download_image_server_side(url, filename_prefix="local_media_")
            return f'{attr}={quote_char}{local_path}{quote_char}'
        except Exception:
            return match.group(0)

    def replace_css_url(match):
        try:
            quote_char = match.group(1)
            url_val = match.group(2).strip()
            if url_val.startswith('/uploads/') or url_val.startswith('data:'): return match.group(0)
            
            # --- PROXY UNWRAPPER ---
            if 'url=' in url_val and ('/api/thumbnail' in url_val or 'thumbnail' in url_val):
                try: url_val = unquote(url_val.split('url=')[1].split('&')[0])
                except: pass
                
            local_path = download_image_server_side(url_val, filename_prefix="local_media_")
            return f'url({quote_char}{local_path}{quote_char})'
        except Exception:
            return match.group(0)

    try:
        # --- GOAL 1 FIX: Improved Regex accommodating optional whitespace around equals sign ---
        html_content = re.sub(r'(src|poster)\s*=\s*([\'"])(http[s]?://[^\'"]+)\2', replace_match, html_content, flags=re.IGNORECASE)
        html_content = re.sub(r'url\(([\'"]?)(http[s]?://[^\'"\)]+)\1\)', replace_css_url, html_content, flags=re.IGNORECASE)
        return html_content
    except Exception:
        return html_content