#!/bin/sh
# Install the reviewed receiver on the existing Frodo host, without restarting it.
set -eu

test "$(id -u)" = 0 || { echo 'Run this installer as root.' >&2; exit 1; }
test "$#" = 1 || { echo 'Usage: install.sh /path/to/ci-key.pub' >&2; exit 1; }
frodo_source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
frodo_public_key=$(realpath -- "$1")
for frodo_command in docker python3 sudo visudo ssh-keygen useradd install caddy; do
  command -v "$frodo_command" >/dev/null
done
test -f "$frodo_source_dir/deploy.py"
test -f "$frodo_source_dir/ssh-gate.py"
test -f "$frodo_source_dir/gate.caddy"
ssh-keygen -lf "$frodo_public_key" >/dev/null

python3 - "$frodo_public_key" <<'PY'
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import subprocess

import sys

key = Path(sys.argv[1]).read_text().strip()
if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?', key):
    raise SystemExit('Supply exactly one Ed25519 public key, without SSH options.')
for target in ['/var/lib/frodo-deploy/.ssh/authorized_keys', '/opt/frodo-deploy',
               '/opt/frodo-deploy-gate', '/etc/caddy/Caddyfile', '/etc/caddy/frodo-deploy.caddy',
               '/usr/local/lib/frodo-deploy', '/usr/local/sbin/frodo-deploy']:
    path = Path(target)
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise SystemExit('Refusing symlink installation paths.')
        if part.exists() and (part.stat().st_uid != 0 or part.stat().st_mode & 0o022):
            raise SystemExit('Installation paths must be root-owned and not writable by other users.')
authorization = Path('/var/lib/frodo-deploy/.ssh/authorized_keys')
expected = 'restrict,command="/usr/local/lib/frodo-deploy/ssh-gate.py" ' + key + '\n'
if authorization.exists() and authorization.read_text() != expected:
    raise SystemExit('Existing CI key differs; review an explicit key rotation first.')
try:
    account = pwd.getpwnam('frodo-deploy')
except KeyError:
    account = None
if account:
    groups = subprocess.check_output(['id', '-G', 'frodo-deploy'], text=True).split()
    if account.pw_uid == 0 or account.pw_dir != '/var/lib/frodo-deploy' or groups != [str(account.pw_gid)]:
        raise SystemExit('Existing frodo-deploy account has unexpected access; inspect it manually.')

container = json.loads(subprocess.check_output(['/usr/bin/docker', 'inspect', 'open-seo-open-seo-1'], text=True))[0]
labels = container['Config'].get('Labels', {})
if labels.get('com.docker.compose.project') != 'open-seo' or labels.get('com.docker.compose.service') != 'open-seo':
    raise SystemExit('Unexpected Compose project or service.')
if not container['State']['Running']:
    raise SystemExit('Frodo must already be running before installing deployment access.')
mounts = [m for m in container['Mounts'] if m['Destination'] == '/app/.wrangler']
if len(mounts) != 1 or mounts[0].get('Name') != 'open-seo_open_seo_data':
    raise SystemExit('Unexpected data volume.')
bindings = container['HostConfig']['PortBindings'].get('3001/tcp', [])
if bindings != [{'HostIp': '127.0.0.1', 'HostPort': '3001'}]:
    raise SystemExit('Expected only the existing loopback port binding.')
volume = Path(mounts[0]['Source'])
if volume.stat().st_dev != Path('/opt').stat().st_dev:
    raise SystemExit('Deployment state and the Docker volume must be on the same filesystem for atomic recovery.')
databases = []
for path in volume.glob('state/v3/d1/miniflare-D1DatabaseObject/*.sqlite'):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
            databases.append(path)
    finally:
        connection.close()
if len(databases) != 1:
    raise SystemExit('Expected exactly one existing OpenSEO D1 database.')
config = {
    'deployment_dir': '/opt/open-seo',
    'state_dir': '/opt/frodo-deploy',
    'project': 'open-seo',
    'service': 'open-seo',
    'container': 'open-seo-open-seo-1',
    'volume': 'open-seo_open_seo_data',
    'volume_path': str(volume),
    'database_relative_path': str(databases[0].relative_to(volume)),
    'health_url': 'http://127.0.0.1:3001/api/health',
    'maintenance_file': '/opt/frodo-deploy-gate/maintenance',
    'maintenance_probe_url': 'https://frodo.hackmamba.io/',
    'port': 3001,
    'max_archive_bytes': 6442450944,
    'disk_reserve_bytes': 2147483648,
    'health_timeout_seconds': 900,
    'stop_timeout_seconds': 60,
    'upload_timeout_seconds': 900,
}
os.umask(0o077)
destination = Path('/etc/frodo-deploy.json')
if destination.is_symlink():
    raise SystemExit('Refusing a symlink deployment config.')
if destination.exists():
    if json.loads(destination.read_text()) != config:
        raise SystemExit('Existing deployment config differs; review it before changing it.')
else:
    temporary = destination.with_suffix('.json.new')
    with temporary.open('x') as stream:
        stream.write(json.dumps(config, indent=2) + '\n')
    temporary.replace(destination)
print('Validated existing deployment and wrote root-only receiver configuration.')
PY

if ! id frodo-deploy >/dev/null 2>&1; then
  useradd --system --user-group --create-home --home-dir /var/lib/frodo-deploy --shell /bin/sh frodo-deploy
fi
# Root owns the public authorization file so this account cannot remove its gate.
install -d -o root -g root -m 755 /var/lib/frodo-deploy /var/lib/frodo-deploy/.ssh
install -d -o root -g root -m 700 /opt/frodo-deploy
install -d -o root -g root -m 755 /opt/frodo-deploy-gate
install -d -o root -g root -m 755 /usr/local/lib/frodo-deploy
install -o root -g root -m 755 "$frodo_source_dir/deploy.py" /usr/local/sbin/frodo-deploy
install -o root -g root -m 755 "$frodo_source_dir/ssh-gate.py" /usr/local/lib/frodo-deploy/ssh-gate.py

# Install the maintenance route with no active marker and validate before reload.
# Refuse to rewrite a customized site: this installer targets the inspected host.
python3 - "$frodo_source_dir/gate.caddy" <<'PY'
import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

marker = Path('/opt/frodo-deploy-gate/maintenance')
if marker.exists():
    raise SystemExit('Maintenance is active; resolve the interrupted deployment before installing.')
path = Path('/etc/caddy/Caddyfile')
snippet = Path('/etc/caddy/frodo-deploy.caddy')
original = path.read_bytes()
addition = '    import /etc/caddy/frodo-deploy.caddy\n'
if addition.encode() in original:
    candidate = original
else:
    pattern = r'(frodo\.hackmamba\.io\s*\{\s*\n)(\s*reverse_proxy 127\.0\.0\.1:3001\s*\n\})'
    updated, count = re.subn(pattern, lambda m: m[1] + addition + m[2], original.decode())
    if count != 1:
        raise SystemExit('Caddy site differs from the inspected layout; review the route manually.')
    candidate = updated.encode()
os.umask(0o077)
backup = Path('/opt/frodo-deploy') / ('bootstrap-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
backup.mkdir(mode=0o700)
(backup / 'Caddyfile').write_bytes(original)
old_snippet = snippet.read_bytes() if snippet.exists() else None
if old_snippet is not None:
    (backup / 'frodo-deploy.caddy').write_bytes(old_snippet)
temporary = Path('/etc/caddy/.frodo-Caddyfile.new')
try:
    # Existing root-owned paths were checked by the earlier preflight.
    snippet.write_bytes(Path(sys.argv[1]).read_bytes())
    snippet.chmod(0o644)
    with temporary.open('xb') as stream:
        stream.write(candidate)
    temporary.chmod(0o644)
    subprocess.run(['/usr/bin/caddy', 'validate', '--config', str(temporary), '--adapter', 'caddyfile'], check=True)
    temporary.replace(path)
    subprocess.run(['/usr/bin/caddy', 'reload', '--config', str(path), '--adapter', 'caddyfile'], check=True)
    with urllib.request.urlopen('http://127.0.0.1:3001/api/health', timeout=15) as response:
        if response.status != 200:
            raise RuntimeError('Application health failed during bootstrap')
except BaseException:
    path.write_bytes(original)
    path.chmod(0o644)
    if old_snippet is None:
        snippet.unlink(missing_ok=True)
    else:
        snippet.write_bytes(old_snippet)
        snippet.chmod(0o644)
    subprocess.run(['/usr/bin/caddy', 'reload', '--config', str(path), '--adapter', 'caddyfile'], check=True)
    raise
finally:
    temporary.unlink(missing_ok=True)
print('Validated and reloaded the inactive Caddy maintenance route; original saved at', backup)
PY

frodo_sudoers=$(mktemp /etc/sudoers.d/.frodo-deploy.XXXXXX)
trap 'rm -f "$frodo_sudoers"' EXIT HUP INT TERM
printf '%s\n' 'frodo-deploy ALL=(root) NOPASSWD: /usr/local/sbin/frodo-deploy' > "$frodo_sudoers"
visudo -cf "$frodo_sudoers"
install -o root -g root -m 440 "$frodo_sudoers" /etc/sudoers.d/frodo-deploy

python3 - "$frodo_public_key" <<'PY'
from pathlib import Path
import os
import sys

key = Path(sys.argv[1]).read_text().strip()
line = 'restrict,command="/usr/local/lib/frodo-deploy/ssh-gate.py" ' + key + '\n'
path = Path('/var/lib/frodo-deploy/.ssh/authorized_keys')
if path.is_symlink():
    raise SystemExit('Refusing a symlink authorized_keys file.')
if path.exists() and path.read_text() != line:
    raise SystemExit('Existing CI key differs; review an explicit key rotation first.')
path.write_text(line)
os.chown(path, 0, 0)
path.chmod(0o644)
PY

echo 'Frodo receiver installed. The application was not restarted.'
echo 'The CI key can invoke the deployment receiver only; shell and forwarding are restricted.'
