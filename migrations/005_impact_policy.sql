-- M10.3: impact business policy (users / estimated cost).
-- JSON example:
--   {"users_per_request": 0.02, "cost_per_minute": 12.5, "currency": "CNY"}
-- The application also auto-adds the column at startup.

ALTER TABLE service_binding
    ADD COLUMN impact_policy MEDIUMTEXT NULL AFTER extra_metrics;
