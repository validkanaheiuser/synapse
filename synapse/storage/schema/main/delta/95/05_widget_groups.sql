/* Copyright 2026 STAFF mod - AGENT P
 *
 * Widget groups: a named collection of staff users that owns a set of
 * general_widgets.  When a staff member is in any of their groups' DMs,
 * those groups' widgets get auto-injected (in addition to that staff's
 * own staff_custom_widgets).
 *
 *   staff_widget_groups          : group definitions (id, name, etc.).
 *   staff_widget_group_widgets   : (group_id, widget_id) join table.
 *   staff_widget_group_members   : (group_id, user_id) join table.
 *
 * No foreign keys back to staff_widget_definitions / staff_users -- we
 * clean orphans at the application level when a widget/staff is deleted
 * (see store.widget_orphan_cleanup_on_widget_delete and
 * widget_orphan_cleanup_on_staff_remove).  This follows the rest of the
 * Synapse schema (see schema/main/delta/72/full.sql) which deliberately
 * avoids FKs on most tables.
 *
 * Standard Synapse DDL only: TEXT / BIGINT.  Both SQLite and Postgres
 * accept this dialect unchanged.
 */

CREATE TABLE staff_widget_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_ts BIGINT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX staff_widget_groups_name ON staff_widget_groups (name);

CREATE TABLE staff_widget_group_widgets (
    group_id TEXT NOT NULL,
    widget_id TEXT NOT NULL,
    PRIMARY KEY (group_id, widget_id)
);
CREATE INDEX staff_widget_group_widgets_w ON staff_widget_group_widgets (widget_id);

CREATE TABLE staff_widget_group_members (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX staff_widget_group_members_u ON staff_widget_group_members (user_id);
