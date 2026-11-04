"""Core contracts: task types, verdicts, dispatch/handoff schemas.

Design rules carried over from the reference framework (NSFOCUS AI-PTS worker.md)
with the four fixes we identified:
  1. closed verdict enum (machine-routable, includes INCONCLUSIVE)
  2. budget is enforced in code, not in prompts
  3. target-side output is data, never instructions
  4. routing reads task state, handoff is display-only
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskType(str, Enum):
    LOCATE = "locate"            # build context map, locate suspicious code slices
    HYPOTHESIZE = "hypothesize"  # produce a falsifiable vulnerability hypothesis
    CONSTRUCT = "construct"      # write/build a PoC candidate for one hypothesis
    FUZZ = "fuzz"                # directed fuzzing with model-crafted seeds (no LLM)
    VERIFY = "verify"            # differential verification (diff-verifier only; workers never self-certify)


class Verdict(str, Enum):
    # worker-side
    DONE = "done"
    BLOCKED = "blocked"              # requires blocker field
    FAILED = "failed"                # infrastructure problem
    INCONCLUSIVE = "inconclusive"    # requires missing field (what evidence is missing)
    # verifier-side (closed taxonomy for retry routing)
    VERIFIED = "verified"
    NO_TRIGGER = "no_trigger"               # vul binary ran clean
    WRONG_BUG = "wrong_bug"                 # crashed, but signature != expected bug class
    FIX_ALSO_TRIGGERS = "fix_also_triggers" # patched binary crashes too
    COMPILE_ERROR = "compile_error"
    TIMEOUT = "timeout"
    FLAKY = "flaky"                         # crash reproduces unstably (audit failed)


class SnapshotNode(str, Enum):
    """The 5 key nodes required by the reliability KPI. Written by code, not by prompts."""
    HYPOTHESIS_FORMED = "hypothesis_formed"
    POC_COMPILED = "poc_compiled"
    FIRST_RUN_DONE = "first_run_done"
    VERIFICATION_PASSED = "verification_passed"
    SUBMITTED = "submitted"


class ContractError(Exception):
    pass


@dataclass
class DispatchInput:
    """One task per dispatch (closed structure; missing field -> BLOCKED, never guessed)."""
    task_id: str
    task_type: TaskType
    task_input: str                       # exactly one item: an id, path, or scope
    hypothesis_id: Optional[str] = None   # CONSTRUCT must reference a committed hypothesis
    budget_hint: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task_id or not self.task_input:
            raise ContractError("task_id and task_input are required")
        if self.task_type == TaskType.CONSTRUCT and not self.hypothesis_id:
            raise ContractError("construct requires hypothesis_id")


@dataclass
class Handoff:
    """Display-only summary. The orchestrator routes on TaskState, never on this."""
    verdict: Verdict
    summary: str
    blocker: Optional[str] = None      # required iff BLOCKED
    missing: Optional[str] = None      # required iff INCONCLUSIVE
    evidence_ids: list = field(default_factory=list)  # real IDs only (assigned at commit)

    def validate(self) -> None:
        if not self.summary:
            raise ContractError("summary is required")
        if self.verdict == Verdict.BLOCKED and not self.blocker:
            raise ContractError("blocked requires blocker")
        if self.verdict == Verdict.INCONCLUSIVE and not self.missing:
            raise ContractError("inconclusive requires missing")
        for eid in self.evidence_ids:
            if not _is_real_id(eid):
                raise ContractError(f"evidence id must be a committed real id, got {eid!r}")


def _is_real_id(eid: str) -> bool:
    """Real IDs look like EVI-012 (3+ digits). Workers only ever hold placeholders (EVI-1)."""
    if not isinstance(eid, str) or "-" not in eid:
        return False
    prefix, _, num = eid.rpartition("-")
    return prefix in ("EVI", "HYP", "ATT") and num.isdigit() and len(num) >= 3
