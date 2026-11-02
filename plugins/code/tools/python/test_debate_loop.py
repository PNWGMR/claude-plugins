"""Exercise debate budgets through the real shell/review scripts with inert CLIs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

BASH = shutil.which("bash")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "debate-loop.sh"


def bash_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    return subprocess.check_output(
        [str(BASH), "-c", 'cygpath -u "$1"', "_", str(path)], text=True
    ).strip()


@unittest.skipUnless(BASH and shutil.which("jq"), "Bash and jq are required")
class DebateBudgetTests(unittest.TestCase):
    def run_debate(
        self,
        replies: list[str],
        *,
        correction: str = "yes",
        extra: tuple[str, ...] = (),
    ) -> tuple[subprocess.CompletedProcess[str], int, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        plan = root / "plan.md"
        plan.write_text("# Plan\nOriginal scope\n", encoding="utf-8")
        plan.with_suffix(".state").write_text(
            "SESSION_ID=test-claude\nCODEX_SESSION_ID=\nROUND=1\n"
            "PHASE=codex_review\nLOG_ID=\n",
            encoding="utf-8",
        )
        plan.with_suffix(".prompt").write_text("Original request", encoding="utf-8")
        plan.with_suffix(".exclusions").write_text("Excluded future work", encoding="utf-8")
        cases = []
        for number, reply in enumerate(replies, 1):
            event = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": reply},
                }
            )
            cases.append(f"{number}) printf '%s\\n' '{event}' ;;")
        codex = bin_dir / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            'count=$(cat "$STUB_ROOT/calls" 2>/dev/null || echo 0)\n'
            'count=$((count + 1)); printf "%s" "$count" > "$STUB_ROOT/calls"\n'
            'printf "%s\\n" \'{"type":"thread.started","thread_id":"test-codex"}\'\n'
            'case "$count" in\n'
            + "\n".join(cases)
            + '\n*) exit 9 ;;\nesac\n',
            encoding="utf-8",
        )
        claude = bin_dir / "claude"
        claude.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$@" > "$STUB_ROOT/claude-args"\n'
            'case "$STUB_CORRECTION" in\n'
            'yes) printf "\\nMaterial correction\\n" >> "$STUB_ROOT/plan.md"; '
            'printf "MATERIAL_CORRECTION: yes\\nFixed a material finding\\n" '
            '> "$STUB_ROOT/plan.revisions" ;;\n'
            'no) printf "MATERIAL_CORRECTION: no\\nOnly advisory suggestions\\n" '
            '> "$STUB_ROOT/plan.revisions" ;;\n'
            'unchanged) printf "MATERIAL_CORRECTION: yes\\nNo changed bytes\\n" '
            '> "$STUB_ROOT/plan.revisions" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        for executable in (codex, claude):
            executable.chmod(0o755)
        return self.run_existing(root, correction=correction, extra=extra)

    def run_existing(
        self,
        root: Path,
        *,
        correction: str = "yes",
        extra: tuple[str, ...] = (),
    ) -> tuple[subprocess.CompletedProcess[str], int, Path]:
        bin_dir = root / "bin"
        plan = root / "plan.md"
        claude = bin_dir / "claude"
        env = os.environ.copy()
        env.update(
            PATH=str(bin_dir) + os.pathsep + env["PATH"],
            CLAUDE_BIN=bash_path(claude),
            STUB_ROOT=bash_path(root),
            STUB_CORRECTION=correction,
            HOME=bash_path(root),
        )
        result = subprocess.run(
            [
                str(BASH),
                bash_path(SCRIPT),
                "--plan-file",
                bash_path(plan),
                *extra,
                "Original request",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        count_file = root / "calls"
        count = int(count_file.read_text()) if count_file.exists() else 0
        return result, count, root

    def test_approved_first_pass_does_not_run_a_quiet_second_pass(self) -> None:
        result, calls, _ = self.run_debate(["VERDICT: APPROVED"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 1)
        self.assertIn("Plan approved by Codex after 1 round", result.stdout)

    def test_material_correction_receives_one_verification_pass(self) -> None:
        result, calls, root = self.run_debate(
            ["### Finding 1: Material gap\nVERDICT: NEEDS_CHANGES", "VERDICT: APPROVED"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 2)
        self.assertIn("Plan approved by Codex after 2 round", result.stdout)
        correction_prompt = (root / "claude-args").read_text()
        self.assertIn("Original request", correction_prompt)
        self.assertIn("Excluded future work", correction_prompt)

    def test_unresolved_findings_stop_at_default_budget_and_are_preserved(self) -> None:
        result, calls, root = self.run_debate(
            ["### Finding 1: Material gap\nVERDICT: NEEDS_CHANGES"] * 3
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 2)
        self.assertIn("without approval", result.stdout)
        self.assertNotIn("Plan approved by Codex", result.stdout)
        self.assertIn("Material gap", (root / "plan.feedback").read_text())
        self.assertEqual((root / "plan.prompt").read_text(), "Original request")
        self.assertEqual((root / "plan.exclusions").read_text(), "Excluded future work")

    def test_exhausted_default_budget_stays_exhausted_on_restart(self) -> None:
        _, calls, root = self.run_debate(
            ["### Finding 1: Material gap\nVERDICT: NEEDS_CHANGES"] * 2
            + ["VERDICT: APPROVED"]
        )
        self.assertEqual(calls, 2)
        feedback = (root / "plan.feedback").read_text()
        self.assertIn("ROUND=2", (root / "plan.state").read_text())
        self.assertIn("PHASE=claude_revision", (root / "plan.state").read_text())
        result, calls, _ = self.run_existing(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 2)
        self.assertNotIn("Plan approved by Codex", result.stdout)
        self.assertEqual((root / "plan.feedback").read_text(), feedback)
        self.assertEqual((root / "plan.prompt").read_text(), "Original request")
        self.assertEqual((root / "plan.exclusions").read_text(), "Excluded future work")

    def test_explicit_larger_budget_resumes_with_correction_then_next_pass(self) -> None:
        _, calls, root = self.run_debate(
            ["### Finding 1: Material gap\nVERDICT: NEEDS_CHANGES"] * 2
            + ["VERDICT: APPROVED"]
        )
        self.assertEqual(calls, 2)
        result, calls, _ = self.run_existing(root, extra=("--max-rounds", "15"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 3)
        self.assertIn("Plan approved by Codex after 3 round", result.stdout)
        self.assertEqual((root / "plan.md").read_text().count("Material correction"), 2)

    def test_advice_or_unchanged_plan_does_not_force_a_second_pass(self) -> None:
        for correction in ("no", "unchanged", "missing"):
            with self.subTest(correction=correction):
                result, calls, root = self.run_debate(
                    ["### Finding 1: Check finding\nVERDICT: NEEDS_CHANGES"] * 2,
                    correction=correction,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, 1)
                self.assertNotIn("Plan approved by Codex", result.stdout)
                self.assertTrue((root / "plan.feedback").exists())

    def test_empty_response_does_not_consume_an_automatic_second_pass(self) -> None:
        result, calls, _ = self.run_debate(["", "VERDICT: APPROVED"])
        self.assertEqual(calls, 1)
        self.assertNotIn("Plan approved by Codex", result.stdout)
        self.assertIn("unapproved", result.stdout)

    def test_explicit_deep_budget_can_continue_past_two_passes(self) -> None:
        result, calls, _ = self.run_debate(
            [
                "### Finding 1: Material gap\nVERDICT: NEEDS_CHANGES",
                "### Finding 1: Material remaining gap\nVERDICT: NEEDS_CHANGES",
                "VERDICT: APPROVED",
            ],
            extra=("--max-rounds", "15"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 3)
        self.assertIn("Plan approved by Codex after 3 round", result.stdout)

    def test_quoted_approval_does_not_override_terminal_unresolved_verdict(self) -> None:
        result, calls, _ = self.run_debate(
            [
                "Example VERDICT: APPROVED\n### Finding 1: Unresolved\n"
                "VERDICT: NEEDS_CHANGES"
            ],
            extra=("--max-rounds", "1"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 1)
        self.assertNotIn("Plan approved by Codex", result.stdout)

    def test_round_limit_rejects_more_than_explicit_deep_maximum(self) -> None:
        result, calls, _ = self.run_debate(["VERDICT: APPROVED"], extra=("--max-rounds", "16"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, 0)
        self.assertIn("1 to 15", result.stderr)


if __name__ == "__main__":
    unittest.main()
