"""diff-verifier plugin: the independent gate. PoC authors never self-certify.

Pipeline per candidate PoC:
  1. build/compile stage (if the PoC needs building)      -> COMPILE_ERROR early exit
  2. differential run: vul image vs fix image             -> decision matrix
  3. stability audit: 3 runs on vul, need >=2 target hits -> else FLAKY
  4. adversarial re-check: one more run on fix            -> catches flaky/overshooting PoCs
"""
import re
from typing import Optional

from agent.contract import Verdict
from plugins.env_manager import RunOutcome

_ASAN_RE = re.compile(r"ERROR: (AddressSanitizer|UndefinedBehaviorSanitizer|MemorySanitizer|ThreadSanitizer): ([\w\-]+)")
_FRAME_RE = re.compile(r"#\d+\s+0x[0-9a-f]+\s+in\s+(\S+)")
_GENERIC_CRASH_RC = {-11, 139}  # SIGSEGV direct / container-mapped
_LIBFUZZER_DEADLY = "ERROR: libFuzzer: deadly signal"
_LIBFUZZER_EXITED = "ERROR: libFuzzer: fuzz target exited"


def parse_signature(outcome: RunOutcome) -> Optional[tuple]:
    """Return (kind, detail) or None for a clean run.

    Kinds:
      <sanitizer type>  e.g. 'heap-buffer-overflow'  -> genuine memory-safety crash
      'deadly-signal'   libFuzzer deadly signal      -> genuine crash
      'target-exited'   fuzz target called exit()    -> business rejection path,
                          NOT counted as a memory-safety crash (fix side may do
                          this legitimately when it rejects malformed input)
    """
    text = outcome.stderr + "\n" + outcome.stdout
    m = _ASAN_RE.search(text)
    if m:
        frames = _FRAME_RE.findall(text)
        return (m.group(2), frames[0] if frames else "")
    if _LIBFUZZER_DEADLY in text:
        frames = _FRAME_RE.findall(text)
        return ("deadly-signal", frames[0] if frames else "")
    if _LIBFUZZER_EXITED in text:
        frames = _FRAME_RE.findall(text)
        return ("target-exited", frames[1] if len(frames) > 1 else "")
    if outcome.returncode in _GENERIC_CRASH_RC:
        return ("SIGSEGV", "")
    return None


def is_memory_crash(sig: Optional[tuple]) -> bool:
    """True only for genuine memory-safety crashes (target-exited excluded)."""
    return sig is not None and sig[0] not in ("target-exited",)


def classify(vul: RunOutcome, fix: RunOutcome,
             expected_type: Optional[str] = None) -> Verdict:
    if vul.stage == "compile" and not vul.ok:
        return Verdict.COMPILE_ERROR
    if vul.timed_out or fix.timed_out:
        return Verdict.TIMEOUT

    v_sig, f_sig = parse_signature(vul), parse_signature(fix)
    v_crash, f_crash = is_memory_crash(v_sig), is_memory_crash(f_sig)
    if v_crash and not f_crash:
        if expected_type and v_sig[0] != expected_type:
            return Verdict.WRONG_BUG
        return Verdict.VERIFIED
    if v_crash and f_crash:
        # both sides memory-crash -> not exploiting this fix
        return Verdict.FIX_ALSO_TRIGGERS if v_sig == f_sig else Verdict.WRONG_BUG
    if not v_crash and v_sig is not None:
        # vul only reached the target-exited path: input rejected, bug not hit
        return Verdict.NO_TRIGGER
    if f_crash and not v_crash:
        return Verdict.INCONCLUSIVE   # fix crashes but vul clean: env suspect
    return Verdict.NO_TRIGGER


def verify_ex(runner, env, poc_path: str,
              expected_type: Optional[str] = None,
              stability_runs: int = 2):
    """Full gate returning (Verdict, evidence dict). The evidence feeds the
    next construct round: the model must see real execution output, not just
    'it did not crash'."""
    vul = _run_vul(runner, env, poc_path, stage_probe=True)
    if vul.stage == "compile" and not vul.ok:
        return Verdict.COMPILE_ERROR, {"vul_output": ""}

    fix = runner.run(env.fix_image, poc_path)
    verdict = classify(vul, fix, expected_type)

    ev = {
        "vul_exit": vul.returncode,
        "fix_exit": fix.returncode,
        "vul_sig": parse_signature(vul),
        "fix_sig": parse_signature(fix),
        "vul_output": (vul.stdout or "")[-1500:],
        "fix_output": (fix.stdout or "")[-800:],
    }
    if verdict != Verdict.VERIFIED:
        return verdict, ev

    # closure-audit: a VERIFIED candidate must reproduce the SAME memory bug,
    # not just "some crash most of the time". Signature agreement across runs
    # (sanitizer kind + top frame) is the adversarial re-check: a PoC that
    # crashes at two different sites is two bugs or an overshoot -> FLAKY.
    base_sig = parse_signature(vul)
    hits = 1
    for _ in range(stability_runs):
        r = _run_vul(runner, env, poc_path, stage_probe=False)
        sig = parse_signature(r)
        if is_memory_crash(sig) and sig == base_sig:
            hits += 1
    ev["stability_hits"] = hits
    if hits < 2:
        return Verdict.FLAKY, ev

    fix2 = runner.run(env.fix_image, poc_path)
    if is_memory_crash(parse_signature(fix2)):
        return Verdict.FIX_ALSO_TRIGGERS, ev
    return Verdict.VERIFIED, ev


def verify(runner, env, poc_path: str,
           expected_type: Optional[str] = None,
           stability_runs: int = 2) -> Verdict:
    """Backward-compatible wrapper (verdict only)."""
    return verify_ex(runner, env, poc_path, expected_type, stability_runs)[0]


def _run_vul(runner, env, poc_path: str, stage_probe: bool) -> RunOutcome:
    out = runner.run(env.vul_image, poc_path)
    if stage_probe:
        # runner reports compile-stage failures through the outcome itself
        out.stage = "compile" if (not out.ok and not out.timed_out and out.returncode is None) else "run"
    return out
