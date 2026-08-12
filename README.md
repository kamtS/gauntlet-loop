# Gauntlet Loop

Gauntlet Loop routes a bounded build-and-review cycle across Claude Code, Codex, and TFCode: one client plans, another implements, an independent client tries to disprove completion, and the work is revised until it passes a concrete quality bar or reaches a hard iteration limit.

It is both a portable [Agent Skill](https://agentskills.io/) and a local, auditable CLI orchestrator. The skill can still generate a client-neutral prompt, but runtime mode performs real handoffs through the three installed command-line clients and stores every prompt and response as a run artifact.

## What it adds

- Non-overlapping worker ownership instead of undirected agent fan-out
- Independent critics that fail work on evidence, not vibes
- Artifact-appropriate verification for code, interfaces, documents, data, and creative work
- A bounded revision loop that stops and escalates unresolved failures
- Final integration and end-to-end verification by the orchestrator
- Explicit per-phase runtime routing: decide exactly when TFCode is and is not used
- Local handoffs through installed CLIs; no API keys or orchestration service

## Install

Clone the repository, then run the installer from its root:

```bash
git clone https://github.com/kamtS/gauntlet-loop.git
cd gauntlet-loop
./scripts/install.sh all
```

The default install uses symlinks, so a later `git pull` updates every client immediately — see [Trust model](#trust-model-symlinks-vs-copies) below before relying on that. Install one client with `claude`, `codex`, or `tfcode` instead of `all`:

```bash
./scripts/install.sh codex
```

Default destinations are:

| Client | Destination | Override |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/gauntlet-loop` | `CLAUDE_SKILLS_DIR` |
| Codex | `~/.codex/skills/gauntlet-loop` | `CODEX_SKILLS_DIR` |
| tfcode | `~/.claude/skills/gauntlet-loop` | `TFCODE_SKILLS_DIR` |

Codex's default is `~/.codex/skills` because that is where this installation was actually discovered by a live Codex session. tfcode is installed through its Claude-compatible fallback at `~/.claude/skills`, which is the behavior used to validate this release; TF Code's public documentation does not currently state a discovery path. If your TFCode installation documents a native directory, set `TFCODE_SKILLS_DIR` to that path (for example, `~/.tfcode/skill`). When `tfcode` and `claude` resolve to the same destination (the default), `all` installs it once and reports it as shared for both — this is expected, not a conflict.

Override a base directory with `CLAUDE_SKILLS_DIR`, `CODEX_SKILLS_DIR`, or `TFCODE_SKILLS_DIR`. Use `--copy` where symlinks are unsuitable:

```bash
./scripts/install.sh --copy all
```

The installer preflights every selected client's destination before making any change and reports every conflict it finds at once, not just the first one. If any destination conflicts, nothing is installed for any client in that run — there is no partial `all` install. It refuses to overwrite an existing installation and refuses to redirect a symlink that points somewhere other than this checkout (including symlinks pointing to unrelated locations). To install manually, place or link this repository at the destination for your client.

### Trust model: symlinks vs. copies

The default `link` mode makes the installed skill a symlink back to this checkout. That means every client using it runs whatever is currently on disk here — a `git pull`, a local edit, or a checkout of a different branch all take effect immediately, for every linked client, with no separate approval step. Only use link mode against a checkout you trust and control.

Pass `--copy` to opt out of that live-update behavior. Copy mode duplicates `SKILL.md`, `LICENSE`, `agents/`, `scripts/`, and `examples/` into the destination and does not track this checkout afterward — it stays pinned to whatever was copied until you rerun the installer. Use `--copy` when you want to review and pin a specific version, or when the destination filesystem doesn't support symlinks.

### Uninstall

Symlink installs: remove the symlink itself; this checkout is untouched.

```bash
rm ~/.claude/skills/gauntlet-loop
rm ~/.codex/skills/gauntlet-loop
rm ~/.tfcode/skill/gauntlet-loop   # only if you installed a native tfcode copy
```

Copy installs: remove the copied directory.

```bash
rm -r ~/.claude/skills/gauntlet-loop
rm -r ~/.codex/skills/gauntlet-loop
```

Adjust the paths above if you installed with `CLAUDE_SKILLS_DIR`, `CODEX_SKILLS_DIR`, or `TFCODE_SKILLS_DIR` overrides. Removing a shared destination (the tfcode default) uninstalls it for both clients that pointed at it.

## Route and run

Copy [the example config](examples/gauntlet.json) and set the project, task, acceptance bar, pass limit, and route:

```json
{
  "project_dir": "/path/to/project",
  "task": "Implement the requested change and prove it works.",
  "acceptance": "Tests pass and no unrelated files change.",
  "max_passes": 3,
  "route": {
    "planner": "claude",
    "worker": "codex",
    "critic": "tfcode",
    "integrator": "claude"
  }
}
```

That policy launches TFCode only for criticism. Change `critic` to `claude` or `codex` and TFCode is never invoked. Any phase may use any supported runtime, with two guardrails: worker and critic must differ, and a TFCode worker requires `"allow_tfcode_write": true` because TFCode's non-interactive write mode uses `--auto`.

Preview the exact client route and commands, check that its required CLIs are installed, then run:

```bash
python3 scripts/gauntlet.py route gauntlet.json
python3 scripts/gauntlet.py doctor gauntlet.json
python3 scripts/gauntlet.py run gauntlet.json
```

The runner uses no shell interpolation. It invokes each local CLI as a subprocess, gives write permissions only to the worker phase, caps loops at five passes, and stores the config snapshot, phase prompts/responses, verdict, and final integration report under `<project>/.gauntlet/runs/`. Read-only phases are also guarded by a before/after Git-state check.

Binary paths can be overridden with `GAUNTLET_CLAUDE_BIN`, `GAUNTLET_CODEX_BIN`, and `GAUNTLET_TFCODE_BIN`. Optional client models belong in a `models` object keyed by runtime. Relative config paths resolve from the config file's directory.

Client invocations can consume substantial tokens and may incur provider charges. `route` and `doctor` do not invoke a model; `run` does. Review the resolved route before each live run.

## Prompt-only use

Invoke the skill named `gauntlet-loop` from your agent host and ask for prompt mode:

```text
Use the gauntlet-loop skill in prompt mode to create an orchestration prompt for polishing this web app.
The bar is the supplied Figma design, passing tests, and no accessibility
violations. Cap each workstream at three review passes.
```

Exact skill invocation syntax depends on your host. Better references produce better criticism: provide a design, test suite, rubric, example, or measurable definition of done when possible.

## Security and design boundaries

Runtime mode is a deterministic local coordinator, not a background service or remote protocol. It relies on the authentication already configured in each CLI. Codex phases use its read-only or workspace-write sandbox; Claude phases use plan or accept-edits permissions. TFCode is read-only by default, and its `--auto` mode is reachable only through the explicit TFCode-worker opt-in. These controls reduce risk but do not make model-generated commands intrinsically safe: use a clean branch or worktree and review the resulting diff.

The runner is sequential and repository-scoped. It hands text artifacts and the shared working tree between fresh client invocations; it does not transfer proprietary session state, hidden reasoning, account credentials, or conversations. It deliberately does not run independent writers concurrently against the same checkout.

Repeated review can consume substantial time and tokens. The skill defaults to three worker/critic passes and requires unresolved failures to be surfaced rather than hidden behind an endless loop.

## Origins

Gauntlet Loop's worker/critic/orchestrator pattern is inspired by Matt Shumer's "Claude of Duty" experiment, which used adversarial critique loops to drive iterative improvement. This repository is an independent, portable Agent Skill implementation of that pattern — it is not affiliated with or endorsed by that project.

## Contributing

Issues and focused pull requests are welcome. Keep the core skill client-neutral, concise, bounded, and testable. Validate changes with:

```bash
bash scripts/validate.sh
```

This runs shell/Python syntax checks, `SKILL.md` frontmatter checks, clean end-to-end link/copy installs, and fake-client routing tests proving when TFCode is and is not launched. It uses disposable temp directories, makes no network calls, and never touches your real skill directories. The same script runs in CI on every push and pull request.

By contributing, you agree that your contributions are licensed under the MIT License.

## License

[MIT](LICENSE)
