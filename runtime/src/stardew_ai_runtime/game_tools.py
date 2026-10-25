"""Small, stable game surface with operation-specific typed payloads."""
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

GAME_TOOLS = frozenset({"get_status", "query_game", "submit_work", "manage_arrangements",
                        "remember", "request_player_decision", "consult", "read_reload_history"})

OPERATION_HELP = {
    "refill_watering_can": "Refill once on the companion's current map. Omit tiles to use observed native water sources; max_tiles limits candidate sources, not refills. Explicit tiles are alternative water-source tiles, not standing positions. A full can needs no refill. If unreachable, inspect obstacles and decide whether to clear them before retrying or changing source.",
    "water_auto": "Water currently observed dry crops without enumerating every tile. max_tiles caps this batch; omit target_tiles for all observed work, or supply an observed subset for a restricted area. Does not refill the can. Plan refill steps around the observed water capacity.",
    "water_tiles": "Water an explicit observed tile list on the companion's map. Use water_auto for unrestricted farm watering; exact tiles are useful for a selected area or remaining crops after partial completion.",
    "query_route": "Preview reachability without moving. Supply location_id and tile {x,y}; there is no landmark parameter. Omit tile only for a public adjacent-map exit. A failed route does not prove the obstacle is removable: inspect the area before choosing clearing or another route.",
    "observe_farming_helpers": "Observe nearby native water-source candidates, ground items, fertilizer and choppable trees. Missing nearby sources does not mean the entire map has no water. Coordinates are evidence, not a guarantee of reachability.",
    "clear_debris": "Clear observed weeds, stones or twigs at explicit tiles with native tools. Protects crops, machines, chests and buildings. For a blocked route, clear only identified obstacles consistent with the player's preferences and available tools/stamina; grass/dead crops use cut_grass, wild trees use chop_tree.",
    "chop_tree": "Chop explicitly observed wild trees, stumps or logs with the native Axe; fruit trees are protected. Check tool capability, stamina and player preservation preferences before clearing a blocked route. Do not remove valuable or reserved trees just to shorten travel.",
    "read_guidance": "Read one packaged topic: crop-selection, farm-region, livestock-processing, procurement-travel, wiki-lookup, daily-rhythm, player-rhythm, execution. No file or source-code access is needed.",
}


class OneOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def exactly_one(self):
        if sum(value is not None for value in self.__dict__.values()) != 1:
            raise ValueError("Choose exactly one named operation and its own parameter object")
        return self

    def selected(self):
        name, value = next((k, v) for k, v in self.__dict__.items() if v is not None)
        return name, value.model_dump(exclude_unset=True)


def register_game_tools(mcp, implementations, declarations, operations, *, current_save,
                        store_for_run, read_only):
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
            description = description.removeprefix("Stardew Valley (SMAPI), current saved game. ")
            fields[name] = (arguments | None, Field(default=None, description=description))
        return create_model(label, __base__=OneOperation, **fields)

    queries = {name for name in declarations if name.startswith(("query_", "observe_"))
               and name != "query_wiki"} | {"work_plan_overview", "get_work_overview", "autonomy_status"}
    Query = model("GameQuery", queries)
    Action = model("GameAction", operations, plan=True)
    Arrangement = model("Arrangement", {"manage_milestones", "manage_companion", "remember_intent", "manage_goal", "manage_todo", "set_autonomy"})
    Memory = model("Memory", {"record_decision_note", "answer_player_decision"})
    Reference = model("Reference", {"query_wiki", "read_guidance"})

    async def invoke(value):
        name, args = value.selected()
        return await implementations[name](**args)

    async def query_game(query):
        """Read one named game query. While work owns execution, get_status supplies cached progress."""
        if read_only and query.selected()[0] not in {"get_work_overview", "work_plan_overview", "autonomy_status"}:
            return {"freshQueryAvailable": False, "status": await implementations["get_status"](),
                    "requestedQuery": query.selected()[0],
                    "note": "Cached status only; missing query-specific facts remain unknown until current work settles."}
        result = await invoke(query)
        if isinstance(result, Image):
            return CallToolResult(content=[result.to_image_content()], structuredContent={"kind": "map_image"})
        return result
    query_game.__annotations__ = {"query": Query, "return": dict[str, Any]}

    async def submit_work(steps, title="当前工作", instruction=None):
        """Queue ordered actions for the current goal. Each step has ONE named action with its typed arguments.

        Execution starts after this model turn returns. Accepted does not mean completed.
        The program binds the current goal and original player instruction, including follow-up work.
        Only when choosing a different offered instruction, pass its exact text as instruction. Never copy internal IDs.
        """
        if read_only:
            raise ToolError("WORK_DISPATCH_UNAVAILABLE: retain the instruction; use get_status for cached progress")
        if not steps:
            raise ToolError("At least one action is required")
        sid = await current_save()
        state = store_for_run().state(sid)
        decision = state.decision
        request_id = decision.get("requestId")
        if instruction is not None:
            offered = {request_id, *decision.get("requestIds", [])}
            matches = [r for r in state.requests if r.get("id") in offered and r.get("text") == instruction]
            if len(matches) != 1:
                raise ToolError("Choose one offered player instruction by its exact text; no work selected.")
            request_id = matches[0]["id"]
        selected = [{"operation": name, "params": args} for name, args in (step.selected() for step in steps)]
        return await implementations["submit_plan"](tasks=[{"title": title, "steps": selected}],
            goal_id=decision.get("goalScope") or decision.get("goalId"),
            goal_text=title, request_id=request_id)
    submit_work.__annotations__ = {"steps": list[Action], "title": str, "instruction": str | None, "return": dict[str, Any]}

    async def manage_arrangements(change):
        """Save or change an arrangement, preference or work control using one named operation."""
        return await invoke(change)
    manage_arrangements.__annotations__ = {"change": Arrangement, "return": dict[str, Any]}

    async def remember(entry):
        """Record a dated observation, hypothesis, intent or failure; a note is not an execution receipt."""
        return await invoke(entry)
    remember.__annotations__ = {"entry": Memory, "return": dict[str, Any]}

    async def consult(request):
        """Read a guidance topic or look up wiki knowledge only when current facts do not answer the question."""
        return await invoke(request)
    consult.__annotations__ = {"request": Reference, "return": dict[str, Any]}

    for fn in (query_game, submit_work, manage_arrangements, remember, consult):
        mcp.add_tool(fn)
