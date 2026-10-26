"""Precision and recall of findings against the planted ground truth."""

from collections.abc import Iterable
from dataclasses import dataclass

from tilgangsinnsikt.analyses.base import Finding

# Orphan accounts have no identity, so the ground truth lists their account ids.
ACCOUNT_RULES = {"orphan_account"}


@dataclass(frozen=True)
class Score:
    rule: str
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 1.0

    @property
    def recall(self) -> float:
        planted = self.true_positives + self.false_negatives
        return self.true_positives / planted if planted else 1.0

    def __str__(self) -> str:
        return (
            f"{self.rule}: tp={self.true_positives} fp={self.false_positives} "
            f"fn={self.false_negatives} precision={self.precision:.2f} recall={self.recall:.2f}"
        )


def refs(findings: Iterable[Finding], rule: str) -> set[str]:
    column = "account_id" if rule in ACCOUNT_RULES else "identity_id"
    return {getattr(f, column) for f in findings if f.rule == rule}


def score(rule: str, findings: Iterable[Finding], planted: Iterable[str]) -> Score:
    found, planted = refs(findings, rule), set(planted)
    return Score(rule, len(found & planted), len(found - planted), len(planted - found))
