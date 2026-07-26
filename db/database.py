import json
import sqlite3
from pathlib import Path

from models.models import PlayerModel, WorldEntityModel, SessionModel, ChatMessageModel

DB_PATH = Path(__file__).parent.parent / "game.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row_to_player(row: sqlite3.Row) -> PlayerModel:
    return PlayerModel(
        name=row["name"],
        class_archetype=row["class_archetype"],
        class_description=row["class_description"] if "class_description" in row.keys() else "",
        hp_current=row["hp_current"],
        hp_max=row["hp_max"],
        stats=json.loads(row["stats"]),
        inventory=json.loads(row["inventory"]),
        status_effects=json.loads(row["status_effects"]),
        backstory=row["backstory"] if "backstory" in row.keys() else "",
    )


def _row_to_session(row: sqlite3.Row) -> SessionModel:
    return SessionModel(
        game_status=row["game_status"],
        turn_order=json.loads(row["turn_order"]),
        current_turn_index=row["current_turn_index"],
        timer_ends_at=row["timer_ends_at"],
    )


def _row_to_chat_message(row: sqlite3.Row) -> ChatMessageModel:
    return ChatMessageModel(
        sender=row["sender"],
        message_text=row["message_text"],
        is_action=bool(row["is_action"]),
        timestamp=row["timestamp"],
    )


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            class_archetype TEXT    NOT NULL DEFAULT '',
            hp_current      INTEGER NOT NULL DEFAULT 0,
            hp_max          INTEGER NOT NULL DEFAULT 0,
            stats           TEXT    NOT NULL DEFAULT '{}',
            inventory       TEXT    NOT NULL DEFAULT '[]',
            status_effects  TEXT    NOT NULL DEFAULT '[]',
            is_occupied     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS world_entities (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type  TEXT    NOT NULL,
            name         TEXT    NOT NULL,
            data         TEXT    NOT NULL DEFAULT '{}',
            tags         TEXT    NOT NULL DEFAULT '[]',
            UNIQUE(entity_type, name)
        );

        CREATE TABLE IF NOT EXISTS game_session (
            session_id       INTEGER PRIMARY KEY,
            game_status      TEXT    NOT NULL DEFAULT 'lobby',
            turn_order       TEXT    NOT NULL DEFAULT '[]',
            current_turn_index INTEGER NOT NULL DEFAULT 0,
            global_lore      TEXT    NOT NULL DEFAULT '',
            setting_blob     TEXT    NOT NULL DEFAULT '{}',
            timer_ends_at    TEXT    NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sender       TEXT    NOT NULL,
            player_id    INTEGER DEFAULT NULL,
            message_text TEXT    NOT NULL DEFAULT '',
            is_action    INTEGER NOT NULL DEFAULT 0,
            action_type  TEXT    NOT NULL DEFAULT 'narrative',
            result_blob  TEXT    NOT NULL DEFAULT '{}',
            timestamp    TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (player_id) REFERENCES players(id)
        );
    """)

    cur.execute("SELECT COUNT(*) FROM game_session")
    if cur.fetchone()[0] == 0:
        cur.execute(
            """
            INSERT INTO game_session (session_id, game_status)
            VALUES (1, 'lobby')
            """
        )

    cur.execute("PRAGMA table_info(players)")
    cols = {row[1] for row in cur.fetchall()}
    if "is_occupied" not in cols:
        cur.execute("ALTER TABLE players ADD COLUMN is_occupied INTEGER NOT NULL DEFAULT 0")
    if "backstory" not in cols:
        cur.execute("ALTER TABLE players ADD COLUMN backstory TEXT NOT NULL DEFAULT ''")
    if "class_description" not in cols:
        cur.execute("ALTER TABLE players ADD COLUMN class_description TEXT NOT NULL DEFAULT ''")

    cur.execute("PRAGMA table_info(game_session)")
    gs_cols = {row[1] for row in cur.fetchall()}
    if "timer_ends_at" not in gs_cols:
        cur.execute("ALTER TABLE game_session ADD COLUMN timer_ends_at TEXT NOT NULL DEFAULT ''")

    conn.commit()
    conn.close()


def add_or_update_entity(entity: WorldEntityModel) -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO world_entities (entity_type, name, data)
        VALUES (?, ?, ?)
        ON CONFLICT(entity_type, name) DO UPDATE SET
            data = excluded.data
        """,
        (entity.entity_type, entity.name, json.dumps(entity.data, ensure_ascii=False)),
    )

    conn.commit()
    conn.close()


def get_all_players() -> list[PlayerModel]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    return [_row_to_player(r) for r in rows]


def get_player_by_id(player_id: int) -> PlayerModel | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_player(row)


def upsert_player(player: PlayerModel) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO players (name, class_archetype, class_description, hp_current, hp_max, stats, inventory, status_effects, backstory)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rowid) DO UPDATE SET
            class_archetype    = excluded.class_archetype,
            class_description  = excluded.class_description,
            hp_current         = excluded.hp_current,
            hp_max             = excluded.hp_max,
            stats              = excluded.stats,
            inventory          = excluded.inventory,
            status_effects     = excluded.status_effects,
            backstory          = excluded.backstory
        """,
        (
            player.name,
            player.class_archetype,
            player.class_description,
            player.hp_current,
            player.hp_max,
            json.dumps(player.stats, ensure_ascii=False),
            json.dumps(player.inventory, ensure_ascii=False),
            json.dumps(player.status_effects, ensure_ascii=False),
            player.backstory,
        ),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def get_session() -> SessionModel | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM game_session WHERE session_id = 1").fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_session(row)


def update_session(session: SessionModel) -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE game_session
        SET game_status = ?, turn_order = ?, current_turn_index = ?, timer_ends_at = ?
        WHERE session_id = 1
        """,
        (
            session.game_status,
            json.dumps(session.turn_order),
            session.current_turn_index,
            session.timer_ends_at,
        ),
    )
    conn.commit()
    conn.close()


def extend_timer(seconds: int = 15) -> None:
    from datetime import datetime, timedelta

    conn = get_connection()
    expires = (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()
    conn.execute("UPDATE game_session SET timer_ends_at = ? WHERE session_id = 1", (expires,))
    conn.commit()
    conn.close()


def reset_timer() -> None:
    conn = get_connection()
    conn.execute("UPDATE game_session SET timer_ends_at = '' WHERE session_id = 1")
    conn.commit()
    conn.close()


def add_chat_message(msg: ChatMessageModel, player_id: int | None = None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chat_history (sender, player_id, message_text, is_action, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (msg.sender, player_id, msg.message_text, int(msg.is_action), msg.timestamp),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return msg_id


def get_player_stats_descriptions(player_name: str) -> dict[str, str]:
    conn = get_connection()
    row = conn.execute(
        "SELECT data FROM world_entities WHERE entity_type = 'rule' AND name = 'stats_system'"
    ).fetchone()
    conn.close()
    if not row:
        return {}
    data = json.loads(row["data"])
    stats_data = data.get("stats", {})
    if not isinstance(stats_data, dict):
        return {}
    player_stats = stats_data.get(player_name, {})
    if not isinstance(player_stats, dict):
        return {}
    result = {}
    for sname, sval in player_stats.items():
        if isinstance(sval, dict):
            result[sname] = sval.get("description", "")
        elif isinstance(sval, str):
            result[sname] = sval
    return result


def get_player_stat_types(player_name: str) -> dict[str, str]:
    conn = get_connection()
    row = conn.execute(
        "SELECT data FROM world_entities WHERE entity_type = 'rule' AND name = 'stats_system'"
    ).fetchone()
    conn.close()
    if not row:
        return {}
    data = json.loads(row["data"])
    stats_data = data.get("stats", {})
    if not isinstance(stats_data, dict):
        return {}
    player_stats = stats_data.get(player_name, {})
    if not isinstance(player_stats, dict):
        return {}
    result = {}
    for sname, sval in player_stats.items():
        if isinstance(sval, dict):
            result[sname] = sval.get("stat_type", "other")
        else:
            result[sname] = "other"
    return result


def clear_game_data() -> None:
    conn = get_connection()
    conn.executescript("""
        DELETE FROM chat_history;
        DELETE FROM world_entities;
        DELETE FROM players;
        UPDATE game_session SET
            game_status = 'lobby',
            turn_order = '[]',
            current_turn_index = 0,
            timer_ends_at = '',
            global_lore = '',
            setting_blob = '{}'
        WHERE session_id = 1;
    """)
    conn.commit()
    conn.close()
