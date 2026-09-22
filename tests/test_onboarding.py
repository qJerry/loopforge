from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge.cli import build_parser, dispatch
from loopforge.commands import project_doctor
from loopforge.onboarding import onboarding_apply as service_onboarding_apply
from loopforge.onboarding import onboarding_preview as service_onboarding_preview


def onboarding_preview(*args, **kwargs):
    """旧脚手架用例显式使用 builtin，避免把外部 Trellis 初始化混入测试范围。"""
    kwargs.setdefault("owner", "test-owner")
    kwargs.setdefault("planning_adapter", "builtin")
    return service_onboarding_preview(*args, **kwargs)


def onboarding_apply(*args, **kwargs):
    """与旧 preview fixture 使用相同的 builtin onboarding 合同。"""
    kwargs.setdefault("owner", "test-owner")
    kwargs.setdefault("planning_adapter", "builtin")
    return service_onboarding_apply(*args, **kwargs)


class OnboardingTests(unittest.TestCase):
    def _seed_complete_trellis(self, root: Path, owner: str = "galaxy") -> None:
        files = {
            ".trellis/workflow.md": "# Workflow\n",
            ".trellis/config.yaml": "version: 1\n",
            ".trellis/.developer": f"{owner}\n",
            ".agents/skills/trellis-start/SKILL.md": "# Trellis Start\n",
            ".codex/hooks/session-start.py": "# hook\n",
            ".claude/settings.json": "{}\n",
        }
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (root / ".trellis" / "tasks").mkdir(parents=True, exist_ok=True)
        (root / ".trellis" / "workspace").mkdir(parents=True, exist_ok=True)

    def test_preview_generates_complete_contract_for_each_explicit_profile(self) -> None:
        fixtures = {
            "project-group": {},
            "go-backend": {"go.mod": "module example.com/demo\n\ngo 1.23\n"},
            "react-frontend": {"package.json": json.dumps({"dependencies": {"react": "19.0.0"}})},
            "flutter-app": {"pubspec.yaml": "name: demo\ndependencies:\n  flutter:\n    sdk: flutter\n"},
            "java-backend": {"pom.xml": "<project><modelVersion>4.0.0</modelVersion></project>\n"},
            "python-scripts": {"pyproject.toml": "[project]\nname = 'demo'\nversion = '0.1.0'\n"},
        }
        fifth_docs = {
            "project-group": "agent_docs/PROJECT_GROUP.md",
            "go-backend": "agent_docs/DATA_ENGINEERING.md",
            "react-frontend": "agent_docs/DATA_CONTRACT.md",
            "flutter-app": "agent_docs/DATA_CONTRACT.md",
            "java-backend": "agent_docs/DATA_ENGINEERING.md",
            "python-scripts": "agent_docs/SCRIPT_ENGINEERING.md",
        }
        expected_scripts = {
            "project-group": {"build.sh", "format-check.sh", "format.sh", "lint.sh", "test.sh"},
            "go-backend": {
                "build.sh",
                "format-check.sh",
                "format.sh",
                "go-env.sh",
                "lint.sh",
                "setup.sh",
                "setup-tools.sh",
                "test.sh",
                "tool-env.sh",
            },
            "react-frontend": {
                "build.sh",
                "format-check.sh",
                "format.sh",
                "lint.sh",
                "package-manager.sh",
                "setup.sh",
                "test.sh",
            },
            "flutter-app": {
                "build.sh",
                "flutter-env.sh",
                "format-check.sh",
                "format.sh",
                "lint.sh",
                "setup.sh",
                "test.sh",
            },
            "java-backend": {
                "build-tool.sh",
                "build.sh",
                "format-check.sh",
                "format.sh",
                "java-env.sh",
                "lint.sh",
                "test.sh",
            },
            "python-scripts": {
                "build.sh",
                "format-check.sh",
                "format.sh",
                "lint.sh",
                "python-env.sh",
                "setup-tools.sh",
                "test.sh",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            for project_type, files in fixtures.items():
                root = Path(tmp) / project_type
                root.mkdir()
                for rel, content in files.items():
                    (root / rel).write_text(content, encoding="utf-8")

                preview = onboarding_preview(
                    root,
                    project_type,
                    project_id=project_type,
                    build_target="apk" if project_type == "flutter-app" else "",
                )

                self.assertEqual(preview["status"], "completed", preview)
                planned = {entry["path"] for entry in preview["files"]}
                self.assertIn("AGENTS.md", planned)
                self.assertIn(".loopforge/project.json", planned)
                self.assertIn("agent_docs/DEV_TASK_EXECUTION.md", planned)
                self.assertIn(fifth_docs[project_type], planned)
                self.assertEqual(len([path for path in planned if path.startswith("agent_docs/")]), 5)
                self.assertEqual(
                    {Path(path).name for path in planned if path.startswith("scripts/")},
                    expected_scripts[project_type],
                )

    def test_java_profile_accepts_gradle_build_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")

            preview = onboarding_preview(root, "java-backend", project_id="java-service")

            self.assertEqual(preview["status"], "completed", preview)
            self.assertEqual(preview["evidence"]["signals"], ["build.gradle.kts"])

    def test_flutter_profile_requires_and_accepts_explicit_build_target_when_detection_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "projects.json"
            (root / "pubspec.yaml").write_text(
                "name: demo\ndependencies:\n  flutter:\n    sdk: flutter\n",
                encoding="utf-8",
            )

            blocked = onboarding_preview(root, "flutter-app", project_id="flutter-app")
            preview = onboarding_preview(
                root,
                "flutter-app",
                project_id="flutter-app",
                build_target="apk",
            )

            self.assertEqual(blocked["status"], "blocked", blocked)
            self.assertTrue(any(item["code"] == "flutter_build_target_required" for item in blocked["blockers"]))
            self.assertEqual(preview["status"], "completed", preview)
            build = next(entry for entry in preview["files"] if entry["path"] == "scripts/build.sh")
            self.assertIn('BUILD_TARGET="${1:-apk}"', build["content"])
            applied = onboarding_apply(
                config,
                root,
                "flutter-app",
                project_id="flutter-app",
                build_target="apk",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)
            doctor = project_doctor(config, "flutter-app")
            self.assertTrue(doctor["valid"], doctor)
            self.assertFalse(any(check["id"] == "flutter_build_target_required" for check in doctor["checks"]))

    def test_flutter_build_target_rejects_unknown_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pubspec.yaml").write_text(
                "name: demo\ndependencies:\n  flutter:\n    sdk: flutter\n",
                encoding="utf-8",
            )

            preview = onboarding_preview(
                root,
                "flutter-app",
                project_id="flutter-app",
                build_target="server",
            )

            self.assertEqual(preview["status"], "blocked", preview)
            self.assertTrue(any(item["code"] == "flutter_build_target_invalid" for item in preview["blockers"]))


    def test_preview_requires_explicit_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

            preview = service_onboarding_preview(root, "go-backend", project_id="demo")

            self.assertEqual(preview["status"], "blocked", preview)
            self.assertTrue(any(item["code"] == "onboarding_owner_required" for item in preview["blockers"]))


    def test_preview_rejects_unknown_development_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

            preview = service_onboarding_preview(
                root,
                "go-backend",
                project_id="demo",
                owner="galaxy",
                planning_adapter="unknown",
            )

            self.assertEqual(preview["status"], "blocked", preview)
            self.assertTrue(any(item["code"] == "planning_adapter_invalid" for item in preview["blockers"]))


    def test_apply_reuses_complete_trellis_without_running_init_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "projects.json"
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            self._seed_complete_trellis(root)
            preview = service_onboarding_preview(root, "go-backend", project_id="demo", owner="galaxy")

            with patch("loopforge.onboarding.subprocess.run") as run:
                applied = service_onboarding_apply(
                    config,
                    root,
                    "go-backend",
                    project_id="demo",
                    owner="galaxy",
                    plan_hash=preview["plan_hash"],
                )

            self.assertEqual(applied["status"], "completed", applied)
            run.assert_not_called()

    def test_apply_builtin_and_openspec_never_run_trellis(self) -> None:
        for provider in ("builtin", "openspec"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "projects.json"
                (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
                preview = service_onboarding_preview(
                    root,
                    "go-backend",
                    project_id="demo",
                    owner="galaxy",
                    planning_adapter=provider,
                )

                with patch("loopforge.onboarding.subprocess.run") as run:
                    applied = service_onboarding_apply(
                        config,
                        root,
                        "go-backend",
                        project_id="demo",
                        owner="galaxy",
                        planning_adapter=provider,
                        plan_hash=preview["plan_hash"],
                    )

                self.assertEqual(applied["status"], "completed", applied)
                run.assert_not_called()

    def test_owner_change_invalidates_confirmed_onboarding_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "projects.json"
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            alice = service_onboarding_preview(root, "go-backend", project_id="demo", owner="alice")
            bob = service_onboarding_preview(root, "go-backend", project_id="demo", owner="bob")

            applied = service_onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                owner="bob",
                plan_hash=alice["plan_hash"],
            )

            self.assertNotEqual(alice["plan_hash"], bob["plan_hash"])
            self.assertEqual(applied["code"], "onboarding_plan_changed", applied)
            self.assertFalse(config.exists())

    def test_explicit_planning_adapter_overrides_detected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            (root / ".trellis").mkdir()

            preview = onboarding_preview(root, "go-backend", project_id="demo", planning_adapter="builtin")

            self.assertEqual(preview["status"], "completed", preview)
            self.assertEqual(preview["planning_adapter"], "builtin")

    def test_every_generated_profile_script_has_valid_bash_syntax(self) -> None:
        fixtures = {
            "project-group": {},
            "go-backend": {"go.mod": "module example.com/demo\n\ngo 1.23\n"},
            "react-frontend": {"package.json": json.dumps({"dependencies": {"react": "19.0.0"}})},
            "flutter-app": {"pubspec.yaml": "name: demo\ndependencies:\n  flutter:\n    sdk: flutter\n"},
            "java-backend": {"pom.xml": "<project><modelVersion>4.0.0</modelVersion></project>\n"},
            "python-scripts": {"pyproject.toml": "[project]\nname = 'demo'\nversion = '0.1.0'\n"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for project_type, files in fixtures.items():
                root = Path(tmp) / project_type
                root.mkdir()
                for rel, content in files.items():
                    (root / rel).write_text(content, encoding="utf-8")
                preview = onboarding_preview(
                    root,
                    project_type,
                    project_id=project_type,
                    build_target="apk" if project_type == "flutter-app" else "",
                )

                for entry in preview["files"]:
                    if not entry["path"].startswith("scripts/") or not entry["path"].endswith(".sh"):
                        continue
                    result = subprocess.run(
                        ["bash", "-n"],
                        input=entry["content"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, f"{project_type}/{entry['path']}: {result.stderr}")

    def test_go_build_script_does_not_leave_binary_in_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            (root / "main.go").write_text(
                'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("demo") }\n',
                encoding="utf-8",
            )
            config = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="demo")
            applied = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)

            result = subprocess.run(
                [str(root / "scripts" / "build.sh")],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "demo").exists(), "build 不应在项目根目录留下二进制")


    def test_go_dependency_merge_preserves_existing_versions_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            original = (
                "module example.com/demo\n\n"
                "go 1.23\n\n"
                "require github.com/cucumber/godog v0.15.0 // keep project version\n\n"
                "replace example.com/local => ./local\n"
            )
            (root / "go.mod").write_text(original, encoding="utf-8")
            config = Path(tmp) / "projects.json"

            preview = onboarding_preview(root, "go-backend", project_id="demo")
            go_mod_plan = next(entry for entry in preview["files"] if entry["path"] == "go.mod")
            self.assertIn("github.com/cucumber/godog v0.15.0 // keep project version", go_mod_plan["content"])
            self.assertEqual(go_mod_plan["content"].count("github.com/cucumber/godog"), 1)
            self.assertIn("replace example.com/local => ./local", go_mod_plan["content"])

            applied = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)
            repeated = onboarding_preview(root, "go-backend", project_id="demo")
            repeated_go_mod = next(entry for entry in repeated["files"] if entry["path"] == "go.mod")
            self.assertEqual(repeated_go_mod["action"], "unchanged")

    def test_react_profile_generates_fixed_dependencies_runner_and_doctor_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "react-project"
            root.mkdir()
            package = {
                "name": "demo",
                "dependencies": {
                    "react": "19.0.0",
                    "@cucumber/cucumber": "11.0.0",
                },
                "scripts": {
                    "format": "echo format",
                    "format:check": "echo format-check",
                    "lint": "echo lint",
                    "test": "echo test",
                    "build": "echo build",
                },
            }
            (root / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"

            preview = onboarding_preview(root, "react-frontend", project_id="demo")

            self.assertEqual(preview["status"], "completed", preview)
            planned = {entry["path"] for entry in preview["files"]}
            self.assertIn("package.json", planned)
            package_plan = next(entry for entry in preview["files"] if entry["path"] == "package.json")
            self.assertTrue(package_plan["content"].splitlines()[1].startswith("    "))
            desired = json.loads(package_plan["content"])
            self.assertEqual(desired["dependencies"]["react"], "19.0.0")
            self.assertEqual(desired["dependencies"]["@umijs/max"], "^4.4.12")
            self.assertEqual(desired["devDependencies"]["ws"], "^8.21.0")

            applied = onboarding_apply(
                config,
                root,
                "react-frontend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)
            doctor = project_doctor(config, "demo")
            self.assertTrue(doctor["valid"], doctor)


    def test_dependency_manifest_change_invalidates_onboarding_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="demo")
            (root / "go.mod").write_text(
                "module example.com/demo\n\ngo 1.23\n\nrequire example.com/other v1.0.0\n",
                encoding="utf-8",
            )

            applied = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )

            self.assertEqual(applied["status"], "blocked", applied)
            self.assertEqual(applied["code"], "onboarding_plan_changed")


    def test_go_doctor_warns_when_project_local_quality_tools_are_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="demo")
            applied = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)

            doctor = project_doctor(config, "demo")

            self.assertTrue(doctor["valid"], doctor)
            self.assertTrue(any(check["id"] == "go_quality_tools_verify" for check in doctor["checks"]))

    def test_go_profile_generates_gitignore_and_lint_config_with_effective_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="demo")
            applied = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)
            self.assertTrue((root / ".golangci.yml").is_file())

            ignored_paths = [
                ".tools/bin/goimports",
                ".loopforge/state.json",
                "bin/demo",
                "coverage.out",
                ".env",
                ".DS_Store",
            ]
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", *ignored_paths],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ignored_paths)

    def test_go_test_script_supports_unit_integration_all_and_rejects_unknown_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            (root / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
            integration_dir = root / "test" / "integration"
            integration_dir.mkdir(parents=True)
            (integration_dir / "smoke_test.go").write_text(
                "//go:build integration\n\n"
                "package integration\n\n"
                'import "testing"\n\n'
                "func TestSmoke(t *testing.T) {}\n",
                encoding="utf-8",
            )
            config = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="demo")
            applied = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)

            results = {
                mode: subprocess.run(
                    [str(root / "scripts" / "test.sh"), mode],
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                for mode in ("unit", "integration", "all", "unknown")
            }

            self.assertEqual(results["unit"].returncode, 0, results["unit"].stderr)
            self.assertNotIn("test/integration", results["unit"].stdout)
            self.assertEqual(results["integration"].returncode, 0, results["integration"].stderr)
            self.assertIn("test/integration", results["integration"].stdout)
            self.assertEqual(results["all"].returncode, 0, results["all"].stderr)
            self.assertIn("test/integration", results["all"].stdout)
            self.assertEqual(results["unknown"].returncode, 2)

    def test_unified_onboarding_script_dry_run_is_read_only(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "onboard-project.sh"
        if not script.is_file():
            self.fail("缺少统一 onboarding 脚本 scripts/onboard-project.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "flutter-project"
            root.mkdir()
            (root / "pubspec.yaml").write_text(
                "name: demo\ndependencies:\n  flutter:\n    sdk: flutter\n",
                encoding="utf-8",
            )
            config = Path(tmp) / "projects.json"

            result = subprocess.run(
                [
                    str(script),
                    "--config",
                    str(config),
                    "--root",
                    str(root),
                    "--type",
                    "flutter-app",
                    "--project-id",
                    "demo",
                    "--owner",
                    "test-owner",
                    "--planning-adapter",
                    "builtin",
                    "--build-target",
                    "apk",
                    "--dry-run",
                ],
                check=False,
                cwd=script.parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "completed")
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse(config.exists())

    def test_unified_onboarding_script_apply_and_doctor_use_the_same_service(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "onboard-project.sh"
        if not script.is_file():
            self.fail("缺少统一 onboarding 脚本 scripts/onboard-project.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-project"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="demo")

            applied = subprocess.run(
                [
                    str(script),
                    "--config",
                    str(config),
                    "--root",
                    str(root),
                    "--type",
                    "go-backend",
                    "--project-id",
                    "demo",
                    "--owner",
                    "test-owner",
                    "--planning-adapter",
                    "builtin",
                    "--apply",
                    "--plan-hash",
                    preview["plan_hash"],
                ],
                check=False,
                cwd=script.parents[1],
                capture_output=True,
                text=True,
            )
            doctor = subprocess.run(
                [str(script), "--config", str(config), "--project-id", "demo", "--doctor"],
                check=False,
                cwd=script.parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(json.loads(applied.stdout)["status"], "completed")
            self.assertTrue((root / "scripts" / "go-env.sh").is_file())
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertTrue(json.loads(doctor.stdout)["valid"])

    def test_new_agents_is_a_neutral_router_without_onboarding_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")

            preview = onboarding_preview(root, "go-backend", project_id="demo")

            agents = next(entry for entry in preview["files"] if entry["path"] == "AGENTS.md")
            self.assertEqual(agents["action"], "create")
            self.assertNotIn("LOOPFORGE:AGENT-DOCS", agents["content"])
            self.assertNotIn("# 项目 Agent 入口", agents["content"])
            self.assertNotIn("由 LoopForge 统一更新", agents["content"])
            self.assertIn("agent_docs/DEV_TASK_EXECUTION.md", agents["content"])

    def test_existing_complete_agents_router_is_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            original = """# 团队规则

- `agent_docs/ARCH.md`
- `agent_docs/BUILD.md`
- `agent_docs/TEST.md`
- `agent_docs/DEV_TASK_EXECUTION.md`
- `agent_docs/DATA_ENGINEERING.md`
"""
            (root / "AGENTS.md").write_text(original, encoding="utf-8")

            preview = onboarding_preview(root, "go-backend", project_id="demo")

            agents = next(entry for entry in preview["files"] if entry["path"] == "AGENTS.md")
            self.assertEqual(agents["action"], "unchanged")
            self.assertEqual(agents["content"], original)

    def test_existing_legacy_agents_block_is_removed_without_duplicate_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            own_router = """# 团队规则

- `agent_docs/ARCH.md`
- `agent_docs/BUILD.md`
- `agent_docs/TEST.md`
- `agent_docs/DEV_TASK_EXECUTION.md`
- `agent_docs/DATA_ENGINEERING.md`
"""
            legacy = """
<!-- LOOPFORGE:AGENT-DOCS:START -->
# 项目 Agent 入口

- `agent_docs/ARCH.md`
- `agent_docs/BUILD.md`
- `agent_docs/TEST.md`
- `agent_docs/DEV_TASK_EXECUTION.md`
- `agent_docs/DATA_ENGINEERING.md`
<!-- LOOPFORGE:AGENT-DOCS:END -->
"""
            (root / "AGENTS.md").write_text(own_router + legacy, encoding="utf-8")

            preview = onboarding_preview(root, "go-backend", project_id="demo")

            agents = next(entry for entry in preview["files"] if entry["path"] == "AGENTS.md")
            self.assertEqual(agents["action"], "update")
            self.assertNotIn("LOOPFORGE:AGENT-DOCS", agents["content"])
            self.assertNotIn("# 项目 Agent 入口", agents["content"])
            self.assertEqual(agents["content"].count("agent_docs/ARCH.md"), 1)

    def test_existing_incomplete_agents_gets_only_a_neutral_minimal_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("# 团队自有说明\n\n不得删除。\n", encoding="utf-8")

            preview = onboarding_preview(root, "go-backend", project_id="demo")

            agents = next(entry for entry in preview["files"] if entry["path"] == "AGENTS.md")
            self.assertEqual(agents["action"], "update")
            self.assertIn("不得删除。", agents["content"])
            self.assertIn("## Agent 文档路由", agents["content"])
            self.assertNotIn("LOOPFORGE:AGENT-DOCS", agents["content"])
            self.assertNotIn("由 LoopForge 统一更新", agents["content"])
            for path in (
                "agent_docs/ARCH.md",
                "agent_docs/BUILD.md",
                "agent_docs/TEST.md",
                "agent_docs/DEV_TASK_EXECUTION.md",
                "agent_docs/DATA_ENGINEERING.md",
            ):
                self.assertIn(path, agents["content"])

    def test_v1_managed_script_with_project_customization_is_preserved_during_v2_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            (root / "scripts").mkdir()
            customized = """#!/usr/bin/env bash
# Generated by LoopForge onboarding v1; managed
set -euo pipefail
echo '项目自定义构建'
"""
            (root / "scripts" / "build.sh").write_text(customized, encoding="utf-8")
            (root / "scripts" / "build.sh").chmod(0o755)

            preview = onboarding_preview(root, "go-backend", project_id="demo")

            build = next(entry for entry in preview["files"] if entry["path"] == "scripts/build.sh")
            self.assertEqual(build["action"], "skip")
            self.assertEqual((root / "scripts" / "build.sh").read_text(encoding="utf-8"), customized)

    def test_existing_custom_script_gets_executable_permission_without_content_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-service"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            (root / "scripts").mkdir()
            custom = "#!/usr/bin/env bash\necho '项目自定义环境'\n"
            helper = root / "scripts" / "go-env.sh"
            helper.write_text(custom, encoding="utf-8")
            helper.chmod(0o644)
            config = Path(tmp) / "projects.json"

            preview = onboarding_preview(root, "go-backend", project_id="demo")
            planned = next(entry for entry in preview["files"] if entry["path"] == "scripts/go-env.sh")

            self.assertEqual(planned["action"], "chmod")
            result = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(helper.read_text(encoding="utf-8"), custom)
            self.assertTrue(helper.stat().st_mode & 0o111)

    def test_project_group_preview_uses_only_explicitly_selected_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "group"
            selected = root / "selected-api"
            omitted = root / "omitted-ui"
            for child in (selected, omitted):
                (child / ".git").mkdir(parents=True)

            preview = onboarding_preview(
                root,
                "project-group",
                project_id="group",
                project_group_children=[{"key": "api", "path": str(selected)}],
            )

            self.assertEqual(preview["status"], "completed", preview)
            self.assertEqual(
                preview["profile"]["project_group"]["children"],
                [{"key": "api", "path": str(selected.resolve()), "repo": "selected-api"}],
            )

    def test_apply_is_hash_guarded_idempotent_and_preserves_existing_agents_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-service"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/service\n\ngo 1.23\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("# 团队自有说明\n\n不得删除。\n", encoding="utf-8")
            config_path = Path(tmp) / "projects.json"
            preview = onboarding_preview(root, "go-backend", project_id="go-service")

            mismatch = onboarding_apply(
                config_path,
                root,
                "go-backend",
                project_id="go-service",
                plan_hash="invalid",
            )
            self.assertEqual(mismatch["status"], "blocked")

            applied = onboarding_apply(
                config_path,
                root,
                "go-backend",
                project_id="go-service",
                plan_hash=preview["plan_hash"],
            )
            self.assertEqual(applied["status"], "completed", applied)
            self.assertIn("不得删除。", (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue((root / "agent_docs" / "DEV_TASK_EXECUTION.md").exists())
            self.assertTrue((root / "scripts" / "test.sh").stat().st_mode & 0o111)

            repeated = onboarding_preview(root, "go-backend", project_id="go-service")
            self.assertTrue(all(entry["action"] in {"unchanged", "skip"} for entry in repeated["files"]))

            doctor = project_doctor(config_path, "go-service")
            self.assertEqual(doctor["status"], "completed", doctor)
            self.assertTrue(doctor["valid"])

    def test_project_group_doctor_clears_placeholder_warning_after_aggregators_are_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "group"
            child = root / "api"
            (child / ".git").mkdir(parents=True)
            config_path = Path(tmp) / "projects.json"
            selected = [{"key": "api", "path": str(child)}]
            preview = onboarding_preview(
                root,
                "project-group",
                project_id="group",
                project_group_children=selected,
            )
            applied = onboarding_apply(
                config_path,
                root,
                "project-group",
                project_id="group",
                plan_hash=preview["plan_hash"],
                project_group_children=selected,
            )
            self.assertEqual(applied["status"], "completed", applied)

            before = project_doctor(config_path, "group")
            self.assertTrue(
                any(check["id"] == "project_group_quality_commands_required" for check in before["checks"])
            )

            for name in ("format", "format-check", "lint", "test", "build"):
                (root / "scripts" / f"{name}.sh").write_text(
                    "#!/usr/bin/env bash\nset -euo pipefail\n./api/scripts/" + name + ".sh\n",
                    encoding="utf-8",
                )
            after = project_doctor(config_path, "group")

            self.assertFalse(
                any(check["id"] == "project_group_quality_commands_required" for check in after["checks"])
            )

    def test_explicit_profile_must_match_repository_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/service\n", encoding="utf-8")

            preview = onboarding_preview(root, "react-frontend", project_id="wrong")

            self.assertEqual(preview["status"], "blocked")
            self.assertTrue(any(blocker["code"] == "profile_evidence_missing" for blocker in preview["blockers"]))

    def test_empty_root_never_falls_back_to_loopforge_working_directory(self) -> None:
        preview = onboarding_preview("", "go-backend", project_id="unsafe")

        self.assertEqual(preview["status"], "blocked")
        self.assertTrue(any(blocker["code"] == "project_root_missing" for blocker in preview["blockers"]))

    def test_cli_exposes_onboard_preview_apply_and_doctor(self) -> None:
        parser = build_parser()
        preview_args = parser.parse_args(
            [
                "--config",
                "/tmp/projects.json",
                "project",
                "onboard",
                "--root",
                "/tmp/demo",
                "--type",
                "go-backend",
                "--project-id",
                "demo",
                "--owner",
                "galaxy",
                "--build-target",
                "apk",
                "--dry-run",
            ]
        )
        doctor_args = parser.parse_args(["project", "doctor", "demo"])

        self.assertEqual(preview_args.project_command, "onboard")
        self.assertTrue(preview_args.dry_run)
        self.assertEqual(preview_args.owner, "galaxy")
        self.assertEqual(preview_args.planning_adapter, "builtin")
        self.assertEqual(preview_args.build_target, "apk")
        self.assertEqual(doctor_args.project_command, "doctor")

    def test_cli_project_group_child_flags_control_preview_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "group"
            api = root / "api-repo"
            ignored = root / "ignored-repo"
            for child in (api, ignored):
                (child / ".git").mkdir(parents=True)
            args = build_parser().parse_args(
                [
                    "project",
                    "onboard",
                    "--root",
                    str(root),
                    "--type",
                    "project-group",
                    "--project-id",
                    "group",
                    "--owner",
                    "galaxy",
                    "--child",
                    f"api={api}",
                    "--dry-run",
                ]
            )

            preview = dispatch(args)

            children = preview["profile"]["project_group"]["children"]
            self.assertEqual([child["key"] for child in children], ["api"])

    def test_registration_conflict_blocks_before_any_project_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "new-project"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/new\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {"projects": [{"id": "demo", "name": "Existing", "root_dir": str(Path(tmp) / "other")}]}
                ),
                encoding="utf-8",
            )
            preview = onboarding_preview(root, "go-backend", project_id="demo")

            result = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )

            self.assertEqual(result["status"], "blocked")
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "data" / "dev-task.json").exists())

    def test_reonboarding_existing_registration_converges_to_safe_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-service"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "demo",
                                "name": "旧名称",
                                "root_dir": str(root),
                                "project_type": "",
                                "planning_adapter": "builtin",
                                "automation_mode": "execute",
                                "schedule_enabled": True,
                                "auto_commit": True,
                                "worktree_managed": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            preview = onboarding_preview(root, "go-backend", project_id="demo")

            result = onboarding_apply(
                config,
                root,
                "go-backend",
                project_id="demo",
                plan_hash=preview["plan_hash"],
            )

            self.assertEqual(result["status"], "completed", result)
            registered = json.loads(config.read_text(encoding="utf-8"))["projects"][0]
            self.assertNotIn("project_type", registered)
            self.assertNotIn("bdd", registered)
            tracked = json.loads((root / ".loopforge" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(tracked["project_type"], "go-backend")
            self.assertEqual(registered["automation_mode"], "off")
            self.assertFalse(registered["schedule_enabled"])
            self.assertFalse(registered["auto_commit"])
            self.assertTrue(registered["worktree_managed"])

    def test_invalid_existing_dev_task_blocks_during_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "dev-task.json").write_text("[]\n", encoding="utf-8")

            preview = onboarding_preview(root, "go-backend", project_id="demo")

            self.assertEqual(preview["status"], "blocked")
            self.assertTrue(any(blocker["code"] == "dev_task_invalid" for blocker in preview["blockers"]))


if __name__ == "__main__":
    unittest.main()
