"""Regression contracts at LLM and discovery boundaries; no live services."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from moonlighter.application.assisted.composer import _generate
from moonlighter.application.assisted.questions import FormQuestion, QuestionKind
from moonlighter.application.cvgen.generate import USE_BASE, decide_cv
from moonlighter.application.cvgen.pool import CVPool
from moonlighter.core.db import init_db
from moonlighter.discovery.evaluator import (
    EVAL_BATCH_PREFIX,
    EVAL_PREFIX,
    EvalInput,
    evaluate_job,
    evaluate_jobs_batch,
    profile_for_eval,
)
from moonlighter.discovery.service import _evaluate_and_store, _scan_gupy
from moonlighter.discovery.sources.base import RawJob
from moonlighter.discovery.sources.http import RemotiveScanner

PROFILES = [
    {
        "headline": "BIM Modeler",
        "skills": ["Revit"],
        "location": "Dourados, Brazil",
        "preferences": {"relocation": True},
        "criteria": {"soft_filters": ["International contracts welcome"]},
    },
    {
        "headline": "Senior Software Engineer",
        "skills": ["Python"],
        "location": "Berlin",
        "criteria": {"hard_filters": ["Onsite work required; remote-only roles excluded"]},
    },
    {
        "headline": "Nurse",
        "skills": ["Patient care"],
        "location": "Nairobi",
        "preferences": {"relocation": False},
        "criteria": {"soft_filters": ["Prefer permanent employment"]},
    },
    {},
]
RESULT = {"score": 7.0, "score_notes": "Profile-based match", "caveats": []}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("mode", ["single", "batch", "fallback"])
async def test_scoring_uses_only_the_supplied_candidate_identity(profile, mode):
    calls = []

    async def caller(prompt, model, cache_prefix=None):
        calls.append(cache_prefix)
        if mode == "fallback" and len(calls) == 1:
            return "invalid batch"
        return json.dumps([RESULT, RESULT] if mode == "batch" else RESULT)

    job = EvalInput("Employer", "Unrelated role", "External description")
    if mode == "single":
        result = await evaluate_job(
            job.company, job.title, job.description, profile, _caller=caller
        )
        assert result.score == 7.0
    else:
        results = await evaluate_jobs_batch([job, job], profile, "test", caller)
        assert [r.score for r in results] == [7.0, 7.0]
    assert len(calls) == (3 if mode == "fallback" else 1)
    for prefix in calls:
        assert "exclusively from" in prefix
        assert "profile.yaml" in prefix
        assert "unknown" in prefix.lower()
        assert "for a senior software engineer" not in prefix.lower()
        assert "Belo Horizonte" not in prefix
        assert "targets permanent full-time" not in prefix
        if profile:
            assert profile["headline"] in prefix
            assert profile["location"] in prefix
            for values in profile["criteria"].values():
                assert values[0] in prefix


@pytest.mark.parametrize("prefix", [EVAL_PREFIX, EVAL_BATCH_PREFIX])
def test_scoring_instructions_do_not_add_candidate_preferences(prefix):
    assert "stack match" not in prefix
    assert "requires .NET" not in prefix
    assert "Do not assume" in prefix
    assert "relocation" in prefix
    assert "no default preference" in prefix
    assert "criteria.hard_filters" in prefix
    assert "unmet mandatory:" in prefix


def test_scoring_keeps_location_education_and_explicit_preferences_without_contacts():
    profile = {
        "location": "Any candidate location",
        "education": [{"degree": "Professional qualification"}],
        "preferences": {"remote": True, "relocation": True},
        "criteria": {
            "hard_filters": ["A stated dealbreaker"],
            "soft_filters": ["Contracts welcome"],
        },
        "email": "private@example.com",
    }
    assert profile_for_eval(profile) == {k: v for k, v in profile.items() if k != "email"}


@pytest.mark.parametrize("profile", PROFILES)
async def test_application_answers_infer_identity_from_profile_only(profile):
    calls = []

    async def caller(prompt, model):
        calls.append(prompt)
        return "UNKNOWN"

    question = FormQuestion(
        label="Describe your professional background",
        kind=QuestionKind.SINGLE_SELECT,
        options=("Experienced", "Entry level"),
        required=True,
    )
    assert await _generate(question, profile, {"description": "An unrelated role"}, caller) is None
    assert "exclusively from" in calls[0]
    assert "profile.yaml" in calls[0]
    assert "technology used in production" not in calls[0]
    if profile:
        assert profile["headline"] in calls[0]


@pytest.mark.parametrize("profile", PROFILES)
async def test_cv_does_not_infer_profession_from_template_or_require_open_source(profile):
    calls = []

    async def caller(prompt, model, cache_prefix=None):
        calls.append(cache_prefix)
        return json.dumps({"decision": "USE_BASE"})

    pool = CVPool(experiences=(), open_source=(), summary_facts=())
    result = await decide_cv({}, pool, profile, "A base summary", "Documented skills", caller)
    assert result == USE_BASE
    assert "exclusively from" in calls[0]
    assert "profile.yaml" in calls[0]
    assert "Open source:\n" not in calls[0]
    assert "optional" in calls[0].lower()
    assert "technical_expertise" in calls[0]  # keep the existing output schema
    if profile:
        assert profile["headline"] in calls[0]


@pytest.mark.parametrize("keywords", ["", "Revit", "nursing", "software engineer"])
async def test_gupy_never_substitutes_a_profession(keywords):
    with patch("moonlighter.discovery.sources.http.GupyScanner") as scanner:
        scanner.return_value.scan = AsyncMock(return_value=[])
        await _scan_gupy(keywords, {"scan_gupy": True}, {})
    scanner.return_value.scan.assert_awaited_once_with(keywords=keywords, stats={})


async def test_remotive_request_has_no_implicit_professional_category():
    with patch("moonlighter.discovery.sources.http.get_json", new_callable=AsyncMock) as get_json:
        get_json.return_value = {"jobs": []}
        await RemotiveScanner().scan()
    assert get_json.await_args.args[1] == "https://remotive.com/api/remote-jobs"


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("remote_type", ["onsite", "hybrid"])
async def test_foreign_location_reaches_evaluator_with_profile_and_structured_fields(
    tmp_db, profile, remote_type
):
    # Exercise persistence with synthetic input directly, without invoking a scan.
    init_db()
    raw = RawJob(
        source="greenhouse",
        company="Employer",
        title="Contract role",
        url="https://example.com/job/1",
        location="London, UK",
        remote_type=remote_type,
        description="A complete description of a professional role.",
    )
    calls = []

    async def caller(prompt, model, cache_prefix=None):
        calls.append((prompt, cache_prefix))
        return json.dumps(RESULT)

    saved, stopped = await _evaluate_and_store([raw], {"score_threshold": 6.5}, profile, caller)
    assert not stopped
    assert len(saved) == 1
    assert saved[0].status == "new"
    assert saved[0].score == 7.0
    assert len(calls) == 1
    assert "Location: London, UK" in calls[0][0]
    assert f"Remote type: {remote_type}" in calls[0][0]
    if profile:
        assert profile["location"] in calls[0][1]
        for values in profile["criteria"].values():
            assert values[0] in calls[0][1]
