"""Synthetic contract tests: never start Podman or execute candidate source."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, call, patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "module-repos/feedback/src/luigi_web/modules/feedback/sandbox.py"
SPEC = importlib.util.spec_from_file_location("_trusted_sandbox_test_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
sandbox = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sandbox
SPEC.loader.exec_module(sandbox)
IMAGE = "localhost/luigi-maintainer@sha256:" + "a" * 64
ENVIRONMENT = {"LUIGI_MAINTAINER_SANDBOX_IMAGE": IMAGE, "HOME": "/synthetic-worker"}
RUNNER_SPEC = importlib.util.spec_from_file_location("_trusted_sandbox_runner_tests", ROOT / "scripts/maintainer_sandbox_check.py")
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(runner)


def synthetic_png(width, height, *, pixels=None):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    data = pixels if pixels is not None else (b"\x00" + b"\x21\x43\x65" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b"")


def publication_paths(root):
    """Read Git metadata in a checkout, or source metadata in its Git-free export."""
    if (root / ".git").exists():
        git = shutil.which("git") or ("C:/Program Files/Git/cmd/git.exe" if sys.platform == "win32" else "/usr/bin/git")
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if key in os.environ}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
        result = subprocess.run(
            [git, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
             "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"],
            cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        return sorted(set(result.stdout.decode("utf-8").rstrip("\0").split("\0"))) if result.stdout else []
    names = []
    for directory, children, files in os.walk(root):
        current = Path(directory)
        children[:] = [name for name in children if (
            (current != root or name in sandbox.SOURCE_ROOTS or name == ".github")
            and name.casefold() not in sandbox.DENIED_PARTS
            and (not name.startswith(".") or name == ".github")
            and not name.casefold().startswith("local")
            and not any(word in name.casefold() for word in ("credential", "secret", "token", ".env"))
            and not (current / name).is_symlink()
        )]
        names.extend((current / name).relative_to(root).as_posix() for name in files)
    return sorted(names)


class PublicationExportTests(unittest.TestCase):
    def test_actual_publication_tree_exports_mandatory_resources_without_execution(self):
        features = ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback")
        roles = ("generate", "publish", "refresh", "notify", "release", "preview", "gateway")
        expected = {
            "app.py", "pyproject.toml", "README.md", "SECURITY.md", ".gitignore",
            "requirements.txt", "requirements-modules.txt", "maintainer.env.example",
            "luigi-maintainer.service", "luigi-maintainer.timer", "luigi-web.service", "luigi-web-preview.service",
            "examples/maintainer-sandbox.Dockerfile", "examples/example-module/pyproject.toml",
            "scripts/maintainer_sandbox_check.py", "scripts/maintainer_preview_app.py", "scripts/validate_repo.py",
            "scripts/install_maintainer.sh", "docs/autonomous-maintainer.md",
            "examples/maintainer-review/common.conf", "examples/maintainer-review/restricted.conf",
            "examples/maintainer-review/web-queue.conf",
        }
        expected.update(f"module-repos/{feature}/{name}" for feature in features for name in (
            "pyproject.toml", "README.md", ".gitignore", ".github/workflows/release.yml", "tests/test_package.py",
        ))
        expected.update(f"examples/maintainer-review/luigi-maintainer-{role}.service" for role in roles)
        expected.update(f"examples/maintainer-review/luigi-maintainer-{role}.timer" for role in roles if role != "gateway")
        expected.update(f"examples/maintainer-review/{role}.env.example" for role in (*roles[1:], "web"))
        expected.update(script for script, _ in runner.PREVIEW_TARGETS.values())
        names = publication_paths(ROOT)
        self.assertTrue(expected.issubset(names), sorted(expected - set(names)))
        expected.update(name for name in names if name.startswith("tests/") and name.endswith((".py", ".js")))
        selected = {name for name in names if sandbox._source_path(name) is not None}
        self.assertTrue(expected.issubset(selected), sorted(expected - selected))
        with tempfile.TemporaryDirectory(prefix="sandbox-export-audit-") as temporary:
            destination = Path(temporary) / "source"
            with patch.object(sandbox, "_listed_files", return_value=names), patch.object(sandbox.subprocess, "run", side_effect=AssertionError("No candidate execution")):
                snapshot = sandbox.export_candidate(ROOT, destination)
            exported = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
            self.assertTrue(expected.issubset(exported), sorted(expected - exported))
            self.assertEqual(snapshot.source_digest, sandbox._snapshot_digest(destination))
            self.assertEqual(snapshot.files, len(exported))
            self.assertLessEqual(snapshot.files, sandbox.MAX_FILES)
            self.assertLessEqual(snapshot.bytes, sandbox.MAX_TOTAL_BYTES)
            for name in exported:
                self.assertNotIn(".git", Path(name).parts)
                self.assertNotIn("data", Path(name).parts)
                self.assertNotEqual(Path(name).name, "AGENTS.md")
                self.assertFalse(name.endswith(".env"))
                self.assertEqual((destination / name).read_bytes(), (ROOT / name).read_bytes())
                if name.startswith("tests/") and name.endswith(".py"):
                    compile((destination / name).read_bytes(), name, "exec")
                if sys.platform == "linux":
                    self.assertEqual((destination / name).stat().st_mode & 0o222, 0)
            with patch.object(subprocess, "run", side_effect=AssertionError("Export must not require Git")):
                self.assertEqual(set(publication_paths(destination)), exported)
            print(f"Publication export audit: {snapshot.files} files, {snapshot.bytes} bytes; digest verified; tests compiled, not executed.")


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()

    def source(self, name="app.py", content="raise RuntimeError('must never execute')\n"):
        path = self.worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_image_requires_exact_local_digest(self):
        self.assertEqual(sandbox._image(ENVIRONMENT), IMAGE)
        for image in ("", "localhost/luigi-maintainer:latest", IMAGE + "\n", IMAGE.replace("localhost/", "registry.example/"), IMAGE.replace("a" * 64, "a" * 63)):
            with self.subTest(image=image), self.assertRaises(sandbox.SandboxError):
                sandbox._image({"LUIGI_MAINTAINER_SANDBOX_IMAGE": image})

    def test_no_runtime_without_prerequisites(self):
        with patch.object(sandbox.subprocess, "run") as run:
            result = sandbox.run_checks(self.worktree, self.artifacts, "proposal", environment={})
        self.assertFalse(result.passed)
        self.assertIsNone(result.exit_code)
        run.assert_not_called()

    def test_non_linux_has_no_fallback(self):
        with patch.object(sandbox.sys, "platform", "win32"), patch.object(sandbox.subprocess, "run") as run:
            self.assertFalse(sandbox.available(environment=ENVIRONMENT).available)
        run.assert_not_called()

    def test_rootless_cgroup_and_seccomp_are_required(self):
        host = {"security": {"rootless": True, "seccompEnabled": True}, "cgroupVersion": "v2", "cgroupControllers": ["cpu", "memory", "pids"]}
        variants = [host, {**host, "security": {"rootless": False}}, {**host, "cgroupVersion": "v1"}, {**host, "cgroupControllers": ["cpu"]}, {**host, "serviceIsRemote": True}]
        with patch.object(sandbox.sys, "platform", "linux"), patch.object(sandbox.os, "geteuid", return_value=1000, create=True):
            for index, variant in enumerate(variants):
                response = subprocess.CompletedProcess([], 0, json.dumps({"host": variant}).encode())
                with self.subTest(index=index), patch.object(sandbox.subprocess, "run", return_value=response) as run:
                    self.assertEqual(sandbox.available(environment=ENVIRONMENT).available, index == 0)
                    self.assertEqual(run.call_args_list[0].args[0], [sandbox.RUNTIME, "--remote=false", "info", "--format=json"])
                    if index == 0:
                        self.assertEqual(run.call_args.args[0], [sandbox.RUNTIME, "--remote=false", "image", "exists", IMAGE])

    def test_runtime_environment_drops_credentials_and_remote_overrides(self):
        environment = {**ENVIRONMENT, "GH_TOKEN": "synthetic-secret", "COPILOT_TOKEN": "synthetic-secret", "CONTAINER_HOST": "ssh://invalid", "DOCKER_HOST": "invalid", "LD_PRELOAD": "invalid", "PYTHONPATH": "invalid", "HTTPS_PROXY": "invalid", "PATH": "/unsafe"}
        result = sandbox._runtime_env(environment)
        self.assertEqual(set(result), {"PATH", "LANG", "LC_ALL", "HOME", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"})
        self.assertNotIn("synthetic-secret", str(result))

    def test_command_has_fixed_isolation_and_no_host_home_mount(self):
        command = sandbox._command(IMAGE, Path("/export"), Path("/artifacts/one"), "luigi-sandbox-fixed", "workspace")
        for flag in ("--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2g", "--cpus=2", "--user=65534:65534", "--unsetenv-all", "--http-proxy=false", "--log-driver=none", "--no-hosts", "--passwd=false", "--image-volume=ignore", "--read-only-tmpfs=false", "--security-opt=mask=/etc/resolv.conf"):
            self.assertIn(flag, command)
        self.assertEqual(command[-5:], [IMAGE, "-I", "/opt/luigi-tests/check.py", "--preview-target", "workspace"])
        mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
        self.assertEqual(mounts, ["type=bind,source=/export,destination=/workspace,ro,relabel=private,bind-nonrecursive", "type=bind,source=/artifacts/one,destination=/output,rw,relabel=private,bind-nonrecursive"])
        self.assertNotIn("/synthetic-worker", " ".join(command))

    def test_export_source_and_new_files_not_private_files(self):
        names = ["app.py", "luigi_web/new.py", "data/ledger.json", "LOCAL_DEPLOYMENT.md", "AGENTS.md", "luigi_web/.env", "luigi_web/.env.production", "luigi_web/credentials.json", "luigi_web/secret.json", "tests/example.py", ".github/workflows/test.yml"]
        for name in names:
            self.source(name)
        with patch.object(sandbox, "_listed_files", return_value=sorted(names)):
            snapshot = sandbox.export_candidate(self.worktree, self.root / "export")
        exported = {path.relative_to(self.root / "export").as_posix() for path in (self.root / "export").rglob("*") if path.is_file()}
        self.assertEqual(exported, {"app.py", "luigi_web/new.py", "tests/example.py", ".github/workflows/test.yml"})
        self.assertEqual(snapshot.files, 4)
        self.assertEqual(snapshot.source_digest, sandbox._snapshot_digest(self.root / "export"))

    def test_export_rejects_hardlinks(self):
        original = self.source()
        os.link(original, self.worktree / "alias.py")
        with patch.object(sandbox, "_listed_files", return_value=["app.py"]), self.assertRaises(sandbox.SandboxError):
            sandbox.export_candidate(self.worktree, self.root / "export")

    def test_export_rejects_symlinks(self):
        original = self.source()
        target = self.worktree / "luigi_web/alias.py"
        target.parent.mkdir()
        try:
            target.symlink_to(original)
        except OSError:
            self.skipTest("Host does not permit creating synthetic symlinks")
        with patch.object(sandbox, "_listed_files", return_value=["luigi_web/alias.py"]), self.assertRaises(sandbox.SandboxError):
            sandbox.export_candidate(self.worktree, self.root / "export")

    def test_rejects_bad_paths_and_case_collisions(self):
        for name in ("../app.py", "/app.py", "tests/../app.py", "tests\\app.py", "tests/x,ro.py", "tests/evil\n.py"):
            with self.subTest(name=name), self.assertRaises(sandbox.SandboxError):
                sandbox._source_path(name)
        self.source()
        with patch.object(sandbox, "_listed_files", return_value=["app.py", "App.py"]), self.assertRaises(sandbox.SandboxError):
            sandbox.export_candidate(self.worktree, self.root / "export")

    def test_export_limits(self):
        self.source(content="12345")
        with patch.object(sandbox, "_listed_files", return_value=["app.py"]), patch.object(sandbox, "MAX_FILE_BYTES", 4), self.assertRaises(sandbox.SandboxError):
            sandbox.export_candidate(self.worktree, self.root / "export")
        self.assertFalse((self.root / "export").exists())

    def fake_runtime(self, command, **kwargs):
        self.assertNotIn("shell", kwargs)
        self.assertEqual(command[0], sandbox.RUNTIME)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        if "run" in command:
            mount = next(value for value in command if "destination=/output," in value)
            output = Path(mount.split("source=", 1)[1].split(",destination=", 1)[0])
            report = {"version": 1, "checks": [{"name": name, "exit_code": 0, "count": 0 if name == "whitespace" else 3} for name in sandbox.CHECK_NAMES[:-1]]}
            (output / "report.json").write_text(json.dumps(report), encoding="utf-8")
            (output / "untrusted.log").write_text("must not persist", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def run_mocked(self, runtime=None):
        self.source()
        with patch.object(sandbox, "available", return_value=sandbox.Availability(True, "synthetic")), patch.object(sandbox, "_listed_files", return_value=["app.py"]), patch.object(sandbox.subprocess, "run", side_effect=runtime or self.fake_runtime):
            return sandbox.run_checks(self.worktree, self.artifacts, "proposal", environment=ENVIRONMENT)

    def test_evidence_is_advisory_and_raw_output_not_retained(self):
        result = self.run_mocked()
        self.assertTrue(result.passed)
        self.assertFalse(result.report_trusted)
        self.assertEqual(result.tests_run, 3)
        self.assertEqual({path.name for path in (self.artifacts / "proposal").iterdir()}, {"evidence.json"})
        self.assertEqual(len(result.source_digest), 64)

    def test_mutating_worktree_invalidates_result(self):
        def runtime(command, **kwargs):
            result = self.fake_runtime(command, **kwargs)
            if "run" in command:
                self.source(content="changed = True\n")
            return result
        result = self.run_mocked(runtime)
        self.assertFalse(result.passed)
        self.assertIn("Candidate changed", result.reason)

    def test_timeout_forces_cleanup_and_never_passes(self):
        commands = []
        def runtime(command, **kwargs):
            commands.append(command)
            if "run" in command:
                raise subprocess.TimeoutExpired(command, 1, output="synthetic private output")
            return subprocess.CompletedProcess(command, 0)
        result = self.run_mocked(runtime)
        self.assertFalse(result.passed)
        self.assertIn("time limit", result.reason)
        self.assertIn("rm", commands[-1])
        self.assertNotIn("private output", str(result))

    def test_report_cannot_inject_text_or_claim_zero_tests_pass(self):
        output = self.root / "output"
        output.mkdir()
        for malicious in ("<script>bad</script>", {"version": 1, "checks": []}, {"version": 1, "checks": [{"name": name, "exit_code": 0, "count": 0} for name in sandbox.CHECK_NAMES[:-1]]}):
            (output / "report.json").write_text(json.dumps(malicious), encoding="utf-8")
            with self.assertRaises(sandbox.SandboxError):
                sandbox._report(output, None)

    def test_user_command_and_path_inputs_are_not_accepted(self):
        for identifier, kind in (("../escape", None), ("proposal", "workspace; env"), ("proposal", "/arbitrary")):
            with self.subTest(identifier=identifier, kind=kind), patch.object(sandbox.subprocess, "run") as run, self.assertRaises(sandbox.SandboxError):
                sandbox.run_checks(self.worktree, self.artifacts, identifier, preview_kind=kind, environment=ENVIRONMENT)
            run.assert_not_called()

    def test_screenshot_validation_checks_crc_dimensions_and_expansion(self):
        content = synthetic_png(390, 844)
        self.assertEqual(sandbox._png_info(content, (390, 844)), (390, 844))
        for invalid in (content[:-1], content + b"hidden", b"not png", synthetic_png(390, 844, pixels=b"\x00" * 2_000_000)):
            with self.assertRaises(sandbox.SandboxError):
                sandbox._png_info(invalid, (390, 844))
        with self.assertRaises(sandbox.SandboxError):
            sandbox._png_info(content, (1440, 900))

    def test_preview_copies_only_two_validated_images(self):
        self.source()
        def runtime(command, **kwargs):
            result = self.fake_runtime(command, **kwargs)
            if "run" in command:
                mount = next(value for value in command if "destination=/output," in value)
                output = Path(mount.split("source=", 1)[1].split(",destination=", 1)[0])
                report = json.loads((output / "report.json").read_text())
                report["checks"].append({"name": "screenshots", "exit_code": 0, "count": 2})
                (output / "report.json").write_text(json.dumps(report))
                (output / "desktop.png").write_bytes(synthetic_png(1440, 900))
                (output / "mobile.png").write_bytes(synthetic_png(390, 844))
            return result
        with patch.object(sandbox, "available", return_value=sandbox.Availability(True, "synthetic")), patch.object(sandbox, "_listed_files", return_value=["app.py"]), patch.object(sandbox.subprocess, "run", side_effect=runtime):
            result = sandbox.run_candidate(self.worktree, self.artifacts, "proposal", environment=ENVIRONMENT)
        self.assertTrue(result.passed)
        self.assertEqual(len(result.screenshots), 2)
        self.assertEqual({path.name for path in (self.artifacts / "proposal").iterdir()}, {"evidence.json", "desktop.png", "mobile.png"})

    def test_git_enumeration_is_fixed_and_includes_new_source(self):
        response = subprocess.CompletedProcess([], 0, b"app.py\0luigi_web/new.py\0")
        with patch.object(sandbox.subprocess, "run", return_value=response) as run:
            self.assertEqual(sandbox._listed_files(self.worktree), ["app.py", "luigi_web/new.py"])
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/git")
        self.assertIn("core.fsmonitor=false", command)
        for value in ("--cached", "--others", "--exclude-standard", "-z"):
            self.assertIn(value, command)
        self.assertNotIn("HOME", run.call_args.kwargs["env"])

    def test_cleanup_failure_rejects_evidence(self):
        def runtime(command, **kwargs):
            if "rm" in command:
                return subprocess.CompletedProcess(command, 1)
            return self.fake_runtime(command, **kwargs)
        result = self.run_mocked(runtime)
        self.assertFalse(result.passed)
        self.assertIn("cleanup", result.reason)

    def test_missing_image_or_runtime_failure_never_passes(self):
        def runtime(command, **kwargs):
            if "run" in command:
                return subprocess.CompletedProcess(command, 125)
            return subprocess.CompletedProcess(command, 0)
        result = self.run_mocked(runtime)
        self.assertFalse(result.passed)
        self.assertEqual(result.exit_code, 125)

    def test_count_and_total_export_limits(self):
        self.source(content="12345")
        with patch.object(sandbox, "_listed_files", return_value=["app.py"]), patch.object(sandbox, "MAX_TOTAL_BYTES", 4), self.assertRaises(sandbox.SandboxError):
            sandbox.export_candidate(self.worktree, self.root / "export")
        response = subprocess.CompletedProcess([], 0, b"app.py\0tests/new.py\0")
        with patch.object(sandbox.subprocess, "run", return_value=response), patch.object(sandbox, "MAX_FILES", 1), self.assertRaises(sandbox.SandboxError):
            sandbox._listed_files(self.worktree)

    def test_export_includes_only_the_known_build_fixture(self):
        self.assertIsNotNone(sandbox._source_path("examples/maintainer-sandbox.Dockerfile"))
        self.assertIsNone(sandbox._source_path("examples/unknown.Dockerfile"))

    def test_export_includes_fixed_root_units_and_module_ignore_files(self):
        allowed = {
            "luigi-maintainer.service", "luigi-maintainer.timer", "luigi-web.service",
            "luigi-web-preview.service", "module-repos/feedback/.gitignore",
            "module-repos/tasks/.github/workflows/release.yml",
        }
        denied = {
            "unknown.service", "examples/unknown.service", "luigi-web.service.env",
            "module-repos/feedback/private/.gitignore", "module-repos/.private/.gitignore",
            "module-repos/feedback/.git/config", ".env.example", "LUIGI-WEB.SERVICE",
        }
        for name in allowed | denied:
            with self.subTest(path=name):
                self.assertEqual(sandbox._source_path(name) is not None, name in allowed)

    def test_only_audited_environment_and_deployment_templates_are_exported(self):
        denied = {
            "maintainer.env", "maintainer.env.example.py", ".env.example",
            "examples/maintainer-review/unknown.env.example", "examples/maintainer-review/.env",
            "examples/maintainer-review/unknown.service", "examples/maintainer-review/unknown.timer",
            "examples/maintainer-review/unknown.conf", "examples/maintainer-review/publish.env",
            "examples/maintainer-review/private/publish.env.example",
            "examples/maintainer-review/PUBLISH.env.example", "examples/module-installer.env.example",
        }
        for name in sandbox.PUBLIC_TEMPLATE_PATHS | denied:
            with self.subTest(path=name):
                self.assertEqual(sandbox._source_path(name) is not None, name not in denied)
        for name in sandbox.PUBLIC_TEMPLATE_PATHS:
            content = "LUIGI_MAINTAINER_REPOSITORY_URL=https://github.com/<owner>/<repository>.git\n" if name.endswith(".env.example") else "[Unit]\nDescription=Synthetic fixture\n"
            self.source(name, content)
        folded = {name.casefold() for name in sandbox.PUBLIC_TEMPLATE_PATHS}
        for name in denied:
            if name.casefold() not in folded:
                self.source(name, "must not be read\n")
        with patch.object(sandbox, "_listed_files", return_value=sorted(sandbox.PUBLIC_TEMPLATE_PATHS | denied)):
            snapshot = sandbox.export_candidate(self.worktree, self.root / "export")
        self.assertEqual(snapshot.files, len(sandbox.PUBLIC_TEMPLATE_PATHS))
        self.assertEqual(snapshot.source_digest, sandbox._snapshot_digest(self.root / "export"))

    def test_environment_examples_reject_filled_values_unknown_keys_and_duplicates(self):
        invalid = (
            "LUIGI_MAINTAINER_GITHUB_TOKEN=ghp_" + "0" * 36 + "\n",
            "LUIGI_MAINTAINER_GITHUB_TOKEN=<unaudited-placeholder>\n",
            "LUIGI_MAINTAINER_MODEL=synthetic-private-value\n",
            "LUIGI_MAINTAINER_REPOSITORY_URL=https://example.invalid/private/repository\n",
            "UNKNOWN_CONFIG=synthetic-private-value\n",
            "LUIGI_MAINTAINER_REMOTE=origin\nLUIGI_MAINTAINER_REMOTE=origin\n",
            "export LUIGI_MAINTAINER_REMOTE=origin\n", "# no assignments\n", "malformed\n",
        )
        for index, content in enumerate(invalid):
            with self.subTest(case=index):
                self.source("maintainer.env.example", content)
                destination = self.root / "export"
                with patch.object(sandbox, "_listed_files", return_value=["maintainer.env.example"]), self.assertRaises(sandbox.SandboxError) as caught:
                    sandbox.export_candidate(self.worktree, destination)
                self.assertEqual(str(caught.exception), "Public environment template contains unaudited assignments.")
                self.assertFalse(destination.exists())

    def test_audited_environment_assignments_match_public_examples(self):
        for name in sorted(sandbox.PUBLIC_TEMPLATE_PATHS):
            if name.endswith(".env.example"):
                with self.subTest(path=name):
                    sandbox._validate_public_template(sandbox.PurePosixPath(name), (ROOT / name).read_bytes())

    def test_page_mapping_never_accepts_record_urls_or_arbitrary_routes(self):
        for page, expected in (("/tasks", "workspace"), ("/discipline", "workspace"), ("/games", "media"), ("/cards", "cards"), ("/finance", "finance")):
            self.assertEqual(sandbox.preview_kind_for_path(page), expected)
        for page in ("https://example.invalid/tasks", "/finance?account=example", "/tasks/example-id", "/admin", "/home#section"):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.preview_kind_for_path(page)

    def test_changed_export_invalidates_even_successful_report(self):
        def runtime(command, **kwargs):
            result = self.fake_runtime(command, **kwargs)
            if "run" in command:
                mount = next(value for value in command if "destination=/workspace," in value)
                source = Path(mount.split("source=", 1)[1].split(",destination=", 1)[0])
                path = source / "app.py"
                path.chmod(0o644)
                path.write_text("changed = True\n")
            return result
        result = self.run_mocked(runtime)
        self.assertFalse(result.passed)
        self.assertIn("Export changed", result.reason)

    def test_report_symlink_is_rejected_without_reading_target(self):
        output = self.root / "output"
        output.mkdir()
        target = output / "report.json"
        target.write_text("synthetic")
        original = Path.is_symlink
        def linked(path):
            return path == target or original(path)
        with patch.object(sandbox.sys, "platform", "win32"), patch.object(Path, "is_symlink", linked), self.assertRaises(sandbox.SandboxError):
            sandbox._report(output, None)

    def test_regular_reader_rejects_nonregular_and_oversized_artifacts(self):
        source = self.source(content="12345")
        with self.assertRaises(sandbox.SandboxError):
            sandbox._regular_bytes(self.worktree, sandbox.PurePosixPath(source.name), 4)
        (self.worktree / "directory.py").mkdir()
        with self.assertRaises(sandbox.SandboxError):
            sandbox._regular_bytes(self.worktree, sandbox.PurePosixPath("directory.py"), 10)

    def test_report_is_size_bounded(self):
        output = self.root / "output"
        output.mkdir()
        (output / "report.json").write_bytes(b" " * (sandbox.MAX_REPORT_BYTES + 1))
        with self.assertRaises(sandbox.SandboxError):
            sandbox._report(output, None)

    def test_unavailable_local_image_does_not_launch_candidate(self):
        host = {"security": {"rootless": True, "seccompEnabled": True}, "cgroupVersion": "v2", "cgroupControllers": ["cpu", "memory", "pids"]}
        results = [subprocess.CompletedProcess([], 0, json.dumps({"host": host}).encode()), subprocess.CompletedProcess([], 1)]
        with patch.object(sandbox.sys, "platform", "linux"), patch.object(sandbox.os, "geteuid", return_value=1000, create=True), patch.object(sandbox.subprocess, "run", side_effect=results) as run:
            result = sandbox.run_checks(self.worktree, self.artifacts, "proposal", environment=ENVIRONMENT)
        self.assertFalse(result.passed)
        self.assertIn("not installed locally", result.reason)
        self.assertTrue(all("run" not in call.args[0] for call in run.call_args_list))

    def test_bad_runtime_info_is_not_reported_verbatim(self):
        with patch.object(sandbox.sys, "platform", "linux"), patch.object(sandbox.os, "geteuid", return_value=1000, create=True):
            for output in (b'{"host": []}', b'{"host": null}', b'synthetic-private-error'):
                with self.subTest(output=output), patch.object(sandbox.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
                    status = sandbox.available(environment=ENVIRONMENT)
                    self.assertFalse(status.available)
                    self.assertNotIn("synthetic-private-error", status.reason)

    def test_failed_preview_keeps_other_checks_without_images(self):
        output = self.root / "output"
        output.mkdir()
        rows = [{"name": name, "exit_code": 0, "count": 0 if name == "whitespace" else 3} for name in sandbox.CHECK_NAMES[:-1]]
        rows.append({"name": "screenshots", "exit_code": 1, "count": 0})
        (output / "report.json").write_text(json.dumps({"version": 1, "checks": rows}))
        checks, images = sandbox._report(output, "workspace")
        self.assertTrue(checks[0].passed)
        self.assertFalse(checks[-1].passed)
        self.assertEqual(images, ())


class RunnerTests(unittest.TestCase):
    def test_preview_bootstraps_probe_and_each_browser_context(self):
        origin = "http://127.0.0.1:8765"
        for kind, (_, route) in runner.PREVIEW_TARGETS.items():
            with self.subTest(kind=kind):
                process = Mock()
                opener = Mock()
                response = MagicMock()
                response.__enter__.return_value.status = 200
                opener.open.return_value = response
                playwright_api = MagicMock()
                browser = playwright_api.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value
                contexts = [MagicMock(), MagicMock()]
                browser.new_context.side_effect = contexts
                image_api = MagicMock()
                converted = image_api.open.return_value.__enter__.return_value.convert.return_value
                converted.getextrema.return_value = ((0, 255),) * 3
                for context, width in zip(contexts, (1440, 390)):
                    page = context.new_page.return_value
                    page.goto.return_value.status = 200
                    page.evaluate.side_effect = [None, {"width": width, "scroll": width, "text": 20}]
                def image_open(path):
                    converted.size = (1440, 900) if not converted.save.call_count else (390, 844)
                    return image_api.open.return_value
                image_api.open.side_effect = image_open
                with patch.dict(sys.modules, {"playwright.sync_api": playwright_api}), patch.object(runner, "import_module", return_value=image_api), patch.object(runner.subprocess, "Popen", return_value=process), patch.object(runner.urllib.request, "build_opener", return_value=opener) as build_opener, patch.object(runner.tempfile, "TemporaryDirectory") as directory, patch.object(runner, "_stop") as stop:
                    directory.return_value.__enter__.return_value = "/synthetic-capture"
                    result = runner._preview(kind)
                self.assertEqual(result, {"name": "screenshots", "exit_code": 0, "count": 2})
                handlers = build_opener.call_args.args
                self.assertEqual(handlers[0].proxies, {})
                self.assertIsInstance(handlers[1], runner.urllib.request.HTTPCookieProcessor)
                self.assertIsInstance(handlers[1].cookiejar, runner.CookieJar)
                self.assertEqual(opener.open.call_args_list, [call(origin + "/", timeout=1), call(origin + route, timeout=1)])
                for context in contexts:
                    self.assertEqual(context.new_page.return_value.goto.call_args_list, [
                        call(origin + "/", wait_until="networkidle", timeout=30_000),
                        call(origin + route, wait_until="networkidle", timeout=30_000),
                    ])
                    handler = context.route.call_args.args[1]
                    for url, allowed in ((origin + route, True), ("https://example.invalid/", False), (origin + ".invalid/", False)):
                        request_route = Mock()
                        request_route.request.url = url
                        handler(request_route)
                        self.assertEqual(request_route.continue_.called, allowed)
                        self.assertEqual(request_route.abort.called, not allowed)
                    context.close.assert_called_once()
                browser.close.assert_called_once()
                stop.assert_called_once_with(process)

    def test_preview_failed_bootstrap_never_captures_or_launches_browser(self):
        process = Mock()
        process.poll.return_value = 1
        response = MagicMock()
        response.__enter__.return_value.status = 503
        opener = Mock()
        opener.open.return_value = response
        playwright_api = MagicMock()
        with patch.dict(sys.modules, {"playwright.sync_api": playwright_api}), patch.object(runner, "import_module"), patch.object(runner.subprocess, "Popen", return_value=process), patch.object(runner.urllib.request, "build_opener", return_value=opener), patch.object(runner, "_stop") as stop:
            result = runner._preview("finance")
        self.assertEqual(result, {"name": "screenshots", "exit_code": 1, "count": 0})
        opener.open.assert_called_once_with("http://127.0.0.1:8765/", timeout=1)
        playwright_api.sync_playwright.assert_not_called()
        stop.assert_called_once_with(process)

    def test_subprocess_output_is_bounded_and_processes_cleaned(self):
        for chunks, limit, expected in (([b"synthetic", b""], 100, 0), ([b"oversized"], 4, 125)):
            process = Mock()
            process.stdout.fileno.return_value = 7
            process.wait.return_value = 0
            monitor = Mock()
            monitor.select.return_value = [(None, None)]
            with self.subTest(expected=expected), patch.object(runner.subprocess, "Popen", return_value=process), patch.object(runner.selectors, "DefaultSelector") as factory, patch.object(runner.os, "read", side_effect=chunks), patch.object(runner, "_kill_group") as kill, patch.object(runner, "MAX_OUTPUT", limit):
                factory.return_value.__enter__.return_value = monitor
                code, output = runner._run(runner.TEST_COMMAND, 5)
            self.assertEqual(code, expected)
            self.assertLessEqual(len(output), runner.TAIL_BYTES)
            kill.assert_called_once_with(process)
            process.stdout.close.assert_called_once()

    def test_subprocess_deadline_always_kills_group(self):
        process = Mock()
        process.wait.return_value = 0
        with patch.object(runner.subprocess, "Popen", return_value=process), patch.object(runner.selectors, "DefaultSelector"), patch.object(runner, "_kill_group") as kill:
            code, output = runner._run(runner.TEST_COMMAND, 0)
        self.assertEqual((code, output), (124, ""))
        kill.assert_called_once_with(process)

    def test_runner_refuses_host_execution_before_child_commands(self):
        with patch.object(sys, "argv", ["check.py"]), patch.object(runner, "_inside_sandbox", return_value=False), patch.object(runner.subprocess, "Popen") as popen, patch("builtins.print"):
            self.assertEqual(runner.main(), 2)
        popen.assert_not_called()

    def test_runner_commands_and_preview_paths_are_fixed(self):
        self.assertEqual(runner.TEST_COMMAND, ("/usr/local/bin/python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"))
        self.assertEqual(runner.VALIDATE_COMMAND, ("/usr/local/bin/python", "-B", "scripts/validate_repo.py"))
        self.assertEqual(set(runner.PREVIEW_TARGETS), set(sandbox.PREVIEW_KINDS))
        for script, route in runner.PREVIEW_TARGETS.values():
            self.assertTrue(script.startswith("scripts/preview_"))
            self.assertTrue(route.startswith("/"))

    def test_runner_requires_real_summary_and_no_skips(self):
        self.assertEqual(runner._test_result(0, "Ran 12 tests in 0.1s\n\nOK\n"), {"name": "tests", "exit_code": 0, "count": 12})
        for output in ("", "OK", "Ran 0 tests in 0.1s\n\nOK\n", "Ran 12 tests in 0.1s\n\nOK (skipped=1)\n"):
            self.assertNotEqual(runner._test_result(0, output)["exit_code"], 0)
        self.assertNotEqual(runner._test_result(7, "Ran 12 tests in 0.1s\n\nOK\n")["exit_code"], 0)

    def test_runner_splits_validation_counts(self):
        rows = runner._validation_results(0, "Validated 5 templates and 8 unique method/path registrations.\n")
        self.assertEqual([row["count"] for row in rows], [5, 8])
        self.assertTrue(all(row["exit_code"] == 0 for row in rows))
        self.assertTrue(all(row["exit_code"] != 0 for row in runner._validation_results(0, "")))

    def test_whitespace_check_uses_synthetic_source_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "synthetic.py"
            path.write_text("example = 1\n", encoding="utf-8")
            self.assertEqual(runner._whitespace(root)["exit_code"], 0)
            path.write_text("example = 1 \n<<<<<<< branch\n", encoding="utf-8")
            self.assertEqual(runner._whitespace(root)["count"], 2)

    def test_build_context_cannot_copy_candidate_tree(self):
        dockerfile = (ROOT / "examples/maintainer-sandbox.Dockerfile").read_text(encoding="utf-8")
        copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
        self.assertEqual(copies, ["COPY requirements.txt /opt/luigi-tests/requirements.txt", "COPY scripts/maintainer_sandbox_check.py /opt/luigi-tests/check.py", "COPY --chmod=0444 scripts/maintainer_preview_app.py /opt/luigi-tests/preview.py"])
        self.assertNotIn("ADD ", "\n".join(line for line in dockerfile.splitlines() if not line.startswith("#")))
        self.assertIn("USER 65534:65534", dockerfile)

    def test_new_source_files_have_clean_whitespace(self):
        for path in (MODULE_PATH, Path(__file__), ROOT / "scripts/maintainer_sandbox_check.py", ROOT / "examples/maintainer-sandbox.Dockerfile"):
            content = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertTrue(content.endswith("\n"))
                self.assertTrue(all(line == line.rstrip() for line in content.splitlines()))


if __name__ == "__main__":
    unittest.main()
