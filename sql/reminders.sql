drop table if exists reminders;
CREATE TABLE IF NOT EXISTS reminders
(
    id           SERIAL PRIMARY KEY,
    session_id   TEXT      NOT NULL,
    text         TEXT      NOT NULL,
    date_time    TIMESTAMP NOT NULL,
    user_name    TEXT,
    repeat_type  TEXT,
    holiday_type TEXT,
    creator_id   TEXT,
    creator_name TEXT,
    is_task      BOOLEAN   DEFAULT FALSE,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_id ON reminders (session_id);
CREATE INDEX IF NOT EXISTS idx_creator_id ON reminders (creator_id);