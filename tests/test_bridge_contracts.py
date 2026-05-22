import ast
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.architecture


def _import_targets(relative_path: str) -> set[str]:
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            targets.add("." * node.level + (node.module or ""))
    return targets


def test_bridge_core_does_not_import_concrete_adapters():
    targets = _import_targets("src/bridge/core.py")

    assert not any("adapters.max_adapter" in target for target in targets)
    assert not any("adapters.tg_adapter" in target for target in targets)
    assert any(target.endswith("contracts") for target in targets)


def test_bridge_contracts_stay_transport_neutral():
    targets = _import_targets("src/bridge/contracts.py")

    forbidden = ("pymax", "aiogram", "adapters.")
    assert not any(any(name in target for name in forbidden) for target in targets)


def test_main_keeps_runtime_wiring_in_composition_root():
    targets = _import_targets("src/main.py")

    forbidden = ("adapters.max_adapter", "adapters.tg_adapter", "bridge.core")
    assert not any(any(name in target for name in forbidden) for target in targets)
    assert any(target.endswith("startup.composition") for target in targets)


def test_pymax_imports_stay_inside_max_adapter_boundary():
    offenders = []
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        if "from pymax" not in content and "import pymax" not in content:
            continue
        if not relative.startswith("src/adapters/max/backends/pymax/"):
            offenders.append(relative)

    assert offenders == []


def test_max_adapter_uses_composition_not_mixins():
    adapter_source = (PROJECT_ROOT / "src/adapters/max/adapter.py").read_text(encoding="utf-8")
    assert "class MaxAdapter(" not in adapter_source
    assert "Mixin" not in adapter_source

    offenders = []
    for path in (PROJECT_ROOT / "src/adapters/max").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        if "Mixin" in content:
            offenders.append(relative)
        if "MaxAdapter" in content and "def __init__(" in content and "MaxAdapter" in content.split("def __init__(", 1)[1].split(")", 1)[0]:
            offenders.append(relative)
    assert offenders == []


def test_max_services_use_explicit_dependencies():
    offenders = []
    for path in (PROJECT_ROOT / "src/adapters/max").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        if "ExplicitMaxService" in content:
            offenders.append(f"{relative}: ExplicitMaxService")
        if "MaxServiceRegistry" in content:
            offenders.append(f"{relative}: MaxServiceRegistry")
        if "def __getattr__" in content:
            offenders.append(f"{relative}: __getattr__")
        if "service_base" in content:
            offenders.append(f"{relative}: service_base")
        if (
            "MaxAdapter" in content
            and "def __init__(" in content
            and "MaxAdapter" in content.split("def __init__(", 1)[1].split(")", 1)[0]
        ):
            offenders.append(f"{relative}: service takes MaxAdapter")

    test_source = (PROJECT_ROOT / "tests/test_max_adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(test_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id in {"MaxAdapter", "RealMaxAdapter"}:
                    offenders.append(f"tests/test_max_adapter.py: {node.name} subclasses {base.id}")

    assert offenders == []


def test_max_services_do_not_use_god_base_forwarders():
    offenders = []
    for path in (PROJECT_ROOT / "src/adapters/max").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                base_name = getattr(base, "id", None) or getattr(base, "attr", None)
                if base_name == "ExplicitMaxService":
                    offenders.append(f"{relative}: {node.name} inherits ExplicitMaxService")
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                has_varargs = child.args.vararg is not None or child.args.kwarg is not None
                body = ast.get_source_segment(content, child) or ""
                if has_varargs and "return self._deps." in body:
                    offenders.append(f"{relative}: {node.name}.{child.name} is a deps forwarder")

    assert offenders == []


def test_bridge_core_keeps_heavy_leaf_logic_outside_coordinator():
    source = (PROJECT_ROOT / "src/bridge/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    forbidden_methods = {
        "_build_status_message",
        "_build_recovery_report_message",
        "_parse_recovery_set_fields",
        "_enqueue_retryable_media_failures",
        "_find_existing_pending_media_for_failure",
        "_safe_recovery_scan",
        "_cmd_recovery",
        "_cmd_dm",
    }
    forbidden_attrs = {
        "_recovery_scan_task",
        "_recovery_event_scan_task",
        "_recovery_event_scan_at",
        "_recovery_event_scan_reasons",
        "_recovery_event_last_scan_at",
        "_last_recovery_notification_digest",
    }
    targets = _import_targets("src/bridge/core.py")

    assert forbidden_methods.isdisjoint(method_names)
    assert not any(attr in source for attr in forbidden_attrs)
    assert not any(target.endswith("recovery.reporter") for target in targets)
    assert any(target.endswith("commands.dispatcher") for target in targets)
