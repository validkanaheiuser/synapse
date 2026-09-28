/* Copyright 2026 STAFF mod
 *
 * Staff DM custom nicknames (isolated per staff user).
 * Each staff member can set custom names for rooms/DMs without affecting other staff.
 */

CREATE TABLE staff_dm_names (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    original_name TEXT,
    custom_name TEXT NOT NULL,
    updated_ts BIGINT NOT NULL,
    PRIMARY KEY (user_id, room_id)
);

CREATE INDEX staff_dm_names_user ON staff_dm_names (user_id);
