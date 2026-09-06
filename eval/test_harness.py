"""The harness's own logic: rubrics, the judge, and the one rule that a
discarded row is never an abstention."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import ALL, CRASHLOOP, IMAGEPULL, OOM, Rubric, by_id
from run import judge


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "incident_id": "inc-1",
        "failure_type": "CrashLoopBackOff",
        "outcome": "DIAGNOSED",
        "evidence_class": "crashloop_with_fatal_log",
        "root_cause": "",
        "explanation": "",
        "competing_hypothesis": "",
        "confidence_final": 0.8,
        "cost_usd": 0.001,
        "prompt_tokens": 4000,
        "completion_tokens": 200,
        "latency_ms_total": 3000,
        "validation_retries": 0,
    }
    base.update(overrides)
    return base


def test_catalogue_is_large_enough_and_shaped_as_designed() -> None:
    assert len(CRASHLOOP) >= 20
    assert len(IMAGEPULL) >= 20
    assert len(OOM) >= 20
    assert len({i.id for i in ALL}) == len(ALL)
    shapes = {i.log_shape for i in CRASHLOOP}
    assert shapes == {"fatal", "no_clear_error", "no_logs"}, "every 2.3 row is represented"
    assert sum(1 for i in CRASHLOOP if not i.diagnosable) >= 8
    assert {i.why for i in OOM} == {"limit_too_low", "leak", "spike", "init"}


def test_rubric_passes_paraphrase_and_fails_a_different_cause() -> None:
    inc = by_id()["crash-db-refused"]
    assert inc.rubric.matches("The app could not reach the Postgres database.")
    assert inc.rubric.matches("connection to 10.96.31.14:5432 was refused")
    assert not inc.rubric.matches("The container exceeded its memory limit.")
    assert not Rubric(any_of=(("a",),), none_of=("b",)).matches("a and b")


def test_diagnosed_rows_score_against_the_truth() -> None:
    inc = by_id()["crash-db-refused"]
    assert judge(inc, _row(root_cause="cannot connect to the postgres database"))["correct"]
    assert not judge(inc, _row(root_cause="the memory limit is too low"))["correct"]
    assert not judge(inc, _row(root_cause="cannot connect to postgres", failure_type="OOMKilled"))[
        "correct"
    ]


def test_abstention_is_correct_only_when_the_cause_is_not_in_the_evidence() -> None:
    thin = by_id()["crash-silent-exit-1"]
    rich = by_id()["crash-db-refused"]
    assert judge(thin, _row(outcome="INSUFFICIENT_CONTEXT"))["correct"]
    assert not judge(rich, _row(outcome="INSUFFICIENT_CONTEXT"))["correct"]
    assert judge(thin, _row(outcome="INSUFFICIENT_CONTEXT"))["abstained"]


def test_a_discarded_row_is_never_an_abstention_whatever_the_reason() -> None:
    """Quota exhaustion is not the gate firing, and it must never look like it."""
    thin = by_id()["crash-silent-exit-1"]
    scored = judge(
        thin,
        _row(
            outcome="DISCARDED",
            discard_reason=(
                "RateLimitError: 429 tokens per day (TPD): Limit 200000 ... try again in 24m"
            ),
            confidence_final=None,
            cost_usd=0.0,
        ),
    )
    assert scored["discarded"]
    assert not scored["abstained"]
    assert not scored["correct"], "a discard is not a correct abstention on a thin incident"


def test_oom_why_is_scored_as_correct_declined_or_wrong() -> None:
    leak = next(i for i in OOM if i.why == "leak")
    assert (
        judge(
            leak, _row(failure_type="OOMKilled", root_cause="OOMKilled: the process leaks memory")
        )["oom_why"]
        == "correct"
    )
    assert (
        judge(
            leak,
            _row(failure_type="OOMKilled", root_cause="OOMKilled: the memory limit is too low"),
        )["oom_why"]
        == "wrong"
    )
    declined = judge(
        leak,
        _row(
            failure_type="OOMKilled",
            root_cause="killed for exceeding its memory limit",
            competing_hypothesis="a leak or a transient spike would produce the same evidence",
        ),
    )
    assert declined["oom_why"] == "declined"
    assert declined["oom_what"]


def test_no_script_relies_on_a_dollar_kubernetes_would_eat() -> None:
    """Recorded: three incidents never ran what they were written to run.

    Kubernetes expands ``$(VAR)`` in container args and treats ``$$`` as the
    escape for a literal dollar. A script written with ``kill -9 $$`` reached
    the shell as ``kill -9 $``, which busybox refused, so the container
    exited 1 with a shell error on stdout instead of dying on a signal with
    nothing. The evidence was wrong and the catalogue's stated truth was
    wrong with it, which would have scored a correct diagnosis as a miss.
    """
    for inc in ALL:
        assert "$$" not in inc.script, f"{inc.id}: Kubernetes will collapse $$ to $"
