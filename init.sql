CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    extracted_text TEXT,
    confidence     FLOAT,
    submitted_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_submitted_at ON jobs (submitted_at DESC);
