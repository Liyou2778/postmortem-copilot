-- MySQL 8.0 schema for Postmortem Copilot (RDS).
--
-- Translation decisions from the SQLite original:
--   * TEXT columns that are indexed become VARCHAR(64/191); utf8mb4 costs 4 bytes
--     per char, so a 191-char column is the practical limit for an index key.
--   * `events.payload`, `report.markdown` and the JSON blobs become MEDIUMTEXT:
--     reports run 4-8 KB and evidence payloads are nested JSON.
--   * SQLite REAL becomes DOUBLE.
--   * `idempotency`.`key` is a reserved word in MySQL, so it stays backticked
--     in every statement (SQLite also accepts backticks, so one spelling works
--     for both backends).
--   * No foreign keys: events are append-only and rows are never deleted, so the
--     cost of FK checks buys nothing here.

CREATE TABLE IF NOT EXISTS events (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    task_id    VARCHAR(64)  NOT NULL,
    seq        INT          NOT NULL,
    type       VARCHAR(64)  NOT NULL,
    payload    MEDIUMTEXT   NOT NULL,
    actor      VARCHAR(128) NOT NULL,
    created_at VARCHAR(40)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_events_task_seq (task_id, seq),
    KEY idx_events_task (task_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS idempotency (
    `key`      VARCHAR(191) NOT NULL,
    task_id    VARCHAR(64)  NOT NULL,
    step_id    VARCHAR(64)  NOT NULL,
    status     VARCHAR(16)  NOT NULL,
    result     MEDIUMTEXT   NULL,
    created_at VARCHAR(40)  NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS leases (
    task_id    VARCHAR(64)  NOT NULL,
    step_id    VARCHAR(64)  NOT NULL,
    owner      VARCHAR(128) NOT NULL,
    fence      INT          NOT NULL,
    expires_at BIGINT       NOT NULL,
    PRIMARY KEY (task_id, step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS compensations (
    id         VARCHAR(64)  NOT NULL,
    task_id    VARCHAR(64)  NOT NULL,
    step_id    VARCHAR(64)  NOT NULL,
    action     VARCHAR(64)  NOT NULL,
    payload    MEDIUMTEXT   NOT NULL,
    status     VARCHAR(16)  NOT NULL,
    attempts   INT          NOT NULL DEFAULT 0,
    last_error TEXT         NULL,
    created_at DOUBLE       NOT NULL,
    updated_at DOUBLE       NOT NULL,
    PRIMARY KEY (id),
    KEY idx_comp_task (task_id, status),
    KEY idx_comp_step (step_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS report (
    task_id    VARCHAR(64) NOT NULL,
    markdown   MEDIUMTEXT  NOT NULL,
    created_at DOUBLE      NOT NULL,
    PRIMARY KEY (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS step_output (
    task_id    VARCHAR(64) NOT NULL,
    step_id    VARCHAR(64) NOT NULL,
    role       VARCHAR(64) NOT NULL,
    payload    MEDIUMTEXT  NOT NULL,
    updated_at DOUBLE      NOT NULL,
    PRIMARY KEY (task_id, step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS task_evidence (
    task_id     VARCHAR(64) NOT NULL,
    evidence_id VARCHAR(64) NOT NULL,
    payload     MEDIUMTEXT  NOT NULL,
    PRIMARY KEY (task_id, evidence_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS archive (
    task_id    VARCHAR(64) NOT NULL,
    kind       VARCHAR(64) NOT NULL,
    object_key VARCHAR(255) NOT NULL,
    bytes      BIGINT      NOT NULL,
    created_at DOUBLE      NOT NULL,
    PRIMARY KEY (task_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- M8 service catalog: service → datasource bindings, with an explicit
-- real/mock label that the application enforces at write time.
CREATE TABLE IF NOT EXISTS datasource (
    name        VARCHAR(128) NOT NULL,
    kind        VARCHAR(32)  NOT NULL,
    base_url    VARCHAR(512) NOT NULL,
    mock        INT          NOT NULL DEFAULT 0,
    description VARCHAR(512) NULL,
    enabled     INT          NOT NULL DEFAULT 1,
    updated_at  DOUBLE       NOT NULL,
    updated_by  VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS service_catalog (
    name         VARCHAR(128)  NOT NULL,
    display_name VARCHAR(255)  NULL,
    owner        VARCHAR(128)  NULL,
    tier         VARCHAR(16)   NULL,
    description  VARCHAR(1024) NULL,
    enabled      INT           NOT NULL DEFAULT 1,
    updated_at   DOUBLE        NOT NULL,
    updated_by   VARCHAR(128)  NOT NULL DEFAULT 'system',
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS service_binding (
    service_name     VARCHAR(128)  NOT NULL,
    kind             VARCHAR(32)   NOT NULL,
    datasource_name  VARCHAR(128)  NOT NULL,
    selector         VARCHAR(1024) NULL,
    metric           VARCHAR(128)  NULL,
    secondary_metric VARCHAR(128)  NULL,
    mode             VARCHAR(16)   NOT NULL,
    enabled          INT           NOT NULL DEFAULT 1,
    updated_at       DOUBLE        NOT NULL,
    updated_by       VARCHAR(128)  NOT NULL DEFAULT 'system',
    PRIMARY KEY (service_name, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
