import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "entrypoint.sh"


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)
        self.log_path = temp_path / "jobs.log"
        self.python_log_path = temp_path / "python.log"
        bin_path = temp_path / "bin"
        bin_path.mkdir()

        fake_python = bin_path / "python3"
        fake_python.write_text(
            """#!/bin/sh
echo "$*" >> "$KITCHEN_OPS_PYTHON_LOG"
if [ "$1" = "-c" ]; then
    if [ "$#" -ge 3 ]; then
        echo UP
    else
        echo test-version
    fi
    exit 0
fi

case "$1" in
    kitchen_ops_tagger.py) job=tagger ;;
    kitchen_ops_cleaner.py) job=cleaner ;;
    kitchen_ops_parser.py)
        if [ "${2:-}" = "--review-catalog" ]; then
            job=catalog-review
        else
            job=parser
        fi
        ;;
    *) exit 2 ;;
esac
echo "$job" >> "$KITCHEN_OPS_TEST_LOG"
"""
        )
        fake_python.chmod(0o755)

        self.env = os.environ.copy()
        self.env.pop("SCRIPT_TO_RUN", None)
        self.env.update(
            {
                "DRY_RUN": "true",
                "KITCHEN_OPS_PYTHON_LOG": str(self.python_log_path),
                "KITCHEN_OPS_TEST_LOG": str(self.log_path),
                "MEALIE_API_TOKEN": "test-token",
                "MEALIE_URL": "http://mealie.test",
                "PATH": f"{bin_path}{os.pathsep}{self.env['PATH']}",
            }
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_entrypoint(self, *args, env=None):
        return subprocess.run(
            [str(ENTRYPOINT), *args],
            cwd=ROOT,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def recorded_jobs(self):
        if not self.log_path.exists():
            return []
        return self.log_path.read_text().splitlines()

    def test_each_positional_command_runs_the_selected_job(self):
        expected_jobs = {
            "tagger": ["tagger"],
            "parser": ["parser"],
            "cleaner": ["cleaner"],
            "catalog-review": ["catalog-review"],
            "all": ["tagger", "cleaner", "parser"],
        }

        for command, expected in expected_jobs.items():
            with self.subTest(command=command):
                self.log_path.unlink(missing_ok=True)
                result = self.run_entrypoint(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recorded_jobs(), expected)

    def test_positional_command_takes_precedence_over_environment(self):
        env = self.env.copy()
        env["SCRIPT_TO_RUN"] = "all"

        result = self.run_entrypoint("tagger", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_jobs(), ["tagger"])

    def test_script_to_run_remains_supported_without_an_argument(self):
        env = self.env.copy()
        env["SCRIPT_TO_RUN"] = "cleaner"

        result = self.run_entrypoint(env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_jobs(), ["cleaner"])

    def test_unknown_positional_command_fails_without_running_a_job(self):
        env = self.env.copy()
        env["SCRIPT_TO_RUN"] = "all"

        result = self.run_entrypoint("not-a-command", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown KitchenOps command", result.stderr)
        self.assertEqual(self.recorded_jobs(), [])

    def test_interactive_manual_run_keeps_safety_prompts(self):
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [str(ENTRYPOINT), "parser"],
            cwd=ROOT,
            env=self.env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)
        output = bytearray()
        try:
            # Accept dry-run, proceed, and then decline another tool.
            os.write(master_fd, b"\n\n\n")
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.1)
                if readable:
                    output.extend(os.read(master_fd, 4096))
            self.assertIsNotNone(process.poll(), "interactive launcher did not exit")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master_fd)

        rendered = output.decode(errors="replace")
        self.assertIn("Enable Dry Run?", rendered)
        self.assertIn("Proceed?", rendered)
        self.assertIn("Run another tool?", rendered)
        self.assertEqual(self.recorded_jobs(), ["parser"])

    def test_idle_stays_running_without_starting_python(self):
        process = subprocess.Popen(
            [str(ENTRYPOINT), "idle"],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIn("KitchenOps is idle", process.stdout.readline())
            self.assertIsNone(process.poll())
            self.assertEqual(self.recorded_jobs(), [])
            self.assertFalse(self.python_log_path.exists())
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
