# Frodo: OpenSEO v0.1.9 to v0.1.24

This fork includes upstream tag `v0.1.24` and retains the existing
`get_ai_brand_visibility` and `explore_ai_prompt` MCP tools. The latter shares
the upstream `explore_prompt` implementation, including authorization, billing,
and project-country defaults.

Frodo runs at `https://frodo.hackmamba.io/` on Hetzner server `openseo`
(`162721813`, `46.225.52.53`). Inspection on 2026-11-04 confirmed version
`0.1.9`, a clean checkout at `7839485`, source directory `/opt/open-seo`,
Compose project `open-seo`, container `open-seo-open-seo-1`, and volume
`open-seo_open_seo_data` mounted at `/app/.wrangler`. Cloudflare Access and
Caddy protect the existing `local_noauth` application; preserve that boundary.

## Keep the existing Docker deployment

Use `deploy/docker/Dockerfile`, the supported single-container edition. Its
entrypoint applies SQLite migrations and starts the built-in scheduler every
five minutes. Keep the existing data volume and authentication/reverse-proxy
configuration. Moving to `deploy/standalone/` is a separate data migration and
is not required for Prompt Tracking.

Build the fork from the reviewed update commit. Leave the running deployment
directory unchanged until its backup has been taken. Frodo uses x86_64; when
building on an ARM computer, explicitly select `linux/amd64`, then transfer
the image with `docker save/load` and verify its checksum:

```sh
docker build --platform linux/amd64 -f deploy/docker/Dockerfile -t hackmamba/open-seo:0.1.24-frodo .
```

The default Compose image is `ghcr.io/every-app/open-seo:latest`. It does not
include this fork's two custom MCP tools. Upstream's Docker publish workflow
also skips forks. The service must explicitly use the fork's image after the
backup below. Do not rely on an upstream image pull to deliver these changes.

This legacy image builds the application on its first boot, after migrations.
An image build alone does not precompile the app. The repository sets a 4 GiB
Node heap ceiling; Frodo has 4 GiB RAM and 4 GiB swap. Complete a bounded-memory
boot test first, then stop the old app before starting the new image so its
resident memory is released. Do not build two live copies on this host.

The scheduler invokes the common cron immediately after startup and then every
five minutes. Inspect existing rank trackers as well as AI trackers before
booting; startup can otherwise initiate paid work. Before this upgrade Frodo
had no active audits or rank runs, no tracked keywords, and one weekly rank
configuration whose next check was a week away.

## Back up and start the new image

Build before downtime. Let active checks finish, then stop the existing service
before copying its data. From the existing Compose project directory:

```sh
frodo_container=$(docker compose ps -q open-seo)
test -n "$frodo_container"
frodo_backup_dir="$PWD/../frodo-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$frodo_backup_dir"
docker inspect --format '{{.Image}}' "$frodo_container" > "$frodo_backup_dir/image-id"
cp .env "$frodo_backup_dir/env"
cp compose.yaml "$frodo_backup_dir/compose.yaml"
chmod 600 "$frodo_backup_dir/env"
docker compose stop open-seo
docker cp "$frodo_container":/app/.wrangler "$frodo_backup_dir/wrangler"
```

Confirm that the backup contains the database and storage state. Also retain
the previous image and any active Compose overrides, reverse-proxy configuration
and external secret configuration. Only after backing up the original files,
set `OPEN_SEO_IMAGE=hackmamba/open-seo:0.1.24-frodo` in the existing deployment's `.env`,
preserving its other settings. Update the deployment files for the new checkout
without changing its Compose project name or volume mapping, then recreate only
the application service:

```sh
docker compose up -d --no-deps open-seo
docker compose ps
docker compose logs --tail 100 open-seo
docker compose exec -T open-seo node -e 'fetch("http://127.0.0.1:"+(process.env.PORT||3001)+"/api/health").then(async r=>{const h=await r.json();console.log(JSON.stringify(h));process.exit(r.ok&&h.status==="ok"&&h.version==="0.1.24"&&h.checks.database.status==="ok"?0:1)}).catch(()=>process.exit(1))'
```

Expect version `0.1.24`, database status `ok`, successful migrations, and no
scheduler failures. Check existing client projects, saved keywords, reports,
and both custom MCP tools before enabling new schedules. Preserve the original
mount target `/app/.wrangler` and the D1/KV IDs in `wrangler.jsonc`.

## AI Visibility setup

The existing `DATAFORSEO_API_KEY` supplies answer collection.
`OPENROUTER_API_KEY` is needed by the UI setup/research flow and generated prompt
suggestions. Without it the entire Prompt Tracking UI displays a missing-key
gate. Frodo had no OpenRouter key before the upgrade. Keep credentials in the
server's existing secret configuration.
Open a client project, choose **AI Visibility → Prompt Tracking**, review its
market, prompts, engines and estimated cost, and enable the requested schedule.
Enabling tracking starts an initial paid check; verify it and the next scheduled
check with the operator's approval. See the [AI Visibility guide](../../web/content/docs/ai-visibility.md).

## Recovery

Do not remove the original volume. Do not run `docker compose down -v`.
Reverting the image does not reverse database migrations. If recovery requires
the old version, stop the new container and restore the matching pre-upgrade
data, environment and image together into the original deployment layout.
Keep the failed volume state in a separate recovery directory and restore the
snapshot into an empty volume. Do not overlay the archive onto migrated data:
new WAL, sidecar and runtime files could remain. Verify the backup checksum
before migration and before restoration.

## Local verification, 2026-11-04

- Frozen-lockfile install, production build, typecheck, lint and Knip passed.
- All 1,891 tests passed, including both prompt-tool names, project authorization,
  paid-plan enforcement, output-schema compatibility and scheduler behavior.
- Built and ran this fork's legacy Docker image against a populated v0.1.9 D1
  baseline. All 64 migrations applied; seeded identity, project, saved keyword,
  rank tracker, tracked keyword and report fields were preserved. The database
  foreign-key check passed.
- Browser navigation displayed the migrated keyword. Creating a new project
  and reopening it preserved its data. Prompt Tracking displayed its missing-key
  setup state. Live MCP listed 84 tools and read the new tracker; access to an
  unknown project was rejected. The real scheduled handler returned `outcome: ok`
  with no due trackers.
- A separate `linux/amd64` container completed first boot with a 3 GiB memory
  limit and 5 GiB combined memory/swap limit. Health reported version `0.1.24`
  and database `ok`; no OOM kill occurred.
- Independent review found no remaining confirmed integration defects.
  Provider-funded answer collection and a due tracker completing a real
  scheduled check were not exercised.

## Live deployment, 2026-11-04

Frodo was upgraded using image `hackmamba/open-seo:0.1.24-frodo`, container-reported image ID
`sha256:3d1a23031e2d8e1cb7ab47e5220613087686579c3d790355f58d1770287ecf7c`.
The existing Compose project, named volume, loopback port, Caddy and Cloudflare
Access boundary were preserved. The application container started at
`2026-11-04T21:34:33Z`; it became healthy without restarts or OOM kills.

Recovery material is under `/opt/frodo-backups/20261104T212952Z`: the old image
export and image ID, original environment and Compose files, Caddy configuration,
pre-upgrade revision, row baseline, and a consistent stopped-volume archive with
SHA256 checksum. The data/configuration backup also has a checksum-verified
off-server copy in the operator's private local configuration directory. Keep
these backups private: they contain credentials and client data. The retained
old image is also tagged `hackmamba/open-seo:pre-v0.1.24-frodo`.

Post-upgrade checks confirmed:

- Public HTTPS health reported version `0.1.24`, status `ok`, and database `ok`.
- All 64 migrations were present. Integrity and foreign-key checks passed.
  Every pre-existing table retained its row count except the migration ledger
  (`48` to `64`). Original fields in the user, organization, projects and rank
  configuration matched the stopped baseline; all four projects were preserved.
- Live MCP exposed 84 tools, including both fork-specific aliases and AI
  visibility schedule tools. It listed the same four projects, read a tracker,
  and rejected access to an unknown project.
- The real scheduled handler returned `outcome: ok`; no AI or rank check runs
  were started by verification. A due, provider-funded scheduled run remains
  untested.
- Browser navigation loaded an existing project, its GSC data, and the new
  **AI Visibility → Prompt Tracking** page. The page correctly displayed
  **Add an OpenRouter API key**. Setup for a client still requires configuring
  `OPENROUTER_API_KEY` and restarting OpenSEO; no client schedule was enabled.

The reviewed source branch is `update/openseo-v0.1.24` in both the local and
server checkouts. GitHub publication was unavailable because the authenticated
GitHub account received HTTP 403 on push. Retain the source bundle with the
deployment artifacts until the branch can be pushed by an authorized maintainer.
