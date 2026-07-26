import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

load_dotenv()


class PhaseZeroOutput(BaseModel):
    setting_name: str = Field(description="Название созданного мира")
    setting_description: str = Field(description="Описание сеттинга, логично объединяющего концепты всех игроков")
    global_conflict: str = Field(description="Глобальный конфликт — завязка сюжета и общая цель для группы")
    character_stats_templates: dict[str, dict[str, str]] = Field(
        description="Словарь, где ключ — имя персонажа, значение — словарь его персональных характеристик {название стата: описание}. У каждого персонажа свои 2–4 стата, подходящие его концепту"
    )
    character_classes: dict[str, str] = Field(
        description="Словарь, где ключ — имя персонажа (из списка игроков), значение — его класс/архетип, уникальный для этого мира"
    )
    initial_narrative_text: str = Field(description="Вступительный текст, погружающий игроков в мир")

    @model_validator(mode="before")
    @classmethod
    def _normalize_stats(cls, data: Any) -> Any:
        stats = data.get("character_stats_templates")
        if isinstance(stats, list):
            normalized: dict[str, dict[str, str]] = {}
            for item in stats:
                if isinstance(item, dict):
                    player_name = item.get("player_name") or item.get("name") or item.get("player", "")
                    per_player_stats = item.get("stats") or item.get("characteristics") or {}
                    if isinstance(per_player_stats, list):
                        resolved = {}
                        for s in per_player_stats:
                            sn = s.get("name") or s.get("stat_name", "")
                            sd = s.get("description") or s.get("desc", "")
                            if sn:
                                resolved[sn] = sd
                        per_player_stats = resolved
                    if player_name and isinstance(per_player_stats, dict):
                        normalized[player_name] = per_player_stats
            data["character_stats_templates"] = normalized
        elif isinstance(stats, dict) and stats:
            first_val = next(iter(stats.values()))
            if not isinstance(first_val, dict):
                players = data.get("character_classes", {})
                if players:
                    data["character_stats_templates"] = {p: dict(stats) for p in players}
        return data


_PROMPT_TEMPLATE = """Ты — Гейм-Мастер, создающий новый игровой мир для настольной ролевой игры.

Игроки предоставили следующие концепты своих персонажей:

{descriptions}

Проанализируй эти концепты и создай единый уникальный сеттинг, который логично объединяет их всех.
Твоя задача:
1. Придумать название мира (setting_name)
2. Описать единый сеттинг, в который органично вписываются все персонажи (setting_description)
3. Придумать глобальный конфликт — завязку сюжета и общую цель, которая объединит группу (global_conflict)
4. Для КАЖДОГО персонажа сгенерировать 2–4 персональные характеристики, отражающие его уникальную природу и концепт. Не навязывай всем один набор: у бесплотного духа могут быть «Эктоплазменная плотность» и «Сила воли», а у морской черепахи — «Прочность панциря» и «Скорость в воде». Если стат объективно подходит нескольким персонажам — он может пересекаться. character_stats_templates: ключ — ИМЯ персонажа (ровно как в списке), значение — словарь его характеристик (название стата: описание).
5. Для каждого персонажа придумать уникальный класс/архетип, соответствующий его концепту и сеттингу (character_classes: ключ — имя персонажа, значение — краткое название класса/архетипа, 2-4 слова, без описания)
6. Написать атмосферный вступительный текст (initial_narrative_text), который погружает игроков в мир и даёт им первую сцену

Ответь строго в JSON по указанной схеме. Без markdown-разметки, только чистый JSON.
ВАЖНО: весь ответ должен быть на том же языке, что и описания персонажей."""


def _build_system_prompt() -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "Ты — креативный Гейм-Мастер. Отвечай строго в JSON по заданной схеме.",
        }
    ]


def _build_user_prompt(player_descriptions: list[str]) -> str:
    formatted = "\n\n".join(
        f"--- Персонаж {i + 1} ---\n{desc}"
        for i, desc in enumerate(player_descriptions)
    )
    return _PROMPT_TEMPLATE.format(descriptions=formatted)


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        for line in raw.splitlines():
            if line.startswith("```"):
                raw = raw[len(line) :].strip()
                break
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return json.loads(raw)


async def generate_initial_world(
    player_descriptions: list[str],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> PhaseZeroOutput:
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
            "messages": [
                *_build_system_prompt(),
                {"role": "user", "content": _build_user_prompt(player_descriptions)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.8,
        }

        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()

        raw_content = response.json()["choices"][0]["message"]["content"]
        data = _extract_json(raw_content)

        try:
            return PhaseZeroOutput(**data)
        except Exception as e:
            import json as _json
            print(f"[ai_generator] RAW LLM response: {raw_content}")
            print(f"[ai_generator] Parsed data: {_json.dumps(data, ensure_ascii=False, indent=2)}")
            raise

    finally:
        if close_client:
            await client.aclose()
