"""Загрузка config.yaml.

Отдельный модуль, чтобы путь к конфигу и разбор чисел жили в одном месте:
им пользуются и costs.py, и scan.py, и analyze.py.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_config(path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """Прочитать конфиг. Проверку содержимого делает tools/validate_config.py."""
    target = pathlib.Path(path) if path else DEFAULT_CONFIG
    return yaml.safe_load(target.read_text(encoding="utf-8"))
