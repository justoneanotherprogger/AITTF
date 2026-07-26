import json
import re
from db.database import get_connection

_SYSTEM_MANIFEST = """Ты — Рассказчик (Game Master) в настольной ролевой игре.
Твоя задача — вести захватывающее повествование для группы игроков.

Правила:
- Ты — рассказчик, а не игрок. Твоя роль — описывать мир, NPC, окружение и последствия действий игроков.
- Все персонажи, перечисленные в блоке "СОСТОЯНИЕ ПЕРСОНАЖЕЙ", — это персонажи игроков. НИКОГДА не описывай, что они говорят или делают. За них говорят и действуют только их игроки.
- Если игрок заявляет действие, опиши результат или реакцию мира на это действие, а не само действие и не реплику персонажа.
- Ты можешь говорить только за NPC, монстров и прочих неигровых персонажей. За персонажей игроков не говоришь никогда.
- Ты работаешь по принципу "Да, и": принимай любые валидные действия игроков и развивай их.
- Провалы не блокируют сюжет — они создают новые повороты.
- Ты генерируешь новые элементы мира (NPC, фракции, локации, события) на ходу.
- Отвечай строго в JSON-формате, описанном в контракте.
- Не нарушай базовые правила мироустройства, установленные в Фазе 0.
- Броски кубиков уже сделаны бэкендом. Не меняй их результаты — опиши их нарративно.
- Генерируй весь игровой текст, лор, сеттинг и реплики NPC СТРОГО на том языке, на котором написаны имена и описания персонажей. Если имена и описания на русском — пиши только на русском. Никогда не смешивай языки в одном ответе.
- Ответ ВСЕГДА должен быть ровно одним JSON-объектом, соответствующим схеме TurnResponse. Никакого текста до или после JSON — ни пояснений, ни markdown, ни заголовков.
- У каждого игрока есть предыстория (поле "Предыстория" в блоке персонажа). Ты обязан органично вплетать эти предыстории в повествование: создавать отсылки к прошлому персонажей, вовлекать их личные цели и мотивации в глобальный сюжет, давать персонажам моменты, связанные с их прошлым.
- В блоке "ПОСЛЕДНИЕ СОБЫТИЯ" передана вся недавняя история игры, включая твои предыдущие ответы GM и весь произошедший лор. ВСЕГДА учитывай её при формировании ответа: события, описанные GM ранее, — неотъемлемая часть сюжета, они не забываются.
- Блок "СТАТУС ИГРЫ" показывает текущий режим игры. "exploration" — обычное исследование мира, игроки свободно действуют. "combat" — боевая ситуация, все действия и описания должны соответствовать бою.

Твой ответ должен быть строго в формате JSON и содержать исключительно следующие ключи:
- narrative_text (строка с описанием происходящего)
- game_state_trigger (строка: "none", "combat_start" или "combat_end")
- new_lore_discovered (список словарей с новыми фактами о мире; если нечего добавить — пустой список [])
- mechanical_action (словарь с механическими действиями; возможные типы: "damage" — нанести урон, "heal" — вылечить, "stat_change" — изменить характеристику персонажа на дельту +1/-1; target указывается как "player:<id>", где <id> — числовой ID персонажа из блока СОСТОЯНИЕ ПЕРСОНАЖЕЙ; если нечего — пустой словарь {})

Запрещено использовать синонимы вроде description, narrative, elements или state.
Если нечего вернуть в поле — оставь его пустым (строка "none", пустой список [] или пустой словарь {}).

Пример идеального JSON-ответа:
{
  "narrative_text": "Вы заходите в тёмную пещеру. В воздухе пахнет сыростью и плесенью.",
  "game_state_trigger": "none",
  "new_lore_discovered": [],
  "mechanical_action": {}
}

Пример с боем:
{
  "narrative_text": "Гоблин замахивается дубиной и наносит вам удар.",
  "game_state_trigger": "combat_start",
  "new_lore_discovered": [
    {"entity_type": "npc", "name": "Гоблин-вожак", "data": {"description": "Здоровенный гоблин с дубиной"}, "tags": ["гоблин", "враг"]}
  ],
  "mechanical_action": {"type": "damage", "target": "player:1", "value": 5}
}

Пример с изменением характеристики:
{
  "narrative_text": "Поток древней энергии пронзает Фаэргаса, его воля закаляется.",
  "game_state_trigger": "none",
  "new_lore_discovered": [],
  "mechanical_action": {"type": "stat_change", "target": "player:1", "stat": "Сила воли", "value": 1}
}"""


def _get_setting_and_conflict() -> str:
    conn = get_connection()
    rows = conn.execute(
        "SELECT entity_type, name, data FROM world_entities WHERE entity_type IN ('setting', 'rule')"
    ).fetchall()
    conn.close()

    parts = []
    for row in rows:
        data = json.loads(row["data"])
        if row["entity_type"] == "setting":
            parts.append(f"[СЕТТИНГ: {row['name']}]")
            parts.append(data.get("description", ""))
        elif row["name"] == "global_conflict":
            parts.append("[ГЛОБАЛЬНЫЙ КОНФЛИКТ]")
            parts.append(data.get("description", ""))
        elif row["name"] == "stats_system":
            raw = data.get("stats", {})
            lines = []
            for pname, pstats in raw.items():
                stat_parts = []
                for sname, sval in pstats.items():
                    if isinstance(sval, dict):
                        desc = sval.get("description", "")
                        iv = sval.get("initial_value", 1)
                        st = sval.get("stat_type", "other")
                        stat_parts.append(f"{sname} ({iv}, {st}): {desc}")
                    else:
                        stat_parts.append(f"{sname}: {sval}")
                lines.append(f"  {pname}: {', '.join(stat_parts)}")
            parts.append("[СИСТЕМА ХАРАКТЕРИСТИК]\n" + "\n".join(lines))

    return "\n\n".join(parts)


def _get_game_status() -> str:
    from db.database import get_session
    session = get_session()
    if not session:
        return "lobby"
    return session.game_status


def _get_players_state() -> str:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    lines = ["Персонажи игроков (за них ты НЕ говоришь):"]
    for row in rows:
        stats = json.loads(row["stats"])
        inv = json.loads(row["inventory"])
        effects = json.loads(row["status_effects"])
        backstory = row["backstory"] if "backstory" in row.keys() and row["backstory"] else "Не указана"
        stats_str = " | ".join(f"{k} {v}" for k, v in stats.items())
        inv_str = ", ".join(inv) if inv else "пусто"
        effects_str = ", ".join(effects) if effects else "нет"
        lines.append(
            f"[{row['name']}] (id={row['id']})  HP {row['hp_current']}/{row['hp_max']}  |  "
            f"{stats_str}  |  Предыстория: {backstory}  |  "
            f"Инвентарь: {inv_str}  |  Эффекты: {effects_str}"
        )

    return "\n".join(lines)


def _extract_keywords(text: str) -> list[str]:
    words = re.findall(r"[а-яёА-ЯЁa-zA-Z]+", text)
    return [w.lower() for w in words if len(w) > 2]


def _get_relevant_lore(keywords: list[str]) -> str:
    if not keywords:
        return ""

    conn = get_connection()
    rows = conn.execute(
        "SELECT entity_type, name, data, tags FROM world_entities"
    ).fetchall()
    conn.close()

    matched: list[str] = []
    for row in rows:
        data = json.loads(row["data"])
        tags = json.loads(row["tags"])
        searchable = f"{row['name']} {data.get('description', '')} {' '.join(tags)}".lower()
        if any(kw in searchable for kw in keywords):
            desc = data.get("description", "")
            matched.append(f"[{row['entity_type'].upper()}] {row['name']}: {desc}")

    return "\n\n".join(matched) if matched else ""


def _get_last_gm_message_id() -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(id) as max_id FROM chat_history WHERE sender = 'GM'"
    ).fetchone()
    conn.close()
    return row["max_id"] if row and row["max_id"] else 0


def _get_recent_history() -> str:
    last_gm_id = _get_last_gm_message_id()
    conn = get_connection()

    gm_ids = conn.execute(
        "SELECT id FROM chat_history WHERE sender = 'GM' AND id <= ? ORDER BY id DESC LIMIT 10",
        (last_gm_id,),
    ).fetchall()
    boundary_id = gm_ids[-1]["id"] if gm_ids else 0

    rows = conn.execute(
        "SELECT sender, message_text, is_action FROM chat_history WHERE id >= ? AND id <= ? ORDER BY id ASC",
        (boundary_id, last_gm_id),
    ).fetchall()
    conn.close()

    lines = []
    for row in rows:
        if row["is_action"]:
            lines.append(f"Персонаж {row['sender']} заявляет действие: {row['message_text']}")
        else:
            lines.append(f"Персонаж {row['sender']} говорит вслух: {row['message_text']}")

    return "\n".join(lines)


def get_pending_actions() -> str:
    last_gm_id = _get_last_gm_message_id()
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message_text, is_action FROM chat_history WHERE sender != 'GM' AND id > ? ORDER BY id ASC",
        (last_gm_id,),
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    lines = []
    for row in rows:
        if row["is_action"]:
            lines.append(f"Персонаж {row['sender']} заявляет действие: {row['message_text']}")
        else:
            lines.append(f"Персонаж {row['sender']} говорит вслух: {row['message_text']}")

    return "\n".join(lines)


def build_player_descriptions() -> list[str]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    return [
        f"Имя персонажа: {r['name']}\nПредыстория этого персонажа: {r['backstory']}"
        for r in rows
    ]


def build_stateless_prompt() -> dict:
    layer_1 = _SYSTEM_MANIFEST

    layer_2 = _get_setting_and_conflict()

    game_status = _get_game_status()

    layer_3 = _get_players_state()

    pending = get_pending_actions()

    keywords = _extract_keywords(pending)
    layer_4 = _get_relevant_lore(keywords)

    layer_5 = _get_recent_history()

    user_parts = []
    if layer_2:
        user_parts.append("=== МИР ===")
        user_parts.append(layer_2)
    if game_status in ("exploration", "combat"):
        user_parts.append("\n=== СТАТУС ИГРЫ ===")
        user_parts.append(f"Режим: {game_status}")
    if layer_3:
        user_parts.append("\n=== СОСТОЯНИЕ ПЕРСОНАЖЕЙ ===")
        user_parts.append(layer_3)
    if layer_4:
        user_parts.append("\n=== РЕЛЕВАНТНЫЙ ЛОР ===")
        user_parts.append(layer_4)
    if layer_5:
        user_parts.append("\n=== ПОСЛЕДНИЕ СОБЫТИЯ ===")
        user_parts.append(layer_5)
    if pending:
        user_parts.append("\n=== ТЕКУЩИЕ ДЕЙСТВИЯ ===")
        user_parts.append(pending)

    return {
        "messages": [
            {"role": "system", "content": layer_1},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
    }
