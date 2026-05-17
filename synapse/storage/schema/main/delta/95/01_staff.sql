/* Copyright 2025 STAFF mod
 *
 * Tables for the staff_module: allowlist, settings KV, scheduled messages,
 * edit history, and widget definitions/instances.
 */

CREATE TABLE staff_users (
    user_id TEXT PRIMARY KEY,
    added_ts BIGINT NOT NULL,
    added_by TEXT NOT NULL,
    note TEXT
);

CREATE TABLE staff_settings (
    setting_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts BIGINT NOT NULL
);

CREATE TABLE staff_scheduled_messages (
    task_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    as_user TEXT NOT NULL,
    send_at_ms BIGINT NOT NULL,
    message TEXT,
    image_mxc TEXT,
    created_ts BIGINT NOT NULL
);
CREATE INDEX staff_scheduled_send_at ON staff_scheduled_messages (send_at_ms);

CREATE TABLE staff_edit_history (
    edit_id TEXT PRIMARY KEY,
    original_event_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    old_content_json TEXT NOT NULL,
    new_content_json TEXT NOT NULL,
    replace_event_id TEXT,
    redaction_event_id TEXT,
    edited_by TEXT NOT NULL,
    edited_ts BIGINT NOT NULL,
    kind TEXT NOT NULL
);
CREATE INDEX staff_edit_history_original ON staff_edit_history (original_event_id);
CREATE INDEX staff_edit_history_room ON staff_edit_history (room_id);

CREATE TABLE staff_widget_definitions (
    widget_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    widget_type TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_ts BIGINT NOT NULL,
    updated_ts BIGINT NOT NULL
);
CREATE INDEX staff_widget_owner ON staff_widget_definitions (owner_user_id);
CREATE INDEX staff_widget_type ON staff_widget_definitions (widget_type);

CREATE TABLE staff_widget_room_instances (
    instance_id TEXT PRIMARY KEY,
    widget_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    injected_by TEXT NOT NULL,
    last_state_event_id TEXT,
    created_ts BIGINT NOT NULL,
    UNIQUE (widget_id, room_id)
);
CREATE INDEX staff_widget_instances_widget ON staff_widget_room_instances (widget_id);
CREATE INDEX staff_widget_instances_room ON staff_widget_room_instances (room_id);

CREATE TABLE staff_account_data_pushed (
    user_id TEXT PRIMARY KEY,
    pushed_ts BIGINT NOT NULL
);
