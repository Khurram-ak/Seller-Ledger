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

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('DATABASE_PATH', os.path.join(ROOT, 'dashboard.sqlite3'))
app = Flask(__name__)
app.secret_key = os.getenv('APP_SECRET') or secrets.token_urlsafe(48)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0') == '1', MAX_CONTENT_LENGTH=8 * 1024 * 1024)
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
        # Seed a fresh database once. Never recreate defaults when an owner has
        # renamed or removed their accounts.
        if c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0:
            for name, share in [('Account A','30'),('Account B','30'),('Account C','30'),('Account D','60')]:
                cur = c.execute('INSERT INTO accounts(name,share) VALUES(?,?)', (name, share))
                c.execute('INSERT INTO share_history(account_id,percent,loss_sharing,effective_at) VALUES(?,?,0,?)', (cur.lastrowid, share, now()))

init_db()

def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not session.get('owner'): return jsonify(error='Owner login required'), 401
        return fn(*a, **kw)
    return wrapped
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
    # Local-only owner convenience: loopback is the access boundary for this private app.
    session['owner'] = True
    session.setdefault('csrf', secrets.token_urlsafe(32))
    return render_template('index.html')

@app.get('/api/state')
@login_required
def state():
    with db() as c:
        accounts = [dict(r) for r in c.execute('SELECT id,name,share,loss_sharing,marketplace,connected,sync_at,coverage,error FROM accounts ORDER BY id')]
        orders = [dict(r) for r in c.execute('SELECT * FROM orders ORDER BY created_at DESC')]
        costs = [dict(r) for r in c.execute('SELECT * FROM costs')]
        defaults = [dict(r) for r in c.execute('SELECT * FROM sku_costs')]
        listings = [dict(r) for r in c.execute('SELECT account_id,sku,offer_id,title,quantity,status FROM listings')]
        stock_rules = [dict(r) for r in c.execute('SELECT account_id,sku,threshold FROM stock_rules')]
        metrics = [dict(r) for r in c.execute('SELECT account_id,day,payload FROM metrics')]
        ledger = [dict(r) for r in c.execute('SELECT account_id,order_id,type,amount,currency FROM ledger')]
        history = [dict(r) for r in c.execute('SELECT * FROM share_history ORDER BY effective_at')]
    for o in orders: o['source_json'] = json.loads(o['source_json'])
    for l in metrics: l['payload'] = json.loads(l['payload'])
    return jsonify(accounts=accounts,orders=orders,costs=costs,defaults=defaults,listings=listings,stockRules=stock_rules,metrics=metrics,ledger=ledger,history=history,csrf=session.get('csrf'))

@app.post('/api/stock-rules')
@login_required
def set_stock_rule():
    d=request.get_json(force=True)
    sku=str(d.get('sku','')).strip()
    try: threshold=int(d.get('threshold'))
    except (TypeError,ValueError): return jsonify(error='Enter a whole-number stock threshold'),400
    if not sku or len(sku)>100:return jsonify(error='SKU is required (maximum 100 characters)'),400
    if threshold<0 or threshold>1000000:return jsonify(error='Threshold must be between 0 and 1,000,000'),400
    try:
        with db() as c:c.execute('INSERT INTO stock_rules(account_id,sku,threshold,updated_at) VALUES(?,?,?,?) ON CONFLICT(account_id,sku) DO UPDATE SET threshold=excluded.threshold,updated_at=excluded.updated_at',(int(d['account_id']),sku,threshold,now()))
    except sqlite3.IntegrityError:return jsonify(error='Seller account was not found'),404
    return jsonify(ok=True)

@app.post('/api/accounts/<int:aid>')
@login_required
def edit_account(aid):
    d=request.get_json(force=True); percent=Decimal(str(d.get('share','0')))
    if not 0 <= percent <= 100: return jsonify(error='Share must be from 0 to 100'),400
    with db() as c:
        row=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
        if not row:return jsonify(error='Account not found'),404
        c.execute('UPDATE accounts SET name=?,share=?,loss_sharing=?,marketplace=? WHERE id=?',(d.get('name',row['name'])[:80],str(percent),int(bool(d.get('loss_sharing'))),d.get('marketplace',row['marketplace']),aid))
        if str(percent)!=row['share'] or int(bool(d.get('loss_sharing')))!=row['loss_sharing']:
            c.execute('INSERT INTO share_history(account_id,percent,loss_sharing,effective_at) VALUES(?,?,?,?)',(aid,str(percent),int(bool(d.get('loss_sharing'))),now()))
    return jsonify(ok=True)

@app.post('/api/costs')
@login_required
def add_cost():
    d=request.get_json(force=True); amount=money(d.get('amount'))
    if Decimal(amount)<0:return jsonify(error='Costs must be zero or positive'),400
    with db() as c:
        c.execute('INSERT INTO costs(account_id,order_id,sku,kind,amount,currency,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(int(d['account_id']),d.get('order_id') or None,d.get('sku') or None,d['kind'],amount,d['currency'],d.get('note',''),now()))
        c.execute('INSERT INTO audit(account_id,order_id,event,source_json,created_at) VALUES(?,?,?,?,?)',(int(d['account_id']),d.get('order_id'), 'manual_cost',json.dumps(d),now()))
    return jsonify(ok=True)

@app.post('/api/default-cost')
@login_required
def default_cost():
    d=request.get_json(force=True)
    with db() as c:
        c.execute('INSERT INTO sku_costs(account_id,sku,product_cost,fulfilment_cost,currency) VALUES(?,?,?,?,?) ON CONFLICT(account_id,sku) DO UPDATE SET product_cost=excluded.product_cost,fulfilment_cost=excluded.fulfilment_cost,currency=excluded.currency',(int(d['account_id']),d['sku'],money(d.get('product_cost')),money(d.get('fulfilment_cost')),d['currency']))
    return jsonify(ok=True)

@app.post('/api/import-costs')
@login_required
def import_costs():
    if 'file' not in request.files:return jsonify(error='Choose a CSV file'),400
    stream=io.StringIO(request.files['file'].stream.read().decode('utf-8-sig')); reader=csv.DictReader(stream); count=0
    with db() as c:
        for row in reader:
            aid=int(row['account_id']); kind=row.get('kind','product')
            c.execute('INSERT INTO costs(account_id,order_id,sku,kind,amount,currency,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(aid,row.get('order_id') or None,row.get('sku') or None,kind,money(row['amount']),row['currency'],row.get('note','CSV import'),now())); count+=1
    return jsonify(imported=count)

@app.get('/api/export/<kind>.csv')
@login_required
def export(kind):
    if kind not in ('orders','profit'):return 'Unknown export',404
    state_data=state().get_json(); out=io.StringIO(); w=csv.writer(out)
    w.writerow(['account','order_id','created_at','currency','sales','marketplace_tax','refunds','fees','product_cost','fulfilment_cost','other_cost','net_profit','share_percent','my_share','missing_costs'])
    for o in state_data['orders']:
        calc=calculate(o,state_data['costs'],state_data['defaults'],state_data['accounts'],state_data['history'])
        if kind=='orders' or kind=='profit':
            w.writerow([calc['account'],o['order_id'],o['created_at'],o['currency'],o['proceeds'],o['tax'],o['refunds'],o['fees'],calc['product'],calc['fulfilment'],calc['other'],calc['profit'],calc['share'],calc['my_share'],calc['missing']])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="{kind}.csv"'})

def calculate(o,costs,defaults,accounts,history):
    account=next((a for a in accounts if a['id']==o['account_id']),{})
    exact=[c for c in costs if c['account_id']==o['account_id'] and c['order_id']==o['order_id']]
    sku_rows=[c for c in costs if c['account_id']==o['account_id'] and c['sku'] and c['sku'] in [i.get('sku') for i in o.get('source_json',{}).get('lineItems',[])]]
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
    share=shares[-1]['percent'] if shares else (account.get('share','0') if not account_history else None)
    if share is None: missing=True
    mine=Decimal(0) if profit is None or share is None else profit*Decimal(share)/100
    if profit is not None and profit<0 and not account.get('loss_sharing'): mine=Decimal(0)
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
@login_required
def oauth_connect(aid):
    if not os.getenv('EBAY_CLIENT_ID') or not os.getenv('EBAY_RUNAME'):return 'Configure eBay client ID and RuName in .env',503
    state=secrets.token_urlsafe(24); session['oauth_state']=state;session['oauth_account']=aid
    params={'client_id':os.getenv('EBAY_CLIENT_ID'),'response_type':'code','redirect_uri':os.getenv('EBAY_RUNAME'),'scope':' '.join(SCOPES),'state':state}
    return redirect(oauth_host()+'/oauth2/authorize?'+urlencode(params))

@app.get('/oauth/callback')
def oauth_callback():
    if not session.get('owner'):return redirect('/?login=1')
    if request.args.get('state')!=session.pop('oauth_state',None):return 'OAuth state validation failed',400
    aid=session.pop('oauth_account',None); code=request.args.get('code')
    if not aid or not code:return 'Missing OAuth authorization response',400
    basic=requests.auth._basic_auth_str(os.getenv('EBAY_CLIENT_ID',''),os.getenv('EBAY_CLIENT_SECRET',''))
    r=requests.post(ebay_base()+'/identity/v1/oauth2/token',headers={'Authorization':basic,'Content-Type':'application/x-www-form-urlencoded'},data={'grant_type':'authorization_code','code':code,'redirect_uri':os.getenv('EBAY_RUNAME')},timeout=30)
    if not r.ok:return 'eBay token exchange failed; check portal redirect URI and application keys',502
    data=r.json()
    with db() as c:c.execute('UPDATE accounts SET connected=1,token=?,refresh_token=?,token_expires=?,error=NULL WHERE id=?',(encrypt(data['access_token']),encrypt(data['refresh_token']), (datetime.now(timezone.utc)+timedelta(seconds=data['expires_in'])).isoformat(),aid))
    return redirect('/?connected=1')

@app.post('/api/disconnect/<int:aid>')
@login_required
def disconnect(aid):
    with db() as c:c.execute('UPDATE accounts SET connected=0,token=NULL,refresh_token=NULL,token_expires=NULL,error=NULL WHERE id=?',(aid,))
    return jsonify(ok=True)

@app.post('/api/sync/<int:aid>')
@login_required
def sync(aid):
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
