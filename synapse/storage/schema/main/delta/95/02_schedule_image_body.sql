/* Copyright 2025 STAFF mod
 *
 * Add image_body column to staff_scheduled_messages so the firing payload
 * can record a distinct caption for the m.image event (rather than
 * defaulting its body to the literal word "image").
 */

ALTER TABLE staff_scheduled_messages ADD COLUMN image_body TEXT;
