"""Immutable runtime loading for published filter-profile versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.filter_engine.catalog import FilterCatalog, build_default_catalog
from engine.filter_engine.models import EvaluationMode, FilterExpression


@dataclass(frozen=True)
class LoadedProfile:
    profile_id: int
    version_id: int
    version_number: int
    expression: FilterExpression
    requirements: dict[str, Any]
    risk_config: dict[str, Any]
    timeframe_config: dict[str, Any]


class ProfileLoader:
    """Load only published profile versions and return immutable runtime snapshots."""

    def __init__(self, profile_store, catalog: FilterCatalog | None = None):
        self._store = profile_store
        self._catalog = catalog or build_default_catalog()

    def load(self, profile_id: int, version_number: int | None = None) -> LoadedProfile:
        version = self._store.get_version(profile_id, version_number)
        if not version:
            raise KeyError(f"filter profile version not found: {profile_id}/{version_number}")
        if version.get("status") != "published":
            raise ValueError("only published filter profile versions can run automatically")
        expression = FilterExpression.model_validate(version.get("expression_json", {}))
        mode = EvaluationMode.SIGNAL
        self._catalog.validate_expression(expression, mode)
        return LoadedProfile(
            profile_id=profile_id,
            version_id=int(version["id"]),
            version_number=int(version["version_number"]),
            expression=expression,
            requirements=version.get("requirements_json", {}) or {},
            risk_config=version.get("risk_config_json", {}) or {},
            timeframe_config=version.get("timeframe_config_json", {}) or {},
        )
