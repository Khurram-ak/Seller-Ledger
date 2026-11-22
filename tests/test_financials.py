import os
import tempfile
import unittest
from decimal import Decimal

import app


class FinancialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp.name, 'test.sqlite')
        app.init_db()
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

    def test_stock_thresholds_are_upserted_per_account_and_sku(self):
        client=app.app.test_client()
        client.get('/')  # establishes the local dashboard session
        first=client.post('/api/stock-rules',json={'account_id':self.a,'sku':'SKU-1','threshold':2})
        second=client.post('/api/stock-rules',json={'account_id':self.b,'sku':'SKU-1','threshold':7})
        update=client.post('/api/stock-rules',json={'account_id':self.a,'sku':'SKU-1','threshold':4})
        self.assertEqual((first.status_code,second.status_code,update.status_code),(200,200,200))
        with app.db() as c:
            rows=c.execute('SELECT account_id,sku,threshold FROM stock_rules ORDER BY account_id').fetchall()
        self.assertEqual([(r['account_id'],r['sku'],r['threshold']) for r in rows],[(self.a,'SKU-1',4),(self.b,'SKU-1',7)])

if __name__ == '__main__': unittest.main()
