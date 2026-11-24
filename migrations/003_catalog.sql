-- M8 migration: service catalog.
--
-- The application also creates these tables at startup (CREATE TABLE IF NOT
-- EXISTS), so this file is for DBAs who prefer an explicit migration step.
-- Idempotent: safe to re-run on an existing database.

CREATE TABLE IF NOT EXISTS datasource (
    name        VARCHAR(128) NOT NULL,
    kind        VARCHAR(32)  NOT NULL,   -- prometheus | loki | cmdb | deploy | kb | multi
    base_url    VARCHAR(512) NOT NULL,   -- http(s) only; host must be in TOOL_URL_ALLOWLIST
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
    tier         VARCHAR(16)   NULL,     -- P0 | P1 | P2 | P3
    description  VARCHAR(1024) NULL,
    enabled      INT           NOT NULL DEFAULT 1,
    updated_at   DOUBLE        NOT NULL,
    updated_by   VARCHAR(128)  NOT NULL DEFAULT 'system',
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS service_binding (
    service_name     VARCHAR(128)  NOT NULL,
    kind             VARCHAR(32)   NOT NULL,  -- one per evidence type
    datasource_name  VARCHAR(128)  NOT NULL,
    selector         VARCHAR(1024) NULL,      -- PromQL / Loki label selector
    metric           VARCHAR(128)  NULL,
    secondary_metric VARCHAR(128)  NULL,
    mode             VARCHAR(16)   NOT NULL,  -- real | mock, must match datasource.mock
    enabled          INT           NOT NULL DEFAULT 1,
    updated_at       DOUBLE        NOT NULL,
    updated_by       VARCHAR(128)  NOT NULL DEFAULT 'system',
    PRIMARY KEY (service_name, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
