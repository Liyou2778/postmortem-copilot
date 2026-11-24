-- M1 schema (MySQL 8.0)
-- The SQLite store used by the domain tests mirrors these tables.

CREATE TABLE IF NOT EXISTS events (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  task_id     CHAR(36)    NOT NULL,
  seq         BIGINT      NOT NULL,
  type        VARCHAR(48) NOT NULL,
  payload     JSON        NOT NULL,
  actor       VARCHAR(64) NOT NULL,
  created_at  DATETIME(3) NOT NULL,
  UNIQUE KEY uk_task_seq (task_id, seq),
  KEY idx_task_seq (task_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS idempotency (
  `key`       CHAR(64)    PRIMARY KEY,
  task_id     CHAR(36)    NOT NULL,
  step_id     VARCHAR(64) NOT NULL,
  status      VARCHAR(16) NOT NULL,   -- INTENT | DONE | UNKNOWN
  result      JSON        NULL,
  created_at  DATETIME(3) NOT NULL,
  KEY idx_task_step (task_id, step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS leases (
  step_id     VARCHAR(64) PRIMARY KEY,
  task_id     CHAR(36)    NOT NULL,
  owner       VARCHAR(128) NOT NULL,
  fence       BIGINT      NOT NULL,
  expires_at  BIGINT      NOT NULL,   -- epoch millis
  KEY idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Read model (derived from events; rebuilt by replay at any time)
CREATE TABLE IF NOT EXISTS task_view (
  task_id       CHAR(36)    PRIMARY KEY,
  title         VARCHAR(255) NOT NULL,
  status        VARCHAR(32) NOT NULL,
  plan_version  INT         NOT NULL DEFAULT 0,
  last_seq      BIGINT      NOT NULL DEFAULT 0,
  updated_at    DATETIME(3) NOT NULL,
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS evidence (
  id              CHAR(36)     PRIMARY KEY,
  task_id         CHAR(36)     NOT NULL,
  step_id         VARCHAR(64)  NOT NULL,
  source          VARCHAR(16)  NOT NULL,
  uri             TEXT         NOT NULL,
  captured_at     DATETIME(3)  NOT NULL,
  digest          CHAR(64)     NOT NULL,
  redaction_level VARCHAR(8)   NOT NULL DEFAULT 'none',
  excerpt         TEXT         NULL,
  KEY idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS claim (
  id            CHAR(36)     PRIMARY KEY,
  task_id       CHAR(36)     NOT NULL,
  step_id       VARCHAR(64)  NOT NULL,
  statement     TEXT         NOT NULL,
  confidence    DECIMAL(3,2) NOT NULL,
  root_cause_rank INT        NULL,
  status        VARCHAR(16)  NOT NULL DEFAULT 'proposed',
  KEY idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS claim_evidence (
  claim_id    CHAR(36)    NOT NULL,
  evidence_id CHAR(36)    NOT NULL,
  PRIMARY KEY (claim_id, evidence_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
