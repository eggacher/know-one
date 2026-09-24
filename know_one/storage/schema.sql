-- docs/architecture.md 对应的 PostgreSQL 初始表结构，仅用于空库初始化。
-- 已有数据的结构升级需要版本化迁移，不能重复执行此文件。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE namespace (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    current_index_generation_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE index_generation (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES namespace(id),
    config_fingerprint text NOT NULL,
    embedding_model text NOT NULL,
    tokenizer_version text NOT NULL,
    dims integer NOT NULL CHECK (dims > 0),
    distance text NOT NULL CHECK (distance IN ('cosine', 'l2', 'inner_product')),
    status text NOT NULL CHECK (status IN ('building', 'active', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace_id, id)
);

-- 当前索引代必须属于同一个 Namespace；允许为空以支持首次建库。
ALTER TABLE namespace ADD CONSTRAINT namespace_current_index_generation_fk
    FOREIGN KEY (id, current_index_generation_id)
    REFERENCES index_generation(namespace_id, id)
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE TABLE document (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES namespace(id),
    source_key text NOT NULL,
    acl jsonb NOT NULL DEFAULT '{}'::jsonb,
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    withdrawn boolean NOT NULL DEFAULT false,
    deleted_at timestamptz,
    state_generation bigint NOT NULL DEFAULT 0 CHECK (state_generation >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace_id, source_key),
    UNIQUE (namespace_id, id)
);

CREATE TABLE document_revision (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES document(id),
    content_hash text NOT NULL,
    content_ref text NOT NULL,
    source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, id),
    -- 同一 Document 的相同内容复用同一 Revision；不能跨 Document 去重。
    UNIQUE (document_id, content_hash)
);

CREATE TABLE publication (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES document(id),
    revision_id uuid NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_until timestamptz,
    published_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_until IS NULL OR valid_from < valid_until),
    FOREIGN KEY (document_id, revision_id)
        REFERENCES document_revision(document_id, id),
    -- 同一文档的业务生效窗口不能重叠，边界采用左闭右开。
    EXCLUDE USING gist (
        document_id WITH =,
        tstzrange(valid_from, valid_until, '[)') WITH &&
    )
);

CREATE TABLE revision_index_build (
    revision_id uuid NOT NULL REFERENCES document_revision(id),
    index_generation_id uuid NOT NULL REFERENCES index_generation(id),
    status text NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    expected_chunk_count integer CHECK (expected_chunk_count >= 0),
    completed_chunk_count integer NOT NULL DEFAULT 0 CHECK (completed_chunk_count >= 0),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (revision_id, index_generation_id),
    -- 只有块数已校验且记录完成时间，构建状态才可以转为 ready。
    CHECK (status <> 'ready' OR (
        expected_chunk_count IS NOT NULL
        AND completed_chunk_count = expected_chunk_count
        AND completed_at IS NOT NULL
    ))
);

CREATE TABLE chunk (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    index_generation_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    raw_text text NOT NULL,
    search_text text NOT NULL,
    heading_path text[] NOT NULL DEFAULT '{}',
    source_locator jsonb NOT NULL,
    -- 向量维度由所属 IndexGeneration 定义；当前 schema 只建通用列。
    embedding vector,
    tsv tsvector,
    FOREIGN KEY (revision_id, index_generation_id)
        REFERENCES revision_index_build(revision_id, index_generation_id),
    FOREIGN KEY (namespace_id, index_generation_id)
        REFERENCES index_generation(namespace_id, id),
    UNIQUE (revision_id, index_generation_id, ordinal)
);

CREATE TABLE ingestion_job (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES namespace(id),
    source_key text NOT NULL,
    source_ref text NOT NULL,
    -- 入库时固定来源快照，保证异步 worker 不依赖临时文件或外部 URL。
    source_bytes bytea NOT NULL,
    source_media_type text NOT NULL,
    source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_hash text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('queued', 'running', 'ready', 'failed', 'needs_review', 'cancelled')
    ),
    stage text,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_expires_at timestamptz,
    error_code text,
    result_revision_id uuid REFERENCES document_revision(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace_id, idempotency_key)
);

CREATE TABLE operation_receipt (
    id uuid PRIMARY KEY,
    namespace_id uuid NOT NULL REFERENCES namespace(id),
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    result_ref jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    -- 不同操作可以复用同一个调用方幂等键，同一操作的重试必须命中原回执。
    UNIQUE (namespace_id, operation, idempotency_key)
);

CREATE TABLE audit_event (
    id uuid PRIMARY KEY,
    actor text NOT NULL,
    object_type text NOT NULL,
    object_id text NOT NULL,
    before_state jsonb,
    after_state jsonb,
    trace_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX chunk_fts_idx ON chunk USING gin (tsv);
CREATE INDEX chunk_build_idx ON chunk (revision_id, index_generation_id);
CREATE INDEX publication_at_idx ON publication (document_id, valid_from);
CREATE INDEX ingestion_job_claim_idx ON ingestion_job (status, lease_expires_at, created_at);

-- revision 通过 document 间接归属 Namespace，普通外键无法跨这两层核对；
-- 写入构建状态和 Chunk 时再检查，避免把其他 Namespace 的内容混入索引。
CREATE FUNCTION check_revision_index_namespace() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    revision_namespace uuid;
    generation_namespace uuid;
BEGIN
    SELECT d.namespace_id INTO revision_namespace
    FROM document_revision AS r JOIN document AS d ON d.id = r.document_id
    WHERE r.id = NEW.revision_id;
    SELECT g.namespace_id INTO generation_namespace
    FROM index_generation AS g WHERE g.id = NEW.index_generation_id;
    IF revision_namespace IS DISTINCT FROM generation_namespace THEN
        RAISE EXCEPTION 'revision and index generation must share a namespace';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER revision_index_build_namespace_check
BEFORE INSERT OR UPDATE OF revision_id, index_generation_id
ON revision_index_build FOR EACH ROW
EXECUTE FUNCTION check_revision_index_namespace();

CREATE FUNCTION check_chunk_namespace() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    revision_namespace uuid;
BEGIN
    SELECT d.namespace_id INTO revision_namespace
    FROM document_revision AS r JOIN document AS d ON d.id = r.document_id
    WHERE r.id = NEW.revision_id;
    IF revision_namespace IS DISTINCT FROM NEW.namespace_id THEN
        RAISE EXCEPTION 'chunk and revision must share a namespace';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER chunk_namespace_check
BEFORE INSERT OR UPDATE OF namespace_id, revision_id, index_generation_id
ON chunk FOR EACH ROW
EXECUTE FUNCTION check_chunk_namespace();
