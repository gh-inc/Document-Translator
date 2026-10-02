"""Static architecture guards for layering and format ownership."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
FORBIDDEN_CORE_ROOTS = {"api", "mcp_server", "worker", "adapters"}
FORBIDDEN_FORMAT_IDENTIFIERS = {
    "JobRecord",
    "JobStatus",
    "JobExecutionRepository",
    "ChunkRecord",
    "ChunkStatus",
    "claim_job",
    "claim_chunk",
    "queue",
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_name(path: Path) -> str:
    module = _module_name(path)
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _import_targets(node: ast.Import | ast.ImportFrom, package: str) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}

    if node.level:
        relative_name = "." * node.level + (node.module or "")
        base = importlib.util.resolve_name(relative_name, package)
    else:
        base = node.module or ""

    targets = {base} if base else set()
    for alias in node.names:
        if alias.name != "*":
            targets.add(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _is_forbidden_core_target(target: str) -> bool:
    parts = target.split(".")
    if len(parts) >= 2 and parts[0] == "app" and parts[1] in FORBIDDEN_CORE_ROOTS:
        return True
    return bool(parts and parts[0] in FORBIDDEN_CORE_ROOTS)


def test_core_imports_respect_forbidden_package_boundaries() -> None:
    violations: list[str] = []
    for path in sorted((APP / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = _package_name(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                for target in _import_targets(node, package):
                    if _is_forbidden_core_target(target):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {target}")

    assert not violations, "core imports forbidden layers:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "package", "expected"),
    [
        ("import app.api.routes", "app.core", "app.api.routes"),
        ("from app import worker", "app.core", "app.worker"),
        ("from ... import adapters", "app.core.nested", "app.adapters"),
        ("from ...api.schemas import Request", "app.core.nested", "app.api.schemas"),
    ],
)
def test_core_import_guard_resolves_absolute_and_relative_imports(
    source: str,
    package: str,
    expected: str,
) -> None:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.Import | ast.ImportFrom)
    assert expected in _import_targets(node, package)
    assert _is_forbidden_core_target(expected)


def test_core_mentions_format_metadata_only_in_block_declaration() -> None:
    references: list[Path] = []
    for path in sorted((APP / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and node.id == "format_metadata"
                or isinstance(node, ast.Attribute)
                and node.attr == "format_metadata"
                or isinstance(node, ast.Constant)
                and node.value == "format_metadata"
            ):
                references.append(path)

    model_path = APP / "core" / "models.py"
    assert references == [model_path], (
        "format_metadata must be declared once on Block and remain opaque to the rest "
        f"of core; found {[path.relative_to(ROOT).as_posix() for path in references]}"
    )

    model_tree = ast.parse(model_path.read_text(encoding="utf-8"))
    block = next(
        node for node in model_tree.body if isinstance(node, ast.ClassDef) and node.name == "Block"
    )
    declaration_count = sum(
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "format_metadata"
        for node in block.body
    )
    assert declaration_count == 1


def test_pdf_and_docx_adapters_stay_within_format_ports() -> None:
    format_dir = APP / "adapters" / "formats"
    adapter_paths = [
        path for path in (format_dir / "pdf.py", format_dir / "docx.py") if path.is_file()
    ]
    assert (format_dir / "registry.py").is_file(), "format registry skeleton is missing"

    for path in adapter_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = _package_name(path)
        imports: set[str] = set()
        identifiers: set[str] = set()
        async_methods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                imports.update(_import_targets(node, package))
            elif isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.AsyncFunctionDef):
                async_methods.add(node.name)

        message = f"{path.relative_to(ROOT)} must implement async extract and render methods"
        assert {"extract", "render"} <= async_methods, message
        assert not any(
            "app.worker" in target or "app.adapters.persistence" in target for target in imports
        ), f"{path.relative_to(ROOT)} imports execution or persistence code"
        assert not (identifiers & FORBIDDEN_FORMAT_IDENTIFIERS), (
            f"{path.relative_to(ROOT)} references job or queue concepts: "
            f"{sorted(identifiers & FORBIDDEN_FORMAT_IDENTIFIERS)}"
        )


def test_format_registry_is_not_a_combined_adapter_port() -> None:
    registry_path = APP / "adapters" / "formats" / "registry.py"
    assert registry_path.is_file(), "format registry skeleton is missing"
    tree = ast.parse(registry_path.read_text(encoding="utf-8"), filename=str(registry_path))
    source = registry_path.read_text(encoding="utf-8")

    assert "FormatAdapter" not in source
    has_registry_class = any(isinstance(node, ast.ClassDef) for node in tree.body)
    assert has_registry_class, "registry should expose a concrete registry class"

    imports: set[str] = set()
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            imports.update(_import_targets(node, _package_name(registry_path)))
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    assert any(target.endswith("DocumentExtractor") for target in imports)
    assert any(target.endswith("DocumentRenderer") for target in imports)
    assert not identifiers & FORBIDDEN_FORMAT_IDENTIFIERS


@pytest.mark.parametrize("relative", ["api", "mcp_server", "worker", "adapters"])
def test_forbidden_import_matcher_checks_full_package_boundaries(relative: str) -> None:
    assert _is_forbidden_core_target(f"app.{relative}")
    assert _is_forbidden_core_target(f"app.{relative}.nested.module")
    assert _is_forbidden_core_target(f"{relative}.nested.module")
    assert not _is_forbidden_core_target(f"app.core.{relative}")
