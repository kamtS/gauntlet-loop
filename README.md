# Gauntlet Loop

Gauntlet Loop turns a build brief into a ready-to-run orchestration prompt: independent workers build focused pieces, separate critics try to disprove that each piece is done, and the work is revised until it passes a concrete quality bar or reaches a hard iteration limit.

It is one portable [Agent Skill](https://agentskills.io/) that can be installed in Claude Code, Codex, and tfcode. The skill travels between those clients; it does **not** transfer live sessions, state, or tasks between their runtimes. The generated prompt executes only where the host supports agent delegation.

## What it adds

- Non-overlapping worker ownership instead of undirected agent fan-out
- Independent critics that fail work on evidence, not vibes
- Artifact-appropriate verification for code, interfaces, documents, data, and creative work
- A bounded revision loop that stops and escalates unresolved failures
- Final integration and end-to-end verification by the orchestrator

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

Pass `--copy` to opt out of that live-update behavior. Copy mode duplicates `SKILL.md`, `LICENSE`, and `agents/` into the destination and does not track this checkout afterward — it stays pinned to whatever was copied until you rerun the installer. Use `--copy` when you want to review and pin a specific version, or when the destination filesystem doesn't support symlinks.

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

## Use

Invoke the skill named `gauntlet-loop` from your agent host and include the work you want sharpened, for example:

```text
Use the gauntlet-loop skill to create an orchestration prompt for polishing this web app.
The bar is the supplied Figma design, passing tests, and no accessibility
violations. Cap each workstream at three review passes.
```

Exact invocation syntax depends on your host; consult its documentation for how it names and triggers installed skills. The skill returns a prompt you can review and run in a delegation-capable host. Better references produce better criticism: provide a design, test suite, rubric, example, or measurable definition of done when possible.

## Design boundaries

Gauntlet Loop is intentionally a prompt skill, not a multi-provider protocol. It does not require an API key, background service, framework, or model vendor. Hosts differ in how they expose subagents and concurrency, so the generated prompt describes roles and evidence without depending on client-specific commands.

Repeated review can consume substantial time and tokens. The skill defaults to three worker/critic passes and requires unresolved failures to be surfaced rather than hidden behind an endless loop.

## Origins

Gauntlet Loop's worker/critic/orchestrator pattern is inspired by Matt Shumer's "Claude of Duty" experiment, which used adversarial critique loops to drive iterative improvement. This repository is an independent, portable Agent Skill implementation of that pattern — it is not affiliated with or endorsed by that project.

## Contributing

Issues and focused pull requests are welcome. Keep the core skill client-neutral, concise, bounded, and testable. Validate changes with:

```bash
bash scripts/validate.sh
```

This runs shell syntax checks, `SKILL.md` frontmatter checks, and clean end-to-end link/copy installs (including conflict and foreign-symlink safety checks) against disposable temp directories — it makes no network calls and never touches your real skill directories. The same script runs in CI on every push and pull request.

By contributing, you agree that your contributions are licensed under the MIT License.

## License

[MIT](LICENSE)
