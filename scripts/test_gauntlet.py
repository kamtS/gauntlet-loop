#!/usr/bin/env python3

import importlib.util
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

# GAUNTLET_TEST_VERDICTS is a comma-separated script of critic behaviours,
# consumed one per critic call; the last entry repeats.
mode = "ANSWER"
if is_critic:
    script = os.environ.get("GAUNTLET_TEST_VERDICTS", os.environ.get("GAUNTLET_TEST_VERDICT", "PASS")).split(",")
    counter = pathlib.Path(os.environ["GAUNTLET_TEST_LOG"] + ".critic")
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    mode = script[min(index, len(script) - 1)]

output_file = None
if runtime == "codex" and "--output-last-message" in sys.argv:
    output_file = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])

def emit(text):
    if output_file is not None:
        output_file.write_text(text + "\n", encoding="utf-8")
    elif runtime == "tfcode":
        print(json.dumps({"type": "text", "part": {"text": text}}))
    else:
        print(text)

if mode in ("PASS", "FAIL"):
    emit("Findings: checked the repository.\nVERDICT: " + mode)
elif mode == "EMPTY":
    pass  # exit 0 without writing anything
elif mode == "TRUNCATED":
    emit("Running the test suite now, then I will check whether the")
elif mode == "EARLY":
    # The verdict appears in an earlier message; the final message omits it.
    if runtime == "codex":
        print("codex\nFindings recorded.\nVERDICT: PASS", file=sys.stderr)
        emit("Summary: see findings above.")
    elif runtime == "tfcode":
        print(json.dumps({"type": "text", "part": {"text": "Findings recorded.\nVERDICT: PASS"}}))
        print(json.dumps({"type": "text", "part": {"text": "Summary: see findings above."}}))
    else:
        emit("Findings recorded.\nVERDICT: PASS\nSummary: see findings above.")
elif mode == "GARBAGE":
    print("{truncated json")
    print("plain narration without a verdict")
else:
    emit(runtime + " completed")
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

    def status(self):
        status_files = list((self.project / ".gauntlet" / "runs").glob("*/status.json"))
        self.assertEqual(len(status_files), 1)
        return json.loads(status_files[0].read_text())

    def run_with_verdicts(self, verdicts, route=None, **extra):
        env = dict(self.env)
        env["GAUNTLET_TEST_VERDICTS"] = verdicts
        return self.execute("run", self.config(route=route, **extra), env=env)

    def test_unparseable_critic_is_retried_without_consuming_a_worker_pass(self):
        result = self.run_with_verdicts("EMPTY,PASS")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["claude", "codex", "tfcode", "tfcode", "claude"])
        self.assertIn("no VERDICT line", result.stderr)
        status = self.status()
        self.assertEqual(status["critic_verdict"], "PASS")
        self.assertEqual(status["passes"], 1)
        self.assertEqual(status["critic_attempts"], 2)

    def test_persistently_unparseable_critic_escalates_instead_of_failing(self):
        result = self.run_with_verdicts("TRUNCATED")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.calls().count("codex"), 1)
        self.assertEqual(self.calls().count("tfcode"), 2)
        self.assertNotIn("pass limit", result.stderr)
        self.assertIn("no parseable VERDICT", result.stderr)
        self.assertEqual(self.status()["critic_verdict"], "UNPARSEABLE")

    def test_critic_retries_zero_escalates_immediately(self):
        result = self.run_with_verdicts("EMPTY", critic_retries=0)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.calls().count("tfcode"), 1)

    def test_critic_retries_are_bounded(self):
        result = self.execute("route", self.config(critic_retries=4))
        self.assertEqual(result.returncode, 2)
        self.assertIn("critic_retries", result.stderr)

    def test_genuine_fail_still_consumes_worker_passes(self):
        result = self.run_with_verdicts("FAIL,PASS")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("codex"), 2)
        self.assertEqual(self.status()["passes"], 2)

    def test_unparseable_then_fail_then_pass(self):
        result = self.run_with_verdicts("EMPTY,FAIL,PASS")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("codex"), 2)
        self.assertEqual(self.status()["critic_attempts"], 3)

    def test_empty_codex_critic_artifact_is_unparseable_not_fail(self):
        route = {"planner": "claude", "worker": "claude", "critic": "codex", "integrator": "claude"}
        result = self.run_with_verdicts("EMPTY", route=route, critic_retries=0)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.status()["critic_verdict"], "UNPARSEABLE")

    def test_codex_verdict_in_earlier_message_is_found(self):
        route = {"planner": "claude", "worker": "claude", "critic": "codex", "integrator": "claude"}
        result = self.run_with_verdicts("EARLY", route=route)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status()["critic_verdict"], "PASS")
        self.assertEqual(self.calls().count("codex"), 1)

    def test_tfcode_verdict_in_earlier_message_is_found(self):
        result = self.run_with_verdicts("EARLY")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status()["critic_verdict"], "PASS")

    def test_tfcode_dropped_lines_are_surfaced(self):
        result = self.run_with_verdicts("GARBAGE", critic_retries=0)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("non-JSON line(s)", result.stderr)
        dropped = list((self.project / ".gauntlet" / "runs").glob("*/*critic*.dropped.log"))
        self.assertEqual(len(dropped), 1)
        self.assertIn("{truncated json", dropped[0].read_text())


class VerdictParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("gauntlet", RUNNER)
        cls.gauntlet = importlib.util.module_from_spec(spec)
        # Dataclasses resolve their module through sys.modules.
        sys.modules[spec.name] = cls.gauntlet
        spec.loader.exec_module(cls.gauntlet)

    def test_empty_and_whitespace_are_unparseable(self):
        self.assertEqual(self.gauntlet.verdict(""), "UNPARSEABLE")
        self.assertEqual(self.gauntlet.verdict("  \n\t\n"), "UNPARSEABLE")

    def test_truncated_transcript_is_unparseable(self):
        self.assertEqual(self.gauntlet.verdict("Reading src/app.py, then I will run the"), "UNPARSEABLE")

    def test_instruction_echo_is_not_a_verdict(self):
        text = "End with exactly one line: VERDICT: PASS or VERDICT: FAIL. Do not edit files."
        self.assertEqual(self.gauntlet.verdict(text), "UNPARSEABLE")

    def test_genuine_pass_and_fail(self):
        self.assertEqual(self.gauntlet.verdict("Findings.\nVERDICT: PASS\n"), "PASS")
        self.assertEqual(self.gauntlet.verdict("Findings.\nverdict: fail"), "FAIL")

    def test_last_verdict_wins(self):
        self.assertEqual(self.gauntlet.verdict("VERDICT: FAIL\nrechecked\nVERDICT: PASS"), "PASS")

    def test_final_output_verdict_beats_transcript(self):
        result = self.gauntlet.PhaseResult("VERDICT: FAIL", "VERDICT: PASS\nVERDICT: FAIL\nearlier VERDICT: PASS")
        self.assertEqual(self.gauntlet.critic_verdict(result), "FAIL")

    def test_transcript_verdict_used_when_final_output_lacks_one(self):
        result = self.gauntlet.PhaseResult("Summary only.", "VERDICT: FAIL\nSummary only.")
        self.assertEqual(self.gauntlet.critic_verdict(result), "FAIL")

    def test_tfcode_parser_reports_dropped_lines(self):
        stdout = '{"part": {"text": "hello"}}\nnot json\n[1, 2]\n\n'
        text, dropped = self.gauntlet.parse_tfcode_events(stdout)
        self.assertEqual(text, "hello")
        self.assertEqual(dropped, ["not json", "[1, 2]"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
