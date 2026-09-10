from fastapi.testclient import TestClient

from database import pool
from main import app


def test_auth_csrf_and_bootstrap(owner):
    stranger = TestClient(app)
    assert stranger.get('/auth/me').status_code == 401
    assert stranger.post('/auth/login', json={'email':'owner@example.com','password':'a long secure password'}).status_code == 403
    assert stranger.post('/auth/login', headers={'x-autus-request':'1','origin':'https://evil.example'}, json={'email':'owner@example.com','password':'a long secure password'}).status_code == 403
    assert stranger.post('/auth/register', headers={'x-autus-request':'1'}, json={'email':'other@example.com','name':'Other','password':'a long secure password','bootstrap_token':'test-bootstrap-private-token'}).status_code == 403
    assert stranger.post('/extract', headers={'x-autus-request':'1'}).status_code == 401


def test_invitation_membership_and_revocation(owner):
    result = owner.post('/teams/invitations',json={'email':'member@example.com'})
    assert result.status_code == 200
    token = result.json()['url'].split('invite=')[1]
    member = TestClient(app,headers={'x-autus-request':'1'})
    body = {'email':'wrong@example.com','name':'Member','password':'another long password','invitation_token':token}
    assert member.post('/auth/register',json=body).status_code == 400
    body['email'] = 'member@example.com'
    result = member.post('/auth/register',json=body)
    assert result.status_code == 200, result.text
    member_id = result.json()['id']
    member.headers['x-team-id'] = owner.headers['x-team-id']
    assert member.get('/teams/members').status_code == 200
    assert member.post('/teams/invitations',json={'email':'third@example.com'}).status_code == 403
    assert member.post('/teams/invitations/accept',json={'token':token}).status_code == 400
    own_team = member.post('/teams',json={'name':'Separate team'}).json()['id']
    assert owner.get('/teams/members',headers={'x-team-id':own_team}).status_code == 403
    assert owner.delete('/teams/members/'+member_id).status_code == 200
    assert member.get('/teams/members').status_code == 403
    assert member.post('/auth/logout').status_code == 200
    assert member.get('/auth/me').status_code == 401


def test_password_change_revokes_sessions(owner):
    client = TestClient(app,headers={'x-autus-request':'1'})
    response = client.post('/auth/login',json={'email':'owner@example.com','password':'a long secure password'})
    assert response.status_code == 200
    assert 'httponly' in response.headers['set-cookie'].lower()
    current = owner.get('/auth/me').json()['user']['id']
    assert owner.delete('/teams/members/'+current).status_code == 400
    assert client.post('/auth/password',json={'current_password':'wrong','new_password':'updated long password'}).status_code == 400
    assert client.post('/auth/logout').status_code == 200
    assert client.get('/auth/me').status_code == 401


def test_expired_invite(owner):
    token = owner.post('/teams/invitations',json={'email':'expired@example.com'}).json()['url'].split('invite=')[1]
    with pool.connection() as conn:
        conn.execute("UPDATE invitations SET expires_at=now()-interval '1 second' WHERE email='expired@example.com'")
    client = TestClient(app,headers={'x-autus-request':'1'})
    assert client.post('/auth/register',json={'email':'expired@example.com','name':'Expired','password':'another long password','invitation_token':token}).status_code == 400
