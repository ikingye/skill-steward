import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "skill-steward" / "scripts" / "skill_steward.py"


def load_module():
    spec = importlib.util.spec_from_file_location("skill_steward", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_skill(root: Path, folder: str, name: str, description: str, body: str = "Body") -> Path:
    path = root / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


class ManageAgentSkillsTest(unittest.TestCase):
    def test_inventory_detects_duplicates_and_scope_recommendations(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "shared-only", "shared-only", "Use when managing general notes")
            write_skill(home / ".agents" / "skills", "codex-tool", "codex-tool", "Use when working with Codex CLI sessions")
            write_skill(home / ".codex" / "skills", "generic-helper", "generic-helper", "Use when organizing research")
            write_skill(home / ".claude" / "skills", "shared-only", "shared-only", "Use when managing general notes")

            report = module.build_report(home=home, project=None, days=90, log_roots=[])

            duplicates = {item["name"]: item for item in report["duplicates"]}
            self.assertIn("shared-only", duplicates)
            self.assertEqual(len(duplicates["shared-only"]["locations"]), 2)

            recommendations = {(item["skill"], item["recommendation"]) for item in report["recommendations"]}
            self.assertIn(("codex-tool", "move-to-agent-specific"), recommendations)
            self.assertIn(("generic-helper", "move-to-shared"), recommendations)

    def test_shared_multi_agent_skill_is_not_misclassified_as_agent_specific(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(
                home / ".agents" / "skills",
                "skill-steward",
                "skill-steward",
                "Use when managing Codex, Claude Code, Gemini CLI, and Cursor skills",
                body="Manage shared and agent-specific directories without duplicating skills.",
            )
            write_skill(
                home / ".agents" / "skills",
                "openai-api-helper",
                "openai-api-helper",
                "Use when working with OpenAI API documentation",
                body="This can be wrapped for Codex skills but is not Codex-only.",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[])
            placement_recommendations = {
                (item["skill"], item["recommendation"]) for item in report["recommendations"]
            }

            self.assertNotIn(("skill-steward", "move-to-agent-specific"), placement_recommendations)
            self.assertNotIn(("openai-api-helper", "move-to-agent-specific"), placement_recommendations)

    def test_agent_specific_body_prevents_false_move_to_shared(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(
                home / ".codex" / "skills",
                "playwright",
                "playwright",
                "Use when automating browsers",
                body='export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"',
            )
            write_skill(
                home / ".claude" / "skills",
                "create-colleague",
                "create-colleague",
                "Use when creating colleague skills",
                body="Run python3 ${CLAUDE_SKILL_DIR}/tools/skill_writer.py from Claude Code.",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[])
            placement_recommendations = {
                (item["skill"], item["recommendation"]) for item in report["recommendations"]
            }

            self.assertNotIn(("playwright", "move-to-shared"), placement_recommendations)
            self.assertNotIn(("create-colleague", "move-to-shared"), placement_recommendations)

    def test_usage_scan_counts_agent_mentions_and_effectiveness_proxy(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "skill-steward", "skill-steward", "Use when auditing skills")
            write_skill(home / ".agents" / "skills", "unused-skill", "unused-skill", "Use when no logs mention it")

            log_dir = home / ".codex" / "sessions"
            log_dir.mkdir(parents=True)
            (log_dir / "session.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"text": "Using skill-steward to audit skills. Validation passed."}),
                        json.dumps({"text": "skill-steward completed successfully."}),
                        json.dumps({"text": "unused-skill failed with an error."}),
                    ]
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[log_dir])
            usage = {item["name"]: item for item in report["usage"]}

            self.assertEqual(usage["skill-steward"]["total_mentions"], 2)
            self.assertEqual(usage["skill-steward"]["by_agent"]["codex"], 2)
            self.assertGreater(usage["skill-steward"]["effectiveness_score"], 0.5)
            self.assertEqual(usage["unused-skill"]["negative_signals"], 1)

    def test_usage_windows_count_24h_7d_and_30d_from_log_timestamps(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "alpha-skill", "alpha-skill", "Use when testing alpha")
            write_skill(home / ".agents" / "skills", "beta-skill", "beta-skill", "Use when testing beta")

            now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
            log_dir = home / ".codex" / "sessions"
            log_dir.mkdir(parents=True)
            (log_dir / "session.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(hours=2)).isoformat(),
                                "text": "Using alpha-skill.",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(days=3)).isoformat(),
                                "text": "Using alpha-skill and beta-skill.",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(days=10)).isoformat(),
                                "text": "Using alpha-skill.",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(days=40)).isoformat(),
                                "text": "Using alpha-skill and beta-skill.",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[log_dir], now=now)
            usage_windows = {item["name"]: item for item in report["usage_windows"]}

            self.assertEqual(usage_windows["alpha-skill"]["last_24h"], 1)
            self.assertEqual(usage_windows["alpha-skill"]["last_7d"], 2)
            self.assertEqual(usage_windows["alpha-skill"]["last_30d"], 3)
            self.assertEqual(usage_windows["beta-skill"]["last_24h"], 0)
            self.assertEqual(usage_windows["beta-skill"]["last_7d"], 1)
            self.assertEqual(usage_windows["beta-skill"]["last_30d"], 1)

    def test_usage_confidence_distinguishes_used_likely_and_mention_signals(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "alpha-skill", "alpha-skill", "Use when testing alpha")
            write_skill(home / ".agents" / "skills", "beta-skill", "beta-skill", "Use when testing beta")
            write_skill(home / ".agents" / "skills", "gamma-skill", "gamma-skill", "Use when testing gamma")

            now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
            log_dir = home / ".codex" / "sessions"
            log_dir.mkdir(parents=True)
            (log_dir / "session.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(hours=1)).isoformat(),
                                "text": "Using alpha-skill to audit skills. Validation passed.",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(hours=2)).isoformat(),
                                "text": "Use beta-skill to inspect this project. It failed.",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(hours=3)).isoformat(),
                                "text": "gamma-skill appears in the README.",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[log_dir], now=now)
            confidence = {item["name"]: item for item in report["usage_confidence"]}

            self.assertEqual(confidence["alpha-skill"]["event_type"], "used")
            self.assertEqual(confidence["alpha-skill"]["strong_signals"], 1)
            self.assertEqual(confidence["alpha-skill"]["actual_or_likely_uses"], 1)
            self.assertEqual(confidence["alpha-skill"]["success_signals"], 1)
            self.assertEqual(confidence["alpha-skill"]["confidence"], 1.0)

            self.assertEqual(confidence["beta-skill"]["event_type"], "likely")
            self.assertEqual(confidence["beta-skill"]["medium_signals"], 1)
            self.assertEqual(confidence["beta-skill"]["actual_or_likely_uses"], 1)
            self.assertEqual(confidence["beta-skill"]["failure_signals"], 1)
            self.assertEqual(confidence["beta-skill"]["confidence"], 0.7)

            self.assertEqual(confidence["gamma-skill"]["event_type"], "mention")
            self.assertEqual(confidence["gamma-skill"]["weak_signals"], 1)
            self.assertEqual(confidence["gamma-skill"]["actual_or_likely_uses"], 0)
            self.assertEqual(confidence["gamma-skill"]["confidence"], 0.2)

    def test_text_report_prints_usage_windows_table(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "alpha-skill", "alpha-skill", "Use when testing alpha")
            now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
            log_dir = home / ".codex" / "sessions"
            log_dir.mkdir(parents=True)
            (log_dir / "session.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": (now - timedelta(hours=1)).isoformat(),
                        "text": "Using alpha-skill.",
                    }
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[log_dir], now=now)
            output = StringIO()
            with redirect_stdout(output):
                module.print_text(report)

            text = output.getvalue()
            self.assertIn("Usage by Window", text)
            self.assertIn("24h", text)
            self.assertIn("7d", text)
            self.assertIn("30d", text)
            self.assertIn("alpha-skill", text)

    def test_text_report_prints_usage_confidence_table(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "alpha-skill", "alpha-skill", "Use when testing alpha")
            now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
            log_dir = home / ".codex" / "sessions"
            log_dir.mkdir(parents=True)
            (log_dir / "session.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": (now - timedelta(hours=1)).isoformat(),
                        "text": "Using alpha-skill to audit skills.",
                    }
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[log_dir], now=now)
            output = StringIO()
            with redirect_stdout(output):
                module.print_text(report)

            text = output.getvalue()
            self.assertIn("Usage Confidence", text)
            self.assertIn("Likely", text)
            self.assertIn("Mentions", text)
            self.assertIn("Confidence", text)
            self.assertIn("alpha-skill", text)

    def test_cleanup_recommendations_group_safe_review_keep_and_protected(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "old-mention", "old-mention", "Use when testing old")
            write_skill(home / ".agents" / "skills", "recent-used", "recent-used", "Use when testing recent")
            write_skill(home / ".agents" / "skills", "never-seen", "never-seen", "Use when testing never")
            write_skill(
                home / ".codex" / "skills" / "codex-primary-runtime",
                "spreadsheets",
                "Excel",
                "Use when editing spreadsheets",
            )

            now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
            log_dir = home / ".codex" / "sessions"
            log_dir.mkdir(parents=True)
            (log_dir / "session.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(days=45)).isoformat(),
                                "text": "old-mention appears in archived notes.",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (now - timedelta(hours=2)).isoformat(),
                                "text": "Using recent-used for this task. Validation passed.",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[log_dir], now=now)
            cleanup = {item["skill"]: item for item in report["cleanup_recommendations"]}

            self.assertEqual(cleanup["old-mention"]["recommendation"], "safe-to-remove")
            self.assertEqual(cleanup["recent-used"]["recommendation"], "keep")
            self.assertEqual(cleanup["never-seen"]["recommendation"], "review-manually")
            self.assertEqual(cleanup["Excel"]["recommendation"], "protected-or-skip")

    def test_skills_cli_cleanup_plan_outputs_json(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "never-seen", "never-seen", "Use when testing never")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(["skills", "cleanup-plan", "--home", str(home), "--format", "json"]),
                    0,
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(payload["cleanup_recommendations"][0]["skill"], "never-seen")
            self.assertEqual(payload["cleanup_recommendations"][0]["recommendation"], "review-manually")

    def test_cli_can_render_static_html_report(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "alpha-skill", "alpha-skill", "Use when testing alpha")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["--home", str(home), "--format", "html"]), 0)

            html = output.getvalue()
            self.assertIn("<!doctype html>", html)
            self.assertIn("Skill Steward Report", html)
            self.assertIn("Usage by Window", html)
            self.assertIn("Usage Confidence", html)
            self.assertIn("Cleanup Recommendations", html)
            self.assertIn("alpha-skill", html)

    def test_event_command_writes_structured_event_and_report_reads_it(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "alpha-skill", "alpha-skill", "Use when testing alpha")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(
                        [
                            "event",
                            "used",
                            "alpha-skill",
                            "--agent",
                            "codex",
                            "--outcome",
                            "success",
                            "--home",
                            str(home),
                            "--format",
                            "json",
                        ]
                    ),
                    0,
                )

            payload = json.loads(output.getvalue())
            event_file = Path(payload["path"])
            self.assertTrue(event_file.is_file())

            report = module.build_report(home=home, project=None, days=90, log_roots=None)
            confidence = {item["name"]: item for item in report["usage_confidence"]}

            self.assertEqual(confidence["alpha-skill"]["event_type"], "used")
            self.assertEqual(confidence["alpha-skill"]["strong_signals"], 1)
            self.assertEqual(confidence["alpha-skill"]["actual_or_likely_uses"], 1)
            self.assertEqual(confidence["alpha-skill"]["success_signals"], 1)
            self.assertEqual(confidence["alpha-skill"]["by_agent"]["codex"]["strong_signals"], 1)

    def test_quality_report_flags_broad_paths_and_script_permissions(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            good = write_skill(
                home / ".agents" / "skills",
                "good-skill",
                "good-skill",
                "Use when reviewing a Python package release checklist",
            )
            bad = write_skill(
                home / ".agents" / "skills",
                "bad-skill",
                "bad-skill",
                "Use when doing anything",
                body="Run /Users/alice/private/tool.py before continuing.",
            )
            script_dir = bad / "scripts"
            script_dir.mkdir()
            (script_dir / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

            report = module.build_report(home=home, project=None, days=90, log_roots=[])
            quality = {item["skill"]: item for item in report["quality"]}

            self.assertEqual(quality["good-skill"]["quality_score"], 100)
            self.assertLess(quality["bad-skill"]["quality_score"], quality["good-skill"]["quality_score"])
            issue_codes = {issue["code"] for issue in quality["bad-skill"]["issues"]}
            self.assertIn("broad-description", issue_codes)
            self.assertIn("hardcoded-absolute-path", issue_codes)
            self.assertIn("non-executable-script", issue_codes)
            self.assertTrue((good / "SKILL.md").is_file())

    def test_quality_report_flags_nested_invalid_skill_frontmatter(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill = write_skill(
                home / ".agents" / "skills",
                "plugin-skill",
                "plugin-skill",
                "Use when checking packaged skill plugin metadata",
            )
            nested_skill = skill / "plugins" / "plugin-skill" / "skills" / "plugin-skill"
            nested_skill.mkdir(parents=True)
            (nested_skill / "SKILL.md").write_text("../../../../SKILL.md", encoding="utf-8")

            report = module.build_report(home=home, project=None, days=90, log_roots=[])
            quality = {item["skill"]: item for item in report["quality"]}

            issues = {issue["code"]: issue for issue in quality["plugin-skill"]["issues"]}
            self.assertIn("invalid-skill-frontmatter", issues)
            issue = issues["invalid-skill-frontmatter"]
            self.assertEqual(issue["paths"], ["plugins/plugin-skill/skills/plugin-skill/SKILL.md"])

    def test_skills_cli_quality_outputs_json(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "bad-skill", "bad-skill", "Use when doing anything")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["skills", "quality", "--home", str(home), "--format", "json"]), 0)

            payload = json.loads(output.getvalue())
            self.assertEqual(payload["quality"][0]["skill"], "bad-skill")
            self.assertLess(payload["quality"][0]["quality_score"], 100)

    def test_skills_cli_quality_text_shows_only_issues_by_default(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "good-skill", "good-skill", "Use when reviewing package releases")
            write_skill(home / ".agents" / "skills", "bad-skill", "bad-skill", "Use when doing anything")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["skills", "quality", "--home", str(home)]), 0)

            text = output.getvalue()
            self.assertIn("bad-skill", text)
            self.assertNotIn("good-skill", text)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["skills", "quality", "--home", str(home), "--all"]), 0)

            self.assertIn("good-skill", output.getvalue())

    def test_multiline_description_is_parsed_for_quality_checks(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill_dir = home / ".agents" / "skills" / "block-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: block-skill",
                        "description: |",
                        "  Use when reviewing Python package release checklists before publishing.",
                        "---",
                        "",
                        "# Block Skill",
                    ]
                ),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=None, days=90, log_roots=[])
            quality = {item["skill"]: item for item in report["quality"]}

            self.assertEqual(quality["block-skill"]["quality_score"], 100)

    def test_project_policy_can_ignore_quality_issue_codes(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "repo"
            home.mkdir()
            project.mkdir()
            write_skill(project / ".agents" / "skills", "bad-skill", "bad-skill", "Use when doing anything")
            (project / ".skill-steward.json").write_text(
                json.dumps({"quality": {"ignore_issue_codes": ["broad-description"]}}),
                encoding="utf-8",
            )

            report = module.build_report(home=home, project=project, days=90, log_roots=[])
            quality = {item["skill"]: item for item in report["quality"]}

            self.assertEqual(quality["bad-skill"]["quality_score"], 100)
            self.assertEqual(quality["bad-skill"]["issues"], [])
            self.assertEqual(quality["bad-skill"]["ignored_issues"][0]["code"], "broad-description")

    def test_skills_cli_quality_fix_makes_shebang_scripts_executable(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill = write_skill(
                home / ".agents" / "skills",
                "script-skill",
                "script-skill",
                "Use when running helper scripts for release checks",
            )
            script_dir = skill / "scripts"
            script_dir.mkdir()
            script = script_dir / "run.sh"
            script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            script.chmod(0o644)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(["skills", "quality", "--home", str(home), "--fix", "--format", "json"]),
                    0,
                )

            payload = json.loads(output.getvalue())

            self.assertTrue(os.access(script, os.X_OK))
            self.assertTrue(any(action["action"] == "chmod-executable" for action in payload["fix_actions"]))
            quality = {item["skill"]: item for item in payload["quality"]}
            issue_codes = {issue["code"] for issue in quality["script-skill"]["issues"]}
            self.assertNotIn("non-executable-script", issue_codes)

    def test_skills_cli_quality_fail_on_issues_returns_nonzero(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_skill(home / ".agents" / "skills", "bad-skill", "bad-skill", "Use when doing anything")

            output = StringIO()
            with redirect_stdout(output):
                result = module.main(["skills", "quality", "--home", str(home), "--fail-on-issues"])

            self.assertEqual(result, 1)
            self.assertIn("bad-skill", output.getvalue())

    def test_symlinked_agent_root_does_not_create_false_duplicate(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shared_root = home / ".agents" / "skills"
            write_skill(shared_root, "shared-only", "shared-only", "Use when managing general notes")

            cursor_parent = home / ".cursor"
            cursor_parent.mkdir(parents=True)
            (cursor_parent / "skills").symlink_to(shared_root, target_is_directory=True)

            report = module.build_report(home=home, project=None, days=90, log_roots=[])
            shared_only = [item for item in report["skills"] if item["name"] == "shared-only"]

            self.assertEqual(len(shared_only), 1)
            self.assertFalse(any(item["name"] == "shared-only" for item in report["duplicates"]))

    def test_apply_project_layout_migrates_legacy_skills_and_creates_agent_dirs(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "repo"
            home.mkdir()
            project.mkdir()
            write_skill(project / "skills", "shared-only", "shared-only", "Use when managing general notes")

            (project / ".agents").mkdir()
            (project / ".agents" / "skills").symlink_to("../skills", target_is_directory=True)

            (project / ".claude").mkdir()
            (project / ".claude" / "skills").symlink_to("../skills", target_is_directory=True)

            actions = module.apply_project_layout(project=project, agents=["codex", "claude"])

            self.assertFalse((project / "skills").exists())
            self.assertFalse((project / ".agents" / "skills").is_symlink())
            self.assertTrue((project / ".agents" / "skills" / "shared-only" / "SKILL.md").is_file())
            self.assertTrue((project / ".codex" / "skills").is_dir())
            self.assertTrue((project / ".claude" / "skills").is_dir())
            self.assertFalse((project / ".claude" / "skills").is_symlink())
            self.assertTrue((project / "AGENTS.md").read_text(encoding="utf-8").find(".agents/skills") >= 0)
            self.assertTrue((project / "CLAUDE.md").read_text(encoding="utf-8").find(".claude/skills") >= 0)
            self.assertTrue(any(action["action"] == "move-legacy-skills" for action in actions))

            report = module.build_report(home=home, project=project, days=90, log_roots=[])
            self.assertEqual([d for d in report["duplicates"] if d["name"] == "shared-only"], [])

    def test_apply_project_layout_removes_identical_agent_specific_duplicates(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "repo"
            project.mkdir()
            write_skill(project / ".agents" / "skills", "shared-only", "shared-only", "Use when managing general notes")
            write_skill(project / ".codex" / "skills", "shared-only", "shared-only", "Use when managing general notes")

            actions = module.apply_project_layout(project=project, agents=["codex"])

            self.assertTrue((project / ".agents" / "skills" / "shared-only").exists())
            self.assertFalse((project / ".codex" / "skills" / "shared-only").exists())
            self.assertTrue(any(action["action"] == "remove-identical-duplicate" for action in actions))

    def test_apply_project_layout_replaces_legacy_unmanaged_loader_text(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "repo"
            project.mkdir()
            (project / "AGENTS.md").write_text(
                "\n".join(
                    [
                        "# Project Skills",
                        "",
                        "Project shared skills live in `.agents/skills`. Codex-specific project skills live in `.codex/skills`.",
                        "",
                        "When working in this repository with Codex, consider both directories as project skill sources:",
                        "- Use `.agents/skills` for agent-neutral GPU/AICP workflows.",
                        "- Use `.codex/skills` only for Codex-specific workflows, tool names, or runtime behavior.",
                        "",
                        "Do not duplicate the same skill name in both directories. If a skill is useful outside Codex, keep the canonical copy in `.agents/skills`.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            module.apply_project_layout(project=project, agents=["codex"])

            text = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("# Project Skills"), 1)
            self.assertIn("<!-- skill-steward project skills start -->", text)
            self.assertNotIn("GPU/AICP workflows", text)

    def test_managed_agents_config_supports_add_delete_and_layout_defaults(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            project = Path(tmp) / "repo"
            project.mkdir()
            write_skill(project / "skills", "shared-only", "shared-only", "Use when managing general notes")

            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "claude"])

            added = module.add_managed_agents(["gemini", "codex"], config_path=config_path)
            self.assertEqual(added, ["codex", "claude", "gemini"])

            deleted = module.delete_managed_agents(["claude"], config_path=config_path)
            self.assertEqual(deleted, ["codex", "gemini"])

            module.apply_project_layout(project=project, agents=None, config_path=config_path)

            self.assertTrue((project / ".codex" / "skills").is_dir())
            self.assertTrue((project / ".gemini" / "skills").is_dir())
            self.assertFalse((project / ".claude" / "skills").exists())

    def test_agents_cli_add_delete_list(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["agents", "set", "codex", "--config", str(config_path)]), 0)
            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["agents", "add", "claude", "gemini", "--config", str(config_path)]), 0)
            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "claude", "gemini"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["agents", "delete", "claude", "--config", str(config_path)]), 0)
            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "gemini"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["agents", "list", "--config", str(config_path)]), 0)
            self.assertIn("codex", output.getvalue())
            self.assertIn("gemini", output.getvalue())

    def test_top_level_agent_aliases_and_install_command(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            project = Path(tmp) / "repo"
            project.mkdir()

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(["install", "codex", "gemini", "--config", str(config_path), "--project", str(project)]),
                    0,
                )
            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "gemini"])
            self.assertTrue((project / ".codex" / "skills").is_dir())
            self.assertTrue((project / ".gemini" / "skills").is_dir())

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["add", "claude", "--config", str(config_path)]), 0)
            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "gemini", "claude"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["delete", "gemini", "--config", str(config_path)]), 0)
            self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "claude"])

    def test_no_arg_agent_commands_prompt_for_numbered_selection(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            project = Path(tmp) / "repo"
            project.mkdir()
            original_stdin = sys.stdin
            try:
                output = StringIO()
                sys.stdin = StringIO("1,3\n")
                with redirect_stdout(output):
                    self.assertEqual(
                        module.main(["install", "--config", str(config_path), "--project", str(project)]),
                        0,
                    )
                self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "gemini"])
                self.assertIn("Select agents", output.getvalue())
                self.assertTrue((project / ".codex" / "skills").is_dir())
                self.assertTrue((project / ".gemini" / "skills").is_dir())

                output = StringIO()
                sys.stdin = StringIO("1\n")
                with redirect_stdout(output):
                    self.assertEqual(module.main(["add", "--config", str(config_path)]), 0)
                self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "gemini", "claude"])

                output = StringIO()
                sys.stdin = StringIO("2\n")
                with redirect_stdout(output):
                    self.assertEqual(module.main(["delete", "--config", str(config_path)]), 0)
                self.assertEqual(module.list_managed_agents(config_path=config_path), ["codex", "claude"])

                output = StringIO()
                sys.stdin = StringIO("2,4\n")
                with redirect_stdout(output):
                    self.assertEqual(module.main(["set", "--config", str(config_path)]), 0)
                self.assertEqual(module.list_managed_agents(config_path=config_path), ["claude", "cursor"])
            finally:
                sys.stdin = original_stdin

    def test_native_bridges_link_shared_skills_into_agent_native_dirs(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "repo"
            home.mkdir()
            project.mkdir()
            write_skill(home / ".agents" / "skills", "global-shared", "global-shared", "Use when managing general notes")
            write_skill(project / ".agents" / "skills", "project-shared", "project-shared", "Use when managing project notes")
            write_skill(project / ".claude" / "skills", "claude-only", "claude-only", "Use when working with Claude-specific tools")

            actions = module.apply_native_bridges(home=home, project=project, agents=["codex", "claude"])

            global_codex_bridge = home / ".codex" / "skills" / "global-shared"
            global_claude_bridge = home / ".claude" / "skills" / "global-shared"
            project_codex_bridge = project / ".codex" / "skills" / "project-shared"
            project_claude_bridge = project / ".claude" / "skills" / "project-shared"

            self.assertTrue(global_codex_bridge.is_symlink())
            self.assertTrue(global_claude_bridge.is_symlink())
            self.assertTrue(project_codex_bridge.is_symlink())
            self.assertTrue(project_claude_bridge.is_symlink())
            self.assertEqual(global_codex_bridge.resolve(), (home / ".agents" / "skills" / "global-shared").resolve())
            self.assertEqual(project_claude_bridge.resolve(), (project / ".agents" / "skills" / "project-shared").resolve())
            self.assertTrue((project / ".claude" / "skills" / "claude-only").is_dir())
            self.assertTrue(any(action["action"] == "create-native-bridge" for action in actions))

            report = module.build_report(home=home, project=project, days=90, log_roots=[])
            self.assertFalse(any(item["name"] in {"global-shared", "project-shared"} for item in report["duplicates"]))

    def test_native_bridges_replace_identical_agent_copy_with_symlink(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            write_skill(home / ".agents" / "skills", "shared-only", "shared-only", "Use when managing general notes")
            write_skill(home / ".codex" / "skills", "shared-only", "shared-only", "Use when managing general notes")

            actions = module.apply_native_bridges(home=home, project=None, agents=["codex"])

            bridge = home / ".codex" / "skills" / "shared-only"
            self.assertTrue(bridge.is_symlink())
            self.assertEqual(bridge.resolve(), (home / ".agents" / "skills" / "shared-only").resolve())
            self.assertTrue(any(action["action"] == "replace-identical-copy-with-native-bridge" for action in actions))

    def test_cli_can_apply_native_bridges(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "repo"
            home.mkdir()
            project.mkdir()
            write_skill(home / ".agents" / "skills", "global-shared", "global-shared", "Use when managing general notes")
            write_skill(project / ".agents" / "skills", "project-shared", "project-shared", "Use when managing project notes")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(
                        [
                            "--home",
                            str(home),
                            "--project",
                            str(project),
                            "--apply-native-bridges",
                            "--agent",
                            "claude",
                            "--format",
                            "json",
                        ]
                    ),
                    0,
                )

            self.assertTrue((home / ".claude" / "skills" / "global-shared").is_symlink())
            self.assertTrue((project / ".claude" / "skills" / "project-shared").is_symlink())
            payload = json.loads(output.getvalue())
            self.assertTrue(any(action["action"] == "create-native-bridge" for action in payload["layout_actions"]))

    def test_cli_native_bridges_can_target_project_scope_only(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "repo"
            home.mkdir()
            project.mkdir()
            write_skill(home / ".agents" / "skills", "global-shared", "global-shared", "Use when managing general notes")
            write_skill(project / ".agents" / "skills", "project-shared", "project-shared", "Use when managing project notes")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(
                        [
                            "--home",
                            str(home),
                            "--project",
                            str(project),
                            "--apply-native-bridges",
                            "--bridge-scope",
                            "project",
                            "--agent",
                            "claude",
                            "--format",
                            "json",
                        ]
                    ),
                    0,
                )

            self.assertFalse((home / ".claude" / "skills" / "global-shared").exists())
            self.assertTrue((project / ".claude" / "skills" / "project-shared").is_symlink())
            payload = json.loads(output.getvalue())
            scopes = {action.get("scope") for action in payload["layout_actions"]}
            self.assertEqual(scopes, {"project"})

    def test_install_can_apply_native_bridges(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "repo"
            config_path = Path(tmp) / "config.json"
            home.mkdir()
            project.mkdir()
            write_skill(home / ".agents" / "skills", "global-shared", "global-shared", "Use when managing general notes")
            write_skill(project / ".agents" / "skills", "project-shared", "project-shared", "Use when managing project notes")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(
                        [
                            "install",
                            "codex",
                            "claude",
                            "--home",
                            str(home),
                            "--project",
                            str(project),
                            "--config",
                            str(config_path),
                            "--native-bridges",
                        ]
                    ),
                    0,
                )

            self.assertTrue((home / ".codex" / "skills" / "global-shared").is_symlink())
            self.assertTrue((home / ".claude" / "skills" / "global-shared").is_symlink())
            self.assertTrue((project / ".codex" / "skills" / "project-shared").is_symlink())
            self.assertTrue((project / ".claude" / "skills" / "project-shared").is_symlink())
            self.assertIn("Native bridge actions:", output.getvalue())

    def test_quarantine_moves_skill_and_removes_native_bridge(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            shared = write_skill(home / ".agents" / "skills", "shared-only", "shared-only", "Use when managing notes")
            bridge_root = home / ".codex" / "skills"
            bridge_root.mkdir(parents=True)
            (bridge_root / "shared-only").symlink_to(shared, target_is_directory=True)

            actions = module.quarantine_skills(home=home, project=None, skill_names=["shared-only"])

            self.assertFalse(shared.exists())
            self.assertFalse((bridge_root / "shared-only").exists())
            quarantined = [action for action in actions if action["action"] == "quarantine-skill"]
            self.assertEqual(len(quarantined), 1)
            trash_dir = Path(quarantined[0]["trash_dir"])
            self.assertTrue((trash_dir / "shared-only" / "SKILL.md").is_file())
            self.assertTrue((trash_dir / "manifest.json").is_file())

            restore_actions = module.restore_skills(home=home, selectors=["shared-only"])

            self.assertTrue((shared / "SKILL.md").is_file())
            self.assertTrue((bridge_root / "shared-only").is_symlink())
            self.assertEqual((bridge_root / "shared-only").resolve(), shared.resolve())
            self.assertTrue(any(action["action"] == "restore-skill" for action in restore_actions))

    def test_quarantine_skips_protected_skills(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            protected = write_skill(
                home / ".codex" / "skills" / "codex-primary-runtime",
                "spreadsheets",
                "Excel",
                "Use when editing spreadsheets",
            )

            actions = module.quarantine_skills(home=home, project=None, skill_names=["Excel"])

            self.assertTrue((protected / "SKILL.md").is_file())
            self.assertEqual(actions[0]["action"], "skip-protected")
            self.assertEqual(actions[0]["skill"], "Excel")

    def test_skills_cli_quarantine_outputs_json(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            write_skill(home / ".agents" / "skills", "shared-only", "shared-only", "Use when managing notes")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    module.main(["skills", "quarantine", "shared-only", "--home", str(home), "--format", "json"]),
                    0,
                )

            payload = json.loads(output.getvalue())
            self.assertTrue(any(action["action"] == "quarantine-skill" for action in payload["actions"]))
            self.assertFalse((home / ".agents" / "skills" / "shared-only").exists())


if __name__ == "__main__":
    unittest.main()
