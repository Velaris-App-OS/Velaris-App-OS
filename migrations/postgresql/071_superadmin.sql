-- 071: Superadmin — god-mode user created once during setup
-- is_superadmin=TRUE is enforced unique at DB level (only one can ever exist)

ALTER TABLE helix_users
  ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE;

-- DB-level enforcement: exactly one superadmin row allowed, ever
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_superadmin
  ON helix_users (is_superadmin)
  WHERE is_superadmin = TRUE;
