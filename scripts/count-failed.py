#!/usr/bin/env python3
"""Count pods that have reached a failure state Coroner would collect.

Reads `kubectl get pods -o json` on stdin. Used by the demo to wait, which
is why it is a script rather than a jsonpath: waiting-reason alone is not a
failure, a container that has restarted into backoff is.
"""

import json
import sys

WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "RunContainerError"}


def has_failed(status: dict) -> bool:
    """True once this container has failed in a way Coroner would collect.

    A pod that is crash looping spends part of every cycle showing Error
    rather than CrashLoopBackOff, so waiting reason alone is a coin flip:
    waiting for four of them to show it at the same instant can take
    minutes or never happen. A container that has terminated nonzero has
    failed, and stays failed, which is what the caller is waiting for.
    """
    if (status.get("state", {}).get("waiting") or {}).get("reason", "") in WAITING:
        return True
    for state in ("lastState", "state"):
        terminated = (status.get(state, {}) or {}).get("terminated") or {}
        if terminated and terminated.get("exitCode", 0) != 0:
            return True
    return False


def main() -> int:
    pods = json.load(sys.stdin).get("items", [])
    print(sum(1 for pod in pods
              if any(has_failed(s) for s in pod.get("status", {}).get("containerStatuses") or [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
