#!/usr/bin/env python3
"""Isolated Docker/Compose/SQLite rollout test; never contacts Frodo.

Run `PYTHONDONTWRITEBYTECODE=1 python3 deploy/frodo/integration.py` with a
local Docker daemon. Only uniquely labelled fixture resources are removed.
The HTTP maintenance callback exercises the marker contract; production Caddy
TLS configuration needs its separate deployment smoke test.
"""
import contextlib
import gzip
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

FIXTURE = r'''
import contextlib,http.server,json,os,pathlib,sqlite3,threading,time
root=pathlib.Path('/app/.wrangler');root.mkdir(exist_ok=True)
mode=os.environ['FIXTURE_MODE'];version=os.environ['FIXTURE_VERSION']
with contextlib.closing(sqlite3.connect(root/'db.sqlite')) as db,db:
 db.executescript("CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT); CREATE TABLE IF NOT EXISTS d1_migrations(id INTEGER PRIMARY KEY); CREATE TABLE IF NOT EXISTS external_writes(id INTEGER PRIMARY KEY);")
 if not db.execute('SELECT 1 FROM projects').fetchone():db.execute("INSERT INTO projects VALUES ('client-1','original client')")
 if not db.execute('SELECT 1 FROM d1_migrations WHERE id=48').fetchone():db.execute('INSERT INTO d1_migrations VALUES (48)')
 if mode=='good' and not db.execute('SELECT 1 FROM d1_migrations WHERE id=49').fetchone():
  db.execute('ALTER TABLE projects ADD COLUMN upgraded TEXT');db.execute('INSERT INTO d1_migrations VALUES (49)')
 if mode=='bad':
  db.execute("UPDATE projects SET name='failed migration'")
  (root/'candidate-sidecar').write_text('must be quarantined on rollback')
def cron():
 while True:
  if not (root/'.openseo-deploy-pause').exists():(root/('cron-'+mode)).write_text('fired')
  time.sleep(.05)
threading.Thread(target=cron,daemon=True).start()
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  if self.path=='/write':
   with contextlib.closing(sqlite3.connect(root/'db.sqlite')) as db,db:db.execute('INSERT INTO external_writes DEFAULT VALUES')
   result={'write':'accepted'}
  else:result={'status':'ok','version':version,'checks':{'database':{'status':'error' if mode=='bad' else 'ok'}}}
  body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
http.server.HTTPServer(('0.0.0.0',int(os.environ['PORT'])),Handler).serve_forever()
'''

DOCKERFILE = '''FROM node:22-alpine
RUN apk add --no-cache python3
WORKDIR /app
COPY fixture.py wrangler.jsonc ./
COPY scripts ./scripts
COPY deploy ./deploy
ARG MODE
ARG VERSION
ARG REVISION
ARG OWNER
ENV FIXTURE_MODE=$MODE FIXTURE_VERSION=$VERSION
LABEL org.opencontainers.image.revision=$REVISION org.opencontainers.image.version=$VERSION io.hackmamba.frodo.fixture=$OWNER
CMD ["sh", "deploy/docker/docker-entrypoint.sh"]
'''


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args[:3]}\n{result.stderr[-3000:]}")
    return result.stdout


def controller():
    sys.path.insert(0, "/code")
    import deploy
    spec = json.loads(Path("/input/spec.json").read_text())
    work, volume = Path(spec["control_path"]), Path(spec["volume_path"])
    app, state = work / "app", work / "state"
    app.mkdir(); state.mkdir()
    marker = work / "maintenance"
    port = spec["port"]
    env = f"# fixture secrets must survive\nOPEN_SEO_IMAGE={spec['old']['tag']}\nPORT={port}\nUNCHANGED=original-value\n"
    (app / ".env").write_text(env)
    (app / "compose.yaml").write_text(f'''services:
  open-seo:
    image: ${{OPEN_SEO_IMAGE}}
    env_file: .env
    ports:
      - "127.0.0.1:{port}:{port}"
    volumes:
      - data:/app/.wrangler
volumes:
  data:
    external: true
    name: {spec['volume']}
''')
    cfg = {"deployment_dir": str(app), "state_dir": str(state), "project": spec["project"],
           "service": "open-seo", "container": spec["container"], "volume": spec["volume"],
           "volume_path": str(volume), "database_relative_path": "db.sqlite", "port": port,
           "health_url": f"http://127.0.0.1:{port}/api/health", "maintenance_file": str(marker),
           "maintenance_probe_url": "https://frodo.hackmamba.io/", "max_archive_bytes": 1024**3,
           "max_unpacked_bytes": 2 * 1024**3, "disk_reserve_bytes": 64 * 1024**2,
           "health_timeout_seconds": 6, "stop_timeout_seconds": 3, "upload_timeout_seconds": 60}
    config_file = work / "fixture-config.json"
    config_file.write_text(json.dumps(cfg))
    config_file.chmod(0o666)
    try:
        deploy.load_config(config_file)
    except deploy.DeployError:
        pass
    else:
        raise AssertionError("Writable root configuration was accepted")
    config_file.chmod(0o600)
    cfg = deploy.load_config(config_file)

    def backend(path):
        code = f"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:{port}{path}',timeout=2).read().decode())"
        return command(["/usr/bin/docker", "exec", spec["container"], "python3", "-c", code])

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if marker.exists():
                body = b"Frodo deployment in progress"
                self.send_response(503)
                self.send_header("X-Frodo-Deployment", "maintenance")
            else:
                body = backend(self.path).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

    proxy = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    blocked_writes = 0

    def probe():
        try:
            urllib.request.urlopen(proxy_url, timeout=3)
        except urllib.error.HTTPError as response:
            return (response.code == 503 and response.headers.get("X-Frodo-Deployment") == "maintenance"
                    and response.read().strip() == b"Frodo deployment in progress")
        return False

    def health():
        nonlocal blocked_writes
        try:
            result = json.loads(backend("/api/health"))
        except RuntimeError as error:
            raise OSError("fixture HTTP not ready") from error
        if marker.exists():
            try:
                urllib.request.urlopen(proxy_url + "/write", timeout=3)
            except urllib.error.HTTPError as response:
                if response.code != 503:
                    raise AssertionError("External write was not blocked")
                blocked_writes += 1
            else:
                raise AssertionError("Unaccepted candidate accepted an external write")
            mode = command(["/usr/bin/docker", "inspect", "--format", '{{range .Config.Env}}{{println .}}{{end}}', spec["container"]])
            if "FIXTURE_MODE=bad\n" in mode and (volume / "cron-bad").exists():
                raise AssertionError("Bad candidate ran cron before acceptance")
        return result

    deployment = deploy.Deployment(cfg, health=health, maintenance_probe=probe)
    deployment.compose("up", "-d", "--no-deps", "--pull", "never", "open-seo")
    deployment.wait(spec["old"]["version"])
    old_id = deployment.inspect_container()["Image"]
    results = {}
    try:
        for name in ("good", "bad"):
            image = spec[name]
            archive = Path("/input") / (name + ".tar.gz")
            args = [image["sha"], image["version"], image["archive_sha"], str(archive.stat().st_size)]
            previous_db = hashlib.sha256((volume / "db.sqlite").read_bytes()).hexdigest()
            previous_image = deployment.inspect_container()["Image"]
            try:
                with archive.open("rb") as stream:
                    deployment.execute(args, stream)
            except deploy.DeployError as error:
                if name != "bad" or "readiness" not in str(error):
                    raise
            else:
                if name == "bad":
                    raise AssertionError("Bad candidate unexpectedly passed")
            with contextlib.closing(sqlite3.connect(volume / "db.sqlite")) as db:
                assert db.execute("SELECT id,name FROM projects").fetchall() == [("client-1", "original client")]
                assert db.execute("SELECT count(*) FROM external_writes").fetchone()[0] == 0
            assert "UNCHANGED=original-value\n" in (app / ".env").read_text()
            assert not marker.exists() and not (volume / ".openseo-deploy-pause").exists()
            assert not (deployment.transaction / "image.tar.gz").exists()
            if name == "good":
                assert deployment.inspect_container()["Image"] != old_id
                assert (deployment.transaction / "receipt.json").exists()
                deadline = time.monotonic() + 3
                while not (volume / "cron-good").exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert (volume / "cron-good").exists(), "Cron must resume after acceptance"
            else:
                assert deployment.inspect_container()["Image"] == previous_image
                assert hashlib.sha256((volume / "db.sqlite").read_bytes()).hexdigest() == previous_db
                assert not (volume / "candidate-sidecar").exists()
                assert not (volume / "cron-bad").exists()
                assert (deployment.transaction / "failed-volume" / "candidate-sidecar").exists()
                assert not (deployment.transaction / "receipt.json").exists()
            results[name] = {"image": deployment.inspect_container()["Image"], "database_preserved": True,
                             "transport_removed": True, "maintenance_released": True}
        assert blocked_writes >= 2
        results["blocked_http_writes"] = blocked_writes
        results["docker_save_load"] = "actual Docker Desktop archive"
        print("FRODO_INTEGRATION_RESULT=" + json.dumps(results), flush=True)
    finally:
        proxy.shutdown()
        deployment.compose("stop", "-t", "3", "open-seo")


def host():
    owner = uuid.uuid4().hex
    project = "frodo-fixture-" + owner[:12]
    volume, control = project + "-data", project + "-control"
    container = project + "-open-seo-1"
    label = "io.hackmamba.frodo.fixture=" + owner
    script = Path(__file__).resolve()
    print(f"Running isolated Frodo fixture {project}", flush=True)
    command(["docker", "info"], timeout=30)
    with tempfile.TemporaryDirectory(prefix=project + "-") as temp:
        root = Path(temp)
        try:
            (root / "Dockerfile").write_text(DOCKERFILE)
            (root / "fixture.py").write_text(FIXTURE)
            (root / "wrangler.jsonc").write_text(json.dumps({"d1_databases": [{"binding": "DB", "database_id": "fixture-persistent-db"}],
                                                            "kv_namespaces": [{"binding": "KV", "id": "fixture-persistent-kv"}]}))
            (root / "scripts").mkdir()
            (root / "scripts" / "selfhost-scheduler.mjs").write_text("// fixture.py obeys .openseo-deploy-pause\n")
            (root / "deploy" / "docker").mkdir(parents=True)
            (root / "deploy" / "docker" / "docker-entrypoint.sh").write_text("#!/bin/sh\nexec python3 /app/fixture.py\n")
            spec = {"project": project, "volume": volume, "container": container,
                    "port": 39000 + int(owner[:4], 16) % 20000}
            for mode, version in (("old", "0.1.23"), ("good", "0.1.24"), ("bad", "0.1.25")):
                sha = (uuid.uuid4().hex + uuid.uuid4().hex)[:40]
                tag = "hackmamba/open-seo:git-" + sha
                print(f"Building isolated {mode} fixture", flush=True)
                command(["docker", "build", "--platform", "linux/amd64", "--build-arg", "MODE=" + mode,
                         "--build-arg", "VERSION=" + version, "--build-arg", "REVISION=" + sha,
                         "--build-arg", "OWNER=" + owner, "-t", tag, str(root)], timeout=600)
                spec[mode] = {"sha": sha, "tag": tag, "version": version}
                if mode != "old":
                    archive = root / (mode + ".tar.gz")
                    process = subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    with gzip.open(archive, "wb", compresslevel=1) as output:
                        shutil.copyfileobj(process.stdout, output)
                    _, error = process.communicate()
                    if process.returncode:
                        raise RuntimeError(error.decode())
                    spec[mode]["archive_sha"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            for name in (volume, control):
                command(["docker", "volume", "create", "--label", label, name])
            spec["volume_path"] = json.loads(command(["docker", "volume", "inspect", volume]))[0]["Mountpoint"]
            spec["control_path"] = json.loads(command(["docker", "volume", "inspect", control]))[0]["Mountpoint"]
            (root / "spec.json").write_text(json.dumps(spec))
            (root / "Controller.Dockerfile").write_text("FROM docker:28-cli\nRUN apk add --no-cache python3 && ([ -e /usr/bin/docker ] || ln -s /usr/local/bin/docker /usr/bin/docker)\n")
            controller_tag = project + ":controller"
            command(["docker", "build", "--label", label, "-f", str(root / "Controller.Dockerfile"), "-t", controller_tag, str(root)], timeout=600)
            result = subprocess.run(["docker", "run", "--rm", "--name", project + "-controller", "--label", label,
                                     "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", "/var/run/docker.sock:/var/run/docker.sock",
                                     "-v", str(root) + ":/input:ro", "-v", str(script.parent) + ":/code:ro",
                                     "--mount", "type=bind,src=" + str(Path(spec["volume_path"]).parent.parent) + ",dst=" + str(Path(spec["volume_path"]).parent.parent),
                                     "--entrypoint", "python3", controller_tag, "/code/integration.py", "--controller"], timeout=180)
            if result.returncode:
                raise RuntimeError("Isolated Docker fixture controller failed")
        finally:
            # Resource ownership is the unique label, never a global prune.
            for resource in command(["docker", "ps", "-aq", "--filter", "label=" + label]).split():
                command(["docker", "rm", "-f", resource])
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            for name in (volume, control):
                subprocess.run(["docker", "volume", "rm", name], capture_output=True)
            tags = command(["docker", "image", "ls", "--filter", "label=" + label, "--format", "{{.Repository}}:{{.Tag}}"]).splitlines()
            for tag in tags:
                if "<none>" not in tag:
                    subprocess.run(["docker", "image", "rm", tag], capture_output=True)
    print("Isolated Docker fixture finished; owned containers/volumes/tags removed", flush=True)


if __name__ == "__main__":
    controller() if sys.argv[1:] == ["--controller"] else host()
