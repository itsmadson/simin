-- Bot control, shared between the API and the trader process.
--
-- The API container and the trader container are separate processes, so an
-- in-memory flag in one is invisible to the other: pressing Pause in the
-- dashboard would change nothing at all. The flag lives in the database so both
-- see the same truth, and so it survives a restart.
CREATE TABLE IF NOT EXISTS bot_control (
    id          smallint PRIMARY KEY DEFAULT 1,
    paused      boolean NOT NULL DEFAULT false,
    reason      text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL DEFAULT 'system',
    CONSTRAINT bot_control_single_row CHECK (id = 1)
);

INSERT INTO bot_control (id, paused, reason, updated_by)
VALUES (1, false, NULL, 'init')
ON CONFLICT (id) DO NOTHING;
