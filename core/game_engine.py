import json
import random
import re
from db.database import get_connection, get_session, update_session
from models.models import SessionModel


def roll_dice(modifier: int = 0) -> int:
    return random.randint(1, 20) + modifier


def _extract_speed_stat(stats: dict[str, int]) -> int:
    keywords = [
        "скорость", "ловкость", "реакция", "проворство",
        "speed", "dexterity", "agility", "reflex", "initiative",
    ]
    for key in stats:
        key_lower = key.lower()
        if any(kw in key_lower for kw in keywords):
            return stats[key]
    return max(stats.values()) if stats else 0


def start_combat_mode(player_ids: list[int]) -> list[dict]:
    conn = get_connection()
    cur = conn.cursor()

    turn_order: list[dict] = []

    for pid in player_ids:
        row = cur.execute("SELECT id, name, stats FROM players WHERE id = ?", (pid,)).fetchone()
        if row is None:
            continue
        stats = json.loads(row["stats"])
        modifier = _extract_speed_stat(stats)
        initiative = roll_dice(modifier)
        turn_order.append({
            "entity_type": "player",
            "entity_id": row["id"],
            "name": row["name"],
            "initiative": initiative,
            "is_active": False,
        })

    npc_rows = cur.execute(
        "SELECT id, name, data FROM world_entities WHERE entity_type = 'npc'"
    ).fetchall()
    for row in npc_rows:
        data = json.loads(row["data"])
        npc_stats = data.get("stats", {})
        modifier = _extract_speed_stat(npc_stats)
        initiative = roll_dice(modifier)
        turn_order.append({
            "entity_type": "npc",
            "entity_id": row["id"],
            "name": row["name"],
            "initiative": initiative,
            "is_active": False,
        })

    turn_order.sort(key=lambda x: x["initiative"], reverse=True)

    if turn_order:
        turn_order[0]["is_active"] = True

    conn.execute(
        """UPDATE game_session
           SET game_status = 'combat',
               turn_order = ?,
               current_turn_index = 0
           WHERE session_id = 1""",
        (json.dumps(turn_order, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()

    return turn_order


def advance_turn() -> SessionModel:
    session = get_session()
    if session is None:
        raise RuntimeError("No active session found")

    turn_order = session.turn_order
    if not turn_order:
        return session

    for entry in turn_order:
        entry["is_active"] = False

    next_index = session.current_turn_index + 1
    if next_index >= len(turn_order):
        next_index = 0

    turn_order[next_index]["is_active"] = True

    updated = SessionModel(
        game_status=session.game_status,
        turn_order=turn_order,
        current_turn_index=next_index,
    )
    update_session(updated)
    return updated
