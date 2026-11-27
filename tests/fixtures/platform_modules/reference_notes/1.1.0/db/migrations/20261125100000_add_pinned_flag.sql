-- Reference notes module 1.1.0: an optional pinned flag. Nullable, so rows written
-- by 1.0.0 stay valid and the upgrade rewrites nothing.
ALTER TABLE notes ADD COLUMN IF NOT EXISTS pinned boolean;
