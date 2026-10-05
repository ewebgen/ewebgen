import json
import time
import requests
from flask import request
from flask_login import current_user

from database_setup import db, PaymentGateway, Transaction

# =======================================================
# 9. PAYMENT SERVICE
# =======================================================

class PaymentService:
    
    @staticmethod
    def get_safe_url_root():
        forwarded_host = request.headers.get('X-Forwarded-Host')
        forwarded_proto = request.headers.get('X-Forwarded-Proto', 'https')
        if forwarded_host:
            return f"{forwarded_proto}://{forwarded_host}/"
            
        url_root = request.url_root
        if "localhost" not in url_root and "127.0.0.1" not in url_root:
            url_root = url_root.replace("http://", "https://")
        return url_root

    @staticmethod
    def create_payment_link(txn, gateway_code):
        gateway_entry = PaymentGateway.query.filter_by(code=gateway_code).first()
        if not gateway_entry or not gateway_entry.is_active:
            return False, "Gateway not active or found."

        try:
            config = json.loads(gateway_entry.config_json)
        except:
            return False, "Invalid Gateway Configuration JSON."
            
        mode = gateway_entry.mode  

        if gateway_code == 'paypal':
            return PaymentService._create_paypal_rest_link(txn, config, mode)
        elif gateway_code == 'cashfree':
            return PaymentService._create_cashfree_link(txn, config, mode)
        elif gateway_code == 'ccavenue':
            return PaymentService._create_ccavenue_link(txn, config, mode)

        return False, f"Gateway '{gateway_code}' logic not implemented."

    # ---------------------------------------------------
    # PAYPAL LOGIC
    # ---------------------------------------------------
    @staticmethod
    def _create_paypal_rest_link(txn, config, mode):
        client_id = str(config.get('client_id', '')).strip()
        secret = str(config.get('client_secret', '')).strip()

        if not client_id or not secret:
            return False, "PayPal credentials missing. Please configure them in the Admin CMS."

        currency_safe = str(txn.currency).strip().upper()[:3] if txn.currency else "USD"
        final_amount = float(txn.amount)
        
        if currency_safe == 'INR':
            currency_safe = 'USD'
            final_amount = round(final_amount / 83.5, 2)
            if final_amount <= 0:
                final_amount = 0.01

        base_url = "https://api-m.sandbox.paypal.com" if mode == 'sandbox' else "https://api-m.paypal.com"

        try:
            auth_response = requests.post(
                f"{base_url}/v1/oauth2/token",
                auth=(client_id, secret),
                data={'grant_type': 'client_credentials'},
                headers={'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
            )
            
            if auth_response.status_code != 200:
                err_detail = auth_response.text
                try:
                    js_err = auth_response.json()
                    err_detail = js_err.get('error_description', js_err.get('error', auth_response.text))
                except:
                    pass
                return False, f"PayPal Auth Failed ({auth_response.status_code}): Ensure your Client ID and Secret match the selected mode ({mode.upper()}). Details: {err_detail}"
                
            access_token = auth_response.json()['access_token']
        except Exception as e:
            return False, f"PayPal Connection Error: {str(e)}"

        try:
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}'
            }
            safe_url_root = PaymentService.get_safe_url_root()
            return_url = f"{safe_url_root}api/payment/callback/paypal?status=success"
            cancel_url = f"{safe_url_root}api/payment/callback/paypal?status=cancel"
            
            item_desc = "Service Subscription"
            if txn.response_json:
                try:
                    meta = json.loads(txn.response_json)
                    item_desc = meta.get('plan_name', meta.get('product_name', item_desc))
                except: pass
            
            item_desc = "".join(c for c in item_desc if c.isprintable()).strip()

            payload = {
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": str(txn.id),
                    "amount": {
                        "currency_code": currency_safe,
                        "value": f"{final_amount:.2f}"
                    },
                    "description": item_desc[:127]
                }],
                "application_context": {
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                    "brand_name": "AI Website Builder",
                    "user_action": "PAY_NOW"
                }
            }

            response = requests.post(f"{base_url}/v2/checkout/orders", headers=headers, json=payload)
            order_data = response.json()

            if response.status_code in [200, 201]:
                for link in order_data.get('links', []):
                    if link['rel'] == 'approve':
                        txn.transaction_id = order_data['id'] 
                        db.session.commit()
                        return True, {"action": "redirect", "url": link['href']}
            
            err_msg = order_data.get('message', 'Order creation failed')
            details = order_data.get('details', [])
            if details and isinstance(details, list):
                issues = [f"{d.get('issue', '')} - {d.get('description', '')}" for d in details]
                if issues:
                    err_msg += " | " + " | ".join(issues)
                    
            return False, f"PayPal Error: {err_msg}"
            
        except Exception as e:
            return False, f"PayPal API Error: {str(e)}"

    # ---------------------------------------------------
    # CASHFREE LOGIC (UPGRADED TO NATIVE LINK ROUTER)
    # ---------------------------------------------------
    @staticmethod
    def _create_cashfree_link(txn, config, mode):
        app_id = config.get('app_id')
        secret_key = config.get('secret_key')
        
        if not app_id or not secret_key:
            return False, "Cashfree credentials missing."
            
        app_id = str(app_id).strip()
        secret_key = str(secret_key).strip()

        env_url = "https://sandbox.cashfree.com/pg" if mode == 'sandbox' else "https://api.cashfree.com/pg"
        # FIX: Utilize the robust Links API instead of Orders API to guarantee a hosted URL return
        url = f"{env_url}/links"
        
        headers = {
            "x-client-id": app_id,
            "x-client-secret": secret_key,
            "x-api-version": "2023-08-01",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        customer_email = current_user.email if current_user.is_authenticated else "guest@example.com"
        
        raw_phone = getattr(current_user, 'phone_number', '9876543210') or '9876543210'
        clean_phone = ''.join(filter(str.isdigit, raw_phone))
        # Cashfree strictly requires valid phone numbers. Fallback to a standard test dummy if empty/zeros.
        if len(clean_phone) < 10 or clean_phone == "0000000000": 
            clean_phone = "9876543210"
        elif len(clean_phone) > 10:
            clean_phone = clean_phone[-10:] 

        customer_name = getattr(current_user, 'full_name', 'Guest User') or 'Guest User'
        if not customer_name.strip(): customer_name = "Guest User"

        link_id = f"ORDER_{txn.id}_{int(time.time())}"
        
        safe_url_root = PaymentService.get_safe_url_root()
        
        # --- CASHFREE CURRENCY FIX ---
        currency_safe = str(txn.currency).strip().upper()[:3] if txn.currency else "INR"
        final_amount = float(txn.amount)

        # Cashfree Domestic Accounts throw "transactions not enabled" if charged in USD.
        # We auto-convert non-INR currencies to INR for Cashfree.
        if currency_safe != 'INR':
            conversion_rates = {'USD': 83.5, 'EUR': 90.0, 'GBP': 105.0, 'CAD': 61.0, 'AUD': 54.0}
            rate = conversion_rates.get(currency_safe, 83.5)
            final_amount = round(final_amount * rate, 2)
            currency_safe = 'INR'

        if final_amount < 1.0:
            final_amount = 1.0

        # Build payload matching the V3 Links schema
        payload = {
            "link_id": link_id,
            "link_amount": final_amount,
            "link_currency": currency_safe,
            "link_purpose": "Subscription",
            "customer_details": {
                "customer_phone": clean_phone,
                "customer_email": customer_email,
                "customer_name": customer_name
            },
            "link_meta": {
                # Cashfree explicitly replaces {link_id} upon successful payment redirection
                "return_url": f"{safe_url_root}api/payment/callback/cashfree?order_id={{link_id}}"
            }
        }
        
        try:
            resp = requests.post(url, headers=headers, json=payload)
            data = resp.json()
            
            # The Links API directly provides a foolproof hosted link URL
            payment_url = data.get('link_url')
            
            if resp.status_code == 200 and payment_url:
                txn.transaction_id = link_id
                db.session.commit()
                return True, {"action": "redirect", "url": payment_url}
            else:
                err_message = data.get('message', resp.text)
                return False, f"Cashfree API Error (Mode: {mode.upper()}): {err_message}"
        except Exception as e:
            return False, f"Cashfree Connection Error: {str(e)}"

    # ---------------------------------------------------
    # CCAVENUE LOGIC
    # ---------------------------------------------------
    @staticmethod
    def _create_ccavenue_link(txn, config, mode):
        merchant_id = str(config.get('merchant_id', '')).strip()
        access_code = str(config.get('access_code', '')).strip()
        working_key = str(config.get('working_key', '')).strip()
        
        if not merchant_id or not access_code or not working_key:
            return False, "CCAvenue credentials missing."
            
        try:
            from Crypto.Cipher import AES
            import hashlib
        except ImportError:
            return False, "Python module 'pycryptodome' is required for CCAvenue integration."

        order_id = f"ORD_{txn.id}_{int(time.time())}"
        amount = f"{float(txn.amount):.2f}"
        currency = str(txn.currency).strip().upper()[:3] if txn.currency else "INR"
        
        safe_url_root = PaymentService.get_safe_url_root()
        redirect_url = f"{safe_url_root}api/payment/callback/ccavenue"
        
        merchant_data = (
            f"merchant_id={merchant_id}&order_id={order_id}&"
            f"currency={currency}&amount={amount}&"
            f"redirect_url={redirect_url}&cancel_url={redirect_url}&"
            f"language=EN"
        )
        
        byte_key = hashlib.md5(working_key.encode('utf-8')).digest()
        
        def pad(data):
            pad_len = 16 - (len(data) % 16)
            return data + (chr(pad_len) * pad_len)
            
        iv = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f'
        
        try:
            cipher = AES.new(byte_key, AES.MODE_CBC, iv)
            padded_data = pad(merchant_data).encode('utf-8')
            enc_data = cipher.encrypt(padded_data).hex()
            
            txn.transaction_id = order_id
            db.session.commit()
            
            url = "https://test.ccavenue.com/transaction/transaction.do?command=initiateTransaction" if mode == 'sandbox' else "https://secure.ccavenue.com/transaction/transaction.do?command=initiateTransaction"
            
            return True, {
                "action": "form",
                "action_url": url,
                "form_fields": {
                    "encRequest": enc_data,
                    "access_code": access_code,
                    "merchant_id": merchant_id
                }
            }
        except Exception as e:
            return False, f"Encryption Error: {str(e)}"

    @staticmethod
    def verify_payment(gateway_code, data):
        if gateway_code == 'paypal':
            status = data.get('status')
            token = data.get('token')
            if status == 'cancel':
                return False, None, "Payment Cancelled by User."
            if status == 'success' and token:
                txn = Transaction.query.filter_by(transaction_id=token).first()
                if not txn:
                    txn = Transaction.query.filter_by(user_id=current_user.id, status='pending').order_by(Transaction.id.desc()).first()
                if txn:
                    return True, txn.id, "Payment Successful"
                return False, None, "Transaction not found."

        elif gateway_code == 'cashfree':
            order_id = data.get('order_id')
            if order_id:
                txn = Transaction.query.filter_by(transaction_id=order_id).first()
                if txn:
                    gw = PaymentGateway.query.filter_by(code='cashfree').first()
                    if gw:
                        try:
                            config = json.loads(gw.config_json)
                            app_id = config.get('app_id', '').strip()
                            secret_key = config.get('secret_key', '').strip()
                            env_url = "https://sandbox.cashfree.com/pg" if gw.mode == 'sandbox' else "https://api.cashfree.com/pg"
                            
                            headers = {
                                "x-client-id": app_id,
                                "x-client-secret": secret_key,
                                "x-api-version": "2023-08-01",
                                "Content-Type": "application/json"
                            }

                            # 1. Try checking as a Payment Link (Matches our new Checkout Logic)
                            resp = requests.get(f"{env_url}/links/{order_id}", headers=headers)
                            if resp.status_code == 200:
                                link_data = resp.json()
                                if link_data.get('link_status') == 'PAID':
                                    return True, txn.id, "Payment Successful"
                                else:
                                    return False, txn.id, f"Payment Status: {link_data.get('link_status')}"
                            else:
                                # 2. Graceful Fallback to check as standard Order (Supports old pending transactions)
                                resp_order = requests.get(f"{env_url}/orders/{order_id}", headers=headers)
                                if resp_order.status_code == 200:
                                    order_data = resp_order.json()
                                    if order_data.get('order_status') == 'PAID':
                                        return True, txn.id, "Payment Successful"
                                    else:
                                        return False, txn.id, f"Payment Status: {order_data.get('order_status')}"

                        except Exception as e:
                            print(f"Cashfree API verification error: {e}")
                            
                    return False, txn.id, "Payment Verification Failed or Cancelled."
            return False, None, "Cashfree callback missing order_id or transaction not found."

        elif gateway_code == 'ccavenue':
            encResp = data.get('encResp')
            if encResp:
                gw = PaymentGateway.query.filter_by(code='ccavenue').first()
                if gw:
                    try:
                        config = json.loads(gw.config_json)
                        working_key = str(config.get('working_key', '')).strip()
                        from Crypto.Cipher import AES
                        import hashlib
                        
                        byte_key = hashlib.md5(working_key.encode('utf-8')).digest()
                        iv = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f'
                        
                        cipher = AES.new(byte_key, AES.MODE_CBC, iv)
                        dec_data = cipher.decrypt(bytes.fromhex(encResp)).decode('utf-8')
                        
                        pad_len = ord(dec_data[-1])
                        if 1 <= pad_len <= 16:
                            dec_data = dec_data[:-pad_len]
                        
                        parsed = dict(q.split('=') for q in dec_data.split('&') if '=' in q)
                        if parsed.get('order_status') == 'Success' or parsed.get('order_status') == 'Shipped':
                            txn = Transaction.query.filter_by(transaction_id=parsed.get('order_id')).first()
                            if txn: 
                                return True, txn.id, "Payment Successful"
                    except Exception:
                        pass
            return False, None, "CCAvenue Payment Failed or Cancelled."
                
        return False, None, "Unknown Gateway or Invalid Data"