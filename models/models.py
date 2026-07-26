from enum import Enum

from pydantic import BaseModel


class StatType(str, Enum):
    offensive = "offensive"
    defensive = "defensive"
    other = "other"


class StatDef(BaseModel):
    description: str
    initial_value: int
    stat_type: StatType = StatType.other


class ClassDef(BaseModel):
    name: str
    description: str


class PlayerModel(BaseModel):
    name: str
    class_archetype: str
    class_description: str = ""
    hp_current: int
    hp_max: int
    stats: dict[str, int]
    inventory: list[str]
    status_effects: list[str]
    backstory: str = ""


class WorldEntityModel(BaseModel):
    entity_type: str  # setting, npc, faction, rule
    name: str
    data: dict


class SessionModel(BaseModel):
    game_status: str  # lobby, backstory_gathering, exploration, combat
    turn_order: list
    current_turn_index: int
    timer_ends_at: str = ""


class ChatMessageModel(BaseModel):
    sender: str
    message_text: str
    is_action: bool
    timestamp: str
