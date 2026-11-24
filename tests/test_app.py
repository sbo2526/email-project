import os
import tempfile
import unittest
from unittest.mock import patch

folder = tempfile.TemporaryDirectory()
os.environ['DATA_DIR'] = folder.name
os.environ['ADMIN_PASSWORD'] = 'strong-test-password'
os.environ['APP_SECRET'] = 'test-secret-at-least-32-characters-long'
import app


def provider(method, path, payload=None, token=None):
    if path == '/domains':
        return {'hydra:member': [{'domain':'example.test','isActive':True,'isPrivate':False}]}
    if path == '/accounts':
        return {'id':'remote-id'}
    if path == '/token':
        return {'token':'remote-token'}
    if path.startswith('/messages?'):
        return {'hydra:member':[{'id':'abc123','subject':'hello','from':{'address':'a@example.org'},'intro':'Hi'}]}
    if path == '/messages/abc123':
        return {'subject':'hello','from':{'address':'a@example.org'},'html':['<p>safe</p><script>alert(1)</script>']}
    raise AssertionError(path)


class AppTest(unittest.TestCase):
    def setUp(self):
        with app.db() as con: con.execute('DELETE FROM mailboxes')
        self.client = app.app.test_client()
        self.csrf = self.client.get('/api/state').json['csrf']
    def post(self, path, data):
        return self.client.post(path, json=data, headers={'X-CSRF-Token':self.csrf})
    def login(self):
        result = self.post('/api/login', {'password':'strong-test-password'})
        self.assertEqual(result.status_code, 200)
        self.csrf = result.json['csrf']
    def test_security_and_mailbox_flow(self):
        self.assertEqual(self.client.get('/api/mailboxes').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'password':'strong-test-password'}).status_code,403)
        self.login()
        with patch('app.api', side_effect=provider):
            result = self.post('/api/mailboxes', {'username':'sample'})
            self.assertEqual(result.status_code,201)
            mailbox_id=result.json['id']
            self.assertEqual(result.json['address'],'sample@example.test')
            creds=self.client.get(f'/api/mailboxes/{mailbox_id}/credentials').json
            self.assertTrue(creds['password'])
            with app.db() as con:
                encrypted=con.execute('SELECT password_encrypted FROM mailboxes').fetchone()[0]
                self.assertNotIn(creds['password'].encode(), encrypted)
            self.assertEqual(len(self.client.get('/api/mailboxes').json),1)
            self.assertEqual(self.client.get(f'/api/mailboxes/{mailbox_id}/messages').json['messages'][0]['subject'],'hello')
            detail=self.client.get(f'/api/mailboxes/{mailbox_id}/messages/abc123').json
            self.assertIn('safe',detail['text'])
            self.assertNotIn('alert',detail['text'])
            self.assertEqual(self.client.delete(f'/api/mailboxes/{mailbox_id}', headers={'X-CSRF-Token':self.csrf}).status_code,200)
            self.assertEqual(self.client.get('/api/mailboxes').json,[])
    def test_invalid_username(self):
        self.login()
        self.assertEqual(self.post('/api/mailboxes', {'username':'bad @ name'}).status_code,400)
    def test_auto_generated_realistic_name(self):
        self.login()
        with patch('app.api', side_effect=provider):
            result = self.post('/api/mailboxes', {})
            self.assertEqual(result.status_code,201)
            local = result.json['address'].split('@')[0]
            self.assertRegex(local, r'^[a-z][a-z0-9._-]{2,23}$')
            self.assertFalse(local.startswith('mail'))
    def test_auto_name_retries_when_taken(self):
        self.login()
        state = {'calls': 0}
        def flaky(method, path, payload=None, token=None):
            if path == '/accounts':
                state['calls'] += 1
                if state['calls'] == 1:
                    raise app.ProviderError('taken', 400)
            return provider(method, path, payload, token)
        with patch('app.api', side_effect=flaky):
            result = self.post('/api/mailboxes', {})
        self.assertEqual(result.status_code,201)
        self.assertEqual(state['calls'],2)
    def test_user_chosen_taken_name_not_replaced(self):
        self.login()
        def always_taken(method, path, payload=None, token=None):
            if path == '/domains':
                return provider(method, path)
            raise app.ProviderError('taken', 400)
        with patch('app.api', side_effect=always_taken):
            result = self.post('/api/mailboxes', {'username':'sample'})
        self.assertEqual(result.status_code,400)
    def test_remote_failure(self):
        self.login()
        with patch('app.api', side_effect=app.ProviderError('limited',429)):
            self.assertEqual(self.post('/api/mailboxes', {}).status_code,429)

if __name__ == '__main__': unittest.main()
