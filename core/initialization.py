from db.database import add_or_update_entity, init_db, upsert_player
from models.models import PlayerModel, WorldEntityModel
from llm.ai_generator import generate_initial_world, PhaseZeroOutput


_DEFAULT_HP = 10
_DEFAULT_STAT_VALUE = 1


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
    add_or_update_entity(
        WorldEntityModel(
            entity_type="rule",
            name="stats_system",
            data={"stats": phase_zero.character_stats_templates},
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
        player_stats_map = phase_zero.character_stats_templates.get(inp["name"], {})
        player = PlayerModel(
            name=inp["name"],
            class_archetype="",
            hp_current=_DEFAULT_HP,
            hp_max=_DEFAULT_HP,
            stats={s: _DEFAULT_STAT_VALUE for s in player_stats_map},
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
