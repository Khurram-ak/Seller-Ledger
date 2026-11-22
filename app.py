import csv
import io
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from urllib.parse import urlencode

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
        stores=[r['id'] for r in c.execute('SELECT id FROM demo_stores ORDER BY id')]
        if len(stores)<4:
            raise RuntimeError('Demo stores are not initialized')
        credentials=[(os.getenv('DEMO_MANAGER_USERNAME','manager@demo.local'),'Demo Manager','manager',manager_password,stores),(os.getenv('DEMO_CLIENT_USERNAME','client@demo.local'),'Demo Client','client',client_password,[stores[0]])]
        for username,display,role,password,assigned in credentials:
            c.execute('INSERT INTO users(username,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?) ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name,password_hash=excluded.password_hash,role=excluded.role,active=1',(username,display,generate_password_hash(password,method='scrypt'),role,now()))
            user_id=c.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()['id']
            c.execute('DELETE FROM demo_user_stores WHERE user_id=?',(user_id,))
            c.executemany('INSERT INTO demo_user_stores(user_id,store_id) VALUES(?,?)',[(user_id,sid) for sid in assigned])
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

@app.route('/login',methods=['GET','POST'])
def login():
    if current_user():
        return redirect(url_for('home'))
    session.setdefault('csrf',secrets.token_urlsafe(32))
    error=None
    if request.method=='POST':
        username=request.form.get('username','').strip()
        password=request.form.get('password','')
        with db() as c:
            row=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
        if row and check_password_hash(row['password_hash'],password):
            session.clear()
            session['user_id']=row['id']
            session['csrf']=secrets.token_urlsafe(32)
            session.permanent=True
            return redirect(url_for('home'))
        error='Username or password is incorrect.'
    with db() as c:
        configured=c.execute('SELECT COUNT(*) FROM users WHERE active=1').fetchone()[0]>0
    return render_template('login.html',csrf=session['csrf'],configured=configured,error=error), (401 if error else 200)

@app.post('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

def build_demo_state(user):
    ids=allowed_store_ids(user['id'])
    if not ids:
        return {'user':{'name':user['display_name'],'role':user['role']},'dataset':'SYNTHETIC DEMO','stores':[],'orders':[],'costs':[],'defaults':[],'history':[],'csrf':session.get('csrf')}
    marks=','.join('?' for _ in ids)
    with db() as c:
        stores=[dict(r) for r in c.execute(f'SELECT id,name,currency,share,loss_sharing,fulfilment_mode,low_stock_threshold FROM demo_stores WHERE id IN ({marks}) ORDER BY id',ids)]
        history=[dict(r) for r in c.execute(f'SELECT store_id AS account_id,percent,effective_at,loss_sharing FROM demo_share_history WHERE store_id IN ({marks}) ORDER BY effective_at,id',ids)]
        orders=[dict(r) for r in c.execute(f'SELECT id,store_id AS account_id,order_id,created_at,currency,proceeds,tax,refunds,fees,status,source_json FROM demo_orders WHERE store_id IN ({marks}) ORDER BY created_at DESC',ids)]
        costs=[dict(r) for r in c.execute(f'SELECT id,store_id AS account_id,order_id,sku,kind,amount,currency,note FROM demo_costs WHERE store_id IN ({marks})',ids)]
    for order in orders:
        order['source_json']=json.loads(order['source_json'])
        result=calculate(order,costs,[],stores,history)
        order['calculation']=result
        order['demo']=True
    return {'user':{'name':user['display_name'],'role':user['role']},'dataset':'SYNTHETIC DEMO','stores':stores,'orders':orders,'costs':costs,'defaults':[],'history':[],'csrf':session.get('csrf')}

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

def save_api_snapshot(aid, table, records):
    with db() as c:
        if table=='listing':
            for x in records:
                c.execute('INSERT INTO listings(account_id,sku,offer_id,title,quantity,status,source_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,offer_id) DO UPDATE SET sku=excluded.sku,title=excluded.title,quantity=excluded.quantity,status=excluded.status,source_json=excluded.source_json',(aid,x.get('sku'),str(x.get('offerId') or x.get('sku') or ''),x.get('title'),x.get('quantityAvailable'),x.get('status'),json.dumps(x)))
        elif table=='metric':
            for x in records:c.execute('INSERT INTO metrics(account_id,day,payload) VALUES(?,?,?)',(aid,x.get('date') or x.get('dimensionKey') or now()[:10],json.dumps(x)))

def sync_inventory(aid, marketplace):
    items=[];offset=0
    while True:
        page=ebay_get(aid,'/sell/inventory/v1/inventory_item',{'limit':1000,'offset':offset}); batch=page.get('inventoryItems',[]);items+=batch
        if len(batch)<1000:break
        offset+=len(batch)
    offers=[]
    # getOffers is SKU scoped; limit calls to unique SKUs and preserve supported coverage.
    for item in items:
        sku=item.get('sku')
        if not sku:continue
        try:
            out=ebay_get(aid,'/sell/inventory/v1/offer',{'sku':sku,'marketplace_id':marketplace,'limit':100})
            for offer in out.get('offers',[]):
                offer['sku']=sku;offer['quantityAvailable']=item.get('availability',{}).get('shipToLocationAvailability',{}).get('quantity')
                offers.append(offer)
        except Exception: continue
    save_api_snapshot(aid,'listing',offers)
    return len(items),len(offers)

def sync_analytics(aid,marketplace):
    # eBay traffic reports accept a bounded 90-day window; request daily aggregates.
    end=datetime.now(timezone.utc).date();start=end-timedelta(days=89)
    filt=f'marketplace_ids:{{{marketplace}}},date_range:[{start:%Y%m%d}..{end:%Y%m%d}]'
    payload=ebay_get(aid,'/sell/analytics/v1/traffic_report',{'dimension':'DAY','filter':filt,'metric':'LISTING_VIEWS_TOTAL,LISTING_SALES_CONVERSION_RATE'})
    records=payload.get('records',[]);save_api_snapshot(aid,'metric',records);return len(records)

def token_for(aid):
    with db() as c: a=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
    if not a or not a['refresh_token']:raise RuntimeError('Account is not connected')
    expires=datetime.fromisoformat(a['token_expires']) if a['token_expires'] else datetime.now(timezone.utc)
    if a['token'] and expires>datetime.now(timezone.utc)+timedelta(minutes=2):return decrypt(a['token'])
    basic=requests.auth._basic_auth_str(os.getenv('EBAY_CLIENT_ID',''),os.getenv('EBAY_CLIENT_SECRET',''))
    resp=requests.post(ebay_base()+'/identity/v1/oauth2/token',headers={'Authorization':basic,'Content-Type':'application/x-www-form-urlencoded'},data={'grant_type':'refresh_token','refresh_token':decrypt(a['refresh_token']),'scope':' '.join(SCOPES)},timeout=30)
    resp.raise_for_status(); payload=resp.json()
    with db() as c:c.execute('UPDATE accounts SET token=?,token_expires=? WHERE id=?',(encrypt(payload['access_token']), (datetime.now(timezone.utc)+timedelta(seconds=payload['expires_in'])).isoformat(),aid))
    return payload['access_token']

def ebay_get(aid,path,params=None):
    token=token_for(aid); url=ebay_base()+path
    for attempt in range(4):
        r=requests.get(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'},params=params,timeout=45)
        if r.status_code==429 or r.status_code>=500:
            time.sleep(min(2**attempt,12));continue
        if r.status_code==401:
            with db() as c:c.execute('UPDATE accounts SET token=NULL,token_expires=NULL WHERE id=?',(aid,))
            token=token_for(aid);continue
        r.raise_for_status();return r.json() if r.content else {}
    r.raise_for_status()

@app.get('/oauth/connect/<int:aid>')
@manager_required
def oauth_connect(aid):
    return jsonify(error='Live eBay OAuth is disabled in the current synthetic demo phase'),501

@app.get('/oauth/callback')
@manager_required
def oauth_callback():
    return jsonify(error='Live eBay OAuth is disabled in the current synthetic demo phase'),501

@app.post('/api/disconnect/<int:aid>')
@manager_required
def disconnect(aid):
    return jsonify(error='Live eBay account management is disabled in the current synthetic demo phase'),501

@app.post('/api/sync/<int:aid>')
@manager_required
def sync(aid):
    return jsonify(error='Live eBay sync is disabled in the current synthetic demo phase'),501
    # Retained below for the future live-data phase; demo routes never call it.
    try:
        with db() as c:a=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
        if not a: return jsonify(error='Account not found'),404
        token_for(aid)
        cursor=a['cursor'] or (datetime.now(timezone.utc)-timedelta(days=90)).isoformat(timespec='milliseconds').replace('+00:00','Z')
        offset=0;total=0;currency_set=set()
        while True:
            payload=ebay_get(aid,'/sell/fulfillment/v1/order',{'filter':f'lastmodifieddate:[{cursor}..]','limit':200,'offset':offset,'fieldGroups':'TAX_BREAKDOWN'})
            orders=payload.get('orders',[])
            with db() as c:
                for o in orders:
                    summary=o.get('pricingSummary',{}); curr=(summary.get('total') or summary.get('priceSubtotal') or {}).get('currency')
                    currency_set.add(curr)
                    item_sales=summary.get('priceSubtotal')
                    delivery=summary.get('deliveryCost') or {};discount=summary.get('deliveryDiscount') or {}
                    proceeds_value=(Decimal(item_sales['value'])+Decimal(delivery.get('value','0'))+Decimal(discount.get('value','0'))) if item_sales and item_sales.get('value') is not None else None
                    tax=summary.get('tax')
                    if tax is None:
                        marketplace_taxes=[t.get('amount',{}) for line in o.get('lineItems',[]) for t in line.get('ebayCollectAndRemitTaxes',[])]
                        tax_value=sum((Decimal(t['value']) for t in marketplace_taxes if t.get('value') is not None),Decimal(0)) if marketplace_taxes else None
                    else: tax_value=Decimal(tax.get('value','0'))
                    c.execute('''INSERT INTO orders(account_id,order_id,created_at,modified_at,currency,proceeds,tax,source_json,status) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,order_id) DO UPDATE SET modified_at=excluded.modified_at,currency=excluded.currency,proceeds=excluded.proceeds,tax=excluded.tax,source_json=excluded.source_json,status=excluded.status''',(aid,o['orderId'],o.get('creationDate'),o.get('lastModifiedDate'),curr,money(proceeds_value) if proceeds_value is not None else None,money(tax_value) if tax_value is not None else None,json.dumps(o),o.get('orderPaymentStatus')))
                total+=len(orders)
            if len(orders)<200:break
            offset+=200
        results={'orders':total,'finances':'unavailable','inventory':'unavailable','analytics':'unavailable'};errors=[]
        if a['marketplace'] in ('EBAY_GB','EBAY_DE','EBAY_FR','EBAY_IT','EBAY_ES'):
            errors.append('Finances skipped: EU/UK-domiciled sellers require eBay digital-signature headers and registered signing keys.')
        else:
            try:
                # Transactions remain in a separate account ledger; payouts never become order proceeds.
                start=datetime.now(timezone.utc)-timedelta(days=90);end=datetime.now(timezone.utc)
                offset=0;rows=0
                while True:
                    tx=ebay_get(aid,'/sell/finances/v1/transaction',{'filter':f'transactionDate:[{start:%Y-%m-%dT%H:%M:%S.000Z}..{end:%Y-%m-%dT%H:%M:%S.000Z}]','limit':1000,'offset':offset})
                    items=tx.get('transactions',[])
                    with db() as c:
                        for t in items:
                            am=t.get('amount',{});c.execute('INSERT INTO ledger(account_id,transaction_id,order_id,type,amount,currency,source_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,transaction_id) DO UPDATE SET order_id=excluded.order_id,type=excluded.type,amount=excluded.amount,currency=excluded.currency,source_json=excluded.source_json',(aid,t['transactionId'],t.get('orderId'),t.get('transactionType'),money(am.get('value')),am.get('currency'),json.dumps(t)))
                    rows+=len(items)
                    if len(items)<1000:break
                    offset+=1000
                results['finances']=rows
            except Exception as ex:errors.append('Finances sync unavailable: '+str(ex)[:180])
        try:results['inventory']=sync_inventory(aid,a['marketplace'])
        except Exception as ex:errors.append('Inventory sync unavailable: '+str(ex)[:180])
        try:results['analytics']=sync_analytics(aid,a['marketplace'])
        except Exception as ex:errors.append('Analytics sync unavailable: '+str(ex)[:180])
        coverage={'orders':True,'finances':results['finances']!='unavailable','inventory':results['inventory']!='unavailable','analytics':results['analytics']!='unavailable','currencies':sorted(x for x in currency_set if x),'gaps':errors}
        with db() as c:c.execute('UPDATE accounts SET sync_at=?,cursor=?,coverage=?,error=? WHERE id=?',(now(),datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),json.dumps(coverage),'; '.join(errors) or None,aid))
        return jsonify(ok=True,results=results,coverage=coverage)
    except Exception as e:
        message=f'{type(e).__name__}: {e}'[:500]
        with db() as c:c.execute('UPDATE accounts SET error=? WHERE id=?',(message,aid))
        return jsonify(error=message),502

@app.get('/health')
def health():return jsonify(status='ok')

if __name__=='__main__': app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
