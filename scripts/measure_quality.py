"""Run an explicit live end-to-end translation and report quality/cost metrics.

Example: ``uv run python -m scripts.measure_quality sample.docx --env-file .env``.
The application and worker run in-process against a private temporary database
and storage roots; the REST upload/job/download routes and real OpenAI adapters
are used. JSON is written to stdout and the readable summary to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.adapters.formats.docx import DocxExtractor, DocxRenderer
from app.adapters.formats.pdf import PdfExtractor, PdfRenderer
from app.adapters.formats.registry import FormatRegistry
from app.adapters.persistence.database import SqliteConnectionFactory
from app.adapters.persistence.measurements import MeasurementPersistence
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.api.main import create_app
from app.config import Settings
from app.core.models import Block, DocumentAnalysisRecord
from app.worker.__main__ import run_worker


class MeasurementError(RuntimeError):
    """A safe, user-facing error raised by this explicit measurement command."""


_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}|%s|\[[^\[\]]*\]")
_DATE_RE = re.compile(
    r"(?<!\w)(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\w)"
)
_CURRENCY_CODES = "USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|CNY|INR|UAH|PLN|SEK|NOK|DKK"
_CURRENCY_RE = re.compile(
    rf"(?<!\w)(?:(?:[$€£¥]\s*|(?:{_CURRENCY_CODES})\s*)[-+]?\d[\d.,]*"
    rf"|[-+]?\d[\d.,]*\s*(?:[$€£¥]|(?:{_CURRENCY_CODES})))(?!\w)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?:\s?%)(?!\w)|(?<![\w])[-+]?\d+(?:[.,]\d+)*(?!\w)"
)
_TOKEN_RE = re.compile(
    rf"(?P<placeholder>{_PLACEHOLDER_RE.pattern})"
    rf"|(?P<date>{_DATE_RE.pattern})"
    rf"|(?P<currency>{_CURRENCY_RE.pattern})"
    rf"|(?P<number>{_NUMBER_RE.pattern})",
    re.IGNORECASE,
)


def chrf_score(candidate: str, reference: str, *, beta: int = 2, max_order: int = 6) -> float:
    """Return corpus-style chrF on a 0..100 scale, excluding whitespace.

    This implements character n-gram orders 1 through 6 with the standard
    chrF beta=2 weighting. It is intentionally a compact single-reference
    implementation for the measurement command, not a replacement for a
    general-purpose metric library.
    """
    if beta <= 0 or max_order <= 0:
        raise ValueError("beta and max_order must be positive")
    predicted = "".join(candidate.split())
    expected = "".join(reference.split())
    order_precisions: list[float] = []
    order_recalls: list[float] = []
    for order in range(1, max_order + 1):
        predicted_ngrams = Counter(
            predicted[index : index + order] for index in range(max(0, len(predicted) - order + 1))
        )
        expected_ngrams = Counter(
            expected[index : index + order] for index in range(max(0, len(expected) - order + 1))
        )
        predicted_total = sum(predicted_ngrams.values())
        expected_total = sum(expected_ngrams.values())
        if predicted_total and expected_total:
            matched = sum((predicted_ngrams & expected_ngrams).values())
            order_precisions.append(matched / predicted_total)
            order_recalls.append(matched / expected_total)
    if not order_precisions:
        return 0.0
    precision = sum(order_precisions) / len(order_precisions)
    recall = sum(order_recalls) / len(order_recalls)
    beta_squared = beta**2
    denominator = beta_squared * precision + recall
    if denominator == 0:
        return 0.0
    return 100.0 * (1 + beta_squared) * precision * recall / denominator


def preservation_metrics(source: str, translated: str) -> dict[str, object]:
    """Compare source token multisets so repeated literals count separately."""
    source_tokens = _preservation_tokens(source)
    translated_tokens = _preservation_tokens(translated)
    result: dict[str, object] = {}
    total_source = 0
    total_preserved = 0
    for category in ("placeholder", "date", "currency", "number"):
        expected = Counter(token for kind, token in source_tokens if kind == category)
        actual = Counter(token for kind, token in translated_tokens if kind == category)
        count = sum(expected.values())
        preserved = sum(min(amount, actual[token]) for token, amount in expected.items())
        total_source += count
        total_preserved += preserved
        result[category] = {
            "source_count": count,
            "preserved_count": preserved,
            "preservation_percent": 100.0 * preserved / count if count else None,
        }
    result["source_token_count"] = total_source
    result["preserved_token_count"] = total_preserved
    result["preservation_percent"] = (
        100.0 * total_preserved / total_source if total_source else None
    )
    return result


def _preservation_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(text):
        category = next(name for name, value in match.groupdict().items() if value is not None)
        tokens.append((category, match.group(0)))
    return tokens


def require_live_provider(settings: Settings) -> None:
    """Reject fake or uncredentialed runs before touching application state."""
    if settings.llm_provider != "openai":
        raise MeasurementError(
            "Quality/cost measurement requires LLM_PROVIDER=openai; "
            "fake-provider runs are rejected."
        )
    if not settings.openai_api_key.get_secret_value().strip():
        raise MeasurementError(
            "Quality/cost measurement requires OPENAI_API_KEY; no provider call was made."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path, help="PDF or DOCX sample document")
    parser.add_argument(
        "--target-language", default="de", help="target language code (default: de)"
    )
    parser.add_argument(
        "--reference", type=Path, help="optional translated reference (PDF, DOCX, or UTF-8 text)"
    )
    parser.add_argument(
        "--env-file", type=Path, help="dotenv file passed to app Settings (for example .env)"
    )
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="maximum seconds per job (default: 900)"
    )
    return parser


async def measure(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    require_live_provider(settings)
    source_path = args.document.expanduser().resolve()
    if not source_path.is_file():
        raise MeasurementError("The input document does not exist or is not a regular file.")
    if source_path.suffix.lower() not in {".pdf", ".docx"}:
        raise MeasurementError("The input document must be a PDF or DOCX file.")
    if args.reference is not None and not args.reference.expanduser().is_file():
        raise MeasurementError("The reference path does not exist or is not a regular file.")
    target_language = str(args.target_language).strip()
    if not target_language:
        raise MeasurementError("Target language must not be empty.")
    timeout = float(args.timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise MeasurementError("Timeout must be a finite positive number.")

    original_bytes = await asyncio.to_thread(source_path.read_bytes)
    sample_sha256 = hashlib.sha256(original_bytes).hexdigest()
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="document-translator-measure-") as temporary:
        root = Path(temporary)
        run_settings = settings.model_copy(
            update={
                "database_path": root / "app.db",
                "upload_storage_path": root / "uploads",
                "output_storage_path": root / "out",
                "worker_id": f"measurement-{uuid4()}",
            }
        )
        application = create_app(run_settings, frontend_dir=root / "frontend")
        stop_worker = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        jobs: list[dict[str, object]] = []
        try:
            async with application.router.lifespan_context(application):
                transport = httpx.ASGITransport(app=application)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://measurement"
                ) as client:
                    worker_task = asyncio.create_task(
                        _run_worker_with_logs_on_stderr(run_settings, stop_worker),
                        name="measurement-worker",
                    )
                    first = await _translate_one(
                        client,
                        source_path.name,
                        original_bytes,
                        target_language,
                        run_settings,
                        timeout,
                    )
                    forward_measurement = first["measurement"]
                    assert isinstance(forward_measurement, dict)
                    forward_measurement["phase"] = "source_to_target"
                    jobs.append(forward_measurement)
                    source_blocks, analysis = await _document_snapshot(
                        run_settings, str(first["document_id"])
                    )
                    source_text = _join_blocks(source_blocks)
                    translated_path = root / f"forward-rendered{source_path.suffix.lower()}"
                    await asyncio.to_thread(translated_path.write_bytes, first["artifact"])
                    translated_text = await _extract_rendered_text(
                        translated_path, str(first["document_id"])
                    )
                    quality_mode = "reference" if args.reference is not None else "back_translation"
                    if args.reference is not None:
                        reference_text = await _read_reference(
                            args.reference.expanduser().resolve()
                        )
                        quality_candidate = translated_text
                        quality_reference = reference_text
                    else:
                        source_language = analysis.source_language.strip()
                        if not source_language or source_language.casefold() == "und":
                            raise MeasurementError(
                                "Triage did not identify a source language; "
                                "cannot run back-translation."
                            )
                        backward = await _translate_one(
                            client,
                            translated_path.name,
                            first["artifact"],
                            source_language,
                            run_settings,
                            timeout,
                        )
                        backward_measurement = backward["measurement"]
                        assert isinstance(backward_measurement, dict)
                        backward_measurement["phase"] = "back_translation"
                        jobs.append(backward_measurement)
                        backward_path = root / f"back-rendered{source_path.suffix.lower()}"
                        await asyncio.to_thread(backward_path.write_bytes, backward["artifact"])
                        quality_candidate = await _extract_rendered_text(
                            backward_path, str(backward["document_id"])
                        )
                        quality_reference = source_text

                    quality = {
                        "mode": quality_mode,
                        "metric": "chrF",
                        "score": chrf_score(quality_candidate, quality_reference),
                        "scale": "0-100",
                        "beta": 2,
                        "character_ngram_orders": "1-6",
                        "case_sensitive": True,
                        "effective_order": True,
                        "order_averaging": "mean precision and recall by effective n-gram order",
                        "whitespace": "excluded",
                        "segmentation": "document text stream, blocks joined with newlines",
                    }
                    preservation = preservation_metrics(source_text, translated_text)
                    finished_at = datetime.now(UTC)
                    all_cost = sum(float(job["cost_usd"]) for job in jobs)
                    attempt_cost = sum(float(job["attempt_cost_usd"]) for job in jobs)
                    retry_cost = sum(float(job["retry_cost_usd"]) for job in jobs)
                    latency_observations = [int(job["job_latency_ms"]) for job in jobs]
                    chunk_latencies = [
                        int(latency)
                        for job in jobs
                        for latency in job["chunk_attempt_latencies_ms"]  # type: ignore[union-attr]
                    ]
                    return {
                        "measurement": {
                            "date_utc": started_at.date().isoformat(),
                            "started_at": started_at.isoformat(),
                            "finished_at": finished_at.isoformat(),
                            "elapsed_seconds": (finished_at - started_at).total_seconds(),
                            "sample_filename": source_path.name,
                            "sample_sha256": sample_sha256,
                            "source_block_count": len(source_blocks),
                            "detected_source_language": analysis.source_language,
                            "target_language": target_language,
                            "provider": "openai",
                            "model": settings.openai_model,
                        },
                        "quality": quality,
                        "number_placeholder_preservation": preservation,
                        "usage": {
                            "job_count": len(jobs),
                            "cost_usd": all_cost,
                            "attempt_cost_usd": attempt_cost,
                            "retry_cost_usd": retry_cost,
                            "retry_cost_share_percent": 100.0 * retry_cost / attempt_cost
                            if attempt_cost
                            else 0.0,
                            "tokens_in": sum(int(job["tokens_in"]) for job in jobs),
                            "tokens_out": sum(int(job["tokens_out"]) for job in jobs),
                            "attempt_count": sum(int(job["attempt_count"]) for job in jobs),
                            "retry_attempt_count": sum(
                                int(job["retry_attempt_count"]) for job in jobs
                            ),
                            "p95_chunk_attempt_latency_ms": _percentile_nearest_rank(
                                chunk_latencies, 95
                            ),
                            "p95_job_latency_ms": (
                                _percentile_nearest_rank(latency_observations, 95)
                                if len(latency_observations) >= 20
                                else None
                            ),
                            "job_latency_note": (
                                "p95 omitted: fewer than 20 comparable job observations; "
                                "see per-job latency."
                            ),
                        },
                        "jobs": jobs,
                        "measurement_exclusions": [
                            "Triage/provider analysis usage is not persisted in jobs or "
                            "chunk_attempts and is excluded.",
                            "Chunk latency p95 uses persisted provider-attempt latency_ms rows, "
                            "including retries.",
                            "Job latency is persisted updated_at minus created_at; one run "
                            "does not establish a meaningful p95.",
                            "Quality and preservation metrics cover extracted text; DOCX tables "
                            "and other unsupported text regions are excluded.",
                        ],
                    }
        finally:
            if worker_task is not None:
                stop_worker.set()
                try:
                    await asyncio.wait_for(asyncio.shield(worker_task), timeout=4)
                except TimeoutError:
                    worker_task.cancel()
                    await asyncio.gather(worker_task, return_exceptions=True)


async def _run_worker_with_logs_on_stderr(settings: Settings, stop: asyncio.Event) -> None:
    # The worker's normal structured logger targets stdout; reserve stdout for
    # the JSON document emitted by this command.
    with redirect_stdout(sys.stderr):
        await run_worker(settings, shutdown_event=stop)


async def _translate_one(
    client: httpx.AsyncClient,
    filename: str,
    content: bytes,
    target_language: str,
    settings: Settings,
    max_wait_seconds: float,
) -> dict[str, object]:
    try:
        async with asyncio.timeout(max_wait_seconds):
            return await _translate_one_impl(
                client,
                filename,
                content,
                target_language,
                settings,
                max_wait_seconds,
            )
    except TimeoutError:
        raise MeasurementError(
            "Upload, triage, translation, or rendering exceeded the configured timeout."
        ) from None


async def _translate_one_impl(
    client: httpx.AsyncClient,
    filename: str,
    content: bytes,
    target_language: str,
    settings: Settings,
    max_wait_seconds: float,
) -> dict[str, object]:
    media_type = (
        "application/pdf"
        if filename.lower().endswith(".pdf")
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    uploaded = await client.post(
        "/api/documents",
        files={"file": (filename, content, media_type)},
        timeout=max_wait_seconds,
    )
    upload_data = _json_response(uploaded, "document upload")
    document_id = str(upload_data["id"])
    document_data = await _wait_document_ready(client, document_id, max_wait_seconds)
    if document_data.get("status") != "extracted":
        raise MeasurementError("Document triage did not complete successfully.")

    created = await client.post(
        "/api/jobs",
        json={
            "document_id": document_id,
            "target_languages": [target_language],
            "idempotency_key": f"measurement-{uuid4()}",
        },
        timeout=max_wait_seconds,
    )
    batch_data = _json_response(created, "job enqueue")
    job_id = str(batch_data["jobs"][0]["id"])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    while True:
        if loop.time() >= deadline:
            raise MeasurementError(f"Job {job_id} did not finish within the configured timeout.")
        status_response = await client.get(f"/api/jobs/{job_id}", timeout=max_wait_seconds)
        status = _json_response(status_response, "job status")
        if status.get("status") in {"done", "completed_with_errors", "failed"}:
            break
        await asyncio.sleep(0.25)
    if status.get("status") not in {"done", "completed_with_errors"}:
        error = status.get("error")
        error_code = error.get("error_code") if isinstance(error, dict) else "unknown"
        raise MeasurementError(f"Translation job failed ({error_code}).")
    if status.get("status") == "completed_with_errors":
        raise MeasurementError(
            "Translation job completed with failed chunks; quality score omitted."
        )

    download = await client.get(f"/api/jobs/{job_id}/download", timeout=max_wait_seconds)
    if download.status_code != 200:
        _json_response(download, "translated artifact download")
        raise MeasurementError("Translated artifact download failed.")
    measurement = await _job_measurement(settings, job_id)
    return {
        "document_id": document_id,
        "job_id": job_id,
        "artifact": download.content,
        "measurement": measurement,
    }


async def _wait_document_ready(
    client: httpx.AsyncClient, document_id: str, max_wait_seconds: float
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    while loop.time() < deadline:
        response = await client.get(f"/api/documents/{document_id}", timeout=max_wait_seconds)
        document = _json_response(response, "document status")
        status = document.get("status")
        if status == "extracted":
            return document
        if status == "failed":
            raise MeasurementError("Document triage or extraction failed.")
        await asyncio.sleep(0.1)
    raise MeasurementError("Document triage did not finish within the configured timeout.")


def _json_response(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_error:
        if isinstance(payload, dict):
            code = payload.get("error_code", "request_failed")
            message = payload.get("message", "request failed")
            raise MeasurementError(f"{operation} failed ({code}): {message}")
        raise MeasurementError(f"{operation} failed with HTTP {response.status_code}.")
    if not isinstance(payload, dict):
        raise MeasurementError(f"{operation} returned an invalid response.")
    return payload


async def _job_measurement(settings: Settings, job_id: str) -> dict[str, object]:
    connection = await SqliteConnectionFactory(settings.database_path, init_schema=False).create()
    try:
        result = await MeasurementPersistence(connection).get_job_measurement(job_id)
    finally:
        await connection.close()
    if result is None:
        raise MeasurementError("Completed job had no persisted measurement record.")
    return result


async def _document_snapshot(
    settings: Settings, document_id: str
) -> tuple[list[Block], DocumentAnalysisRecord]:
    connection = await SqliteConnectionFactory(settings.database_path, init_schema=False).create()
    try:
        repository = SqliteDocumentRepository(connection)
        blocks = await repository.get_blocks(document_id)
        analysis = await repository.get_analysis(document_id)
    finally:
        await connection.close()
    if not blocks:
        raise MeasurementError("Input document produced no extractable text blocks.")
    if analysis is None:
        raise MeasurementError("Triage did not persist a document analysis.")
    return blocks, analysis


async def _extract_rendered_text(path: Path, document_id: str) -> str:
    registry = _format_registry()
    resolved = await registry.resolve(path)
    if resolved is None:
        raise MeasurementError("The rendered artifact format could not be extracted.")
    extractor, _renderer = resolved
    document = await extractor.extract(path, document_id)
    if not document.blocks:
        raise MeasurementError("Rendered artifact contains no extractable translated text.")
    return _join_blocks(document.blocks)


async def _read_reference(path: Path) -> str:
    if path.suffix.lower() == ".txt":
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except (OSError, UnicodeError):
            raise MeasurementError("Reference text must be readable UTF-8.") from None
        if not text.strip():
            raise MeasurementError("Reference text is empty.")
        return text
    return await _extract_rendered_text(path, f"reference-{uuid4()}")


def _format_registry() -> FormatRegistry:
    registry = FormatRegistry()
    registry.register("pdf", PdfExtractor(), PdfRenderer())
    registry.register("docx", DocxExtractor(), DocxRenderer())
    return registry


def _join_blocks(blocks: Sequence[Block]) -> str:
    return "\n".join(block.source_text for block in blocks)


def _percentile_nearest_rank(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered) / 100) - 1)]


def render_human_report(report: dict[str, object]) -> str:
    quality = report["quality"]
    usage = report["usage"]
    measurement = report["measurement"]
    jobs = report["jobs"]
    assert (
        isinstance(quality, dict)
        and isinstance(usage, dict)
        and isinstance(measurement, dict)
        and isinstance(jobs, list)
    )
    rows = [
        ("Date (UTC)", measurement["date_utc"]),
        ("Sample", measurement["sample_filename"]),
        ("Provider / model", f"{measurement['provider']} / {measurement['model']}"),
        ("Source blocks", str(measurement["source_block_count"])),
        ("Quality mode", quality["mode"]),
        ("chrF", f"{quality['score']:.2f} / 100"),
        (
            "Number/placeholder preservation",
            _format_percent(report["number_placeholder_preservation"]),
        ),
        ("Job count", str(usage["job_count"])),
        ("Cost (all jobs)", f"${usage['cost_usd']:.6f}"),
        ("Retry cost share", f"{usage['retry_cost_share_percent']:.2f}%"),
        ("Tokens in / out", f"{usage['tokens_in']} / {usage['tokens_out']}"),
        ("Provider attempts", str(usage["attempt_count"])),
        ("p95 chunk attempt latency", _format_ms(usage["p95_chunk_attempt_latency_ms"])),
        ("p95 job latency", _format_ms(usage["p95_job_latency_ms"])),
    ]
    for index, job in enumerate(jobs, start=1):
        if isinstance(job, dict):
            rows.append(
                (
                    f"Job {index} ({job['phase']})",
                    f"{job['target_language']}; {job['block_count']} blocks / "
                    f"{job['total_chunks']} chunks; ${job['cost_usd']:.6f}; "
                    f"{job['job_latency_ms']} ms",
                )
            )
    width = max(len(label) for label, _value in rows)
    return "\n".join(
        [
            f"{'Metric':<{width}} | Value",
            f"{'-' * width}-+-{'-' * 30}",
            *[f"{label:<{width}} | {value}" for label, value in rows],
        ]
    )


def _format_percent(value: object) -> str:
    if not isinstance(value, dict):
        return "not available"
    percent = value.get("preservation_percent")
    return "not applicable (no tokens)" if percent is None else f"{float(percent):.2f}%"


def _format_ms(value: object) -> str:
    return "not statistically meaningful" if value is None else f"{value} ms"


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError:
        print("Application settings are invalid; check configured values.", file=sys.stderr)
        return 2
    except OSError:
        print("Could not read the requested environment file.", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Could not load application settings; check dotenv syntax and values.",
            file=sys.stderr,
        )
        return 2

    try:
        report = asyncio.run(measure(args, settings))
    except MeasurementError as error:
        print(f"measurement failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Keep provider/adapter internals and settings values out of the report.
        print(f"measurement failed unexpectedly ({type(error).__name__}).", file=sys.stderr)
        return 1
    print(render_human_report(report), file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
