import os
import tempfile
import unittest
import re
from werkzeug.security import generate_password_hash
from decimal import Decimal

import app


class FinancialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp.name, 'test.sqlite')
        app.init_db()
        app.seed_demo_data()
        with app.db() as c:
            c.execute("INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES('manager@test.local','Test Manager',?,'manager',?)",(generate_password_hash('manager-test-password'),app.now()))
            c.execute("INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES('client@test.local','Test Client',?,'client',?)",(generate_password_hash('client-test-password'),app.now()))
            manager=c.execute("SELECT id FROM users WHERE username='manager@test.local'").fetchone()['id']
            client_user=c.execute("SELECT id FROM users WHERE username='client@test.local'").fetchone()['id']
            ids=[r['id'] for r in c.execute('SELECT id FROM demo_stores ORDER BY id')]
            c.executemany('INSERT INTO demo_user_stores(user_id,store_id) VALUES(?,?)',[(manager,i) for i in ids])
            c.execute('INSERT INTO demo_user_stores(user_id,store_id) VALUES(?,?)',(client_user,ids[0]))
        with app.db() as c:
            self.a = c.execute('select id from accounts where name="Account A"').fetchone()['id']
            self.b = c.execute('select id from accounts where name="Account B"').fetchone()['id']

    def tearDown(self):
        app.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_decimal_profit_after_refund_fees_and_costs(self):
        order={'account_id':self.a,'order_id':'o1','created_at':'2026-01-01T00:00:00+00:00','currency':'GBP','proceeds':'100.00','refunds':'10.00','fees':'12.34','source_json':{'lineItems':[{'sku':'sku1','quantity':2}]}}
        costs=[{'account_id':self.a,'order_id':'o1','sku':'sku1','kind':'product','amount':'20.11','currency':'GBP'}, {'account_id':self.a,'order_id':'o1','sku':'sku1','kind':'fulfilment','amount':'5.55','currency':'GBP'}]
        defaults=[];accounts=[{'id':self.a,'name':'Account A','share':'30','loss_sharing':0}];history=[{'account_id':self.a,'effective_at':'2025-01-01','percent':'30'}]
        result=app.calculate(order,costs,defaults,accounts,history)
        self.assertEqual(result['profit'],'52.00')
        self.assertEqual(result['my_share'],'15.60')
        self.assertFalse(result['missing'])

    def test_missing_costs_and_finance_values_are_not_zero_profit(self):
        o={'account_id':self.a,'order_id':'o2','created_at':'2026-01-01','currency':'GBP','proceeds':'50','source_json':{'lineItems':[{'sku':'x'}]}}
        result=app.calculate(o,[],[],[{'id':self.a,'name':'A','share':'30'}],[])
        self.assertTrue(result['missing'])
        self.assertIsNone(result['profit'])
        self.assertIsNone(result['my_share'])

    def test_effective_share_and_configurable_loss_sharing(self):
        o={'account_id':self.a,'order_id':'o3','created_at':'2026-02-01','currency':'GBP','proceeds':'10','refunds':'20','fees':'0','source_json':{'lineItems':[{'sku':'x'}]}}
        costs=[{'account_id':self.a,'order_id':'o3','sku':'x','kind':'product','amount':'1','currency':'GBP'}]
        history=[{'account_id':self.a,'effective_at':'2026-01-01','percent':'30'},{'account_id':self.a,'effective_at':'2026-02-01','percent':'60'}]
        base=app.calculate(o,costs,[],[{'id':self.a,'name':'A','share':'90','loss_sharing':0}],history)
        self.assertEqual(base['share'],'60')
        self.assertEqual(base['my_share'],'0.00')
        shared=app.calculate(o,costs,[],[{'id':self.a,'name':'A','share':'90','loss_sharing':1}],history)
        self.assertEqual(shared['my_share'],'-6.60')

    def test_accounts_are_namespaced_and_sync_upsert_is_idempotent(self):
        with app.db() as c:
            for aid in (self.a,self.b):
                c.execute('INSERT INTO orders(account_id,order_id,proceeds,currency,source_json) VALUES(?,?,?,?,?)',(aid,'same-id','10','GBP','{}'))
            c.execute('INSERT INTO orders(account_id,order_id,proceeds,currency,source_json) VALUES(?,?,?,?,?) ON CONFLICT(account_id,order_id) DO UPDATE SET proceeds=excluded.proceeds',(self.a,'same-id','12','GBP','{}'))
            self.assertEqual(c.execute('SELECT count(*) n FROM orders').fetchone()['n'],2)
            self.assertEqual(c.execute('SELECT proceeds FROM orders WHERE account_id=?',(self.a,)).fetchone()['proceeds'],'12')
            self.assertEqual(c.execute('SELECT proceeds FROM orders WHERE account_id=?',(self.b,)).fetchone()['proceeds'],'10')

    def login(self, client, username, password):
        page=client.get('/login')
        token=re.search(r'name="csrf_token" value="([^"]+)',page.get_data(as_text=True)).group(1)
        return client.post('/login',data={'username':username,'password':password,'csrf_token':token},follow_redirects=True)

    def test_manager_and_client_store_isolation_and_exports(self):
        anon=app.app.test_client()
        self.assertEqual(anon.get('/api/state').status_code,401)
        self.assertEqual(anon.get('/api/export/profit.csv').status_code,401)
        client=app.app.test_client(); self.login(client,'client@test.local','client-test-password')
        own=client.get('/api/state').get_json()
        self.assertEqual(len(own['stores']),1)
        store_id=own['stores'][0]['id']
        self.assertTrue(all(o['account_id']==store_id for o in own['orders']))
        other=store_id+1
        self.assertEqual(client.get(f'/api/export/profit.csv?store_id={other}').status_code,404)
        self.assertEqual(client.get(f'/api/export/orders.csv?account_id={other}').status_code,404)
        spoofed=app.app.test_client(); page=spoofed.get('/login')
        token=re.search(r'name=\"csrf_token\" value=\"([^\"]+)',page.get_data(as_text=True)).group(1)
        spoofed.post('/login',data={'username':'client@test.local','password':'client-test-password','role':'manager','csrf_token':token})
        self.assertEqual(spoofed.get('/api/state').get_json()['user']['role'],'client')
        csv_data=client.get('/api/export/profit.csv').get_data(as_text=True)
        self.assertIn(own['stores'][0]['name'],csv_data)
        self.assertNotIn('Maple & Main',csv_data)
        csrf=own['csrf']
        self.assertEqual(client.post(f'/api/stores/{store_id}',json={'name':'Manager takeover'},headers={'X-CSRF-Token':csrf}).status_code,403)
        manager=app.app.test_client(); self.login(manager,'manager@test.local','manager-test-password')
        all_data=manager.get('/api/state').get_json()
        self.assertEqual(len(all_data['stores']),4)
        self.assertGreater(len(all_data['orders']),len(own['orders']))
        self.assertEqual(all_data['dataset'],'SYNTHETIC DEMO')



    def test_manager_share_change_is_effective_dated(self):
        manager=app.app.test_client(); self.login(manager,'manager@test.local','manager-test-password')
        state=manager.get('/api/state').get_json(); store=state['stores'][0]; token=state['csrf']
        response=manager.post(f"/api/stores/{store['id']}",json={'name':store['name'],'share':'60','effective_date':'2026-11-01','loss_sharing':'1','fulfilment_mode':store['fulfilment_mode'],'low_stock_threshold':store['low_stock_threshold']},headers={'X-CSRF-Token':token})
        self.assertEqual(response.status_code,200)
        changed=manager.get('/api/state').get_json()
        before=next(o for o in changed['orders'] if o['account_id']==store['id'] and o['created_at']<'2026-11-01')
        after=next(o for o in changed['orders'] if o['account_id']==store['id'] and o['created_at']>='2026-11-01')
        self.assertEqual(before['calculation']['share'],'30')
        self.assertEqual(after['calculation']['share'],'60')

    def test_stock_rules_are_scoped_to_manager_assigned_demo_stores(self):
        manager=app.app.test_client(); self.login(manager,'manager@test.local','manager-test-password')
        stores=manager.get('/api/state').get_json()['stores']; sid=stores[0]['id']; token=manager.get('/api/state').get_json()['csrf']
        first=manager.post('/api/stock-rules',json={'store_id':sid,'sku':'SKU-1','threshold':2},headers={'X-CSRF-Token':token})
        update=manager.post('/api/stock-rules',json={'store_id':sid,'sku':'SKU-1','threshold':4},headers={'X-CSRF-Token':token})
        self.assertEqual((first.status_code,update.status_code),(200,200))
        with app.db() as c:self.assertEqual(c.execute('SELECT threshold FROM demo_stock_rules WHERE store_id=? AND sku=?',(sid,'SKU-1')).fetchone()['threshold'],4)
        client=app.app.test_client(); self.login(client,'client@test.local','client-test-password')
        self.assertEqual(client.get(f'/api/state?store_id={sid+1}').get_json()['stores'][0]['id'],sid)
        self.assertEqual(client.post('/api/stock-rules',json={'store_id':sid,'sku':'SKU-1','threshold':1},headers={'X-CSRF-Token':client.get('/api/state').get_json()['csrf']}).status_code,403)

    def test_actual_order_cost_does_not_leak_across_same_sku(self):
        orders=[{'account_id':self.a,'order_id':'o1','created_at':'2026-01-01','currency':'GBP','proceeds':'20','refunds':'0','fees':'0','source_json':{'lineItems':[{'sku':'same'}]}},{'account_id':self.a,'order_id':'o2','created_at':'2026-01-01','currency':'GBP','proceeds':'20','refunds':'0','fees':'0','source_json':{'lineItems':[{'sku':'same'}]}}]
        cost=[{'id':1,'account_id':self.a,'order_id':'o1','sku':'same','kind':'product','amount':'5','currency':'GBP'}]
        result=app.calculate(orders[1],cost,[],[{'id':self.a,'name':'A','share':'30'}],[])
        self.assertTrue(result['missing']); self.assertIsNone(result['profit'])

if __name__ == '__main__': unittest.main()
