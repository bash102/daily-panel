-- Zozo's button box worker state (D1). Timestamps are ISO 8601 UTC strings
-- from Date.toISOString(): fixed width, so they sort lexically in time order.

-- Box config pushed by the Pi (control/config.json). One row, id = 1.
CREATE TABLE config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  quiet_start TEXT NOT NULL,
  quiet_end TEXT NOT NULL,
  timezone TEXT NOT NULL,
  review_delay_s INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

-- Zozo's replies to grandma (control/zozo/<rec_id>.json).
CREATE TABLE zozo_messages (
  rec_id TEXT PRIMARY KEY,
  media_key TEXT,
  recorded_at TEXT,
  duration_s REAL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'published', 'withheld', 'superseded')),
  registered_at TEXT NOT NULL,
  publish_at TEXT,
  published_at TEXT,
  withheld_at TEXT,
  superseded_by TEXT,
  sms_sent_at TEXT,
  sms_attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE INDEX zozo_status ON zozo_messages(status, recorded_at);

-- Grandma's messages, archived in R2 under recordings/grandma/.
CREATE TABLE grandma_messages (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  duration_s REAL,
  call_sid TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX grandma_recorded ON grandma_messages(recorded_at);

-- Twilio recordings to copy into R2 and then delete from Twilio.
CREATE TABLE twilio_ingest (
  recording_sid TEXT PRIMARY KEY,
  recording_url TEXT NOT NULL,
  call_sid TEXT,
  duration_s REAL,
  recorded_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'stored', 'done', 'failed', 'rejected')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX twilio_ingest_status ON twilio_ingest(status);

-- Per-caller PIN failures and lockouts.
CREATE TABLE pin_callers (
  caller TEXT PRIMARY KEY,
  failures INTEGER NOT NULL DEFAULT 0,
  first_failure_at TEXT,
  locked_until TEXT,
  notified_at TEXT
);

-- Line-wide brute-force limit. One row, id = 1.
CREATE TABLE pin_global (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  window_start TEXT,
  failures INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  notified_at TEXT
);

-- Calls that entered the right PIN (the menu and recording callbacks require it).
CREATE TABLE calls (
  call_sid TEXT PRIMARY KEY,
  caller TEXT,
  authed_at TEXT NOT NULL
);
