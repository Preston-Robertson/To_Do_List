"""Source-only deployment checks; never import the app or run service actions."""

import configparser
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "examples" / "maintainer-review"
ROLES = ("generate", "publish", "refresh", "notify", "release", "preview", "gateway")
CONTROLLERS = ("generate", "publish", "refresh", "release", "preview")
SECRET_KEYS = {
    "LUIGI_MAINTAINER_GITHUB_TOKEN",
    "LUIGI_MAINTAINER_COPILOT_TOKEN",
    "LUIGI_RELEASE_GITHUB_TOKEN",
    "LUIGI_MAINTAINER_SMTP_PASSWORD",
    "LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY",
    "LUIGI_WEB_UI_TOKEN",
    "LUIGI_WEB_FINANCE_TOKEN",
}


def unit_values(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def environment_values(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in values or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Duplicate or invalid environment key")
        values[key] = value
    return values


class ReviewDeploymentTests(unittest.TestCase):
    def test_explicit_role_dispatch_and_single_environment(self):
        for role in ROLES:
            with self.subTest(role=role):
                path = TEMPLATES / f"luigi-maintainer-{role}.service"
                source = path.read_text(encoding="utf-8")
                service = unit_values(path)["Service"]
                self.assertEqual(source.count("EnvironmentFile="), 1)
                self.assertEqual(service["EnvironmentFile"], f"/etc/luigi-web/maintainer-review/{role}.env")
                self.assertEqual(unit_values(path)["Unit"]["ConditionPathExists"], service["EnvironmentFile"])
                command = shlex.split(service["ExecStart"])
                self.assertNotIn("/bin/sh", command)
                if role == "gateway":
                    self.assertEqual(command[-5:], ["-m", "luigi_web.modules.feedback.test_preview", "--serve", "--port", "58120"])
                else:
                    self.assertIn("luigi_web.modules.feedback.review_worker", command)
                    self.assertEqual(command[-1], "--preview-pending" if role == "preview" else f"--{role}")
                if role in CONTROLLERS:
                    self.assertEqual(command[0], "/usr/bin/flock")
                    self.assertIn("/var/lib/luigi-maintainer/controller.lock", command)

    def test_secret_keys_are_role_specific(self):
        allowed = {
            "generate": {"LUIGI_MAINTAINER_GITHUB_TOKEN", "LUIGI_MAINTAINER_COPILOT_TOKEN"},
            "publish": {"LUIGI_MAINTAINER_GITHUB_TOKEN"},
            "refresh": {"LUIGI_MAINTAINER_GITHUB_TOKEN"},
            "release": {"LUIGI_RELEASE_GITHUB_TOKEN"},
            "notify": {"LUIGI_MAINTAINER_SMTP_PASSWORD"},
            "preview": {"LUIGI_MAINTAINER_GITHUB_TOKEN"},
            "gateway": {"LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY"},
            "web": {"LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY"},
        }
        for role, keys in allowed.items():
            with self.subTest(role=role):
                path = ROOT / "maintainer.env.example" if role == "generate" else TEMPLATES / f"{role}.env.example"
                values = environment_values(path)
                self.assertEqual(values.keys() & SECRET_KEYS, keys)
                for key in keys:
                    self.assertRegex(values[key], r"^<[^>]+>$")
                if role in {"publish", "refresh", "release"}:
                    self.assertEqual(values["LUIGI_MAINTAINER_REQUIRED_CHECKS"], "offline-regression")

    def test_rootless_exceptions_are_limited_to_runtime_roles(self):
        common = (TEMPLATES / "common.conf").read_text(encoding="utf-8")
        self.assertNotIn("NoNewPrivileges=", common)
        self.assertNotIn("CapabilityBoundingSet=", common)
        for setting in ("PrivateDevices", "LockPersonality", "ProtectClock", "ProtectKernelLogs",
                        "ProtectKernelModules", "ProtectKernelTunables", "RestrictAddressFamilies",
                        "RestrictRealtime", "SystemCallArchitectures"):
            self.assertNotIn(setting + "=", common)
        self.assertIn("InaccessiblePaths=/etc/luigi-web", common)
        self.assertIn("ProtectSystem=strict", common)
        for role in ROLES:
            with self.subTest(role=role):
                service = unit_values(TEMPLATES / f"luigi-maintainer-{role}.service")["Service"]
                runtime = role in {"generate", "preview"}
                self.assertEqual(service["NoNewPrivileges"], "false" if runtime else "true")
                self.assertEqual(service["RestrictNamespaces"], "false" if runtime else "true")
                self.assertEqual(service["ProtectControlGroups"], "false" if runtime else "true")
                if runtime:
                    self.assertEqual(service["Delegate"], "yes")
                    self.assertEqual(service["RuntimeDirectoryPreserve"], "yes")
                    self.assertNotIn("CapabilityBoundingSet", service)
                else:
                    self.assertEqual(service["CapabilityBoundingSet"], "")

    def test_non_agent_roles_hide_copilot_state(self):
        for role in ("publish", "refresh", "release", "preview"):
            with self.subTest(role=role):
                source = (TEMPLATES / f"luigi-maintainer-{role}.service").read_text(encoding="utf-8")
                self.assertIn("InaccessiblePaths=-/var/lib/luigi-maintainer/copilot", source)
        for role in ("notify", "gateway"):
            service = unit_values(TEMPLATES / f"luigi-maintainer-{role}.service")["Service"]
            self.assertEqual(service["User"], f"luigi-maintainer-{role}")

    def test_gateway_and_web_do_not_own_preview_state(self):
        for filename in ("luigi-maintainer-gateway.service", "web-queue.conf"):
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text(encoding="utf-8")
                self.assertIn("ReadOnlyPaths=/var/lib/luigi-maintainer-preview", source)
                self.assertNotRegex(source, r"(?m)^ReadWritePaths=.* /var/lib/luigi-maintainer-preview")
        values = environment_values(TEMPLATES / "web.env.example")
        self.assertEqual(values["LUIGI_WEB_MAINTAINER_REVIEW_ENABLED"], "0")
        self.assertEqual(values["LUIGI_WEB_RELEASE_ENABLED"], "0")
        self.assertNotEqual(values["LUIGI_MAINTAINER_UI_URL"], values["LUIGI_MAINTAINER_PREVIEW_URL"])

    def test_timer_targets_and_cadence(self):
        timers = list(TEMPLATES.glob("*.timer"))
        self.assertEqual(len(timers), 6)
        for path in timers:
            with self.subTest(path=path.name):
                timer = unit_values(path)["Timer"]
                self.assertEqual(timer["Unit"], path.with_suffix(".service").name)
                self.assertTrue((TEMPLATES / timer["Unit"]).is_file())
                if path.stem.endswith("-generate"):
                    self.assertEqual(timer["OnCalendar"], "*-*-* 03:30:00")
                else:
                    self.assertEqual(timer["OnUnitInactiveSec"], "1m")

    def test_installer_is_explicit_inactive_and_does_not_load_secrets(self):
        source = (ROOT / "scripts" / "install_maintainer.sh").read_text(encoding="utf-8")
        self.assertIn('--install) [ "$#" -eq 1 ] || exit 2', source)
        self.assertLess(source.index('--install)'), source.index('id -u'))
        self.assertNotRegex(source, r"systemctl\s+(?:enable|start|restart|try-restart)\b")
        self.assertIn("systemctl disable --now luigi-maintainer.timer", source)
        self.assertIn('systemctl is-active --quiet "$unit"', source)
        self.assertIn('systemctl disable --now "luigi-maintainer-$role.timer"', source)
        self.assertNotRegex(source, r"(?m)^\s*(?:source |\. |pip |podman |curl |wget )")
        self.assertNotIn("ENV_FILE=", source)
        self.assertNotIn("cat >", source)
        self.assertIn('"$ROLE_ENV_DIR/generate.env.example"', source)
        self.assertIn('"$TEMPLATES/common.conf"', source)
        self.assertIn('generate|preview) ;;', source)
        self.assertIn('"$TEMPLATES/restricted.conf"', source)
        self.assertIn('"$TEMPLATES/web-queue.conf"', source)
        self.assertIn('-m 2750 /var/lib/luigi-maintainer-preview/r', source)
        self.assertIn('-m 0700 /var/lib/luigi-maintainer-preview/sources', source)

    def test_installer_shell_syntax_without_execution(self):
        candidates = [shutil.which("bash"), "C:/Program Files/Git/bin/bash.exe", shutil.which("sh")]
        shell = next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)
        if shell is None:
            self.skipTest("No existing shell available for parse-only validation")
        result = subprocess.run([shell, "-n", str(ROOT / "scripts" / "install_maintainer.sh")],
                                capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_json_examples_parse(self):
        for path in sorted((ROOT / "examples").glob("*.json")):
            with self.subTest(path=path.name):
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_implemented_preview_dispatch_is_documented(self):
        guide = (ROOT / "docs" / "autonomous-maintainer.md").read_text(encoding="utf-8")
        preview = (ROOT / "docs" / "maintainer-test-preview.md").read_text(encoding="utf-8")
        for source in (guide, preview):
            with self.subTest(guide=source.splitlines()[0]):
                for flag in ("--preview UUID", "--preview-pending", "--stop-preview"):
                    self.assertIn(flag, source)
                self.assertIn("oldest eligible", source)
                self.assertIn("multiple queued", source)
                self.assertIn("Linux runtime/setup", source)
                self.assertNotIn("integration prerequisite", source)
                self.assertNotIn("not yet implemented", source)
                self.assertNotIn("AGENTS.md", source)
        self.assertIn("delegates through `maintainer_worker.main`", guide)
        self.assertIn("to `review_worker.main`", guide)
        self.assertIn("parent mounts `review_routes`", preview)
        self.assertIn("/feedback/reviews/{run_id}/preview/", preview)
        self.assertIn("test_preview.create_ticket(run)", preview)
        self.assertIn('method="post"', preview)
        for extension in ("service", "timer"):
            unit = unit_values(TEMPLATES / f"luigi-maintainer-preview.{extension}")
            self.assertIn("oldest eligible", unit["Unit"]["Description"])
            self.assertNotIn("requires", unit["Unit"]["Description"])

    def test_phase_two_guide_keeps_approval_and_artifact_contracts(self):
        guide = (ROOT / "docs" / "autonomous-maintainer.md").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"(?m)^# .+$", guide), ["# Maintenance Review: Phase 2 Operator Guide"])
        self.assertNotIn("Phase 1 converts", guide)
        workflow = guide.split("## Workflow\n", 1)[1].split("## Trust Boundaries\n", 1)[0]
        publication = workflow.split("4. ", 1)[1].split("5. ", 1)[0]
        release = workflow.split("7. ", 1)[1]
        self.assertIn("enters the literal version", publication)
        self.assertIn("before\n  approving publication", publication)
        self.assertIn("confirm the exact version chosen in step 4", release)
        self.assertIn("HEAD", release)
        self.assertIn("literally, not just numerically", release)
        self.assertIn("1.01", guide)
        self.assertIn("default\n  `LUIGI_MAINTAINER_REQUIRED_CHECKS` value is `offline-regression`", guide)
        self.assertIn("do not create a live release or version tag", guide)
        for artifact in ("<option-artifact-uuid>/candidate.patch",
                         "<desktop-screenshot-uuid>/desktop.png",
                         "<mobile-screenshot-uuid>/mobile.png"):
            self.assertIn(artifact, guide)
        self.assertIn("digest verification", guide)

    def test_owned_public_guides_have_existing_local_links(self):
        for filename in ("autonomous-maintainer.md", "maintainer-test-preview.md"):
            path = ROOT / "docs" / filename
            source = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source):
                with self.subTest(guide=filename, target=target):
                    self.assertNotRegex(target, r"^(?:file:|[A-Za-z]:|/)")
                    self.assertNotIn("AGENTS.md", target)
                    self.assertNotIn("architecture.md", target)
                    if "://" not in target and not target.startswith("#"):
                        self.assertTrue((path.parent / target.split("#", 1)[0]).is_file(), target)

    def test_templates_are_ascii_with_final_newline(self):
        paths = [ROOT / "maintainer.env.example", ROOT / "scripts" / "install_maintainer.sh", *TEMPLATES.iterdir()]
        for path in paths:
            if path.is_file():
                with self.subTest(path=path.name):
                    content = path.read_bytes()
                    content.decode("ascii")
                    self.assertTrue(content.endswith(b"\n"))
                    self.assertFalse(any(line.rstrip() != line for line in content.splitlines()))


if __name__ == "__main__":
    unittest.main()
