"""LD inspect tool contract: per-tool parameters, no widening.

Incident (recorded separately from the Worker permission P0): a real decision
model addressed a task read as
``{"tool": "get_task_provenance", "object_type": "task", "task_id": ...}``.
``object_type`` belongs to the recall tools, so the coordinator raised
``DecisionPolicyError`` and the Goal stopped -- correctly, because a policy
violation is fail-closed and must never be retried.

The fix is *documentation + regression*, not a relaxed validator:

* the decision schema now states, per tool, which parameters are accepted and
  which are rejected (so a model can read the contract it is held to);
* ``get_task_provenance`` still rejects recall parameters, with a message that
  names the correct parameter.

If someone later "fixes" the confusion by silently accepting ``object_type`` on
task tools, these tests fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ld.decision import (  # noqa: E402
    DecisionPolicyError,
    DecisionValidationError,
    LDDecision,
    _READ_TOOLS,
    _RECALL_OBJECT_TYPES,
    decision_schema,
)

PROJECT_ID = "prj-abc"


def _inspect(parameters: dict, project_id: str | None = PROJECT_ID):
    return LDDecision.from_mapping({
        "action": "inspect",
        "project_id": project_id,
        "reason": "look at the task",
        "evidence_refs": [],
        "parameters": parameters,
    })


# --------------------------------------------------------------------------- #
# schema is honest about the contract
# --------------------------------------------------------------------------- #
def test_schema_documents_every_read_tool():
    contract = decision_schema()["actions"]["inspect"]["tool_contract"]
    assert set(contract) == _READ_TOOLS, (
        "a tool without a documented contract is how the incident happened")


def test_schema_marks_task_tools_as_rejecting_object_type():
    contract = decision_schema()["actions"]["inspect"]["tool_contract"]
    for tool in ("get_task_provenance", "get_task_status"):
        assert "object_type" in contract[tool]["forbids"]
        assert "task_id" in contract[tool]["accepts"]
        assert contract[tool]["requires_project_id"] is True
        assert "object_type/object_id" in contract[tool]["note"]


def test_schema_marks_recall_tools_as_rejecting_task_id():
    contract = decision_schema()["actions"]["inspect"]["tool_contract"]
    for tool in ("get_recall_summary", "get_recall_detail"):
        assert "task_id" in contract[tool]["forbids"]
        assert "object_type" in contract[tool]["accepts"]
        assert set(contract[tool]["accepts"]) - {"tool"} <= (
            {"object_type", "object_id", "limit", "char_budget", "cursor",
             "max_total_nodes", "max_depth", "max_output_bytes"})


def test_schema_and_validator_agree_on_the_object_types():
    contract = decision_schema()["actions"]["inspect"]["tool_contract"]
    declared = set(contract["get_recall_summary"]["accepts"])  # sanity: list exists
    assert "object_type" in declared
    assert _RECALL_OBJECT_TYPES == {
        "project", "goal", "task", "attempt", "session", "evidence",
        "context_entry"}


# --------------------------------------------------------------------------- #
# the incident shape is still refused, with a useful message
# --------------------------------------------------------------------------- #
def test_object_type_on_get_task_provenance_is_a_policy_error():
    with pytest.raises(DecisionPolicyError) as excinfo:
        _inspect({"tool": "get_task_provenance", "task_id": "cp-934341b3f942",
                  "object_type": "task"})
    message = str(excinfo.value)
    assert "recall parameters" in message
    # The message must point at the correct parameter *and* at the contract.
    assert "parameters.task_id" in message
    assert "tool_contract.get_task_provenance" in message


@pytest.mark.parametrize("tool", ["get_task_status", "get_task_provenance"])
@pytest.mark.parametrize("extra", [{"object_type": "task"}, {"object_id": "x"},
                                   {"cursor": "abc"}, {"char_budget": 1024}])
def test_task_tools_reject_every_recall_parameter(tool: str, extra: dict):
    with pytest.raises(DecisionPolicyError):
        _inspect({"tool": tool, "task_id": "cp-1", **extra})


@pytest.mark.parametrize("tool", ["get_task_status", "get_task_provenance"])
def test_task_tools_accept_the_documented_shape(tool: str):
    decision = _inspect({"tool": tool, "task_id": "cp-1"})
    assert decision.parameters == {"tool": tool, "task_id": "cp-1"}


def test_recall_tools_still_reject_task_id():
    with pytest.raises(DecisionPolicyError):
        _inspect({"tool": "get_recall_summary", "object_type": "project",
                  "task_id": "cp-1"})


def test_recall_detail_requires_object_id():
    with pytest.raises(DecisionValidationError):
        _inspect({"tool": "get_recall_detail", "object_type": "task"})
    ok = _inspect({"tool": "get_recall_detail", "object_type": "task",
                   "object_id": "cp-1"})
    assert ok.parameters["object_id"] == "cp-1"


def test_task_tools_still_require_a_project_scope():
    """The scope rule lives in the coordinator; the schema documents it too."""
    from ld.coordinator import LDCoordinator

    decision = _inspect({"tool": "get_task_provenance", "task_id": "cp-1"},
                        project_id=None)
    with pytest.raises(DecisionValidationError):
        LDCoordinator._validate_decision_scope(decision, "prj-selected")
    contract = decision_schema()["actions"]["inspect"]["tool_contract"]
    for tool in ("get_task_status", "get_task_provenance"):
        assert contract[tool]["requires_project_id"] is True


def test_missing_task_id_stays_a_validation_error_not_a_policy_error():
    """A generation defect must remain retryable; only contract abuse is fatal."""
    with pytest.raises(DecisionValidationError) as excinfo:
        _inspect({"tool": "get_task_provenance"})
    assert not isinstance(excinfo.value, DecisionPolicyError)


def test_unknown_tool_is_still_a_policy_error():
    with pytest.raises(DecisionPolicyError):
        _inspect({"tool": "delete_everything"})
