# Frodo deployments

`Deploy Frodo` updates `https://frodo.hackmamba.io/` after the fork's complete
`CI` workflow succeeds for a push to the current `main` commit. A manual dispatch
on `main` requires the same successful CI. Pull requests, upstream repositories,
other branches and failed CI cannot deploy. Superseded commits are skipped
before image transfer; a release already in progress finishes without
interruption. This workflow is separate from upstream's Cloudflare and Docker
publishing workflows.

The workflow builds this fork's legacy `deploy/docker/Dockerfile` for
`linux/amd64`, labels it with the full Git revision and package version, and
streams one checksum-verified Docker image over SSH. There is no registry login.
Application credentials stay in `/opt/open-seo/.env` on Hetzner; GitHub receives
only a dedicated, restricted deployment key and the pinned SSH host key.

## Server setup

Install the reviewed `deploy/frodo/` files on Hetzner server `openseo`
(`46.225.52.53`). The installer targets the inspected deployment: directory
`/opt/open-seo`, Compose project `open-seo`, service `open-seo`, data volume
`open-seo_open_seo_data`, and loopback port `3001`. It refuses a different layout
or an unexpected existing deployment account/key.

Generate a dedicated Ed25519 CI key outside the checkout. Copy only its public
key and the reviewed installer files to the server, then run as root:

```sh
sh /path/to/reviewed/deploy/frodo/install.sh /path/to/frodo-ci-key.pub
```

The installer creates:

- Root-owned `/etc/frodo-deploy.json` with the detected SQLite path and limits.
- Root-owned `/usr/local/sbin/frodo-deploy` and a fixed SSH command gate.
- An unprivileged `frodo-deploy` account with no Docker group membership.
  Its key has OpenSSH `restrict` and a forced command. Sudo permits only the
  receiver, which validates its four arguments and accepts an image on stdin.
- An inactive maintenance route in Caddy. The original config is backed up,
  the new config is validated before reload, and installation does not restart
  OpenSEO. A customized Caddy site requires manual review.

The dedicated CI key cannot open a shell, forward ports, modify its own
authorization file, change receiver code or choose another deployment path.
Possession still authorizes deploying application code and therefore accessing
the application's data and credentials. Keep it in the protected environment.
Updating receiver code requires a separate reviewed installer run; application
deployments do not overwrite the receiver.

## GitHub setup

Create environment **`frodo`** in `hackmamba-io/open-seo`, with deployment branches
restricted to the exact branch **`main`**. Store:

| Kind     | Name                    | Value                                             |
| -------- | ----------------------- | ------------------------------------------------- |
| Secret   | `FRODO_SSH_PRIVATE_KEY` | Dedicated CI private key, including final newline |
| Secret   | `FRODO_SSH_KNOWN_HOSTS` | Previously verified host key for `46.225.52.53`   |
| Variable | `FRODO_HOST`            | `46.225.52.53`                                    |

Use the already verified host key; do not generate trust with `ssh-keyscan`
during a workflow run. Do not store the operator's root key, root password,
DataForSEO key, Google credentials or OpenRouter key in this environment.

Review the workflow and its permissions before merging. Repository `AGENTS.md`
requires explicit maintainer review for `.github/**` changes. Restrict who can
update `main` and deployment workflow files; where valid code-owner rules are
configured, require their approval. If fork Actions are disabled, enable them
only after reviewing the inherited workflows and their repository guards.

After merging the reviewed branch, the push starts CI; its successful completion
starts deployment automatically. For a retry use **Actions → Deploy Frodo →
Run workflow → main**. Deployments serialize in GitHub and hold a host-side lock.
New pushes do not cancel a migration or restore already in progress.

## Deployment and acceptance

The receiver checks available disk space, upload length/digest, archive tags,
image architecture, revision/version labels, legacy startup and persistent
D1/KV identities before downtime. Archives with extra image tags are rejected
before `docker load`. Existing active audits or tracking jobs defer deployment.
An unsupported image or storage-layout change requires an operator-reviewed
migration rather than an automatic update.

Before stopping the app, the receiver creates the durable maintenance marker
`/opt/frodo-deploy-gate/maintenance`. Caddy must return the exact maintenance
`503` response; the receiver verifies it through loopback HTTPS with normal
certificate/SNI validation. Clients cannot write through the proxy during
candidate validation. Existing in-flight work is checked again before and
after stopping the app; if it remains active, the old app restarts unchanged.

The stopped volume is backed up in full, including SQLite WAL and runtime
state. The archive's checksum and a trial extraction are checked. Original
environment, Compose configuration and image metadata are saved alongside it;
the old image also receives a retained rollback tag. The state directory and
data volume must share a filesystem for atomic recovery moves.

Before starting the candidate, the receiver writes
`.wrangler/.openseo-deploy-pause` inside the data volume. The Docker scheduler
keeps serving health/UI but starts no cron work while this marker exists. Both
maintenance and scheduler markers survive a restart. Image validation requires
support for this pause protocol.

First startup still performs migrations and a build on Hetzner. The receiver
waits up to 15 minutes for JSON health to report the expected version,
`status: ok` and database `ok`, then checks persistence and row counts. Docker's
HTTP-200-only health check is insufficient for acceptance.

The accepted receipt is the commit point. Only after it is durably written are
the scheduler and public traffic released. The next scheduled tick can occur
up to five minutes later. A failure after acceptance keeps the accepted image
and data; it must never restore a snapshot over newly accepted writes.

## Failure and recovery

A candidate failure before acceptance automatically stops the candidate,
verifies no volume writer remains, moves its entire data state into a recovery
directory, and restores the stopped snapshot into an empty volume. It restores
the old environment/image and waits for database health before reopening
traffic. The workflow remains failed so the operator can inspect the cause.

Receipts, backups and sanitized logs live under `/opt/frodo-deploy/`, one private
directory per attempt. An interrupted upload leaves the old app running. A lost
SSH client after upload does not cancel an in-progress migration or restore.
GitHub cancellation can therefore finish before the receiver does; inspect its
receipt/log and host lock before retrying.

If rollback itself fails, maintenance remains active. Inspect `deploy.log`,
`previous.json`, `volume.tar.gz`, its checksum and any `failed-volume` first.
Use the matching old image, environment and data together. Never overlay a
snapshot on migrated files or run `docker compose down -v`.

After a host power loss or `SIGKILL`, automatic Python cleanup cannot run.
Durable markers keep the deployment closed until the operator establishes
which image/data were accepted. Resume the accepted version, or restore the
matching pre-acceptance snapshot; remove the scheduler pause and maintenance
marker only after checking health and confirming no deploy process holds the
lock. See the previous [Frodo upgrade runbook](frodo-upgrade-v0.1.24.md).

Backups and rollback image tags are retained deliberately. Uploaded image
archives are removed after the attempt. No global Docker prune or automatic
backup deletion runs. Disk checks stop a future deployment before downtime if
space becomes insufficient; review retention periodically. These same-host
automatic snapshots complement an independent off-server backup.

## Verification

```sh
python3 -m unittest discover -s deploy/frodo -p 'test_*.py' -v
python3 deploy/frodo/integration.py
pnpm exec vitest run src/server/lib/selfhost-scheduler.test.ts
shellcheck deploy/frodo/install.sh
actionlint .github/workflows/deploy-frodo.yml .github/workflows/ci.yml
```

The integration fixture uses isolated Docker resources and synthetic SQLite
data. It exercises real image transfer, Compose, HTTP readiness and clean
snapshot restoration. It never connects to Frodo or a paid data provider.
Actual GitHub activation additionally requires a successful workflow run and
verification of the live site; local tests alone do not establish that setup.

Bootstrap verification on 2026-11-05 passed 29 receiver tests, six scheduler
tests, the repository CI checks and the real Docker migration/rollback fixture.
Caddy 2.6.2 blocked GET/POST during maintenance and resumed normally. The
installed receiver also passed the live TLS maintenance probe, rejected a shell
command and an invalid image upload through the dedicated SSH key, and left the
existing healthy OpenSEO container running without a restart. GitHub activation
and its first real deployment are separate from these bootstrap checks.
