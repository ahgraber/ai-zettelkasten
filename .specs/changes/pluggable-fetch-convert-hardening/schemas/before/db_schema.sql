-- Post-migration DB schema: c1d2e3f4a5b6 (bookmarks → sources)
-- Generated from: uv run alembic upgrade head on SQLite

CREATE TABLE sources (
	id INTEGER NOT NULL,
	karakeep_id VARCHAR(255),
	aizk_uuid CHAR(32) NOT NULL,
	source_ref TEXT,
	source_ref_hash TEXT,
	url VARCHAR,
	normalized_url VARCHAR,
	title VARCHAR(500),
	content_type VARCHAR(10),
	source_type VARCHAR(20),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_sources_aizk_uuid ON sources (aizk_uuid);
CREATE UNIQUE INDEX ix_sources_karakeep_id ON sources (karakeep_id);
CREATE INDEX ix_sources_normalized_url ON sources (normalized_url);
CREATE UNIQUE INDEX ix_sources_source_ref_hash ON sources (source_ref_hash);

CREATE TABLE conversion_jobs (
	id INTEGER NOT NULL,
	aizk_uuid CHAR(32) NOT NULL,
	title VARCHAR(500) NOT NULL,
	payload_version INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	error_code VARCHAR(50),
	error_message TEXT,
	idempotency_key VARCHAR(64) NOT NULL,
	earliest_next_attempt_at DATETIME,
	last_error_at DATETIME,
	queued_at DATETIME,
	started_at DATETIME,
	finished_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	error_detail TEXT,
	source_ref TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(aizk_uuid) REFERENCES sources (aizk_uuid)
);
CREATE INDEX ix_conversion_jobs_aizk_uuid ON conversion_jobs (aizk_uuid);
CREATE INDEX ix_conversion_jobs_created_at ON conversion_jobs (created_at);
CREATE INDEX ix_conversion_jobs_earliest_next_attempt_at ON conversion_jobs (earliest_next_attempt_at);
CREATE UNIQUE INDEX ix_conversion_jobs_idempotency_key ON conversion_jobs (idempotency_key);
CREATE INDEX ix_conversion_jobs_status ON conversion_jobs (status);
CREATE INDEX ix_conversion_jobs_status_next_attempt_queued ON conversion_jobs (status, earliest_next_attempt_at, queued_at);

CREATE TABLE conversion_outputs (
	id INTEGER NOT NULL,
	job_id INTEGER NOT NULL,
	aizk_uuid CHAR(32) NOT NULL,
	title VARCHAR(500) NOT NULL,
	payload_version INTEGER NOT NULL,
	s3_prefix TEXT NOT NULL,
	markdown_key TEXT NOT NULL,
	manifest_key TEXT NOT NULL,
	markdown_hash_xx64 VARCHAR(16) NOT NULL,
	figure_count INTEGER NOT NULL,
	docling_version VARCHAR(20) NOT NULL,
	pipeline_name VARCHAR(50) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(aizk_uuid) REFERENCES sources (aizk_uuid),
	FOREIGN KEY(job_id) REFERENCES conversion_jobs (id)
);
CREATE INDEX ix_conversion_outputs_aizk_uuid ON conversion_outputs (aizk_uuid);
CREATE INDEX ix_conversion_outputs_created_at ON conversion_outputs (created_at);
CREATE UNIQUE INDEX ix_conversion_outputs_job_id ON conversion_outputs (job_id);
CREATE INDEX ix_conversion_outputs_markdown_hash_xx64 ON conversion_outputs (markdown_hash_xx64);
