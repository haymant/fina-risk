CREATE TABLE IF NOT EXISTS riskcube_metadata (
  kind TEXT NOT NULL,
  record_key TEXT NOT NULL,
  payload JSONB NOT NULL,
  version_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, record_key)
);

CREATE INDEX IF NOT EXISTS riskcube_metadata_kind_created_idx
  ON riskcube_metadata (kind, created_at, record_key);

CREATE INDEX IF NOT EXISTS riskcube_metadata_reports_version_idx
  ON riskcube_metadata (kind, version_id)
  WHERE kind = 'reports';
