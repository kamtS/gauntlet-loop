#!/usr/bin/env python3
"""Run a bounded worker/critic loop across Claude Code, Codex, and TFCode."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


RUNTIMES = ("claude", "codex", "tfcode")
PHASES = ("planner", "worker", "critic", "integrator")
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
# The critic returned no usable verdict line: an empty artifact, a truncated or
# dead transcript, or tool narration that never ruled. This is not a FAIL and
# must not be fed back to the worker as if it were one.
VERDICT_UNPARSEABLE = "UNPARSEABLE"
EXIT_PASS_LIMIT = 3
EXIT_UNPARSEABLE = 4
DEFAULT_CRITIC_RETRIES = 1
DEFAULT_ROUTE = {
    "planner": "claude",
    "worker": "codex",
    "critic": "tfcode",
    "integrator": "claude",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    source: pathlib.Path
    project_dir: pathlib.Path
    task: str
    acceptance: str
    max_passes: int
    route: dict[str, str]
    models: dict[str, str]
    allow_tfcode_write: bool
    run_root: pathlib.Path
    critic_retries: int = DEFAULT_CRITIC_RETRIES


def read_config(path: pathlib.Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")

    base = path.parent.resolve()
    project_dir = pathlib.Path(raw.get("project_dir", "."))
    if not project_dir.is_absolute():
        project_dir = base / project_dir
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise ConfigError(f"project_dir is not a directory: {project_dir}")

    task = raw.get("task")
    task_file = raw.get("task_file")
    if bool(task) == bool(task_file):
        raise ConfigError("set exactly one of task or task_file")
    if task_file:
        task_path = pathlib.Path(task_file)
        if not task_path.is_absolute():
            task_path = base / task_path
        try:
            task = task_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read task_file {task_path}: {exc}") from exc
    if not isinstance(task, str) or not task.strip():
        raise ConfigError("task must be non-empty text")

    acceptance = raw.get("acceptance", "Meet the task brief and leave verification evidence.")
    if not isinstance(acceptance, str) or not acceptance.strip():
        raise ConfigError("acceptance must be non-empty text")

    max_passes = raw.get("max_passes", 3)
    if not isinstance(max_passes, int) or isinstance(max_passes, bool) or not 1 <= max_passes <= 5:
        raise ConfigError("max_passes must be an integer from 1 to 5")

    route = dict(DEFAULT_ROUTE)
    supplied_route = raw.get("route", {})
    if not isinstance(supplied_route, dict):
        raise ConfigError("route must be an object")
    unknown = sorted(set(supplied_route) - set(PHASES))
    if unknown:
        raise ConfigError(f"unknown route phase(s): {', '.join(unknown)}")
    route.update(supplied_route)
    for phase, runtime in route.items():
        if runtime not in RUNTIMES:
            raise ConfigError(f"route.{phase} must be one of: {', '.join(RUNTIMES)}")
    if route["worker"] == route["critic"]:
        raise ConfigError("worker and critic must use different runtimes")

    models = raw.get("models", {})
    if not isinstance(models, dict) or any(key not in RUNTIMES for key in models):
        raise ConfigError("models may only contain claude, codex, and tfcode")
    models = {key: value for key, value in models.items() if isinstance(value, str) and value}

    allow_tfcode_write = raw.get("allow_tfcode_write", False)
    if not isinstance(allow_tfcode_write, bool):
        raise ConfigError("allow_tfcode_write must be true or false")
    if route["worker"] == "tfcode" and not allow_tfcode_write:
        raise ConfigError(
            "TFCode worker mode requires allow_tfcode_write: true because it invokes tfcode --auto"
        )

    critic_retries = raw.get("critic_retries", DEFAULT_CRITIC_RETRIES)
    if (
        not isinstance(critic_retries, int)
        or isinstance(critic_retries, bool)
        or not 0 <= critic_retries <= 3
    ):
        raise ConfigError("critic_retries must be an integer from 0 to 3")

    run_root = pathlib.Path(raw.get("run_root", ".gauntlet/runs"))
    if not run_root.is_absolute():
        run_root = project_dir / run_root

    return Config(
        source=path.resolve(),
        project_dir=project_dir,
        task=task.strip(),
        acceptance=acceptance.strip(),
        max_passes=max_passes,
        route=route,
        models=models,
        allow_tfcode_write=allow_tfcode_write,
        run_root=run_root.resolve(),
        critic_retries=critic_retries,
    )


def binary_for(runtime: str) -> str:
    override = os.environ.get(f"GAUNTLET_{runtime.upper()}_BIN")
    return override or runtime


def command_for(
    runtime: str,
    phase: str,
    model: str | None,
    output_file: pathlib.Path | None = None,
) -> tuple[list[str], bool]:
    writable = phase == "worker"
    binary = binary_for(runtime)
    if runtime == "codex":
        command = [binary, "exec", "--ephemeral"]
        if writable:
            command.append("--approve-for-me")
        else:
            command.extend(["--sandbox", "read-only"])
        if model:
            command.extend(["--model", model])
        if output_file:
            command.extend(["--output-last-message", str(output_file)])
        command.append("-")
        return command, True
    if runtime == "claude":
        command = [binary, "-p", "--no-session-persistence", "--output-format", "text"]
        command.extend(["--permission-mode", "acceptEdits" if writable else "plan"])
        if model:
            command.extend(["--model", model])
        return command, True
    command = [binary, "run", "--format", "json"]
    if writable:
        command.append("--auto")
    if model:
        command.extend(["--model", model])
    return command, False


def display_command(command: list[str], stdin_prompt: bool) -> str:
    rendered = " ".join(json.dumps(part) if re.search(r"\s", part) else part for part in command)
    return rendered + (" < prompt" if stdin_prompt else " <prompt appended>")


def git_state(project_dir: pathlib.Path) -> str | None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=project_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def parse_tfcode_events(stdout: str) -> tuple[str, list[str]]:
    """Return TFCode's joined message text and every line that was not a JSON event.

    Dropped lines are returned rather than silently discarded so the caller can
    record them: a stream that is mostly unparseable is evidence of a dead or
    truncated run, not of a verdict.
    """
    pieces: list[str] = []
    dropped: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            dropped.append(line)
            continue
        if not isinstance(event, dict):
            dropped.append(line)
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            pieces.append(part["text"])
        elif isinstance(event.get("text"), str):
            pieces.append(event["text"])
    text = "\n".join(pieces).strip()
    if not text and dropped:
        # No structured text at all: keep the raw output visible rather than
        # returning an empty string that hides what the client printed.
        text = stdout.strip()
    return text, dropped


def tfcode_text(stdout: str) -> str:
    return parse_tfcode_events(stdout)[0]


@dataclass(frozen=True)
class PhaseResult:
    output: str
    # Everything the client emitted, including earlier messages that the final
    # output omits. Used to find a verdict the final message did not repeat.
    transcript: str


def invoke(
    config: Config,
    phase: str,
    prompt: str,
    artifact: pathlib.Path,
    *,
    dry_run: bool,
) -> str:
    return invoke_phase(config, phase, prompt, artifact, dry_run=dry_run).output


def invoke_phase(
    config: Config,
    phase: str,
    prompt: str,
    artifact: pathlib.Path,
    *,
    dry_run: bool,
) -> PhaseResult:
    runtime = config.route[phase]
    command, use_stdin = command_for(runtime, phase, config.models.get(runtime), artifact if runtime == "codex" else None)
    if dry_run:
        print(f"{phase:10} {runtime:7} {display_command(command, use_stdin)}")
        return PhaseResult("", "")

    # Make the expected audit artifact visible to Git before the read-only
    # snapshot. Rewriting an already-untracked artifact then leaves porcelain
    # state unchanged, while client edits elsewhere are still detected.
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.touch(exist_ok=True)
    before = git_state(config.project_dir) if phase != "worker" else None
    run_command = command if use_stdin else [*command, prompt]
    result = subprocess.run(
        run_command,
        cwd=config.project_dir,
        input=prompt if use_stdin else None,
        # Clients that take the prompt as an argument must not inherit the
        # caller's stdin: an open pipe there can block a client forever.
        stdin=None if use_stdin else subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{phase} ({runtime}) failed with exit {result.returncode}:\n{result.stderr.strip()}"
        )
    dropped: list[str] = []
    if runtime == "codex":
        # --output-last-message holds only the final message. The artifact was
        # touched above, so an exit-0 run that never wrote it reads as empty;
        # fall back to stdout instead of silently scoring an empty string.
        output = artifact.read_text(encoding="utf-8").strip() if artifact.exists() else ""
        if not output:
            output = result.stdout.strip()
        # Codex streams earlier agent messages on stderr and stdout.
        transcript = "\n".join(part for part in (result.stderr, result.stdout, output) if part)
    elif runtime == "tfcode":
        output, dropped = parse_tfcode_events(result.stdout)
        transcript = output
    else:
        output = result.stdout.strip()
        transcript = output
    artifact.write_text(output + "\n", encoding="utf-8")

    if phase != "worker" and before is not None:
        after = git_state(config.project_dir)
        if after != before:
            raise RuntimeError(
                f"read-only {phase} ({runtime}) changed the repository; inspect {artifact} and restore deliberately"
            )
    # Written after the read-only guard so the audit file itself is not
    # mistaken for a client edit.
    if dropped:
        dropped_path = artifact.with_name(artifact.stem + ".dropped.log")
        dropped_path.write_text("\n".join(dropped) + "\n", encoding="utf-8")
        print(
            f"warning: {phase} ({runtime}) emitted {len(dropped)} non-JSON line(s); kept in {dropped_path}",
            file=sys.stderr,
        )
    return PhaseResult(output, transcript)


def common_context(config: Config, run_dir: pathlib.Path) -> str:
    return f"""PROJECT
{config.project_dir}

TASK
{config.task}

ACCEPTANCE BAR
{config.acceptance}

RUN ARTIFACTS
{run_dir}
"""


VERDICT_LINE = re.compile(r"(?im)^VERDICT:\s*(PASS|FAIL)\s*$")


def verdict(text: str) -> str:
    """Return PASS, FAIL, or UNPARSEABLE for a critic transcript.

    A missing verdict line is never treated as FAIL: an empty artifact or a
    transcript cut off mid-sentence says nothing about the work under review.
    """
    if not text or not text.strip():
        return VERDICT_UNPARSEABLE
    matches = VERDICT_LINE.findall(text)
    return matches[-1].upper() if matches else VERDICT_UNPARSEABLE


def critic_verdict(result: PhaseResult) -> str:
    """Prefer the verdict in the final output, then any earlier message."""
    final = verdict(result.output)
    if final != VERDICT_UNPARSEABLE:
        return final
    return verdict(result.transcript)


def run(config: Config, *, dry_run: bool) -> pathlib.Path | None:
    if dry_run:
        print("Resolved Gauntlet route (no clients launched):")
        for phase in PHASES:
            output = pathlib.Path("<run-dir>") / f"{phase}.md"
            invoke(config, phase, "", output, dry_run=True)
        print(f"max passes: {config.max_passes}")
        print(f"critic retries on unparseable verdict: {config.critic_retries}")
        print("TFCode used: " + ", ".join(p for p in PHASES if config.route[p] == "tfcode") if "tfcode" in config.route.values() else "TFCode used: never")
        return None

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = config.run_root / f"{stamp}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot = {
        "project_dir": str(config.project_dir),
        "task": config.task,
        "acceptance": config.acceptance,
        "max_passes": config.max_passes,
        "route": config.route,
        "models": config.models,
        "allow_tfcode_write": config.allow_tfcode_write,
        "critic_retries": config.critic_retries,
    }
    (run_dir / "config.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    context = common_context(config, run_dir)

    planner_prompt = context + """
ROLE: PLANNER (READ ONLY)
Inspect the repository and write a concrete implementation plan. Identify constraints, likely failure modes,
verification commands, and the smallest coherent implementation. Do not edit files.
"""
    plan_path = run_dir / "01-plan.md"
    print(f"planner: {config.route['planner']}")
    invoke(config, "planner", planner_prompt, plan_path, dry_run=False)

    critic_output = "No critique yet."
    final_verdict = VERDICT_FAIL
    completed_passes = 0
    critic_attempts = 0
    for pass_number in range(1, config.max_passes + 1):
        completed_passes = pass_number
        worker_path = run_dir / f"{pass_number + 1:02d}-worker-pass-{pass_number}.md"
        worker_prompt = context + f"""
ROLE: WORKER (WRITE ENABLED), PASS {pass_number} OF {config.max_passes}
Read the plan at {plan_path}.
The previous critic report is included below. Implement or revise the task in the project repository.
Preserve unrelated user changes. Run appropriate verification and report changed files, commands, and results.

PREVIOUS CRITIC REPORT
{critic_output}
"""
        print(f"worker pass {pass_number}: {config.route['worker']}")
        invoke(config, "worker", worker_prompt, worker_path, dry_run=False)

        critic_prompt = context + f"""
ROLE: INDEPENDENT CRITIC (READ ONLY), PASS {pass_number} OF {config.max_passes}
Inspect the actual repository and the worker report at {worker_path}. Try to disprove completion against the
acceptance bar. Run read-only checks and tests where safe. Report concrete findings with evidence and required
fixes. End with exactly one line: VERDICT: PASS or VERDICT: FAIL. Do not edit files.
"""
        # An unparseable critic run is retried against the same worker output;
        # it never consumes a worker pass or reaches the worker as a FAIL.
        for attempt in range(config.critic_retries + 1):
            suffix = f"-retry-{attempt}" if attempt else ""
            critic_path = run_dir / f"{pass_number + 1:02d}-critic-pass-{pass_number}{suffix}.md"
            label = f" (retry {attempt} after unparseable verdict)" if attempt else ""
            print(f"critic pass {pass_number}: {config.route['critic']}{label}")
            critic_attempts += 1
            critic_result = invoke_phase(config, "critic", critic_prompt, critic_path, dry_run=False)
            final_verdict = critic_verdict(critic_result)
            if final_verdict != VERDICT_UNPARSEABLE:
                break
            print(
                f"warning: critic pass {pass_number} produced no VERDICT line; see {critic_path}",
                file=sys.stderr,
            )
        critic_output = critic_result.output
        if final_verdict != VERDICT_FAIL:
            break

    integration_path = run_dir / "99-integration.md"
    integration_prompt = context + f"""
ROLE: INTEGRATOR (READ ONLY)
Inspect the final repository, the plan at {plan_path}, and every artifact in {run_dir}. Reconcile the result,
run end-to-end verification, and report whether the acceptance bar is met. Do not edit files. State residual
risks and unresolved findings. The critic's final verdict was {final_verdict} after {completed_passes} pass(es).
"""
    print(f"integrator: {config.route['integrator']}")
    invoke(config, "integrator", integration_prompt, integration_path, dry_run=False)
    status = {
        "critic_verdict": final_verdict,
        "passes": completed_passes,
        "critic_attempts": critic_attempts,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (run_dir / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    print(f"run artifacts: {run_dir}")
    if final_verdict == VERDICT_UNPARSEABLE:
        print(
            "gauntlet stopped: the critic returned no parseable VERDICT line after "
            f"{config.critic_retries + 1} attempt(s); inspect the critic artifacts before trusting or rerunning",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_UNPARSEABLE)
    if final_verdict != VERDICT_PASS:
        print("gauntlet stopped at its pass limit with unresolved findings", file=sys.stderr)
        raise SystemExit(EXIT_PASS_LIMIT)
    return run_dir


def doctor(config: Config) -> int:
    missing = False
    for runtime in sorted(set(config.route.values())):
        binary = binary_for(runtime)
        resolved = shutil.which(binary) if os.sep not in binary else (binary if pathlib.Path(binary).is_file() else None)
        if resolved:
            print(f"ok      {runtime:7} {resolved}")
        else:
            print(f"missing {runtime:7} {binary}")
            missing = True
    return 1 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "route", "run"):
        child = subparsers.add_parser(name)
        child.add_argument("config", type=pathlib.Path)
    args = parser.parse_args()
    try:
        config = read_config(args.config.resolve())
        if args.command == "doctor":
            return doctor(config)
        if args.command == "route":
            run(config, dry_run=True)
            return 0
        run(config, dry_run=False)
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"run error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
