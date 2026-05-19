/* Copyright 2026 STAFF mod
 *
 * AGENT H — S1/S3 backing tables.
 *
 *   staff_audit_log         : S3, one row per successful staff request.
 *   staff_jwt_keys          : S1, HMAC signing keys (rotatable).
 *   staff_jwt_revocations   : S1, jti deny-list (logout / refresh).
 *
 * `id` columns are TEXT (uuid hex) to stay portable between Synapse's
 * supported databases (SQLite + Postgres) without depending on
 * AUTOINCREMENT / BIGSERIAL semantics.
 */

CREATE TABLE staff_audit_log (
    id TEXT PRIMARY KEY,
    ts BIGINT NOT NULL,
    actor_user_id TEXT,
    actor_kind TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    method TEXT NOT NULL,
    status INTEGER NOT NULL,
    target TEXT,
    body_hash TEXT,
    ip TEXT
);
CREATE INDEX staff_audit_log_ts ON staff_audit_log (ts);
CREATE INDEX staff_audit_log_actor ON staff_audit_log (actor_user_id);
CREATE INDEX staff_audit_log_endpoint ON staff_audit_log (endpoint);

CREATE TABLE staff_jwt_keys (
    key_id TEXT PRIMARY KEY,
    secret_b64 TEXT NOT NULL,
    created_ts BIGINT NOT NULL,
    active SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX staff_jwt_keys_active ON staff_jwt_keys (active);

CREATE TABLE staff_jwt_revocations (
    jti TEXT PRIMARY KEY,
    revoked_ts BIGINT NOT NULL,
    exp_ts BIGINT NOT NULL
);
CREATE INDEX staff_jwt_revocations_exp ON staff_jwt_revocations (exp_ts);
