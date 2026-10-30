"""Contract tests require no OpenRouter access or repository scan."""
import math
import pytest
from pydantic import ValidationError
from schemas import FileSelectionResult, MapRequest, MapResponse, RepoMapResult, scope_selection_to_repo

def test_map_request_normalizes_inputs():
    request = MapRequest(root=" /tmp/project/../project ", prompt="  fix auth  ")
    assert request.root == "/tmp/project"
    assert request.prompt == "fix auth"
    assert request.agentic is False
    assert request.boost_factor == 10.0
    assert request.downgrade_factor == 0.1

@pytest.mark.parametrize("bad_root", ["", "  ", "relative/path"])
def test_map_request_rejects_nonabsolute_root(bad_root):
    with pytest.raises(ValidationError):
        MapRequest(root=bad_root, prompt="task")

@pytest.mark.parametrize("bad_prompt", ["", " \n "])
def test_map_request_rejects_empty_prompt(bad_prompt):
    with pytest.raises(ValidationError):
        MapRequest(root="/tmp", prompt=bad_prompt)

@pytest.mark.parametrize("bad_factor", [0, -1, math.inf, math.nan, 101])
def test_map_request_rejects_invalid_boosts(bad_factor):
    with pytest.raises(ValidationError):
        MapRequest(root="/tmp", prompt="task", boost_factor=bad_factor)

@pytest.mark.parametrize("bad_factor", [0, -0.5, 1.1, math.inf, math.nan])
def test_map_request_rejects_invalid_downgrades(bad_factor):
    with pytest.raises(ValidationError):
        MapRequest(root="/tmp", prompt="task", downgrade_factor=bad_factor)

def test_map_request_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        MapRequest(root="/tmp", prompt="task", extra_key="unexpected")

@pytest.mark.parametrize("invalid", ["../secret", "src/../secret", "/etc/passwd", "C:/secret", "", "src//file.py", "src/./file.py"])
def test_selection_rejects_unsafe_paths(invalid):
    with pytest.raises(ValidationError):
        FileSelectionResult(selected_files=[invalid])

def test_selection_deduplicates_and_scopes_to_real_files():
    selection = FileSelectionResult(
        selected_files=["./src\\auth.py", "src/auth.py", "src/ghost.py"],
        downgrade_files=["src/auth.py", "README.md", "not-here.py"],
        context_notes="Keep API contracts aligned.",
    )
    valid = scope_selection_to_repo(selection, ["src/auth.py", "README.md"])
    assert valid.selected_files == ["src/auth.py"]
    assert valid.downgrade_files == ["README.md"]
    assert valid.context_notes == "Keep API contracts aligned."

def test_response_contract_round_trip():
    result = RepoMapResult(
        root="/tmp/project", model_name="gpt-4o-mini", edit_format="diff",
        map_tokens=1024, max_input_tokens=128_000, effective_budget=2048,
        used_git=True, rel_files=["src/auth.py"], mentioned_idents=["auth"],
        mentioned_fnames=["src/auth.py"], repo_map="src/auth.py:\n",
        agentic_steps=[{"title": "Pass 1", "content": "done"}],
    )
    resp = MapResponse.model_validate({**result.model_dump(), "agentic_log": "complete"})
    assert resp.agentic_steps[0].title == "Pass 1"
    assert MapResponse.model_validate_json(resp.model_dump_json()) == resp
