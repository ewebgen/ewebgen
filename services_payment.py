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
    # PAYPAL (REST API: OAuth 2.0 + Orders v2 + Payments v2 + Webhooks)
    #   create order -> buyer approves on PayPal -> server CAPTURES the order
    #   -> verifies status / amount / reference -> plan is activated
    #   Refunds go through /v2/payments/captures/{id}/refund.
    #   Sandbox: https://api-m.sandbox.paypal.com   Live: https://api-m.paypal.com
    # ---------------------------------------------------
    PAYPAL_TIMEOUT = (15, 45)
    INR_PER_USD = 83.5

    @staticmethod
    def paypal_base_url(mode):
        return "https://api-m.sandbox.paypal.com" if mode == 'sandbox' else "https://api-m.paypal.com"

    @staticmethod
    def paypal_settings():
        """(config, mode, base_url) of the PayPal gateway, or (None, None, None)."""
        gw = PaymentGateway.query.filter_by(code='paypal').first()
        if not gw:
            return None, None, None
        try:
            config = json.loads(gw.config_json or '{}')
        except ValueError:
            config = {}
        return config, gw.mode, PaymentService.paypal_base_url(gw.mode)

    @staticmethod
    def paypal_access_token(config, mode):
        client_id = str(config.get('client_id', '')).strip()
        secret = str(config.get('client_secret', '')).strip()
        if not client_id or not secret:
            return False, "PayPal credentials missing. Please configure them in the Admin CMS."
        try:
            r = requests.post(f"{PaymentService.paypal_base_url(mode)}/v1/oauth2/token",
                              auth=(client_id, secret), data={'grant_type': 'client_credentials'},
                              headers={'Accept': 'application/json'}, timeout=PaymentService.PAYPAL_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return False, f"PayPal Connection Error: {e}"
        if r.status_code != 200:
            detail = r.text
            try:
                js = r.json()
                detail = js.get('error_description', js.get('error', r.text))
            except ValueError:
                pass
            return False, (f"PayPal Auth Failed ({r.status_code}): Ensure your Client ID and Secret match the "
                           f"selected mode ({str(mode).upper()}). Details: {detail}")
        return True, r.json()['access_token']

    @staticmethod
    def _paypal_error(data, fallback):
        msg = data.get('message', fallback) if isinstance(data, dict) else fallback
        details = data.get('details', []) if isinstance(data, dict) else []
        issues = [f"{d.get('issue', '')} - {d.get('description', '')}" for d in details if isinstance(d, dict)]
        return msg + (" | " + " | ".join(issues) if issues else "")

    @staticmethod
    def paypal_amount_for(txn):
        """PayPal does not accept INR for most merchants: convert to USD. Returns (currency, amount)."""
        currency = str(txn.currency).strip().upper()[:3] if txn.currency else "USD"
        amount = float(txn.amount)
        if currency == 'INR':
            currency = 'USD'
            amount = max(0.01, round(amount / PaymentService.INR_PER_USD, 2))
        return currency, round(amount, 2)

    @staticmethod
    def _create_paypal_rest_link(txn, config, mode):
        ok, token = PaymentService.paypal_access_token(config, mode)
        if not ok:
            return False, token
        base_url = PaymentService.paypal_base_url(mode)
        currency, amount = PaymentService.paypal_amount_for(txn)

        meta = {}
        try:
            meta = json.loads(txn.response_json or '{}')
        except ValueError:
            pass
        item_desc = meta.get('plan_name', meta.get('product_name', "eWebGen Subscription"))
        item_desc = "".join(c for c in str(item_desc) if c.isprintable()).strip() or "eWebGen Subscription"

        root = PaymentService.get_safe_url_root()
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": str(txn.id),
                "custom_id": str(txn.id),
                "invoice_id": f"EWG-{txn.id}-{int(time.time())}",
                "description": item_desc[:127],
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"}
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "eWebGen",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": f"{root}api/payment/callback/paypal?status=success",
                "cancel_url": f"{root}api/payment/callback/paypal?status=cancel"
            }}}
        }
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}',
                   'PayPal-Request-Id': f"ewg-order-{txn.id}-{int(time.time())}"}
        try:
            r = requests.post(f"{base_url}/v2/checkout/orders", headers=headers, json=payload,
                              timeout=PaymentService.PAYPAL_TIMEOUT)
            data = r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, f"PayPal API Error: {e}"

        if r.status_code in (200, 201):
            link = next((l['href'] for l in data.get('links', []) if l.get('rel') in ('payer-action', 'approve')), None)
            if link:
                txn.transaction_id = data['id']
                meta.update({'paypal_order_id': data['id'], 'paypal_currency': currency,
                             'paypal_amount': f"{amount:.2f}", 'paypal_mode': mode})
                txn.response_json = json.dumps(meta)
                db.session.commit()
                return True, {"action": "redirect", "url": link}
        return False, "PayPal Error: " + PaymentService._paypal_error(data, 'Order creation failed')

    @staticmethod
    def paypal_capture_order(order_id):
        """
        Captures an approved order (idempotent). Returns (ok, info) where info has
        status, capture_id, amount, currency, reference_id, payer_email - or an error string.
        """
        config, mode, base_url = PaymentService.paypal_settings()
        if config is None:
            return False, "PayPal gateway not configured."
        ok, token = PaymentService.paypal_access_token(config, mode)
        if not ok:
            return False, token
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
        try:
            r = requests.get(f"{base_url}/v2/checkout/orders/{order_id}", headers=headers,
                             timeout=PaymentService.PAYPAL_TIMEOUT)
            order = r.json()
            if r.status_code != 200:
                return False, "PayPal Error: " + PaymentService._paypal_error(order, 'Order not found')
            if order.get('status') == 'APPROVED':
                r = requests.post(f"{base_url}/v2/checkout/orders/{order_id}/capture",
                                  headers={**headers, 'PayPal-Request-Id': f"ewg-capture-{order_id}"},
                                  json={}, timeout=PaymentService.PAYPAL_TIMEOUT)
                order = r.json()
                if r.status_code not in (200, 201):
                    return False, "PayPal Capture Failed: " + PaymentService._paypal_error(order, 'Capture failed')
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, f"PayPal API Error: {e}"

        unit = (order.get('purchase_units') or [{}])[0]
        captures = (unit.get('payments') or {}).get('captures') or []
        cap = captures[0] if captures else {}
        amt = cap.get('amount') or unit.get('amount') or {}
        return True, {
            "order_status": order.get('status'),
            "status": cap.get('status') or order.get('status'),
            "capture_id": cap.get('id'),
            "amount": amt.get('value'),
            "currency": amt.get('currency_code'),
            "reference_id": unit.get('reference_id') or cap.get('custom_id'),
            "payer_email": (order.get('payer') or {}).get('email_address'),
        }

    @staticmethod
    def paypal_finalize(txn):
        """Capture + verify a PayPal transaction. Returns (success, message). Safe to call repeatedly."""
        if txn.status == 'success':
            return True, "Payment already confirmed."
        meta = {}
        try:
            meta = json.loads(txn.response_json or '{}')
        except ValueError:
            pass
        ok, info = PaymentService.paypal_capture_order(txn.transaction_id)
        if not ok:
            return False, info
        meta.update({'paypal_status': info['status'], 'paypal_capture_id': info['capture_id'],
                     'paypal_payer': info['payer_email']})
        txn.response_json = json.dumps(meta)
        db.session.commit()

        exp_currency, exp_amount = PaymentService.paypal_amount_for(txn)
        exp_currency = meta.get('paypal_currency', exp_currency)
        exp_amount = float(meta.get('paypal_amount', exp_amount))
        if str(info.get('reference_id')) != str(txn.id):
            return False, "PayPal order does not belong to this transaction."
        if info['status'] != 'COMPLETED':
            if info['status'] == 'PENDING':
                return False, "PayPal is still reviewing this payment (PENDING). Your plan will activate once it completes."
            return False, f"Payment not completed (PayPal status: {info['status']})."
        try:
            paid = float(info.get('amount') or 0)
        except ValueError:
            paid = 0
        if (info.get('currency') or '').upper() != exp_currency.upper() or abs(paid - exp_amount) > 0.01:
            return False, f"Paid amount {info.get('amount')} {info.get('currency')} does not match {exp_amount:.2f} {exp_currency}."
        return True, "Payment Successful"

    @staticmethod
    def paypal_refund(txn, amount=None, note="Refund from eWebGen"):
        """Full refund (amount=None) or partial refund (amount in the transaction's currency)."""
        meta = {}
        try:
            meta = json.loads(txn.response_json or '{}')
        except ValueError:
            pass
        capture_id = meta.get('paypal_capture_id')
        if not capture_id:
            return False, "No PayPal capture ID on this transaction (it was never captured)."
        config, mode, base_url = PaymentService.paypal_settings()
        if config is None:
            return False, "PayPal gateway not configured."
        ok, token = PaymentService.paypal_access_token(config, mode)
        if not ok:
            return False, token
        body = {"note_to_payer": note[:255]}
        if amount is not None:
            currency = meta.get('paypal_currency') or PaymentService.paypal_amount_for(txn)[0]
            ratio = float(meta.get('paypal_amount', txn.amount)) / float(txn.amount) if txn.amount else 1
            body["amount"] = {"currency_code": currency, "value": f"{max(0.01, float(amount) * ratio):.2f}"}
        try:
            r = requests.post(f"{base_url}/v2/payments/captures/{capture_id}/refund",
                              headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}',
                                       'PayPal-Request-Id': f"ewg-refund-{txn.id}-{int(time.time())}"},
                              json=body, timeout=PaymentService.PAYPAL_TIMEOUT)
            data = r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, f"PayPal API Error: {e}"
        if r.status_code in (200, 201):
            return True, {"refund_id": data.get('id'), "status": data.get('status'),
                          "amount": (data.get('amount') or body.get('amount') or {}).get('value')}
        return False, "PayPal Refund Failed: " + PaymentService._paypal_error(data, 'Refund failed')

    @staticmethod
    def paypal_verify_webhook(headers, event):
        """Checks a webhook's signature with PayPal (needs the Webhook ID from the PayPal developer dashboard)."""
        config, mode, base_url = PaymentService.paypal_settings()
        webhook_id = str((config or {}).get('webhook_id', '')).strip()
        if not webhook_id:
            return False, "Webhook ID not configured."
        ok, token = PaymentService.paypal_access_token(config, mode)
        if not ok:
            return False, token
        payload = {
            "auth_algo": headers.get('PAYPAL-AUTH-ALGO'),
            "cert_url": headers.get('PAYPAL-CERT-URL'),
            "transmission_id": headers.get('PAYPAL-TRANSMISSION-ID'),
            "transmission_sig": headers.get('PAYPAL-TRANSMISSION-SIG'),
            "transmission_time": headers.get('PAYPAL-TRANSMISSION-TIME'),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        try:
            r = requests.post(f"{base_url}/v1/notifications/verify-webhook-signature",
                              headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
                              json=payload, timeout=PaymentService.PAYPAL_TIMEOUT)
            status = r.json().get('verification_status')
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, str(e)
        return status == 'SUCCESS', status

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
            token = data.get('token')          # PayPal returns ?token=<order id>
            txn = Transaction.query.filter_by(transaction_id=token).first() if token else None
            if status == 'cancel':
                return False, (txn.id if txn else None), "Payment Cancelled by User."
            if not txn:
                return False, None, "Transaction not found."
            # Never trust the return URL: capture the order on the server and verify it with PayPal.
            ok, message = PaymentService.paypal_finalize(txn)
            return ok, txn.id, message

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