import json
import os
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

import app


def ebay_order(order_id, sku='ABC-1', subtotal='50.00', fee='6.40', refund=None, cancelled=False, date='2026-11-20T10:00:00.000Z'):
    o = {'orderId': order_id, 'creationDate': date, 'lastModifiedDate': date, 'orderPaymentStatus': 'PAID',
         'pricingSummary': {'priceSubtotal': {'value': subtotal, 'currency': 'GBP'}, 'deliveryCost': {'value': '3.00', 'currency': 'GBP'}, 'deliveryDiscount': {'value': '-1.00', 'currency': 'GBP'}},
         'lineItems': [{'sku': sku, 'quantity': 2, 'title': 'Widget', 'legacyItemId': '123', 'ebayCollectAndRemitTaxes': [{'amount': {'value': '10.00', 'currency': 'GBP'}}]}],
         'paymentSummary': {'refunds': []}}
    if fee is not None:
        o['totalMarketplaceFee'] = {'value': fee, 'currency': 'GBP'}
    if refund:
        o['paymentSummary']['refunds'] = [{'refundStatus': 'REFUNDED', 'amount': {'value': refund, 'currency': 'GBP'}}, {'refundStatus': 'FAILED', 'amount': {'value': '99.00', 'currency': 'GBP'}}]
    if cancelled:
        o['cancelStatus'] = {'cancelState': 'CANCELED'}
    return o


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.ok, self.content, self.text = payload, status, status < 400, b'x', ''
    def json(self):
        return self.payload


class EbayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp.name, 'test.sqlite')
        self.env = mock.patch.dict(os.environ, {'EBAY_CLIENT_ID': 'id', 'EBAY_CLIENT_SECRET': 'secret', 'EBAY_RUNAME': 'ru-name', 'TOKEN_ENCRYPTION_KEY': Fernet.generate_key().decode(),
                                               'DEMO_MANAGER_USERNAME': 'manager@test.local', 'DEMO_CLIENT_USERNAME': 'client@test.local', 'DEMO_MANAGER_PASSWORD': 'manager-test-password', 'DEMO_CLIENT_PASSWORD': 'client-test-password',
                                               'DEMO_PARTNER_USERNAME': 'partner@test.local', 'DEMO_PARTNER_PASSWORD': 'partner-test-password'})
        self.env.start()
        app.init_db(); app.seed_demo_data(); app.seed_demo_users()
        self.orders = [ebay_order('O-1'), ebay_order('O-2', fee=None, refund='5.00'), ebay_order('O-3', cancelled=True)]
        self.client = app.app.test_client()

    def tearDown(self):
        self.env.stop()
        app.DB_PATH = self.old_path
        self.tmp.cleanup()

    def fake_post(self, url, **kw):
        assert url.endswith('/identity/v1/oauth2/token')
        return Resp({'access_token': 'acc', 'expires_in': 7200, 'refresh_token': 'ref', 'refresh_token_expires_in': 47304000})

    def fake_get(self, url, **kw):
        if 'identity/v1/user' in url:
            return Resp({'username': 'real_seller_uk'})
        if url.endswith('/sell/fulfillment/v1/order'):
            return Resp({'orders': self.orders, 'total': len(self.orders)})
        raise AssertionError(url)

    def login(self, username, password):
        with self.client.session_transaction() as s:
            s.clear()  # switch user: /login redirects while already signed in
        self.client.get('/login')
        with self.client.session_transaction() as s:
            csrf = s['csrf']
        self.client.post('/login', data={'csrf_token': csrf, 'username': username, 'password': password})
        self.client.get('/')
        with self.client.session_transaction() as s:
            return s['csrf']  # login rotates the CSRF token

    def connect(self, csrf):
        r = self.client.get('/oauth/connect')
        self.assertEqual(r.status_code, 302)
        self.assertIn('prompt=login', r.location)
        with self.client.session_transaction() as s:
            state = s['oauth_state']
        with mock.patch('app.requests.post', self.fake_post), mock.patch('app.requests.get', self.fake_get):
            return self.client.post('/api/oauth/complete', json={'url': f'https://localhost:5050/oauth/callback?state={state}&code=v%5E1.1%23abc'}, headers={'X-CSRF-Token': csrf})

    def test_partner_sees_everything_except_share(self):
        csrf = self.login('manager@test.local', 'manager-test-password')
        self.connect(csrf)
        with mock.patch('app.requests.get', self.fake_get):
            store_id = self.client.get('/api/state').get_json()['stores'][0]['id']
        with app.db() as c:
            c.execute("UPDATE demo_stores SET share='30' WHERE id=?", (store_id,))
        mgr = self.client.get('/api/state').get_json()
        self.assertIn('share', mgr['stores'][0])
        self.assertIn('my_share', mgr['orders'][0]['calculation'])
        self.assertIn('my_share', self.client.get('/api/export/profit.csv').get_data(as_text=True))
        pcsrf = self.login('partner@test.local', 'partner-test-password')
        p = self.client.get('/api/state').get_json()
        self.assertEqual(p['user']['role'], 'manager')
        self.assertTrue(p['user']['hide_share'])
        self.assertEqual([s['id'] for s in p['stores']], [s['id'] for s in mgr['stores']])
        self.assertEqual(len(p['orders']), len(mgr['orders']))
        body = json.dumps(p)
        for word in ('my_share', '"share"', 'loss_sharing'):
            self.assertNotIn(word, body)
        self.assertNotIn('share', self.client.get('/api/export/profit.csv').get_data(as_text=True).splitlines()[0])
        # Partner can edit store settings but cannot change the share.
        r = self.client.post(f'/api/stores/{store_id}', json={'name': 'Renamed', 'share': '99', 'loss_sharing': '1', 'fulfilment_mode': 'dropshipping', 'low_stock_threshold': 3}, headers={'X-CSRF-Token': pcsrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with app.db() as c:
            row = c.execute('SELECT name,share,loss_sharing FROM demo_stores WHERE id=?', (store_id,)).fetchone()
        self.assertEqual((row['name'], row['share'], row['loss_sharing']), ('Renamed', '30', 0))

    def test_map_order_fields(self):
        m = app.map_order(ebay_order('X', refund='5.00'))
        self.assertEqual((m['proceeds'], m['fees'], m['refunds'], m['tax'], m['status']), ('52.00', '6.40', '5.00', '10.00', 'PAID'))
        self.assertEqual(app.map_order(ebay_order('Y', fee=None))['fees'], '')
        self.assertEqual(app.map_order(ebay_order('Z', cancelled=True))['status'], 'CANCELLED')
        no_sku = ebay_order('N', sku=None)
        self.assertIn('ITEM-123', app.map_order(no_sku)['source_json'])

    def test_connect_sync_live_replaces_demo_and_client_access(self):
        csrf = self.login('manager@test.local', 'manager-test-password')
        self.assertEqual(self.connect(csrf).status_code, 200)
        state = self.client.get('/api/state').get_json()
        self.assertTrue(state['live'])
        self.assertEqual([s['name'] for s in state['stores']], ['real_seller_uk'])
        self.assertEqual(sorted(o['order_id'] for o in state['orders']), ['O-1', 'O-2'])  # cancelled hidden
        o2 = next(o for o in state['orders'] if o['order_id'] == 'O-2')
        self.assertIsNone(o2['fees'])
        self.assertTrue(o2['calculation']['missing'])
        with app.db() as c:
            row = c.execute("SELECT token,refresh_token FROM demo_stores WHERE source='ebay'").fetchone()
        self.assertNotIn(b'ref', bytes(row['refresh_token']))  # encrypted at rest
        store_id = state['stores'][0]['id']

        # Per-SKU cost clears the missing flag and applies per unit (qty 2).
        r = self.client.post('/api/costs', json={'store_id': store_id, 'sku': 'ABC-1', 'kind': 'product', 'amount': '10.00', 'currency': 'GBP'}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(r.status_code, 200)
        o1 = next(o for o in self.client.get('/api/state').get_json()['orders'] if o['order_id'] == 'O-1')
        self.assertFalse(o1['calculation']['missing'])
        self.assertEqual(o1['calculation']['profit'], '25.60')  # 52.00 - 6.40 fees - 20.00 product

        # Client sees nothing until granted, then only the live store.
        client_id = state['clients'][0]['id']
        self.client.post('/logout', data={'csrf_token': csrf})
        self.login('client@test.local', 'client-test-password')
        self.assertEqual(self.client.get('/api/state').get_json()['stores'], [])
        csrf = self.login('manager@test.local', 'manager-test-password')
        self.client.post('/api/client-access', json={'client_id': client_id, 'store_id': store_id, 'grant': True}, headers={'X-CSRF-Token': csrf})
        self.login('client@test.local', 'client-test-password')
        self.assertEqual([s['id'] for s in self.client.get('/api/state').get_json()['stores']], [store_id])

        # Restart keeps live-store assignments.
        app.seed_demo_users()
        self.assertEqual([s['id'] for s in self.client.get('/api/state').get_json()['stores']], [store_id])

    def test_cost_sheet_import_matches_net_payout(self):
        csrf = self.login('manager@test.local', 'manager-test-password')
        self.connect(csrf)
        store_id = self.client.get('/api/state').get_json()['stores'][0]['id']
        sheet = ('Date,Supplier,Status,Supplier Order Id,Ebay Order ID,,"£2.00",x,y\n'
                 ',,,,,Link,Sales ,Ebay Net Payout,Sourcing Net Cost,Shipping Cost,Prep Cost\n'
                 '1 Nov,AliExpress,Delivered,A1,O-1,,£52.00,£45.00,£20.00,£1.50,#REF!\n'   # eBay says 52-6.40=45.60 -> 0.60 extra charges
                 '1 Nov,AliExpress,Delivered,A2,O-2,,£52.00,£40.00,"£1,000.00",,\n'            # fee unknown from eBay -> derived from payout
                 '1 Nov,AliExpress,Cancelled,A3,O-3,,,,£5.00,,\n'
                 '1 Nov,AliExpress,Delivered,A4,NOPE,,,,£5.00,,\n')
        # Per-item cost must not be double counted on sheet-costed orders.
        self.client.post('/api/costs', json={'store_id': store_id, 'sku': 'ABC-1', 'kind': 'product', 'amount': '10.00', 'currency': 'GBP'}, headers={'X-CSRF-Token': csrf})
        rep = app.import_cost_sheet(store_id, sheet)
        self.assertEqual((rep['imported'], rep['cancelled'], rep['unmatched'], rep['extra_charges']), (2, 1, ['NOPE'], '0.60'))
        orders = {o['order_id']: o for o in self.client.get('/api/state').get_json()['orders']}
        self.assertEqual(orders['O-1']['calculation']['profit'], '23.50')  # payout 45.00 - 20.00 - 1.50
        self.assertEqual(orders['O-2']['calculation']['profit'], '-960.00')  # payout 40.00 (after 5.00 refund) - 1000.00
        self.assertFalse(orders['O-2']['calculation']['missing'])
        # Re-import replaces, never duplicates.
        app.import_cost_sheet(store_id, sheet)
        with app.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM demo_costs WHERE source IN ('sheet','sheet-fee')").fetchone()[0], 5)

    def test_sheet_url_must_be_google_sheets(self):
        self.assertEqual(app.sheet_csv_url('https://docs.google.com/spreadsheets/d/ABC/edit?gid=7#gid=7'), 'https://docs.google.com/spreadsheets/d/ABC/export?format=csv&gid=7')
        with self.assertRaises(ValueError):
            app.sheet_csv_url('http://169.254.169.254/latest')

    def test_parse_listing_with_variations(self):
        ns = 'urn:ebay:apis:eBLBaseComponents'
        xml = (f'<Item xmlns="{ns}"><ItemID>800890792770</ItemID><Title>Water Carrier</Title><Quantity>99</Quantity><WatchCount>8</WatchCount>'
               '<SellingStatus><CurrentPrice currencyID="GBP">3.29</CurrentPrice><QuantitySold>69</QuantitySold></SellingStatus>'
               '<ListingDetails><StartTime>2026-09-01T00:00:00.000Z</StartTime><ViewItemURL>https://www.ebay.co.uk/itm/800890792770</ViewItemURL></ListingDetails>'
               '<Variations><Variation><StartPrice>3.29</StartPrice><Quantity>50</Quantity><SellingStatus><QuantitySold>40</QuantitySold></SellingStatus></Variation>'
               '<Variation><StartPrice>5.99</StartPrice><Quantity>49</Quantity><SellingStatus><QuantitySold>29</QuantitySold></SellingStatus></Variation></Variations></Item>')
        x = app.parse_listing(app.ET.fromstring(xml))
        self.assertEqual((x['item_id'], x['price'], x['quantity'], x['quantity_sold'], x['quantity_available'], x['has_variations'], x['watch_count']), ('800890792770', '3.29', 99, 69, 30, 1, 8))

    def test_stale_state_rejected(self):
        csrf = self.login('manager@test.local', 'manager-test-password')
        self.client.get('/oauth/connect')
        with mock.patch('app.requests.post', self.fake_post), mock.patch('app.requests.get', self.fake_get):
            r = self.client.post('/api/oauth/complete', json={'url': 'https://x/cb?state=wrong&code=abc'}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(app.live_mode())

    def test_client_cannot_connect_or_sync(self):
        self.login('client@test.local', 'client-test-password')
        self.assertEqual(self.client.get('/oauth/connect').status_code, 403)


if __name__ == '__main__':
    unittest.main()
