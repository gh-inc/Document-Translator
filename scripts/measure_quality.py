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
import sys
import tempfile
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
from app.adapters.llm.pricing import ModelCostCalculator
from app.adapters.persistence.database import SqliteConnectionFactory
from app.adapters.persistence.measurements import MeasurementPersistence
from app.adapters.persistence.repositories import SqliteDocumentRepository
from app.api.main import create_app
from app.config import Settings
from app.core.models import Block, DocumentAnalysisRecord
from app.core.quality import chrf_score, preservation_metrics
from app.worker.__main__ import run_worker


class MeasurementError(RuntimeError):
    """A safe, user-facing error raised by this explicit measurement command."""


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
    parser.add_argument(
        "--models",
        help="comma-separated priced models to compare (for example gpt-4o-mini,gpt-4o)",
    )
    return parser


def _models_to_compare(value: str) -> list[str]:
    models = [model.strip() for model in value.split(",")]
    if not models or any(not model for model in models):
        raise MeasurementError("--models must be a comma-separated list of model names.")
    if len(models) != len(set(models)):
        raise MeasurementError("--models must not contain duplicate model names.")
    calculator = ModelCostCalculator()
    for model in models:
        try:
            calculator.estimate(model, 0, 0)
        except ValueError:
            raise MeasurementError(
                f"--models includes an unsupported priced model: {model}."
            ) from None
    return models


async def measure_models(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    """Run each model through a fresh private database and cache."""
    models = _models_to_compare(args.models)
    reports: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    for model in models:
        run_settings = settings.model_copy(update={"openai_model": model})
        report = await measure(args, run_settings, include_pipeline_usage=True)
        reports.append(report)
        rows.append(_comparison_row(report))
    return {
        "comparison": rows,
        "reports": reports,
        "notes": [
            "Cost is the application estimate for provider-reported bulk and triage usage; "
            "ambiguous or unrecorded usage is excluded, so this is not a billing total.",
            "Cost per million is observed known cost divided by all reported input and output "
            "tokens combined, multiplied by 1,000,000; it is not a provider price tier.",
            "Requests counts recorded bulk chunk attempts only. Triage request counts are "
            "not persisted and are excluded; glossary lookup makes no provider requests.",
        ],
    }


def _comparison_row(report: dict[str, object]) -> dict[str, object]:
    measurement = report["measurement"]
    quality = report["quality"]
    preservation = report["number_placeholder_preservation"]
    pipeline_usage = report["pipeline_usage"]
    assert isinstance(measurement, dict)
    assert isinstance(quality, dict)
    assert isinstance(preservation, dict)
    assert isinstance(pipeline_usage, dict)
    tokens_in = int(pipeline_usage["known_tokens_in"])
    tokens_out = int(pipeline_usage["known_tokens_out"])
    cost = float(pipeline_usage["known_estimated_cost_usd"])
    total_tokens = tokens_in + tokens_out
    return {
        "model": measurement["model"],
        "cost_per_million_mixed_tokens_usd": cost * 1_000_000 / total_tokens
        if total_tokens
        else None,
        "known_estimated_run_cost_usd": cost,
        "known_tokens_in": tokens_in,
        "known_tokens_out": tokens_out,
        "chrf": quality["score"],
        "preservation_percent": preservation["preservation_percent"],
        "recorded_bulk_requests": pipeline_usage["recorded_bulk_attempts"],
    }


async def measure(
    args: argparse.Namespace, settings: Settings, *, include_pipeline_usage: bool = False
) -> dict[str, object]:
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
        analyses: list[DocumentAnalysisRecord] = []
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
                    analyses.append(analysis)
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
                        if include_pipeline_usage:
                            _backward_blocks, backward_analysis = await _document_snapshot(
                                run_settings, str(backward["document_id"])
                            )
                            analyses.append(backward_analysis)
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
                    report: dict[str, object] = {
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
                            (
                                "The legacy usage field covers bulk jobs only; persisted "
                                "triage usage is included separately in pipeline_usage."
                                if include_pipeline_usage
                                else "Triage/provider analysis usage is not persisted in jobs or "
                                "chunk_attempts and is excluded."
                            ),
                            "Chunk latency p95 uses persisted provider-attempt latency_ms rows, "
                            "including retries.",
                            "Job latency is persisted updated_at minus created_at; one run "
                            "does not establish a meaningful p95.",
                            "Quality and preservation metrics cover extracted text; DOCX tables "
                            "and other unsupported text regions are excluded.",
                        ],
                    }
                    if include_pipeline_usage:
                        report["pipeline_usage"] = _known_pipeline_usage(jobs, analyses)
                    return report
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


def _known_pipeline_usage(
    jobs: list[dict[str, object]], analyses: list[DocumentAnalysisRecord]
) -> dict[str, object]:
    bulk_cost = sum(float(job["attempt_cost_usd"]) for job in jobs)
    triage_cost = sum(analysis.cost_usd_total for analysis in analyses)
    bulk_tokens_in = sum(int(job["attempt_tokens_in"]) for job in jobs)
    bulk_tokens_out = sum(int(job["attempt_tokens_out"]) for job in jobs)
    triage_tokens_in = sum(analysis.tokens_in_total for analysis in analyses)
    triage_tokens_out = sum(analysis.tokens_out_total for analysis in analyses)
    return {
        "known_estimated_cost_usd": bulk_cost + triage_cost,
        "known_tokens_in": bulk_tokens_in + triage_tokens_in,
        "known_tokens_out": bulk_tokens_out + triage_tokens_out,
        "bulk_estimated_cost_usd": bulk_cost,
        "bulk_tokens_in": bulk_tokens_in,
        "bulk_tokens_out": bulk_tokens_out,
        "triage_estimated_cost_usd": triage_cost,
        "triage_tokens_in": triage_tokens_in,
        "triage_tokens_out": triage_tokens_out,
        "recorded_bulk_attempts": sum(int(job["attempt_count"]) for job in jobs),
        "triage_request_count": None,
    }


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


def render_matrix_report(report: dict[str, object]) -> str:
    rows = report["comparison"]
    reports = report["reports"]
    assert isinstance(rows, list)
    assert isinstance(reports, list) and reports
    first_quality = reports[0]["quality"]
    assert isinstance(first_quality, dict)
    lines = [
        f"Quality mode: {first_quality['mode']}",
        "",
        "| Model | Cost / 1M tokens in+out (USD) | Known run cost (USD) | "
        "chrF | Preservation % | Recorded bulk requests | Tokens in / out |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        assert isinstance(row, dict)
        unit_cost = row["cost_per_million_mixed_tokens_usd"]
        preservation = row["preservation_percent"]
        lines.append(
            f"| {row['model']} | "
            f"{f'${float(unit_cost):.4f}' if unit_cost is not None else 'n/a'} | "
            f"${float(row['known_estimated_run_cost_usd']):.6f} | "
            f"{float(row['chrf']):.2f} | "
            f"{f'{float(preservation):.2f}%' if preservation is not None else 'n/a'} | "
            f"{row['recorded_bulk_requests']} | "
            f"{row['known_tokens_in']} / {row['known_tokens_out']} |"
        )
    notes = report["notes"]
    assert isinstance(notes, list)
    return "\n".join([*lines, "", *[f"- {note}" for note in notes]])


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
        matrix_mode = getattr(args, "models", None) is not None
        report = asyncio.run(
            measure_models(args, settings) if matrix_mode else measure(args, settings)
        )
    except MeasurementError as error:
        print(f"measurement failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Keep provider/adapter internals and settings values out of the report.
        print(f"measurement failed unexpectedly ({type(error).__name__}).", file=sys.stderr)
        return 1
    print(
        render_matrix_report(report) if matrix_mode else render_human_report(report),
        file=sys.stderr,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
