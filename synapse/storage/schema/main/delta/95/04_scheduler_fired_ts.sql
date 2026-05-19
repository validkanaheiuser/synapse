/* Copyright 2025 STAFF mod - Agent I (S13)
 *
 * Track when a scheduled-message row actually fired so we can clean it up.
 * The scheduler now sets `fired_ts` on success; a nightly background job
 * removes rows whose `fired_ts` is older than one week.  Both engines
 * accept the same `ALTER TABLE ... ADD COLUMN` syntax.
 */

ALTER TABLE staff_scheduled_messages ADD COLUMN fired_ts BIGINT;
CREATE INDEX staff_scheduled_fired_ts ON staff_scheduled_messages (fired_ts);
