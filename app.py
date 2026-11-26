import csv
import io
import json
import os
import secrets
import sqlite3
import threading
import xml.etree.ElementTree as ET
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from cryptography.fernet import Fernet
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('DATABASE_PATH', os.path.join(ROOT, 'dashboard.sqlite3'))
app = Flask(__name__)
app.secret_key = os.getenv('APP_SECRET') or secrets.token_urlsafe(48)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0') == '1', PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=8 * 1024 * 1024)
CENT = Decimal('0.01')
SCOPES = [
    'https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly',
    'https://api.ebay.com/oauth/api_scope/sell.finances',
    'https://api.ebay.com/oauth/api_scope/sell.inventory.readonly',
    'https://api.ebay.com/oauth/api_scope/sell.analytics.readonly',
    'https://api.ebay.com/oauth/api_scope/commerce.identity.readonly',
]

def now(): return datetime.now(timezone.utc).isoformat()
def money(value):
    try: return str(Decimal(str(value or '0')).quantize(CENT, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError): return '0.00'
@contextmanager
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
def init_db():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=1').fetchone():
            with open(os.path.join(ROOT,'migrations','001_initial.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(1,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
            with open(os.path.join(ROOT,'migrations','002_stock_rules.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(2,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
            with open(os.path.join(ROOT,'migrations','003_demo_roles.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(3,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
            with open(os.path.join(ROOT,'migrations','004_demo_store_modes.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(4,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=5').fetchone():
            with open(os.path.join(ROOT,'migrations','005_demo_share_history.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(5,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=6').fetchone():
            with open(os.path.join(ROOT,'migrations','006_live_ebay_stores.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(6,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=7').fetchone():
            with open(os.path.join(ROOT,'migrations','007_cost_sheets.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(7,?)',(now(),))
        if not c.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone():
            with open(os.path.join(ROOT,'migrations','008_listings.sql'),encoding='utf-8') as f:c.executescript(f.read())
            c.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(8,?)',(now(),))
        # Seed a fresh database once. Never recreate defaults when an owner has
        # renamed or removed their accounts.
        if c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0:
            for name, share in [('Account A','30'),('Account B','30'),('Account C','30'),('Account D','60')]:
                cur = c.execute('INSERT INTO accounts(name,share) VALUES(?,?)', (name, share))
                c.execute('INSERT INTO share_history(account_id,percent,loss_sharing,effective_at) VALUES(?,?,0,?)', (cur.lastrowid, share, now()))

def seed_demo_data():
    with db() as c:
        if c.execute('SELECT COUNT(*) FROM demo_stores').fetchone()[0]:
            return
        configs=[('North Star Goods','30','0.38','0.12','dropshipping',3),('Maple & Main','30','0.49','0.14','low_stock',4),('Coastal Finds','30','0.63','0.16','dropshipping',3),('Peak Home','60','0.84','0.18','low_stock',5)]
        stores=[]
        for name,share,cost_ratio,fee_ratio,mode,threshold in configs:
            store_id=c.execute('INSERT INTO demo_stores(name,currency,share,fulfilment_mode,low_stock_threshold) VALUES(?,\'GBP\',?,?,?)',(name,share,mode,threshold)).lastrowid
            stores.append((store_id,Decimal(cost_ratio),Decimal(fee_ratio)))
            c.execute('INSERT INTO demo_share_history(store_id,percent,loss_sharing,effective_at) VALUES(?,?,0,?)',(store_id,share,(datetime.now(timezone.utc)-timedelta(days=91)).isoformat()))
        today=datetime.now(timezone.utc)
        for store_id,cost_ratio,fee_ratio in stores:
            for i in range(45):
                day=today-timedelta(days=(i*2 + store_id) % 90)
                order_id=f'DEMO-{store_id:02d}-{1001+i:04d}'
                qty=1+i%2
                sku=f'SKU-{i%4+1}'
                proceeds=Decimal(34 + (i*7)%67 + store_id*5).quantize(CENT)
                refunds=Decimal('8.25') if i%11==0 else Decimal('0.00')
                fees=(proceeds*fee_ratio).quantize(CENT)
                tax=(proceeds*Decimal('0.20')).quantize(CENT)
                source={'lineItems':[{'sku':sku,'quantity':qty,'title':f'Sample item {i%4+1}'}],'demo':True}
                c.execute('INSERT INTO demo_orders(store_id,order_id,created_at,currency,proceeds,tax,refunds,fees,status,source_json) VALUES(?,?,?,?,?,?,?,?,?,?)',(store_id,order_id,day.isoformat(), 'GBP',str(proceeds),str(tax),str(refunds),str(fees),'PAID',json.dumps(source)))
                if i%9 != 0:
                    product=(proceeds*cost_ratio).quantize(CENT)
                    c.execute('INSERT INTO demo_costs(store_id,order_id,sku,kind,amount,currency,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(store_id,order_id,sku,'product',str(product),'GBP','Synthetic product cost',now()))
                fulfilment=(Decimal('3.50')+Decimal(qty)*Decimal('1.25')).quantize(CENT)
                c.execute('INSERT INTO demo_costs(store_id,order_id,sku,kind,amount,currency,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(store_id,order_id,sku,'fulfilment',str(fulfilment),'GBP','Synthetic fulfilment cost',now()))

def seed_demo_users():
    manager_password=os.getenv('DEMO_MANAGER_PASSWORD','')
    client_password=os.getenv('DEMO_CLIENT_PASSWORD','')
    if len(manager_password)<12 or len(client_password)<12:
        return False
    with db() as c:
        stores=[r['id'] for r in c.execute("SELECT id FROM demo_stores WHERE source='demo' ORDER BY id")]
        if len(stores)<4:
            raise RuntimeError('Demo stores are not initialized')
        credentials=[(os.getenv('DEMO_MANAGER_USERNAME','manager@demo.local'),'Demo Manager','manager',manager_password,stores),(os.getenv('DEMO_CLIENT_USERNAME','client@demo.local'),'Demo Client','client',client_password,[stores[0]])]
        for username,display,role,password,assigned in credentials:
            c.execute('INSERT INTO users(username,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?) ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name,password_hash=excluded.password_hash,role=excluded.role,active=1',(username,display,generate_password_hash(password,method='scrypt'),role,now()))
            user_id=c.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()['id']
            # Only reset synthetic-store assignments; live eBay store access is managed in the app and must survive restarts.
            c.execute("DELETE FROM demo_user_stores WHERE user_id=? AND store_id IN (SELECT id FROM demo_stores WHERE source='demo')",(user_id,))
            c.executemany('INSERT OR IGNORE INTO demo_user_stores(user_id,store_id) VALUES(?,?)',[(user_id,sid) for sid in assigned])
            if role=='manager':
                c.execute("INSERT OR IGNORE INTO demo_user_stores(user_id,store_id) SELECT ?,id FROM demo_stores WHERE source='ebay'",(user_id,))
    return True

def current_user():
    user_id=session.get('user_id')
    if not user_id:
        return None
    with db() as c:
        row=c.execute('SELECT id,username,display_name,role,active FROM users WHERE id=?',(user_id,)).fetchone()
    if not row or not row['active']:
        session.clear()
        return None
    return dict(row)

def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not current_user():
            if request.path.startswith('/api/') or request.path.endswith('.csv') or request.path.startswith('/oauth/'):
                return jsonify(error='Authentication required'),401
            return redirect(url_for('login'))
        return fn(*a, **kw)
    return wrapped

def manager_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*a, **kw):
        if current_user()['role']!='manager':
            return jsonify(error='Manager role required'),403
        return fn(*a, **kw)
    return wrapped

def allowed_store_ids(user_id):
    with db() as c:
        return [r['store_id'] for r in c.execute('SELECT store_id FROM demo_user_stores WHERE user_id=? ORDER BY store_id',(user_id,))]

def allowed_store(user, store_id):
    if store_id not in allowed_store_ids(user['id']):
        return False
    return True

def csrf_valid():
    token=request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
    return bool(token and secrets.compare_digest(token,session.get('csrf','')))

@app.before_request
def protect_post_requests():
    if request.method=='POST' and not csrf_valid():
        return jsonify(error='Invalid or missing CSRF token'),400

init_db()
seed_demo_data()
seed_demo_users()
def encryption():
    key = os.getenv('TOKEN_ENCRYPTION_KEY')
    return Fernet(key.encode()) if key else None
def encrypt(value):
    f = encryption()
    if not f: raise RuntimeError('Set TOKEN_ENCRYPTION_KEY before connecting eBay accounts')
    return f.encrypt(value.encode())
def decrypt(value): return encryption().decrypt(value).decode()
def ebay_base(): return 'https://api.sandbox.ebay.com' if os.getenv('EBAY_ENV','production') == 'sandbox' else 'https://api.ebay.com'
def oauth_host(): return 'https://auth.sandbox.ebay.com' if os.getenv('EBAY_ENV','production') == 'sandbox' else 'https://auth.ebay.com'

@app.get('/')
def home():
    if not current_user():
        return redirect(url_for('login'))
    session.setdefault('csrf',secrets.token_urlsafe(32))
    return render_template('index.html')

LOGIN_FAILS={};LOGIN_WINDOW=900;LOGIN_MAX=5
def client_ip():
    # Behind Cloudflare Tunnel every request arrives from localhost; trust its header only then.
    if request.remote_addr in ('127.0.0.1','::1') and request.headers.get('CF-Connecting-IP'):return request.headers['CF-Connecting-IP']
    return request.remote_addr or '?'
def too_many_failures(key):
    cutoff=time.time()-LOGIN_WINDOW;LOGIN_FAILS[key]=[t for t in LOGIN_FAILS.get(key,[]) if t>cutoff]
    return len(LOGIN_FAILS[key])>=LOGIN_MAX
def record_failure(key):LOGIN_FAILS.setdefault(key,[]).append(time.time())

@app.route('/login',methods=['GET','POST'])
def login():
    if current_user():
        return redirect(url_for('home'))
    session.setdefault('csrf',secrets.token_urlsafe(32))
    error=None
    if request.method=='POST':
        username=request.form.get('username','').strip()
        password=request.form.get('password','')
        keys=(('ip',client_ip()),('user',username.lower()))
        if any(too_many_failures(k) for k in keys):
            error='Too many failed sign-ins. Wait 15 minutes and try again.'
            return render_template('login.html',csrf=session['csrf'],configured=True,error=error),429
        with db() as c:
            row=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
        if row and check_password_hash(row['password_hash'],password):
            session.clear()
            session['user_id']=row['id']
            session['csrf']=secrets.token_urlsafe(32)
            session.permanent=True
            return redirect(url_for('home'))
        for k in keys:record_failure(k)
        error='Username or password is incorrect.'
    with db() as c:
        configured=c.execute('SELECT COUNT(*) FROM users WHERE active=1').fetchone()[0]>0
    return render_template('login.html',csrf=session['csrf'],configured=configured,error=error), (401 if error else 200)

@app.post('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

def live_mode():
    """True once any eBay store exists: live stores then replace the synthetic demo everywhere."""
    with db() as c:
        return bool(c.execute("SELECT 1 FROM demo_stores WHERE source='ebay' LIMIT 1").fetchone())

def visible_store_ids(user_id):
    ids=allowed_store_ids(user_id)
    if not ids or not live_mode():return ids
    with db() as c:
        live={r['id'] for r in c.execute("SELECT id FROM demo_stores WHERE source='ebay'")}
    return [i for i in ids if i in live]

def sku_defaults(costs):
    """Latest product/fulfilment cost per (store, SKU) from cost rows with no order ID.
    These apply per unit to every order of that SKU and count as a known product cost."""
    latest={}
    for row in sorted(costs,key=lambda r:r['id']):
        if row.get('order_id') or not row.get('sku') or row['kind'] not in ('product','fulfilment'):continue
        d=latest.setdefault((row['account_id'],row['sku']),{'account_id':row['account_id'],'sku':row['sku'],'product_cost':None,'fulfilment_cost':'0','currency':row['currency']})
        d[row['kind']+'_cost']=row['amount']
    return [d for d in latest.values() if d['product_cost'] is not None]

def sheet_estimates(orders,costs):
    """Per-unit cost of each (store, SKU) from its most recent single-item order in the cost sheet."""
    by_order={(o['account_id'],o['order_id']):o for o in orders}
    rows={}
    for x in costs:
        if x.get('source')=='sheet' and x['kind'] in ('product','fulfilment'):rows.setdefault((x['account_id'],x['order_id']),[]).append(x)
    out={}
    for key,xs in rows.items():
        o=by_order.get(key);lines=(o or {}).get('source_json',{}).get('lineItems',[])
        if not o or len(lines)!=1 or not lines[0].get('sku'):continue
        qty=Decimal(str(lines[0].get('quantity') or 1));sku=lines[0]['sku']
        prev=out.get((key[0],sku))
        if prev and prev['_at']>=o['created_at']:continue
        get=lambda k:sum((Decimal(x['amount']) for x in xs if x['kind']==k),Decimal(0))
        out[(key[0],sku)]={'account_id':key[0],'sku':sku,'product_cost':str(get('product')/qty),'fulfilment_cost':str(get('fulfilment')/qty),'currency':xs[0]['currency'],'_at':o['created_at'],'estimate':True}
    return out

def build_demo_state(user):
    ids=visible_store_ids(user['id']); live=live_mode()
    base={'user':{'name':user['display_name'],'role':user['role']},'dataset':'LIVE EBAY' if live else 'SYNTHETIC DEMO','live':live,'csrf':session.get('csrf')}
    if user['role']=='manager':base['ebay']=ebay_status()
    if not ids:
        if user['role']=='manager':base['clients']=client_access([])
        return {**base,'stores':[],'orders':[],'costs':[],'defaults':[],'history':[]}
    marks=','.join('?' for _ in ids)
    with db() as c:
        stores=[dict(r) for r in c.execute(f'SELECT id,name,currency,share,loss_sharing,fulfilment_mode,low_stock_threshold,source,ebay_user,connected,sync_at,sync_error FROM demo_stores WHERE id IN ({marks}) ORDER BY id',ids)]
        history=[dict(r) for r in c.execute(f'SELECT store_id AS account_id,percent,effective_at,loss_sharing FROM demo_share_history WHERE store_id IN ({marks}) ORDER BY effective_at,id',ids)]
        orders=[dict(r) for r in c.execute(f"SELECT id,store_id AS account_id,order_id,created_at,currency,proceeds,tax,refunds,fees,status,source_json FROM demo_orders WHERE store_id IN ({marks}) AND status!='CANCELLED' ORDER BY created_at DESC",ids)]
        costs=[dict(r) for r in c.execute(f'SELECT id,store_id AS account_id,order_id,sku,kind,amount,currency,note,source FROM demo_costs WHERE store_id IN ({marks})',ids)]
    defaults=sku_defaults(costs)
    # SKU product/fulfilment rows are applied per unit via defaults; keep only order-level and 'other' rows as direct costs.
    order_costs=[x for x in costs if x.get('order_id') or x['kind']=='other']
    has_order_product={(x['account_id'],x['order_id']) for x in costs if x.get('order_id') and x['kind']=='product'}
    fee_from_sheet={(x['account_id'],x['order_id']) for x in costs if x.get('source')=='sheet-fee'}
    for order in orders:order['source_json']=json.loads(order['source_json'])
    estimates=sheet_estimates(orders,costs)
    for order in orders:
        for k in ('proceeds','tax','refunds','fees'):
            if order[k]=='':order[k]=None  # unknown from eBay; never treated as zero in profit
        key=(order['account_id'],order['order_id'])
        if order['fees'] is None and key in fee_from_sheet:order['fees']='0.00'  # fee is carried as a sheet-derived 'other' cost
        # An order-level product cost (e.g. from the cost sheet) replaces per-item defaults so nothing is counted twice.
        if key in has_order_product:order['calculation']=calculate(order,order_costs,[],stores,history)
        else:
            # No actual cost yet: fall back to this item's latest cost-sheet cost, flagged as an estimate.
            have={(d['account_id'],d['sku']) for d in defaults}
            est=[estimates[(order['account_id'],l.get('sku'))] for l in order['source_json'].get('lineItems',[]) if (order['account_id'],l.get('sku')) not in have and (order['account_id'],l.get('sku')) in estimates]
            order['calculation']=calculate(order,order_costs,defaults+est,stores,history)
            order['calculation']['estimated']=bool(est) and order['calculation']['profit'] is not None
        order['demo']=not live
    with db() as c:
        listings=[dict(r) for r in c.execute(f'''SELECT l.store_id AS account_id,l.item_id,l.title,l.price,l.currency,l.quantity,l.quantity_available,l.quantity_sold,l.watch_count,l.has_variations,l.start_time,l.view_url,l.image_url,l.status,
            t.days AS traffic_days,t.impressions,t.views,t.transactions,t.ctr,t.conversion FROM demo_listings l LEFT JOIN demo_listing_traffic t ON t.store_id=l.store_id AND t.item_id=l.item_id WHERE l.store_id IN ({marks})''',ids)]
    out={**base,'stores':stores,'orders':orders,'costs':costs,'defaults':defaults,'history':[],'listings':listings}
    if user['role']=='manager':out['clients']=client_access(ids)
    return out

def client_access(store_ids):
    with db() as c:
        users=[dict(r) for r in c.execute("SELECT id,username,display_name FROM users WHERE role='client' AND active=1 ORDER BY username")]
        for u in users:
            u['store_ids']=[r['store_id'] for r in c.execute('SELECT store_id FROM demo_user_stores WHERE user_id=?',(u['id'],)) if r['store_id'] in store_ids]
    return users

@app.post('/api/client-access')
@manager_required
def set_client_access():
    d=request.get_json(force=True); user=current_user()
    try:client_id=int(d.get('client_id'));store_id=int(d.get('store_id'))
    except (TypeError,ValueError):return jsonify(error='Choose a client and a store'),400
    if not allowed_store(user,store_id):return jsonify(error='Store not found'),404
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=? AND role='client'",(client_id,)).fetchone():return jsonify(error='Client not found'),404
        if d.get('grant'):c.execute('INSERT OR IGNORE INTO demo_user_stores(user_id,store_id) VALUES(?,?)',(client_id,store_id))
        else:c.execute('DELETE FROM demo_user_stores WHERE user_id=? AND store_id=?',(client_id,store_id))
    return jsonify(ok=True)

@app.get('/api/state')
@login_required
def state():
    return jsonify(build_demo_state(current_user()))

@app.post('/api/stock-rules')
@manager_required
def set_stock_rule():
    d=request.get_json(force=True); user=current_user()
    try:store_id=int(d.get('store_id',d.get('account_id'))); threshold=int(d.get('threshold'))
    except (TypeError,ValueError):return jsonify(error='Store, SKU and whole-number threshold are required'),400
    sku=str(d.get('sku','')).strip()
    if not allowed_store(user,store_id):return jsonify(error='Store not found'),404
    if not sku or len(sku)>120 or threshold<0:return jsonify(error='SKU is required and threshold must be zero or higher'),400
    with db() as c:c.execute('INSERT INTO demo_stock_rules(store_id,sku,threshold,updated_at) VALUES(?,?,?,?) ON CONFLICT(store_id,sku) DO UPDATE SET threshold=excluded.threshold,updated_at=excluded.updated_at',(store_id,sku,threshold,now()))
    return jsonify(ok=True)

@app.post('/api/stores/<int:aid>')
@manager_required
def edit_account(aid):
    user=current_user()
    if not allowed_store(user,aid):return jsonify(error='Store not found'),404
    d=request.get_json(force=True)
    name=str(d.get('name','')).strip()
    if not name or len(name)>80:return jsonify(error='Store name must be 1 to 80 characters'),400
    mode=d.get('fulfilment_mode','dropshipping')
    loss_sharing=1 if str(d.get('loss_sharing','0')) in ('1','true','True') else 0
    try:
        threshold=int(d.get('low_stock_threshold',3)); share=Decimal(str(d.get('share','0')))
        effective_date=datetime.fromisoformat(str(d.get('effective_date') or datetime.now(timezone.utc).date().isoformat())).date()
    except (TypeError,ValueError,InvalidOperation):return jsonify(error='Enter a valid share, effective date, and whole-number threshold'),400
    if mode not in ('dropshipping','low_stock') or threshold<0 or not share.is_finite() or share<0 or share>100:return jsonify(error='Choose a stock model, threshold of zero or more, and share from 0 to 100'),400
    effective_at=effective_date.isoformat()+'T00:00:00+00:00'
    with db() as c:
        c.execute('UPDATE demo_stores SET name=?,fulfilment_mode=?,low_stock_threshold=?,share=?,loss_sharing=? WHERE id=?',(name,mode,threshold,str(share),loss_sharing,aid))
        c.execute('INSERT INTO demo_share_history(store_id,percent,loss_sharing,effective_at) VALUES(?,?,?,?) ON CONFLICT(store_id,effective_at) DO UPDATE SET percent=excluded.percent,loss_sharing=excluded.loss_sharing',(aid,str(share),loss_sharing,effective_at))
    return jsonify(ok=True)

@app.post('/api/costs')
@manager_required
def add_cost():
    d=request.get_json(force=True); user=current_user()
    try:store_id=int(d.get('store_id',d.get('account_id')))
    except (TypeError,ValueError):return jsonify(error='Select a store'),400
    if not allowed_store(user,store_id):return jsonify(error='Store not found'),404
    try:
        parsed=Decimal(str(d.get('amount')))
        if not parsed.is_finite():raise InvalidOperation
        amount=money(parsed)
    except (InvalidOperation,ValueError,TypeError):return jsonify(error='Enter a valid cost amount'),400
    if Decimal(amount)<0:return jsonify(error='Costs must be zero or positive'),400
    kind=d.get('kind','product')
    if kind not in ('product','fulfilment','other'):return jsonify(error='Unsupported cost type'),400
    currency=str(d.get('currency','')).upper()
    if len(currency)!=3 or not currency.isalpha():return jsonify(error='Currency must be a three-letter code'),400
    order_id=str(d.get('order_id') or '').strip() or None
    sku=str(d.get('sku') or '').strip() or None
    if not order_id and not sku:return jsonify(error='Enter an order ID or SKU'),400
    with db() as c:
        if order_id and not c.execute('SELECT 1 FROM demo_orders WHERE store_id=? AND order_id=?',(store_id,order_id)).fetchone():return jsonify(error='Order not found in this store'),404
        c.execute('INSERT INTO demo_costs(store_id,order_id,sku,kind,amount,currency,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(store_id,order_id,sku,kind,amount,currency,str(d.get('note',''))[:300],now()))
    return jsonify(ok=True)

@app.post('/api/default-cost')
@manager_required
def default_cost():
    return jsonify(error='SKU defaults are not part of the current demo. Add an order cost instead.'),501

@app.post('/api/import-costs')
@manager_required
def import_costs():
    if 'file' not in request.files:return jsonify(error='Choose a CSV file'),400
    try:stream=io.StringIO(request.files['file'].stream.read().decode('utf-8-sig'))
    except UnicodeDecodeError:return jsonify(error='CSV must be UTF-8 encoded'),400
    reader=csv.DictReader(stream); rows=[]; user=current_user(); allowed=set(allowed_store_ids(user['id']))
    try:
        for row in reader:
            store_id=int(row.get('store_id') or row.get('account_id') or '')
            if store_id not in allowed:return jsonify(error='CSV contains a store you cannot manage'),404
            kind=row.get('kind','product')
            if kind not in ('product','fulfilment','other'):return jsonify(error='CSV contains an unsupported cost type'),400
            parsed=Decimal(str(row['amount']))
            if not parsed.is_finite():raise InvalidOperation
            amount=money(parsed)
            if Decimal(amount)<0:return jsonify(error='Costs must be zero or positive'),400
            currency=str(row['currency']).upper()
            if len(currency)!=3 or not currency.isalpha():return jsonify(error='Currency must be a three-letter code'),400
            order_id=str(row.get('order_id') or '').strip() or None
            sku=str(row.get('sku') or '').strip() or None
            if not order_id and not sku:return jsonify(error='Each row must include an order ID or SKU'),400
            rows.append((store_id,order_id,sku,kind,amount,currency,str(row.get('note','CSV import'))[:300]))
    except (TypeError,ValueError,KeyError,InvalidOperation):return jsonify(error='CSV row is invalid; expected store_id, order_id, sku, kind, amount, currency, note'),400
    if len(rows)>5000:return jsonify(error='CSV is limited to 5,000 cost rows'),400
    with db() as c:
        for store_id,order_id,sku,kind,amount,currency,note in rows:
            if order_id and not c.execute('SELECT 1 FROM demo_orders WHERE store_id=? AND order_id=?',(store_id,order_id)).fetchone():return jsonify(error='CSV order ID does not belong to the selected store'),404
        c.executemany('INSERT INTO demo_costs(store_id,order_id,sku,kind,amount,currency,note,created_at) VALUES(?,?,?,?,?,?,?,?)',[(sid,oid,sku,kind,amount,currency,note,now()) for sid,oid,sku,kind,amount,currency,note in rows])
    return jsonify(imported=len(rows))

@app.get('/api/export/<kind>.csv')
@login_required
def export(kind):
    if kind not in ('orders','profit'):return 'Unknown export',404
    user=current_user(); state_data=build_demo_state(user); accounts=state_data['stores']; allowed={a['id'] for a in accounts}
    requested=request.args.get('store_id',request.args.get('account_id'))
    if requested and requested!='all':
        try:store_id=int(requested)
        except ValueError:return jsonify(error='Invalid store selector'),400
        if store_id not in allowed:return jsonify(error='Store not found'),404
        allowed={store_id}
    try:orders=filter_export_orders(state_data['orders'],allowed)
    except ValueError:return jsonify(error='Invalid or reversed date range'),400
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['store','order_id','created_at','currency','sales','marketplace_tax','refunds','fees','known_costs','estimated_profit','share_percent','my_share','missing_costs'])
    for o in orders:
        calc=o['calculation']
        w.writerow([calc['account'],o['order_id'],o['created_at'],o['currency'],o['proceeds'],o['tax'],o['refunds'],o['fees'],money(Decimal(calc['product'])+Decimal(calc['fulfilment'])+Decimal(calc['other'])),calc['profit'],calc['share'],calc['my_share'],calc['missing']])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="{kind}.csv"'})

def filter_export_orders(orders,allowed_ids):
    start=request.args.get('from'); end=request.args.get('to'); query=request.args.get('q','').strip().lower()
    try:
        after=datetime.fromisoformat(start).date() if start else None
        before=datetime.fromisoformat(end).date() if end else None
    except ValueError:
        raise ValueError('Invalid date')
    if after and before and after>before:raise ValueError('Reversed date range')
    selected=[]
    for order in orders:
        date=datetime.fromisoformat(order['created_at'].replace('Z','+00:00')).date()
        if order['account_id'] not in allowed_ids or (after and date<after) or (before and date>before):continue
        if query and query not in order['order_id'].lower() and query not in json.dumps(order['source_json']).lower():continue
        selected.append(order)
    return selected

def calculate(o,costs,defaults,accounts,history):
    account=next((a for a in accounts if a['id']==o['account_id']),{})
    exact=[c for c in costs if c['account_id']==o['account_id'] and c['order_id']==o['order_id']]
    # SKU-level rows are defaults (no order_id); never apply another order's
    # actual cost to this order just because the SKU matches.
    sku_rows=[c for c in costs if c['account_id']==o['account_id'] and not c.get('order_id') and c['sku'] and c['sku'] in [i.get('sku') for i in o.get('source_json',{}).get('lineItems',[])]]
    costrows=exact+sku_rows
    for line in o.get('source_json',{}).get('lineItems',[]):
        d=next((x for x in defaults if x['account_id']==o['account_id'] and x['sku']==line.get('sku')),None)
        if d:
            qty=int(line.get('quantity',1))
            costrows += [{'kind':'product','amount':str(Decimal(d['product_cost'])*qty),'currency':d['currency']},{'kind':'fulfilment','amount':str(Decimal(d['fulfilment_cost'])*qty),'currency':d['currency']}]
    sums={'product':Decimal(0),'fulfilment':Decimal(0),'other':Decimal(0)}
    seen=set()
    for x in costrows:
        key=(x.get('id'),x.get('kind'),x.get('amount'),x.get('sku'))
        if key in seen:continue
        seen.add(key); kind=x['kind']; bucket=kind if kind in ('product','fulfilment') else 'other'
        if x.get('currency')==o.get('currency'): sums[bucket]+=Decimal(x['amount'])
    fees=Decimal(o.get('fees') or '0'); refunds=Decimal(o.get('refunds') or '0'); proceeds=Decimal(o.get('proceeds') or '0')
    # Missing SKU product cost remains unknown; never assume zero.
    lines=o.get('source_json',{}).get('lineItems',[]); missing=any(not any(d['account_id']==o['account_id'] and d['sku']==l.get('sku') for d in defaults) and not any(c['account_id']==o['account_id'] and c.get('order_id')==o['order_id'] and c.get('sku')==l.get('sku') and c['kind']=='product' for c in costs) for l in lines)
    missing=missing or o.get('fees') is None or o.get('refunds') is None or o.get('proceeds') is None
    profit=proceeds-refunds-fees-sums['product']-sums['fulfilment']-sums['other']
    if missing: profit=None
    account_history=[h for h in history if h['account_id']==o['account_id']]
    shares=[h for h in account_history if h['effective_at']<= (o.get('created_at') or '')]
    share_record=shares[-1] if shares else None
    share=share_record['percent'] if share_record else (account.get('share','0') if not account_history else None)
    loss_sharing=share_record.get('loss_sharing',account.get('loss_sharing',0)) if share_record else account.get('loss_sharing',0)
    if share is None: missing=True
    mine=Decimal(0) if profit is None or share is None else profit*Decimal(share)/100
    if profit is not None and profit<0 and not loss_sharing: mine=Decimal(0)
    return {'account':account.get('name','Unknown'),'product':money(sums['product']),'fulfilment':money(sums['fulfilment']),'other':money(sums['other']),'profit':money(profit) if not missing else None,'share':share,'my_share':money(mine) if not missing else None,'missing':missing}

# ---------------------------------------------------------------- live eBay
# Read-only seller integration. Tokens are Fernet-encrypted at rest (TOKEN_ENCRYPTION_KEY).
# Orders come from the Fulfillment API, which includes marketplace fees and refunds per order,
# so UK sellers don't need the Finances API (that one requires digital-signature keys in the EU/UK).
SYNC_LOCK=threading.Lock()

def apiz_base(): return 'https://apiz.sandbox.ebay.com' if os.getenv('EBAY_ENV','production')=='sandbox' else 'https://apiz.ebay.com'

def ebay_config_missing():
    need=['EBAY_CLIENT_ID','EBAY_CLIENT_SECRET','EBAY_RUNAME','TOKEN_ENCRYPTION_KEY']
    return [k for k in need if not os.getenv(k)]

def ebay_status():
    with db() as c:
        stores=[dict(r) for r in c.execute("SELECT id,name,ebay_user,connected,sync_at,sync_error,refresh_expires,cost_sheet_url,cost_sheet_at,cost_sheet_report,(SELECT COUNT(*) FROM demo_orders o WHERE o.store_id=s.id) AS orders FROM demo_stores s WHERE source='ebay' ORDER BY id")]
    return {'configured':not ebay_config_missing(),'missing':ebay_config_missing(),'env':os.getenv('EBAY_ENV','production'),'stores':stores}

def basic_auth():
    return requests.auth._basic_auth_str(os.getenv('EBAY_CLIENT_ID',''),os.getenv('EBAY_CLIENT_SECRET',''))

def token_request(data):
    r=requests.post(ebay_base()+'/identity/v1/oauth2/token',headers={'Authorization':basic_auth(),'Content-Type':'application/x-www-form-urlencoded'},data=data,timeout=30)
    if r.status_code>=400:
        try:detail=r.json().get('error_description') or r.json().get('error')
        except ValueError:detail=r.text[:200]
        raise RuntimeError(f'eBay token request failed ({r.status_code}): {detail}')
    return r.json()

def token_for(store_id):
    with db() as c:a=c.execute('SELECT token,token_expires,refresh_token FROM demo_stores WHERE id=?',(store_id,)).fetchone()
    if not a or not a['refresh_token']:raise RuntimeError('Store is not connected to eBay. Reconnect it.')
    expires=datetime.fromisoformat(a['token_expires']) if a['token_expires'] else datetime.now(timezone.utc)
    if a['token'] and expires>datetime.now(timezone.utc)+timedelta(minutes=2):return decrypt(a['token'])
    payload=token_request({'grant_type':'refresh_token','refresh_token':decrypt(a['refresh_token']),'scope':' '.join(SCOPES)})
    with db() as c:c.execute('UPDATE demo_stores SET token=?,token_expires=? WHERE id=?',(encrypt(payload['access_token']),(datetime.now(timezone.utc)+timedelta(seconds=payload['expires_in'])).isoformat(),store_id))
    return payload['access_token']

def ebay_get(store_id,url,params=None):
    token=token_for(store_id)
    for attempt in range(4):
        r=requests.get(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json','X-EBAY-C-MARKETPLACE-ID':os.getenv('EBAY_MARKETPLACE','EBAY_GB')},params=params,timeout=45)
        if r.status_code==429 or r.status_code>=500:
            time.sleep(min(2**attempt,12));continue
        if r.status_code==401 and attempt==0:
            with db() as c:c.execute('UPDATE demo_stores SET token=NULL,token_expires=NULL WHERE id=?',(store_id,))
            token=token_for(store_id);continue
        if r.status_code>=400:
            try:errs=r.json().get('errors',[]);detail='; '.join(e.get('longMessage') or e.get('message','') for e in errs)
            except ValueError:detail=r.text[:200]
            raise RuntimeError(f'eBay API {r.status_code} on {url.split(".com")[-1]}: {detail}'[:400])
        return r.json() if r.content else {}
    raise RuntimeError('eBay API kept failing (rate limit or server error); try again later')

def dec(amount):
    if not amount or amount.get('value') in (None,''):return None
    try:return Decimal(str(amount['value']))
    except InvalidOperation:return None

def map_order(o):
    """Map an eBay Fulfillment API order to dashboard fields. Unknown values become '' (never zero)."""
    ps=o.get('pricingSummary',{})
    subtotal=dec(ps.get('priceSubtotal'))
    if subtotal is None:proceeds=None
    else:
        # deliveryDiscount and priceDiscount are negative values on eBay.
        proceeds=subtotal+(dec(ps.get('deliveryCost')) or 0)+(dec(ps.get('deliveryDiscount')) or 0)+(dec(ps.get('priceDiscount')) or 0)
    currency=(ps.get('priceSubtotal') or ps.get('total') or {}).get('currency') or 'GBP'
    taxes=[dec(t.get('amount')) for li in o.get('lineItems',[]) for t in li.get('ebayCollectAndRemitTaxes',[])]
    tax=sum((t for t in taxes if t is not None),Decimal(0)) if taxes else (dec(ps.get('tax')) or Decimal(0))
    fees=dec(o.get('totalMarketplaceFee'))
    refunds=sum((dec(r.get('amount')) or Decimal(0) for r in o.get('paymentSummary',{}).get('refunds',[]) if r.get('refundStatus')=='REFUNDED'),Decimal(0))
    cancelled=(o.get('cancelStatus') or {}).get('cancelState')=='CANCELED'
    lines=[{'sku':li.get('sku') or f"ITEM-{li.get('legacyItemId','unknown')}",'quantity':int(li.get('quantity') or 1),'title':li.get('title'),'legacyItemId':li.get('legacyItemId')} for li in o.get('lineItems',[])]
    source={'lineItems':lines,'fulfillmentStatus':o.get('orderFulfillmentStatus'),'paymentStatus':o.get('orderPaymentStatus'),'ebay':True}
    return {'order_id':o['orderId'],'created_at':o.get('creationDate'),'currency':currency,
            'proceeds':money(proceeds) if proceeds is not None else '','tax':money(tax),'refunds':money(refunds),
            'fees':money(fees) if fees is not None else '','status':'CANCELLED' if cancelled else (o.get('orderPaymentStatus') or 'UNKNOWN'),'source_json':json.dumps(source)}

def sync_store(store_id):
    """Incremental order sync. First run pulls EBAY_SYNC_DAYS (default 90) of history."""
    with SYNC_LOCK:
        with db() as c:st=c.execute("SELECT * FROM demo_stores WHERE id=? AND source='ebay'",(store_id,)).fetchone()
        if not st:raise RuntimeError('Store not found')
        started=datetime.now(timezone.utc)
        days=max(1,min(int(os.getenv('EBAY_SYNC_DAYS','90')),730))
        since=st['sync_cursor'] or (started-timedelta(days=days)).isoformat(timespec='milliseconds').replace('+00:00','Z')
        try:
            offset=0;total=0
            while True:
                page=ebay_get(store_id,ebay_base()+'/sell/fulfillment/v1/order',{'filter':f'lastmodifieddate:[{since}..]','limit':200,'offset':offset})
                orders=page.get('orders',[])
                with db() as c:
                    for o in orders:
                        m=map_order(o)
                        c.execute("""INSERT INTO demo_orders(store_id,order_id,created_at,currency,proceeds,tax,refunds,fees,status,source_json) VALUES(?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(store_id,order_id) DO UPDATE SET created_at=excluded.created_at,currency=excluded.currency,proceeds=excluded.proceeds,tax=excluded.tax,refunds=excluded.refunds,fees=excluded.fees,status=excluded.status,source_json=excluded.source_json""",
                          (store_id,m['order_id'],m['created_at'],m['currency'],m['proceeds'],m['tax'],m['refunds'],m['fees'],m['status'],m['source_json']))
                total+=len(orders)
                if len(orders)<200 or offset+200>=int(page.get('total',0) or 0):break
                offset+=200
            # Overlap the next window by 5 minutes so late-indexed changes aren't missed.
            cursor=(started-timedelta(minutes=5)).isoformat(timespec='milliseconds').replace('+00:00','Z')
            with db() as c:c.execute('UPDATE demo_stores SET sync_at=?,sync_cursor=?,sync_error=NULL WHERE id=?',(now(),cursor,store_id))
            problems=[]
            for label,fn in (('Listings',sync_listings),('Traffic',sync_traffic),('Cost sheet',fetch_and_import_sheet)):
                try:fn(store_id)
                except Exception as e:problems.append(f'{label}: {str(e)[:200]}')
            if problems:
                with db() as c:c.execute('UPDATE demo_stores SET sync_error=? WHERE id=?',('; '.join(problems),store_id))
            return total
        except Exception as e:
            with db() as c:c.execute('UPDATE demo_stores SET sync_error=? WHERE id=?',(str(e)[:400],store_id))
            raise

def sync_all():
    with db() as c:ids=[r['id'] for r in c.execute("SELECT id FROM demo_stores WHERE source='ebay' AND connected=1")]
    for sid in ids:
        try:sync_store(sid)
        except Exception as e:app.logger.warning('eBay sync failed for store %s: %s',sid,e)

def start_auto_sync():
    minutes=int(os.getenv('EBAY_AUTO_SYNC_MINUTES','30') or 0)
    if minutes<=0 or ebay_config_missing():return
    def loop():
        while True:
            sync_all();time.sleep(minutes*60)
    threading.Thread(target=loop,daemon=True,name='ebay-auto-sync').start()

# ---------------------------------------------------------------- listings
TRADING_NS='{urn:ebay:apis:eBLBaseComponents}'
TRAFFIC_DAYS=30

def trading_call(store_id,call,body):
    xml=f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body}</{call}Request>'
    trading='https://api.sandbox.ebay.com/ws/api.dll' if os.getenv('EBAY_ENV','production')=='sandbox' else 'https://api.ebay.com/ws/api.dll'
    r=requests.post(trading,data=xml.encode(),headers={'X-EBAY-API-IAF-TOKEN':token_for(store_id),'X-EBAY-API-CALL-NAME':call,'X-EBAY-API-SITEID':'3','X-EBAY-API-COMPATIBILITY-LEVEL':'1225','Content-Type':'text/xml'},timeout=60)
    root=ET.fromstring(r.content)
    if root.findtext(TRADING_NS+'Ack') not in ('Success','Warning'):
        msg=root.findtext(f'.//{TRADING_NS}Errors/{TRADING_NS}LongMessage') or f'HTTP {r.status_code}'
        raise RuntimeError(f'eBay {call}: {msg}'[:300])
    return root

def parse_listing(item):
    t=lambda path:item.findtext('/'.join(TRADING_NS+p for p in path.split('/')))
    price_el=item.find(f'{TRADING_NS}SellingStatus/{TRADING_NS}CurrentPrice')
    variations=item.findall(f'{TRADING_NS}Variations/{TRADING_NS}Variation')
    prices=[Decimal(v.findtext(TRADING_NS+'StartPrice')) for v in variations if v.findtext(TRADING_NS+'StartPrice')]
    price=price_el.text if price_el is not None else (str(min(prices)) if prices else None)
    currency=price_el.get('currencyID') if price_el is not None else 'GBP'
    as_int=lambda v:int(v) if v not in (None,'') else None
    qty=as_int(t('Quantity'));sold=as_int(t('SellingStatus/QuantitySold'))
    avail=as_int(t('QuantityAvailable'))
    if variations:
        qty=sum(as_int(v.findtext(TRADING_NS+'Quantity')) or 0 for v in variations)
        sold=sum(as_int(v.findtext(f'{TRADING_NS}SellingStatus/{TRADING_NS}QuantitySold')) or 0 for v in variations)
        avail=qty-sold
    if avail is None and qty is not None:avail=qty-(sold or 0)
    return {'item_id':t('ItemID'),'title':t('Title'),'price':money(price) if price else None,'currency':currency,'quantity':qty,'quantity_available':avail,'quantity_sold':sold,
            'watch_count':as_int(t('WatchCount')) or 0,'has_variations':1 if variations else 0,'start_time':t('ListingDetails/StartTime'),'view_url':t('ListingDetails/ViewItemURL'),'image_url':t('PictureDetails/GalleryURL')}

def sync_listings(store_id):
    items=[];page=1
    while True:
        root=trading_call(store_id,'GetMyeBaySelling',f'<ActiveList><Include>true</Include><Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination></ActiveList><DetailLevel>ReturnAll</DetailLevel>')
        batch=root.findall(f'.//{TRADING_NS}ActiveList/{TRADING_NS}ItemArray/{TRADING_NS}Item')
        items+=[parse_listing(i) for i in batch]
        pages=int(root.findtext(f'.//{TRADING_NS}ActiveList/{TRADING_NS}PaginationResult/{TRADING_NS}TotalNumberOfPages') or 1)
        if page>=pages or not batch:break
        page+=1
    ts=now()
    with db() as c:
        # Anything no longer in the active list has ended (or sold out without auto-relist).
        c.execute("UPDATE demo_listings SET status='ENDED' WHERE store_id=?",(store_id,))
        for x in items:
            if not x['item_id']:continue
            c.execute("""INSERT INTO demo_listings(store_id,item_id,title,price,currency,quantity,quantity_available,quantity_sold,watch_count,has_variations,start_time,view_url,image_url,status,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?) ON CONFLICT(store_id,item_id) DO UPDATE SET title=excluded.title,price=excluded.price,currency=excluded.currency,quantity=excluded.quantity,
              quantity_available=excluded.quantity_available,quantity_sold=excluded.quantity_sold,watch_count=excluded.watch_count,has_variations=excluded.has_variations,start_time=excluded.start_time,
              view_url=excluded.view_url,image_url=excluded.image_url,status='ACTIVE',updated_at=excluded.updated_at""",
              (store_id,x['item_id'],x['title'],x['price'],x['currency'],x['quantity'],x['quantity_available'],x['quantity_sold'],x['watch_count'],x['has_variations'],x['start_time'],x['view_url'],x['image_url'],ts))
    return len(items)

def sync_traffic(store_id):
    end=datetime.now(timezone.utc).date();start=end-timedelta(days=TRAFFIC_DAYS-1)
    market=os.getenv('EBAY_MARKETPLACE','EBAY_GB')
    d=ebay_get(store_id,ebay_base()+'/sell/analytics/v1/traffic_report',{'dimension':'LISTING','filter':f'marketplace_ids:{{{market}}},date_range:[{start:%Y%m%d}..{end:%Y%m%d}]','metric':'LISTING_IMPRESSION_TOTAL,LISTING_VIEWS_TOTAL,TRANSACTION,CLICK_THROUGH_RATE,SALES_CONVERSION_RATE'})
    keys=[m.get('key') for m in d.get('header',{}).get('metrics',[])]
    ts=now()
    with db() as c:
        c.execute('DELETE FROM demo_listing_traffic WHERE store_id=?',(store_id,))
        for rec in d.get('records',[]):
            item=(rec.get('dimensionValues') or [{}])[0].get('value')
            vals={k:(v.get('value') if v.get('applicable',True) else None) for k,v in zip(keys,rec.get('metricValues',[]))}
            if not item:continue
            c.execute('INSERT INTO demo_listing_traffic(store_id,item_id,days,impressions,views,transactions,ctr,conversion,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
              (store_id,str(item),TRAFFIC_DAYS,vals.get('LISTING_IMPRESSION_TOTAL'),vals.get('LISTING_VIEWS_TOTAL'),vals.get('TRANSACTION'),vals.get('CLICK_THROUGH_RATE'),vals.get('SALES_CONVERSION_RATE'),ts))
    return len(d.get('records',[]))

# ---------------------------------------------------------------- cost sheet import
# A per-store Google Sheet (supplier order log) gives exact per-order costs. Columns used:
# 'Ebay Order ID', 'Sourcing Net Cost', 'Shipping Cost', 'Prep Cost', 'Ebay Net Payout', 'Status'.
SHEET_COLS={'order':'ebay order id','sourcing':'sourcing net cost','shipping':'shipping cost','prep':'prep cost','payout':'ebay net payout','status':'status'}

def sheet_csv_url(url):
    u=urlparse(url.strip())
    if u.scheme!='https' or u.netloc!='docs.google.com' or '/spreadsheets/d/' not in u.path:
        raise ValueError('Use a Google Sheets link (https://docs.google.com/spreadsheets/d/…)')
    sheet_id=u.path.split('/spreadsheets/d/')[1].split('/')[0]
    gid=(parse_qs(u.query).get('gid') or parse_qs(u.fragment).get('gid') or ['0'])[0]
    return f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}'

def sheet_money(v):
    v=(v or '').strip().replace('£','').replace(',','').replace(' ','')
    if not v or v.startswith('#'):return None
    try:
        d=Decimal(v)
        return d if d.is_finite() else None
    except InvalidOperation:return None

def parse_cost_sheet(text):
    """Return [{order, sourcing, shipping, prep, payout, status}] from the sheet CSV.
    Header names may be split over the first rows (totals row above labels), so search the first 5 rows."""
    rows=list(csv.reader(io.StringIO(text)))
    cols={}
    for r in rows[:5]:
        for i,name in enumerate(r):
            n=' '.join(name.lower().split())
            for key,label in SHEET_COLS.items():
                if n==label and key not in cols:cols[key]=i
    if 'order' not in cols or 'sourcing' not in cols:
        raise ValueError('Sheet needs "Ebay Order ID" and "Sourcing Net Cost" columns')
    get=lambda r,k:r[cols[k]] if k in cols and cols[k]<len(r) else ''
    out=[]
    for r in rows:
        oid=get(r,'order').strip()
        if not oid or oid.lower()=='ebay order id':continue
        out.append({'order':oid,'sourcing':sheet_money(get(r,'sourcing')),'shipping':sheet_money(get(r,'shipping')),'prep':sheet_money(get(r,'prep')),'payout':sheet_money(get(r,'payout')),'status':get(r,'status').strip()})
    return out

def import_cost_sheet(store_id,text):
    rows=parse_cost_sheet(text)
    with db() as c:
        orders={r['order_id']:dict(r) for r in c.execute('SELECT order_id,proceeds,fees,refunds,status,source_json FROM demo_orders WHERE store_id=?',(store_id,))}
        c.execute("DELETE FROM demo_costs WHERE store_id=? AND source IN ('sheet','sheet-fee')",(store_id,))
        rep={'rows':len(rows),'imported':0,'unmatched':[],'cancelled':0,'no_cost':0,'extra_charges':'0.00','payout_higher':0}
        extra_total=Decimal(0); ts=now()
        add=lambda oid,sku,kind,amount,note,src='sheet':c.execute("INSERT INTO demo_costs(store_id,order_id,sku,kind,amount,currency,note,created_at,source) VALUES(?,?,?,?,?,'GBP',?,?,?)",(store_id,oid,sku,kind,money(amount),note,ts,src))
        for r in rows:
            o=orders.get(r['order'])
            if not o:rep['unmatched'].append(r['order']);continue
            if r['status'].lower()=='cancelled':rep['cancelled']+=1;continue
            if r['sourcing'] is None:rep['no_cost']+=1;continue
            skus=[l.get('sku') for l in json.loads(o['source_json']).get('lineItems',[])] or [None]
            # Full sourcing cost on the first line; zero rows mark the other lines' costs as known.
            for i,sku in enumerate(dict.fromkeys(skus)):add(r['order'],sku,'product',r['sourcing'] if i==0 else 0,'Cost sheet: sourcing')
            ship=(r['shipping'] or 0)+(r['prep'] or 0)
            if ship:add(r['order'],skus[0],'fulfilment',ship,'Cost sheet: shipping + prep')
            # Reconcile to the sheet's net payout: eBay deducts ad fees/labels outside the order's marketplace fee.
            if r['payout'] is not None and o['proceeds'] not in ('',None):
                proceeds=Decimal(o['proceeds']);refunds=Decimal(o['refunds'] or '0')
                if o['fees'] in ('',None):
                    add(r['order'],None,'other',proceeds-refunds-r['payout'],'Cost sheet: eBay fees (from net payout)','sheet-fee')
                else:
                    extra=proceeds-refunds-Decimal(o['fees'])-r['payout']
                    if extra>Decimal('0.005'):add(r['order'],None,'other',extra,'Cost sheet: other eBay charges (ads/labels)');extra_total+=extra
                    elif extra<Decimal('-0.005'):rep['payout_higher']+=1
            rep['imported']+=1
        rep['extra_charges']=money(extra_total);rep['unmatched']=rep['unmatched'][:20]
        c.execute('UPDATE demo_stores SET cost_sheet_at=?,cost_sheet_report=? WHERE id=?',(ts,json.dumps(rep),store_id))
    return rep

def fetch_and_import_sheet(store_id):
    with db() as c:st=c.execute('SELECT cost_sheet_url FROM demo_stores WHERE id=?',(store_id,)).fetchone()
    if not st or not st['cost_sheet_url']:return None
    r=requests.get(sheet_csv_url(st['cost_sheet_url']),timeout=30)
    if r.status_code!=200 or 'text/csv' not in r.headers.get('content-type',''):
        raise RuntimeError('Could not read the Google Sheet. Share it as "Anyone with the link can view".')
    return import_cost_sheet(store_id,r.content.decode('utf-8-sig'))

@app.post('/api/stores/<int:aid>/cost-sheet')
@manager_required
def set_cost_sheet(aid):
    if not allowed_store(current_user(),aid):return jsonify(error='Store not found'),404
    url=str(request.get_json(force=True).get('url','')).strip()
    if url:
        try:sheet_csv_url(url)
        except ValueError as e:return jsonify(error=str(e)),400
    with db() as c:c.execute('UPDATE demo_stores SET cost_sheet_url=? WHERE id=?',(url or None,aid))
    if not url:
        with db() as c:c.execute("DELETE FROM demo_costs WHERE store_id=? AND source IN ('sheet','sheet-fee')",(aid,))
        return jsonify(ok=True,removed=True)
    try:report=fetch_and_import_sheet(aid)
    except Exception as e:return jsonify(error=str(e)[:300]),502
    return jsonify(ok=True,report=report)

@app.get('/oauth/connect')
@manager_required
def oauth_connect():
    missing=ebay_config_missing()
    if missing:return jsonify(error='Set these in .env and restart: '+', '.join(missing)),400
    session['oauth_state']=secrets.token_urlsafe(24)
    # prompt=login forces the eBay sign-in screen so a different seller account can be connected each time.
    q={'client_id':os.getenv('EBAY_CLIENT_ID'),'redirect_uri':os.getenv('EBAY_RUNAME'),'response_type':'code','scope':' '.join(SCOPES),'state':session['oauth_state'],'prompt':'login'}
    return redirect(oauth_host()+'/oauth2/authorize?'+urlencode(q))

def complete_oauth(code,state):
    if not code:raise ValueError('eBay did not return an authorization code (consent was declined or the link is incomplete).')
    if not state or not secrets.compare_digest(state,session.pop('oauth_state','') or '-'):raise ValueError('This sign-in link is stale. Click "Connect eBay store" again.')
    payload=token_request({'grant_type':'authorization_code','code':code,'redirect_uri':os.getenv('EBAY_RUNAME')})
    access=payload['access_token']
    try:
        who=requests.get(apiz_base()+'/commerce/identity/v1/user/',headers={'Authorization':'Bearer '+access},timeout=30)
        username=who.json().get('username') if who.ok else None
    except requests.RequestException:username=None
    exp=lambda key:(datetime.now(timezone.utc)+timedelta(seconds=int(payload.get(key) or 0))).isoformat()
    user=current_user()
    with db() as c:
        existing=c.execute('SELECT id FROM demo_stores WHERE ebay_user=?',(username,)).fetchone() if username else None
        values=(encrypt(access),exp('expires_in'),encrypt(payload['refresh_token']),exp('refresh_token_expires_in'))
        if existing:
            store_id=existing['id']
            c.execute('UPDATE demo_stores SET token=?,token_expires=?,refresh_token=?,refresh_expires=?,connected=1,sync_error=NULL WHERE id=?',values+(store_id,))
        else:
            n=c.execute("SELECT COUNT(*)+1 FROM demo_stores WHERE source='ebay'").fetchone()[0]
            name=username or f'eBay store {n}'
            store_id=c.execute("INSERT INTO demo_stores(name,currency,share,source,marketplace,ebay_user,connected,token,token_expires,refresh_token,refresh_expires) VALUES(?,'GBP','0','ebay',?,?,1,?,?,?,?)",(name[:80],os.getenv('EBAY_MARKETPLACE','EBAY_GB'),username)+values).lastrowid
            c.execute("INSERT INTO demo_share_history(store_id,percent,loss_sharing,effective_at) VALUES(?,'0',0,'1970-01-01T00:00:00+00:00')",(store_id,))
        c.execute("INSERT OR IGNORE INTO demo_user_stores(user_id,store_id) SELECT id,? FROM users WHERE role='manager'",(store_id,))
    try:sync_store(store_id)
    except Exception:pass  # error is saved on the store and shown in the connections panel
    return store_id

@app.get('/oauth/callback')
@manager_required
def oauth_callback():
    try:complete_oauth(request.args.get('code'),request.args.get('state'))
    except Exception as e:return render_template('oauth_result.html',ok=False,message=str(e)),400
    return redirect('/')

@app.post('/api/oauth/complete')
@manager_required
def oauth_complete_pasted():
    """Fallback when the RuName's accepted URL can't reach this local app: paste the address eBay redirected to."""
    raw=str(request.get_json(force=True).get('url','')).strip()
    q=parse_qs(urlparse(raw).query) if '?' in raw else parse_qs(raw)
    try:complete_oauth((q.get('code') or [None])[0],(q.get('state') or [None])[0])
    except Exception as e:return jsonify(error=str(e)),400
    return jsonify(ok=True)

@app.post('/api/disconnect/<int:aid>')
@manager_required
def disconnect(aid):
    if not allowed_store(current_user(),aid):return jsonify(error='Store not found'),404
    with db() as c:c.execute("UPDATE demo_stores SET connected=0,token=NULL,token_expires=NULL,refresh_token=NULL WHERE id=? AND source='ebay'",(aid,))
    return jsonify(ok=True)

@app.post('/api/sync/<int:aid>')
@manager_required
def sync(aid):
    if not allowed_store(current_user(),aid):return jsonify(error='Store not found'),404
    try:count=sync_store(aid)
    except Exception as e:return jsonify(error=str(e)[:400]),502
    return jsonify(ok=True,orders=count)

@app.get('/health')
def health():return jsonify(status='ok')

if __name__=='__main__':
    start_auto_sync()
    app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
