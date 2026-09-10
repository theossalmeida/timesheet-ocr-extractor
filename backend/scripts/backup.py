import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

from config import settings


def main():
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    connection = urlparse(settings.DATABASE_URL)
    environment = os.environ.copy()
    environment.update(PGHOST=connection.hostname.replace('-pooler.','.'),PGPORT=str(connection.port or 5432),PGDATABASE=connection.path.lstrip('/'),PGUSER=unquote(connection.username or ''),PGPASSWORD=unquote(connection.password or ''),PGSSLMODE='verify-full',PGSSLROOTCERT='system')
    destination = directory/('autus-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.dump')
    descriptor = os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(descriptor,'wb') as output:
            subprocess.run(['pg_dump','--format=custom','--no-owner','--no-acl','--schema=public'],env=environment,stdout=output,check=True)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print(destination)


if __name__=='__main__':
    main()
