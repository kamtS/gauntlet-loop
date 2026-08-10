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

The default install uses symlinks, so a later `git pull` updates every client. Install one client with `claude`, `codex`, or `tfcode` instead of `all`:

```bash
./scripts/install.sh codex
```

Default destinations are:

| Client | Destination |
| --- | --- |
| Claude Code | `~/.claude/skills/gauntlet-loop` |
| Codex | `~/.agents/skills/gauntlet-loop` |
| tfcode | `~/.tfcode/skill/gauntlet-loop` |

Override a base directory with `CLAUDE_SKILLS_DIR`, `CODEX_SKILLS_DIR`, or `TFCODE_SKILLS_DIR`. Use `--copy` where symlinks are unsuitable:

```bash
./scripts/install.sh --copy all
```

The installer preflights every selected client before making changes. It refuses to overwrite an existing installation or redirect a symlink to another checkout. To install manually, place or link this repository at the destination for your client.

## Use

Invoke the skill by name and include the work you want sharpened:

```text
Use $gauntlet-loop to create an orchestration prompt for polishing this web app.
The bar is the supplied Figma design, passing tests, and no accessibility
violations. Cap each workstream at three review passes.
```

The skill returns a prompt you can review and run in a delegation-capable host. Better references produce better criticism: provide a design, test suite, rubric, example, or measurable definition of done when possible.

## Design boundaries

Gauntlet Loop is intentionally a prompt skill, not a multi-provider protocol. It does not require an API key, background service, framework, or model vendor. Hosts differ in how they expose subagents and concurrency, so the generated prompt describes roles and evidence without depending on client-specific commands.

Repeated review can consume substantial time and tokens. The skill defaults to three worker/critic passes and requires unresolved failures to be surfaced rather than hidden behind an endless loop.

## Contributing

Issues and focused pull requests are welcome. Keep the core skill client-neutral, concise, bounded, and testable. Validate changes with:

```bash
python3 /path/to/skill-creator/scripts/quick_validate.py .
bash -n scripts/install.sh
```

By contributing, you agree that your contributions are licensed under the MIT License.

## License

[MIT](LICENSE)
