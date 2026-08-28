"""Deterministic UTF-8 JSON serialization for line protocols."""

import json
from collections.abc import Mapping
from typing import Any


def dumps_line(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
