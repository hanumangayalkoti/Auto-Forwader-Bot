"""
PostgreSQL-based persistent storage using asyncpg.
Survives Railway restarts, main.py edits — kuch bhi ho.
"""
import json
import asyncpg
from config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def init_db():
    """Call once on bot startup to create pool + tables."""
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id           SERIAL PRIMARY KEY,
                name         VARCHAR(30)  NOT NULL DEFAULT 'Group',
                is_active    BOOLEAN      NOT NULL DEFAULT FALSE,
                keywords     TEXT[]       NOT NULL DEFAULT '{}',
                blacklist    TEXT[]       NOT NULL DEFAULT '{}',
                replacements JSONB        NOT NULL DEFAULT '{}',
                caption_mode VARCHAR(10)  NOT NULL DEFAULT 'keep',
                caption_text TEXT         NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS group_channels (
                id         SERIAL  PRIMARY KEY,
                group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                channel_id BIGINT  NOT NULL,
                direction  VARCHAR(10) NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_map (
                id              SERIAL    PRIMARY KEY,
                group_id        INTEGER   NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                source_chat_id  BIGINT    NOT NULL,
                source_msg_id   INTEGER   NOT NULL,
                target_chat_id  BIGINT    NOT NULL,
                target_msg_id   INTEGER   NOT NULL,
                created_at      TIMESTAMP NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_msgmap_source
                ON message_map (group_id, source_chat_id, source_msg_id);
        """)
    print("[DB] Tables ready.")


def _pool_ok():
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_db() first")
    return _pool


# ══════════════════════════════════════════════
# GROUP CRUD
# ══════════════════════════════════════════════

async def create_group(name: str) -> dict:
    async with _pool_ok().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO groups (name) VALUES ($1) RETURNING *", name
        )
        return _row_to_group(row)


async def get_all_groups() -> dict[int, dict]:
    async with _pool_ok().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM groups ORDER BY id")
        channels = await conn.fetch("SELECT * FROM group_channels")

    groups: dict[int, dict] = {}
    for row in rows:
        g = _row_to_group(row)
        g["incoming"] = set()
        g["outgoing"] = set()
        groups[g["id"]] = g

    for ch in channels:
        gid = ch["group_id"]
        if gid in groups:
            if ch["direction"] == "incoming":
                groups[gid]["incoming"].add(ch["channel_id"])
            else:
                groups[gid]["outgoing"].add(ch["channel_id"])

    return groups


async def delete_group(group_id: int):
    async with _pool_ok().acquire() as conn:
        await conn.execute("DELETE FROM groups WHERE id = $1", group_id)


async def rename_group(group_id: int, name: str):
    async with _pool_ok().acquire() as conn:
        await conn.execute(
            "UPDATE groups SET name = $1 WHERE id = $2", name, group_id
        )


async def set_group_active(group_id: int, active: bool):
    async with _pool_ok().acquire() as conn:
        await conn.execute(
            "UPDATE groups SET is_active = $1 WHERE id = $2", active, group_id
        )


async def set_all_groups_active(active: bool):
    async with _pool_ok().acquire() as conn:
        await conn.execute("UPDATE groups SET is_active = $1", active)


async def count_groups() -> int:
    async with _pool_ok().acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM groups")


# ══════════════════════════════════════════════
# CHANNEL CRUD
# ══════════════════════════════════════════════

async def set_channels(group_id: int, direction: str, channel_ids: set[int]):
    async with _pool_ok().acquire() as conn:
        await conn.execute(
            "DELETE FROM group_channels WHERE group_id = $1 AND direction = $2",
            group_id, direction
        )
        if channel_ids:
            await conn.executemany(
                "INSERT INTO group_channels (group_id, channel_id, direction) VALUES ($1, $2, $3)",
                [(group_id, cid, direction) for cid in channel_ids]
            )


# ══════════════════════════════════════════════
# PER-GROUP SETTINGS
# ══════════════════════════════════════════════

async def update_group_settings(group_id: int, **kwargs):
    """
    Supported kwargs:
        keywords     -> list[str]
        blacklist    -> list[str]
        replacements -> dict[str, str]
        caption_mode -> str  ('keep' | 'add' | 'remove')
        caption_text -> str
    """
    if not kwargs:
        return

    parts = []
    values = []
    i = 1

    if "keywords" in kwargs:
        parts.append(f"keywords = ${i}")
        values.append(list(kwargs["keywords"]))
        i += 1

    if "blacklist" in kwargs:
        parts.append(f"blacklist = ${i}")
        values.append(list(kwargs["blacklist"]))
        i += 1

    if "replacements" in kwargs:
        parts.append(f"replacements = ${i}")
        values.append(json.dumps(kwargs["replacements"]))
        i += 1

    if "caption_mode" in kwargs:
        parts.append(f"caption_mode = ${i}")
        values.append(kwargs["caption_mode"])
        i += 1

    if "caption_text" in kwargs:
        parts.append(f"caption_text = ${i}")
        values.append(kwargs["caption_text"])
        i += 1

    if parts:
        values.append(group_id)
        sql = f"UPDATE groups SET {', '.join(parts)} WHERE id = ${i}"
        async with _pool_ok().acquire() as conn:
            await conn.execute(sql, *values)


# ══════════════════════════════════════════════
# MESSAGE MAP  (Live Edit Sync)
# ══════════════════════════════════════════════

async def save_message_map(
    group_id: int,
    source_chat_id: int,
    source_msg_id: int,
    target_chat_id: int,
    target_msg_id: int,
):
    async with _pool_ok().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO message_map
                (group_id, source_chat_id, source_msg_id, target_chat_id, target_msg_id)
            VALUES ($1, $2, $3, $4, $5)
            """,
            group_id, source_chat_id, source_msg_id, target_chat_id, target_msg_id,
        )


async def get_mapped_targets(
    source_chat_id: int, source_msg_id: int
) -> list[dict]:
    """
    Returns all (group_id, target_chat_id, target_msg_id) rows
    for a given source message — used by the live-edit handler.
    """
    async with _pool_ok().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT group_id, target_chat_id, target_msg_id
            FROM   message_map
            WHERE  source_chat_id = $1 AND source_msg_id = $2
            """,
            source_chat_id, source_msg_id,
        )
        return [dict(r) for r in rows]


async def cleanup_old_maps(days: int = 7):
    """Remove mappings older than N days to keep the table small."""
    async with _pool_ok().acquire() as conn:
        await conn.execute(
            f"DELETE FROM message_map WHERE created_at < NOW() - INTERVAL '{days} days'"
        )


# ══════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════

def _row_to_group(row) -> dict:
    return {
        "id":           row["id"],
        "name":         row["name"],
        "active":       row["is_active"],
        "incoming":     set(),
        "outgoing":     set(),
        "keywords":     set(row["keywords"] or []),
        "blacklist":    set(row["blacklist"] or []),
        "replacements": dict(row["replacements"] or {}),
        "caption_mode": row["caption_mode"] or "keep",
        "caption_text": row["caption_text"] or "",
    }
