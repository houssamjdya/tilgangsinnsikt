"""Analyses that read the model tables and write findings."""

from tilgangsinnsikt.analyses import lifecycle, sod
from tilgangsinnsikt.analyses.base import REGISTRY, Finding

__all__ = ["REGISTRY", "Finding", "lifecycle", "sod"]
