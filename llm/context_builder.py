import json
import re
from db.database import get_connection

_SYSTEM_MANIFEST = """Ты — Гейм-Мастер (Game Master) в настольной ролевой игре.
Твоя задача — вести захватывающее повествование для группы игроков.

Правила:
- Ты работаешь по принципу "Да, и": принимай любые валидные действия игроков и развивай их.
- Провалы не блокируют сюжет — они создают новые повороты.
- Ты генерируешь новые элементы мира (NPC, фракции, локации, события) на ходу.
- Отвечай строго в JSON-формате, описанном в контракте.
- Не нарушай базовые правила мироустройства, установленные в Фазе 0.
- Броски кубиков уже сделаны бэкендом. Не меняй их результаты — опиши их нарративно.
- Пиши на языке игроков.
- У каждого игрока есть предыстория (поле "Предыстория" в блоке персонажа). Ты обязан органично вплетать эти предыстории в повествование: создавать отсылки к прошлому персонажей, вовлекать их личные цели и мотивации в глобальный сюжет, давать персонажам моменты, связанные с их прошлым.

Твой ответ должен быть строго в формате JSON и содержать исключительно следующие ключи:
- narrative_text (строка с описанием происходящего)
- game_state_trigger (строка: "none", "combat_start" или "combat_end")
- new_lore_discovered (список словарей с новыми фактами о мире; если нечего добавить — пустой список [])
- mechanical_action (словарь с механическими действиями; если нечего — пустой словарь {})

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
            stats_desc = "\n".join(
                f"  {k}: {v}" for k, v in data.get("stats", {}).items()
            )
            parts.append("[СИСТЕМА ХАРАКТЕРИСТИК]\n" + stats_desc)

    return "\n\n".join(parts)


def _get_players_state() -> str:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()

    lines = []
    for row in rows:
        stats = json.loads(row["stats"])
        inv = json.loads(row["inventory"])
        effects = json.loads(row["status_effects"])
        backstory = row["backstory"] if "backstory" in row.keys() and row["backstory"] else "Не указана"
        stats_str = " | ".join(f"{k} {v}" for k, v in stats.items())
        inv_str = ", ".join(inv) if inv else "пусто"
        effects_str = ", ".join(effects) if effects else "нет"
        lines.append(
            f"[{row['name']}]  HP {row['hp_current']}/{row['hp_max']}  |  "
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


def _get_recent_history(limit: int = 15) -> str:
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message_text, is_action FROM chat_history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    lines = []
    for row in reversed(rows):
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
        f"Имя: {r['name']}, Предыстория: {r['backstory']}"
        for r in rows
    ]


def build_stateless_prompt(active_player_id: int, current_action: str) -> dict:
    layer_1 = _SYSTEM_MANIFEST

    layer_2 = _get_setting_and_conflict()

    layer_3 = _get_players_state()

    keywords = _extract_keywords(current_action)
    layer_4 = _get_relevant_lore(keywords)

    layer_5 = _get_recent_history()

    user_parts = []
    if layer_2:
        user_parts.append("=== МИР ===")
        user_parts.append(layer_2)
    if layer_3:
        user_parts.append("\n=== СОСТОЯНИЕ ПЕРСОНАЖЕЙ ===")
        user_parts.append(layer_3)
    if layer_4:
        user_parts.append("\n=== РЕЛЕВАНТНЫЙ ЛОР ===")
        user_parts.append(layer_4)
    if layer_5:
        user_parts.append("\n=== ПОСЛЕДНИЕ СОБЫТИЯ ===")
        user_parts.append(layer_5)
    user_parts.append(f"\n=== ДЕЙСТВИЕ ИГРОКА ===")
    user_parts.append(current_action)

    return {
        "messages": [
            {"role": "system", "content": layer_1},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
    }
