-- Users who must not see the profit-share figures (share %, loss sharing, "my share").
ALTER TABLE users ADD COLUMN hide_share INTEGER NOT NULL DEFAULT 0;
