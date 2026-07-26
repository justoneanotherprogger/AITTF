import json
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator

from db.database import (
    add_chat_message,
    add_or_update_entity,
    get_connection,
    get_player_stat_types,
    get_session,
    update_session,
)
from models.models import ChatMessageModel, WorldEntityModel, SessionModel
from llm.context_builder import build_stateless_prompt
from core.game_engine import start_combat_mode, advance_turn, calc_hp_max


class TurnResponse(BaseModel):
    narrative_text: str = Field(description="Художественное описание результата действия")
    game_state_trigger: str = Field(
        default="none",
        description="Статус игры: none, combat_start, combat_end",
    )
    new_lore_discovered: list[dict[str, Any]] | None = Field(
        default_factory=list,
        description="Список новых сущностей мира (npc, faction, location, item, event). "
        "Каждый объект содержит entity_type, name, data (dict), tags (list)",
    )
    mechanical_action: dict[str, Any] | None = Field(
        default_factory=dict,
        description="Механическое действие: {type: damage|heal, target: 'player:<id>', value: N}",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "narrative" in data and "narrative_text" not in data:
                data["narrative_text"] = data.pop("narrative")

            if "new_elements" in data and "new_lore_discovered" not in data:
                data["new_lore_discovered"] = data.pop("new_elements")

            if "game_state_trigger" not in data:
                data["game_state_trigger"] = "none"

            if "mechanical_action" not in data:
                data["mechanical_action"] = {}

            gst = data.get("game_state_trigger")
            if not isinstance(gst, str):
                data["game_state_trigger"] = str(gst) if gst is not None else "none"

            lore = data.get("new_lore_discovered")
            if lore is None or lore == "":
                data["new_lore_discovered"] = []

            mech = data.get("mechanical_action")
            if mech is None or mech == "":
                data["mechanical_action"] = {}
        return data


async def _call_llm(
    messages: list[dict],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    api_key = (
        api_key
        or os.environ.get("API_KEY")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    if not api_key:
        raise RuntimeError(
            "API_KEY не задан. Укажите его в переменной окружения "
            "или в файле .env"
        )
    base_url = (
        base_url
        or os.environ.get("BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
    )
    if not base_url:
        raise RuntimeError(
            "BASE_URL не задан. Укажите его в переменной окружения "
            "или в файле .env"
        )
    base_url = base_url.rstrip("/")
    model = (
        model
        or os.environ.get("MODEL")
        or os.environ.get("OPENAI_MODEL", "")
    )

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    try:
        payload = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
        }

        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()

        return response.json()["choices"][0]["message"]["content"]
    finally:
        if close_client:
            await client.aclose()


def _apply_mechanical_action(action: dict[str, Any]) -> dict | None:
    action_type = action.get("type")
    if action_type not in ("damage", "heal", "stat_change"):
        return None

    target = action.get("target", "")
    value = action.get("value", 0)

    if not target.startswith("player:"):
        return None

    try:
        player_id = int(target.split(":")[1])
    except (IndexError, ValueError):
        return None

    conn = get_connection()
    row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        conn.close()
        return None

    if action_type == "stat_change":
        stat_name = action.get("stat", "")
        if not stat_name:
            conn.close()
            return None
        stats = json.loads(row["stats"])
        current = stats.get(stat_name, 0)
        new_val = max(0, current + value)
        stats[stat_name] = new_val
        conn.execute("UPDATE players SET stats = ? WHERE id = ?", (json.dumps(stats, ensure_ascii=False), player_id))

        stat_types = get_player_stat_types(row["name"])
        if stat_types.get(stat_name) == "defensive":
            def_sum = sum(stats[s] for s, t in stat_types.items() if t == "defensive")
            new_hp_max = calc_hp_max(def_sum)
            old_hp_max = row["hp_max"]
            old_hp_cur = row["hp_current"]
            new_hp_cur = max(0, old_hp_cur + (new_hp_max - old_hp_max))
            conn.execute(
                "UPDATE players SET hp_max = ?, hp_current = ? WHERE id = ?",
                (new_hp_max, new_hp_cur, player_id),
            )

        conn.commit()
        conn.close()
        return {"stat": stat_name, "player_id": player_id, "delta": value, "new_value": new_val}

    hp = row["hp_current"]
    hp_max = row["hp_max"]

    if action_type == "damage":
        hp = max(0, hp - value)
    elif action_type == "heal":
        hp = min(hp_max, hp + value)

    conn.execute("UPDATE players SET hp_current = ? WHERE id = ?", (hp, player_id))
    conn.commit()
    conn.close()
    return None


def _apply_new_lore(entities: list[dict[str, Any]]) -> None:
    for ent in entities:
        try:
            model = WorldEntityModel(
                entity_type=ent.get("entity_type", "unknown"),
                name=ent.get("name", "unnamed"),
                data=ent.get("data", {}),
            )
            add_or_update_entity(model)
        except Exception:
            continue


async def process_player_action(
    http_client: httpx.AsyncClient | None = None,
) -> tuple[TurnResponse, list[str]]:
    # 1. Собираем контекст (все pending-сообщения читаются из БД)
    print("[PROCESS] 1/6 building stateless prompt...")
    prompt = build_stateless_prompt()
    messages = prompt["messages"]
    print(f"[PROCESS] 1/6 done — system:{len(messages[0]['content'])} chars, user:{len(messages[1]['content'])} chars")

    # 2. Отправляем в LLM
    print("[PROCESS] 2/6 calling LLM API...")
    raw = await _call_llm(messages, http_client=http_client)
    print(f"[PROCESS] 2/6 done — response {len(raw)} chars")

    # 3. Парсим ответ
    print("[PROCESS] 3/6 parsing turn response...")
    print(f"RAW AI RESPONSE: {raw}")
    try:
        data = json.loads(raw)
        turn = TurnResponse(**data)
        print("[PROCESS] 3/6 done — parsed OK")
    except Exception as e:
        print(f"[PROCESS] 3/6 ERROR — {e}")
        turn = TurnResponse(
            narrative_text=f"Ошибка обработки ответа: {e}. Попробуйте ещё раз.",
            game_state_trigger="none",
        )

    # 4. Записываем narrative_text в чат
    print("[PROCESS] 4/6 saving GM narrative to chat_history...")
    gm_msg = ChatMessageModel(
        sender="GM",
        message_text=turn.narrative_text,
        is_action=False,
        timestamp="",
    )
    add_chat_message(gm_msg)
    print("[PROCESS] 4/6 done")

    # 5. Сохраняем новые сущности и применяем механические действия
    if turn.new_lore_discovered:
        print(f"[PROCESS] 5/6 saving {len(turn.new_lore_discovered)} lore entities...")
        _apply_new_lore(turn.new_lore_discovered)
        print("[PROCESS] 5/6 done")
    else:
        print("[PROCESS] 5/6 skip — no new lore")

    extra_broadcasts: list[str] = []
    if turn.mechanical_action:
        print(f"[PROCESS] 5/6 applying mechanical action: {turn.mechanical_action}")
        result = _apply_mechanical_action(turn.mechanical_action)
        if result:
            pname = "Неизвестно"
            conn = get_connection()
            row = conn.execute("SELECT name FROM players WHERE id = ?", (result["player_id"],)).fetchone()
            if row:
                pname = row["name"]
            conn.close()
            sign = "+" if result["delta"] > 0 else ""
            notice = f"<strong>⚡ {result['stat']}</strong> персонажа {pname} изменена ({sign}{result['delta']}, теперь {result['new_value']})"
            add_chat_message(ChatMessageModel(sender="GM", message_text=notice, is_action=False, timestamp=""))
            extra_broadcasts.append(
                f'<div hx-swap-oob="beforeend:#chat-messages">'
                f'<div class="inline-block max-w-[80%] bg-emerald-700/30 border-emerald-600/20 border rounded-xl px-4 py-2 text-sm" data-author-id="system">'
                f'<span class="text-xs font-semibold text-gray-400 block mb-0.5">GM</span>'
                f'{notice}'
                f'</div></div>'
            )
        print("[PROCESS] 5/6 done")
    else:
        print("[PROCESS] 5/6 skip — no mechanical action")

    # 6. Триггерим смену режима боя
    if turn.game_state_trigger == "combat_start":
        print("[PROCESS] 6/6 combat_start triggered")
        conn = get_connection()
        rows = conn.execute("SELECT id FROM players").fetchall()
        conn.close()
        start_combat_mode([r["id"] for r in rows])
    elif turn.game_state_trigger == "combat_end":
        print("[PROCESS] 6/6 combat_end triggered")
        session = get_session()
        if session:
            updated = SessionModel(
                game_status="exploration",
                turn_order=session.turn_order,
                current_turn_index=session.current_turn_index,
            )
            update_session(updated)
    else:
        print("[PROCESS] 6/6 skip — no state trigger")

    return turn, extra_broadcasts
