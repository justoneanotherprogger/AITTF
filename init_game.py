import asyncio
from core.initialization import init_game_session


async def main() -> None:
    print("=== AI Tabletop Framework — Фаза 0: Рождение вселенной ===")
    print()

    player_count = 0
    while player_count < 1 or player_count > 5:
        try:
            player_count = int(input("Сколько будет игроков? (1–5): "))
        except ValueError:
            pass

    print()
    player_inputs: list[dict] = []
    for i in range(player_count):
        print(f"--- Персонаж {i + 1} ---")
        name = input("  Имя: ").strip()
        description = input("  Описание (свободный текст, 1-2 предложения): ").strip()
        player_inputs.append({"name": name, "description": description})
        print()

    print("Генерация мира через ИИ...")
    print()

    result = await init_game_session(player_inputs)

    print("=" * 60)
    print("  МИР СОЗДАН!")
    print("=" * 60)
    print()
    print(f"  Название:      {result.setting_name}")
    print(f"  Сеттинг:       {result.setting_description}")
    print(f"  Конфликт:      {result.global_conflict}")
    print()
    print("  Характеристики персонажей:")
    for player_name, player_stats in result.character_stats_templates.items():
        print(f"    {player_name}:")
        for stat_name, stat_desc in player_stats.items():
            print(f"      • {stat_name} — {stat_desc}")
    print()
    print("  Вступительный текст:")
    print(f"    {result.initial_narrative_text}")
    print()
    print("=" * 60)
    print("  Запустите веб-сервер:")
    print("    poetry run uvicorn web.main:app --host 0.0.0.0 --port 8000")
    print("  или")
    print("    uvicorn web.main:app --reload")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
