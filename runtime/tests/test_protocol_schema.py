import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPOSITORY_ROOT / "protocol" / "schemas" / "protocol-v0.1.schema.json"
EXAMPLES_PATH = REPOSITORY_ROOT / "protocol" / "examples"


def test_protocol_schema_is_valid() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)


def test_native_work_delivery_receipt_matches_schema():
    from stardew_ai_runtime.protocol import Envelope
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    validator = Draft202012Validator(schema)
    envelope = Envelope.create_chat_reply('bridge', 'delivery', 'completed', '实际取消了。', save_id='farm').to_mapping()
    envelope['payload']['deliveryOutboxIds'] = ['obx-1']
    assert not list(validator.iter_errors(envelope))
    receipt = {**envelope, 'messageType': 'chat.delivery.ack',
               'payload': {'requestId': 'delivery', 'saveId': 'farm', 'outboxIds': ['obx-1']}}
    assert not list(validator.iter_errors(receipt))
    receipt['payload']['outboxIds'] = []
    assert list(validator.iter_errors(receipt))


def test_protocol_examples_match_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    for example_path in sorted(EXAMPLES_PATH.glob("*.json")):
        instance = json.loads(example_path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        assert not errors, f"{example_path.name}: {[error.message for error in errors]}"


def test_production_snapshot_schema_covers_serialized_csharp_record_fields():
    """DTO field drift must fail here before a real Mod snapshot is rejected.

    The native-* examples were serialized with TransportDtos.cs and the real
    transport JsonSerializerOptions, using artificial contract-test inputs.
    They are not live-game captures.
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    wire = defs["worldSnapshot"]["allOf"][1]["properties"]["payload"]["properties"]
    records = {
        "AnimalSnapshot": defs["animalSnapshot"],
        "AnimalBuildingSnapshot": defs["animalBuildingSnapshot"],
        "FarmWorkSnapshot": defs["farmWorkSnapshot"],
        "GroundItemSnapshot": defs["groundItemSnapshot"],
        "ChoppableTreeSnapshot": defs["choppableTreeSnapshot"],
        "ShopSnapshot": defs["shopSnapshot"],
        "ShopItemSnapshot": defs["shopItemSnapshot"],
        "CompanionSnapshot": wire["companion"],
        "WorldStateSnapshot": wire["world"],
        "LivestockSnapshot": wire["livestock"],
        "FarmingSnapshot": wire["farming"],
        "WorldSnapshotPayload": {"properties": wire},
        "AutonomyStatePayload": defs["autonomyState"]["allOf"][1]["properties"]["payload"],
        "SkillResultPayload": defs["skillResult"]["allOf"][1]["properties"]["payload"],
        "ModWelcomePayload": defs["modWelcome"]["allOf"][1]["properties"]["payload"],
        "ChatReplyPayload": defs["chatReply"]["allOf"][1]["properties"]["payload"],
        "InventorySlotSnapshot": wire["inventory"]["properties"]["slots"]["items"],
        "CompanionInventorySnapshot": wire["inventory"],
        "ChestSlotSnapshot": wire["chests"]["properties"]["items"]["items"]["properties"]["contents"]["items"],
        "ChestSnapshot": wire["chests"]["properties"]["items"]["items"],
        "ChestsSnapshot": wire["chests"],
        "MatureCropTile": defs["farmWorkSnapshot"]["properties"]["matureCrops"]["items"],
        "SeedItemSnapshot": wire["planting"]["properties"]["seeds"]["items"],
        "CandidateTilesSnapshot": wire["planting"]["properties"]["candidateTiles"],
        "SearchBoundsSnapshot": wire["planting"]["properties"]["searchBounds"],
        "PlantingSnapshot": wire["planting"],
        "MachineSnapshot": wire["machines"]["properties"]["items"]["items"],
        "MachinesSnapshot": wire["machines"],
        "ProductionSignal": wire["productionSignals"]["items"],
        "WaitingConditionPayload": defs["autonomyState"]["allOf"][1]["properties"]["payload"]["properties"]["waitingConditions"]["items"],
    }
    source = (REPOSITORY_ROOT / "src/StardewAI.Companion.Mod/Transport/TransportDtos.cs").read_text(encoding="utf-8")
    for record, definition in records.items():
        body = re.search(r"public sealed record " + record + r"\([\s\S]*?\n\)", source)
        assert body is not None, record
        fields = set(re.findall(r'JsonPropertyName\("([^\"]+)"\)', body.group()))
        assert fields <= set(definition["properties"]), (record, fields - set(definition["properties"]))


@pytest.mark.parametrize("path,value", [
    (("livestock", "roamingAnimals", 0, "animalId"), 123),
    (("farmWork", "observationStatus"), "maybe"),
    (("farmWork", "deadCropCount"), "2"),
    (("productionSignals", 0, "isReady"), "yes"),
    (("productionSignals", 0, "unsupported"), True),
    (("companion", "unsupported"), True),
])
def test_native_snapshot_contract_remains_strict(path, value):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    instance = json.loads((EXAMPLES_PATH / "world-snapshot-native-production.json").read_text(encoding="utf-8"))
    target = instance["payload"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert list(Draft202012Validator(schema).iter_errors(instance))


@pytest.mark.parametrize("status", ["unknown", "in-flight"])
def test_result_reconciliation_schema_has_execution_deferred_session_bound_shapes(status):
    from stardew_ai_runtime.protocol import Envelope

    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    query = Envelope.create_result_query("runtime-restarted", 1, "save", "session", 1, "old-command").to_mapping()
    assert not list(validator.iter_errors(query))
    response = deepcopy(query)
    response.update(messageType="skill.result.status", correlationId=query["messageId"])
    response["payload"]["status"] = status
    assert not list(validator.iter_errors(response))
    for field in ["saveId", "gameSessionId"]:
        invalid = deepcopy(query)
        invalid.pop(field)
        assert list(validator.iter_errors(invalid))
    response["payload"]["terminalState"] = "succeeded"
    assert list(validator.iter_errors(response))


def test_chat_resume_request_is_optional_boolean():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    instance = json.loads((EXAMPLES_PATH / "chat-reply-resume.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(instance))
    ordinary = deepcopy(instance)
    ordinary["payload"].pop("resumeWorkRequested")
    assert not list(validator.iter_errors(ordinary))
    instance["payload"]["resumeWorkRequested"] = "true"
    assert list(validator.iter_errors(instance))
