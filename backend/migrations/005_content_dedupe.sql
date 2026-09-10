ALTER TABLE extractions ADD COLUMN content_hash text;
CREATE INDEX extractions_dedupe ON extractions(team_id,mode,content_hash) WHERE status='done' AND content_hash IS NOT NULL;
