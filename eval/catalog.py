"""The incident catalogue for the accuracy harness.

Every incident here is constructed, so its actual cause is known before
Coroner looks at it. That is what makes accuracy measurable: the label is
not a human's opinion of the diagnosis, it is how the workload was built to
fail. docs/DESIGN.md section 5.

Each incident says which failure class it belongs to, what the cause is, and
whether that cause is present in the evidence Coroner collects. The last
flag is what scores abstention: abstaining on an incident whose cause is not
in the evidence is correct; abstaining on one whose cause is written in the
log is a miss. The rubric is a set of keyword groups the root cause must
touch, deliberately loose on wording and strict on substance, so that a
paraphrase passes and a different cause does not.

The 2.3 table's middle and bottom rows are represented on purpose: logs
present with no clear error, and logs empty or unretrievable. That is where
hallucination concentrates and where the gate has to earn its place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

IMAGE = "redis:alpine"


@dataclass(frozen=True)
class Rubric:
    """What a correct root cause must and must not say.

    ``any_of`` is a list of groups; every group must be matched by at least
    one of its phrases. ``none_of`` are phrases that mark a wrong cause.
    """

    any_of: tuple[tuple[str, ...], ...] = ()
    none_of: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        t = text.lower()
        if any(bad in t for bad in self.none_of):
            return False
        return all(any(p in t for p in group) for group in self.any_of)


@dataclass(frozen=True)
class Incident:
    id: str
    failure_class: str  # CrashLoopBackOff, ImagePullBackOff, OOMKilled, OOMKilledDuringInit
    truth: str  # one line, how the workload was built to fail
    diagnosable: bool  # is the cause present in the collected evidence
    rubric: Rubric
    # Section 2.3 row for CrashLoopBackOff: fatal, no_clear_error, no_logs.
    log_shape: str = ""
    # For OOM: the constructed why. limit_too_low, leak, spike, init.
    why: str = ""
    # Workload spec.
    image: str = IMAGE
    command: tuple[str, ...] = ("/bin/sh", "-c")
    script: str = ""
    memory_limit: str = ""
    memory_request: str = ""
    extra_labels: dict[str, str] = field(default_factory=dict)

    @property
    def pod_name(self) -> str:
        return f"eval-{self.id}"


def _crash(
    id: str, truth: str, script: str, rubric: Rubric, *, diagnosable: bool, log_shape: str
) -> Incident:
    return Incident(
        id=id,
        failure_class="CrashLoopBackOff",
        truth=truth,
        diagnosable=diagnosable,
        rubric=rubric,
        log_shape=log_shape,
        script=script,
    )


def _pull(id: str, image: str, truth: str, rubric: Rubric, *, diagnosable: bool = True) -> Incident:
    return Incident(
        id=id,
        failure_class="ImagePullBackOff",
        truth=truth,
        diagnosable=diagnosable,
        rubric=rubric,
        image=image,
        command=(),
    )


def _oom(
    id: str, limit: str, why: str, truth: str, script: str, rubric: Rubric, *, init: bool = False
) -> Incident:
    return Incident(
        id=id,
        failure_class="OOMKilledDuringInit" if init else "OOMKilled",
        truth=truth,
        diagnosable=True,
        rubric=rubric,
        why=why,
        script=script,
        memory_limit=limit,
        memory_request=limit,
    )


MEMORY = Rubric(any_of=(("memory", "oom"),))
NOT_MEMORY = ("out of memory", "oom", "memory limit")

# --------------------------------------------------------------- crashloop

CRASHLOOP: list[Incident] = [
    # Fatal line present. Section 2.3 top row; predicted 85 percent.
    _crash(
        "crash-db-refused",
        "the application cannot reach Postgres at 10.96.31.14:5432; connection refused",
        'echo "[startup] orders-api booting"\necho "[startup] connecting to postgres"\nsleep 1\n'
        'echo "[error] dial tcp 10.96.31.14:5432: connect: connection refused" >&2\n'
        'echo "[fatal] could not initialise connection pool after 1 attempt" >&2\nexit 1\n',
        Rubric(any_of=(("postgres", "database", "5432", "connection"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-missing-config",
        "the config file /etc/app/config.yaml does not exist",
        'echo "[startup] loading config"\necho "fatal: open /etc/app/config.yaml: no such file or directory" >&2\nexit 1\n',
        Rubric(
            any_of=(("config",), ("no such file", "missing", "not found", "does not exist")),
            none_of=NOT_MEMORY,
        ),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-bad-flag",
        "the process was started with an unknown flag -workers",
        'echo "flag provided but not defined: -workers" >&2\necho "usage: app [-port] [-db]" >&2\nexit 2\n',
        Rubric(any_of=(("flag", "-workers", "argument", "option"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-port-in-use",
        "port 8080 is already bound by another process",
        'echo "[startup] api listening"\necho "fatal: listen tcp :8080: bind: address already in use" >&2\nexit 1\n',
        Rubric(any_of=(("8080", "port", "address already in use", "bind"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-permission-denied",
        "the process cannot write /var/log/app.log; permission denied",
        'echo "[startup] opening log"\necho "fatal: open /var/log/app.log: permission denied" >&2\nexit 1\n',
        Rubric(any_of=(("permission", "denied", "/var/log/app.log"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-missing-env",
        "the required environment variable STRIPE_KEY is not set",
        'echo "[startup] checking configuration"\necho "fatal: required environment variable STRIPE_KEY is not set" >&2\nexit 3\n',
        Rubric(any_of=(("stripe_key", "environment variable", "env"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-tls-expired",
        "the upstream TLS certificate has expired",
        'echo "[startup] connecting to payments.internal:443"\necho "fatal: x509: certificate has expired or is not yet valid" >&2\nexit 1\n',
        Rubric(any_of=(("certificate", "x509", "tls", "expired"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-dns",
        "db.internal does not resolve in cluster DNS",
        'echo "[startup] resolving db.internal"\necho "fatal: lookup db.internal on 10.96.0.10:53: no such host" >&2\nexit 1\n',
        Rubric(
            any_of=(("dns", "resolve", "no such host", "lookup", "db.internal"),),
            none_of=NOT_MEMORY,
        ),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-migration",
        "database migration 0042 fails because the orders relation does not exist",
        'echo "[migrate] applying 0042_add_index"\necho "fatal: migration 0042_add_index failed: relation \\"orders\\" does not exist" >&2\nexit 1\n',
        Rubric(any_of=(("migration", "0042", "relation", "orders"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-app-oom-abort",
        "the runtime aborted itself with an out-of-memory error; no kernel OOM kill and no memory limit",
        'echo "[startup] building index"\necho "fatal error: runtime: out of memory" >&2\necho "goroutine 1 [running]:" >&2\nexit 2\n',
        Rubric(any_of=(("out of memory", "memory"),)),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-redis-noauth",
        "Redis requires authentication and none was configured",
        'echo "[startup] connecting to cache"\necho "fatal: NOAUTH Authentication required." >&2\nexit 1\n',
        Rubric(any_of=(("auth", "noauth", "password", "credential"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-disk-full",
        "the data volume is full; write fails with no space left on device",
        'echo "[wal] appending segment 12"\necho "fatal: write /data/wal/000012: no space left on device" >&2\nexit 1\n',
        Rubric(any_of=(("no space", "disk", "volume", "space left"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-nil-panic",
        "the application panics on a nil pointer dereference at startup",
        'echo "[startup] wiring handlers"\necho "panic: runtime error: invalid memory address or nil pointer dereference" >&2\n'
        'echo "[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=0x4a2f1c]" >&2\n'
        'echo "goroutine 1 [running]:" >&2\necho "main.(*Server).wire(0x0)" >&2\necho "\\t/src/server.go:88 +0x1c" >&2\nexit 2\n',
        Rubric(
            any_of=(("nil pointer", "panic", "segmentation", "sigsegv", "dereference"),),
            none_of=("memory limit", "oom"),
        ),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-python-import",
        "a Python dependency, psycopg2, is not installed in the image",
        'echo "Traceback (most recent call last):"\necho "  File \\"/app/main.py\\", line 3, in <module>"\n'
        'echo "    import psycopg2"\necho "ModuleNotFoundError: No module named \'psycopg2\'"\nexit 1\n',
        Rubric(any_of=(("psycopg2", "module", "dependency", "import"),), none_of=NOT_MEMORY),
        diagnosable=True,
        log_shape="fatal",
    ),
    _crash(
        "crash-java-classnotfound",
        "the JVM entry class com.acme.Main is missing from the jar",
        'echo "Error: Could not find or load main class com.acme.Main" >&2\n'
        'echo "Caused by: java.lang.ClassNotFoundException: com.acme.Main" >&2\nexit 1\n',
        Rubric(
            any_of=(("com.acme.main", "class", "classnotfound", "main class"),), none_of=NOT_MEMORY
        ),
        diagnosable=True,
        log_shape="fatal",
    ),
    # Logs present, no clear error. Section 2.3 middle row; predicted 30
    # percent. The cause is not in the evidence: the process was built to
    # exit nonzero after ordinary output.
    _crash(
        "crash-silent-after-chatter",
        "the process exits 1 after normal startup output and logs no reason",
        'echo "[startup] booting v2.3.1"\necho "[startup] loading 3 plugins"\necho "[startup] plugins loaded"\nsleep 1\nexit 1\n',
        Rubric(),
        diagnosable=False,
        log_shape="no_clear_error",
    ),
    _crash(
        "crash-progress-then-exit-2",
        "the process exits 2 mid-batch and logs no reason",
        'for i in 1 2 3 4 5; do echo "[batch] processed batch $i of 12"; done\nexit 2\n',
        Rubric(),
        diagnosable=False,
        log_shape="no_clear_error",
    ),
    _crash(
        "crash-warning-only",
        "the process exits 42 after a warning about a cold cache, which is not the cause",
        'echo "[warn] cache cold, first requests will be slow"\necho "[info] ready"\nsleep 1\nexit 42\n',
        Rubric(),
        diagnosable=False,
        log_shape="no_clear_error",
    ),
    _crash(
        "crash-json-logs-no-error",
        "the process exits 1 after info-level JSON logs with no error entry",
        'echo \'{"level":"info","msg":"listening","port":9000}\'\necho \'{"level":"info","msg":"ready"}\'\nsleep 1\nexit 1\n',
        Rubric(),
        diagnosable=False,
        log_shape="no_clear_error",
    ),
    _crash(
        "crash-healthcheck-chatter",
        "the process exits 1 after logging successful health checks",
        'for i in 1 2 3; do echo "[health] ok"; sleep 1; done\nexit 1\n',
        Rubric(),
        diagnosable=False,
        log_shape="no_clear_error",
    ),
    # Logs empty or unretrievable. Section 2.3 bottom row; predicted under
    # 10 percent, and the deterministic gate is expected to abstain.
    _crash(
        "crash-silent-exit-1",
        "the process exits 1 with no output at all",
        "exit 1\n",
        Rubric(),
        diagnosable=False,
        log_shape="no_logs",
    ),
    _crash(
        "crash-silent-exit-3",
        "the process exits 3 with no output at all",
        "sleep 1\nexit 3\n",
        Rubric(),
        diagnosable=False,
        log_shape="no_logs",
    ),
    _crash(
        "crash-self-sigkill",
        "the process sends itself SIGKILL; exit 137 with no memory limit and no OOM",
        "sleep 1\nkill -9 $$\n",
        Rubric(),
        diagnosable=False,
        log_shape="no_logs",
    ),
    _crash(
        "crash-self-sigsegv",
        "the process sends itself SIGSEGV; exit 139, no output",
        "sleep 1\nkill -SEGV $$\n",
        Rubric(),
        diagnosable=False,
        log_shape="no_logs",
    ),
    _crash(
        "crash-self-sigterm",
        "the process sends itself SIGTERM; exit 143, no output",
        "sleep 1\nkill -TERM $$\n",
        Rubric(),
        diagnosable=False,
        log_shape="no_logs",
    ),
    _crash(
        "crash-logs-to-file",
        "the process writes its fatal error to a file, not stdout, and exits 1",
        'echo "fatal: license key rejected by server" > /tmp/app.log\nexit 1\n',
        Rubric(),
        diagnosable=False,
        log_shape="no_logs",
    ),
]

# --------------------------------------------------------------- image pull

ANON_403 = Rubric(
    any_of=(
        ("403", "forbidden", "authoriz", "does not exist", "private", "credential", "not found"),
    )
)
DENIED = Rubric(
    any_of=(("denied", "does not exist", "authoriz", "not found", "private", "credential"),)
)
TAG = Rubric(any_of=(("tag", "manifest", "not found", "does not exist", "unknown"),))
INVALID = Rubric(any_of=(("invalid", "reference", "format", "uppercase", "name", "malformed"),))
UNREACHABLE = Rubric(
    any_of=(
        (
            "no such host",
            "resolve",
            "unreachable",
            "dial",
            "timeout",
            "dns",
            "registry",
            "network",
            "connect",
        ),
    )
)
DIGEST = Rubric(any_of=(("digest", "sha256", "not found", "manifest", "unknown"),))

IMAGEPULL: list[Incident] = [
    _pull(
        "pull-ghcr-missing-1",
        "ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0",
        "ghcr.io repository does not exist; registry answers 403 on the anonymous token",
        ANON_403,
    ),
    _pull(
        "pull-ghcr-missing-2",
        "ghcr.io/saadhtiwana/orders-api:v2.3.1",
        "ghcr.io repository does not exist; 403 on the anonymous token",
        ANON_403,
    ),
    _pull(
        "pull-ghcr-missing-3",
        "ghcr.io/acme-platform/payments:1.9.0",
        "ghcr.io organisation and repository do not exist; 403",
        ANON_403,
    ),
    _pull(
        "pull-ghcr-missing-4",
        "ghcr.io/saadhtiwana/coroner-agent:v9.9.9",
        "ghcr.io repository does not exist under this name; 403",
        ANON_403,
    ),
    _pull(
        "pull-ghcr-private-looking",
        "ghcr.io/github/internal-billing:latest",
        "a repository that, if it exists, is private; ghcr.io answers 403 either way",
        ANON_403,
    ),
    _pull(
        "pull-docker-missing-1",
        "docker.io/saadhtiwana/nope:1.0",
        "Docker Hub repository does not exist; pull access denied",
        DENIED,
    ),
    _pull(
        "pull-docker-missing-2",
        "saadhtiwana/coroner-nope:latest",
        "Docker Hub repository does not exist; pull access denied",
        DENIED,
    ),
    _pull(
        "pull-docker-missing-3",
        "acmecorp-internal/checkout:3.2",
        "Docker Hub repository does not exist; pull access denied",
        DENIED,
    ),
    _pull(
        "pull-wrong-tag-redis",
        "redis:99.99.99-nope",
        "the redis image exists but the tag does not; manifest unknown",
        TAG,
    ),
    _pull(
        "pull-wrong-tag-nginx",
        "nginx:0.0.0-nope",
        "the nginx image exists but the tag does not; manifest unknown",
        TAG,
    ),
    _pull(
        "pull-wrong-tag-alpine",
        "alpine:nonexistent-tag",
        "the alpine image exists but the tag does not; manifest unknown",
        TAG,
    ),
    _pull(
        "pull-wrong-tag-busybox",
        "busybox:1.99.0",
        "the busybox image exists but the tag does not; manifest unknown",
        TAG,
    ),
    _pull(
        "pull-quay-missing-1",
        "quay.io/saadhtiwana/nope:latest",
        "quay.io repository does not exist; unauthorized",
        DENIED,
    ),
    _pull(
        "pull-quay-missing-2",
        "quay.io/acme/worker:2.0",
        "quay.io repository does not exist; unauthorized",
        DENIED,
    ),
    _pull(
        "pull-gcr-missing",
        "gcr.io/saadhtiwana-nope/app:1",
        "gcr.io project does not exist; denied or not found",
        DENIED,
    ),
    _pull(
        "pull-invalid-uppercase",
        "Redis:Alpine",
        "the image name has uppercase letters, which is not a valid reference",
        INVALID,
    ),
    _pull(
        "pull-invalid-double-colon", "redis::alpine", "the image reference is malformed", INVALID
    ),
    _pull(
        "pull-invalid-digest",
        "redis@sha256:notahexdigest",
        "the digest is not a valid sha256",
        INVALID,
    ),
    _pull(
        "pull-unreachable-host",
        "registry.invalid/app:1",
        "the registry host does not resolve",
        UNREACHABLE,
    ),
    _pull(
        "pull-unreachable-ip",
        "10.255.255.1:5000/app:1",
        "the registry address does not answer; dial timeout",
        UNREACHABLE,
    ),
    _pull(
        "pull-bad-digest",
        "redis@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "a well-formed digest that no manifest has",
        DIGEST,
    ),
    _pull(
        "pull-docker-missing-4",
        "library/definitely-not-an-image:1",
        "Docker Hub official-namespace repository does not exist",
        DENIED,
    ),
]

# --------------------------------------------------------------------- oom


def _alloc_script(limit_mib: int, why: str, *, logs: bool) -> str:
    """Shell that exhausts memory in a constructed way. The shell holds the
    allocation in a variable, so it counts against the cgroup."""
    say = 'echo "$1"' if logs else ":"
    if why == "limit_too_low":
        # Steady requirement above the limit: one allocation of 1.5x, held.
        size = int(limit_mib * 1.5)
        return (
            f'say() {{ {say}; }}\nsay "[startup] worker booting"\nsay "[index] loading {size}MiB working set"\n'
            f"chunk=$(head -c 1048576 /dev/zero | tr '\\0' 'x')\nacc=\"\"; i=0\n"
            f'while [ $i -lt {size} ]; do acc="$acc$chunk"; i=$((i+1)); done\n'
            'say "[index] loaded"\nsleep 3600\n'
        )
    if why == "leak":
        return (
            f'say() {{ {say}; }}\nsay "[startup] worker booting"\n'
            "chunk=$(head -c 4194304 /dev/zero | tr '\\0' 'x')\nacc=\"\"; i=0\n"
            'while true; do acc="$acc$chunk"; i=$((i+1)); say "[cache] entries retained: $((i*4))MiB, none evicted"; sleep 1; done\n'
        )
    # spike: modest steady state, then one burst of 2x the limit.
    steady = max(1, limit_mib // 4)
    burst = limit_mib * 2
    return (
        f'say() {{ {say}; }}\nsay "[startup] worker booting"\n'
        "chunk=$(head -c 1048576 /dev/zero | tr '\\0' 'x')\nacc=\"\"; i=0\n"
        f'while [ $i -lt {steady} ]; do acc="$acc$chunk"; i=$((i+1)); done\n'
        f'say "[worker] steady at {steady}MiB, serving"\nsleep 15\n'
        f'say "[worker] tenant bulk export requested, {burst}MiB"\n'
        f'while [ $i -lt {burst + steady} ]; do acc="$acc$chunk"; i=$((i+1)); done\nsleep 3600\n'
    )


WHY_TRUTH = {
    "limit_too_low": "the limit is below the workload's steady requirement",
    "leak": "the process retains memory without bound; any limit would be exhausted",
    "spike": "a one-off burst exceeds the limit; the steady state fits",
}

OOM: list[Incident] = []
for _mib, _why, _logs in (
    (32, "limit_too_low", True),
    (64, "limit_too_low", True),
    (128, "limit_too_low", False),
    (256, "limit_too_low", True),
    (48, "limit_too_low", False),
    (64, "leak", True),
    (128, "leak", True),
    (32, "leak", False),
    (96, "leak", True),
    (64, "spike", True),
    (128, "spike", True),
    (256, "spike", False),
    (80, "spike", True),
):
    OOM.append(
        _oom(
            f"oom-{_why.replace('_', '-')}-{_mib}mi-{'logs' if _logs else 'silent'}",
            f"{_mib}Mi",
            _why,
            f"killed at {_mib}Mi; {WHY_TRUTH[_why]}",
            _alloc_script(_mib, _why, logs=_logs),
            MEMORY,
        )
    )

for _n, (_lim, _cmd) in enumerate(
    [
        ("1Mi", "echo alive; sleep 3600"),
        ("2Mi", "echo alive; sleep 3600"),
        ("3Mi", "sleep 3600"),
        ("2Mi", "redis-server --save ''"),
        ("1Mi", "sleep 3600"),
        ("2Mi", "cat /dev/null; sleep 3600"),
        ("3Mi", "echo starting; redis-server"),
        ("2Mi", "sh -c 'sleep 3600'"),
    ],
    start=1,
):
    OOM.append(
        _oom(
            f"oom-init-{_lim.lower()}-{_n}",
            _lim,
            "init",
            f"the {_lim} limit is below what the container runtime needs to start; init is OOM-killed",
            _cmd,
            Rubric(any_of=(("memory", "oom"), ("limit", "init", "start"))),
            init=True,
        )
    )

ALL: list[Incident] = CRASHLOOP + IMAGEPULL + OOM


def by_id() -> dict[str, Incident]:
    return {i.id: i for i in ALL}


def manifest(inc: Incident, namespace: str) -> dict[str, object]:
    """A bare pod. Bare so that each incident is one pod with a stable name."""
    container: dict[str, object] = {"name": "app", "image": inc.image}
    if inc.command:
        container["command"] = list(inc.command)
        container["args"] = [inc.script]
    if inc.image == IMAGE:
        container["imagePullPolicy"] = "Never"
    if inc.memory_limit:
        container["resources"] = {
            "limits": {"memory": inc.memory_limit},
            "requests": {"memory": inc.memory_request or inc.memory_limit},
        }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": inc.pod_name,
            "namespace": namespace,
            "labels": {
                "coroner.dev/purpose": "eval",
                "coroner.dev/eval-id": inc.id,
                **inc.extra_labels,
            },
        },
        "spec": {
            "restartPolicy": "Always",
            "terminationGracePeriodSeconds": 1,
            "containers": [container],
        },
    }


if __name__ == "__main__":
    from collections import Counter

    print(Counter(i.failure_class for i in ALL))
    print("crashloop log shapes:", Counter(i.log_shape for i in CRASHLOOP))
    print("oom whys:", Counter(i.why for i in OOM))
    ids = [i.id for i in ALL]
    assert len(ids) == len(set(ids)), "duplicate ids"
