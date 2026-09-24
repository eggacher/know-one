-- 为已用旧版 schema 初始化的开发库补齐异步入库所需的来源快照。
-- 生产库应先确认没有 queued/running 旧任务，再执行本迁移。
ALTER TABLE ingestion_job ADD COLUMN source_bytes bytea;
ALTER TABLE ingestion_job ADD COLUMN source_media_type text;
ALTER TABLE ingestion_job ADD COLUMN source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ingestion_job ADD COLUMN content_hash text;

-- 新提交的任务必须具备完整快照；旧任务由运营确认后处理，避免静默伪造内容。
ALTER TABLE ingestion_job
    ADD CONSTRAINT ingestion_job_source_snapshot_complete CHECK (
        source_bytes IS NOT NULL
        AND source_media_type IS NOT NULL
        AND content_hash IS NOT NULL
    ) NOT VALID;

ALTER TABLE document_revision
    ADD CONSTRAINT document_revision_document_content_hash_key
    UNIQUE (document_id, content_hash);
