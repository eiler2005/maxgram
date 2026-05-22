import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
