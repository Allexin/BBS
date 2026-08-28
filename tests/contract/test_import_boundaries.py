import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "backup_system"


def imported_modules(package: str) -> set[str]:
    modules: set[str] = set()
    for path in (SOURCE_ROOT / package).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def test_manager_does_not_import_executor() -> None:
    assert not any(
        name.startswith("backup_system.executor") for name in imported_modules("manager")
    )


def test_executor_does_not_import_manager() -> None:
    assert not any(
        name.startswith("backup_system.manager") for name in imported_modules("executor")
    )
