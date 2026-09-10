from database import pool, migrate
import storage


def main():
    pool.open()
    try:
        migrate()
        migrated = 0
        while True:
            with pool.connection() as conn:
                row = conn.execute('SELECT a.id,a.extraction_id,a.kind,a.mime_type,a.content,e.team_id FROM artifacts a JOIN extractions e ON e.id=a.extraction_id WHERE a.object_key IS NULL AND a.content IS NOT NULL LIMIT 1 FOR UPDATE OF a SKIP LOCKED').fetchone()
                if not row:
                    break
                folder = 'raw_files' if row['kind']=='original' else 'processed_files'
                key = storage.object_key(folder,row['team_id'],row['extraction_id'],str(row['id']))
                storage.put(key,bytes(row['content']),row['mime_type'])
                conn.execute('UPDATE artifacts SET object_key=%s,content=NULL WHERE id=%s',(key,row['id']))
                migrated += 1
        print('Artifacts migrated to R2:',migrated)
    finally:
        pool.close()


if __name__=='__main__':
    main()
