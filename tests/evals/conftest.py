"""Pytest conftest for the golden-dataset retrieval evaluation.

`build_test_case` lives in _helpers.py (conftest can't be imported cross-package
by pytest). This file only holds fixtures the test modules depend on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

GOLDEN_PATH = Path(__file__).parent / "golden_dataset.yaml"


@pytest.fixture(scope="session")
def golden_queries() -> list[dict[str, Any]]:
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data["queries"]
