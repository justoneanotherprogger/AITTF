from pydantic import BaseModel


class PlayerModel(BaseModel):
    name: str
    class_archetype: str
    hp_current: int
    hp_max: int
    stats: dict[str, int]
    inventory: list[str]
    status_effects: list[str]


class WorldEntityModel(BaseModel):
    entity_type: str  # setting, npc, faction, rule
    name: str
    data: dict


class SessionModel(BaseModel):
    game_status: str  # exploration, combat
    turn_order: list
    current_turn_index: int
    timer_ends_at: str = ""


class ChatMessageModel(BaseModel):
    sender: str
    message_text: str
    is_action: bool
    timestamp: str
