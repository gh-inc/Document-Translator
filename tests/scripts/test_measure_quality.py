from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest
from docx import Document

from app.adapters.llm import fake_provider
from app.config import Settings
from scripts import measure_quality
from scripts.measure_quality import (
    MeasurementError,
    chrf_score,
    preservation_metrics,
    require_live_provider,
)


def test_chrf_is_whitespace_insensitive_and_uses_character_orders() -> None:
    assert chrf_score("ab c", "a bc") == 100.0
    # Per-order P/R are averaged, then combined with beta=2. This fixture is
    # 38.888... under the standard effective-order chrF variant.
    assert chrf_score("abc", "abd") == pytest.approx(350 / 9)


def test_chrf_empty_and_nonmatching_inputs() -> None:
    assert chrf_score("", "") == 0.0
    assert chrf_score("", "hello") == 0.0
    assert chrf_score("hello", "") == 0.0


def test_number_and_placeholder_preservation_counts_duplicates_and_categories() -> None:
    source = "Order {{id}} on 2024-12-31 costs $12.50 and $12.50; %s [NAME]"
    translated = "Auftrag {{id}} am 2024-12-31 kostet $12.50; %s [NAME]"

    result = preservation_metrics(source, translated)

    assert result["placeholder"] == {
        "source_count": 3,
        "preserved_count": 3,
        "preservation_percent": 100.0,
    }
    assert result["date"] == {
        "source_count": 1,
        "preserved_count": 1,
        "preservation_percent": 100.0,
    }
    assert result["currency"] == {
        "source_count": 2,
        "preserved_count": 1,
        "preservation_percent": 50.0,
    }
    assert result["number"] == {
        "source_count": 0,
        "preserved_count": 0,
        "preservation_percent": None,
    }
    assert result["preservation_percent"] == pytest.approx(100 * 5 / 6)


def test_live_measurement_rejects_fake_provider_and_missing_key() -> None:
    with pytest.raises(MeasurementError, match="fake-provider runs are rejected"):
        require_live_provider(Settings(llm_provider="fake", openai_api_key="sk-not-used"))
    with pytest.raises(MeasurementError, match="requires OPENAI_API_KEY"):
        require_live_provider(Settings(llm_provider="openai", openai_api_key=""))


def test_live_measurement_accepts_configured_openai_without_revealing_key() -> None:
    settings = Settings(llm_provider="openai", openai_api_key="sk-test-secret")

    require_live_provider(settings)

    assert "sk-test-secret" not in repr(settings)


@pytest.mark.parametrize("with_reference", [False, True])
async def test_offline_measurement_runs_upload_triage_worker_render_and_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_reference: bool
) -> None:
    source_text = "Invoice {{id}} dated 2024-12-31 costs $12.50. Call %s [NAME]."
    document_path = tmp_path / "sample.docx"
    document = Document()
    document.add_paragraph(source_text)
    document.save(document_path)
    reference_path = tmp_path / "reference.txt"
    reference_path.write_text(source_text, encoding="utf-8")
    monkeypatch.setattr(measure_quality, "require_live_provider", lambda _settings: None)
    monkeypatch.setattr(fake_provider, "_count_usage_sync", lambda _input, _output: (10, 4))
    settings = Settings(
        llm_provider="fake",
        fake_latency_ms=1,
        database_path=tmp_path / "unused.db",
        upload_storage_path=tmp_path / "unused-uploads",
        output_storage_path=tmp_path / "unused-out",
    )
    args = Namespace(
        document=document_path,
        target_language="de",
        reference=reference_path if with_reference else None,
        timeout=10.0,
    )

    report = await measure_quality.measure(args, settings)

    assert report["quality"]["mode"] == ("reference" if with_reference else "back_translation")
    assert report["quality"]["metric"] == "chrF"
    assert report["quality"]["score"] > 0
    assert report["number_placeholder_preservation"]["preservation_percent"] == 100.0
    assert report["usage"]["job_count"] == (1 if with_reference else 2)
    assert report["jobs"]
    assert all(job["attempt_count"] >= 1 for job in report["jobs"])
    assert all(job["status"] == "done" for job in report["jobs"])
    assert "DOCX tables" in report["measurement_exclusions"][-1]


def test_human_report_and_json_payload_can_be_emitted_on_separate_streams(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {
        "measurement": {
            "date_utc": "2026-10-03",
            "sample_filename": "sample.docx",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "source_block_count": 1,
        },
        "quality": {"mode": "reference", "score": 71.25},
        "number_placeholder_preservation": {"preservation_percent": 100.0},
        "jobs": [],
        "usage": {
            "job_count": 1,
            "cost_usd": 0.01,
            "retry_cost_share_percent": 0.0,
            "tokens_in": 10,
            "tokens_out": 12,
            "attempt_count": 1,
            "p95_chunk_attempt_latency_ms": 20,
            "p95_job_latency_ms": None,
        },
    }
    monkeypatch.setattr(measure_quality, "build_parser", lambda: _ArgsParser())
    monkeypatch.setattr(measure_quality, "Settings", lambda **_kwargs: Settings())

    def return_report(awaitable: object) -> dict[str, object]:
        awaitable.close()  # type: ignore[attr-defined]
        return report

    monkeypatch.setattr(measure_quality.asyncio, "run", return_report)

    assert measure_quality.main() == 0
    output = capsys.readouterr()

    assert json.loads(output.out) == report
    assert "Metric" in output.err
    assert "71.25 / 100" in output.err


def test_cli_exits_nonzero_with_explicit_fake_provider_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(measure_quality, "build_parser", lambda: _ArgsParser())
    monkeypatch.setattr(
        measure_quality,
        "Settings",
        lambda **_kwargs: Settings(llm_provider="fake", openai_api_key="sk-not-used"),
    )

    assert measure_quality.main() == 1
    output = capsys.readouterr()

    assert output.out == ""
    assert "fake-provider runs are rejected" in output.err


class _ArgsParser:
    def parse_args(self) -> Namespace:
        return Namespace(
            env_file=None,
            document=Path("unused.docx"),
            target_language="de",
            reference=None,
            timeout=1.0,
        )
