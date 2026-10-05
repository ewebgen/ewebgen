import os
import json
from datetime import datetime, timezone, timedelta
from flask import request, jsonify, redirect
from flask_login import login_required, current_user

from database_setup import app, db, PaymentGateway, Transaction, SubscriptionPlan, User, SystemSettings, UserActivityLog, ApiKeyRequest
from services_payment import PaymentService

# We import EmailService to fulfill Goal 4 (Notifications)
try:
    from services_core import EmailService
except ImportError:
    pass

# ==========================================
# NEW DATABASE MODEL: ORDER
# ==========================================
class Order(db.Model):
    __tablename__ = 'user_order'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    transaction_id = db.Column(db.Integer, db.ForeignKey('transaction.id'), nullable=True)
    product_name = db.Column(db.String(100))
    price = db.Column(db.Float)
    currency = db.Column(db.String(10), default='USD')
    duration_days = db.Column(db.Integer)
    status = db.Column(db.String(20), default='active') 
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expiry_date = db.Column(db.DateTime)

    user = db.relationship('User', backref=db.backref('orders', lazy=True))
    transaction = db.relationship('Transaction', backref=db.backref('order_details', uselist=False))

# Safely ensure the new table is created upon boot
with app.app_context():
    db.create_all()

# ==========================================
# NOTIFICATION ENGINE
# ==========================================
def send_order_notification(order, action_type):
    try:
        admin_user = User.query.filter_by(role='admin').first()
        admin_email = admin_user.email if admin_user else "admin@ewebgen.com"
        user_email = order.user.email if order.user else "user@example.com"
        
        if action_type == 'created':
            EmailService.send_email(user_email, f"Order Confirmation: {order.product_name}", f"Thank you for your purchase of {order.product_name}.<br><br>Order ID: #{order.id}<br>Status: {order.status.upper()}<br>Expiry: {order.expiry_date.strftime('%Y-%m-%d') if order.expiry_date else 'Lifetime'}")
            EmailService.send_email(admin_email, f"New Order Received: #{order.id}", f"User {user_email} has purchased {order.product_name}.<br>Price: {order.price} {order.currency}")
        elif action_type == 'cancelled':
            EmailService.send_email(user_email, f"Order Cancelled: {order.product_name}", f"Your order #{order.id} for {order.product_name} has been successfully cancelled.")
            EmailService.send_email(admin_email, f"Order Cancelled: #{order.id}", f"Order #{order.id} by {user_email} has been cancelled.")
        elif action_type == 'expiring_soon':
            EmailService.send_email(user_email, f"Action Required: {order.product_name} Expiring Soon", f"Your order #{order.id} will expire on {order.expiry_date.strftime('%Y-%m-%d')}. Please log in to renew your subscription.")
        elif action_type == 'expired':
            EmailService.send_email(user_email, f"Expired: {order.product_name}", f"Your order #{order.id} has officially expired.")
    except Exception as e:
        print(f"Notification Error: {e}")

# ==========================================
# 0. INITIATE PAYMENT & CALLBACKS
# ==========================================
@app.route('/api/pay/initiate', methods=['POST'])
@login_required
def initiate_payment():
    data = request.json
    gateway_code = data.get('gateway_code')
    amount = float(data.get('amount', 0))
    currency = data.get('currency', 'USD')
    metadata = data.get('metadata', {})
    
    plan_id = data.get('plan_id') or metadata.get('plan_id')
    if plan_id:
        plan = SubscriptionPlan.query.get(plan_id)
        if plan:
            amount = float(plan.price)
            metadata['plan_name'] = plan.name
            metadata['plan_id'] = plan.id 
            metadata['duration'] = getattr(plan, 'duration_days', getattr(plan, 'duration', 30))
    
    if amount <= 0:
        return jsonify({"success": False, "error": "Invalid amount."})

    gateway = PaymentGateway.query.filter_by(code=gateway_code).first()
    if not gateway or not gateway.is_active:
        return jsonify({"success": False, "error": "Gateway not available."})

    txn = Transaction(
        user_id=current_user.id,
        gateway_id=gateway.id,
        amount=amount,
        currency=currency,
        status='pending',
        response_json=json.dumps(metadata)
    )
    db.session.add(txn)
    db.session.commit()

    success, result = PaymentService.create_payment_link(txn, gateway_code)
    
    if success:
        return jsonify({"success": True, **result})
    else:
        txn.status = 'failed'
        meta = json.loads(txn.response_json or '{}')
        meta['error_msg'] = str(result)
        txn.response_json = json.dumps(meta)
        db.session.commit()
        return jsonify({"success": False, "error": str(result)})

@app.route('/api/payment/callback/<gateway_code>', methods=['GET', 'POST'])
def payment_callback(gateway_code):
    data = request.args.to_dict() if request.method == 'GET' else request.form.to_dict()
    
    if request.is_json:
        data.update(request.json)
        
    success, txn_id, message = PaymentService.verify_payment(gateway_code, data)
    
    if success and txn_id:
        txn = Transaction.query.get(txn_id)
        if txn and txn.status != 'success':
            txn.status = 'success'
            
            meta = {}
            try: meta = json.loads(txn.response_json or '{}')
            except: pass
            
            # 1. Handle API Key Purchase
            if meta.get('type') == 'api_key_purchase':
                new_req = ApiKeyRequest(
                    user_id=txn.user_id,
                    provider=meta.get('provider', 'Gemini'),
                    amount=txn.amount,
                    status='Pending'
                )
                db.session.add(new_req)
            
            # 2. Handle Subscription Upgrade/Order
            elif meta.get('type') == 'plan_subscription':
                existing_order = Order.query.filter_by(transaction_id=txn.id).first()
                if not existing_order:
                    plan_name = meta.get('plan_name', 'Subscription/Plan Upgrade')
                    duration = int(meta.get('duration', 30))
                    plan_id = meta.get('plan_id')
                    
                    user = User.query.get(txn.user_id)
                    base_date = datetime.now(timezone.utc)
                    if user and hasattr(user, 'plan_expiry') and user.plan_expiry and user.plan_expiry > base_date:
                        base_date = user.plan_expiry
                    
                    expiry = base_date + timedelta(days=duration) if duration > 0 else None
                    
                    new_order = Order(
                        user_id=txn.user_id,
                        transaction_id=txn.id,
                        product_name=plan_name,
                        price=txn.amount,
                        currency=txn.currency,
                        duration_days=duration,
                        status='active',
                        created_at=datetime.now(timezone.utc),
                        expiry_date=expiry
                    )
                    db.session.add(new_order)
                    
                    plan = SubscriptionPlan.query.get(plan_id)
                    if user and plan:
                        user.user_type_id = plan.user_type_id 
                        if hasattr(user, 'plan_name'): user.plan_name = plan.name
                        if hasattr(user, 'current_plan'): user.current_plan = plan.name
                        if hasattr(user, 'plan_id'): user.plan_id = plan.id
                        if hasattr(user, 'plan_expiry'): user.plan_expiry = expiry
                    
                    try: send_order_notification(new_order, 'created')
                    except: pass

            db.session.commit()
        return redirect('/?payment=success')
    else:
        if txn_id:
            txn = Transaction.query.get(txn_id)
            if txn and txn.status != 'success':
                txn.status = 'failed'
                db.session.commit()
        return redirect(f'/?payment=failed&reason={message}')

# ==========================================
# 1. LIST GATEWAYS & UPDATE GATEWAYS
# ==========================================
@app.route('/api/admin/payment_gateways', methods=['GET'])
@login_required
def list_payment_gateways():
    is_admin = current_user.role == 'admin'
    
    if is_admin:
        gws = PaymentGateway.query.all()
    else:
        gws = PaymentGateway.query.filter_by(is_active=True).all()
        
    admin_user = User.query.filter_by(role='admin').first()
    admin_country = getattr(admin_user, 'country', None) if admin_user else None
    admin_is_indian = admin_country and admin_country.strip().lower() in ['india', 'in', 'ind']
    
    user_country = getattr(current_user, 'country', None)
    user_is_indian = user_country and user_country.strip().lower() in ['india', 'in', 'ind']
    
    results = []
    for gw in gws:
        if not is_admin and gw.code == 'paypal' and admin_is_indian and user_is_indian:
            continue
            
        data = {
            "id": gw.id,
            "code": gw.code,
            "name": gw.name,
            "is_active": gw.is_active,
            "mode": gw.mode 
        }
        if is_admin:
            data["config"] = json.loads(gw.config_json or '{}')
        results.append(data)
        
    return jsonify({"success": True, "gateways": results})

@app.route('/api/admin/payment_gateways/update', methods=['POST'])
@app.route('/api/admin/update_payment_gateway', methods=['POST'])
@app.route('/api/admin/update_gateway', methods=['POST'])
@login_required
def admin_update_payment_gateway():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.json
    gw_id = data.get('id')
    code = data.get('code')
    
    if gw_id:
        gw = PaymentGateway.query.get(gw_id)
    elif code:
        gw = PaymentGateway.query.filter_by(code=code).first()
    else:
        return jsonify({"success": False, "error": "Gateway ID or Code missing"}), 400
        
    if not gw:
        return jsonify({"success": False, "error": "Gateway not found"}), 404
        
    if 'is_active' in data:
        gw.is_active = bool(data['is_active'])
        
    if 'mode' in data:
        gw.mode = data['mode']
        
    if 'config' in data:
        try:
            gw.config_json = json.dumps(data['config']) if isinstance(data['config'], dict) else data['config']
        except Exception as e:
            return jsonify({"success": False, "error": "Invalid config format"}), 400
            
    db.session.commit()
    return jsonify({"success": True, "message": f"{gw.name} updated successfully."})

# ==========================================
# 2. TRANSACTIONS LIST
# ==========================================
@app.route('/api/transactions', methods=['GET'])
@login_required
def get_transactions():
    if current_user.role == 'admin':
        txns = Transaction.query.order_by(Transaction.id.desc()).all()
    else:
        txns = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.id.desc()).all()
        
    results = []
    for t in txns:
        meta = {}
        try: meta = json.loads(t.response_json or '{}')
        except: pass
        
        results.append({
            "id": t.id,
            "user": t.user.email if t.user else "Unknown",
            "gateway": t.gateway.name if t.gateway else "Unknown",
            "amount": t.amount,
            "currency": t.currency,
            "status": t.status,
            "date": t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "error_log": meta.get('error_msg', 'None'),
            "refund_status": meta.get('refund_status', 'None')
        })
    return jsonify({"success": True, "transactions": results})

@app.route('/api/admin/transactions/<int:txn_id>/refund', methods=['POST'])
@login_required
def admin_refund_transaction(txn_id):
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    refund_type = data.get('type', 'full') 
    
    txn = Transaction.query.get(txn_id)
    if not txn:
        return jsonify({"success": False, "error": "Transaction not found"}), 404
        
    meta = {}
    try: meta = json.loads(txn.response_json or '{}')
    except: pass
        
    meta['refund_status'] = refund_type
    meta['refund_date'] = datetime.now(timezone.utc).isoformat()
    
    txn.response_json = json.dumps(meta)
    txn.status = 'refunded'
    db.session.commit()
    
    return jsonify({"success": True, "message": f"{refund_type.capitalize()} refund processed manually."})

# ==========================================
# 3. USER ORDERS & RENEWALS (MODIFIED FOR DOMAINS & API KEYS)
# ==========================================

@app.route('/api/user/orders/external', methods=['POST'])
@login_required
def create_external_order():
    """Logs external purchases like Domains and Hosting before redirect."""
    data = request.json
    order_type = data.get('type') # 'domain' or 'hosting'
    action = data.get('action', 'buy')
    details = data.get('details', '').strip()

    if order_type == 'domain':
        product_name = "Domain Transfer" if action == 'transfer' else "Domain Registration"
        if details:
            product_name += f" ({details})"
    else:
        product_name = "Web Hosting"

    # Prevent immediate double logging
    existing = Order.query.filter_by(
        user_id=current_user.id,
        product_name=product_name,
        status='Pending Configuration'
    ).first()

    if not existing:
        new_order = Order(
            user_id=current_user.id,
            product_name=product_name,
            price=0.0,  
            currency='USD',
            duration_days=365,
            status='Pending Configuration'
        )
        db.session.add(new_order)
        db.session.commit()

    return jsonify({"success": True})


@app.route('/api/orders', methods=['GET'])
@app.route('/api/user/orders', methods=['GET'])
@login_required
def get_user_orders():
    # --- AUTO-MIGRATE OLD SUCCESSFUL TRANSACTIONS TO ORDERS ---
    try:
        old_txns = Transaction.query.filter_by(user_id=current_user.id, status='success').all()
        for txn in old_txns:
            existing_order = Order.query.filter_by(transaction_id=txn.id).first()
            if not existing_order:
                meta = {}
                try: meta = json.loads(txn.response_json or '{}')
                except: pass
                
                # Only migrate plan subscriptions
                if meta.get('type') == 'plan_subscription':
                    plan_name = meta.get('plan_name', 'Subscription/Plan Upgrade')
                    duration = int(meta.get('duration', 30))
                    
                    expiry = txn.timestamp + timedelta(days=duration) if duration > 0 else None
                    status = 'active'
                    if expiry and expiry < datetime.now(timezone.utc):
                        status = 'expired'
                        
                    new_order = Order(
                        user_id=current_user.id,
                        transaction_id=txn.id,
                        product_name=plan_name,
                        price=txn.amount,
                        currency=txn.currency,
                        duration_days=duration,
                        status=status,
                        created_at=txn.timestamp,
                        expiry_date=expiry
                    )
                    db.session.add(new_order)
        
        db.session.commit() 
    except Exception as e:
        db.session.rollback()
        print(f"Error migrating orders: {e}")
    
    # --- FETCH ALL STANDARD ORDERS ---
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    result = []
    
    current_time = datetime.now(timezone.utc)
    for o in orders:
        if o.expiry_date:
            expiry_dt = o.expiry_date
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            
            if o.status == 'active' and expiry_dt < current_time:
                o.status = 'expired'
                db.session.commit()
            
        plan = SubscriptionPlan.query.filter_by(name=o.product_name).first()
        plan_id = plan.id if plan else None

        # Custom tag to flag if it's an external order (Domain/Hosting)
        is_external = 'Domain' in o.product_name or 'Hosting' in o.product_name
        
        result.append({
            "id": o.id,
            "product_name": o.product_name,
            "plan_id": plan_id, 
            "price": o.price,
            "currency": o.currency,
            "duration": o.duration_days,
            "status": o.status,
            "is_external": is_external,
            "created_at": o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else "Unknown",
            "expiry_date": o.expiry_date.strftime('%Y-%m-%d') if o.expiry_date else "Lifetime",
            "timestamp": o.created_at.timestamp() if o.created_at else 0
        })

    # --- FETCH API KEY REQUESTS & INJECT INTO UNIFIED ORDERS ---
    api_reqs = ApiKeyRequest.query.filter_by(user_id=current_user.id).order_by(ApiKeyRequest.created_at.desc()).all()
    for r in api_reqs:
        result.append({
            "id": f"API-{r.id}", # String ID prevents the UI from generating a cancellation button
            "product_name": f"{r.provider} API Key",
            "plan_id": None,
            "price": r.amount,
            "currency": "USD",
            "duration": 0,
            "status": r.status.lower(),
            "is_external": False,
            "assigned_key": r.assigned_key if r.assigned_key else "",
            "created_at": r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else "Unknown",
            "expiry_date": "Lifetime",
            "timestamp": r.created_at.timestamp() if r.created_at else 0
        })

    # Sort combined list by date descending
    result.sort(key=lambda x: x.get('timestamp', 0), reverse=True)

    return jsonify({"success": True, "orders": result})

@app.route('/api/user/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def user_cancel_order(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first()
    if not order: return jsonify({"success": False, "error": "Order not found"}), 404
    
    order.status = 'cancelled'
    db.session.commit()
    send_order_notification(order, 'cancelled')
    return jsonify({"success": True, "message": "Order cancelled successfully"})

@app.route('/api/user/orders/<int:order_id>/renew', methods=['POST'])
@login_required
def user_renew_order(order_id):
    data = request.json or {}
    gateway_code = data.get('gateway_code')
    
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first()
    if not order: 
        return jsonify({"success": False, "error": "Order not found"}), 404
    
    plan = SubscriptionPlan.query.filter_by(name=order.product_name).first()
    if not plan: 
        return jsonify({"success": False, "error": "This subscription plan is no longer available"}), 400
    
    if not gateway_code:
        return jsonify({
            "success": True, 
            "message": "Ready to renew", 
            "plan_id": plan.id, 
            "amount": plan.price,
            "currency": plan.currency
        })
        
    amount = float(plan.price)
    metadata = {
        'type': 'plan_subscription',
        'plan_name': plan.name,
        'plan_id': plan.id,
        'duration': getattr(plan, 'duration_days', getattr(plan, 'duration', 30))
    }
    
    gateway = PaymentGateway.query.filter_by(code=gateway_code).first()
    if not gateway or not gateway.is_active: 
        return jsonify({"success": False, "error": "Gateway not available."})

    txn = Transaction(
        user_id=current_user.id, 
        gateway_id=gateway.id, 
        amount=amount, 
        currency=plan.currency, 
        status='pending', 
        response_json=json.dumps(metadata)
    )
    db.session.add(txn)
    db.session.commit()

    success, result = PaymentService.create_payment_link(txn, gateway_code)
    
    if success: 
        return jsonify({"success": True, **result})
    else:
        txn.status = 'failed'
        db.session.commit()
        return jsonify({"success": False, "error": str(result)})

# ==========================================
# 4. ADMIN ORDERS 
# ==========================================
@app.route('/api/admin/orders', methods=['GET'])
@login_required
def get_all_orders():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    orders = Order.query.order_by(Order.created_at.desc()).all()
    result = []
    
    current_time = datetime.now(timezone.utc)
    
    for o in orders:
        if o.expiry_date:
            expiry_dt = o.expiry_date
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

            if o.status == 'active' and expiry_dt < current_time:
                o.status = 'expired'
                db.session.commit()
            
        result.append({
            "id": o.id,
            "user_email": o.user.email if o.user else "Unknown",
            "user_id": o.user.id if o.user else "",
            "user_country": getattr(o.user, 'country', 'Unknown') if o.user else "Unknown",
            "transaction_id": o.transaction_id,
            "product_name": o.product_name,
            "price": o.price,
            "currency": o.currency,
            "duration": o.duration_days,
            "status": o.status,
            "created_at": o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else "Unknown",
            "expiry_date": o.expiry_date.strftime('%Y-%m-%d') if o.expiry_date else "Lifetime"
        })
    return jsonify({"success": True, "orders": result})

@app.route('/api/admin/orders/<int:order_id>/modify', methods=['POST'])
@login_required
def admin_modify_order(order_id):
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    order = Order.query.get(order_id)
    if not order: return jsonify({"success": False, "error": "Order not found"}), 404
    
    if 'status' in data:
        order.status = data['status']
        if data['status'] == 'cancelled':
            send_order_notification(order, 'cancelled')
            
    if 'expiry_date' in data:
        try: order.expiry_date = datetime.strptime(data['expiry_date'], '%Y-%m-%d')
        except: pass
        
    db.session.commit()
    return jsonify({"success": True, "message": "Order modified successfully"})

# ==========================================
# 5. CAPTURE ORDER ON PAYMENT SUCCESS
# ==========================================
@app.route('/api/user/orders/create_from_txn', methods=['POST'])
@login_required
def create_order_from_txn():
    txn_id = request.json.get('transaction_id')
    txn = Transaction.query.get(txn_id)
    if not txn or txn.user_id != current_user.id:
        return jsonify({"success": False, "error": "Invalid transaction"})
        
    existing = Order.query.filter_by(transaction_id=txn.id).first()
    if existing: return jsonify({"success": True, "order_id": existing.id})
    
    meta = {}
    try: meta = json.loads(txn.response_json or '{}')
    except: pass
    
    # Only create if it's a plan subscription
    if meta.get('type') != 'plan_subscription':
        return jsonify({"success": False, "error": "Transaction not a plan subscription."})
        
    plan_name = request.json.get('plan_name') or meta.get('plan_name', 'Subscription')
    duration = int(request.json.get('duration') or meta.get('duration', 30))
    plan_id = meta.get('plan_id')
    
    user = User.query.get(current_user.id)
    
    # --- GOAL: EXTEND EXPIRY LOGIC (Preserve existing time + add new duration) ---
    base_date = datetime.now(timezone.utc)
    if user and hasattr(user, 'plan_expiry') and user.plan_expiry and user.plan_expiry > base_date:
        base_date = user.plan_expiry
        
    expiry = base_date + timedelta(days=duration) if duration > 0 else None
        
    order = Order(
        user_id=current_user.id,
        transaction_id=txn.id,
        product_name=plan_name,
        price=txn.amount,
        currency=txn.currency,
        duration_days=duration,
        status='active',
        expiry_date=expiry
    )
    db.session.add(order)
    
    plan = None
    if plan_id:
        plan = SubscriptionPlan.query.get(plan_id)
    if not plan and plan_name and plan_name != 'Subscription/Plan Upgrade':
        plan = SubscriptionPlan.query.filter_by(name=plan_name).first()

    if user and plan:
        user.user_type_id = plan.user_type_id 
        
        if hasattr(user, 'plan_name'): user.plan_name = plan.name
        if hasattr(user, 'current_plan'): user.current_plan = plan.name
        if hasattr(user, 'plan_id'): user.plan_id = plan.id
        if hasattr(user, 'plan_expiry'): user.plan_expiry = expiry
        
        try:
            log = UserActivityLog(
                user_id=user.id,
                action_type='order_placed',
                description=f"Purchased subscription: '{plan.name}'"
            )
            db.session.add(log)
        except: pass
    
    db.session.commit()
    
    try: send_order_notification(order, 'created')
    except: pass
    return jsonify({"success": True, "order_id": order.id})

# ==========================================
# 6. CRON CHECK EXPIRIES
# ==========================================
@app.route('/api/cron/check_expiries', methods=['GET'])
def check_expiries():
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    orders = Order.query.filter(Order.status == 'active', Order.expiry_date != None, Order.expiry_date <= soon).all()
    for o in orders:
        send_order_notification(o, 'expiring_soon')
    
    expired = Order.query.filter(Order.status == 'active', Order.expiry_date != None, Order.expiry_date < datetime.now(timezone.utc)).all()
    for o in expired:
        o.status = 'expired'
        send_order_notification(o, 'expired')
        
    db.session.commit()
    return jsonify({"success": True, "message": "Expiry check complete."})


# ==========================================
# 7. EXTERNAL WEBHOOK (API KEY SYNC)
# ==========================================
@app.route('/api/external/api_key_webhook', methods=['POST', 'OPTIONS'])
def external_api_key_webhook():
    """Receives status updates and assigned keys from Owner server."""
    if request.method == 'OPTIONS':
        return jsonify({"success": True}), 200
        
    data = request.json or {}
    local_id = data.get('local_request_id')
    status = data.get('status')
    assigned_key = data.get('assigned_key', '')
    
    if not local_id or not status:
        return jsonify({"success": False, "error": "Missing local_request_id or status."}), 400
        
    req = ApiKeyRequest.query.get(local_id)
    if not req:
        return jsonify({"success": False, "error": "Local request not found."}), 404
        
    req.status = status
    if assigned_key:
        req.assigned_key = assigned_key
        
    db.session.commit()
    
    # Notify User Locally
    try:
        if status.lower() == 'completed' and req.user:
            from database_setup import Notification, UserMessage
            import uuid
            subject = f"Your {req.provider} API Key Request is Completed"
            body = f"Hello,\n\nYour request for a {req.provider} API Key (${req.amount}) has been processed.\n\nYour API Key: {req.assigned_key}\n\nYou can now copy this from your My Orders section and paste it into your AI Settings."
            
            thread_id = str(uuid.uuid4())
            n = Notification(user_id=req.user_id, sender="System", subject=subject, body=body, thread_id=thread_id)
            m = UserMessage(user_id=req.user_id, subject=subject, body=body, thread_id=thread_id)
            db.session.add(n)
            db.session.add(m)
            db.session.commit()
    except Exception as e:
        print(f"Error notifying user locally: {e}")
        
    return jsonify({"success": True, "message": "Local request updated."})


# ==========================================
# 8. USER API KEY PURCHASE REQUESTS
# ==========================================
@app.route('/api/user/api_key_requests', methods=['POST'])
@login_required
def submit_api_key_request():
    data = request.json
    provider = data.get('provider')
    amount = data.get('amount')
    
    if not provider or amount is None:
        return jsonify({"success": False, "error": "Provider and amount are required."}), 400
        
    try:
        amount = float(amount)
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be greater than zero."}), 400
    except ValueError:
        return jsonify({"success": False, "error": "Invalid amount."}), 400
        
    new_req = ApiKeyRequest(
        user_id=current_user.id,
        provider=provider,
        amount=amount,
        status='Pending'
    )
    db.session.add(new_req)
    db.session.commit()
    
    # --- GOAL 1 FIX: Check if forwarding is enabled ---
    settings = SystemSettings.query.first()
    forward_to_owner = getattr(settings, 'forward_api_keys_to_owner', False)
    
    if forward_to_owner:
        import requests
        client_domain = request.host.split(':')[0].lower() if hasattr(request, 'host') else "unknown-client"
        payload = {
            "client_domain": client_domain,
            "user_email": current_user.email,
            "provider": provider,
            "amount": float(amount),
            "local_request_id": new_req.id
        }
        try:
            for target_url in ["https://app.ewebgen.com/api/external/api_key_request", "http://127.0.0.1:5000/api/external/api_key_request"]:
                resp = requests.post(target_url, json=payload, timeout=5)
                if resp.status_code in [200, 201]:
                    break
        except Exception as e:
            print(f"Failed to forward API key request to owner: {e}")
    else:
        # Standard Admin Notification
        try:
            admin_user = User.query.filter_by(role='admin').first()
            admin_email = admin_user.email if admin_user else "admin@ewebgen.com"
            user_email = current_user.email
            
            subject = f"New API Key Purchase Request from {user_email}"
            body = f"User {user_email} has requested a {provider} API key worth ${amount}.<br>Please review and assign the key from the Admin CMS."
            EmailService.send_email(admin_email, subject, body)
        except Exception as e:
            print(f"Failed to notify admin of API key request: {e}")
        
    return jsonify({"success": True, "message": "API Key request submitted successfully. Admin will review and process your request."})

@app.route('/api/user/api_key_requests', methods=['GET'])
@login_required
def get_user_api_key_requests():
    requests_list = ApiKeyRequest.query.filter_by(user_id=current_user.id).order_by(ApiKeyRequest.created_at.desc()).all()
    result = []
    for r in requests_list:
        result.append({
            "id": r.id,
            "provider": r.provider,
            "amount": r.amount,
            "status": r.status,
            "assigned_key": r.assigned_key if r.assigned_key else "",
            "created_at": r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else "",
            "updated_at": r.updated_at.strftime('%Y-%m-%d %H:%M') if getattr(r, 'updated_at', None) else ""
        })
    return jsonify({"success": True, "requests": result})