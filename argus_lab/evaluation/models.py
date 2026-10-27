from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Evidence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Budget(Record):
    tool_calls: int = Field(gt=0)
    wall_seconds: float = Field(gt=0)
    tokens: int | None = Field(default=None, gt=0)


class Case(Record):
    id: Identifier
    expected_bugs: list[Identifier] = Field(max_length=200)
    goals: list[Identifier] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        for values in (self.expected_bugs, self.goals):
            if len(values) != len(set(values)):
                raise ValueError("case identifiers must be unique")
        return self


class Finding(Record):
    id: Identifier
    matched_bug: Identifier | None
    evidence: list[Evidence] = Field(min_length=1, max_length=20)


class Trial(Record):
    arm: Literal["baseline", "argus"]
    case_id: Identifier
    repetition: int = Field(ge=0)
    model: Identifier
    model_settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    host: Identifier
    fixture_revision: Identifier
    task_revision: Identifier
    seed: int
    budget: Budget
    completed: bool
    error: Evidence | None = None
    duration_seconds: float = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    findings: list[Finding] = Field(default_factory=list, max_length=200)
    covered_goals: dict[Identifier, list[Evidence]] = Field(default_factory=dict)
    adjudicator: Identifier

    @model_validator(mode="after")
    def check_evidence(self):
        if self.completed == (self.error is not None):
            raise ValueError("failed trials require an error; completed trials must not have one")
        if len({finding.id for finding in self.findings}) != len(self.findings):
            raise ValueError("finding IDs must be unique within a trial")
        if any(not evidence or len(evidence) > 20 for evidence in self.covered_goals.values()):
            raise ValueError("covered goals require 1-20 evidence references")
        return self


class Dataset(Record):
    schema_version: Literal[1] = 1
    experiment: Identifier
    mode: Literal["scripted_smoke", "agent"]
    cases: list[Case] = Field(min_length=1, max_length=200)
    trials: list[Trial] = Field(min_length=2, max_length=10000)

    @model_validator(mode="after")
    def paired_trials(self):
        cases = {case.id: case for case in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("case IDs must be unique")
        pairs: dict[tuple[str, int], dict[str, Trial]] = {}
        for trial in self.trials:
            if trial.case_id not in cases:
                raise ValueError("trial references an unknown case")
            case = cases[trial.case_id]
            if not set(trial.covered_goals) <= set(case.goals):
                raise ValueError("coverage references an unknown goal")
            if any(
                f.matched_bug not in case.expected_bugs for f in trial.findings if f.matched_bug
            ):
                raise ValueError("adjudication references a bug outside the case ground truth")
            pair = pairs.setdefault((trial.case_id, trial.repetition), {})
            if trial.arm in pair:
                raise ValueError("duplicate trial arm")
            pair[trial.arm] = trial
        if {key[0] for key in pairs} != set(cases):
            raise ValueError("every case must be represented")
        controls = (
            "model",
            "model_settings",
            "host",
            "fixture_revision",
            "task_revision",
            "seed",
            "budget",
            "adjudicator",
        )
        for pair in pairs.values():
            if set(pair) != {"baseline", "argus"}:
                raise ValueError("every trial must have both arms, including failures")
            baseline, argus = pair["baseline"], pair["argus"]
            if any(getattr(baseline, key) != getattr(argus, key) for key in controls):
                raise ValueError("paired trials must use identical experimental controls")
        return self
