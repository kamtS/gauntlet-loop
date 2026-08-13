#!/usr/bin/env python3

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
RUNNER = REPO / "scripts" / "gauntlet.py"


FAKE_CLIENT = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[0]).name
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
if runtime == "tfcode" and sys.argv:
    prompt = sys.argv[-1]
with open(os.environ["GAUNTLET_TEST_LOG"], "a", encoding="utf-8") as handle:
    handle.write(runtime + "\n")
is_critic = "INDEPENDENT CRITIC" in prompt
if os.environ.get("GAUNTLET_TEST_MUTATE_READONLY") == "1" and "PLANNER (READ ONLY)" in prompt:
    pathlib.Path("tracked.txt").write_text("mutated\n", encoding="utf-8")
answer = "VERDICT: " + os.environ.get("GAUNTLET_TEST_VERDICT", "PASS") if is_critic else runtime + " completed"
if runtime == "codex" and "--output-last-message" in sys.argv:
    output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    output.write_text(answer + "\n", encoding="utf-8")
elif runtime == "tfcode":
    print(json.dumps({"type": "text", "part": {"text": answer}}))
else:
    print(answer)
'''


class GauntletRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.project, check=True)
        (self.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.project, check=True)

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        fake = self.bin_dir / "fake-client"
        fake.write_text(FAKE_CLIENT, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        for runtime in ("claude", "codex", "tfcode"):
            (self.bin_dir / runtime).symlink_to(fake)
        self.log = self.root / "clients.log"
        self.env = dict(os.environ)
        self.env["GAUNTLET_TEST_LOG"] = str(self.log)
        self.env["GAUNTLET_CLAUDE_BIN"] = str(self.bin_dir / "claude")
        self.env["GAUNTLET_CODEX_BIN"] = str(self.bin_dir / "codex")
        self.env["GAUNTLET_TFCODE_BIN"] = str(self.bin_dir / "tfcode")

    def tearDown(self):
        self.temp.cleanup()

    def config(self, route=None, **extra):
        payload = {
            "project_dir": str(self.project),
            "task": "Exercise the runner.",
            "acceptance": "The fake critic passes.",
            "max_passes": 2,
            "route": route or {
                "planner": "claude",
                "worker": "codex",
                "critic": "tfcode",
                "integrator": "claude",
            },
            **extra,
        }
        path = self.root / "gauntlet.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def execute(self, command, config, env=None):
        return subprocess.run(
            [sys.executable, str(RUNNER), command, str(config)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env,
            check=False,
        )

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_tfcode_is_only_launched_for_mapped_phase(self):
        result = self.execute("run", self.config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["claude", "codex", "tfcode", "claude"])
        status_files = list((self.project / ".gauntlet" / "runs").glob("*/status.json"))
        self.assertEqual(len(status_files), 1)
        self.assertEqual(json.loads(status_files[0].read_text())["critic_verdict"], "PASS")

    def test_tfcode_is_never_launched_when_unmapped(self):
        route = {"planner": "claude", "worker": "codex", "critic": "claude", "integrator": "codex"}
        result = self.execute("run", self.config(route=route))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tfcode", self.calls())

    def test_route_preview_launches_no_clients(self):
        result = self.execute("route", self.config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TFCode used: critic", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_tfcode_worker_requires_explicit_write_opt_in(self):
        route = {"planner": "claude", "worker": "tfcode", "critic": "codex", "integrator": "claude"}
        result = self.execute("route", self.config(route=route))
        self.assertEqual(result.returncode, 2)
        self.assertIn("allow_tfcode_write", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_failure_revises_until_bounded_limit(self):
        env = dict(self.env)
        env["GAUNTLET_TEST_VERDICT"] = "FAIL"
        result = self.execute("run", self.config(), env=env)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.calls().count("tfcode"), 2)
        self.assertEqual(self.calls().count("codex"), 2)
        self.assertIn("pass limit", result.stderr)

    def test_worker_and_critic_must_be_independent(self):
        route = {"planner": "claude", "worker": "codex", "critic": "codex", "integrator": "claude"}
        result = self.execute("route", self.config(route=route))
        self.assertEqual(result.returncode, 2)
        self.assertIn("must use different runtimes", result.stderr)

    def test_read_only_phase_repository_mutation_aborts(self):
        env = dict(self.env)
        env["GAUNTLET_TEST_MUTATE_READONLY"] = "1"
        result = self.execute("run", self.config(), env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("read-only planner", result.stderr)

    def test_tfcode_worker_opt_in_resolves_to_auto(self):
        route = {"planner": "claude", "worker": "tfcode", "critic": "codex", "integrator": "claude"}
        result = self.execute("route", self.config(route=route, allow_tfcode_write=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        worker_line = next(line for line in result.stdout.splitlines() if line.startswith("worker"))
        self.assertIn("--auto", worker_line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
