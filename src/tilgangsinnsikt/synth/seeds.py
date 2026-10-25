"""Deterministic random streams, one per generation stage."""

import numpy as np

STAGES = ("people", "assignments", "plant", "events", "logins")


def streams(seed: int) -> dict[str, np.random.Generator]:
    # Independent child streams: changing how one stage draws never shifts another stage.
    children = np.random.SeedSequence(seed).spawn(len(STAGES))
    return {
        stage: np.random.default_rng(child) for stage, child in zip(STAGES, children, strict=True)
    }
