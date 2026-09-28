from __future__ import annotations

import sqlite3

TEMPLE_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS temple_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    temple_type TEXT NOT NULL CHECK(temple_type IN ('heritage','urban','mountain','community')),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    max_concurrent_mitigation_sessions INTEGER NOT NULL CHECK(max_concurrent_mitigation_sessions > 0),
    ventilation_capacity INTEGER NOT NULL CHECK(ventilation_capacity > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worship_halls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    visit_order INTEGER NOT NULL CHECK(visit_order >= 0),
    expected_visit_seconds INTEGER NOT NULL CHECK(expected_visit_seconds > 0),
    ventilation_capacity INTEGER NOT NULL CHECK(ventilation_capacity > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closure','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, code),
    UNIQUE(temple_id, visit_order)
);
CREATE TABLE IF NOT EXISTS incense_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incense_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    activity_type TEXT NOT NULL CHECK(activity_type IN ('daily','festival','ceremony','memorial','tour')),
    pm25_target INTEGER NOT NULL CHECK(pm25_target > 0),
    co_target REAL NOT NULL CHECK(co_target >= 0 AND co_target <= 1),
    min_supply_airflow REAL NOT NULL CHECK(min_supply_airflow >= 0),
    min_exhaust_airflow REAL NOT NULL CHECK(min_exhaust_airflow >= 0),
    default_risk_priority INTEGER NOT NULL CHECK(default_risk_priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published','retired')),
    rules_json TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    published_by TEXT,
    effective_from TEXT,
    retired_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, version_no),
    UNIQUE(temple_id, rules_digest)
);
CREATE INDEX IF NOT EXISTS idx_safety_policy_effective ON safety_policy_versions(temple_id,state,effective_from);
CREATE TABLE IF NOT EXISTS incense_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    steward_hash TEXT NOT NULL,
    sensor_class TEXT NOT NULL,
    visitor_density REAL NOT NULL CHECK(visitor_density >= 0),
    pm25_ugm3 REAL NOT NULL CHECK(pm25_ugm3 >= 0),
    co_ppm REAL NOT NULL CHECK(co_ppm >= 0 AND co_ppm <= 1),
    supply_airflow REAL NOT NULL CHECK(supply_airflow >= 0),
    exhaust_airflow REAL NOT NULL CHECK(exhaust_airflow >= 0),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_scene_time ON incense_observations(temple_id,observed_at);
CREATE TABLE IF NOT EXISTS safety_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL UNIQUE REFERENCES incense_observations(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    reasons_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','mitigating','resolved','expired')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_safety_incidents_open ON safety_incidents(state,severity,opened_at);
CREATE TABLE IF NOT EXISTS mitigation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    safety_incident_id INTEGER NOT NULL REFERENCES safety_incidents(id),
    steward_hash TEXT NOT NULL,
    incense_profile_id INTEGER NOT NULL REFERENCES incense_profiles(id),
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    safety_policy_version_id INTEGER NOT NULL REFERENCES safety_policy_versions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled','expired')),
    allocated_supply_airflow REAL NOT NULL CHECK(allocated_supply_airflow >= 0),
    allocated_exhaust_airflow REAL NOT NULL CHECK(allocated_exhaust_airflow >= 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(safety_incident_id)
);
CREATE INDEX IF NOT EXISTS idx_mitigation_sessions_ventilation ON mitigation_sessions(temple_id,hall_id,status,expires_at);
CREATE TABLE IF NOT EXISTS ventilation_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mitigation_session_id INTEGER NOT NULL REFERENCES mitigation_sessions(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    supply_airflow REAL NOT NULL,
    exhaust_airflow REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    held_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(mitigation_session_id)
);
CREATE TABLE IF NOT EXISTS mitigation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mitigation_session_id INTEGER NOT NULL REFERENCES mitigation_sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mitigation_events ON mitigation_events(mitigation_session_id,id);
CREATE TABLE IF NOT EXISTS steward_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    steward_hash TEXT NOT NULL,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    authorization_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','expired','cancelled')),
    source_approval_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authorizations_lookup ON steward_authorizations(steward_hash,temple_id,state,valid_from,valid_until);
CREATE TABLE IF NOT EXISTS restoration_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('phased','halls','scheduled')),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','scheduled','running','paused','completed','cancelled')),
    target_percentage INTEGER NOT NULL DEFAULT 100 CHECK(target_percentage BETWEEN 1 AND 100),
    safety_policy_version_id INTEGER NOT NULL REFERENCES safety_policy_versions(id),
    starts_at TEXT,
    ends_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restoration_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restoration_campaign_id INTEGER NOT NULL REFERENCES restoration_campaigns(id) ON DELETE CASCADE,
    hall_id INTEGER REFERENCES worship_halls(id),
    cohort_key TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','paused','completed','failed')),
    activated_at TEXT,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(restoration_campaign_id,hall_id,cohort_key)
);
CREATE INDEX IF NOT EXISTS idx_restoration_targets_state ON restoration_targets(restoration_campaign_id,state,id);
CREATE TABLE IF NOT EXISTS hall_closure_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    code TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scheduled' CHECK(state IN ('scheduled','active','completed','cancelled')),
    drain_mode TEXT NOT NULL DEFAULT 'finish_active' CHECK(drain_mode IN ('finish_active','cancel_active','block_new')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closure_active ON hall_closure_windows(temple_id,hall_id,state,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS restoration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_restoration_events_resource ON restoration_events(resource_type,resource_id,id);
CREATE TABLE IF NOT EXISTS visit_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    slot_date TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','capacity_locked','closed')),
    note TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, slot_start, slot_end)
);
CREATE INDEX IF NOT EXISTS idx_visit_slots_date ON visit_slots(temple_id,slot_date);
CREATE INDEX IF NOT EXISTS idx_visit_slots_window ON visit_slots(temple_id,slot_start,slot_end);
CREATE TABLE IF NOT EXISTS visit_slot_capacities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL REFERENCES visit_slots(id) ON DELETE CASCADE,
    hall_id INTEGER REFERENCES worship_halls(id) ON DELETE CASCADE,
    hall_key INTEGER GENERATED ALWAYS AS (COALESCE(hall_id,-1)) STORED,
    entrance_code TEXT NOT NULL DEFAULT '',
    visitor_group TEXT NOT NULL DEFAULT '' CHECK(visitor_group IN ('','elder','ceremony','general')),
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(slot_id,hall_key,entrance_code,visitor_group)
);
CREATE INDEX IF NOT EXISTS idx_visit_capacities_slot ON visit_slot_capacities(slot_id);
CREATE TABLE IF NOT EXISTS capacity_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    slot_id INTEGER NOT NULL REFERENCES visit_slots(id) ON DELETE CASCADE,
    release_kind TEXT NOT NULL CHECK(release_kind IN ('cancellation','confirmation_timeout','closure','capacity_adjustment')),
    source_reservation_id INTEGER REFERENCES visit_reservations(id) ON DELETE SET NULL,
    closure_window_id INTEGER REFERENCES hall_closure_windows(id) ON DELETE SET NULL,
    hall_id INTEGER REFERENCES worship_halls(id) ON DELETE CASCADE,
    entrance_code TEXT NOT NULL DEFAULT '',
    visitor_group TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capacity_releases_slot ON capacity_releases(slot_id,id);
CREATE TABLE IF NOT EXISTS visit_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    slot_id INTEGER NOT NULL REFERENCES visit_slots(id) ON DELETE CASCADE,
    hall_id INTEGER NOT NULL REFERENCES worship_halls(id),
    hall_key INTEGER GENERATED ALWAYS AS (hall_id) STORED,
    entrance_code TEXT NOT NULL,
    visitor_group TEXT NOT NULL CHECK(visitor_group IN ('elder','ceremony','general')),
    visitor_hash TEXT NOT NULL,
    party_size INTEGER NOT NULL DEFAULT 1 CHECK(party_size BETWEEN 1 AND 500),
    contact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('pending_confirmation','confirmed','waiting','cancelled','expired','declined')),
    confirm_token TEXT NOT NULL UNIQUE,
    confirm_deadline TEXT NOT NULL,
    waitlist_rank INTEGER,
    queued_at TEXT,
    confirmed_at TEXT,
    cancelled_at TEXT,
    decided_at TEXT,
    end_reason TEXT NOT NULL DEFAULT '',
    request_id TEXT,
    request_digest TEXT,
    promoted_from_release_id INTEGER REFERENCES capacity_releases(id) ON DELETE SET NULL,
    promotion_rule_code TEXT NOT NULL DEFAULT '',
    promoted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_visit_request_id ON visit_reservations(request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_visit_reservations_occupancy ON visit_reservations(slot_id,status);
CREATE INDEX IF NOT EXISTS idx_visit_reservations_waitlist ON visit_reservations(slot_id,hall_key,entrance_code,visitor_group,status,waitlist_rank);
CREATE INDEX IF NOT EXISTS idx_visit_reservations_visitor ON visit_reservations(visitor_hash,status);
CREATE TABLE IF NOT EXISTS waitlist_promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL UNIQUE REFERENCES visit_reservations(id) ON DELETE CASCADE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    slot_id INTEGER NOT NULL REFERENCES visit_slots(id) ON DELETE CASCADE,
    capacity_release_id INTEGER REFERENCES capacity_releases(id) ON DELETE SET NULL,
    rule_code TEXT NOT NULL,
    rule_label TEXT NOT NULL,
    rank_before INTEGER NOT NULL,
    queue_depth INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waitlist_promotions_slot ON waitlist_promotions(slot_id,id);
CREATE TABLE IF NOT EXISTS visit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    reservation_id INTEGER REFERENCES visit_reservations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visit_events_resource ON visit_events(resource_type,resource_id,id);
CREATE INDEX IF NOT EXISTS idx_visit_events_reservation ON visit_events(reservation_id,id);
'''


def ensure_temple_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(TEMPLE_SCHEMA)
