-- Email conversation reconstruction API schema.
--
-- Model overview
-- --------------
-- * Every referenced Message-ID is a *node* inside a mailbox. A node is
--   either a real message (raw EML bytes stored) or a placeholder for a
--   message that has not been imported yet.
-- * Each node has at most one parent pointer (the In-Reply-To id, else the
--   last References id). A thread is identified by the root node's
--   Message-ID. Parent pointers of real messages are immutable; an inferred
--   pointer of a placeholder may be corrected exactly once, when the real
--   message turns out to have another parent.
-- * revisions are append-only. link_events records every parent-pointer
--   state with the revision that produced it, so GET /threads?revision=N
--   reconstructs the exact forest at that revision. Historical revisions are
--   never rewritten, even when a late parent merges threads.

CREATE TABLE IF NOT EXISTS mailboxes (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revisions (
    id           BIGSERIAL PRIMARY KEY,
    mailbox_id   BIGINT NOT NULL REFERENCES mailboxes(id),
    rev          INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch_fp     BYTEA,                      -- NULL for no-op effective change
    item_count   INTEGER NOT NULL,
    merged       JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (mailbox_id, rev),
    UNIQUE (mailbox_id, batch_fp)
);

CREATE TABLE IF NOT EXISTS nodes (
    -- effective (current) parent pointer; historical states live in link_events
    mailbox_id       BIGINT NOT NULL REFERENCES mailboxes(id),
    msg_id           TEXT NOT NULL,
    is_placeholder   BOOLEAN NOT NULL,
    origin_placeholder BOOLEAN NOT NULL,       -- immutable: born as a placeholder
    raw_bytes        BYTEA,
    raw_sha256       BYTEA,
    parent_id        TEXT,                    -- current parent (NULL => thread root)
    parent_inferred  BOOLEAN NOT NULL DEFAULT FALSE, -- TRUE while guessed for a placeholder
    fill_rev         INTEGER,                 -- rev at which a placeholder got real content
    created_rev      INTEGER NOT NULL,
    PRIMARY KEY (mailbox_id, msg_id)
);

CREATE INDEX IF NOT EXISTS nodes_parent_idx
    ON nodes (mailbox_id, parent_id);

CREATE TABLE IF NOT EXISTS link_events (
    mailbox_id  BIGINT NOT NULL REFERENCES mailboxes(id),
    rev         INTEGER NOT NULL,
    node_id     TEXT NOT NULL,
    parent_id   TEXT,                          -- NULL means the node is a root
    PRIMARY KEY (mailbox_id, rev, node_id)
);

-- hot path for revision-scoped reconstruction (DISTINCT ON node_id ... rev)
CREATE INDEX IF NOT EXISTS link_events_hist_idx
    ON link_events (mailbox_id, node_id, rev DESC);
