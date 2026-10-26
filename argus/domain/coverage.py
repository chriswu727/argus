from typing import Literal

from typing_extensions import TypedDict


class FinalCoverageUpdate(TypedDict):
    goal: str | list[str]
    status: Literal["exercised", "blocked"]
    evidence: str
