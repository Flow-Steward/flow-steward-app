-- Each user picks how dates and times read across the interface: a date pattern
-- ("D MMM YYYY", "MM/DD/YYYY", ...) and a 12- or 24-hour clock. Empty means the
-- default; existing users keep reading the default until they choose.
-- transaction: true
ALTER TABLE ONLY public.users ADD COLUMN IF NOT EXISTS date_format character varying(16);
ALTER TABLE ONLY public.users ADD COLUMN IF NOT EXISTS time_format character varying(8);
