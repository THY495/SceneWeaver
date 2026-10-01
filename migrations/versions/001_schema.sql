CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	name VARCHAR(250) NOT NULL,
	version INTEGER NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	blob_path TEXT NOT NULL,
	status VARCHAR(20) NOT NULL,
	error TEXT,
	deleted BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_documents_owner_id ON documents (owner_id);

CREATE TABLE IF NOT EXISTS memories (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	session_id VARCHAR(36),
	subject VARCHAR(100) NOT NULL,
	predicate VARCHAR(100) NOT NULL,
	value TEXT NOT NULL,
	scope VARCHAR(100) NOT NULL,
	status VARCHAR(30) NOT NULL,
	source_ids JSONB NOT NULL,
	valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
	valid_to TIMESTAMP WITH TIME ZONE,
	supersedes VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_memories_owner_id ON memories (owner_id);

CREATE TABLE IF NOT EXISTS operations (
	id VARCHAR(200) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_operations_task_id ON operations (task_id);

CREATE TABLE IF NOT EXISTS sessions (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	title VARCHAR(160) NOT NULL,
	epoch INTEGER NOT NULL,
	seq INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	privacy_version INTEGER NOT NULL,
	current_state JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_sessions_owner_id ON sessions (owner_id);

CREATE TABLE IF NOT EXISTS tombstones (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	kind VARCHAR(30) NOT NULL,
	target_id VARCHAR(36) NOT NULL,
	source_ids JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_tombstones_owner_id ON tombstones (owner_id);

CREATE TABLE IF NOT EXISTS usage (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	role VARCHAR(30) NOT NULL,
	model VARCHAR(100) NOT NULL,
	input_tokens INTEGER NOT NULL,
	output_tokens INTEGER NOT NULL,
	cache_tokens INTEGER NOT NULL,
	cost FLOAT NOT NULL,
	reserved FLOAT NOT NULL,
	status VARCHAR(30) NOT NULL,
	duration_ms INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_usage_owner_id ON usage (owner_id);
CREATE INDEX IF NOT EXISTS ix_usage_task_id ON usage (task_id);

CREATE TABLE IF NOT EXISTS chunks (
	id VARCHAR(36) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	version INTEGER NOT NULL,
	ordinal INTEGER NOT NULL,
	section VARCHAR(250) NOT NULL,
	content TEXT NOT NULL,
	terms TEXT NOT NULL,
	search_vector TSVECTOR,
	embedding VECTOR(1024),
	embedding_model VARCHAR(100),
	PRIMARY KEY (id),
	FOREIGN KEY(document_id) REFERENCES documents (id)
)

;
CREATE INDEX IF NOT EXISTS ix_chunks_document_id ON chunks (document_id);
CREATE INDEX IF NOT EXISTS ix_chunks_search ON chunks USING gin (search_vector);

CREATE TABLE IF NOT EXISTS context_snapshots (
	id VARCHAR(36) NOT NULL,
	session_id VARCHAR(36) NOT NULL,
	covered_seq INTEGER NOT NULL,
	privacy_version INTEGER NOT NULL,
	summary TEXT NOT NULL,
	source_ids JSONB NOT NULL,
	status VARCHAR(20) NOT NULL,
	token_count INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES sessions (id)
)

;
CREATE INDEX IF NOT EXISTS ix_context_snapshots_session_id ON context_snapshots (session_id);

CREATE TABLE IF NOT EXISTS events (
	id SERIAL NOT NULL,
	session_id VARCHAR(36) NOT NULL,
	seq INTEGER NOT NULL,
	kind VARCHAR(40) NOT NULL,
	epoch INTEGER NOT NULL,
	task_id VARCHAR(36),
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_id, seq),
	FOREIGN KEY(session_id) REFERENCES sessions (id)
)

;
CREATE INDEX IF NOT EXISTS ix_events_session_id ON events (session_id);

CREATE TABLE IF NOT EXISTS messages (
	id VARCHAR(36) NOT NULL,
	session_id VARCHAR(36) NOT NULL,
	client_id VARCHAR(80),
	task_id VARCHAR(36),
	seq INTEGER NOT NULL,
	role VARCHAR(20) NOT NULL,
	content TEXT NOT NULL,
	redacted BOOLEAN NOT NULL,
	delivered BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_id, client_id),
	UNIQUE (task_id, role),
	FOREIGN KEY(session_id) REFERENCES sessions (id)
)

;
CREATE INDEX IF NOT EXISTS ix_messages_session_id ON messages (session_id);
CREATE INDEX IF NOT EXISTS ix_messages_task_id ON messages (task_id);

CREATE TABLE IF NOT EXISTS tasks (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	session_id VARCHAR(36),
	parent_id VARCHAR(36),
	kind VARCHAR(30) NOT NULL,
	status VARCHAR(20) NOT NULL,
	epoch INTEGER NOT NULL,
	priority INTEGER NOT NULL,
	payload JSONB NOT NULL,
	result JSONB NOT NULL,
	error TEXT,
	lease_owner VARCHAR(80),
	lease_version INTEGER NOT NULL,
	lease_until TIMESTAMP WITH TIME ZONE,
	attempt INTEGER NOT NULL,
	calls INTEGER NOT NULL,
	deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	dedupe_key VARCHAR(200),
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES sessions (id),
	UNIQUE (dedupe_key)
)

;
CREATE INDEX IF NOT EXISTS ix_tasks_kind ON tasks (kind);
CREATE INDEX IF NOT EXISTS ix_tasks_owner_id ON tasks (owner_id);
CREATE INDEX IF NOT EXISTS ix_tasks_parent_id ON tasks (parent_id);
CREATE INDEX IF NOT EXISTS ix_tasks_session_id ON tasks (session_id);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (status);

CREATE TABLE IF NOT EXISTS evidence (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(80) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	chunk_id VARCHAR(36) NOT NULL,
	document_version INTEGER NOT NULL,
	excerpt TEXT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(chunk_id) REFERENCES chunks (id)
)

;
CREATE INDEX IF NOT EXISTS ix_evidence_owner_id ON evidence (owner_id);
CREATE INDEX IF NOT EXISTS ix_evidence_task_id ON evidence (task_id);
