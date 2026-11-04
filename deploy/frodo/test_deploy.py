"""Run with: python3 -m unittest discover -s deploy/frodo -p 'test_*.py'."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import deploy

SHA = "a" * 40
VERSION = "0.1.24"
CANDIDATE = f"hackmamba/open-seo:git-{SHA}"


class DockerBoundary:
    """Only Docker is simulated. Backups, filesystem, SQLite and locks are real."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
        self.current = "sha256:old"
        self.running = True
        self.exists = True
        self.fail = None
        self.migrate = False
        self.bad_health = False
        self.mismatch_identity = False
        self.extra_port = False
        self.old_image = {"Id": "sha256:old", "Architecture": "amd64", "Os": "linux"}
        self.image = {"Id": "sha256:new", "Architecture": "amd64", "Os": "linux", "Config": {
            "WorkingDir": "/app", "Cmd": ["sh", "deploy/docker/docker-entrypoint.sh"],
            "Labels": {"org.opencontainers.image.revision": SHA,
                       "org.opencontainers.image.version": VERSION}}}

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[0] != deploy.DOCKER:
            raise AssertionError(args)
        command = args[1:]
        if command[0] == "info":
            return self.cfg["state_dir"]
        if command[0] == "ps":
            return "container-id\n" if self.running and self.exists else ""
        if command[0] == "inspect":
            if not self.exists:
                raise deploy.DeployError("No such container")
            ports = {"3001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3001"}]}
            if self.extra_port:
                ports["80/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "80"}]
            return json.dumps([{"Image": self.current, "State": {"Running": self.running}, "Config": {
                "Labels": {"com.docker.compose.project": "open-seo", "com.docker.compose.service": "open-seo"}},
                "Mounts": [{"Destination": "/app/.wrangler", "Name": "open-seo_open_seo_data",
                            "Source": self.cfg["volume_path"], "RW": True}],
                "HostConfig": {"PortBindings": ports}}])
        if command[:2] == ["image", "inspect"]:
            return json.dumps([self.image if command[2] == CANDIDATE else self.old_image])
        if command[0] == "run":
            return json.dumps({"DB": "changed" if self.mismatch_identity and "sha256:new" in command else "original"})
        if command[0] == "load" or command[0] == "tag":
            return ""
        if command[0] == "compose":
            if "stop" in command:
                self.running = False
                if self.fail == "stop":
                    self.fail = None
                    raise deploy.DeployError("injected stop failure after stop")
                return ""
            if "up" in command:
                self.exists = True
                env = (Path(self.cfg["deployment_dir"]) / ".env").read_text()
                self.current = "sha256:new" if CANDIDATE in env else "sha256:old"
                self.running = True
                if self.current == "sha256:new":
                    if not (Path(self.cfg["volume_path"]) / ".openseo-deploy-pause").exists():
                        raise AssertionError("Candidate scheduler must remain paused until acceptance")
                    if not Path(self.cfg["maintenance_file"]).exists():
                        raise AssertionError("Candidate must start behind maintenance")
                    if self.migrate:
                        path = Path(self.cfg["volume_path"])
                        with contextlib.closing(sqlite3.connect(path / "db.sqlite")) as db, db:
                            db.execute("ALTER TABLE projects ADD COLUMN migrated TEXT")
                            db.execute("UPDATE projects SET name='changed by migration'")
                        (path / "created-by-migration").write_text("must not survive rollback")
                    if self.fail == "start":
                        self.fail = None
                        raise deploy.DeployError("injected partial startup failure")
                    if self.fail == "missing-container":
                        self.fail = None
                        self.exists = self.running = False
                        raise deploy.DeployError("injected recreation failure left no container")
                return ""
        raise AssertionError(command)

    def health(self):
        version = VERSION if self.current == "sha256:new" else "0.1.9"
        return {"status": "ok", "version": version, "checks": {"database": {
            "status": "error" if self.bad_health and self.current == "sha256:new" else "ok"}}}


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.directory, self.state, self.volume = (root / name for name in ("app", "state", "volume"))
        for path in (self.directory, self.state, self.volume):
            path.mkdir()
        self.env = b"# preserve this\nSECRET=private-value\nOPEN_SEO_IMAGE=old:stable\nOTHER='a=b'\n"
        (self.directory / ".env").write_bytes(self.env)
        (self.directory / "compose.yaml").write_text("unchanged existing compose\n")
        (self.volume / "kv").mkdir()
        (self.volume / "kv" / "oauth").write_bytes(b"existing token state")
        with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
            db.executescript("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);"
                             "INSERT INTO projects VALUES ('p1','client one');"
                             "CREATE TABLE d1_migrations(id INTEGER); INSERT INTO d1_migrations VALUES (48);"
                             "CREATE TABLE audits(status TEXT); CREATE TABLE rank_check_runs(status TEXT);"
                             "CREATE TABLE ai_runs(status TEXT);")
        self.cfg = {"deployment_dir": str(self.directory), "state_dir": str(self.state),
                    "project": "open-seo", "service": "open-seo", "container": "open-seo-open-seo-1",
                    "volume": "open-seo_open_seo_data", "volume_path": str(self.volume),
                    "database_relative_path": "db.sqlite", "health_url": "http://127.0.0.1:3001/api/health",
                    "port": 3001, "max_archive_bytes": 1024**2, "max_unpacked_bytes": 1024**2,
                    "maintenance_file": str(root / "maintenance"), "maintenance_probe_url": "https://frodo.hackmamba.io/",
                    "disk_reserve_bytes": 1, "health_timeout_seconds": 0.01,
                    "stop_timeout_seconds": 1, "upload_timeout_seconds": 1}
        self.boundary = DockerBoundary(self.cfg)
        self.deployment = deploy.Deployment(self.cfg, self.boundary, self.boundary.health,
                                            maintenance_probe=lambda: Path(self.cfg["maintenance_file"]).exists())
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            info = tarfile.TarInfo("manifest.json")
            data = json.dumps([{"Config": "config.json", "RepoTags": [CANDIDATE], "Layers": []}]).encode()
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
            config = tarfile.TarInfo("config.json")
            config.size = 2
            bundle.addfile(config, io.BytesIO(b"{}"))
        self.archive = archive.getvalue()
        self.args = [SHA, VERSION, hashlib.sha256(self.archive).hexdigest(), str(len(self.archive))]

    def run_deploy(self, args=None, content=None):
        with contextlib.redirect_stdout(io.StringIO()):
            self.deployment.execute(args or self.args, io.BytesIO(self.archive if content is None else content))

    def was_stopped(self):
        return any("compose" in call and "stop" in call for call in self.boundary.calls)

    def test_success_preserves_env_data_and_retains_verifiable_backup(self):
        self.run_deploy()
        self.assertEqual(self.boundary.current, "sha256:new")
        self.assertEqual((self.directory / ".env").read_bytes(), self.env.replace(b"old:stable", CANDIDATE.encode()))
        receipt = json.loads((self.deployment.transaction / "receipt.json").read_text())
        self.assertEqual(receipt["counts"], {"projects": 1})
        self.assertEqual(receipt["status"], "deployed")
        self.assertEqual(receipt["previous"]["image"], "sha256:old")
        archive = self.deployment.transaction / "volume.tar.gz"
        self.assertEqual(deploy.digest(archive), (self.deployment.transaction / "volume.sha256").read_text().strip())
        with tarfile.open(archive) as bundle:
            self.assertEqual(bundle.extractfile("kv/oauth").read(), b"existing token state")
        self.assertFalse((self.deployment.transaction / "image.tar.gz").exists())
        self.assertFalse(Path(self.cfg["maintenance_file"]).exists())
        self.assertFalse((self.volume / ".openseo-deploy-pause").exists())

    def test_corrupt_upload_never_stops_existing_application(self):
        with self.assertRaisesRegex(deploy.DeployError, "checksum"):
            self.run_deploy(content=b"x" * len(self.archive))
        self.assertFalse(self.was_stopped())
        self.assertEqual((self.directory / ".env").read_bytes(), self.env)
        self.assertFalse(any("load" in call for call in self.boundary.calls))

    def test_declared_length_bounds_extra_and_truncated_uploads(self):
        for content in (self.archive[:-1], self.archive + b"extra"):
            with self.subTest(length=len(content)), self.assertRaises(deploy.DeployError):
                self.run_deploy(content=content)
        self.assertFalse(self.was_stopped())

    def test_invalid_gzip_rejected_even_with_correct_transport_checksum(self):
        data = b"not a Docker archive"
        args = [SHA, VERSION, hashlib.sha256(data).hexdigest(), str(len(data))]
        with self.assertRaisesRegex(deploy.DeployError, "Invalid gzip"):
            self.run_deploy(args, data)
        self.assertFalse(self.was_stopped())

    def test_additional_image_tags_are_rejected_before_daemon_load(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            info = tarfile.TarInfo("manifest.json")
            data = json.dumps([{"Config": "config.json", "RepoTags": [CANDIDATE, "unrelated-service:latest"], "Layers": []}]).encode()
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        data = output.getvalue()
        with self.assertRaisesRegex(deploy.DeployError, "exactly the requested"):
            self.run_deploy([SHA, VERSION, hashlib.sha256(data).hexdigest(), str(len(data))], data)
        self.assertFalse(any("load" in call for call in self.boundary.calls))
        self.assertFalse(self.was_stopped())

    def test_oci_ref_cannot_overwrite_an_unrelated_tag(self):
        output = io.BytesIO()
        members = {"manifest.json": [{"Config": "config.json", "RepoTags": [CANDIDATE], "Layers": []}],
                   "config.json": {},
                   "index.json": {"manifests": [{"annotations": {"io.containerd.image.name": "docker.io/unrelated:latest"}}]}}
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            for name, value in members.items():
                data = json.dumps(value).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
        data = output.getvalue()
        with self.assertRaisesRegex(deploy.DeployError, "unrelated OCI"):
            self.run_deploy([SHA, VERSION, hashlib.sha256(data).hexdigest(), str(len(data))], data)
        self.assertFalse(any("load" in call for call in self.boundary.calls))

    def test_active_jobs_rejected_without_downtime(self):
        for table, status in (("audits", "running"), ("rank_check_runs", "pending"), ("ai_runs", "queued")):
            with self.subTest(table=table):
                with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
                    db.execute(f"INSERT INTO {table} VALUES (?)", (status,))
                with self.assertRaisesRegex(deploy.DeployError, "Active work"):
                    self.run_deploy()
                with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
                    db.execute(f"DELETE FROM {table}")
        self.assertFalse(self.was_stopped())

    def test_bad_health_restores_old_image_and_exact_pre_migration_database(self):
        self.boundary.migrate = True
        self.boundary.bad_health = True
        with self.assertRaisesRegex(deploy.DeployError, "readiness"):
            self.run_deploy()
        self.assertEqual(self.boundary.current, "sha256:old")
        with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT * FROM projects").fetchall(), [("p1", "client one")])
            self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(projects)")], ["id", "name"])
        self.assertFalse((self.volume / "created-by-migration").exists())
        self.assertTrue((self.deployment.transaction / "failed-volume" / "created-by-migration").exists())
        self.assertEqual((self.volume / "kv" / "oauth").read_bytes(), b"existing token state")
        self.assertIn(b"SECRET=private-value\n", (self.directory / ".env").read_bytes())
        self.assertIn(b"OTHER='a=b'\n", (self.directory / ".env").read_bytes())
        self.assertFalse((self.deployment.transaction / "receipt.json").exists())
        self.assertFalse(Path(self.cfg["maintenance_file"]).exists())
        self.assertFalse((self.volume / ".openseo-deploy-pause").exists())

    def test_unverified_maintenance_never_stops_the_app(self):
        self.deployment.maintenance_probe = lambda: False
        with self.assertRaisesRegex(deploy.DeployError, "maintenance gate"):
            self.run_deploy()
        self.assertFalse(self.was_stopped())
        self.assertFalse(Path(self.cfg["maintenance_file"]).exists())

    def test_marker_directory_fsync_failure_still_releases_pre_stop_maintenance(self):
        original_sync = deploy.sync_directory
        injected = False

        def fail_visible_marker_once(path):
            nonlocal injected
            if not injected and Path(self.cfg["maintenance_file"]).exists():
                injected = True
                raise OSError("injected maintenance directory fsync failure")
            return original_sync(path)

        with mock.patch.object(deploy, "sync_directory", fail_visible_marker_once):
            with self.assertRaisesRegex(OSError, "maintenance directory fsync"):
                self.run_deploy()
        self.assertTrue(injected)
        self.assertTrue(self.boundary.running)
        self.assertFalse(self.was_stopped())
        self.assertFalse(Path(self.cfg["maintenance_file"]).exists())

    def test_rollback_failure_keeps_public_traffic_blocked(self):
        original_runner = self.deployment.runner
        self.boundary.fail = "start"

        def failed_rollback(args, timeout):
            if "compose" in args and "up" in args and self.boundary.fail is None:
                raise deploy.DeployError("old image startup failed")
            return original_runner(args, timeout)

        self.deployment.runner = failed_rollback
        with self.assertRaisesRegex(deploy.DeployError, "rollback failed"):
            self.run_deploy()
        self.assertTrue(Path(self.cfg["maintenance_file"]).exists())
        self.assertFalse((self.deployment.transaction / "receipt.json").exists())

    def test_activation_failure_after_acceptance_never_rolls_back_new_writes(self):
        self.boundary.migrate = True
        with mock.patch.object(self.deployment, "disable_maintenance", side_effect=PermissionError("marker removal failed")):
            with self.assertRaisesRegex(PermissionError, "marker removal"):
                self.run_deploy()
        self.assertEqual(self.boundary.current, "sha256:new")
        self.assertTrue((self.deployment.transaction / "receipt.json").exists())
        self.assertEqual(json.loads((self.deployment.transaction / "receipt.json").read_text())["status"], "accepted")
        self.assertTrue(Path(self.cfg["maintenance_file"]).exists())
        self.assertFalse((self.volume / ".openseo-deploy-pause").exists())
        self.assertFalse((self.deployment.transaction / "failed-volume").exists())
        with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT name FROM projects").fetchone()[0], "changed by migration")

    def test_partial_candidate_startup_failure_restores_database(self):
        self.boundary.migrate = True
        self.boundary.fail = "start"
        with self.assertRaisesRegex(deploy.DeployError, "partial startup"):
            self.run_deploy()
        self.assertEqual(self.boundary.current, "sha256:old")
        with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT name FROM projects").fetchone()[0], "client one")

    def test_failed_env_fsync_cannot_leave_a_tempfile_blocking_rollback(self):
        original_fsync = os.fsync
        injected = False

        def fail_candidate_env_once(fd):
            nonlocal injected
            if not injected and list(self.directory.glob(".env.*.tmp")):
                injected = True
                raise OSError("injected ENOSPC during candidate pin")
            return original_fsync(fd)

        with mock.patch.object(deploy.os, "fsync", fail_candidate_env_once):
            with self.assertRaisesRegex(OSError, "ENOSPC"):
                self.run_deploy()
        self.assertTrue(injected)
        self.assertEqual(self.boundary.current, "sha256:old")
        self.assertTrue(self.boundary.running)
        self.assertFalse(list(self.directory.glob(".env.*.tmp")))
        self.assertFalse(Path(self.cfg["maintenance_file"]).exists())

    def test_recreation_failure_with_missing_container_still_restores_old_app(self):
        self.boundary.migrate = True
        self.boundary.fail = "missing-container"
        with self.assertRaisesRegex(deploy.DeployError, "left no container"):
            self.run_deploy()
        self.assertTrue(self.boundary.exists)
        self.assertTrue(self.boundary.running)
        self.assertEqual(self.boundary.current, "sha256:old")
        with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT * FROM projects").fetchall(), [("p1", "client one")])

    def test_stop_command_failure_still_restarts_the_old_app(self):
        self.boundary.fail = "stop"
        with self.assertRaisesRegex(deploy.DeployError, "stop failure"):
            self.run_deploy()
        self.assertTrue(self.boundary.running)
        self.assertEqual(self.boundary.current, "sha256:old")
        self.assertEqual((self.volume / "kv" / "oauth").read_bytes(), b"existing token state")

    def test_unrestorable_backup_aborts_before_candidate_and_restarts_old_app(self):
        (self.volume / "outside-link").symlink_to("/etc/passwd")
        with self.assertRaises(tarfile.FilterError):
            self.run_deploy()
        self.assertEqual(self.boundary.current, "sha256:old")
        self.assertTrue(self.boundary.running)
        self.assertTrue((self.volume / "outside-link").is_symlink())
        self.assertFalse((self.deployment.transaction / "failed-volume").exists())

    def test_migration_record_loss_is_rolled_back_even_if_http_health_passes(self):
        original_runner = self.deployment.runner
        deleted = False

        def delete_project_on_start(args, timeout):
            nonlocal deleted
            result = original_runner(args, timeout)
            if not deleted and "compose" in args and "up" in args and self.boundary.current == "sha256:new":
                with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
                    db.execute("DELETE FROM projects")
                deleted = True
            return result

        self.deployment.runner = delete_project_on_start
        with self.assertRaisesRegex(deploy.DeployError, "records disappeared"):
            self.run_deploy()
        self.assertEqual(self.boundary.current, "sha256:old")
        with contextlib.closing(sqlite3.connect(self.volume / "db.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT * FROM projects").fetchall(), [("p1", "client one")])

    def test_wrong_architecture_or_storage_identity_rejected_before_stop(self):
        self.boundary.image["Architecture"] = "arm64"
        with self.assertRaisesRegex(deploy.DeployError, "architecture"):
            self.run_deploy()
        self.boundary.image["Architecture"] = "amd64"
        self.boundary.mismatch_identity = True
        with self.assertRaisesRegex(deploy.DeployError, "identities"):
            self.run_deploy()
        self.assertFalse(self.was_stopped())

    def test_public_port_binding_rejected_before_upload(self):
        self.boundary.extra_port = True
        with self.assertRaisesRegex(deploy.DeployError, "loopback"):
            self.run_deploy()
        self.assertFalse(self.was_stopped())

    def test_second_deployment_cannot_enter_transaction(self):
        with open(self.state / "deploy.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(deploy.DeployError, "host lock"):
                self.run_deploy()
        self.assertFalse(self.boundary.calls)

    def test_lost_stdout_preserves_durable_progress_and_clean_process_exit(self):
        code = '''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import deploy
cfg={key:sys.argv[2] for key in ('deployment_dir','state_dir','volume_path','maintenance_file')}
d=deploy.Deployment(cfg);d.transaction=Path(sys.argv[2])
read,write=os.pipe();os.close(read);os.dup2(write,1);os.close(write)
d.log('after SSH disconnect');d.log('transaction continued')
'''
        result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).parent), str(self.state)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.state / "deploy.log").read_text(), "after SSH disconnect\ntransaction continued\n")

    def test_disk_shortage_rejected_before_stop(self):
        with mock.patch.object(deploy.shutil, "disk_usage", return_value=shutil_usage(0)):
            with self.assertRaisesRegex(deploy.DeployError, "free space"):
                self.run_deploy()
        self.assertFalse(self.was_stopped())

    def test_cross_filesystem_quarantine_rejected_before_any_docker_commands(self):
        original_stat = Path.stat

        def stat_with_different_device(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == self.state:
                values = list(result)
                values[2] += 1  # st_dev
                return os.stat_result(values)
            return result

        with mock.patch.object(Path, "stat", stat_with_different_device):
            with self.assertRaisesRegex(deploy.DeployError, "share a filesystem"):
                self.run_deploy()
        self.assertFalse(self.boundary.calls)

    def test_same_device_but_cross_mount_rename_rejected_before_stop(self):
        original_rename = Path.rename

        def reject_cross_mount(path, destination):
            if path.parent == self.state and Path(destination).parent == self.volume:
                raise OSError(18, "Cross-device link")
            return original_rename(path, destination)

        with mock.patch.object(Path, "rename", reject_cross_mount):
            with self.assertRaisesRegex(deploy.DeployError, "support atomic rename"):
                self.run_deploy()
        self.assertFalse(self.boundary.calls)
        self.assertFalse(list(self.state.glob(".rename-check-*")))
        self.assertFalse(list(self.volume.glob(".frodo-deploy-rename-*")))

    def test_health_http200_with_wrong_version_is_rejected(self):
        self.boundary.health = lambda: {"status": "ok", "version": "wrong", "checks": {"database": {"status": "ok"}}}
        self.deployment.health = self.boundary.health
        with self.assertRaisesRegex(deploy.DeployError, "readiness"):
            self.deployment.wait(VERSION)

    def test_invalid_arguments_cannot_reach_docker(self):
        for args in ([SHA + ";id", *self.args[1:]], [SHA, "latest", *self.args[2:]], self.args + ["/tmp"]):
            with self.subTest(args=args), self.assertRaisesRegex(deploy.DeployError, "arguments"):
                self.run_deploy(args)
        self.assertFalse(self.boundary.calls)


def shutil_usage(free):
    from collections import namedtuple
    return namedtuple("usage", "total used free")(100, 100 - free, free)


class GateTests(unittest.TestCase):
    def test_shell_commands_and_protocol_suffixes_are_denied(self):
        gate = Path(__file__).with_name("ssh-gate.py")
        for command in ("id", f"deploy {SHA} {VERSION} {'b'*64} 12; id", f"deploy {SHA} {VERSION} {'b'*64} 12\n"):
            result = subprocess.run([sys.executable, str(gate)], env={"SSH_ORIGINAL_COMMAND": command}, capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_valid_command_executes_only_fixed_sudo_with_clean_environment(self):
        spec = importlib.util.spec_from_file_location("frodo_gate", Path(__file__).with_name("ssh-gate.py"))
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": f"deploy {SHA} {VERSION} {'b'*64} 12", "SECRET": "not-forwarded"}), mock.patch.object(gate.os, "execve") as execute:
            gate.main()
        program, args, env = execute.call_args.args
        self.assertEqual(program, "/usr/bin/sudo")
        self.assertEqual(args, ["sudo", "-n", "/usr/local/sbin/frodo-deploy", SHA, VERSION, "b" * 64, "12"])
        self.assertNotIn("SECRET", env)


if __name__ == "__main__":
    unittest.main()
