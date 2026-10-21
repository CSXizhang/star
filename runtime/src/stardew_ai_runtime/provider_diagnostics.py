"""Bounded run-local Codex request evidence, without credentials or stderr."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_SECRET_FIELD = re.compile(r'("(?:token|decisionToken|decisionId|api_?key|password|secret|authorization)"\s*:\s*)"(?:\\.|[^"\\])*"', re.I)


def redact_text(text: str) -> str:
    text = _SECRET_FIELD.sub(r'\1"<redacted>"', text)
    return re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~-]+', r'\1<redacted>', text)


def safe_error(error: Any) -> dict[str, Any]:
    """Retain only provider error fields that can explain this exact failure."""
    if not isinstance(error, dict):
        return {"message": redact_text(str(error or 'Codex turn failed'))[:2000]}
    result: dict[str, Any] = {}
    for key in ('message', 'code', 'type', 'param', 'request_id', 'requestId',
                'status_code', 'http_status', 'codex_error_info'):
        value = error.get(key)
        if isinstance(value, str):
            result[key] = redact_text(value)[:2000]
        elif key in {'status_code', 'http_status'} and type(value) is int:
            result[key] = value
        elif key == 'codex_error_info' and isinstance(value, dict):
            nested = {name: redact_text(child)[:500] for name, child in value.items()
                      if name in {'type', 'code', 'message', 'request_id', 'status_code'}
                      and isinstance(child, str)}
            if nested:
                result[key] = nested
    result['availableFieldNames'] = sorted(str(key) for key in error)
    return result


def _paths(run_dir: Path, request_id: str) -> tuple[Path, Path]:
    folder = Path(run_dir) / 'logs/provider-requests'
    stem = 'request-' + hashlib.sha256(request_id.encode()).hexdigest()[:16]
    return folder / (stem + '.json'), folder / (stem + '.txt')


def record_prepared(run_dir: Path, request_id: str, session_id: str | None,
                    prompt: str, instructions: str, *, model: str, effort: str | None,
                    http_provider: str | None) -> None:
    """Record the exact input text hash before stdin newline translation."""
    metadata_path, prompt_path = _paths(run_dir, request_id)
    try:
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        raw = prompt.encode('utf-8')
        safe = redact_text(prompt)
        complete = len(safe.encode('utf-8')) <= 262144
        if not complete:
            safe = safe[:60000] + '\n<local diagnostic copy truncated>\n' + safe[-60000:]
        metadata = {'timestampUnix': time.time(), 'source': 'codex-cli-input-prepared',
                    'applicationRequestId': request_id, 'conversationId': session_id,
                    'model': model, 'effort': effort, 'httpProviderOverride': http_provider,
                    'promptTextSha256': hashlib.sha256(raw).hexdigest(),
                    'promptUtf8BytesBeforeStdinTranslation': len(raw),
                    'instructionsSha256': hashlib.sha256(instructions.encode()).hexdigest(),
                    'instructionsUtf8Bytes': len(instructions.encode()),
                    'promptCopyRedacted': True, 'promptCopyComplete': complete,
                    'promptFile': prompt_path.name, 'requestIdIsServerHttpId': False}
        prompt_path.write_text(safe, encoding='utf-8')
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        # A small number of owned snapshots is enough to compare neighboring
        # turns. Never remove foreign files or walk/delete recursively.
        previous = sorted(metadata_path.parent.glob('request-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in previous[8:]:
            if not re.fullmatch(r'request-[a-f0-9]{16}\.json', old.name):
                continue
            old.with_suffix('.txt').unlink(missing_ok=True)
            old.unlink(missing_ok=True)
    except OSError:
        logger.warning('Could not persist run-local Codex request evidence')


def record_completed(run_dir: Path, request_id: str, result: dict[str, Any]) -> None:
    metadata_path, prompt_path = _paths(run_dir, request_id)
    try:
        if not metadata_path.is_file():
            return  # Preparation failed before any input was captured.
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        if metadata.get('applicationRequestId') != request_id:
            return
        metadata.update(completedUnix=time.time(), success=bool(result.get('success')),
                        conversationId=result.get('conversation_id'), errorCode=result.get('error'),
                        errorDetail=redact_text(str(result.get('error_detail') or ''))[:2000],
                        recordedUsageRequestCount=result.get('usage_request_count'),
                        latestMeasuredInputContext=result.get('usage_latest_request_input_context'))
        metadata['contextMeasurementScope'] = 'last recorded provider request; not a measurement of an unrecorded failed request'
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        label = 'last-successful' if result.get('success') else 'last-failed'
        shutil.copy2(metadata_path, metadata_path.parent / (label + '.json'))
        shutil.copy2(prompt_path, metadata_path.parent / (label + '.txt'))
    except (OSError, ValueError):
        logger.warning('Could not complete run-local Codex request evidence')
