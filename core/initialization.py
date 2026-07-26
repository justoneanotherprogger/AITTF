from db.database import add_or_update_entity, init_db, upsert_player
from models.models import PlayerModel, WorldEntityModel
from llm.ai_generator import generate_initial_world, PhaseZeroOutput
from core.game_engine import calc_hp_max


def _register_entities(phase_zero: PhaseZeroOutput) -> None:
    add_or_update_entity(
        WorldEntityModel(
            entity_type="setting",
            name=phase_zero.setting_name,
            data={"description": phase_zero.setting_description},
        )
    )
    add_or_update_entity(
        WorldEntityModel(
            entity_type="rule",
            name="global_conflict",
            data={"description": phase_zero.global_conflict},
        )
    )
    stats_for_db = {
        p: {s: sd.model_dump() for s, sd in pstats.items()}
        for p, pstats in phase_zero.character_stats_templates.items()
    }
    add_or_update_entity(
        WorldEntityModel(
            entity_type="rule",
            name="stats_system",
            data={"stats": stats_for_db},
        )
    )
    add_or_update_entity(
        WorldEntityModel(
            entity_type="rule",
            name="initial_narrative",
            data={"text": phase_zero.initial_narrative_text},
        )
    )


def _register_players(
    player_inputs: list[dict], phase_zero: PhaseZeroOutput
) -> list[PlayerModel]:
    players: list[PlayerModel] = []
    for inp in player_inputs:
        cls_def = phase_zero.character_classes.get(inp["name"])
        class_name = cls_def.name if cls_def else ""
        class_desc = cls_def.description if cls_def else ""
        player_stats_map = phase_zero.character_stats_templates.get(inp["name"], {})
        def_sum = sum(sd.initial_value for sd in player_stats_map.values() if sd.stat_type == "defensive")
        hp = calc_hp_max(def_sum)
        player = PlayerModel(
            name=inp["name"],
            class_archetype=class_name,
            class_description=class_desc,
            hp_current=hp,
            hp_max=hp,
            stats={s: sd.initial_value for s, sd in player_stats_map.items()},
            inventory=[],
            status_effects=[],
        )
        upsert_player(player)
        players.append(player)
    return players


async def init_game_session(player_inputs: list[dict]) -> PhaseZeroOutput:
    init_db()
    descriptions = [inp["description"] for inp in player_inputs]
    phase_zero = await generate_initial_world(descriptions)

    _register_entities(phase_zero)
    _register_players(player_inputs, phase_zero)

    return phase_zero
