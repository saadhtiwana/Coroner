#!/usr/bin/env python3
"""Count pods that have reached a failure state Coroner would collect.

Reads `kubectl get pods -o json` on stdin. Used by the demo to wait, which
is why it is a script rather than a jsonpath: waiting-reason alone is not a
failure, a container that has restarted into backoff is.
"""

import json
import sys

WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "RunContainerError"}


def main() -> int:
    pods = json.load(sys.stdin).get("items", [])
    failed = 0
    for pod in pods:
        for status in pod.get("status", {}).get("containerStatuses") or []:
            reason = (status.get("state", {}).get("waiting") or {}).get("reason", "")
            terminated = (status.get("lastState", {}) or {}).get("terminated") or {}
            if reason in WAITING or terminated.get("reason") in ("OOMKilled", "StartError"):
                failed += 1
                break
    print(failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
