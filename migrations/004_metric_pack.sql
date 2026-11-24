-- M10.1: metric pack + gauge/counter semantics.
--
-- `extra_metrics` is a JSON array, e.g.
--   [{"metric":"node_disk_io_time_seconds_total","type":"counter","label":"磁盘 IO"}]
--
-- The application also auto-adds missing columns at startup, so this file is
-- for DBAs who want an explicit migration. Idempotent only when the columns are
-- absent; run once.

ALTER TABLE service_binding
    ADD COLUMN metric_type VARCHAR(16) NOT NULL DEFAULT 'gauge' AFTER secondary_metric,
    ADD COLUMN secondary_metric_type VARCHAR(16) NOT NULL DEFAULT 'gauge' AFTER metric_type,
    ADD COLUMN extra_metrics MEDIUMTEXT NULL AFTER secondary_metric_type;
