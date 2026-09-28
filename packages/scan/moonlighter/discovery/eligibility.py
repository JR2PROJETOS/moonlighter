"""Compatibility API for location classification without candidate context.

A posting's location and remote type cannot establish whether an arbitrary
candidate can work there. The evaluator receives those fields together with the
profile's location, mobility preferences and explicit criteria.
"""

from enum import StrEnum


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    AMBIGUOUS = "ambiguous"


def classify_location(location: str | None, remote_type: str | None) -> Eligibility:
    """Defer to profile-aware evaluation; retain the legacy two-argument API.

    Never infer residence, authorization or relocation preferences from a job.
    Existing callers still receive an Eligibility value, without a default city
    or region silently excluding opportunities.
    """
    return Eligibility.AMBIGUOUS
