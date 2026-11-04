#!/usr/bin/env python3
"""A single-host, backup-first Docker rollout. Configuration is root-owned.

The SSH caller streams a gzip docker-save archive and supplies its digest and
length. No caller-controlled command, path, environment or image tag is used.
"""
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import ssl
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

CONFIG = Path("/etc/frodo-deploy.json")
DOCKER = "/usr/bin/docker"
ACTIVE = {"audits": ("running",), "rank_check_runs": ("pending", "running"),
          "ai_runs": ("queued", "running"), "local_rank_checks": ("queued", "running")}
PRESERVED = ("user", "organization", "member", "projects", "saved_keywords",
             "rank_tracking_configs", "rank_tracking_keywords", "reports")
IDENTITY_SCRIPT = r'''
const fs = require('fs');
// Extract only the two known identity arrays, not arbitrary JSONC execution.
const text = fs.readFileSync('/app/wrangler.jsonc', 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const result = {};
for (const [key, id] of [['d1_databases', 'database_id'], ['kv_namespaces', 'id']]) {
  const section = text.match(new RegExp('"' + key + '"\\s*:\\s*\\[([^\\]]*)\\]'));
  if (!section) throw new Error('Missing identity section');
  result[key] = {};
  for (const block of section[1].matchAll(/\{([^{}]*)\}/g)) {
    const binding = block[1].match(/"binding"\s*:\s*"([^"]+)"/);
    const value = block[1].match(new RegExp('"' + id + '"\\s*:\\s*"([^"\\n]+)"'));
    if (!binding || !value || result[key][binding[1]]) throw new Error('Invalid identity');
    result[key][binding[1]] = value[1];
  }
  if (!Object.keys(result[key]).length) throw new Error('Empty identity');
}
if (!fs.existsSync('/app/deploy/docker/docker-entrypoint.sh')) throw new Error('Missing legacy startup');
if (process.argv[1] === 'require-pause' && !fs.readFileSync('/app/scripts/selfhost-scheduler.mjs', 'utf8').includes('.openseo-deploy-pause')) throw new Error('Missing deployment scheduler pause');
console.log(JSON.stringify(result));
'''


class DeployError(RuntimeError):
    pass


def protected(path, owner=0, directory=False):
    path = Path(path)
    for item in [path, *path.parents]:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise DeployError(f"Untrusted ownership/permissions: {item}")
    if not directory and not path.is_file():
        raise DeployError(f"Not a regular file: {path}")
    if directory and not path.is_dir():
        raise DeployError(f"Not a directory: {path}")


def load_config(path=CONFIG):
    protected(path)
    cfg = json.loads(Path(path).read_text())
    required = {"deployment_dir", "state_dir", "project", "service", "container",
                "volume", "volume_path", "database_relative_path", "health_url", "port",
                "maintenance_file", "maintenance_probe_url"}
    allowed = required | {"max_archive_bytes", "max_unpacked_bytes", "disk_reserve_bytes",
                          "health_timeout_seconds", "stop_timeout_seconds", "upload_timeout_seconds"}
    if set(cfg) - allowed or required - set(cfg):
        raise DeployError("Invalid configuration keys")
    for name in ("deployment_dir", "state_dir", "volume_path"):
        if not Path(cfg[name]).is_absolute():
            raise DeployError("Configuration paths must be absolute")
        protected(cfg[name], directory=True)
    directories = [Path(cfg[name]) for name in ("deployment_dir", "state_dir", "volume_path")]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(directories) for b in directories[i + 1:]):
        raise DeployError("Deployment, state and volume directories must be separate")
    rel = Path(cfg["database_relative_path"])
    if rel.is_absolute() or ".." in rel.parts or not str(rel).endswith(".sqlite"):
        raise DeployError("Invalid SQLite path")
    for key in ("project", "service", "container", "volume"):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", cfg[key]):
            raise DeployError("Invalid Docker resource name")
    defaults = {"max_archive_bytes": 6 * 1024**3, "max_unpacked_bytes": 16 * 1024**3,
                "disk_reserve_bytes": 2 * 1024**3, "health_timeout_seconds": 900,
                "stop_timeout_seconds": 60, "upload_timeout_seconds": 900}
    for key, value in defaults.items():
        cfg.setdefault(key, value)
    for key in (*defaults, "port"):
        if type(cfg[key]) is not int or cfg[key] <= 0:
            raise DeployError(f"Invalid positive integer: {key}")
    if cfg["port"] > 65535 or cfg["health_url"] != f'http://127.0.0.1:{cfg["port"]}/api/health':
        raise DeployError("Health URL must use the configured loopback port")
    marker = Path(cfg["maintenance_file"])
    if not marker.is_absolute() or marker.name != "maintenance":
        raise DeployError("Invalid maintenance marker path")
    protected(marker.parent, directory=True)
    if cfg["maintenance_probe_url"] != "https://frodo.hackmamba.io/":
        raise DeployError("Maintenance probe must address Frodo's TLS virtual host")
    for filename in (".env", "compose.yaml"):
        protected(Path(cfg["deployment_dir"]) / filename)
    return cfg


def validate_args(args):
    patterns = (r"[0-9a-f]{40}", r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                r"[0-9a-f]{64}", r"[1-9][0-9]{0,10}")
    if len(args) != 4 or any(not re.fullmatch(p, a) for p, a in zip(patterns, args)):
        raise DeployError("Invalid deployment arguments")
    return args[0], args[1], args[2], int(args[3])


def atomic_bytes(path, content, mode=0o600):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False) as out:
            temp = Path(out.name)
            os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def check_atomic_rename(state, volume):
    # Identical st_dev is insufficient across distinct bind-mount roots.
    # Prove the exact rollback operation before touching the old service.
    source, destination = None, None
    try:
        with tempfile.NamedTemporaryFile(dir=state, prefix=".rename-check-", delete=False) as probe:
            source = Path(probe.name)
        destination = volume / (".frodo-deploy-rename-" + uuid.uuid4().hex)
        source.rename(destination)
        destination.rename(source)
    except OSError as error:
        raise DeployError("State and volume must support atomic rename for rollback") from error
    finally:
        for probe in (source, destination):
            if probe:
                probe.unlink(missing_ok=True)


class Deployment:
    def __init__(self, config, runner=None, health=None, maintenance_probe=None):
        self.c = config
        self.root = Path(config["state_dir"])
        self.volume = Path(config["volume_path"])
        self.directory = Path(config["deployment_dir"])
        self.runner = runner or self._run
        self.health = health or self._health
        self.maintenance_probe = maintenance_probe or self._maintenance_probe
        self.maintenance_file = Path(config["maintenance_file"])
        self.pause_file = self.volume / ".openseo-deploy-pause"
        self.maintenance_owned = False
        self.transaction = None

    def _run(self, args, timeout=120):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"})
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DeployError(f"Command failed or timed out: {args[0]}") from exc
        if result.returncode:
            # Compose errors can include interpolated secrets. Keep them out of logs.
            raise DeployError(f"Command failed ({result.returncode}): {args[0]}")
        return result.stdout

    def docker(self, *args, timeout=120):
        return self.runner([DOCKER, *args], timeout=timeout)

    def compose(self, *args):
        return self.docker("compose", "--project-directory", str(self.directory), "--env-file",
                           str(self.directory / ".env"), "-p", self.c["project"], "-f",
                           str(self.directory / "compose.yaml"), *args, timeout=180)

    def log(self, message):
        if self.transaction:
            with open(self.transaction / "deploy.log", "a") as log:
                log.write(message + "\n")
        try:
            print(message, flush=True)
        except BrokenPipeError:
            # Prevent Python's final stdout flush from failing again at exit.
            # Subsequent progress stays in deploy.log even with no SSH reader.
            try:
                with open(os.devnull, "w") as sink:
                    os.dup2(sink.fileno(), sys.stdout.fileno())
            except (OSError, ValueError, AttributeError):
                pass

    def space(self, required):
        # Docker storage and spool may live on different filesystems.
        docker_root = self.docker("info", "--format", "{{.DockerRootDir}}").strip()
        if not Path(docker_root).is_absolute():
            raise DeployError("Docker did not report an absolute storage directory")
        for path in {self.root, self.volume, Path(docker_root)}:
            if shutil.disk_usage(path).free < required + self.c["disk_reserve_bytes"]:
                raise DeployError(f"Insufficient free space: {path}")

    def inspect_container(self):
        value = json.loads(self.docker("inspect", self.c["container"]))[0]
        labels = value.get("Config", {}).get("Labels", {})
        if labels.get("com.docker.compose.project") != self.c["project"] or labels.get("com.docker.compose.service") != self.c["service"]:
            raise DeployError("Container belongs to another Compose service")
        mounts = [m for m in value["Mounts"] if m["Destination"] == "/app/.wrangler"]
        if len(mounts) != 1 or mounts[0].get("Name") != self.c["volume"] or mounts[0].get("Source") != str(self.volume) or not mounts[0].get("RW"):
            raise DeployError("Unexpected application data mount")
        ports = value.get("HostConfig", {}).get("PortBindings", {})
        expected = {f'{self.c["port"]}/tcp': [{"HostIp": "127.0.0.1", "HostPort": str(self.c["port"])}]}
        if ports != expected:
            raise DeployError("Application must expose only the configured loopback port")
        return value

    def ensure_volume_stopped(self):
        # A failed Compose recreation can leave no app container to inspect.
        # A successful daemon query proves absence of running volume writers;
        # daemon errors are failures, never interpreted as an empty result.
        writers = self.docker("ps", "--filter", f'volume={self.c["volume"]}',
                              "--filter", "status=running", "--format", "{{.ID}}")
        if writers.strip():
            raise DeployError("Application data volume still has a running container")

    def database(self, reject_active=True):
        db_path = self.volume / self.c["database_relative_path"]
        if not db_path.resolve().is_relative_to(self.volume.resolve()):
            raise DeployError("SQLite path escapes the application volume")
        # Opening a nonexistent database must never create an empty substitute.
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise DeployError("SQLite integrity check failed")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "projects" not in tables or "d1_migrations" not in tables:
                raise DeployError("Expected application database not found")
            if reject_active:
                for table, statuses in ACTIVE.items():
                    if table in tables and conn.execute(f'SELECT 1 FROM "{table}" WHERE status IN ({",".join("?" for _ in statuses)}) LIMIT 1', statuses).fetchone():
                        raise DeployError(f"Active work in {table}; retry after it finishes")
            return {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                    for table in PRESERVED if table in tables}
        finally:
            conn.close()

    def upload(self, stream, size, expected, candidate):
        if size > self.c["max_archive_bytes"]:
            raise DeployError("Image archive exceeds configured limit")
        self.space(size * 2)
        partial = self.transaction / "image.tar.gz.partial"
        end = time.monotonic() + self.c["upload_timeout_seconds"]
        received, checksum = 0, hashlib.sha256()
        with open(partial, "xb") as out:
            os.fchmod(out.fileno(), 0o600)
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise DeployError("Upload timed out")
                try:
                    fd = stream.fileno()
                except (AttributeError, OSError):
                    fd = None
                if fd is not None and not select.select([fd], [], [], remaining)[0]:
                    raise DeployError("Upload timed out")
                chunk = os.read(fd, min(1024 * 1024, size - received + 1)) if fd is not None else stream.read(min(1024 * 1024, size - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > size:
                    raise DeployError("Archive length exceeds declaration")
                out.write(chunk)
                checksum.update(chunk)
            out.flush()
            os.fsync(out.fileno())
        if received != size or checksum.hexdigest() != expected:
            raise DeployError("Archive size/checksum mismatch")
        archive = self.transaction / "image.tar.gz"
        partial.rename(archive)
        unpacked, metadata, names = 0, {}, set()
        try:
            with tarfile.open(archive, "r|gz") as bundle:
                for member in bundle:
                    name = member.name.removeprefix("./")
                    if name in names or len(names) >= 100000 or Path(name).is_absolute() or ".." in Path(name).parts or not (member.isfile() or member.isdir()):
                        raise DeployError("Unsupported Docker archive member")
                    names.add(name)
                    unpacked = member.offset_data + member.size
                    if unpacked > self.c["max_unpacked_bytes"] or member.size < 0:
                        raise DeployError("Unpacked image exceeds configured limit")
                    if name in ("manifest.json", "index.json", "repositories"):
                        if member.size > 1024 * 1024 or not member.isfile():
                            raise DeployError("Invalid Docker image metadata")
                        metadata[name] = json.loads(bundle.extractfile(member).read())
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise DeployError("Invalid gzip Docker archive") from exc
        manifest = metadata.get("manifest.json")
        if not isinstance(manifest, list) or len(manifest) != 1 or manifest[0].get("RepoTags") != [candidate]:
            raise DeployError("Archive must contain exactly the requested Frodo image tag")
        image = manifest[0]
        if not isinstance(image.get("Layers"), list) or image.get("Config") not in names or any(layer not in names for layer in image["Layers"]):
            raise DeployError("Docker archive is missing image content")
        # Recent Docker save emits both Docker and OCI metadata. Do not let
        # additional OCI refs or classic repositories overwrite unrelated tags.
        if "index.json" in metadata:
            index = metadata["index.json"]
            descriptors = index.get("manifests", [])
            if len(descriptors) != 1:
                raise DeployError("Archive contains multiple OCI image references")
            allowed_names = {candidate, "docker.io/" + candidate, candidate.split(":", 1)[1]}
            for annotations in (index.get("annotations", {}), descriptors[0].get("annotations", {})):
                for key in ("io.containerd.image.name", "org.opencontainers.image.ref.name"):
                    if key in annotations and annotations[key] not in allowed_names:
                        raise DeployError("Archive contains an unrelated OCI image tag")
        if "repositories" in metadata:
            repository, tag = candidate.rsplit(":", 1)
            repositories = metadata["repositories"]
            if set(repositories) != {repository} or set(repositories[repository]) != {tag}:
                raise DeployError("Archive contains unrelated repository tags")
        volume_bytes = sum(p.lstat().st_size for p in self.volume.rglob("*") if p.is_file())
        self.space(unpacked * 2 + volume_bytes * 3)
        return archive

    def pin(self, image):
        path = self.directory / ".env"
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        pattern = re.compile(rb"^\s*(?:export\s+)?OPEN_SEO_IMAGE\s*=")
        matches = [i for i, line in enumerate(lines) if pattern.match(line)]
        if len(matches) > 1:
            raise DeployError("Ambiguous OPEN_SEO_IMAGE assignments")
        value = f"OPEN_SEO_IMAGE={image}\n".encode()
        if matches:
            lines[matches[0]] = value
        else:
            if original and not original.endswith(b"\n"):
                lines.append(b"\n")
            lines.append(value)
        atomic_bytes(path, b"".join(lines), stat.S_IMODE(path.stat().st_mode))

    def identity(self, image, require_pause=False):
        return json.loads(self.docker("run", "--rm", "--network", "none", "--read-only",
                          "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt",
                          "no-new-privileges", "--memory", "128m", "--pids-limit", "32",
                          "--entrypoint", "node", image, "-e", IDENTITY_SCRIPT,
                          "require-pause" if require_pause else "identity-only"))

    def _health(self):
        # Disable proxy environment and redirects: readiness must be this host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(self.c["health_url"], timeout=10) as response:
            return json.loads(response.read(1024 * 1024))

    def _maintenance_probe(self):
        # Dial loopback, preserving Frodo's SNI/Host and normal CA validation.
        # Public DNS/Cloudflare must never determine this safety check.
        context = ssl.create_default_context()
        with socket.create_connection(("127.0.0.1", 443), timeout=10) as plain:
            with context.wrap_socket(plain, server_hostname="frodo.hackmamba.io") as connection:
                connection.sendall(b"GET / HTTP/1.1\r\nHost: frodo.hackmamba.io\r\nConnection: close\r\n\r\n")
                response = http.client.HTTPResponse(connection)
                response.begin()
                return (response.status == 503 and
                        response.getheader("X-Frodo-Deployment") == "maintenance" and
                        response.read(65536).strip() == b"Frodo deployment in progress")

    def enable_maintenance(self):
        if self.maintenance_file.exists():
            raise DeployError("Maintenance marker already exists; resolve previous deployment first")
        try:
            atomic_bytes(self.maintenance_file, self.transaction.name.encode(), mode=0o644)
        except BaseException:
            # Rename can succeed before the directory durability flush fails.
            # Keep ownership of that visible marker so pre-stop recovery frees it.
            self.maintenance_owned = (self.maintenance_file.exists() and
                                      self.maintenance_file.read_bytes() == self.transaction.name.encode())
            raise
        self.maintenance_owned = True
        if not self.maintenance_probe():
            raise DeployError("Caddy maintenance gate did not pass its TLS probe")
        self.log("Public writes blocked by verified Caddy maintenance gate")

    def disable_maintenance(self):
        if self.maintenance_file.read_bytes() != self.transaction.name.encode():
            raise DeployError("Maintenance marker ownership changed; refusing activation")
        self.maintenance_file.unlink()
        sync_directory(self.maintenance_file.parent)
        self.maintenance_owned = False

    def wait(self, version):
        deadline = time.monotonic() + self.c["health_timeout_seconds"]
        while True:
            try:
                result = self.health()
                if result.get("status") == "ok" and result.get("version") == version and result.get("checks", {}).get("database", {}).get("status") == "ok":
                    return
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise DeployError("Application readiness did not pass")
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def backup(self, old):
        for filename in (".env", "compose.yaml"):
            shutil.copy2(self.directory / filename, self.transaction / filename)
            os.chmod(self.transaction / filename, 0o600)
        archive = self.transaction / "volume.tar.gz"
        with tarfile.open(archive.with_suffix(".partial"), "w:gz", dereference=False) as bundle:
            for item in sorted(self.volume.iterdir()):
                bundle.add(item, arcname=item.name, recursive=True)
        with archive.with_suffix(".partial").open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        archive.with_suffix(".partial").rename(archive)
        sync_directory(self.transaction)
        checksum = digest(archive)
        atomic_bytes(self.transaction / "volume.sha256", (checksum + "\n").encode())
        atomic_bytes(self.transaction / "previous.json", json.dumps(old).encode())
        # Prove restoration is possible before letting the candidate migrate.
        # In particular, reject unsupported special files or escaping symlinks.
        check = self.transaction / "restore-check"
        check.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(check, filter="data")
        shutil.rmtree(check)

    def restore(self, rollback_image, version, restore_data):
        self.log("Candidate failed; restoring previous image and saved state")
        self.compose("stop", "-t", str(self.c["stop_timeout_seconds"]), self.c["service"])
        self.ensure_volume_stopped()
        if restore_data:
            archive = self.transaction / "volume.tar.gz"
            if digest(archive) != (self.transaction / "volume.sha256").read_text().strip():
                raise DeployError("Backup checksum mismatch; refusing restoration")
            failed = self.transaction / "failed-volume"
            failed.mkdir()
            # Move rather than overlay: migration-created files cannot survive rollback.
            for item in list(self.volume.iterdir()):
                item.rename(failed / item.name)
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(self.volume, filter="data")
            self.database(reject_active=False)
            for filename in (".env", "compose.yaml"):
                atomic_bytes(self.directory / filename, (self.transaction / filename).read_bytes())
        self.pin(rollback_image)
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", self.c["service"])
        container = self.inspect_container()
        if container["Image"] != json.loads(self.docker("image", "inspect", rollback_image))[0]["Id"]:
            raise DeployError("Rollback did not start the previous image")
        self.wait(version)
        self.log("Rollback healthy; failed candidate state retained for recovery")

    def execute(self, args, stream):
        sha, version, checksum, size = validate_args(args)
        # Rollback quarantines the failed data with atomic renames. Never
        # discover an EXDEV boundary after stopping or migrating the app.
        if self.root.stat().st_dev != self.volume.stat().st_dev:
            raise DeployError("State and volume directories must share a filesystem")
        lock = open(self.root / "deploy.lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise DeployError("Another deployment holds the host lock") from exc
        self.transaction = self.root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + sha[:12] + "-" + uuid.uuid4().hex[:8])
        self.transaction.mkdir(mode=0o700)
        stopped, backup_ready, accepted = False, False, False
        try:
            check_atomic_rename(self.root, self.volume)
            self.log(f"Preparing deployment {sha} version {version}")
            old = self.inspect_container()
            if not old.get("State", {}).get("Running"):
                raise DeployError("Existing app is not running; resolve before deployment")
            if self.maintenance_file.exists() or self.pause_file.exists():
                raise DeployError("A deployment maintenance/pause marker already exists; inspect recovery state")
            self.database()
            old_health = self.health()
            old_version = old_health.get("version")
            if not old_version or old_health.get("status") != "ok" or old_health.get("checks", {}).get("database", {}).get("status") != "ok":
                raise DeployError("Existing app is unhealthy")
            candidate = f"hackmamba/open-seo:git-{sha}"
            archive = self.upload(stream, size, checksum, candidate)
            self.docker("load", "--input", str(archive), timeout=600)
            image = json.loads(self.docker("image", "inspect", candidate))[0]
            previous_image = json.loads(self.docker("image", "inspect", old["Image"]))[0]
            labels = image.get("Config", {}).get("Labels", {})
            if image.get("Architecture") != "amd64" or image.get("Os") != "linux" or labels.get("org.opencontainers.image.revision") != sha or labels.get("org.opencontainers.image.version") != version:
                raise DeployError("Candidate image architecture/revision/version mismatch")
            if image.get("Config", {}).get("WorkingDir") != "/app" or image.get("Config", {}).get("Cmd") != ["sh", "deploy/docker/docker-entrypoint.sh"]:
                raise DeployError("Candidate is not the supported legacy Docker layout")
            if image.get("Config", {}).get("Entrypoint") != previous_image.get("Config", {}).get("Entrypoint"):
                raise DeployError("Candidate changes the legacy Docker entrypoint")
            if self.identity(old["Image"]) != self.identity(image["Id"], require_pause=True):
                raise DeployError("Candidate changes persistent D1/KV identities")
            rollback = f"hackmamba/open-seo:rollback-{self.transaction.name.lower()}"
            self.docker("tag", old["Image"], rollback)
            # Recheck immediately before downtime: archive loading may take minutes.
            before = self.database()
            old_metadata = {"image": old["Image"], "rollback_image": rollback, "version": old_version,
                            "revision": sha, "candidate": candidate, "counts": before}
            self.enable_maintenance()
            # Reject in-flight work again after blocking all new user requests.
            before = self.database()
            # Mark the window before stop: even a timeout may have stopped the container.
            stopped = True
            self.compose("stop", "-t", str(self.c["stop_timeout_seconds"]), self.c["service"])
            self.ensure_volume_stopped()
            before = self.database()
            self.backup(old_metadata)
            backup_ready = True
            atomic_bytes(self.pause_file, self.transaction.name.encode())
            self.pin(candidate)
            self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", self.c["service"])
            current = self.inspect_container()
            if current["Image"] != image["Id"]:
                raise DeployError("Compose did not start the candidate image")
            self.wait(version)
            if self.inspect_container()["Image"] != image["Id"]:
                raise DeployError("Candidate changed during readiness verification")
            after = self.database(reject_active=False)
            if any(after.get(table, -1) < count for table, count in before.items()):
                raise DeployError("Application records disappeared during migration")
            if not self.maintenance_probe():
                raise DeployError("Maintenance gate stopped protecting the unaccepted candidate")
            receipt = {"status": "accepted", "revision": sha, "version": version,
                       "image": image["Id"], "previous": old_metadata, "counts": after}
            atomic_bytes(self.transaction / "receipt.json", json.dumps(receipt, indent=2).encode())
            # Commit point: after acceptance, releasing cron/users can cause
            # external writes. Never restore an older snapshot beyond here.
            accepted = True
            self.pause_file.unlink()
            sync_directory(self.volume)
            self.disable_maintenance()
            receipt["status"] = "deployed"
            atomic_bytes(self.transaction / "receipt.json", json.dumps(receipt, indent=2).encode())
            self.log(f"Deployment healthy: {version} ({sha}); backup: {self.transaction}")
        except BaseException:
            if accepted:
                self.log(f"Candidate accepted; finalization incomplete, automatic rollback forbidden: {self.transaction}")
            elif stopped:
                try:
                    self.restore(rollback, old_version, backup_ready)
                    self.disable_maintenance()
                except BaseException as rollback_error:
                    self.log(f"ROLLBACK FAILED: {type(rollback_error).__name__}: {rollback_error}; recovery: {self.transaction}")
                    raise DeployError(f"Deployment and rollback failed; manual recovery: {self.transaction}") from rollback_error
            elif self.maintenance_owned:
                self.wait(old_version)
                self.disable_maintenance()
            raise
        finally:
            # Backups and failed data remain; the large transport archive is transient.
            for name in ("image.tar.gz", "image.tar.gz.partial"):
                (self.transaction / name).unlink(missing_ok=True)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def main():
    os.umask(0o077)
    # A lost SSH connection after upload must not terminate a migration/rollback.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    if os.geteuid() != 0:
        raise DeployError("Deployment helper must run as root")
    Deployment(load_config()).execute(sys.argv[1:], sys.stdin.buffer)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Deployment failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
