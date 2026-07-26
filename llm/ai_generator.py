import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from models.models import StatDef, ClassDef, StatType

load_dotenv()


class PhaseZeroOutput(BaseModel):
    setting_name: str = Field(description="Название созданного мира")
    setting_description: str = Field(description="Описание сеттинга, логично объединяющего концепты всех игроков")
    global_conflict: str = Field(description="Глобальный конфликт — завязка сюжета и общая цель для группы")
    character_stats_templates: dict[str, dict[str, StatDef]] = Field(
        description="Словарь: имя персонажа -> {название стата: {description: описание, initial_value: число 1-10}}. У каждого свои 2-4 стата"
    )
    character_classes: dict[str, ClassDef] = Field(
        description="Словарь, где ключ — имя персонажа, значение — {name: название класса, description: описание класса (2-4 предложения)}"
    )
    initial_narrative_text: str = Field(description="Вступительный текст, погружающий игроков в мир")

    @model_validator(mode="before")
    @classmethod
    def _normalize_stats(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        stats = data.get("character_stats_templates")
        if isinstance(stats, dict):
            normalized = {}
            for pname, pstats in stats.items():
                if isinstance(pstats, dict):
                    resolved = {}
                    for sname, sval in pstats.items():
                        if isinstance(sval, dict):
                            sval.setdefault("stat_type", "other")
                            resolved[sname] = StatDef(**sval)
                        elif isinstance(sval, (int, str)):
                            resolved[sname] = StatDef(description=str(sval), initial_value=1)
                    if resolved:
                        normalized[pname] = resolved
            data["character_stats_templates"] = normalized

        classes = data.get("character_classes")
        if isinstance(classes, dict):
            normalized_cls = {}
            for pname, cval in classes.items():
                if isinstance(cval, dict):
                    normalized_cls[pname] = ClassDef(**cval)
                elif isinstance(cval, str):
                    normalized_cls[pname] = ClassDef(name=cval, description="")
            data["character_classes"] = normalized_cls
        return data


_PROMPT_TEMPLATE = """Ты — Гейм-Мастер, создающий новый игровой мир для настольной ролевой игры.

Игроки предоставили следующие концепты своих персонажей:

{descriptions}

Проанализируй эти концепты и создай единый уникальный сеттинг, который логично объединяет их всех.
Твоя задача:
1. Придумать название мира (setting_name)
2. Описать единый сеттинг, в который органично вписываются все персонажи (setting_description)
3. Придумать глобальный конфликт — завязку сюжета и общую цель, которая объединит группу (global_conflict)
4. Для КАЖДОГО персонажа сгенерировать 2–4 персональные характеристики, отражающие его уникальную природу и концепт. Не навязывай всем один набор: у бесплотного духа могут быть «Эктоплазменная плотность» и «Сила воли», а у морской черепахи — «Прочность панциря» и «Скорость в воде». Если стат объективно подходит нескольким персонажам — он может пересекаться. character_stats_templates: ключ — ИМЯ персонажа (ровно как в списке), значение — словарь его характеристик, где для КАЖДОГО стата указываются три поля: "description" (описание стата), "initial_value" (целое число от 1 до 10, отражающее, насколько эта характеристика развита у данного персонажа) и "stat_type" (тип характеристики: "offensive" — повышает атакующий потенциал, урон; "defensive" — повышает выживаемость, хп, защиту; "other" — вспомогательные, не влияют на бой напрямую). Пример: {{"Фаэргас": {{"Сила воли": {{"description": "Способность противостоять ментальному воздействию", "initial_value": 8, "stat_type": "defensive"}}}}}}. Значение initial_value должно быть осмысленным и соответствовать концепту персонажа: у мастера ветров «Владение стихией ветра» будет 9-10, а у подземного червя этот же стат будет 1-2.
5. Для каждого персонажа придумать уникальный класс/архетип, соответствующий его концепту и сеттингу. character_classes: ключ — имя персонажа, значение — объект с двумя полями: "name" (краткое название класса, 2-4 слова) и "description" (описание класса, 2-4 предложения, объясняющие суть этого класса в мире).
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
        print(f"[ai_generator] RAW LLM response (truncated): {raw_content[:2000]}")
        data = _extract_json(raw_content)

        if not isinstance(data, dict):
            raise ValueError(
                f"LLM вернул не объект, а {type(data).__name__}: {raw_content[:500]}"
            )

        try:
            return PhaseZeroOutput(**data)
        except Exception as e:
            import json as _json
            print(f"[ai_generator] RAW LLM response (full): {raw_content}")
            print(f"[ai_generator] Parsed data: {_json.dumps(data, ensure_ascii=False, indent=2)}")
            raise

    finally:
        if close_client:
            await client.aclose()
