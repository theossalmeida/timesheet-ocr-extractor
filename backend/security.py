import hashlib
import secrets
from datetime import datetime, timezone
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request

from database import pool

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))
COOKIE_NAME = "autus_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def verify_password(encoded: str, password: str) -> bool:
    try:
        return password_hasher.verify(encoded, password)
    except VerificationError:
        return False


def authenticate(token: str):
    if not token or len(token) > 128:
        return None
    with pool.connection() as conn:
        return conn.execute("SELECT u.id, u.email, u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at > now()", (digest(token),)).fetchone()


def user(request: Request):
    value = getattr(request.state, "user", None)
    if not value:
        raise HTTPException(401, "Entre na sua conta para continuar.")
    return value


def team(request: Request, admin: bool = False):
    current = user(request)
    try:
        team_id = UUID(request.headers.get("x-team-id", ""))
    except ValueError:
        raise HTTPException(400, "Selecione uma equipe.")
    with pool.connection() as conn:
        member = conn.execute("SELECT team_id, role FROM memberships WHERE team_id=%s AND user_id=%s", (team_id, current['id'])).fetchone()
    if not member or (admin and member['role'] != 'admin'):
        raise HTTPException(403, "Você não tem acesso a esta operação.")
    return member


def throttle(key: str, limit: int = 10):
    with pool.connection() as conn:
        conn.execute("DELETE FROM auth_attempts WHERE window_start < now() - interval '1 day'")
        row = conn.execute("INSERT INTO auth_attempts(key,attempts) VALUES (%s,1) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN auth_attempts.window_start < now()-interval '15 minutes' THEN 1 ELSE auth_attempts.attempts+1 END, window_start=CASE WHEN auth_attempts.window_start < now()-interval '15 minutes' THEN now() ELSE auth_attempts.window_start END RETURNING attempts", (digest(key),)).fetchone()
    if row['attempts'] > limit:
        raise HTTPException(429, "Muitas tentativas. Aguarde 15 minutos.")
