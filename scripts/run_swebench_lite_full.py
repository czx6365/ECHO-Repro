from __future__ import annotations

"""Batch runner: run the ECHO-Repro pipeline over all SWE-bench Lite instances.

Design notes:
- One CLI subprocess per instance (crash isolation, identical semantics to
  ``run-swebench-one``, and prep failures still write their result.json record).
- Resume by default: instances whose ``outputs/<id>/result.json`` already exists
  are skipped.
- Instances are interleaved round-robin across repos so concurrent workers
  rarely touch the same repo cache at the same time.
- Per-instance workdirs under ``repos/<id>`` are deleted after the run to keep
  disk usage bounded; the repo cache under ``repos/cache`` is kept.
- Systemic-failure guard: if ``--max-consecutive-infra-errors`` runs in a row
  finish as repo_error or without a result record (network/LLM outage), the
  batch pauses for ``--pause-seconds`` and probes git connectivity, resuming
  when the network recovers; after ``--max-pauses`` failed pauses it aborts
  instead of silently burning through every instance.
"""

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INFRA_CATEGORIES = {"repo_error", "no_record"}


def load_instances(instances_file: Path) -> list[dict]:
    instances: list[dict] = []
    for line in instances_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        instances.append(json.loads(line))
    return instances


def interleave_by_repo(instances: list[dict]) -> list[dict]:
    """Round-robin across repos so adjacent entries rarely share a repo."""
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for instance in instances:
        by_repo[str(instance.get("repo", ""))].append(instance)
    ordered: list[dict] = []
    while by_repo:
        for repo in sorted(by_repo):
            if by_repo[repo]:
                ordered.append(by_repo[repo].pop(0))
        by_repo = {repo: items for repo, items in by_repo.items() if items}
    return ordered


def has_result(output_root: Path, instance_id: str) -> bool:
    return (Path(output_root) / instance_id / "result.json").is_file()


def read_failure_category(output_root: Path, instance_id: str) -> str | None:
    result_path = Path(output_root) / instance_id / "result.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    final = payload.get("final_result") or {}
    return str(final.get("failure_category") or "")


def run_one(
    *,
    instance: dict,
    index: int,
    total: int,
    args: argparse.Namespace,
    lock: threading.Lock,
    log_path: Path,
    abort_event: threading.Event,
) -> dict:
    instance_id = str(instance["instance_id"])
    repo = str(instance.get("repo", ""))
    status = "ABORTED"
    elapsed = 0.0
    tail = ""

    if not abort_event.is_set():
        # A fetch killed mid-flight (e.g. cmd timeout) can leave a stale
        # git shallow.lock in the shared repo cache; every later instance of
        # that repo then fails repo prep in seconds. Thanks to the no-same-
        # repo-in-flight rule no other worker can be fetching this repo
        # right now, so any lock present here is safe to move aside.
        cache_slug = Path(args.cache_dir) / repo.replace("/", "__")
        for stale in cache_slug.glob(".git/*.lock"):
            try:
                stale.rename(stale.with_suffix(stale.suffix + ".stale"))
            except OSError:
                pass
        start = time.monotonic()
        cmd = [
            str(args.python),
            "-m",
            "echo_repro.cli",
            "run-swebench-one",
            "--instances-file",
            str(args.instances_file),
            "--instance-id",
            instance_id,
            "--workdir",
            str(args.workdir),
            "--cache-dir",
            str(args.cache_dir),
            "--env-root",
            str(args.env_root),
            "--output-root",
            str(args.output_root),
            "--llm",
            args.llm,
            "--max-attempts",
            str(args.max_attempts),
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        # Line-buffer the worker's stdout so per-instance log files stay
        # live-readable and TIMEOUT tails are not empty.
        env["PYTHONUNBUFFERED"] = "1"
        # Default LLM read timeout is 60s, too short for deepseek-v4-pro[1m]
        # on long SWE-bench prompts (first response routinely exceeds it).
        env["ECHO_REPRO_LLM_TIMEOUT"] = str(args.llm_timeout)
        # Default max_tokens is 8192; the reasoning model spends budget on
        # thinking blocks and can end with stop_reason='max_tokens' and no
        # text on long prompts. Give the answer block headroom.
        env["ECHO_REPRO_LLM_MAX_TOKENS"] = str(args.llm_max_tokens)

        # Redirect the worker's stdout/stderr to a per-instance file instead of
        # pipes: capture_output=True blocks in communicate() until the pipe
        # EOFs, and a grandchild of the CLI (lingering pip/git helper) can keep
        # the write end open after the CLI exits - the run_one future then
        # never completes and the as_completed loop wedges (observed twice).
        instance_log = Path(args.output_root) / "_instance_logs" / f"{instance_id}.log"
        instance_log.parent.mkdir(parents=True, exist_ok=True)
        offset = instance_log.stat().st_size if instance_log.exists() else 0
        try:
            with instance_log.open("ab") as handle:
                proc = subprocess.run(
                    cmd,
                    cwd=ROOT,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout_per_instance,
                )
            elapsed = time.monotonic() - start
            status = f"rc={proc.returncode}"
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            status = "TIMEOUT"
        with instance_log.open("rb") as handle:
            handle.seek(offset)
            tail = handle.read().decode("utf-8", errors="replace")[-800:]

    category = read_failure_category(args.output_root, instance_id)
    if category is None:
        if status.startswith("rc="):
            status = f"{status} NO_RECORD"
        category = "no_record"

    # Reclaim disk: the instance workdir is a full copy of the repo cache.
    workdir = Path(args.workdir) / instance_id
    if workdir.exists() and not args.keep_workdirs:
        shutil.rmtree(workdir, ignore_errors=True)

    tail = " ".join(tail.split())
    with lock:
        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"[{index}/{total}] {instance_id} ({repo}) {status} cat={category or '-'} "
            f"{elapsed:.0f}s"
        )
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            if tail:
                handle.write(tail[:600] + "\n")
    return {
        "instance_id": instance_id,
        "repo": repo,
        "status": status,
        "category": category,
        "elapsed_s": round(elapsed, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances-file", type=Path, default=ROOT / "data" / "swebench_lite.jsonl")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs")
    parser.add_argument("--workdir", type=Path, default=ROOT / "repos")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "repos" / "cache")
    parser.add_argument("--env-root", type=Path, default=ROOT / "envs")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv" / "bin" / "python")
    parser.add_argument("--llm", default="anthropic")
    parser.add_argument("--llm-timeout", type=int, default=300, help="Per-LLM-call read timeout in seconds.")
    parser.add_argument("--llm-max-tokens", type=int, default=32768, help="max_tokens sent to the LLM.")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout-per-instance", type=int, default=3600)
    parser.add_argument("--max-consecutive-infra-errors", type=int, default=6)
    parser.add_argument("--pause-seconds", type=int, default=300, help="Pause length after consecutive infra errors.")
    parser.add_argument("--max-pauses", type=int, default=4, help="Give up and abort after this many failed pauses.")
    parser.add_argument("--instance-ids", default="", help="Comma-separated subset filter.")
    parser.add_argument("--no-resume", action="store_true", help="Rerun instances even if a result.json exists.")
    parser.add_argument(
        "--keep-workdirs",
        action="store_true",
        help="Do not delete per-instance workdirs after each run.",
    )
    parser.add_argument(
        "--watchdog-minutes",
        type=int,
        default=75,
        help="Exit the driver if no instance completes for this long (awake time).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and first commands, then exit.")
    args = parser.parse_args()

    instances = load_instances(args.instances_file)
    if args.instance_ids:
        requested = {value.strip() for value in args.instance_ids.split(",") if value.strip()}
        instances = [instance for instance in instances if instance["instance_id"] in requested]
    instances = interleave_by_repo(instances)

    todo = []
    skipped = []
    for instance in instances:
        instance_id = str(instance["instance_id"])
        if not args.no_resume and has_result(args.output_root, instance_id):
            skipped.append(instance_id)
        else:
            todo.append(instance)
    print(f"total={len(instances)} todo={len(todo)} skipped_existing={len(skipped)}", flush=True)

    if args.dry_run:
        for instance in todo[:5]:
            print("  would run:", instance["instance_id"], instance["repo"])
        print("dry-run done")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(args.output_root) / f"_batch_run_{stamp}.log"
    lock = threading.Lock()
    abort_event = threading.Event()
    infra_streak = 0
    pause_count = 0
    results: list[dict] = []
    start_all = time.monotonic()
    total_n = len(todo)
    queue: list[dict] = list(todo)
    pending: dict = {}

    def probe_git_connectivity() -> bool:
        repo = str(queue[0].get("repo", "pallets/flask")) if queue else "pallets/flask"
        url = f"https://github.com/{repo}.git"
        try:
            proc = subprocess.run(
                ["git", "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=15", "ls-remote", url, "HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        inflight_repos: set[str] = set()

        def submit_next() -> None:
            """Pop the next queued instance whose repo isn't already in flight.

            Two workers fetching the same repo cache at once collide on
            git's shallow.lock and fail in seconds (repo_error). Skipping
            same-repo entries at submit time avoids that; skipped entries
            stay in the queue for a later round.
            """
            nonlocal inflight_repos
            if not queue:
                return
            for offset, candidate in enumerate(queue):
                if str(candidate.get("repo", "")) not in inflight_repos:
                    break
            else:
                return
            instance = queue.pop(offset)
            index = total_n - len(queue)
            inflight_repos.add(str(instance.get("repo", "")))
            pending[pool.submit(
                run_one,
                instance=instance,
                index=index,
                total=total_n,
                args=args,
                lock=lock,
                log_path=log_path,
                abort_event=abort_event,
            )] = instance

        # Watchdog: if nothing completes for a long stretch of awake time the
        # driver is wedged (observed once: 99% CPU spin). Start it BEFORE the
        # initial wave so it also covers submission-time spins. Per-instance
        # timeout caps legit gaps at 60 min, so 75 is safe.
        last_progress = [time.monotonic()]

        def watchdog() -> None:
            while True:
                time.sleep(60)
                if time.monotonic() - last_progress[0] > args.watchdog_minutes * 60:
                    print(
                        f"[watchdog] no completion for {args.watchdog_minutes} min - "
                        "driver wedged, exiting",
                        flush=True,
                    )
                    os._exit(3)

        threading.Thread(target=watchdog, daemon=True, name="watchdog").start()

        # Submit the initial wave. submit_next() may decline when every
        # remaining queued instance shares a repo with one already in flight
        # (e.g. a rerun list with only two distinct repos); break instead of
        # spinning - later completions free those repos and the main loop
        # refills the worker slots.
        while len(pending) < args.workers and queue:
            before = len(pending)
            submit_next()
            if len(pending) == before:
                break

        done = 0
        while pending:
            snapshot = list(pending)
            for future in as_completed(snapshot):
                result = future.result()
                del pending[future]
                inflight_repos.discard(str(result["repo"]))
                results.append(result)
                done += 1
                last_progress[0] = time.monotonic()
                if result["category"] in INFRA_CATEGORIES:
                    infra_streak += 1
                else:
                    infra_streak = 0
                if infra_streak >= args.max_consecutive_infra_errors and not abort_event.is_set():
                    print(
                        f"[guard] {infra_streak} consecutive infra errors - pausing "
                        f"{args.pause_seconds}s and probing connectivity",
                        flush=True,
                    )
                    time.sleep(args.pause_seconds)
                    if probe_git_connectivity():
                        print("[guard] connectivity restored - resuming", flush=True)
                        infra_streak = 0
                        pause_count = 0
                    else:
                        pause_count += 1
                        print(f"[guard] still down (pause {pause_count}/{args.max_pauses})", flush=True)
                        if pause_count >= args.max_pauses:
                            print("[guard] giving up after repeated pauses - aborting remaining", flush=True)
                            abort_event.set()
                            queue.clear()
                elapsed_all = time.monotonic() - start_all
                avg = elapsed_all / done
                eta_min = avg * (len(queue) + len(pending)) / 60
                print(
                    f"[progress] {done}/{total_n} done, avg {avg:.0f}s/instance, "
                    f"eta ~{eta_min:.0f} min remaining, infra_streak={infra_streak}",
                    flush=True,
                )
                if queue and not abort_event.is_set():
                    submit_next()
                # Workers submitted above are not in this as_completed
                # snapshot, so their completions would go unprocessed (and
                # their slots stay unfilled) until every future in the
                # snapshot finishes. If one future runs long (LLM hang
                # capped at timeout-per-instance), that idles the other
                # slots. Break so the outer loop re-snapshots and picks
                # the new futures up promptly.
                if len(pending) > len(snapshot) - 1:
                    break

    summary = {
        "started": stamp,
        "log": str(log_path),
        "total": len(instances),
        "skipped_existing": len(skipped),
        "ran": len(results),
        "aborted_by_guard": abort_event.is_set(),
        "by_category": {
            category: sum(1 for r in results if r["category"] == category)
            for category in sorted({r["category"] for r in results})
        },
    }
    (Path(args.output_root) / f"_batch_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    print(
        "Next: python scripts/summarize_swebench_experiment.py "
        "--instances-file data/swebench_lite.jsonl --outputs-dir outputs "
        "--output-md outputs/swebench_lite_full_summary.md "
        "--output-csv outputs/swebench_lite_full_summary.csv",
        flush=True,
    )


if __name__ == "__main__":
    main()
