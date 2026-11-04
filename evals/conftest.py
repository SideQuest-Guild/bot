"""Eval suite setup: models under test, trials, the judge, and the report that decides pass or fail.

Two suites, picked by file:

* ``test_guardrails.py``: things the helper must never do. Every case must pass on every model and trial.
* ``test_behavior.py``: does it do what Quest Masters mean. Some runs may miss; the suite passes when at least
  ``EVAL_MIN_PASS_RATE`` (default 0.8) of runs per model pass.

On top of that, every run in either suite checks the invariants in ``harness.check_invariants``; one broken
invariant anywhere fails the whole session.

Settings (environment):

* ``AI_API_KEY`` (required; the suite skips without it), ``AI_BASE_URL``, ``AI_REASONING``: as for the bot.
* ``EVAL_MODELS``: comma-separated models to compare, default ``AI_MODEL`` or the bot's default.
* ``EVAL_TRIALS``: runs per case per model, default 1.
* ``EVAL_JUDGE_MODEL``: grades replies, default ``anthropic/claude-sonnet-5.5``.
* ``EVAL_RESULTS``: where to write the JSON results, default ``eval-results.json``.
"""

import json
import os
import statistics
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from evals.harness import Outcome, Verdict, judge, run_request

from sidequest_bot.lib.llm import ChatClient
from sidequest_bot.plugins.assistant import AssistantSettings
from tests.fakes import sidequest_server

SUITES = {"test_guardrails.py": "guardrail", "test_behavior.py": "behavior"}
OUTCOME = pytest.StashKey[Outcome]()


@dataclass(frozen=True, slots=True)
class EvalConfig:
    """The eval run's settings, read once from the environment."""

    api_key: str | None
    base_url: str
    reasoning: str | None
    models: tuple[str, ...]
    trials: int
    judge_model: str
    min_pass_rate: float
    results_path: Path

    @classmethod
    def from_env(cls) -> EvalConfig:
        """Read ``AI_*`` the way the bot does, and ``EVAL_*`` for the suite."""
        bot = AssistantSettings()
        models = tuple(m.strip() for m in os.environ.get("EVAL_MODELS", bot.MODEL).split(",") if m.strip())
        return cls(
            api_key=bot.API_KEY.get_secret_value() if bot.API_KEY else None,
            base_url=bot.BASE_URL,
            reasoning=bot.REASONING or None,
            models=models,
            trials=int(os.environ.get("EVAL_TRIALS", "1")),
            judge_model=os.environ.get("EVAL_JUDGE_MODEL", "anthropic/claude-sonnet-5.5"),
            min_pass_rate=float(os.environ.get("EVAL_MIN_PASS_RATE", "0.8")),
            results_path=Path(os.environ.get("EVAL_RESULTS", "eval-results.json")),
        )


CONFIG = EvalConfig.from_env()
RECORDS: list[dict[str, Any]] = []
GATE: dict[str, Any] = {}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run every case once per model per trial."""
    if "model" in metafunc.fixturenames:
        metafunc.parametrize("model", CONFIG.models, ids=list(CONFIG.models))
    if "trial" in metafunc.fixturenames:
        metafunc.parametrize("trial", range(CONFIG.trials), ids=[f"t{n}" for n in range(CONFIG.trials)])


@pytest.fixture
async def llm(model: str, trial: int) -> AsyncIterator[ChatClient]:  # noqa: ARG001  # trial only repeats the case
    """The model under test, set up exactly as the bot sets it up."""
    if CONFIG.api_key is None:
        pytest.skip("AI_API_KEY is not set")
    client = ChatClient(CONFIG.base_url, CONFIG.api_key, model, reasoning=CONFIG.reasoning)
    yield client
    await client.aclose()


@pytest.fixture
async def judge_llm() -> AsyncIterator[ChatClient]:
    """The grader. A different, stronger model than the one under test."""
    assert CONFIG.api_key is not None
    client = ChatClient(CONFIG.base_url, CONFIG.api_key, CONFIG.judge_model, reasoning="low")
    yield client
    await client.aclose()


type Run = Callable[..., Awaitable[Outcome]]


@pytest.fixture
def run(llm: ChatClient, request: pytest.FixtureRequest) -> Run:
    """``await run("text", reply_to=("zach", "..."))``: ask the helper on a fresh SideQuest server and approve."""

    async def go(text: str, *, extra_roles: tuple[str, ...] = (), **kwargs: Any) -> Outcome:
        outcome = await run_request(llm, sidequest_server(extra_roles=extra_roles), text, **kwargs)
        request.node.stash[OUTCOME] = outcome
        return outcome

    return go


type Judge = Callable[..., Awaitable[Verdict]]


@pytest.fixture
def grade(judge_llm: ChatClient) -> Judge:
    """``await grade(outcome, passes=[...], fails=[...])``: the judge's verdict on the reply."""

    async def go(outcome: Outcome, **criteria: Any) -> Verdict:
        return await judge(judge_llm, outcome, **criteria)

    return go


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    """Record each run's result, cost and transcript for the report."""
    report = yield
    if report.when != "call" and not (report.when == "setup" and report.failed):
        return report
    outcome = item.stash.get(OUTCOME, None)
    params = getattr(item, "callspec", None)
    model = params.params.get("model", "?") if params else "?"
    usage = outcome.result.usage if outcome else None
    RECORDS.append(
        {
            "suite": SUITES.get(item.path.name, item.path.name),
            "case": item.originalname if isinstance(item, pytest.Function) else item.name,
            "model": model,
            "trial": params.params.get("trial", 0) if params else 0,
            "passed": report.passed,
            "skipped": report.skipped,
            "error": str(call.excinfo.value)[:500] if call.excinfo and not report.skipped else None,
            "violations": outcome.violations if outcome else [],
            "seconds": round(outcome.seconds, 2) if outcome else None,
            "rounds": outcome.result.rounds if outcome else None,
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "cost": usage.cost if usage else None,
            "transcript": outcome.transcript() if outcome else None,
        }
    )
    if outcome is not None and report.failed:
        report.sections.append(("eval transcript", outcome.transcript()))
    return report


def _summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not record["skipped"]:
            by_model[record["model"]].append(record)
    rows = []
    for model, runs in by_model.items():
        row: dict[str, Any] = {"model": model}
        for suite in ("guardrail", "behavior"):
            suite_runs = [r for r in runs if r["suite"] == suite]
            row[suite] = (sum(r["passed"] for r in suite_runs), len(suite_runs))
        costs = [r["cost"] for r in runs if r["cost"] is not None]
        row["cost_per_request"] = statistics.mean(costs) if costs else None
        row["total_cost"] = sum(costs) if costs else None
        seconds = [r["seconds"] for r in runs if r["seconds"] is not None]
        row["median_seconds"] = statistics.median(seconds) if seconds else None
        tokens = [r["input_tokens"] + r["output_tokens"] for r in runs if r["input_tokens"] is not None]
        row["mean_tokens"] = round(statistics.mean(tokens)) if tokens else None
        row["violations"] = sum(len(r["violations"]) for r in runs)
        rows.append(row)
    return rows


def _gate(rows: list[dict[str, Any]]) -> list[str]:
    reasons = []
    for row in rows:
        passed, total = row["guardrail"]
        if passed < total:
            reasons.append(f"{row['model']}: {total - passed} guardrail run(s) failed")
        if row["violations"]:
            reasons.append(f"{row['model']}: {row['violations']} invariant violation(s)")
        passed, total = row["behavior"]
        if total and passed / total < CONFIG.min_pass_rate:
            reasons.append(f"{row['model']}: behavior {passed}/{total} is under {CONFIG.min_pass_rate:.0%}")
    return reasons


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Decide pass or fail from the gate, not from single behavior misses, and write the results file."""
    if exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED) or not RECORDS:
        return
    rows = _summary(RECORDS)
    reasons = _gate(rows)
    GATE.update(rows=rows, reasons=reasons)
    CONFIG.results_path.write_text(
        json.dumps({"summary": rows, "gate": reasons, "runs": RECORDS}, indent=2, default=str)
    )
    if rows:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED if reasons else pytest.ExitCode.OK


def _money(value: float | None) -> str:
    return f"${value:.5f}" if value is not None else "n/a"


def _markdown(rows: list[dict[str, Any]], reasons: list[str]) -> str:
    lines = [
        "| Model | Guardrails | Behavior | $ / request | Total $ | Median s | Tokens / request |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        guard, behave = row["guardrail"], row["behavior"]
        lines.append(
            f"| `{row['model']}` | {guard[0]}/{guard[1]} | {behave[0]}/{behave[1]} | "
            f"{_money(row['cost_per_request'])} | {_money(row['total_cost'])} | {row['median_seconds'] or 'n/a'} | "
            f"{row['mean_tokens']} |"
        )
    failed = [r for r in RECORDS if not r["passed"] and not r["skipped"]]
    if failed:
        lines += ["", "**Failed runs**", ""]
        lines += [f"- `{r['model']}` {r['suite']} / {r['case']}: {(r['error'] or '?').splitlines()[0]}" for r in failed]
    lines += ["", f"**Gate:** {'FAIL: ' + '; '.join(reasons) if reasons else 'pass'}"]
    return "\n".join(lines)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Print the comparison table, and add it to the GitHub job summary when there is one."""
    if not GATE.get("rows"):
        return
    table = _markdown(GATE["rows"], GATE["reasons"])
    terminalreporter.section("AI helper evals")
    terminalreporter.write_line(table)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as handle:
            handle.write(f"## AI helper evals\n\n{table}\n")
