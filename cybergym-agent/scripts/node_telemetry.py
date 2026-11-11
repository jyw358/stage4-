#!/usr/bin/env python3
"""node_telemetry.py -- per-task observables for the seven-node funnel (K1-K7).

The funnel (docs/SEVEN_NODE_MODEL.md):

    K1 locate crash site -> K2 construct input -> K3 local verification
    -> K4 submit decision -> K5 fix side not triggered
    -> K6 fix side over-triggered (non-specific) -> K7 verdict trustworthy

This module only MEASURES.  It never changes a verdict, a budget or a
submission.  Every node reads an existing artifact; the two artifacts added
for K1/K4 (``crashpoint.json`` and ``.check_log.jsonl``) are written by the
agent-side check.py and carry no scoring semantics.

Data sources
    K1  crashpoint.json (declared site), description.txt (information density)
    K2  workspace files (poc.bin / poc_gen.py) + session transcripts
    K3  .oracle.log (schema-2 local oracle telemetry) + tool calls
    K4  .checks, submissions.jsonl, .check_log.jsonl, transcripts
    K5  server final record -> row.final.fix_exit
    K6  server final record -> row.final.{vul_exit,fix_exit}
    K7  scoring evidence -> row.score_status + row.final.hash_check

Usage
    # task mode: compute one workspace, optionally merge into its row
    python3 node_telemetry.py task --ws /path/ws [--out OUT --task-id arvo:1]

    # report mode: aggregate rows/ into the funnel table (audit side)
    python3 node_telemetry.py report --out OUT [--answer-key KEY.json]

Answer-key matching (K1 accuracy) is REPORT-ONLY: it never writes to a row
and must not be used during a blind scoring run.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = 2
CHECKS_BUDGET_DEFAULT = 25

TOOLS = ("repo_map.py", "reach_graph.py", "probe_path.sh", "try_local.sh",
         "stab_run.sh", "minsep.py", "directed_fuzz.py", "boundary_synth.py",
         "mutate_seed.py", "hf_mutator.py")
# accept `python3 check.py`, `python3 -u /abs/path/check.py`, `./check.py`
STRICT_CHECK_RE = re.compile(
    r"python3?\s+(?:-u\s+)?(?:[A-Za-z0-9_./-]*/)?check\.py\b"
    r"|(?:^|[;&|]\s*)\./check\.py\b")
FILE_LINE_RE = re.compile(r"([A-Za-z0-9_./+-]+\.[ch](?:pp|xx)?)[:\s]+(\d{1,6})")
FUNC_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
ORACLE_CRASH = ("FATAL", "NONFATAL")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_key(value) -> str:
    """Comparable timestamp prefix (all stage-4 logs are UTC, second-level)."""
    return str(value or "")[:19]


def _load_jsonl(path: Path) -> list:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sha256(path: Path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _norm_func(name) -> str:
    """Normalise a frame/function name for comparison (args and noise removed)."""
    s = str(name or "").strip().lower()
    s = s.split("(")[0]
    s = re.sub(r"[^a-z0-9_].*$", "", s)
    return s


# --------------------------------------------------------------------------
# K1 -- locate crash site
# --------------------------------------------------------------------------
def read_declared_crashpoint(ws: Path) -> dict:
    """Structured declaration first, SOLUTION.md fallback second.

    Two different questions are kept separate (r-review P1):
    ``declaration_present`` -- did the agent state anything at all;
    ``site_identifiable``   -- does the statement name a concrete site
    (a function, or a file:line pair).  A JSON blob with only ``mechanism``
    or ``evidence`` is a declaration but NOT an identifiable site.

    The SOLUTION.md fallback is heuristic and is always labelled
    ``source=fallback`` so it never mixes with structured declarations in
    the audit statistic.
    """
    out = {"declaration_present": False, "site_identifiable": False,
           "source": "none", "site": None, "declared_at": None}
    cp = ws / "crashpoint.json"
    if cp.is_file():
        data = _load_json(cp)
        out["declaration_present"] = True
        try:
            out["declared_at"] = datetime.fromtimestamp(
                cp.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            pass
        if isinstance(data, dict):
            site = {k: data.get(k) for k in ("file", "func", "line",
                                             "mechanism", "evidence",
                                             "anchor", "anchor_func")
                    if data.get(k) not in (None, "")}
            out.update(source="structured", site=site or None,
                       site_identifiable=bool(data.get("func") or
                                              (data.get("file") and
                                               data.get("line"))))
        else:
            out["source"] = "structured_unparsed"
        return out
    sol = ws / "SOLUTION.md"
    if sol.is_file():
        try:
            text = sol.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            text = ""
        if not text.strip():
            return out
        out["declaration_present"] = True
        out["source"] = "fallback"
        m_file = FILE_LINE_RE.search(text)
        m_func = FUNC_RE.search(text)
        site = {}
        if m_file:
            site["file"] = m_file.group(1)
            site["line"] = int(m_file.group(2))
        if not m_func and m_file:
            # free-text fallback: the identifier right after file:line
            tail = text[m_file.end():m_file.end() + 60]
            m_fb = re.search(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", tail)
            if m_fb and m_fb.group(1).lower() not in (
                    "at", "in", "the", "via", "function", "line", "path"):
                m_func = m_fb
        if m_func:
            site["func"] = m_func.group(1)
        out["site"] = site or None
        out["site_identifiable"] = bool(site.get("func") or
                                        (site.get("file") and site.get("line")))
    return out


def description_features(ws: Path) -> dict:
    """How much locating information the task statement actually provides."""
    p = ws / "description.txt"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"chars": None, "has_func": None, "has_file": None}
    return {"chars": len(text.strip()),
            "has_func": bool(FUNC_RE.search(text)),
            "has_file": bool(FILE_LINE_RE.search(text) or
                            re.search(r"\.[ch](?:pp|xx)?\b", text))}


def node_k1(ws: Path) -> dict:
    cp = read_declared_crashpoint(ws)
    if not cp["declaration_present"]:
        state = "missing"
    elif not cp["site_identifiable"]:
        state = "not_identifiable"
    else:
        state = "declared"
    return {
        **cp,
        "state": state,          # audit adds audit_match/audit_mismatch/not_auditable
        "observed": cp["declaration_present"],
        "description": description_features(ws),
    }


# --------------------------------------------------------------------------
# transcripts / generic artifacts
# --------------------------------------------------------------------------
def _sessions(ws: Path) -> list:
    files = []
    for f in glob.glob(str(ws / ".claude-config" / "projects" / "*" / "*.jsonl")):
        try:
            files.append((os.path.getmtime(f), f))
        except OSError:
            continue
    return [f for _, f in sorted(files)]


def scan_transcripts(ws: Path) -> dict:
    """One pass over the session transcripts: turns, tool calls, writes."""
    st = {"sessions": 0, "assistant_msgs": 0, "tool_calls": {},
          "first_construct_turn": None, "first_check_turn": None,
          "gen_writes": 0, "poc_writes": 0, "solution_writes": 0,
          "crashpoint_writes": 0, "strict_checks": 0, "event_ts": []}
    for path in _sessions(ws):
        st["sessions"] += 1
        turn = 0
        for d in _load_jsonl(Path(path)):
            if d.get("type") != "assistant":
                continue
            turn += 1
            st["assistant_msgs"] += 1
            ts = str(d.get("timestamp", ""))[:19]
            if ts:
                st["event_ts"].append(ts)
            for c in (d.get("message", {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input") or {}
                if name in ("Write", "Edit", "MultiEdit"):
                    fp = str(inp.get("file_path") or inp.get("path") or "")
                    base = os.path.basename(fp)
                    if base == "poc_gen.py":
                        st["gen_writes"] += 1
                        if st["first_construct_turn"] is None:
                            st["first_construct_turn"] = turn
                    elif base in ("poc.bin", "poc"):
                        st["poc_writes"] += 1
                        if st["first_construct_turn"] is None:
                            st["first_construct_turn"] = turn
                    elif base == "SOLUTION.md":
                        st["solution_writes"] += 1
                    elif base == "crashpoint.json":
                        st["crashpoint_writes"] += 1
                if name != "Bash":
                    continue
                cmd = str(inp.get("command", ""))
                for t in TOOLS:
                    if t in cmd:
                        st["tool_calls"][t] = st["tool_calls"].get(t, 0) + 1
                if STRICT_CHECK_RE.search(cmd):
                    st["strict_checks"] += 1
                    if st["first_check_turn"] is None:
                        st["first_check_turn"] = turn
    return st


def oracle_metrics(ws: Path) -> dict:
    """.oracle.log: requests, attempts, confirmed executions are DIFFERENT counts.

    ``validation_attempts`` includes every execution attempt, successful or
    not, so ``attempt_valid_rate = execution_confirmed / validation_attempts``
    can be < 1 (this is the whole point of the P0-6 lesson: a docker-permission
    failure must show up as an invalid attempt, never vanish from the ratio).
    ``tag=sweep`` records are counted separately and are not validation
    requests.  Schema-1 legacy rows cannot prove execution: they land in
    ``legacy_unverified``, never in the confirmed counts.
    """
    path = ws / ".oracle.log"
    st = {"requests_total": 0, "sweep_events": 0, "validation_attempts": 0,
          "execution_confirmed": 0, "local_crashes_confirmed": 0,
          "clean_confirmed": 0, "oracle_errors": 0, "input_errors": 0,
          "legacy_unverified": 0, "first_crash_ts": None,
          "refused": 0, "refusal_kinds": {}, "broker_request_ids": [],
          "crashed_input_sha256": [], "distinct_local_inputs": 0,
          "recent_input_sha256": []}
    seen = []
    for d in _load_jsonl(path):
        st["requests_total"] += 1
        if d.get("tag") == "sweep":
            # housekeeping, not a validation request: count it and STOP, so it
            # can never become an execution/legacy-unverified sample (P1-B).
            st["sweep_events"] += 1
            continue
        v = d.get("verdict")
        ts = d.get("ts")
        hashes = [str(h) for h in (d.get("input_sha256") or []) if h]
        for h in hashes:
            if h not in seen:
                seen.append(h)
        if v in ("NO_INPUT", "INPUT_ERROR", "FIX_SIDE_BLOCKED"):
            st["input_errors"] += 1
            st["oracle_errors"] += 1
            continue
        kind = d.get("runner_error_kind")
        if d.get("request_id"):
            st["broker_request_ids"].append(str(d["request_id"]))
        if v == "ORACLE_ERROR" and kind in ("broker_refused", "oracle_busy"):
            # A policy/concurrency refusal is visible, but it is not an
            # execution-quality sample: no container was ever requested to run.
            st["refused"] += 1
            st["refusal_kinds"][kind] = st["refusal_kinds"].get(kind, 0) + 1
            continue
        # An explicit ORACLE_ERROR is a real attempt that failed before the
        # sentinel existed (sweep preflight, docker missing).  Those records
        # carry no runner_started, so without this branch a systematically
        # broken local oracle would be filed as "legacy, unverifiable" and the
        # failed tries would disappear from attempt_valid_rate -- the P0-6
        # lesson again, one layer up.
        if d.get("schema") == 2 or "runner_started" in d or v == "ORACLE_ERROR":
            st["validation_attempts"] += 1
            if (d.get("runner_started") and d.get("target_started")
                    and v in ("CLEAN", "FATAL", "NONFATAL", "TIMEOUT")):
                st["execution_confirmed"] += 1
                if v == "CLEAN":
                    st["clean_confirmed"] += 1
                elif v in ORACLE_CRASH:
                    st["local_crashes_confirmed"] += 1
                    if st["first_crash_ts"] is None:
                        st["first_crash_ts"] = ts
                    for h in hashes:
                        if h not in st["crashed_input_sha256"]:
                            st["crashed_input_sha256"].append(h)
            else:
                st["oracle_errors"] += 1
        else:
            st["legacy_unverified"] += 1
    st["execution_unconfirmed"] = st["validation_attempts"] - st["execution_confirmed"]
    st["attempt_valid_rate"] = (
        round(st["execution_confirmed"] / st["validation_attempts"], 3)
        if st["validation_attempts"] else None)
    st["crashed_input_sha256"] = st["crashed_input_sha256"][-5:]
    st["distinct_local_inputs"] = len(seen)
    st["recent_input_sha256"] = seen[-20:]
    st["broker_request_ids"] = st["broker_request_ids"][-20:]
    return st


def check_log_metrics(ws: Path) -> dict:
    """.check_log.jsonl: one record per official check.py run (K4 source)."""
    st = {"checks": 0, "first_ts": None, "last": None,
          "distinct_check_inputs": 0, "recent_inputs": [],
          "declared_crashpoint_seen": False,
          "local_crashes_at_submit": None}
    hashes = []
    for d in _load_jsonl(ws / ".check_log.jsonl"):
        st["checks"] += 1
        if st["first_ts"] is None:
            st["first_ts"] = d.get("ts")
        st["last"] = {"seq": d.get("seq"), "ts": d.get("ts"),
                      "verdict": d.get("verdict"),
                      "vul_exit": d.get("vul_exit"),
                      "sha256": d.get("sha256"), "size": d.get("size")}
        h = d.get("sha256")
        if h and h not in hashes:
            hashes.append(h)
        if d.get("declared_crashpoint"):
            st["declared_crashpoint_seen"] = True
        if d.get("local_crashes_so_far") is not None:
            st["local_crashes_at_submit"] = d.get("local_crashes_so_far")
    st["distinct_check_inputs"] = len(hashes)
    st["recent_inputs"] = hashes[-10:]
    return st


def ledger_metrics(ws: Path) -> dict:
    entries = []
    path = Path(os.environ.get("CG_SUBMISSION_LEDGER") or
                (ws / "submissions.jsonl"))
    for d in _load_jsonl(path):
        entries.append(d)
    return {"entries": len(entries),
            "first_ts": entries[0].get("ts") if entries else None,
            "last": entries[-1] if entries else None}


def _node_k2(ws: Path, tr: dict, orc: dict, chk: dict, led: dict) -> dict:
    """A generator file is NOT a constructed input; keep the layers separate."""
    poc = ws / "poc.bin"
    gen = ws / "poc_gen.py"
    generator_written = gen.is_file() or tr["gen_writes"] > 0
    poc_present = poc.is_file()
    candidate_submitted = led["entries"] > 0
    if candidate_submitted:
        state = "candidate_submitted"
    elif poc_present:
        state = "candidate_exists"
    elif generator_written:
        state = "generator_only"
    else:
        state = "no_artifact"
    return {
        "state": state,
        "observed": state != "no_artifact",
        "source": "workspace+transcripts+ledger",
        "generator_written": generator_written,
        "poc_file_present": poc_present,
        "candidate_submitted": candidate_submitted,
        "poc_bytes": poc.stat().st_size if poc_present else None,
        "poc_sha256": _sha256(poc) if poc_present else None,
        "gen_writes": tr["gen_writes"],
        "poc_writes": tr["poc_writes"],
        "first_construct_turn": tr["first_construct_turn"],
        # exact totals; the recent-* lists are display-only and truncated
        "distinct_local_inputs": orc["distinct_local_inputs"],
        "distinct_check_inputs": chk["distinct_check_inputs"],
        "recent_local_input_sha256": orc["recent_input_sha256"],
        "recent_check_input_sha256": chk["recent_inputs"],
    }


def _node_k3(orc: dict, tr: dict) -> dict:
    if orc["execution_confirmed"] > 0:
        state = "execution_confirmed"
    elif orc["validation_attempts"] > 0:
        state = "oracle_error"
    elif orc["legacy_unverified"] > 0:
        state = "execution_unknown"
    elif orc["refused"] > 0:
        state = "oracle_refused"
    else:
        state = "not_attempted"
    return {
        "state": state,
        "observed": orc["requests_total"] > 0,
        "source": ".oracle.log+transcripts",
        "requests_total": orc["requests_total"],
        "sweep_events": orc["sweep_events"],
        "validation_attempts": orc["validation_attempts"],
        "execution_confirmed": orc["execution_confirmed"],
        "execution_unconfirmed": orc["execution_unconfirmed"],
        "local_crashes_confirmed": orc["local_crashes_confirmed"],
        "clean_confirmed": orc["clean_confirmed"],
        "oracle_errors": orc["oracle_errors"],
        "input_errors": orc["input_errors"],
        "legacy_unverified": orc["legacy_unverified"],
        "refused": orc["refused"],
        "refusal_kinds": orc["refusal_kinds"],
        "broker_request_ids": orc["broker_request_ids"],
        "attempt_valid_rate": orc["attempt_valid_rate"],
        "first_local_crash_ts": orc["first_crash_ts"],
        "crashed_input_sha256": orc["crashed_input_sha256"],
        "tool_calls": tr["tool_calls"],
        "stability_runs": tr["tool_calls"].get("stab_run.sh", 0),
    }


def _node_k4(row: dict, ws: Path, tr: dict, orc: dict, chk: dict,
             led: dict) -> dict:
    checks_used = 0
    try:
        checks_used = int((ws / ".checks").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    # Three different layers (r21 / review P0-2).  `.checks` is incremented
    # BEFORE runner.run(), so a network failure consumes budget without any
    # accepted submission.  Only the protected ledger counts as accepted.
    intent = tr["strict_checks"] > 0
    attempted = checks_used > 0
    accepted = led["entries"] > 0
    if accepted:
        state = "accepted"
    elif attempted:
        state = "attempted"
    elif intent:
        state = "rejected_or_unknown"
    else:
        state = "no_intent"
    first_check_ts = chk["first_ts"] or led["first_ts"]
    crash_ts = orc["first_crash_ts"]
    before = None
    if first_check_ts and crash_ts:
        before = _ts_key(crash_ts) <= _ts_key(first_check_ts)
    vul_exit = (chk["last"] or {}).get("vul_exit")
    return {
        "state": state,
        "observed": intent or attempted or accepted,
        "source": "transcripts+.checks+submissions.jsonl",
        "check_intent": intent,
        "check_intent_count": tr["strict_checks"],
        "check_attempted": attempted,
        "checks_used": checks_used,
        "checks_budget": int(os.environ.get("CG_CHECKS_BUDGET")
                             or CHECKS_BUDGET_DEFAULT),
        "submission_accepted": accepted,
        # ledger-level acceptance claim; the root-side hash/run binding is
        # K7's job.  Must be False when nothing was accepted (review P1-A).
        "accepted_provisional": accepted,
        "submission_count": led["entries"],
        "first_check_turn": tr["first_check_turn"],
        "first_check_ts": first_check_ts,
        "local_crash_before_first_check": before,
        "local_crashes_at_submit": chk["local_crashes_at_submit"],
        # auxiliary telemetry only: check.py hashes the PoC path AFTER the
        # upload returns, so this is NOT proof of the uploaded bytes.
        "check_input_sha256_unverified": chk["recent_inputs"],
        "sha_source": "post_submit_file_read",
        "vul_exit_client": vul_exit if vul_exit is not None
        else (row.get("final") or {}).get("vul_exit"),
        "vul_triggered_client": (None if vul_exit is None
                                 else vul_exit not in (0, 300)),
    }


# --------------------------------------------------------------------------
# K5 / K6 / K7 -- server-side outcome (available only after scoring)
# --------------------------------------------------------------------------
def score_side_nodes(final, score_status=None, score_reason=None) -> dict:
    """Derive K5/K6/K7 from the authoritative final record.

    ``final.fix_exit`` is the ONLY fix-side signal stage-4 ever receives; it
    arrives through the root-side scorer after the run (never to the agent).
    """
    final = final or {}
    fix = final.get("fix_exit")
    vul = final.get("vul_exit")
    fix_observed = fix is not None
    if not fix_observed:
        k5 = {"fix_observed": False, "fix_exit": None, "fix_clean": None,
              "state": "not_observed", "source": "server final record"}
        k6 = {"fix_nonclean_observed": None, "both_sides_nonclean": None,
              "fix_exit_class": "unknown", "non_specific_confirmed": None,
              "state": "not_auditable", "source": "server final record"}
    else:
        if fix == 0:
            cls, k5_state = "clean", "fix_clean"
        elif fix == 300:
            cls, k5_state = "timeout", "timeout"
        else:
            # a nonzero exit is an abnormal termination, NOT proof of a memory
            # error; only the audit side may upgrade this to "confirmed".
            cls, k5_state = "nonzero_exit", "fix_nonclean"
        k5 = {"fix_observed": True, "fix_exit": fix, "fix_clean": fix == 0,
              "state": k5_state, "source": "server final record"}
        k6 = {
            "fix_nonclean_observed": fix != 0,
            "both_sides_nonclean": (vul not in (None, 0, 300)
                                    and fix not in (0, 300)),
            "fix_exit_class": cls,
            # mechanical exit codes can never confirm a non-specific trigger
            "non_specific_confirmed": None,   # audit-side only
            "state": ("no_non_specific_evidence" if fix in (0, 300)
                      else "suspected"),
            "source": "server final record",
        }
    # K7 inherits the OFFICIAL evidence verdict; it never re-derives trust
    # from a weaker subset of fields.  Contract (plugins/evidence.py):
    # score_status == "ok" is only reachable after the created_at ordering,
    # protected-ledger and final-hash binding checks have all passed --
    # mismatch/unverified short-circuit to their own statuses first.
    status = str(score_status or "")
    if status == "ok":
        trusted, k7_state = True, "trusted"
    elif status in ("", "fix_pending"):
        trusted, k7_state = None, "pending"
    else:
        trusted, k7_state = False, "untrusted"
    hash_check = final.get("hash_check")
    reasons = []
    if status != "ok" and score_status:
        reasons.append(f"score_status={score_status}")
    if hash_check not in (None, "match"):
        reasons.append(f"hash_check={hash_check}")
    if score_reason and not trusted:
        reasons.append(str(score_reason)[:120])
    k7 = {"trusted": trusted, "state": k7_state,
          "source": "official_scorer",
          "contract": "score_status=ok implies created_at order + protected "
                      "ledger + final hash binding all passed",
          "score_status": score_status, "hash_check": hash_check,
          "reasons": reasons, "score_reason": score_reason}
    return {"k5": k5, "k6": k6, "k7": k7}


def build_task_nodes(ws, row=None) -> dict:
    """Full per-task node block; K5-K7 stay null until scoring."""
    ws = Path(ws)
    row = row or {}
    tr = scan_transcripts(ws)
    orc = oracle_metrics(ws)
    chk = check_log_metrics(ws)
    led = ledger_metrics(ws)
    nodes = {
        "schema": SCHEMA,
        "generated_at": _utcnow(),
        "workspace": str(ws),
        "k1": node_k1(ws),
        "k2": _node_k2(ws, tr, orc, chk, led),
        "k3": _node_k3(orc, tr),
        "k4": _node_k4(row, ws, tr, orc, chk, led),
        "k5": {"fix_observed": False, "fix_exit": None, "fix_clean": None,
               "state": "not_observed", "source": "server final record"},
        "k6": {"fix_nonclean_observed": None, "both_sides_nonclean": None,
               "fix_exit_class": "unknown", "non_specific_confirmed": None,
               "state": "not_auditable", "source": "server final record"},
        "k7": {"trusted": None, "state": "pending",
               "source": "official_scorer", "score_status": None,
               "hash_check": None, "reasons": []},
        "session": {"sessions": tr["sessions"],
                    "assistant_msgs": tr["assistant_msgs"],
                    "solution_writes": tr["solution_writes"],
                    "crashpoint_writes": tr["crashpoint_writes"]},
    }
    if row.get("final") or row.get("score_status"):
        nodes.update(score_side_nodes(row.get("final"),
                                      row.get("score_status"),
                                      row.get("score_reason")))
    return nodes


def merge_score_nodes(existing: dict, final, score_status,
                      score_reason=None) -> dict:
    nodes = dict(existing or {})
    nodes.setdefault("schema", SCHEMA)
    nodes.update(score_side_nodes(final, score_status, score_reason))
    return nodes


# --------------------------------------------------------------------------
# report mode
# --------------------------------------------------------------------------
def _rate(num, den):
    return round(num / den, 4) if den else None


def _answer_key_index(path):
    data = _load_json(Path(path))
    if not isinstance(data, list):
        return {}
    return {str(d.get("task_id")): d for d in data if isinstance(d, dict)}


def _split_sym(name):
    """Split ``Scope::func(args)`` into (base, args-or-None).

    Args are kept apart instead of being thrown away: ``Foo::bar(int)`` and
    ``Foo::bar(double)`` must stay distinguishable (overloads), while a bare
    ``SortByPTypeProcess::Execute`` has to match
    ``Assimp::SortByPTypeProcess::Execute(aiScene*)`` -- the declaration just
    leaves the argument list out.
    """
    s = re.sub(r"<[^<>]*>", "", str(name or "").strip())
    if "(" in s:
        # ASAN frames are often truncated at the argument list
        # (`phase_one_flat_field(int,`), so never require a closing paren.
        base, args = s.split("(", 1)
        base, args = base.strip(), args.strip().rstrip(")").strip()
    else:
        base, args = s, None
    parts = base.split()
    if parts:
        base = parts[-1]              # drop a leading return type
    return base, args


def _scope_suffix_match(declared_base: str, frame_base: str) -> bool:
    """``Foo::bar`` matches ``Baz::Foo::bar``; ``bar`` matches ``Foo::bar``.

    ASAN frames are frequently unqualified even when the source function is a
    method (``phase_one_flat_field(int,`` for ``LibRaw::phase_one_flat_field``),
    so the comparison has to work in both directions.  Equality of the last
    segment is only used when at most one side is scoped -- two different
    scopes with the same method name stay ambiguous and are audited instead.
    """
    if not declared_base:
        return False
    if declared_base == frame_base:
        return True
    d_scoped, f_scoped = "::" in declared_base, "::" in frame_base
    if d_scoped and f_scoped:
        return (frame_base.endswith("::" + declared_base)
                or declared_base.endswith("::" + frame_base))
    return declared_base.split("::")[-1] == frame_base.split("::")[-1]


def _k1_match(nodes: dict, key_entry):
    """Report-only crash-site match against the official crash frames.

    Comparison is per-symbol, not substring: ``parse`` never matches
    ``parse_header``.  Scope-aware suffix matching is allowed because the
    declaration often omits the namespace; an overload (same qualified name,
    different explicit argument list) or several equally good candidates is
    ``needs_audit`` -- never force-matched.  Tasks without answer-key coverage
    are ``not_auditable`` and never mixed into ``no_match``.
    """
    if not key_entry:
        return "not_auditable"
    site = (nodes.get("k1") or {}).get("site") or {}
    raw_declared = str(site.get("func") or "").strip()
    if not raw_declared:
        return "no_declaration"
    raw_top = str(key_entry.get("crash_top_frame") or "").strip()
    raw_frames = [str(f).strip() for f in (key_entry.get("crash_funcs") or [])]
    if not raw_top and not raw_frames:
        return "not_auditable"

    d_base, d_args = _split_sym(raw_declared)

    def classify(raw):
        b, a = _split_sym(raw)
        if not _scope_suffix_match(d_base, b):
            return None
        if d_args is not None and a is not None and d_args != a:
            return "overload"        # same name, explicitly different args
        return b

    top_b = classify(raw_top) if raw_top else None
    frame_bs = [classify(f) for f in raw_frames]
    if top_b == "overload" or "overload" in frame_bs:
        return "needs_audit"
    hits = {b for b in ([top_b] + frame_bs) if b}
    if len(hits) > 1:
        return "needs_audit"          # several scopes fit the bare declaration
    if top_b:
        return "top_frame"
    if hits:
        return "in_crash_frames"
    if "<" in raw_declared or "operator" in raw_declared:
        return "needs_audit"
    return "no_match"


# metric name -> (node, field, predicate).  ``None`` always means UNOBSERVED,
# never failure; the report keeps observed/missing/eligible separate.
def _bool_metric(node, field):
    return (node, field, lambda v: v if isinstance(v, bool) else None)


def _positive_int_metric(node, field):
    return (node, field,
            lambda v: (v > 0) if isinstance(v, int) and not isinstance(v, bool)
            else None)


REPORT_METRICS = {
    "k1_declaration_present": _bool_metric("k1", "declaration_present"),
    "k1_site_identifiable": _bool_metric("k1", "site_identifiable"),
    "k2_generator_written": _bool_metric("k2", "generator_written"),
    "k2_poc_file_present": _bool_metric("k2", "poc_file_present"),
    "k2_candidate_submitted": _bool_metric("k2", "candidate_submitted"),
    "k3_validation_attempted": _positive_int_metric("k3", "validation_attempts"),
    "k3_execution_confirmed": _positive_int_metric("k3", "execution_confirmed"),
    "k3_local_crash_confirmed": _positive_int_metric("k3", "local_crashes_confirmed"),
    "k4_check_intent": _bool_metric("k4", "check_intent"),
    "k4_check_attempted": _bool_metric("k4", "check_attempted"),
    "k4_submission_accepted": _bool_metric("k4", "submission_accepted"),
    "k5_fix_clean": _bool_metric("k5", "fix_clean"),
    "k6_fix_nonclean_observed": _bool_metric("k6", "fix_nonclean_observed"),
    "k6_non_specific_suspected": ("k6", "state",
                                  lambda v: (v == "suspected")
                                  if isinstance(v, str) else None),
    "k7_trusted": _bool_metric("k7", "trusted"),
}


def _load_queue(path):
    """Frozen preregistered task universe: JSON list or one task id per line."""
    # utf-8-sig: Windows-generated queue files often start with a BOM, which
    # would otherwise become part of the first task id and look like a missing
    # row instead of a matching task.
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(t.get("task_id") if isinstance(t, dict) else t)
                    for t in data]
    except json.JSONDecodeError:
        pass
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


def build_report(out_dir, answer_key=None, queue=None) -> dict:
    """Aggregate rows/.  Every metric reports eligible/observed/missing.

    ``eligible_n`` is the FULL preregistered task set (``--queue``) or, as a
    fallback for exploratory runs, the rows present.  Environment failures are
    never silently removed from a primary denominator.  Tasks without a row,
    without nodes, or with a LEGACY telemetry schema stay in ``eligible_n``
    but are NOT observed: same-named fields from another schema are not the
    same semantics (review P1-C).  A 1/1 observed ratio over 2 tasks is
    reported as rate_among_observed=1.0 AND rate_over_all_eligible=0.5.
    """
    rows_by_id = {}
    for p in sorted((Path(out_dir) / "rows").glob("*.json")):
        row = _load_json(p)
        if isinstance(row, dict) and row.get("task_id"):
            rows_by_id[str(row["task_id"])] = row
    task_ids = list(queue) if queue else sorted(rows_by_id)
    key = _answer_key_index(answer_key) if answer_key else {}
    rep = {"tasks": len(rows_by_id), "eligible_n": len(task_ids),
           "eligible_source": "queue" if queue else "rows",
           "no_row_n": 0, "out_of_queue_n": 0, "with_nodes": 0,
           "missing_nodes": 0, "legacy_schema_n": 0,
           "nodes": {}, "states": {}, "k1_accuracy": None,
           "telemetry_schema": SCHEMA}
    if queue:
        qset = set(task_ids)
        rep["out_of_queue_n"] = sum(1 for t in rows_by_id if t not in qset)
    observed = []          # (task_id, nodes) with the CURRENT schema only
    k1_match = {}
    for tid in task_ids:
        row = rows_by_id.get(tid)
        if row is None:
            rep["no_row_n"] += 1
            continue
        nodes = row.get("nodes") or {}
        if not nodes:
            rep["missing_nodes"] += 1
            continue
        if nodes.get("schema") != SCHEMA:
            rep["legacy_schema_n"] += 1
            continue                       # unknown for schema-2 metrics
        rep["with_nodes"] += 1
        observed.append((tid, nodes))
        for kn in ("k1", "k2", "k3", "k4", "k5", "k6", "k7"):
            state = ((nodes.get(kn) or {}).get("state")) or "unstated"
            rep["states"].setdefault(kn, {})
            rep["states"][kn][state] = rep["states"][kn].get(state, 0) + 1
        if key:
            m = _k1_match(nodes, key.get(tid))
            k1_match[m] = k1_match.get(m, 0) + 1
    rep["k1_accuracy"] = k1_match or None
    eligible = len(task_ids)
    for name, (kn, field, pred) in REPORT_METRICS.items():
        obs = positive = 0
        for _tid, nodes in observed:
            val = pred((nodes.get(kn) or {}).get(field))
            if val is None:
                continue
            obs += 1
            if val is True:
                positive += 1
        rep["nodes"][name] = {
            "eligible_n": eligible,
            "observed_n": obs,
            "positive_n": positive,
            "missing_n": eligible - obs,
            "rate_among_observed": _rate(positive, obs),
            # all-missing is UNDEFINED, never 0%: an unmeasured task is not a
            # negative example (same rule as rate_among_observed).
            "rate_over_all_eligible": (_rate(positive, eligible)
                                       if obs else None),
        }
    return rep


def print_report(rep: dict) -> None:
    print(f"[nodes] rows={rep['tasks']} eligible={rep['eligible_n']}"
          f"({rep['eligible_source']}) with_nodes={rep['with_nodes']} "
          f"missing_nodes={rep['missing_nodes']} no_row={rep['no_row_n']} "
          f"legacy_schema={rep['legacy_schema_n']} "
          f"out_of_queue={rep['out_of_queue_n']} schema={rep['telemetry_schema']}")
    for k, v in rep["nodes"].items():
        print(f"  {k:<28} pos={v['positive_n']} observed={v['observed_n']} "
              f"eligible={v['eligible_n']} missing={v['missing_n']} "
              f"rate_obs={v['rate_among_observed']} "
              f"rate_all={v['rate_over_all_eligible']}")
    for kn, states in rep["states"].items():
        print(f"  {kn} states: {states}")
    if rep.get("k1_accuracy"):
        print(f"  k1_site_match            {rep['k1_accuracy']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("task", help="compute one workspace")
    t.add_argument("--ws", required=True)
    t.add_argument("--out", default="")
    t.add_argument("--task-id", default="")
    t.add_argument("--json", action="store_true")

    r = sub.add_parser("report", help="aggregate rows/ into the K1-K7 funnel")
    r.add_argument("--out", required=True)
    r.add_argument("--answer-key", default="")
    # frozen preregistered queue: the eligible universe for a formal A/B run
    r.add_argument("--queue", default="")
    r.add_argument("--json", action="store_true")

    args = ap.parse_args()
    if args.cmd == "task":
        if args.out and args.task_id:
            from plugins import results_store
            row = results_store.read_row(args.out, args.task_id) or {}
            node = build_task_nodes(Path(args.ws), row)
            results_store.merge_row(args.out, args.task_id, {"nodes": node})
        else:
            node = build_task_nodes(Path(args.ws))
        if args.json:
            json.dump(node, sys.stdout, indent=1, ensure_ascii=False)
            print()
        else:
            print(json.dumps(node, ensure_ascii=False))
        return 0

    rep = build_report(args.out, args.answer_key or None,
                       _load_queue(args.queue) if args.queue else None)
    if args.json:
        json.dump(rep, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
