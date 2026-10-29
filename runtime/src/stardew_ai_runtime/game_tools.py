"""Small, stable game surface with operation-specific typed payloads."""
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import Image
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

GAME_TOOLS = frozenset({"get_status", "query_game", "submit_work", "manage_arrangements",
                        "remember", "request_player_decision", "consult", "read_reload_history"})

OPERATION_HELP = {
    "pickup_items": "Collect currently existing loose drops or directly grabbable ground items at observed tiles. Drops may move or be collected while walking; no-item means nothing eligible remains there, not that clearing is required. Inspect current ground and inventory facts without attributing every change to yourself. Does not open chests or clear obstacles.",
    "ship_items": "Ship the listed backpack items through the native shipping bin. Call submit_work with {\"action\":{\"ship_items\":{\"items\":[{\"itemId\":\"(O)24\",\"count\":1}]}}}. Use current inventory and the player's request to choose items and counts; do not omit the ship_items wrapper.",
    "refill_watering_can": "Refill once on the companion's current map. Omit tiles to use observed native water sources; max_tiles limits candidate sources, not refills. Explicit tiles are alternative water-source tiles, not standing positions. A full can needs no refill. If unreachable, inspect obstacles and decide whether to clear them before retrying or changing source.",
    "water_auto": "Water currently observed dry crops without enumerating every tile. max_tiles caps this batch; omit target_tiles for all observed work, or supply an observed subset for a restricted area. Does not refill the can. Plan refill steps around the observed water capacity.",
    "water_tiles": "Water an explicit observed tile list on the companion's map. Use water_auto for unrestricted farm watering; exact tiles are useful for a selected area or remaining crops after partial completion.",
    "query_route": "Preview reachability without moving. Supply location_id and tile {x,y}; there is no landmark parameter. Omit tile only for a public adjacent-map exit. A failed route does not prove the obstacle is removable: inspect the area before choosing clearing or another route.",
    "observe_farming_helpers": "Observe nearby native water-source candidates, ground items, fertilizer and choppable trees. Missing nearby sources does not mean the entire map has no water. Coordinates are evidence, not a guarantee of reachability.",
    "clear_debris": "Clear observed weeds, stones or twigs for an explicit clearing request or an identified route obstruction. Collecting existing drops does not require clearing unrelated debris. Protects crops, machines, chests and buildings. Respect player preferences and available tools/stamina; grass/dead crops use cut_grass, wild trees use chop_tree.",
    "chop_tree": "Chop explicitly observed wild trees, stumps or logs with the native Axe; fruit trees are protected. Check tool capability, stamina and player preservation preferences before clearing a blocked route. Do not remove valuable or reserved trees just to shorten travel.",
    "read_guidance": "Read one packaged topic: crop-selection, farm-region, livestock-processing, procurement-travel, wiki-lookup, daily-rhythm, player-rhythm, execution. No file or source-code access is needed.",
}


class OneOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def named_object(cls, value):
        if isinstance(value, dict):
            selected = [name for name, args in value.items() if args is not None]
            if not selected:
                raise ValueError("Choose exactly one named operation: {operation_name: {parameters}}. An empty object does not select an operation; parameter-free operations still need their name, e.g. {water_auto: {}} in action.")
            unknown = set(value) - set(cls.model_fields)
            if unknown:
                raise ValueError(f"Unknown operation key(s): {', '.join(sorted(unknown))}. Put parameters inside a named operation, not directly in this object. For shipping use action.ship_items.items; consult this tool's schema for available operations.")
        return value

    @model_validator(mode="after")
    def exactly_one(self):
        if sum(value is not None for value in self.__dict__.values()) != 1:
            raise ValueError("Choose exactly one named operation and its own parameter object")
        return self

    def selected(self):
        name, value = next((k, v) for k, v in self.__dict__.items() if v is not None)
        return name, value.model_dump(exclude_unset=True)


def register_game_tools(mcp, implementations, declarations, operations, *, execute_action):
    def model(label, names, *, plan=False):
        fields = {}
        for name in sorted(names):
            if name not in declarations:
                continue
            source = declarations[name].fn_metadata.arg_model
            allowed = operations[name][1] if plan else source.model_fields
            arguments = create_model(label + name, __config__=ConfigDict(extra="forbid"), **{
                key: (field.annotation, deepcopy(field)) for key, field in source.model_fields.items()
                if key in allowed})
            description = OPERATION_HELP.get(name) or " ".join(declarations[name].description.split("\n\n", 1)[0].split())
            if name in {"save_memory", "save_method", "recall_memory", "forget_memory", "inspect_capabilities", "develop_capability", "manage_capability", "prepare_capability_builder"}:
                description = " ".join(declarations[name].description.split())
            description = description.removeprefix("Stardew Valley (SMAPI), current saved game. ")
            fields[name] = (arguments | None, Field(default=None, description=description))
        return create_model(label, __base__=OneOperation, **fields)

    queries = {name for name in declarations if name.startswith(("query_", "observe_"))
               and name != "query_wiki"} | {"work_plan_overview", "get_work_overview", "autonomy_status", "learned_query"}
    Query = model("GameQuery", queries)
    Action = model("GameAction", operations, plan=True)
    Arrangement = model("Arrangement", {"manage_milestones", "manage_companion", "remember_intent", "manage_goal", "manage_todo", "set_autonomy", "develop_capability", "manage_capability", "prepare_capability_builder"})
    Memory = model("Memory", {"save_memory", "save_method", "forget_memory", "record_decision_note", "answer_player_decision", "say_to_player"})
    Reference = model("Reference", {"recall_memory", "query_wiki", "read_guidance", "inspect_capabilities"})

    async def invoke(value):
        name, args = value.selected()
        return await implementations[name](**args)

    async def query_game(query):
        """Read one named query from the game."""
        result = await invoke(query)
        if isinstance(result, Image):
            return CallToolResult(content=[result.to_image_content()], structuredContent={"kind": "map_image"})
        return result
    query_game.__annotations__ = {"query": Query, "return": dict[str, Any]}

    async def submit_work(action):
        """Execute one named action now and return its native result; no queue is saved.

        Required shape: {"action":{"operation_name":{"parameter":value}}}.
        Examples: {"action":{"water_auto":{}}} or
        {"action":{"ship_items":{"items":[{"itemId":"(O)24","count":1}]}}}.
        action={} selects nothing. Do not put items/tiles directly inside action.
        Waits for the native terminal result; do not poll or sleep to finish this action.
        Harvesting and clearing also return ground items and backpack observations.
        Choose pickup, storage or the next action from the returned facts.
        A partial batch does not complete the player's whole goal.
        World and inventory observations may also reflect player actions; do not
        infer personal output or remaining work solely from inventory differences.
        """
        name, args = action.selected()
        return await execute_action(name, args)
    submit_work.__annotations__ = {"action": Action, "return": dict[str, Any]}

    async def manage_arrangements(change):
        """Save or change an arrangement, preference or work control using one named operation."""
        return await invoke(change)
    manage_arrangements.__annotations__ = {"change": Arrangement, "return": dict[str, Any]}

    async def remember(entry):
        """Speak with say_to_player, save/correct a natural-language memory or learned method, or answer a decision.

        Example: {"entry":{"save_memory":{"text":"雨天想一起去海边，今天先休息。","scope":"共同约定"}}}.
        save_method keeps an adaptable approach; save_memory keeps preferences and experiences.
        Notes do not execute actions or prove current world state. Changes stay in this companion's memory.
        """
        return await invoke(entry)
    remember.__annotations__ = {"entry": Memory, "return": dict[str, Any]}

    async def consult(request):
        """Recall companion memories or learned methods, read guidance, or look up wiki knowledge.

        Example: {"request":{"recall_memory":{"query":"雨天 海边"}}}.
        Use kind='method' inside recall_memory to find learned approaches; note_id reads a full note.
        Memories preserve prior context; live game observations describe the current world.
        """
        return await invoke(request)
    consult.__annotations__ = {"request": Reference, "return": dict[str, Any]}

    for fn in (query_game, submit_work, manage_arrangements, remember, consult):
        mcp.add_tool(fn)
