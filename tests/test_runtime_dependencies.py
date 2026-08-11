from __future__ import annotations

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dependencies_declare_cpu_compatible_torch() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]

    assert "torch>=2.6,<3" in project["dependencies"]
