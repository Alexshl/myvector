CREATE TABLE nodes (
  name TEXT PRIMARY KEY,
  created_at INTEGER,
  last_seen_at INTEGER,
  last_gen INTEGER,
  last_snapshot_at INTEGER,
  myvec_version TEXT
);
CREATE TABLE node_tokens (
  token_hash TEXT PRIMARY KEY,
  node TEXT NOT NULL REFERENCES nodes(name) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  label TEXT
);
CREATE INDEX node_tokens_node ON node_tokens(node);
CREATE TABLE node_projects (
  node TEXT NOT NULL,
  project TEXT NOT NULL,
  fingerprint TEXT,
  head_sha TEXT,
  head_time INTEGER,
  head_branch TEXT,
  dirty INTEGER,
  tickets_mtime INTEGER,
  received_at INTEGER,
  PRIMARY KEY (node, project)
);
CREATE TABLE tickets (
  node TEXT NOT NULL,
  project TEXT NOT NULL,
  id TEXT NOT NULL,
  kind TEXT,
  status TEXT,
  parent TEXT,
  data TEXT NOT NULL,
  PRIMARY KEY (node, project, id)
);
CREATE TABLE sessions (
  node TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT,
  data TEXT NOT NULL,
  PRIMARY KEY (node, session_id)
);
