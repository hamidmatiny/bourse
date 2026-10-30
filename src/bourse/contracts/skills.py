"""Closed skill taxonomy. Adding a skill requires an ADR."""

from enum import StrEnum


class SkillId(StrEnum):
    SCHEMA_PROFILE = "schema.profile"
    STATS_COMPUTE = "stats.compute"
    PII_DETECT = "pii.detect"
    UNITS_NORMALIZE = "units.normalize"
    REPORT_COMPOSE = "report.compose"
