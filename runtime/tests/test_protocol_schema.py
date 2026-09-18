import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPOSITORY_ROOT / "protocol" / "schemas" / "protocol-v0.1.schema.json"
EXAMPLES_PATH = REPOSITORY_ROOT / "protocol" / "examples"


def test_protocol_schema_is_valid() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)


def test_protocol_examples_match_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    for example_path in sorted(EXAMPLES_PATH.glob("*.json")):
        instance = json.loads(example_path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        assert not errors, f"{example_path.name}: {[error.message for error in errors]}"
