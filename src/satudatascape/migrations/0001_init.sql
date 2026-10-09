-- 0001_init (S4): catalogue tables, SPEC §4 plus runs.status / runs.error.

CREATE TABLE packages (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    title TEXT,
    org_id TEXT,
    org_name TEXT,
    org_type TEXT,
    prioritas_years TEXT,
    harvest_source_id TEXT,
    harvest_source_title TEXT,
    metadata_created TEXT,
    metadata_modified TEXT,
    num_resources INT,
    private INT,
    state TEXT,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT,
    last_seen_at TEXT,
    deleted_at TEXT
);
CREATE INDEX packages_metadata_modified ON packages(metadata_modified);

CREATE TABLE resources (
    id TEXT PRIMARY KEY,
    package_id TEXT REFERENCES packages(id),
    name TEXT,
    url TEXT,
    url_host TEXT,
    format_raw TEXT,
    format_norm TEXT,
    metadata_modified TEXT,
    last_modified TEXT
);
CREATE INDEX resources_package_id ON resources(package_id);

CREATE TABLE organizations (
    id TEXT PRIMARY KEY,
    name TEXT,
    title TEXT,
    raw_json TEXT
);

CREATE TABLE package_versions (
    package_id TEXT,
    content_hash TEXT,
    raw_json TEXT,
    seen_at TEXT,
    PRIMARY KEY (package_id, content_hash)
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    kind TEXT,
    started_at TEXT,
    finished_at TEXT,
    count_search INT,
    count_list INT,
    count_stored INT,
    upserts INT,
    changed INT,
    errors INT,
    checkpoint TEXT,
    notes TEXT,
    status TEXT,
    error TEXT
);
