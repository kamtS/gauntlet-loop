#!/usr/bin/env python3
"""Run a bounded worker/critic loop across Claude Code, Codex, and TFCode."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, IO


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
# A phase, or a descendant that inherited its pipes, never closed stdout/stderr.
EXIT_PIPE_DRAIN = 5
DEFAULT_CRITIC_RETRIES = 1
# Long enough for a real client phase, short enough that a stuck pipe cannot
# hold the runner open for the rest of the session.
DEFAULT_PIPE_DRAIN_TIMEOUT_SECONDS = 1800
DEFAULT_ROUTE = {
    "planner": "claude",
    "worker": "codex",
    "critic": "tfcode",
    "integrator": "claude",
}


class ConfigError(ValueError):
    pass


class PipeDrainTimeout(Exception):
    """A phase kept stdout or stderr open until the drain bound."""


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
    pipe_drain_timeout_seconds: float = DEFAULT_PIPE_DRAIN_TIMEOUT_SECONDS


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

    pipe_drain_timeout_seconds = raw.get(
        "pipe_drain_timeout_seconds", DEFAULT_PIPE_DRAIN_TIMEOUT_SECONDS
    )
    if (
        isinstance(pipe_drain_timeout_seconds, bool)
        or not isinstance(pipe_drain_timeout_seconds, (int, float))
        or not math.isfinite(pipe_drain_timeout_seconds)
        or pipe_drain_timeout_seconds <= 0
    ):
        raise ConfigError("pipe_drain_timeout_seconds must be a positive number of seconds")
    pipe_drain_timeout_seconds = float(pipe_drain_timeout_seconds)

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
        pipe_drain_timeout_seconds=pipe_drain_timeout_seconds,
    )


def binary_for(runtime: str) -> str:
    override = os.environ.get(f"GAUNTLET_{runtime.upper()}_BIN")
    return override or runtime


def claude_worker_options(project_dir: pathlib.Path, run_dir: pathlib.Path) -> list[str]:
    secret_paths = [
        "~/.ssh",
        "~/.aws",
        "~/.kube",
        "~/.azure",
        "~/.docker",
        "~/.config",
        "~/.claude",
        "~/.claude.json",
        "~/.codex",
        "~/Library/Keychains",
        *(str(project_dir / pattern) for pattern in (
            "**/.env", "**/.env.*", "**/.npmrc", "**/.netrc", "**/secrets", "**/*.pem", "**/*.key",
        )),
        "~/.npmrc",
        "~/.netrc",
        "~/.git-credentials",
        "~/.gitconfig",
    ]
    blocked_commands = [
        "git push *", "git * push *", "git * --force*", "git * -f", "git * -f *",
        "rm -rf*", "rm -fr*", "rm * -r* -f*", "rm * -f* -r*",
        "rm --recursive* --force*", "rm --force* --recursive*",
        "npm publish *", "npm dist-tag *", "npm run deploy*", "npm run publish*",
        "mvn *deploy*", "gradle *publish*", "./gradlew *publish*",
        *(f"{tool} *" for tool in (
            "aws", "az", "fly", "flyctl", "gcloud", "helm", "kubectl", "netlify",
            "terraform", "tofu", "vercel", "wrangler", "op", "security",
        )),
    ]
    denied = [
        *(f"Bash({rule})" for rule in blocked_commands),
        *(f"Read({path if path.startswith('~') else '/' + path})" for path in secret_paths),
        f"Edit(/{run_dir}/**)",
    ]
    # Keep CLI authentication available to Claude itself while hiding inherited
    # credentials from build scripts and their subprocesses.
    credential_pattern = re.compile(
        r"TOKEN|SECRET|PASSWORD|PASSPHRASE|AUTH|(?:API|ACCESS|PRIVATE)_KEY|CREDENTIAL|^SSH_|DATABASE_URL",
        re.I,
    )
    credential_names = sorted(
        name for name in os.environ
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name)
        and credential_pattern.search(name)
    )
    settings = {
        "env": {"npm_config_cache": str(project_dir / ".gauntlet" / "npm-cache")},
        "permissions": {"deny": denied, "blockReadsOutsideWorkingDirectories": True},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "filesystem": {"denyWrite": [str(run_dir)]},
            "network": {"allowedDomains": ["registry.npmjs.org:443"], "strictAllowlist": True},
            "credentials": {"envVars": [{"name": name, "mode": "deny"} for name in credential_names]},
        },
    }
    options = [
        "--restricted", "--strict-mcp-config", "--no-chrome", "--permission-prompts", "none",
        "--tools", "Bash,Read,Glob,Grep,Edit,Write",
        "--allowedTools",
        *(f"Bash({command} *)" for command in (
            "npm install", "npm ci", "npm test", "npm run",
            "make", "cmake", "ctest", "pytest", "python -m pytest", "python3 -m pytest",
            "go build", "go test", "cargo build", "cargo test", "cargo check",
            "dotnet build", "dotnet test", "gradle", "./gradlew", "mvn",
        )),
        "--settings", json.dumps(settings),
    ]
    if not run_dir.is_relative_to(project_dir):
        options.extend(["--add-dir", str(run_dir)])
    return options


def command_for(
    runtime: str,
    phase: str,
    model: str | None,
    output_file: pathlib.Path | None = None,
    *,
    project_dir: pathlib.Path,
    run_dir: pathlib.Path,
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
        if writable:
            command.extend(claude_worker_options(project_dir, run_dir))
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


def _close_stream(stream: IO[bytes] | None) -> None:
    if stream is None or stream.closed:
        return
    try:
        stream.close()
    except OSError:
        pass


def _feed_stdin(stdin: IO[bytes], payload: bytes) -> None:
    try:
        if payload:
            stdin.write(payload)
            try:
                stdin.flush()
            except OSError:
                pass
    except (BrokenPipeError, OSError):
        pass
    finally:
        _close_stream(stdin)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Stop the child and any descendant that stayed in its session."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except OSError:
            return


def run_client(
    command: list[str],
    *,
    cwd: pathlib.Path,
    prompt: str | None,
    timeout: float,
) -> tuple[int, str, str, bool]:
    """Run a client and drain both pipes until EOF or `timeout` seconds.

    `subprocess.run` waits for EOF with no bound, so a child (or a descendant
    that inherited the pipes) can stall the runner forever. On timeout the
    process group is killed; if it cannot be reaped the pipes are closed and
    the child is abandoned. Partial output captured before the bound is kept.
    """
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    feeder = None
    if prompt is not None and proc.stdin is not None:
        feeder = threading.Thread(
            target=_feed_stdin,
            args=(proc.stdin, prompt.encode("utf-8")),
            daemon=True,
        )
        feeder.start()

    stdout_pipe = proc.stdout
    stderr_pipe = proc.stderr
    if stdout_pipe is None or stderr_pipe is None:
        raise RuntimeError("phase stdout and stderr were not captured")
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    streams: dict[int, tuple[IO[bytes], list[bytes]]] = {
        stdout_pipe.fileno(): (stdout_pipe, stdout_chunks),
        stderr_pipe.fileno(): (stderr_pipe, stderr_chunks),
    }
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while streams or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not streams:
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                break
            readable, _, _ = select.select(list(streams), [], [], min(remaining, 0.25))
            if not readable:
                continue
            for fd in readable:
                stream, chunks = streams[fd]
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if data:
                    chunks.append(data)
                    continue
                streams.pop(fd, None)
                _close_stream(stream)
        if timed_out:
            _kill_process_group(proc)
            for stream, _chunks in list(streams.values()):
                _close_stream(stream)
            streams.clear()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Still alive after SIGKILL (for example uninterruptible sleep).
                # The pipes are already closed, so the runner does not wait.
                pass
    finally:
        if feeder is not None:
            feeder.join(timeout=1)
        _close_stream(proc.stdout)
        _close_stream(proc.stderr)

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    returncode = proc.returncode if proc.returncode is not None else 1
    return returncode, stdout, stderr, timed_out


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
    command, use_stdin = command_for(
        runtime, phase, config.models.get(runtime),
        artifact if runtime == "codex" else None,
        project_dir=config.project_dir, run_dir=artifact.parent,
    )
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
    # Clients that take the prompt as an argument get no stdin pipe: an
    # inherited open stdin can block a client forever. `prompt is None` selects
    # that path inside run_client.
    returncode, stdout, stderr, timed_out = run_client(
        run_command,
        cwd=config.project_dir,
        prompt=prompt if use_stdin else None,
        timeout=config.pipe_drain_timeout_seconds,
    )
    if timed_out:
        partial = stdout.strip()
        artifact.write_text((partial + "\n") if partial else "", encoding="utf-8")
        drain_log = artifact.with_name(artifact.stem + ".pipe-drain.log")
        drain_log.write_text(
            "pipe drain timed out after "
            f"{config.pipe_drain_timeout_seconds:g}s\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
            encoding="utf-8",
        )
        status = {
            "outcome": "pipe_drain_timeout",
            "phase": phase,
            "runtime": runtime,
            "pipe_drain_timeout_seconds": config.pipe_drain_timeout_seconds,
            "partial_output": str(artifact),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        (artifact.parent / "status.json").write_text(
            json.dumps(status, indent=2) + "\n", encoding="utf-8"
        )
        raise PipeDrainTimeout(
            f"{phase} ({runtime}) timed out draining stdout and stderr after "
            f"{config.pipe_drain_timeout_seconds:g}s; the child was killed or abandoned "
            f"and partial output was kept in {artifact}"
        )
    if returncode != 0:
        raise RuntimeError(
            f"{phase} ({runtime}) failed with exit {returncode}:\n{stderr.strip()}"
        )
    dropped: list[str] = []
    if runtime == "codex":
        # --output-last-message holds only the final message. The artifact was
        # touched above, so an exit-0 run that never wrote it reads as empty;
        # fall back to stdout instead of silently scoring an empty string.
        output = artifact.read_text(encoding="utf-8").strip() if artifact.exists() else ""
        if not output:
            output = stdout.strip()
        # Codex streams earlier agent messages on stderr and stdout.
        transcript = "\n".join(part for part in (stderr, stdout, output) if part)
    elif runtime == "tfcode":
        output, dropped = parse_tfcode_events(stdout)
        transcript = output
    else:
        output = stdout.strip()
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
            output = config.run_root / "<run-id>" / f"{phase}.md"
            invoke(config, phase, "", output, dry_run=True)
        print(f"max passes: {config.max_passes}")
        print(f"critic retries on unparseable verdict: {config.critic_retries}")
        print(f"pipe drain timeout: {config.pipe_drain_timeout_seconds:g}s")
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
        "pipe_drain_timeout_seconds": config.pipe_drain_timeout_seconds,
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
Return that report in your final response. The runner records it in the run directory.

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
    except PipeDrainTimeout as exc:
        print(f"run error: {exc}", file=sys.stderr)
        return EXIT_PIPE_DRAIN
    except RuntimeError as exc:
        print(f"run error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
