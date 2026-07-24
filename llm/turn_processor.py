import json
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator

from db.database import (
    add_chat_message,
    add_or_update_entity,
    get_connection,
    get_session,
    update_session,
)
from models.models import ChatMessageModel, WorldEntityModel, SessionModel
from llm.context_builder import build_stateless_prompt
from core.game_engine import start_combat_mode, advance_turn


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


def _apply_mechanical_action(action: dict[str, Any]) -> None:
    action_type = action.get("type")
    if action_type not in ("damage", "heal"):
        return

    target = action.get("target", "")
    value = action.get("value", 0)

    if not target.startswith("player:"):
        return

    try:
        player_id = int(target.split(":")[1])
    except (IndexError, ValueError):
        return

    conn = get_connection()
    row = conn.execute(
        "SELECT hp_current, hp_max FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return

    hp = row["hp_current"]
    hp_max = row["hp_max"]

    if action_type == "damage":
        hp = max(0, hp - value)
    elif action_type == "heal":
        hp = min(hp_max, hp + value)

    conn.execute("UPDATE players SET hp_current = ? WHERE id = ?", (hp, player_id))
    conn.commit()
    conn.close()


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
    player_id: int,
    action_text: str,
    *,
    is_action: bool = True,
    save_message: bool = True,
    http_client: httpx.AsyncClient | None = None,
) -> TurnResponse:
    print(f"[PROCESS] player={player_id} action='{action_text[:60]}...'")

    # 1. Сохраняем действие игрока (если save_message)
    if save_message:
        label = "action" if is_action else "message"
        print(f"[PROCESS] 1/8 saving {label} to chat_history...")
        conn = get_connection()
        player_row = conn.execute("SELECT name FROM players WHERE id=?", (player_id,)).fetchone()
        conn.close()
        player_name = player_row["name"] if player_row else f"Player{player_id}"
        msg = ChatMessageModel(
            sender=player_name,
            message_text=action_text,
            is_action=is_action,
            timestamp="",
        )
        add_chat_message(msg, player_id=player_id)
        print(f"[PROCESS] 1/8 done")
    else:
        print("[PROCESS] 1/8 skip — save_message=False")

    # 2. Собираем пятислойный контекст
    print("[PROCESS] 2/8 building stateless prompt...")
    prompt = build_stateless_prompt(active_player_id=player_id, current_action=action_text)
    messages = prompt["messages"]
    print(f"[PROCESS] 2/8 done — system:{len(messages[0]['content'])} chars, user:{len(messages[1]['content'])} chars")

    # 3. Отправляем в LLM
    print("[PROCESS] 3/8 calling LLM API...")
    raw = await _call_llm(messages, http_client=http_client)
    print(f"[PROCESS] 3/8 done — response {len(raw)} chars")

    # 4. Парсим ответ
    print("[PROCESS] 4/8 parsing turn response...")
    print(f"RAW AI RESPONSE: {raw}")
    try:
        data = json.loads(raw)
        turn = TurnResponse(**data)
        print("[PROCESS] 4/8 done — parsed OK")
    except Exception as e:
        print(f"[PROCESS] 4/8 ERROR — {e}")
        turn = TurnResponse(
            narrative_text=f"Ошибка обработки ответа: {e}. Попробуйте ещё раз.",
            game_state_trigger="none",
        )

    # 5. Записываем narrative_text в чат
    print("[PROCESS] 5/8 saving GM narrative to chat_history...")
    gm_msg = ChatMessageModel(
        sender="GM",
        message_text=turn.narrative_text,
        is_action=False,
        timestamp="",
    )
    add_chat_message(gm_msg)
    print("[PROCESS] 5/8 done")

    # 6. Сохраняем новые сущности
    if turn.new_lore_discovered:
        print(f"[PROCESS] 6/8 saving {len(turn.new_lore_discovered)} lore entities...")
        _apply_new_lore(turn.new_lore_discovered)
        print("[PROCESS] 6/8 done")
    else:
        print("[PROCESS] 6/8 skip — no new lore")

    # 7. Обрабатываем механические действия
    if turn.mechanical_action:
        print(f"[PROCESS] 7/8 applying mechanical action: {turn.mechanical_action}")
        _apply_mechanical_action(turn.mechanical_action)
        print("[PROCESS] 7/8 done")
    else:
        print("[PROCESS] 7/8 skip — no mechanical action")

    # 8. Триггерим смену режима боя
    if turn.game_state_trigger == "combat_start":
        print("[PROCESS] 8/8 combat_start triggered")
        conn = get_connection()
        rows = conn.execute("SELECT id FROM players").fetchall()
        conn.close()
        start_combat_mode([r["id"] for r in rows])
    elif turn.game_state_trigger == "combat_end":
        print("[PROCESS] 8/8 combat_end triggered")
        session = get_session()
        if session:
            updated = SessionModel(
                game_status="exploration",
                turn_order=session.turn_order,
                current_turn_index=session.current_turn_index,
            )
            update_session(updated)
    else:
        print("[PROCESS] 8/8 skip — no state trigger")

    return turn
