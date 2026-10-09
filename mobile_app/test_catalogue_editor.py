import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import server


class CatalogueEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.catalogue = self.path/'catalogue.json'
        self.original = json.loads((Path(__file__).parent.parent/'given_industrial_products.json').read_text())
        self.catalogue.write_text(json.dumps(self.original))
        self.patch = patch.object(server.matcher, 'CATALOGUE_PATH', self.catalogue); self.patch.start()
        self.service = server.App('fake', self.path/'history.sqlite3', 'https://example.test')
        self.owner = self.service.store.add_user('owner','a-long-owner-password')
        self.other = self.service.store.add_user('other','a-long-other-password')
        self.app = server.create_app(self.service)
        with self.service.store.connect() as db:
            db.execute('INSERT INTO catalogue_owners VALUES (?)',(self.owner,))
        self.a = self.app.test_client(); self.b = self.app.test_client()
        self.ca = self.login(self.a,'owner','a-long-owner-password')
        self.cb = self.login(self.b,'other','a-long-other-password')

    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()

    def get(self,client,path='/api/owner/catalogue'):
        return client.get(path,base_url=self.service.origin)

    def post(self,client,path,body,csrf=''):
        return client.post(path,json=body,base_url=self.service.origin,headers={'Origin':self.service.origin,'X-CSRF-Token':csrf})

    def login(self,client,user,password):
        self.assertEqual(self.post(client,'/api/login',{'username':user,'password':password}).status_code,200)
        return self.get(client,'/api/session').json['csrf']

    def payload(self,product=None,old_id=None):
        product = product or {'id':'NEW-1','name':'New sensor','category':'sensor','custom':{'range':[1,2],'certified':False,'unknown':None}}
        return dict(product=product,original_id=old_id,revision=self.get(self.a).json['revision'])

    def save(self,body):
        return self.post(self.a,'/api/owner/products',body,self.ca)

    def test_owner_authorization_csrf_and_role_revocation(self):
        self.assertTrue(self.get(self.a,'/api/session').json['is_owner'])
        self.assertFalse(self.get(self.b,'/api/session').json['is_owner'])
        self.assertEqual(self.get(self.app.test_client()).status_code,401)
        self.assertEqual(self.get(self.b).status_code,403)
        body=self.payload()
        self.assertEqual(self.post(self.b,'/api/owner/products',body,self.cb).status_code,403)
        self.assertEqual(self.post(self.a,'/api/owner/products',body).status_code,403)
        with self.service.store.connect() as db:db.execute('DELETE FROM catalogue_owners')
        self.assertEqual(self.save(body).status_code,403)

    def test_noop_preserves_all_fields_types_and_metadata(self):
        p=copy.deepcopy(self.original['products'][0])
        self.assertEqual(self.save(self.payload(p,p['id'])).status_code,200)
        self.assertEqual(json.loads(self.catalogue.read_text()),self.original)
        backups=list((self.path/'catalogue-backups').glob('*.json'))
        self.assertEqual(len(backups),1)
        self.assertEqual(json.loads(backups[0].read_text()),self.original)

    def test_add_edit_and_matcher_reload(self):
        body=self.payload()
        self.assertEqual(self.save(body).status_code,200)
        self.assertEqual(server.matcher.load_catalogue()['products'][-1],body['product'])
        product=body['product'];product['id']='CHANGED';product['custom']['range']=[2,5]
        self.assertEqual(self.save(self.payload(product,'NEW-1')).status_code,200)
        self.assertEqual(server.matcher.load_catalogue()['products'][-1],product)

    def test_stale_save_and_external_edit_preserved(self):
        stale=self.payload()
        external=copy.deepcopy(self.original);external['purpose']='Externally edited'
        self.catalogue.write_text(json.dumps(external))
        self.assertEqual(self.save(stale).status_code,409)
        self.assertEqual(json.loads(self.catalogue.read_text()),external)

    def test_duplicate_invalid_product_and_no_partial_write(self):
        before=self.catalogue.read_bytes()
        p=copy.deepcopy(self.original['products'][0])
        self.assertEqual(self.save(self.payload(p)).status_code,400)
        for update in [{'name':''},{'options':[{'name':'Missing code'}]},{'custom':float('nan')}]:
            body=self.payload();body['product'].update(update)
            self.assertEqual(self.save(body).status_code,400)
        self.assertEqual(self.catalogue.read_bytes(),before)

    def test_snapshot_unchanged_after_catalogue_edit(self):
        ident=self.service.store.create('saved request','model',self.original,self.owner)
        self.assertEqual(self.save(self.payload()).status_code,200)
        record=self.service.store.get(ident,self.owner,include_catalogue=True)
        self.assertEqual(record['catalogue'],self.original)

if __name__=='__main__':unittest.main()
