CREATE TABLE users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('operator','reviewer'))
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    owner_id bigint NOT NULL REFERENCES users,
    status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED','DELETING','EXPIRED')),
    progress smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    stage text NOT NULL DEFAULT 'В очереди',
    protocol_start integer NOT NULL, protocol_end integer NOT NULL, fio_column integer,
    expiry_date date NOT NULL, qualification text NOT NULL,
    pipeline_version text NOT NULL DEFAULT 'notebook-cpu-v1',
    revision integer NOT NULL DEFAULT 1,
    published_revision integer NOT NULL DEFAULT 0,
    warnings jsonb NOT NULL DEFAULT '[]',
    error text, created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
    expires_at timestamptz, last_download_requested_at timestamptz, cleaned_at timestamptz,
    next_dispatch_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_owner ON jobs(owner_id, created_at DESC);
CREATE INDEX jobs_expiry ON jobs(expires_at) WHERE cleaned_at IS NULL;
CREATE TABLE documents (
    job_id uuid PRIMARY KEY REFERENCES jobs, filename text NOT NULL, source_relative_path text NOT NULL,
    sha256 char(64) NOT NULL, page_count integer NOT NULL
);
CREATE TABLE pages (
    job_id uuid NOT NULL REFERENCES documents, page_number integer NOT NULL,
    width integer NOT NULL, height integer NOT NULL, dpi integer NOT NULL,
    rotation integer NOT NULL, kernel jsonb, preview_relative_path text,
    PRIMARY KEY(job_id,page_number)
);
CREATE TABLE protocol_rows (
    job_id uuid NOT NULL REFERENCES jobs, row_no integer NOT NULL, page_number integer NOT NULL,
    kind text NOT NULL, raw jsonb NOT NULL, fio text, needs_review boolean NOT NULL DEFAULT false,
    PRIMARY KEY(job_id,row_no)
);
CREATE TABLE cards (
    job_id uuid NOT NULL, card_no integer NOT NULL, page_number integer NOT NULL,
    filename text NOT NULL, bbox jsonb NOT NULL, detected_xywh jsonb NOT NULL,
    warnings jsonb NOT NULL, crop_relative_path text NOT NULL,
    manual_row_no integer, PRIMARY KEY(job_id,card_no),
    FOREIGN KEY(job_id,page_number) REFERENCES pages,
    FOREIGN KEY(job_id,manual_row_no) REFERENCES protocol_rows,
    UNIQUE(job_id,manual_row_no)
);
CREATE TABLE matches (
    job_id uuid NOT NULL, card_no integer NOT NULL, row_no integer, fio text, score real NOT NULL,
    needs_review boolean NOT NULL, output_file text NOT NULL, PRIMARY KEY(job_id,card_no),
    FOREIGN KEY(job_id,card_no) REFERENCES cards, FOREIGN KEY(job_id,row_no) REFERENCES protocol_rows,
    UNIQUE(job_id,row_no)
);
CREATE TABLE annotations (
    job_id uuid NOT NULL, card_no integer NOT NULL, geometry jsonb NOT NULL,
    source text NOT NULL, status text NOT NULL, version integer NOT NULL,
    annotator bigint REFERENCES users, verified_at timestamptz,
    PRIMARY KEY(job_id,card_no), FOREIGN KEY(job_id,card_no) REFERENCES cards
);
CREATE TABLE corrections (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs,
    card_no integer NOT NULL, revision integer NOT NULL, author_id bigint NOT NULL REFERENCES users,
    before_value jsonb NOT NULL, after_value jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE artifacts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs,
    revision integer NOT NULL, kind text NOT NULL, relative_path text NOT NULL,
    media_type text NOT NULL, sha256 char(64) NOT NULL, deleted_at timestamptz,
    UNIQUE(job_id,revision,kind)
);
CREATE TABLE download_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs,
    user_id bigint NOT NULL REFERENCES users, artifact_id bigint NOT NULL REFERENCES artifacts,
    requested_at timestamptz NOT NULL DEFAULT now(), download_lease_until timestamptz
);
CREATE TABLE datasets (
    id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs, revision integer NOT NULL,
    approved_by bigint NOT NULL REFERENCES users, storage_basis text NOT NULL,
    source_relative_path text NOT NULL, annotations_relative_path text NOT NULL,
    sha256 char(64) NOT NULL, annotations jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
