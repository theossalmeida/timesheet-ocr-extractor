import getpass
import sys

from database import pool
from security import password_hasher


def main():
    email = sys.argv[1].strip().lower()
    password = getpass.getpass('New password (at least 8 characters): ')
    if not 8<=len(password)<=128 or password!=getpass.getpass('Repeat password: '):
        raise SystemExit('Passwords must match and contain 8–128 characters.')
    pool.open()
    try:
        with pool.connection() as conn:
            current = conn.execute('UPDATE users SET password_hash=%s WHERE email=%s RETURNING id',(password_hasher.hash(password),email)).fetchone()
            if not current:
                raise SystemExit('Account not found.')
            conn.execute('DELETE FROM sessions WHERE user_id=%s',(current['id'],))
    finally:
        pool.close()
    print('Password changed and all sessions revoked.')


if __name__=='__main__':
    main()
