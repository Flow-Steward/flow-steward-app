-- Reference notes module: one note belongs to exactly one account.
-- Runs with search_path set to the module schema, so names stay unqualified.
CREATE TABLE notes (
    id text PRIMARY KEY,
    account_id text NOT NULL,
    title text NOT NULL,
    body text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX notes_account_created_idx ON notes (account_id, created_at, id);
