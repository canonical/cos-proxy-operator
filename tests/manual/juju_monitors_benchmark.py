"""Manual Juju benchmark for COS Proxy monitors fan-in.

Run with:

    tox -e benchmark

Useful environment variables:

    COS_PROXY_BASELINE_CHARM=cos-proxy
    COS_PROXY_BASELINE_CHANNEL=2/stable
    COS_PROXY_BASELINE_REVISION=123
    COS_PROXY_BASELINE_REF=origin/main
    COS_PROXY_CANDIDATE_CHARM=/path/to/candidate.charm
    COS_PROXY_BENCHMARK_CASES=both
    COS_PROXY_BENCHMARK_UNITS=20
    COS_PROXY_BENCHMARK_CHECKS=3
    COS_PROXY_BENCHMARK_TIMEOUT=1800
    COS_PROXY_BENCHMARK_BASE=ubuntu@22.04
    COS_PROXY_CHARMCRAFT_PACK_ARGS=--platform ubuntu@22.04:amd64

The benchmark packs two COS Proxy charms:

* baseline: from Charmhub cos-proxy 2/stable by default
* candidate: from the current working tree, or COS_PROXY_CANDIDATE_CHARM if set

It deploys each into an isolated Juju model with a synthetic monitors provider
and a synthetic prometheus_scrape consumer, then measures how long the model
takes to reach the expected downstream scrape/alert counts.
Results are labelled sequential for the baseline implementation and batched for
the candidate implementation.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import jubilant

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = Path(os.environ.get("COS_PROXY_BENCHMARK_ARTIFACTS", REPO_ROOT / ".benchmark"))
CHARM_CACHE_DIR = ARTIFACTS_DIR / "charms"
UNITS = int(os.environ.get("COS_PROXY_BENCHMARK_UNITS", "20"))
CHECKS = int(os.environ.get("COS_PROXY_BENCHMARK_CHECKS", "3"))
TIMEOUT = int(os.environ.get("COS_PROXY_BENCHMARK_TIMEOUT", "1800"))
CASES = os.environ.get("COS_PROXY_BENCHMARK_CASES", "both")
BASE = os.environ.get("COS_PROXY_BENCHMARK_BASE", "ubuntu@22.04")
BASELINE_CHANNEL = os.environ.get("COS_PROXY_BASELINE_CHANNEL", "2/stable")
BASELINE_REVISION = os.environ.get("COS_PROXY_BASELINE_REVISION")
DEFAULT_CHARMCRAFT_PACK_ARGS = f"--platform {BASE}:amd64"
CHARMCRAFT_PACK_ARGS = tuple(
    shlex.split(os.environ.get("COS_PROXY_CHARMCRAFT_PACK_ARGS", DEFAULT_CHARMCRAFT_PACK_ARGS))
)
COPY_IGNORE = shutil.ignore_patterns(
    ".benchmark",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "*.charm",
    "*.pyc",
    "build",
)


def run(*cmd: str, cwd: Path = REPO_ROOT) -> str:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output_lines = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output_lines.append(line)

    returncode = process.wait()
    output = "".join(output_lines)
    if returncode:
        raise RuntimeError(
            f"Command failed in {cwd} with exit code {returncode}: {' '.join(cmd)}\n"
            f"{output}"
        )
    return output


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def hash_charm_source(source: Path, *, name: str, version: str | None) -> str:
    digest = hashlib.sha256()
    digest.update(name.encode())
    digest.update(b"\0")
    digest.update(json.dumps(CHARMCRAFT_PACK_ARGS).encode())
    digest.update(b"\0")
    digest.update((version or "").encode())
    digest.update(b"\0")

    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(source).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def stage_charm_source(source: Path, name: str, version: str | None = None) -> Path:
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    stage = ARTIFACTS_DIR / f"{name}-source-{time.time_ns()}"
    if stage.exists():
        shutil.rmtree(stage)

    shutil.copytree(source, stage, ignore=COPY_IGNORE)
    if version is not None:
        charmcraft_yaml = stage / "charmcraft.yaml"
        text = charmcraft_yaml.read_text()
        text = text.replace(
            "git describe --always > $CRAFT_PART_INSTALL/version",
            f"printf '%s\\n' {shell_quote(version)} > $CRAFT_PART_INSTALL/version",
        )
        charmcraft_yaml.write_text(text)
    return stage


def pack_charm(source: Path, name: str, *, version: str | None = None) -> Path:
    pack_source = stage_charm_source(source, name, version)
    cache_key = hash_charm_source(pack_source, name=name, version=version)
    CHARM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination = CHARM_CACHE_DIR / f"{name}-{cache_key}.charm"
    if destination.exists():
        print(f"\nReusing packed charm: {destination}", flush=True)
        shutil.rmtree(pack_source)
        return destination

    before = set(pack_source.glob("*.charm"))
    try:
        run("charmcraft", "pack", *CHARMCRAFT_PACK_ARGS, cwd=pack_source)
    except Exception:
        print(f"\nPreserving failed pack source for debugging: {pack_source}", flush=True)
        raise

    after = set(pack_source.glob("*.charm"))
    candidates = sorted(after - before or after, key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise RuntimeError(f"charmcraft pack did not produce a charm in {pack_source}")

    packed = candidates[-1]
    shutil.copy2(packed, destination)
    shutil.rmtree(pack_source)
    return destination


def pack_cos_proxy_from_ref(ref: str) -> Path:
    worktree = ARTIFACTS_DIR / f"cos-proxy-baseline-{ref.replace('/', '-')}"
    if worktree.exists():
        run("git", "worktree", "remove", "--force", str(worktree))
    run("git", "worktree", "add", "--detach", str(worktree), ref)
    try:
        version = run("git", "describe", "--always", cwd=worktree).strip()
        return pack_charm(worktree, "cos-proxy-baseline", version=version)
    finally:
        run("git", "worktree", "remove", "--force", str(worktree))


def pack_candidate() -> Path:
    if candidate := os.environ.get("COS_PROXY_CANDIDATE_CHARM"):
        return Path(candidate).resolve()
    version = run("git", "describe", "--always", "--dirty", cwd=REPO_ROOT).strip()
    return pack_charm(REPO_ROOT, "cos-proxy-candidate", version=version)


def baseline_deploy_spec() -> tuple[str | Path, str, str | None, int | None]:
    if baseline_charm := os.environ.get("COS_PROXY_BASELINE_CHARM"):
        revision = int(BASELINE_REVISION) if BASELINE_REVISION else None
        if baseline_charm.startswith((".", "/")):
            return Path(baseline_charm).resolve(), f"local:{baseline_charm}", None, None
        return baseline_charm, f"{baseline_charm}:{BASELINE_CHANNEL}", BASELINE_CHANNEL, revision

    if baseline_ref := os.environ.get("COS_PROXY_BASELINE_REF"):
        return pack_cos_proxy_from_ref(baseline_ref), f"git:{baseline_ref}", None, None

    revision = int(BASELINE_REVISION) if BASELINE_REVISION else None
    return "cos-proxy", f"cos-proxy:{BASELINE_CHANNEL}", BASELINE_CHANNEL, revision


def relation_counts(juju: jubilant.Juju) -> tuple[int, int]:
    status = json.loads(juju.cli("status", "--format=json"))
    units = status["applications"]["scrape-consumer"]["units"]
    message = units["scrape-consumer/0"]["workload-status"].get("message", "")
    match = re.search(r"scrape_jobs=(\d+) alert_rules=(\d+)", message)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def wait_for_expected_counts(juju: jubilant.Juju) -> tuple[float, tuple[int, int], bool]:
    expected_jobs = UNITS * CHECKS + 1
    expected_alerts = UNITS * CHECKS
    started = time.monotonic()
    deadline = started + TIMEOUT
    last_counts = (0, 0)

    while time.monotonic() < deadline:
        last_counts = relation_counts(juju)
        if last_counts[0] >= expected_jobs and last_counts[1] >= expected_alerts:
            juju.wait(lambda status: jubilant.all_agents_idle(status), timeout=TIMEOUT)
            return time.monotonic() - started, last_counts, False
        time.sleep(5)

    return time.monotonic() - started, last_counts, True


def run_case(
    juju: jubilant.Juju,
    *,
    label: str,
    cos_proxy_charm: str | Path,
    cos_proxy_channel: str | None = None,
    cos_proxy_revision: int | None = None,
    provider_charm: Path,
    consumer_charm: Path,
) -> dict:
    juju.wait_timeout = TIMEOUT
    print(f"\n[{label}] deploying applications", flush=True)
    juju.deploy(
        cos_proxy_charm,
        "cos-proxy",
        base=BASE,
        channel=cos_proxy_channel,
        revision=cos_proxy_revision,
    )
    juju.deploy(
        provider_charm,
        "monitors-provider",
        base=BASE,
        config={"checks": CHECKS},
        num_units=UNITS,
    )
    juju.deploy(consumer_charm, "scrape-consumer", base=BASE)
    print(f"[{label}] waiting for deployed applications to become idle", flush=True)
    juju.wait(lambda status: jubilant.all_agents_idle(status), timeout=TIMEOUT)

    started = time.monotonic()
    print(f"[{label}] integrating downstream-prometheus-scrape", flush=True)
    juju.integrate("cos-proxy:downstream-prometheus-scrape", "scrape-consumer")
    print(f"[{label}] integrating monitors", flush=True)
    juju.integrate("cos-proxy:monitors", "monitors-provider:monitors")
    print(f"[{label}] waiting for expected downstream counts", flush=True)
    elapsed, counts, timed_out = wait_for_expected_counts(juju)
    if timed_out:
        print(f"[{label}] timed out after {elapsed:.1f}s; last counts={counts}", flush=True)

    return {
        "label": label,
        "units": UNITS,
        "checks_per_unit": CHECKS,
        "timed_out": timed_out,
        "timeout_seconds": TIMEOUT,
        "elapsed_seconds": elapsed,
        "wall_seconds_including_integrate_calls": time.monotonic() - started,
        "scrape_jobs": counts[0],
        "alert_rules": counts[1],
        "model": juju.model,
    }


def destroy_model(juju_factory, juju: jubilant.Juju) -> None:
    model = juju.model
    if not model:
        return
    print(f"\nDestroying benchmark model before next case: {model}", flush=True)
    try:
        juju.destroy_model(model, destroy_storage=True, force=True, timeout=TIMEOUT)
    except jubilant.CLIError as exc:
        print(f"WARNING: failed to destroy benchmark model {model}: {exc}", flush=True)
    finally:
        # The baseline model is intentionally destroyed before the candidate case to keep
        # large fan-in benchmarks within local LXD capacity. pytest-jubilant still has the
        # model registered for final teardown, so unregister it after our explicit destroy.
        models = getattr(juju_factory, "_models", None)
        if isinstance(models, dict):
            models.pop(model, None)
        juju.model = None


def print_results(results: list[dict]) -> None:
    print("\nCOS Proxy monitors fan-in benchmark")
    print(json.dumps(results, indent=2))


def selected_cases() -> set[str]:
    cases = {case.strip() for case in CASES.split(",") if case.strip()}
    if cases == {"both"}:
        return {"baseline", "candidate"}
    valid_cases = {"baseline", "candidate"}
    if not cases or not cases <= valid_cases:
        raise ValueError(
            "COS_PROXY_BENCHMARK_CASES must be one of: both, baseline, candidate, "
            "or a comma-separated combination of baseline,candidate"
        )
    return cases


def test_cos_proxy_monitors_fan_in_benchmark(juju_factory):
    cases = selected_cases()
    provider_charm = pack_charm(REPO_ROOT / "tests/manual/charms/monitors-provider", "monitors-provider")
    consumer_charm = pack_charm(REPO_ROOT / "tests/manual/charms/scrape-consumer", "scrape-consumer")

    results = []

    if "baseline" in cases:
        baseline_charm, baseline_label, baseline_channel, baseline_revision = baseline_deploy_spec()
        baseline = juju_factory.get_juju(suffix="baseline")
        try:
            baseline_result = run_case(
                baseline,
                label=f"sequential:{baseline_label}",
                cos_proxy_charm=baseline_charm,
                cos_proxy_channel=baseline_channel,
                cos_proxy_revision=baseline_revision,
                provider_charm=provider_charm,
                consumer_charm=consumer_charm,
            )
            results.append(baseline_result)
            print_results(results)
        finally:
            destroy_model(juju_factory, baseline)

    if "candidate" in cases:
        candidate_charm = pack_candidate()
        candidate = juju_factory.get_juju(suffix="candidate")
        candidate_result = run_case(
            candidate,
            label="batched:working-tree",
            cos_proxy_charm=candidate_charm,
            provider_charm=provider_charm,
            consumer_charm=consumer_charm,
        )
        results.append(candidate_result)
        print_results(results)

    results_by_case = {
        result["label"].split(":", 1)[0]: result
        for result in results
    }
    if batched_result := results_by_case.get("batched"):
        assert not batched_result["timed_out"]
    if (
        (sequential_result := results_by_case.get("sequential"))
        and (batched_result := results_by_case.get("batched"))
        and not sequential_result["timed_out"]
    ):
        assert sequential_result["scrape_jobs"] == batched_result["scrape_jobs"]
        assert sequential_result["alert_rules"] == batched_result["alert_rules"]
