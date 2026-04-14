from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class FocusArea:
    name: str
    description: str
    paths: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)


@dataclass
class Config:
    url: str
    focus_areas: List[FocusArea] = field(default_factory=list)
    max_steps: int = 50
    model: str = "gpt-4o-mini"
    headless: bool = True
    screenshot_on_error: bool = True
    output_dir: str = "./argus-reports"
    viewport_width: int = 1280
    viewport_height: int = 720
    api_base: Optional[str] = None
    api_key: Optional[str] = None

    @classmethod
    def from_yaml(cls, path: "str | Path", url: Optional[str] = None) -> Config:
        with open(path) as f:
            data = yaml.safe_load(f)

        focus_areas = []
        for fa in data.get("focus_areas", []):
            focus_areas.append(FocusArea(
                name=fa["name"],
                description=fa.get("description", ""),
                paths=fa.get("paths", []),
                actions=fa.get("actions", []),
            ))

        config_url = url or data.get("url", "")
        if not config_url:
            raise ValueError("URL is required (provide via --url or in config file)")

        return cls(
            url=config_url,
            focus_areas=focus_areas,
            max_steps=data.get("max_steps", 50),
            model=data.get("model", "claude-sonnet-4-20250514"),
            headless=data.get("headless", True),
            screenshot_on_error=data.get("screenshot_on_error", True),
            output_dir=data.get("output_dir", "./argus-reports"),
            viewport_width=data.get("viewport_width", 1280),
            viewport_height=data.get("viewport_height", 720),
        )

    @classmethod
    def from_args(cls, url: str, focus: Optional[List[str]] = None, **kwargs) -> Config:
        focus_areas = []
        if focus:
            for f in focus:
                focus_areas.append(FocusArea(name=f, description=f))
        return cls(url=url, focus_areas=focus_areas, **kwargs)
