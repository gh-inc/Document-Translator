"""Stable semantic cache identity, independent of job and document IDs."""

import hashlib
import json

from app.core.models import JobRecord


def translation_key(job: JobRecord) -> str:
    glossary_hash = hashlib.sha256(
        json.dumps(job.glossary, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    payload = [job.target_language, job.model, job.prompt_version, glossary_hash]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
