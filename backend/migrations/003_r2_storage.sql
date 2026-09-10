ALTER TABLE artifacts ADD COLUMN object_key text UNIQUE;
ALTER TABLE artifacts ALTER COLUMN content DROP NOT NULL;
ALTER TABLE artifacts ADD CONSTRAINT artifact_storage CHECK (object_key IS NOT NULL OR content IS NOT NULL);
ALTER TABLE upload_parts ADD COLUMN object_key text UNIQUE;
ALTER TABLE upload_parts ALTER COLUMN content DROP NOT NULL;
ALTER TABLE upload_parts ADD CONSTRAINT upload_part_storage CHECK (object_key IS NOT NULL OR content IS NOT NULL);
