"""A posting's location alone cannot establish a candidate's eligibility."""

import pytest
from moonlighter.discovery.eligibility import Eligibility, classify_location


@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        "Belo Horizonte, Brazil",
        "Dourados, Brazil",
        "London, UK",
        "Nairobi",
        "LATAM",
        "Worldwide",
    ],
)
@pytest.mark.parametrize("remote_type", [None, "onsite", "hybrid", "remote"])
def test_location_requires_profile_and_posting_context(location, remote_type):
    # Keep the legacy two-argument API without inventing candidate preferences.
    assert classify_location(location, remote_type) is Eligibility.AMBIGUOUS
