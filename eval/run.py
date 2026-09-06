"""The accuracy harness. docs/DESIGN.md section 5.

Three phases, each resumable, because the model provider allows a fixed
token budget a day and a 68-incident run does not fit in one:

  collect   create every catalogue incident in its own namespace, wait for
            each to reach its failure state, and capture the contracts with
            the agent, once, at detection time. Logs are racy; the capture
            is the evidence and it is kept.
  diagnose  post each captured contract to the brain. A DISCARDED verdict
            caused by the provider's daily limit is retried after the wait
            the provider named; anything else is kept as discarded.
  score     join the ledger with the catalogue and report per failure type,
            never aggregated: root cause accuracy, abstention rate and
            correctness, calibration against the ceiling, cost per diagnosis
            and per correct diagnosis, and the section 2.4 predictions scored.

Usage, from the repository root:
  cd brain && uv run python ../eval/run.py collect|diagnose|score [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog import ALL, Incident, by_id, manifest

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "coroner-eval"
BRAIN = os.environ.get("CORONER_BRAIN_URL", "http://127.0.0.1:8000")

# What the pod must look like before its contract is worth capturing: the
# kubelet has given up on it at least this many times, so the evidence has
# the shape a real incident has rather than a first failure.
MIN_RESTARTS = 3
WAIT_FOR_FAILURE_SECONDS = 600


def sh(*args: str, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(args, check=check, capture_output=capture, text=True)
    return result.stdout if capture else ""


# ----------------------------------------------------------------- collect


def pod_state(name: str) -> tuple[str, int, str]:
    raw = sh("kubectl", "get", "pod", "-n", NAMESPACE, name, "-o", "json", check=False)
    if not raw.strip():
        return "", 0, ""
    pod = json.loads(raw)
    statuses = pod.get("status", {}).get("containerStatuses") or []
    if not statuses:
        return pod.get("status", {}).get("phase", ""), 0, ""
    cs = statuses[0]
    waiting = (cs.get("state") or {}).get("waiting") or {}
    last = (cs.get("lastState") or {}).get("terminated") or {}
    return waiting.get("reason", ""), int(cs.get("restartCount", 0)), last.get("reason", "")


def failed_enough(inc: Incident, waiting: str, restarts: int) -> bool:
    if inc.failure_class == "ImagePullBackOff":
        return waiting in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName")
    return restarts >= MIN_RESTARTS and waiting in ("CrashLoopBackOff", "RunContainerError")


def collect(out: Path, batch: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sh("kubectl", "create", "namespace", NAMESPACE, check=False)
    contracts = out / "contracts.jsonl"
    done = (
        {json.loads(line)["pod"]["name"] for line in contracts.open()}
        if contracts.exists()
        else set()
    )
    todo = [i for i in ALL if i.pod_name not in done]
    print(f"{len(done)} captured, {len(todo)} to go", flush=True)

    for start in range(0, len(todo), batch):
        group = todo[start : start + batch]
        docs = [manifest(i, NAMESPACE) for i in group]
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps({"apiVersion": "v1", "kind": "List", "items": docs}),
            text=True,
            check=True,
            capture_output=True,
        )
        print(f"applied {len(group)} incidents", flush=True)

        deadline = time.time() + WAIT_FOR_FAILURE_SECONDS
        pending = {i.pod_name: i for i in group}
        while pending and time.time() < deadline:
            for name, inc in list(pending.items()):
                waiting, restarts, _ = pod_state(name)
                if failed_enough(inc, waiting, restarts):
                    del pending[name]
            time.sleep(5)
        if pending:
            print(f"not failed in time, captured anyway: {sorted(pending)}", flush=True)

        # One scan, all contracts, at detection time.
        agent = ROOT / "agent" / "bin" / "coroner-agent"
        raw = sh(str(agent), "--once", "--compact", "--namespace", NAMESPACE)
        captured = [json.loads(line) for line in raw.splitlines() if line.strip()]
        names = {i.pod_name for i in group}
        with contracts.open("a") as fh:
            for c in captured:
                if c["pod"]["name"] in names:
                    fh.write(json.dumps(c) + "\n")
        print(
            f"captured {sum(1 for c in captured if c['pod']['name'] in names)} contracts",
            flush=True,
        )

        subprocess.run(
            ["kubectl", "delete", "-n", NAMESPACE, "pod", *sorted(names), "--wait=false"],
            check=False,
            capture_output=True,
        )


# ---------------------------------------------------------------- diagnose

RETRY_IN = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", re.IGNORECASE)


def wait_from(reason: str) -> float | None:
    m = RETRY_IN.search(reason)
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))


def diagnose(out: Path, ledger: Path) -> None:
    contracts = [json.loads(line) for line in (out / "contracts.jsonl").open()]
    print(f"{len(contracts)} contracts", flush=True)
    client = httpx.Client(timeout=600.0)
    for c in contracts:
        while True:
            row = ledger_row(ledger, c["incident_id"])
            if row and row["outcome"] != "DISCARDED":
                break
            if row and "tokens per" not in (row.get("discard_reason") or ""):
                print(f"{c['pod']['name']}: discarded, not a rate limit; kept", flush=True)
                break
            response = client.post(f"{BRAIN}/diagnose", json=c)
            response.raise_for_status()
            verdict = response.json()
            if verdict["outcome"] != "DISCARDED":
                print(
                    f"{c['pod']['name']}: {verdict['outcome']} class={verdict['evidence_class']} "
                    f"final={verdict['confidence_final']}",
                    flush=True,
                )
                break
            wait = wait_from(verdict["discard_reason"])
            if wait is None:
                print(
                    f"{c['pod']['name']}: discarded: {verdict['discard_reason'][:120]}", flush=True
                )
                break
            print(f"{c['pod']['name']}: rate limited, waiting {wait:.0f}s", flush=True)
            time.sleep(wait + 2)


def ledger_row(ledger: Path, incident_id: str) -> dict[str, Any] | None:
    if not ledger.exists():
        return None
    conn = sqlite3.connect(ledger)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM diagnoses WHERE incident_id = ?", (incident_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ------------------------------------------------------------------- score

PREDICTIONS = {
    # docs/DESIGN.md 2.4, root cause. OOM is split into what and why in 2.2.
    "ImagePullBackOff": 0.90,
    "CrashLoopBackOff": 0.60,
    "OOMKilled": 0.95,
}
CRASH_ROWS = {"fatal": 0.85, "no_clear_error": 0.30, "no_logs": 0.10}


def score(out: Path, ledger: Path) -> None:
    catalogue = by_id()
    conn = sqlite3.connect(ledger)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM diagnoses")]
    conn.close()

    scored: list[dict[str, Any]] = []
    for row in rows:
        contract = json.loads(row.get("contract_json") or "{}")
        pod = (contract.get("pod") or {}).get("name", "")
        inc = catalogue.get(pod.removeprefix("eval-"))
        if inc is None:
            continue
        scored.append(judge(inc, row))
    (out / "scored.json").write_text(json.dumps(scored, indent=1))

    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in scored:
        by_class[s["class"]].append(s)

    lines = []
    lines.append(
        "| Failure type | n | Discarded | Root cause correct | Abstained | Abstentions correct "
        "| Misclassified | Cost per diagnosis | Cost per correct | Cost per abstention |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    summary: dict[str, dict[str, Any]] = {}
    for cls in ("ImagePullBackOff", "CrashLoopBackOff", "OOMKilled", "OOMKilledDuringInit"):
        items = by_class.get(cls, [])
        if not items:
            continue
        n = len(items)
        discarded = [i for i in items if i["outcome"] == "DISCARDED"]
        live = [i for i in items if i["outcome"] != "DISCARDED"]
        correct = [i for i in live if i["correct"]]
        abstained = [i for i in live if i["abstained"]]
        abst_ok = [i for i in abstained if not i["diagnosable"]]
        misclassified = [i for i in live if i["misclassified"]]
        cost_live = sum(i["cost_usd"] for i in live if i["outcome"] == "DIAGNOSED")
        diagnosed = [i for i in live if i["outcome"] == "DIAGNOSED"]
        per_diag = cost_live / len(diagnosed) if diagnosed else 0.0
        per_correct = cost_live / len(correct) if correct else float("nan")
        per_abst = (
            (sum(i["cost_usd"] for i in abstained) / len(abstained)) if abstained else float("nan")
        )
        acc = len(correct) / len(live) if live else float("nan")
        summary[cls] = {
            "n": n,
            "discarded": len(discarded),
            "live": len(live),
            "correct": len(correct),
            "accuracy": acc,
            "abstained": len(abstained),
            "abstentions_correct": len(abst_ok),
            "misclassified": len(misclassified),
            "cost_per_diagnosis": per_diag,
            "cost_per_correct": per_correct,
            "cost_per_abstention": per_abst,
        }
        lines.append(
            f"| {cls} | {n} | {len(discarded)} | {len(correct)}/{len(live)} ({acc:.0%}) | "
            f"{len(abstained)} | {len(abst_ok)}/{len(abstained)} | {len(misclassified)} | "
            f"${per_diag:.5f} | {f'${per_correct:.5f}' if correct else 'n/a'} | "
            f"{f'${per_abst:.5f}' if abstained else 'n/a'} |"
        )
    table = "\n".join(lines)

    # CrashLoopBackOff by the 2.3 row.
    crash_lines = [
        "| Section 2.3 row | n | Correct | Abstained | Abstentions correct | Predicted |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for shape, predicted in CRASH_ROWS.items():
        items = [
            i
            for i in by_class.get("CrashLoopBackOff", [])
            if i["log_shape"] == shape and i["outcome"] != "DISCARDED"
        ]
        if not items:
            continue
        n_correct = sum(1 for i in items if i["correct"])
        abst = [i for i in items if i["abstained"]]
        ok = sum(1 for i in abst if not i["diagnosable"])
        crash_lines.append(
            f"| {shape} | {len(items)} | {n_correct}/{len(items)} ({n_correct / len(items):.0%}) "
            f"| {len(abst)} | {ok}/{len(abst)} | {predicted:.0%} |"
        )
    crash_table = "\n".join(crash_lines)

    # OOM what and why.
    oom = [
        i
        for i in scored
        if i["class"] in ("OOMKilled", "OOMKilledDuringInit") and i["outcome"] != "DISCARDED"
    ]
    what = sum(1 for i in oom if i["oom_what"])
    why_items = [i for i in oom if i["class"] == "OOMKilled"]
    why_right = sum(1 for i in why_items if i["oom_why"] == "correct")
    why_declined = sum(1 for i in why_items if i["oom_why"] == "declined")
    why_wrong = sum(1 for i in why_items if i["oom_why"] == "wrong")
    oom_text = (
        (
            f"OOM what (killed for memory) correct: {what}/{len(oom)} "
            f"({what / len(oom):.0%} vs predicted 95%). "
            f"OOM why on {len(why_items)} runtime kills: correct {why_right}, "
            f"declined to choose {why_declined}, wrong {why_wrong} "
            "(section 2.2 predicted 50% and said Coroner should usually decline)."
        )
        if oom
        else "no OOM rows"
    )

    # Calibration against the ceiling.
    cal_lines = [
        "| Ceiling (evidence class) | n | Correct | Observed accuracy |",
        "| --- | --- | --- | --- |",
    ]
    by_ceiling: dict[tuple[float, str], list[dict[str, Any]]] = defaultdict(list)
    for i in scored:
        if i["outcome"] == "DIAGNOSED":
            by_ceiling[(i["ceiling"], i["evidence_class"])].append(i)
    for (ceil, cls_name), items in sorted(by_ceiling.items(), reverse=True):
        n_correct = sum(1 for i in items if i["correct"])
        cal_lines.append(
            f"| {ceil:.2f} ({cls_name}) | {len(items)} | {n_correct} "
            f"| {n_correct / len(items):.0%} |"
        )
    cal_table = "\n".join(cal_lines)

    # Predictions scored.
    pred_lines = ["| Failure type | Predicted | Measured | Verdict |", "| --- | --- | --- | --- |"]
    for cls, predicted in PREDICTIONS.items():
        if cls == "OOMKilled":
            measured = what / len(oom) if oom else float("nan")
            label = "OOMKilled (what)"
        else:
            measured = summary.get(cls, {}).get("accuracy", float("nan"))
            label = cls
        if measured != measured:
            verdict = "no data"
        elif measured >= predicted:
            verdict = "met or beaten"
        else:
            verdict = "missed"
        pred_lines.append(f"| {label} | {predicted:.0%} | {measured:.0%} | {verdict} |")
    pred_table = "\n".join(pred_lines)

    falsification = []
    ipb = summary.get("ImagePullBackOff", {}).get("accuracy")
    if ipb is not None and ipb == ipb:
        falsification.append(
            f"ImagePullBackOff {ipb:.0%}: "
            + (
                "above 75 percent, the prompt and parser are not indicted."
                if ipb >= 0.75
                else "BELOW 75 percent: by section 2.4 the prompt or the parser is wrong."
            )
        )
    clb = summary.get("CrashLoopBackOff", {})
    if clb.get("live", 0) >= 20:
        falsification.append(
            f"CrashLoopBackOff {clb['accuracy']:.0%} over {clb['live']} incidents: "
            + (
                "above 40 percent, the context contract is not indicted."
                if clb["accuracy"] >= 0.40
                else "BELOW 40 percent over 20 or more incidents: by section 2.4 the context "
                "contract is wrong, not the prompt."
            )
            + (
                " Above 80 percent: section 2.4 says the sample is likely biased toward "
                "well-instrumented applications."
                if clb["accuracy"] > 0.80
                else ""
            )
        )

    report = "\n\n".join(
        [
            "## Accuracy per failure type\n\n" + table,
            "## CrashLoopBackOff by section 2.3 row\n\n" + crash_table,
            "## OOM, what and why\n\n" + oom_text,
            "## Calibration against the deterministic ceiling\n\n" + cal_table,
            "## Section 2.4 predictions scored\n\n" + pred_table,
            "## Falsification criteria as written\n\n" + "\n".join(f"- {f}" for f in falsification),
        ]
    )
    (out / "report.md").write_text(report + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(report)


def judge(inc: Incident, row: dict[str, Any]) -> dict[str, Any]:
    """Score one row against its constructed truth. Deterministic; no model."""
    outcome = str(row["outcome"])
    # A DISCARDED row, quota exhaustion included, is neither a diagnosis nor
    # an abstention: nothing was reasoned. It is counted, it is never scored,
    # and it is never an abstention whatever its discard reason says.
    abstained = outcome == "INSUFFICIENT_CONTEXT"
    assert not (outcome == "DISCARDED" and abstained)
    text = " ".join(
        str(row.get(k) or "") for k in ("root_cause", "explanation", "competing_hypothesis")
    )
    root = str(row.get("root_cause") or "")
    classified = str(row["failure_type"])
    misclassified = classified != inc.failure_class

    correct = False
    if outcome == "DIAGNOSED":
        if inc.failure_class in ("OOMKilled", "OOMKilledDuringInit"):
            correct = inc.rubric.matches(root) and not misclassified
        else:
            correct = inc.diagnosable and inc.rubric.matches(root) and not misclassified
    elif abstained:
        # Abstaining is the right answer when the cause is not in the evidence.
        correct = not inc.diagnosable

    oom_what = False
    oom_why = ""
    if inc.failure_class in ("OOMKilled", "OOMKilledDuringInit") and outcome == "DIAGNOSED":
        oom_what = bool(re.search(r"memory|oom", root, re.IGNORECASE)) and not misclassified
        if inc.failure_class == "OOMKilled":
            lower = text.lower()
            names = {
                "limit_too_low": any(
                    p in lower
                    for p in (
                        "limit is too low",
                        "limit too low",
                        "below",
                        "insufficient limit",
                        "undersized",
                        "too low",
                    )
                ),
                "leak": "leak" in lower,
                "spike": any(
                    p in lower for p in ("spike", "burst", "transient", "one-off", "bulk")
                ),
            }
            competing = str(row.get("competing_hypothesis") or "").strip()
            if competing and sum(names.values()) >= 2:
                oom_why = "declined"
            elif names.get(inc.why) and sum(names.values()) == 1:
                oom_why = "correct"
            elif competing or sum(names.values()) == 0:
                oom_why = "declined"
            else:
                oom_why = "wrong"
    return {
        "id": inc.id,
        "class": inc.failure_class,
        "classified_as": classified,
        "misclassified": misclassified,
        "abstained": abstained,
        "discarded": outcome == "DISCARDED",
        "log_shape": inc.log_shape,
        "why": inc.why,
        "diagnosable": inc.diagnosable,
        "outcome": outcome,
        "evidence_class": row.get("evidence_class"),
        "ceiling": float(row.get("confidence_final") or 0) if outcome == "DIAGNOSED" else None,
        "correct": correct,
        "oom_what": oom_what,
        "oom_why": oom_why,
        "root_cause": root,
        "truth": inc.truth,
        "cost_usd": float(row.get("cost_usd") or 0),
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "latency_ms": row.get("latency_ms_total"),
        "retries": row.get("validation_retries"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["collect", "diagnose", "score"])
    parser.add_argument("--out", default=str(ROOT / "eval" / "results"))
    parser.add_argument("--ledger", default=str(ROOT / "eval" / "results" / "ledger.sqlite3"))
    parser.add_argument("--batch", type=int, default=12)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.phase == "collect":
        collect(out, args.batch)
    elif args.phase == "diagnose":
        diagnose(out, Path(args.ledger))
    else:
        score(out, Path(args.ledger))


if __name__ == "__main__":
    main()
