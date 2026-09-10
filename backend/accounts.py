import secrets
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from psycopg.errors import UniqueViolation

from config import settings
from database import pool
from security import COOKIE_NAME, digest, dummy_hash, password_hasher, team, throttle, user, verify_password

router = APIRouter()


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class Registration(Credentials):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    password: str = Field(min_length=15, max_length=128)
    bootstrap_token: str = Field(default="", max_length=128)
    invitation_token: str = Field(default="", max_length=128)


class TeamName(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")


class Invite(BaseModel):
    email: EmailStr


class Token(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=15, max_length=128)


def create_session(conn, response: Response, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (%s,%s,now() + %s * interval '1 hour')", (digest(token), user_id, settings.SESSION_HOURS))
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.COOKIE_SECURE, samesite="lax", max_age=settings.SESSION_HOURS*3600, path="/")


def accept_invitation(conn, token, current):
    invitation = conn.execute("SELECT * FROM invitations WHERE token_hash=%s AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now() FOR UPDATE", (digest(token),)).fetchone()
    if not invitation or invitation['email'] != current['email']:
        raise HTTPException(400, "Convite inválido, expirado ou destinado a outro e-mail.")
    conn.execute("INSERT INTO memberships(team_id,user_id,role) VALUES (%s,%s,'member') ON CONFLICT DO NOTHING", (invitation['team_id'], current['id']))
    conn.execute("UPDATE invitations SET accepted_at=now() WHERE id=%s", (invitation['id'],))


@router.post('/auth/register')
def register(data: Registration, request: Request, response: Response):
    throttle('register:' + (request.client.host if request.client else 'local'))
    email = str(data.email).lower()
    encoded = password_hasher.hash(data.password)
    try:
        with pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(72401933)")
            has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            bootstrap = not has_users and settings.BOOTSTRAP_TOKEN and secrets.compare_digest(data.bootstrap_token, settings.BOOTSTRAP_TOKEN)
            if not bootstrap and not data.invitation_token:
                raise HTTPException(403, "É necessário um convite para criar uma conta.")
            current = {'id': uuid4(), 'email': email, 'name': data.name.strip()}
            conn.execute("INSERT INTO users(id,email,name,password_hash) VALUES (%s,%s,%s,%s)", (current['id'], email, current['name'], encoded))
            if not bootstrap:
                accept_invitation(conn, data.invitation_token, current)
            create_session(conn, response, current['id'])
            return current
    except UniqueViolation:
        raise HTTPException(400, "Não foi possível criar a conta. Entre ou confira seu convite.")


@router.post('/auth/login')
def login(data: Credentials, request: Request, response: Response):
    email = str(data.email).lower()
    throttle('login:' + email)
    throttle('login-ip:' + (request.client.host if request.client else 'local'), 40)
    with pool.connection() as conn:
        current = conn.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone()
        if not verify_password(current['password_hash'] if current else dummy_hash, data.password):
            raise HTTPException(401, "E-mail ou senha inválidos.")
        if password_hasher.check_needs_rehash(current['password_hash']):
            conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hasher.hash(data.password), current['id']))
        create_session(conn, response, current['id'])
        return {'id':current['id'], 'email':current['email'], 'name':current['name']}


@router.get('/auth/me')
def me(request: Request):
    current = user(request)
    with pool.connection() as conn:
        teams = conn.execute("SELECT t.id,t.name,m.role FROM memberships m JOIN teams t ON t.id=m.team_id WHERE m.user_id=%s ORDER BY t.created_at", (current['id'],)).fetchall()
    return {'user':current, 'teams':teams}


@router.post('/auth/logout')
def logout(request: Request, response: Response):
    with pool.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=%s", (digest(request.cookies.get(COOKIE_NAME, '')),))
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.COOKIE_SECURE, httponly=True, samesite="lax")
    return {'ok':True}


@router.post('/auth/password')
def change_password(data: PasswordChange, request: Request, response: Response):
    current = user(request)
    throttle('password:' + str(current['id']))
    with pool.connection() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id=%s FOR UPDATE", (current['id'],)).fetchone()
        if not verify_password(row['password_hash'], data.current_password):
            raise HTTPException(400, "Senha atual inválida.")
        conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hasher.hash(data.new_password),current['id']))
        conn.execute("DELETE FROM sessions WHERE user_id=%s", (current['id'],))
        create_session(conn, response, current['id'])
    return {'ok':True}


@router.post('/teams')
def create_team(data: TeamName, request: Request):
    current = user(request)
    throttle('team:' + str(current['id']),20)
    team_id = uuid4()
    with pool.connection() as conn:
        conn.execute("INSERT INTO teams(id,name) VALUES (%s,%s)", (team_id,data.name.strip()))
        conn.execute("INSERT INTO memberships(team_id,user_id,role) VALUES (%s,%s,'admin')", (team_id,current['id']))
    return {'id':team_id,'name':data.name.strip(),'role':'admin'}


@router.get('/teams/members')
def members(request: Request):
    selected = team(request)
    with pool.connection() as conn:
        rows = conn.execute("SELECT u.id,u.name,u.email,m.role,(SELECT count(*) FROM extractions e WHERE e.team_id=m.team_id AND e.user_id=u.id) AS documents FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.team_id=%s ORDER BY u.name", (selected['team_id'],)).fetchall()
        invitations = conn.execute("SELECT id,email,expires_at FROM invitations WHERE team_id=%s AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>now() ORDER BY created_at", (selected['team_id'],)).fetchall() if selected['role']=='admin' else []
    return {'members':rows,'invitations':invitations}


@router.post('/teams/invitations')
def invite(data: Invite, request: Request):
    selected = team(request, admin=True)
    token = secrets.token_urlsafe(32)
    with pool.connection() as conn:
        conn.execute("UPDATE invitations SET revoked_at=now() WHERE team_id=%s AND email=%s AND accepted_at IS NULL", (selected['team_id'],str(data.email).lower()))
        conn.execute("INSERT INTO invitations(id,team_id,email,token_hash,created_by,expires_at) VALUES (%s,%s,%s,%s,%s,now()+interval '7 days')", (uuid4(),selected['team_id'],str(data.email).lower(),digest(token),user(request)['id']))
    return {'url':settings.APP_ORIGIN + '/?invite=' + token}


@router.post('/teams/invitations/accept')
def accept(data: Token, request: Request):
    with pool.connection() as conn:
        accept_invitation(conn, data.token, user(request))
    return {'ok':True}


@router.delete('/teams/invitations/{invitation_id}')
def revoke(invitation_id: UUID, request: Request):
    selected = team(request, admin=True)
    with pool.connection() as conn:
        conn.execute("UPDATE invitations SET revoked_at=now() WHERE id=%s AND team_id=%s", (invitation_id,selected['team_id']))
    return {'ok':True}


@router.delete('/teams/members/{user_id}')
def remove_member(user_id: UUID, request: Request):
    selected = team(request, admin=True)
    with pool.connection() as conn:
        removed = conn.execute("DELETE FROM memberships WHERE team_id=%s AND user_id=%s AND role='member' RETURNING user_id", (selected['team_id'],user_id)).fetchone()
    if not removed:
        raise HTTPException(400, "Administradores não podem ser removidos.")
    return {'ok':True}
