# Gauntlet Loop contributor guide

Gauntlet Loop is a public Agent Skill (`SKILL.md`) plus a local CLI runner
(`scripts/gauntlet.py`) that routes planner, worker, critic and integrator phases to
Claude Code, Codex or TFCode. This repository is the source and release home; any
other copy of the skill (for example a shared skills checkout) is a consumer, not a
second release authority.

## Commands
- Validate before pushing: `scripts/validate.sh` (syntax, `SKILL.md` frontmatter,
  link/copy installs, fake-client routing tests). CI runs the same in
  `.github/workflows/validate.yml`.
- Runner tests alone: `python3 scripts/test_gauntlet.py`.

## Runtime rules (binding; keep them true in code, tests and docs)
- A runtime is launched only when it is named in the route. No hidden selection.
- Worker and critic must be different runtimes.
- Only the worker phase gets write access. Read-only phases are guarded by comparing
  Git state before and after.
- TFCode as worker requires an explicit opt-in to its `--auto` mode.
- Runs are sequential, bounded to one to five passes, and audited under the target
  project's `.gauntlet/runs/`.
- No shell interpolation; each CLI is a subprocess. No API keys or remote service.
- Tests use fake clients only; never spend tokens or call a live model in the suite.
- Public docs, and the danjohnson.au feature page, describe the actual routing, not
  just skill portability. Fresh CLI sessions share the repository and recorded text
  artifacts, never proprietary session state.

## Needs the maintainer's approval
Releases and tags, publishing, paid or live multi-client test runs, changes to the
runtime rules above, and changes to the danjohnson.au feature page (that lives in
the website repo).
