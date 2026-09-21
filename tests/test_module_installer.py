"""Offline installer checks using synthetic releases and disposable source-wheel builds."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx

from luigi_web.core import module_installer as installer
from luigi_web.core import module_repositories as repositories


FEATURES = ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback")
BUILD_LOCAL_WHEELS = """import os
from pathlib import Path
import socket
import sys

def refuse_network(*args, **kwargs):
    raise AssertionError('Wheel builds must remain offline')

socket.socket.connect = refuse_network
socket.getaddrinfo = refuse_network
from setuptools.build_meta import build_wheel

root = Path(sys.argv[1])
for project in sorted((root / 'projects').iterdir()):
    os.chdir(project)
    output = root / 'wheels' / project.name
    output.mkdir(parents=True)
    build_wheel(str(output))
"""


def build_wheel(*, version="1.0", package="luigi_web_extensions.example", distribution="luigi-web-example",
                module_id="example", requirements=(), extra_files=None, metadata_changes="", entry_point=None):
    directory = package.replace(".", "/")
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    files = {f"{directory}/__init__.py": b"",
             f"{directory}/manifest.py": b"module = 'synthetic installed module'\n",
             f"{dist_info}/METADATA": (f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\nRequires-Python: >=3.11\n"
                                       + "".join(f"Requires-Dist: {value}\n" for value in requirements) + metadata_changes).encode(),
             f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
             f"{dist_info}/entry_points.txt": (entry_point or f"[luigi_web.modules]\n{module_id} = {package}.manifest:module\n").encode()}
    files.update(extra_files or {})
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", str(len(content))))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    files[f"{dist_info}/RECORD"] = records.getvalue().encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def rewrite_wheel(content, changes=None, omitted=(), additional=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as original, zipfile.ZipFile(buffer, "w") as archive:
        for info in original.infolist():
            if info.filename not in omitted:
                archive.writestr(info, (changes or {}).get(info.filename, original.read(info)))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, data in additional:
                archive.writestr(name, data)
    return buffer.getvalue()


class InstallerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.start_patch(patch.object(repositories, "DATA_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": "example-owner",
                                                "LUIGI_WEB_MODULE_REPOSITORIES_FILE": ""}))
        self.start_patch(patch("socket.socket.connect", side_effect=AssertionError("Unexpected network access")))
        self.start_patch(patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS lookup")))
        self.source = {"id": "example", "repository": "https://github.com/example-owner/example-module"}

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def register(self):
        return repositories.register_repository(self.source, trust_confirmed=True)

    def queue(self, **changes):
        values = {"source_id": "example", "tag": "v1.0", "asset": "luigi_web_example-1.0-py3-none-any.whl",
                  "sha256": "a" * 64, "trust_confirmed": True}
        values.update(changes)
        return repositories.queue_install(**values)


class RepositoryApprovalTests(InstallerFixture):
    def test_queue_readback_accepts_a_job_already_claimed_by_the_worker(self):
        self.register()
        original = repositories.get_job

        def readback(job_id):
            with repositories._database(write=True) as connection:
                connection.execute("UPDATE jobs SET state = 'installing' WHERE id = ?", (job_id,))
                connection.execute("INSERT INTO worker(slot, job_id) VALUES (1, ?)", (job_id,))
            return original(job_id)

        with patch.object(repositories, "get_job", side_effect=readback):
            result = self.queue()
        self.assertEqual(result["state"], "installing")
        self.assertEqual(original(result["id"])["source"]["id"], "example")

    def test_registration_disabled_without_deployment_owner(self):
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""}):
            with self.assertRaises(repositories.ModuleInstallError):
                self.register()
            self.assertFalse(repositories.repository_policy()["enabled"])

    def test_explicit_boolean_trust_is_required(self):
        invalid_values: tuple[Any, ...] = (False, "true", 1, None)
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(repositories.ModuleInstallError):
                repositories.register_repository(self.source, trust_confirmed=value)
        self.register()
        with self.assertRaises(repositories.ModuleInstallError):
            self.queue(trust_confirmed=False)
        self.assertEqual(repositories.list_jobs(), [])

    def test_urls_and_namespaces_are_strict(self):
        for url in ("http://github.com/example-owner/repo", "https://github.com.evil.test/example-owner/repo",
                    "https://user@github.com/example-owner/repo", "https://github.com/example-owner/repo?x=1",
                    "https://github.com/example-owner/../repo", "https://github.com/example-owner/repo/extra"):
            with self.subTest(url=url), self.assertRaises(repositories.ModuleInstallError):
                repositories.normalize_repository(url)
        self.assertEqual(repositories.normalize_repository("https://github.com/Example-Owner/Example.git"),
                         "https://github.com/example-owner/example")
        with self.assertRaises(repositories.ModuleInstallError):
            repositories.register_repository(dict(self.source, package="luigi_web.core"), trust_confirmed=True)

    def test_source_is_immutable_and_job_is_a_snapshot(self):
        source = self.register()
        self.assertEqual(self.register(), source)
        job = self.queue()
        self.assertEqual(job["source"], source)
        self.assertEqual(job["state"], "pending")
        with self.assertRaises(repositories.ModuleInstallError):
            repositories.register_repository(dict(self.source, repository="https://github.com/example-owner/different"),
                                             trust_confirmed=True)
        self.assertEqual(repositories.get_job(job["id"])["source"], source)

    def test_revoked_owner_cannot_queue(self):
        self.register()
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": "other-owner"}):
            self.assertEqual(repositories.list_repositories(), [])
            with self.assertRaises(repositories.ModuleInstallError):
                self.queue()

    def test_artifacts_are_pinned_pure_wheels(self):
        self.register()
        for changes in ({"tag": "../v1"}, {"asset": "luigi_web_example-1.0-cp311-cp311-win_amd64.whl"},
                        {"asset": "other-1.0-py3-none-any.whl"}, {"sha256": "not-a-hash"}, {"asset": "../example.whl"}):
            with self.subTest(changes=changes), self.assertRaises(repositories.ModuleInstallError):
                self.queue(**changes)
        self.assertFalse((self.root / "module-packages").exists())


class WheelFixture(InstallerFixture):
    def setUp(self):
        super().setUp()
        self.register()
        self.requests = []
        self.content = build_wheel()
        self.release_version = "1.0"
        self.release_source = "example"
        self.release_distribution = "luigi_web_example"
        self.release_repository = self.source["repository"]
        self.client_class = httpx.Client
        self.start_patch(patch.object(installer.httpx, "Client", side_effect=self.client))

    def client(self, **kwargs):
        return self.client_class(transport=httpx.MockTransport(self.respond), **kwargs)

    def respond(self, request):
        self.requests.append(request)
        if request.url.host == "api.github.com":
            asset = f"{self.release_distribution}-{self.release_version}-py3-none-any.whl"
            return httpx.Response(200, json={"tag_name": "v1.0", "draft": False, "assets": [
                {"name": asset, "state": "uploaded", "id": 123,
                 "browser_download_url": f"{self.release_repository}/releases/download/v1.0/{asset}"}]})
        return httpx.Response(200, content=self.content)

    def queued_wheel(self, **changes):
        changes.setdefault("asset", f"{self.release_distribution}-{self.release_version}-py3-none-any.whl")
        changes.setdefault("source_id", self.release_source)
        return self.queue(sha256=hashlib.sha256(self.content).hexdigest(), **changes)

    def select_module(self, module_id):
        self.source = {"id": module_id, "repository": f"https://github.com/example-owner/{module_id}-module"}
        self.register()
        self.release_source = module_id
        self.release_distribution = f"luigi_web_{module_id}"
        self.release_repository = self.source["repository"]


class WheelInstallationTests(WheelFixture):
    def test_already_installed_extra_dependencies_are_accepted_without_imports(self):
        self.content = build_wheel(requirements=["psycopg[binary]>=3.2,<4"])
        versions = {"psycopg": "3.2.10", "psycopg-binary": "3.2.10"}
        distribution = SimpleNamespace(
            metadata=installer._headers(b"Provides-Extra: binary\n"),
            requires=['psycopg-binary==3.2.10; extra == "binary"'],
        )
        with patch.object(installer, "_installed_version", side_effect=versions.get), \
                patch.object(installer.metadata, "distribution", return_value=distribution), \
                patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No entry point loading")):
            result = installer.apply_job(self.queued_wheel()["id"])
            self.assertEqual(result["state"], "installed")
            self.assertEqual(len(installer.installation_paths()), 1)

    def test_real_wheel_stages_and_activates_without_importing(self):
        job = self.queued_wheel()
        with patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No entry point loading")):
            result = installer.apply_job(job["id"])
        self.assertEqual(result["state"], "installed")
        self.assertTrue(result["restart_required"])
        records = installer.installed_modules()
        self.assertEqual(records[0]["version"], "1.0")
        self.assertEqual(len(installer.installation_paths()), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(installer.apply_job(job["id"])["state"], "installed")
        self.assertEqual(len(self.requests), 2)

    def test_hash_mismatch_never_parses_or_activates(self):
        job = self.queue()
        with patch.object(installer.zipfile, "ZipFile", side_effect=AssertionError("No unpinned ZIP parsing")):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(job["id"])
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")
        self.assertEqual(installer.installation_paths(), [])

    def test_revocation_is_checked_before_download_and_at_bootstrap(self):
        job = self.queued_wheel()
        installer.apply_job(job["id"])
        pending = self.queued_wheel()
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""}):
            self.assertEqual(installer.installation_paths(), [])
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(pending["id"])
        self.assertEqual(len(self.requests), 2)

    def test_missing_dependency_preserves_previous_release(self):
        installer.apply_job(self.queued_wheel()["id"])
        original = installer.installation_paths()
        self.content = build_wheel(requirements=["synthetic-missing-dependency>=100"])
        with patch.object(installer, "_installed_version", return_value=None):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(installer.installation_paths(), original)

    def test_only_one_pending_job_is_processed(self):
        first = self.queued_wheel()
        second = self.queued_wheel()
        result = installer.apply_pending()
        assert result is not None
        self.assertEqual(result["id"], first["id"])
        self.assertEqual(repositories.get_job(second["id"])["state"], "pending")


    def test_worker_does_not_change_live_import_paths(self):
        paths = list(sys.path)
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/manifest.py": b"raise RuntimeError('must not execute while staging')\n"})
        installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(sys.path, paths)
        self.assertNotIn("luigi_web_extensions.example.manifest", sys.modules)

    def test_new_process_can_import_approved_namespace_and_discover_metadata(self):
        installer.apply_job(self.queued_wheel()["id"])
        site_path = installer.installation_paths()[0]
        environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}}
        environment.update({key: str(self.root) for key in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "SQLITE_TMPDIR")})
        probe = """import sys
from importlib import metadata
sys.path.insert(0, sys.argv[1])
from luigi_web_extensions.example.manifest import module
assert module == 'synthetic installed module'
distributions = list(metadata.distributions(path=[sys.argv[1]]))
assert len(distributions) == 1
assert next(iter(distributions[0].entry_points)).name == 'example'
print('approved namespace import passed')
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, site_path], env=environment,
                                capture_output=True, text=True, timeout=30, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("approved namespace import passed", result.stdout)

    def test_rollback_uses_only_verified_existing_artifacts(self):
        first = self.queued_wheel()
        installer.apply_job(first["id"])
        first_path = installer.installation_paths()
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/manifest.py": b"module = 'second synthetic release'\n"})
        installer.apply_job(self.queued_wheel()["id"])
        self.assertNotEqual(installer.installation_paths(), first_path)
        rollback = repositories.queue_rollback("example", first["sha256"], trust_confirmed=True)
        self.requests.clear()
        installer.apply_job(rollback["id"])
        self.assertEqual(self.requests, [])
        self.assertEqual(installer.installation_paths(), first_path)

    def test_transaction_failure_preserves_active_release_and_allows_clean_retry(self):
        installer.apply_job(self.queued_wheel()["id"])
        original = installer.installation_paths()
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/new.py": b"value = 2\n"})
        with repositories._database(write=True) as connection:
            connection.execute("CREATE TRIGGER activation_failure BEFORE UPDATE ON active BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        job = self.queued_wheel()
        with self.assertRaises(repositories.ModuleInstallError):
            installer.apply_job(job["id"])
        self.assertEqual(installer.installation_paths(), original)
        self.assertEqual(len(repositories.inventory_records(include_inactive=True)), 1)
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")
        with repositories._database(write=True) as connection:
            connection.execute("DROP TRIGGER activation_failure")
        self.requests.clear()
        installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(self.requests, [])
        self.assertNotEqual(installer.installation_paths(), original)

    def test_post_publish_exception_preserves_previous_active_release(self):
        installer.apply_job(self.queued_wheel()["id"])
        original = installer.installation_paths()
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/new.py": b"value = 2\n"})
        job = self.queued_wheel()
        publish = installer._publish

        def fail_after_publish(published_job, record):
            publish(published_job, record)
            raise RuntimeError("synthetic post-publish failure")

        with patch.object(installer, "_publish", side_effect=fail_after_publish):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(job["id"])
        self.assertEqual(installer.installation_paths(), original)
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")
        self.assertFalse(repositories.get_job(job["id"])["restart_required"])
        self.requests.clear()
        installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(self.requests, [])

    def test_post_publish_verification_failure_removes_first_activation(self):
        job = self.queued_wheel()
        with patch.object(installer, "installed_modules", side_effect=RuntimeError("synthetic verification failure")):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(job["id"])
        self.assertEqual(installer.installation_paths(), [])
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")
        with repositories._database() as connection:
            self.assertIsNone(connection.execute("SELECT job_id FROM worker").fetchone())

    def test_final_job_readback_failure_restores_previous_release(self):
        installer.apply_job(self.queued_wheel()["id"])
        original = installer.installation_paths()
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/new.py": b"value = 2\n"})
        job = self.queued_wheel()
        job_record = repositories._job_record

        def fail_readback(row):
            if row["id"] == job["id"] and row["state"] == "installed":
                raise RuntimeError("synthetic final readback failure")
            return job_record(row)

        with patch.object(repositories, "_job_record", side_effect=fail_readback):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(job["id"])
        self.assertEqual(installer.installation_paths(), original)
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")

    def test_worker_lease_is_retained_until_final_verification(self):
        first = self.queued_wheel()
        second = self.queued_wheel()
        verify = installer.installed_modules

        def verify_with_competing_worker():
            self.assertEqual(repositories.get_job(first["id"])["state"], "installing")
            with self.assertRaises(repositories.ModuleInstallError):
                installer._claim(second["id"])
            return verify()

        with patch.object(installer, "installed_modules", side_effect=verify_with_competing_worker):
            self.assertEqual(installer.apply_job(first["id"])["state"], "installed")
        self.assertEqual(repositories.get_job(second["id"])["state"], "pending")

    def test_crashed_worker_is_not_automatically_retried(self):
        first = self.queued_wheel()
        second = self.queued_wheel()
        installer._claim(first["id"])
        for job in (first, second):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(job["id"])
        self.assertEqual(self.requests, [])
        self.assertEqual(repositories.get_job(first["id"])["state"], "installing")
        self.assertEqual(repositories.get_job(second["id"])["state"], "pending")

    def test_interrupted_job_can_be_explicitly_abandoned_without_retry(self):
        first = self.queued_wheel()
        second = self.queued_wheel()
        installer._claim(first["id"])
        with self.assertRaises(repositories.ModuleInstallError):
            installer.abandon_job(first["id"])
        result = installer.abandon_job(first["id"], worker_stopped_confirmed=True)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.requests, [])
        next_result = installer.apply_pending()
        assert next_result is not None
        self.assertEqual(next_result["id"], second["id"])

    def test_abandoned_worker_cannot_publish_even_if_it_finishes_staging(self):
        job = self.queued_wheel()
        installer._claim(job["id"])
        record = installer._stage(job)
        installer.abandon_job(job["id"], worker_stopped_confirmed=True)
        with self.assertRaises(repositories.ModuleInstallError):
            installer._publish(job, record)
        self.assertEqual(installer.installation_paths(), [])

    def test_concurrent_workers_cannot_claim_the_same_job(self):
        job = self.queued_wheel()

        def claim():
            try:
                installer._claim(job["id"])
                return True
            except repositories.ModuleInstallError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda unused: claim(), range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_corrupted_installed_files_are_not_exposed_at_bootstrap(self):
        installer.apply_job(self.queued_wheel()["id"])
        root = Path(installer.installation_paths()[0])
        (root / "luigi_web_extensions/example/manifest.py").write_text("module = 'altered'\n", encoding="utf-8")
        with self.assertRaises(repositories.ModuleInstallError):
            installer.installation_paths()

    def test_installed_tree_rejects_unexpected_empty_directories(self):
        installer.apply_job(self.queued_wheel()["id"])
        root = Path(installer.installation_paths()[0])
        (root / "unexpected" / "nested").mkdir(parents=True)
        with self.assertRaises(repositories.ModuleInstallError):
            installer.installation_paths()

    def test_installed_tree_discards_unpinned_bytecode_without_executing_it(self):
        installer.apply_job(self.queued_wheel()["id"])
        original = installer.installation_paths()
        cache = Path(original[0]) / "luigi_web_extensions/example/__pycache__/manifest.cpython-314.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"synthetic-unpinned-bytecode")
        with patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No entry point loading")):
            self.assertEqual(installer.installation_paths(), original)
        self.assertFalse(cache.exists())

    def test_unremovable_unpinned_bytecode_fails_closed(self):
        installer.apply_job(self.queued_wheel()["id"])
        cache = Path(installer.installation_paths()[0]) / "luigi_web_extensions/example/__pycache__/manifest.cpython-314.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"synthetic-unpinned-bytecode")
        with patch.object(Path, "unlink", side_effect=PermissionError("synthetic permission failure")):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.installation_paths()

    def test_artifact_is_reinspected_before_publication_and_at_bootstrap(self):
        job = self.queued_wheel()
        stage = installer._stage

        def corrupt_after_staging(queued_job):
            record = stage(queued_job)
            wheel = repositories._storage_path(*record["site_path"].split("/")) / installer._WHEEL_FILE
            wheel.write_bytes(b"synthetic corrupted artifact")
            return record

        with patch.object(installer, "_stage", side_effect=corrupt_after_staging):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.apply_job(job["id"])
        self.assertEqual(installer.installation_paths(), [])
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/new.py": b"value = 2\n"})
        installer.apply_job(self.queued_wheel()["id"])
        wheel = Path(installer.installation_paths()[0]) / installer._WHEEL_FILE
        wheel.write_bytes(b"synthetic corrupted artifact")
        with self.assertRaises(repositories.ModuleInstallError):
            installer.installation_paths()

    def test_installed_tree_bounds_manifest_reads_despite_stale_stat_size(self):
        installer.apply_job(self.queued_wheel()["id"])
        root = Path(installer.installation_paths()[0])
        manifest = root / installer._MANIFEST_FILE
        manifest.write_bytes(manifest.read_bytes() + b" " * (installer.MAX_RELEASE_BYTES + 1))
        original_stat = Path.stat

        def stale_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == manifest:
                values = list(result)
                values[6] = 1
                return os.stat_result(values)
            return result

        with patch.object(Path, "stat", autospec=True, side_effect=stale_stat):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.installation_paths()

    def test_injected_database_path_cannot_escape_installer_root(self):
        installer.apply_job(self.queued_wheel()["id"])
        record = repositories.inventory_records()[0]
        record["site_path"] = "../../outside"
        with repositories._database(write=True) as connection:
            connection.execute("UPDATE releases SET record = ?", (json.dumps(record),))
        with self.assertRaises(repositories.ModuleInstallError):
            installer.installation_paths()

    def test_storage_link_is_rejected_before_writing(self):
        target = self.root / "module-installations.db"
        original = repositories._is_link
        with patch.object(repositories, "_is_link", side_effect=lambda path: path == target or original(path)):
            with self.assertRaises(repositories.ModuleInstallError):
                self.queued_wheel()

    def test_only_generic_failure_details_are_persisted(self):
        job = self.queued_wheel()
        with patch.object(installer, "_download", side_effect=RuntimeError("synthetic-sensitive-child-output")):
            with self.assertRaises(repositories.ModuleInstallError) as raised:
                installer.apply_job(job["id"])
        self.assertNotIn("synthetic-sensitive-child-output", str(raised.exception))
        self.assertNotIn("synthetic-sensitive-child-output", json.dumps(repositories.list_jobs()))

    def test_legacy_feature_namespace_import_in_a_fresh_process(self):
        self.select_module("tasks")
        self.content = build_wheel(package="luigi_web.modules.tasks", distribution="luigi-web-tasks", module_id="tasks")
        installer.apply_job(self.queued_wheel()["id"])
        site_path = installer.installation_paths()[0]
        environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}}
        environment.update({key: str(self.root) for key in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "SQLITE_TMPDIR", "LUIGI_WEB_DATA_DIR")})
        probe = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import luigi_web.modules
luigi_web.modules.__path__.insert(0, str(Path(sys.argv[1]) / 'luigi_web' / 'modules'))
from luigi_web.modules.tasks.manifest import module
assert module == 'synthetic installed module'
assert 'luigi_web.application' not in sys.modules
print('legacy namespace import passed')
"""
        project_root = str(Path(__file__).resolve().parents[1])
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, site_path, project_root], env=environment,
                                capture_output=True, text=True, timeout=30, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_activated_sibling_dependencies_and_reverse_compatibility(self):
        installer.apply_job(self.queued_wheel()["id"])
        original_source = dict(self.source)
        self.select_module("companion")
        self.content = build_wheel(package="luigi_web_extensions.companion", distribution="luigi-web-companion",
                                   module_id="companion", requirements=["luigi-web-example>=1,<2"])
        with patch.object(installer, "_installed_version", return_value=None):
            installer.apply_job(self.queued_wheel()["id"])
        self.source = original_source
        self.release_source = "example"
        self.release_distribution = "luigi_web_example"
        self.release_repository = original_source["repository"]
        self.release_version = "2.0"
        self.content = build_wheel(version="2.0")
        with self.assertRaises(repositories.ModuleInstallError):
            installer.apply_job(self.queued_wheel()["id"])
        versions = {record["module_id"]: record["version"] for record in installer.installed_modules()}
        self.assertEqual(versions, {"example": "1.0", "companion": "1.0"})

    def test_staged_extras_round_trip_and_are_rechecked_at_bootstrap(self):
        self.content = build_wheel(metadata_changes="Provides-Extra: fast\n",
                                   requirements=['synthetic-extra-runtime==1; extra == "fast"'])
        installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(installer.installed_modules()[0]["provides_extra"], ["fast"])
        self.select_module("companion")
        self.content = build_wheel(package="luigi_web_extensions.companion", distribution="luigi-web-companion",
                                   module_id="companion", requirements=["luigi-web-example[fast]>=1"])
        with patch.object(installer, "_installed_version", return_value=None):
            with self.assertRaisesRegex(repositories.ModuleInstallError, "synthetic-extra-runtime.*missing"):
                installer.apply_job(self.queued_wheel()["id"])
        with patch.object(installer, "_installed_version", side_effect={"synthetic-extra-runtime": "1.0"}.get):
            installer.apply_job(self.queued_wheel()["id"])
            self.assertEqual(len(installer.installation_paths()), 2)
        with patch.object(installer, "_installed_version", return_value=None):
            with self.assertRaisesRegex(repositories.ModuleInstallError, "synthetic-extra-runtime.*missing"):
                installer.installation_paths()

    def test_replacement_can_repair_obsolete_dependency_requirements(self):
        self.content = build_wheel(requirements=["synthetic-dependency==1.0"])
        with patch.object(installer, "_installed_version", return_value="1.0"):
            installer.apply_job(self.queued_wheel()["id"])
        self.release_version = "2.0"
        self.content = build_wheel(version="2.0", requirements=["synthetic-dependency==2.0"])
        with patch.object(installer, "_installed_version", return_value="2.0"):
            installer.apply_job(self.queued_wheel()["id"])
            self.assertEqual(installer.installed_modules()[0]["version"], "2.0")

    def test_corrupted_rollback_is_rejected_without_network(self):
        first = self.queued_wheel()
        installer.apply_job(first["id"])
        first_path = Path(installer.installation_paths()[0])
        self.content = build_wheel(extra_files={"luigi_web_extensions/example/new.py": b"value = 2\n"})
        installer.apply_job(self.queued_wheel()["id"])
        current = installer.installation_paths()
        (first_path / "luigi_web_extensions/example/manifest.py").write_bytes(b"altered")
        rollback = repositories.queue_rollback("example", first["sha256"], trust_confirmed=True)
        self.requests.clear()
        with self.assertRaises(repositories.ModuleInstallError):
            installer.apply_job(rollback["id"])
        self.assertEqual(installer.installation_paths(), current)
        self.assertEqual(self.requests, [])


class WheelSecurityTests(WheelFixture):
    def reject(self, content):
        self.content = content
        job = self.queued_wheel()
        with self.assertRaises(repositories.ModuleInstallError):
            installer.apply_job(job["id"])
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")
        self.assertEqual(repositories.inventory_records(), [])

    def test_unsafe_zip_paths_and_namespace_escapes(self):
        paths = ("../escape.py", "/absolute.py", "C:/drive.py", "luigi_web_extensions/example//double.py",
                 "luigi_web_extensions/example/back\\slash.py", "luigi_web_extensions/example/CON.py",
                 "luigi_web_extensions/example/trailing./file.py", "luigi_web/auth.py", "luigi_web/core/cli.py",
                 "luigi_web_extensions/__init__.py", "other_package/__init__.py", "luigi_web_extensions/example/startup.pth",
                 "luigi_web_extensions/example/native.pyd", "luigi_web_example-1.0.data/scripts/run")
        for path in paths:
            with self.subTest(path=path):
                self.reject(build_wheel(extra_files={path: b"synthetic"}))

    def test_harmless_license_readme_and_top_level_metadata_are_allowed(self):
        self.content = build_wheel(extra_files={
            "luigi_web_example-1.0.dist-info/licenses/LICENSE.txt": b"Synthetic license fixture\n",
            "luigi_web_example-1.0.dist-info/top_level.txt": b"luigi_web_extensions\n",
            "luigi_web_extensions/example/README.md": b"Synthetic packaged documentation\n",
        })
        installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(len(installer.installation_paths()), 1)

    def test_root_documentation_and_shared_namespace_initializers_remain_forbidden(self):
        for filename in ("README.md", "LICENSE.txt", "luigi_web/__init__.py", "luigi_web/modules/__init__.py"):
            with self.subTest(filename=filename):
                self.reject(build_wheel(extra_files={filename: b"synthetic"}))

    def test_duplicate_and_casefold_duplicate_paths(self):
        path = "luigi_web_extensions/example/manifest.py"
        for duplicate in (path, "luigi_web_extensions/example/MANIFEST.py"):
            with self.subTest(path=duplicate):
                self.reject(rewrite_wheel(build_wheel(), additional=[(duplicate, b"synthetic")]))

    def test_zip_symlinks_and_nul_paths(self):
        link = zipfile.ZipInfo("luigi_web_extensions/example/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.reject(rewrite_wheel(build_wheel(), additional=[(link, b"../../../outside")]))
        with self.assertRaises(repositories.ModuleInstallError):
            installer._safe_member(zipfile.ZipInfo("luigi_web_extensions/example/null\0.py"))
        with self.assertRaises(repositories.ModuleInstallError):
            installer._safe_member(zipfile.ZipInfo("luigi_web_extensions/example/directory//"))

    def test_encrypted_and_unsupported_compression_entries(self):
        for attribute, value in (("flag_bits", 1), ("compress_type", zipfile.ZIP_BZIP2)):
            info = zipfile.ZipInfo("luigi_web_extensions/example/extra.py")
            setattr(info, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(repositories.ModuleInstallError):
                installer._safe_member(info)

    def test_archive_resource_limits(self):
        with patch.object(installer, "MAX_FILES", 2):
            self.reject(build_wheel())
        with patch.object(installer, "MAX_EXPANDED_BYTES", 10):
            self.reject(build_wheel())
        self.reject(build_wheel(extra_files={"luigi_web_extensions/example/bomb.txt": b"a" * (1024 * 1024)}))

    def test_record_missing_mismatched_or_unrecorded_content(self):
        record = "luigi_web_example-1.0.dist-info/RECORD"
        self.reject(rewrite_wheel(build_wheel(), omitted=[record]))
        self.reject(rewrite_wheel(build_wheel(), changes={"luigi_web_extensions/example/manifest.py": b"altered"}))
        self.reject(rewrite_wheel(build_wheel(), additional=[("luigi_web_extensions/example/unrecorded.py", b"value = 1")]))
        self.reject(rewrite_wheel(build_wheel(), changes={record: b"bad,row\n"}))

    def test_only_exact_pure_wheel_metadata_is_accepted(self):
        for metadata_bytes in (b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n",
                               b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp311-none-any\n",
                               b"Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"):
            with self.subTest(metadata=metadata_bytes):
                self.reject(build_wheel(extra_files={"luigi_web_example-1.0.dist-info/WHEEL": metadata_bytes}))

    def test_metadata_identity_python_and_entry_point_are_bound(self):
        for metadata_bytes in (b"Name: different\nVersion: 1.0\nRequires-Python: >=3.11\n",
                               b"Name: luigi-web-example\nVersion: 2.0\nRequires-Python: >=3.11\n",
                               b"Name: luigi-web-example\nVersion: 1.0\nRequires-Python: >=999\n"):
            self.reject(build_wheel(extra_files={"luigi_web_example-1.0.dist-info/METADATA": metadata_bytes}))
        for entry_point in ("[luigi_web.modules]\nother = luigi_web_extensions.example.manifest:module\n",
                            "[luigi_web.modules]\nexample = luigi_web.core.cli:main\n",
                            "[luigi_web.modules]\nexample = luigi_web_extensions.example.manifest:module\n[console_scripts]\nrun = package:main\n"):
            self.reject(build_wheel(entry_point=entry_point))

    def test_url_and_missing_extra_dependencies_are_not_installed(self):
        for requirement in ("dependency @ https://example.invalid/dependency.whl", "dependency[extra]>=1"):
            self.reject(build_wheel(requirements=[requirement]))

    def test_installed_dependencies_and_markers_are_checked(self):
        self.content = build_wheel(requirements=["luigi-web>=0.2,<0.3", 'absent-dependency; python_version < "2"'])
        with patch.object(installer, "_installed_version", return_value="0.2.0"):
            installer.apply_job(self.queued_wheel()["id"])
        with patch.object(installer, "_installed_version", return_value="0.1.0"):
            with self.assertRaises(repositories.ModuleInstallError):
                installer.installation_paths()


class DependencyResolutionTests(InstallerFixture):
    def setUp(self):
        super().setUp()
        self.versions: dict[str, str | None] = {"adapter": "2.3", "binary-runtime": "2.3"}
        self.distributions = {
            "adapter": SimpleNamespace(metadata=installer._headers(b"Provides-Extra: binary\n"),
                                       requires=['binary-runtime==2.3; extra == "binary"'])}
        self.start_patch(patch.object(installer, "_installed_version", side_effect=self.versions.get))
        self.distribution_lookup = self.start_patch(
            patch.object(installer.metadata, "distribution", side_effect=self.distributions.__getitem__))

    def check(self, *requirements, records=()):
        installer._check_dependencies([
            {"distribution": "luigi-web-example", "version": "1.0", "requires_dist": list(requirements)}, *records])

    def test_missing_and_incompatible_extra_children_are_named(self):
        for version, message in ((None, "missing"), ("1.0", "incompatible"), ("bad-version", "invalid version")):
            with self.subTest(version=version):
                self.versions["binary-runtime"] = version
                with self.assertRaisesRegex(repositories.ModuleInstallError, f"binary-runtime.*{message}"):
                    self.check("adapter[binary]>=2")

    def test_base_version_is_checked_before_extras(self):
        with self.assertRaisesRegex(repositories.ModuleInstallError, "adapter.*incompatible"):
            self.check("adapter[binary]>=3")
        self.distribution_lookup.assert_not_called()

    def test_unknown_extra_and_missing_extra_metadata_fail_closed(self):
        with self.assertRaisesRegex(repositories.ModuleInstallError, "does not declare"):
            self.check("adapter[unknown]")
        with patch.object(installer.metadata, "distribution", side_effect=installer.metadata.PackageNotFoundError):
            with self.assertRaisesRegex(repositories.ModuleInstallError, "no installed extras metadata"):
                self.check("adapter[binary]")

    def test_empty_declared_extra_and_normalized_names_are_supported(self):
        self.distributions["adapter"].metadata = installer._headers(b"Provides-Extra: Empty_Extra\n")
        self.distributions["adapter"].requires = []
        self.check("ADAPTER[empty-extra]==2.3")

    def test_selected_extra_uses_base_and_environment_marker_requirements(self):
        self.distributions["adapter"].requires.extend([
            'absent-runtime; python_version < "2" and extra == "binary"',
            'absent-dev-runtime; extra == "dev"',
            'base-runtime>=1; extra != "binary"',
        ])
        with self.assertRaisesRegex(repositories.ModuleInstallError, "base-runtime.*missing"):
            self.check("adapter[binary]")
        self.versions["base-runtime"] = "1.0"
        self.check("adapter[binary]")

    def test_nested_extras_and_cycles_are_checked_without_recursion(self):
        self.distributions["adapter"].requires = ['binary-runtime[fast]==2.3; extra == "binary"']
        self.distributions["binary-runtime"] = SimpleNamespace(
            metadata=installer._headers(b"Provides-Extra: fast\n"),
            requires=['adapter[binary]>=2; extra == "fast"'])
        self.check("adapter[binary]")
        self.distributions["binary-runtime"].requires = ['adapter[binary]>=3; extra == "fast"']
        with self.assertRaisesRegex(repositories.ModuleInstallError, "adapter.*incompatible"):
            self.check("adapter[binary]")

    def test_multiple_extras_all_receive_their_marker_context(self):
        self.distributions["adapter"].metadata = installer._headers(b"Provides-Extra: binary\nProvides-Extra: fast\n")
        self.distributions["adapter"].requires.append('fast-runtime>=1; extra == "fast"')
        with self.assertRaisesRegex(repositories.ModuleInstallError, "fast-runtime.*missing"):
            self.check("adapter[binary,fast]")
        self.versions["fast-runtime"] = "1.0"
        self.check("adapter[binary]", "adapter[fast]")

    def test_staged_extra_metadata_overrides_installed_distribution(self):
        record = {"distribution": "Adapter", "version": "4.0", "provides_extra": ["binary"],
                  "requires_dist": ['binary-runtime==2.3; extra == "binary"']}
        self.check("adapter[binary]==4.0", records=[record])
        self.distribution_lookup.assert_not_called()
        with self.assertRaisesRegex(repositories.ModuleInstallError, "adapter.*incompatible"):
            self.check("adapter[binary]==2.3", records=[record])

    def test_plain_installed_host_and_sdk_do_not_expand_unrequested_extras(self):
        self.versions.update({"luigi-web": "0.2.0", "github-copilot-sdk": "1.0.11"})
        self.check("luigi-web>=0.2,<0.3", "github-copilot-sdk==1.0.11")
        self.distribution_lookup.assert_not_called()

    def test_direct_and_nested_url_requirements_are_rejected_even_when_inactive(self):
        for requirement in ('runtime @ https://example.invalid/runtime.whl',
                            'runtime @ file:///synthetic/runtime.whl',
                            'runtime @ git+https://example.invalid/repo',
                            'runtime @ https://example.invalid/runtime.whl ; extra == "unused"'):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(repositories.ModuleInstallError, "URL dependencies"):
                    self.check(requirement)
                self.distributions["adapter"].requires = [requirement]
                with self.assertRaisesRegex(repositories.ModuleInstallError, "URL dependencies"):
                    self.check("adapter[binary]")

    def test_invalid_requirements_and_unsupported_markers_are_public_errors(self):
        for requirement in ('runtime>>2', 'runtime; made_up_environment == "x"',
                            'runtime; os_name ~= "unsupported"', 'runtime; "secret" in dependency_groups'):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(repositories.ModuleInstallError, "invalid or unsupported|unsupported marker") as raised:
                    self.check(requirement)
                self.assertNotIn("secret", str(raised.exception))
                self.distributions["adapter"].requires = [requirement]
                with self.assertRaises(repositories.ModuleInstallError):
                    self.check("adapter[binary]")

    def test_invalid_extra_declarations_are_rejected(self):
        self.distributions["adapter"].metadata = installer._headers(b"Provides-Extra: ../binary\n")
        with self.assertRaisesRegex(repositories.ModuleInstallError, "invalid extra"):
            self.check("adapter[binary]")

    def test_dependency_graph_and_metadata_are_bounded(self):
        with patch.object(installer, "MAX_DEPENDENCIES", 1):
            with self.assertRaisesRegex(repositories.ModuleInstallError, "safety limit"):
                self.check("adapter[binary]")
            with self.assertRaisesRegex(repositories.ModuleInstallError, "safety limit"):
                self.check("adapter", "binary-runtime")

    def test_public_install_error_names_missing_dependency_without_persisting_details(self):
        self.register()
        content = build_wheel(requirements=["adapter[binary]"])
        self.versions.pop("binary-runtime")
        job = self.queue(sha256=hashlib.sha256(content).hexdigest())
        with patch.object(installer, "_download", side_effect=lambda job, path: path.write_bytes(content)):
            with self.assertRaisesRegex(repositories.ModuleInstallError, "binary-runtime.*missing"):
                installer.apply_job(job["id"])
        self.assertNotIn("binary-runtime", json.dumps(repositories.list_jobs()))


class SourceWheelCompatibilityTests(WheelFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            if installer.Version(installer.metadata.version("setuptools")) < installer.Version("68"):
                raise unittest.SkipTest("Source wheel checks require setuptools>=68")
            installer.metadata.version("wheel")
        except installer.metadata.PackageNotFoundError:
            raise unittest.SkipTest("Source wheel checks require installed setuptools and wheel") from None
        temporary = tempfile.TemporaryDirectory(prefix="installer-source-wheels-")
        cls.addClassCleanup(temporary.cleanup)
        cls.build_root = Path(temporary.name)
        cls.projects = {}
        root = Path(__file__).resolve().parents[1]
        allowed = {".py", ".html", ".css", ".js", ".svg", ".woff", ".woff2", ".ttf", ".ico",
                   ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md", ".json", ".map"}
        for module_id in ("host", *FEATURES):
            original = root if module_id == "host" else root / "module-repos" / module_id
            project = cls.build_root / "projects" / module_id
            project.mkdir(parents=True)
            configuration = tomllib.loads((original / "pyproject.toml").read_text(encoding="utf-8"))
            if configuration["build-system"] != {"requires": ["setuptools>=68", "wheel"], "build-backend": "setuptools.build_meta"}:
                raise AssertionError("Only the existing setuptools build backend is permitted")
            cls.projects[module_id] = configuration["project"]
            for filename in ("pyproject.toml", "README.md", "LICENSE", "LICENSE.txt", "LICENSE.md"):
                source = original / filename
                if source.is_file():
                    if source.is_symlink():
                        raise AssertionError("Build inputs must not be symlinks")
                    shutil.copyfile(source, project / filename)
            source_root = original if module_id == "host" else original / "src"
            destination_root = project if module_id == "host" else project / "src"
            for source in (source_root / "luigi_web").rglob("*"):
                relative = source.relative_to(source_root)
                if source.is_symlink():
                    raise AssertionError("Build inputs must not be symlinks")
                if (not source.is_file() or "__pycache__" in relative.parts
                        or any(part.startswith((".", "LOCAL_")) for part in relative.parts)
                        or source.suffix.lower() not in allowed):
                    continue
                destination = destination_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}}
        environment.update({key: str(cls.build_root) for key in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "SQLITE_TMPDIR", "HOME", "USERPROFILE")})
        result = subprocess.run([sys.executable, "-I", "-B", "-c", BUILD_LOCAL_WHEELS, str(cls.build_root)],
                                cwd=cls.build_root, env=environment, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError("Offline wheel build failed:\n" + result.stderr[-4000:] + result.stdout[-4000:])
        cls.wheels = {}
        cls.records = []
        for module_id in ("host", *FEATURES):
            wheels = list((cls.build_root / "wheels" / module_id).glob("*.whl"))
            if len(wheels) != 1:
                raise AssertionError("Expected exactly one freshly built wheel per project")
            cls.wheels[module_id] = wheels[0]
            if module_id != "host":
                source = repositories._source({"id": module_id, "repository": f"https://github.com/example-owner/{module_id}-module"})
                digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
                record, _ = installer._validate_wheel(wheels[0], source, wheels[0].name, digest)
                cls.records.append(record)

    def test_all_eleven_feature_wheels_match_source_contents_and_requirements(self):
        self.assertEqual(len(self.wheels), 12)
        for module_id in FEATURES:
            with self.subTest(module=module_id):
                wheel = self.wheels[module_id]
                source = repositories._source({"id": module_id, "repository": f"https://github.com/example-owner/{module_id}-module"})
                info, contents = installer._validate_wheel(wheel, source, wheel.name, hashlib.sha256(wheel.read_bytes()).hexdigest())
                project = self.projects[module_id]
                self.assertEqual(info["distribution"], project["name"])
                self.assertEqual(info["version"], project["version"])
                self.assertEqual(info["requires_python"], project["requires-python"])
                self.assertEqual(set(installer._requirements(info["requires_dist"])),
                                 set(installer._requirements(project["dependencies"])))
                source_root = self.build_root / "projects" / module_id / "src"
                package_path = f"luigi_web/modules/{module_id}/"
                expected = {path.relative_to(source_root).as_posix(): path.read_bytes()
                            for path in (source_root / package_path).rglob("*") if path.is_file()}
                actual = {name: data for name, data in contents.items() if name.startswith(package_path)}
                self.assertEqual(actual, expected)
                self.assertNotIn("luigi_web/__init__.py", contents)
                self.assertNotIn("luigi_web/modules/__init__.py", contents)
                top_level = next(data for name, data in contents.items() if name.endswith(".dist-info/top_level.txt"))
                self.assertEqual(top_level.strip(), b"luigi_web")

    def test_host_wheel_metadata_is_02_and_host_replacement_remains_forbidden(self):
        project = self.projects["host"]
        with zipfile.ZipFile(self.wheels["host"]) as archive:
            message = installer._headers(archive.read("luigi_web-0.2.0.dist-info/METADATA"))
            self.assertEqual(str(message["Name"]), "luigi-web")
            self.assertEqual(str(message["Version"]), "0.2.0")
            self.assertEqual(set(installer._requirements(message.get_all("Requires-Dist", []))),
                             set(installer._requirements(project["dependencies"])))
            self.assertIn(installer.Requirement("uvicorn[standard]==0.30.6"), installer._requirements(message.get_all("Requires-Dist", [])))
            self.assertIn("luigi_web/modules/__init__.py", archive.namelist())
            for module_id in FEATURES:
                self.assertFalse(any(name.startswith(f"luigi_web/modules/{module_id}/") for name in archive.namelist()))
        with self.assertRaises(repositories.ModuleInstallError):
            repositories._source(dict(self.source, distribution="luigi-web"))

    def test_real_feature_metadata_uses_installed_dependencies_and_source_host_fallback(self):
        with patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No entry point loading")):
            installer._check_dependencies(self.records)
        version = installer._installed_version("luigi-web")
        assert version is not None
        self.assertTrue(installer.SpecifierSet(">=0.2,<0.3").contains(version))

    def test_all_eleven_actual_feature_wheels_activate_with_mock_release_transport(self):
        with patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No entry point loading")):
            for module_id in FEATURES:
                with self.subTest(module=module_id):
                    self.select_module(module_id)
                    self.release_version = self.projects[module_id]["version"]
                    self.content = self.wheels[module_id].read_bytes()
                    result = installer.apply_job(self.queued_wheel()["id"])
                    self.assertEqual(result["state"], "installed")
            self.assertEqual(len(installer.installation_paths()), 11)
        self.assertEqual(len(self.requests), 22)


class DownloadSecurityTests(WheelFixture):
    def failing_download(self):
        job = self.queued_wheel()
        with self.assertRaises(repositories.ModuleInstallError):
            installer.apply_job(job["id"])
        self.assertEqual(repositories.get_job(job["id"])["state"], "failed")

    def test_unsafe_redirects_are_never_followed(self):
        for location in ("http://release-assets.githubusercontent.com/file", "https://example.invalid/file",
                         "https://github.com/other-owner/repository/file", "https://api.github.com/other/path",
                         "https://user:synthetic@objects.githubusercontent.com/file", "https://objects.githubusercontent.com:8443/file",
                         "https://objects.githubusercontent.com.evil.test/file", "//objects.githubusercontent.com/file"):
            original = self.respond

            def respond(request):
                if request.url.host == "api.github.com":
                    return original(request)
                return httpx.Response(302, headers={"Location": location})

            with self.subTest(location=location), patch.object(self, "respond", side_effect=respond):
                self.failing_download()

    def test_permitted_signed_redirect_has_no_authentication(self):
        original = self.respond
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.host == "github.com":
                return httpx.Response(302, headers={"Location": "https://release-assets.githubusercontent.com/example/file?signature=synthetic",
                                                   "Set-Cookie": "session=synthetic; Domain=.githubusercontent.com"})
            return original(request)

        with patch.object(self, "respond", side_effect=respond):
            installer.apply_job(self.queued_wheel()["id"])
        self.assertEqual(len(requests), 3)
        self.assertTrue(all("authorization" not in request.headers and "cookie" not in request.headers for request in requests))

    def test_redirect_loops_are_bounded(self):
        original = self.respond
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.host == "api.github.com":
                return original(request)
            return httpx.Response(302, headers={"Location": "https://objects.githubusercontent.com/loop"})

        with patch.object(self, "respond", side_effect=respond):
            self.failing_download()
        self.assertEqual(len(requests), 5)

    def test_release_and_asset_responses_are_bounded(self):
        with patch.object(installer, "MAX_RELEASE_BYTES", 20):
            self.failing_download()
        with patch.object(installer, "MAX_WHEEL_BYTES", 20):
            self.failing_download()

    def test_metadata_redirect_and_foreign_asset_url_are_rejected(self):
        with patch.object(self, "respond", return_value=httpx.Response(302, headers={"Location": "https://api.github.com/other"})):
            self.failing_download()
        original = self.respond

        def respond(request):
            response = original(request)
            release = response.json()
            release["assets"][0]["browser_download_url"] = "https://github.com/example-owner/other/releases/download/v1.0/file.whl"
            return httpx.Response(200, json=release)

        with patch.object(self, "respond", side_effect=respond):
            self.failing_download()

    def test_http_request_logs_do_not_contain_release_or_signed_urls(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("httpx")
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, previous_level)
        self.test_permitted_signed_redirect_has_no_authentication()
        self.assertEqual(output.getvalue(), "")

    def test_chunked_download_is_bounded_without_content_length(self):
        original = self.respond

        def respond(request):
            response = original(request)
            response.headers.pop("content-length", None)
            return response

        with patch.object(self, "respond", side_effect=respond), patch.object(installer, "MAX_WHEEL_BYTES", 10):
            self.failing_download()

    def test_public_release_identity_draft_and_asset_uniqueness(self):
        original = self.respond
        for change in ("wrong-tag", "draft", "duplicate"):
            def respond(request):
                response = original(request)
                release = response.json()
                if change == "wrong-tag":
                    release["tag_name"] = "other-tag"
                elif change == "draft":
                    release["draft"] = True
                else:
                    release["assets"] *= 2
                return httpx.Response(200, json=release)

            with self.subTest(change=change), patch.object(self, "respond", side_effect=respond):
                self.failing_download()


class PolicyAndCliTests(InstallerFixture):
    def test_source_host_version_is_only_a_missing_metadata_fallback(self):
        (self.root / "pyproject.toml").write_text('[project]\nname = "luigi-web"\nversion = "0.2.0"\n', encoding="utf-8")
        with patch.object(installer, "PROJECT_ROOT", self.root):
            with patch.object(installer.metadata, "version", side_effect=installer.metadata.PackageNotFoundError):
                self.assertEqual(installer._installed_version("luigi-web"), "0.2.0")
                self.assertIsNone(installer._installed_version("synthetic-uninstalled-package"))
            with patch.object(installer.metadata, "version", return_value="0.1.0"):
                self.assertEqual(installer._installed_version("luigi-web"), "0.1.0")

    def test_missing_host_metadata_and_project_file_do_not_invent_a_version(self):
        with patch.object(installer, "PROJECT_ROOT", self.root), \
                patch.object(installer.metadata, "version", side_effect=installer.metadata.PackageNotFoundError):
            self.assertIsNone(installer._installed_version("luigi-web"))

    def test_posix_policy_must_be_root_owned_and_not_group_writable(self):
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps({"version": 1, "sources": [self.source]}), encoding="utf-8")
        for owner, mode in ((1, 0o644), (0, 0o664), (0, 0o646)):
            status = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner)
            protected_os = SimpleNamespace(name="posix", environ=os.environ, fstat=lambda descriptor: status)
            with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORIES_FILE": str(policy_path)}), \
                    patch.object(repositories, "os", protected_os), self.assertRaises(repositories.ModuleInstallError):
                repositories._policy()

    def test_sources_cannot_claim_other_distributions_or_overlapping_namespaces(self):
        with self.assertRaises(repositories.ModuleInstallError):
            repositories.register_repository(dict(self.source, distribution="httpx"), trust_confirmed=True)
        self.register()
        with self.assertRaises(repositories.ModuleInstallError):
            repositories.register_repository(dict(self.source, id="second-source", module_id="example"), trust_confirmed=True)

    def test_fixed_deployment_policy_allows_selection_but_not_registration(self):
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps({"version": 1, "sources": [self.source]}), encoding="utf-8")
        status = list(policy_path.stat())
        status[0] = stat.S_IFREG | 0o644
        status[4] = 0
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORIES_FILE": str(policy_path), "LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""}), \
                patch.object(repositories.os, "fstat", return_value=os.stat_result(status)):
            self.assertEqual(repositories.list_repositories()[0]["id"], "example")
            self.assertEqual(self.queue()["state"], "pending")
            with self.assertRaises(repositories.ModuleInstallError):
                self.register()

    def test_malformed_policy_and_wildcard_owners_fail_closed(self):
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": "*"}):
            with self.assertRaises(repositories.ModuleInstallError):
                repositories.list_repositories()
        policy_path = self.root / "invalid.json"
        policy_path.write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORIES_FILE": str(policy_path)}):
            with self.assertRaises(repositories.ModuleInstallError):
                repositories.list_repositories()

    def test_cli_handles_one_job_or_pending_without_loading_application(self):
        with patch.object(installer, "apply_pending", return_value=None) as pending, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(installer.main(["--pending"]), 0)
        pending.assert_called_once_with()
        self.assertIn("No pending", output.getvalue())
        with patch.object(installer, "apply_job", side_effect=repositories.ModuleInstallError("synthetic detail")), \
                patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(installer.main(["--job", "a" * 32]), 1)
        self.assertNotIn("synthetic detail", output.getvalue())


if __name__ == "__main__":
    unittest.main()