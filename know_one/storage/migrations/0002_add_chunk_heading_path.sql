-- 为已用旧版 schema 初始化的开发库补齐 Markdown 标题路径列。
-- 全新初始化的库由 schema.sql 直接携带该列，无需执行本迁移。
ALTER TABLE chunk ADD COLUMN heading_path text[] NOT NULL DEFAULT '{}';
