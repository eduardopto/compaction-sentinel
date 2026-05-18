from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class ReplayHarnessTests(unittest.TestCase):
    def test_all_replay_scenarios_pass(self) -> None:
        root = Path(__file__).resolve().parents[1]
        scenarios = sorted((root / "tests" / "fixtures" / "scenarios").glob("*.jsonl"))
        self.assertGreaterEqual(len(scenarios), 7)
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "replay_hooks.py"), *map(str, scenarios)],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
