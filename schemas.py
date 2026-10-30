"""Validated Pydantic v2 contracts shared by the API, mapper, and engine."""
from __future__ import annotations
import os
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MapRequest(BaseModel):
    """Validated input for POST /map."""
    model_config = ConfigDict(extra="forbid")
    root: str = Field(min_length=1, description="Absolute repository root on the server")
    prompt: str = Field(min_length=1, max_length=20_000, description="Coding task")
    agentic: bool = False
    boost_factor: float = Field(default=10.0, gt=0, le=100, allow_inf_nan=False)
    downgrade_factor: float = Field(default=0.1, gt=0, le=1, allow_inf_nan=False)

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        value = value.strip()
        if not value or not os.path.isabs(value):
            raise ValueError("root must be an absolute directory path")
        return os.path.abspath(value)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt must not be empty")
        return value


class AgenticStep(BaseModel):
    """Diagnostic pipeline step presented in the frontend."""
    model_config = ConfigDict(extra="forbid")
    title: str
    content: str
    meta: str | None = None


class RepoMapResult(BaseModel):
    """Shared validated result for deterministic and agentic pipelines."""
    model_config = ConfigDict(extra="forbid")
    root: str
    model_name: str
    edit_format: str
    map_tokens: int = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    effective_budget: int = Field(ge=0)
    used_git: bool
    rel_files: list[str]
    mentioned_idents: list[str]
    mentioned_fnames: list[str]
    repo_map: str
    chat_rel_fnames: list[str] = Field(default_factory=list)
    recency_boost_fnames: list[str] = Field(default_factory=list)
    cochange_boost_fnames: list[str] = Field(default_factory=list)
    dict_boost_fnames: list[str] = Field(default_factory=list)
    agent_boost_fnames: list[str] = Field(default_factory=list)
    agent_downgrade_fnames: list[str] = Field(default_factory=list)
    agentic_steps: list[AgenticStep] = Field(default_factory=list)


class MapResponse(RepoMapResult):
    """HTTP response with run log."""
    agentic_log: str = ""


class FileSelectionResult(BaseModel):
    """LLM output: normalize relative paths and reject traversal."""
    model_config = ConfigDict(extra="ignore")
    selected_files: list[str] = Field(default_factory=list, max_length=100)
    downgrade_files: list[str] = Field(default_factory=list, max_length=100)
    context_notes: str = Field(default="", max_length=20_000)

    @field_validator("selected_files", "downgrade_files")
    @classmethod
    def validate_relative_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for path in values:
            clean = path.strip().replace("\\", "/")
            if clean.startswith("./"):
                clean = clean[2:]
            segments = clean.split("/")
            if (
                not clean or clean.startswith("/")
                or re.match(r"^[A-Za-z]:", clean)
                or any(segment in ("", ".", "..") for segment in segments)
                or "\x00" in clean
            ):
                raise ValueError(f"invalid repository-relative file path: {path!r}")
            if clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized


def scope_selection_to_repo(
    selection: FileSelectionResult, repo_files: list[str]
) -> FileSelectionResult:
    """Keep only discovered files; boost takes precedence over downgrade."""
    allowed = set(repo_files)
    selected = [name for name in selection.selected_files if name in allowed]
    selected_set = set(selected)
    downgraded = [
        name for name in selection.downgrade_files
        if name in allowed and name not in selected_set
    ]
    return FileSelectionResult(
        selected_files=selected, downgrade_files=downgraded,
        context_notes=selection.context_notes,
    )
