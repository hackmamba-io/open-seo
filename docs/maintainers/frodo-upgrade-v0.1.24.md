# Frodo: OpenSEO v0.1.9 to v0.1.24

This fork includes upstream tag `v0.1.24` and retains the existing
`get_ai_brand_visibility` and `explore_ai_prompt` MCP tools. The latter shares
the upstream `explore_prompt` implementation, including authorization, billing,
and project-country defaults.

Frodo is reported to run on Hetzner. Its live version, container configuration,
and credentials have not been inspected. The steps below apply to an existing
installation using the root `compose.yaml` and its `/app/.wrangler` data mount.
Confirm those details on the server before using them.

## Keep the existing Docker deployment

Use `deploy/docker/Dockerfile`, the supported single-container edition. Its
entrypoint applies SQLite migrations and starts the built-in scheduler every
five minutes. Keep the existing data volume and authentication/reverse-proxy
configuration. Moving to `deploy/standalone/` is a separate data migration and
is not required for Prompt Tracking.

Build the fork on the target server, from the reviewed update commit:

```sh
docker build -f deploy/docker/Dockerfile -t hackmamba-open-seo:0.1.24 .
```

The default Compose image is `ghcr.io/every-app/open-seo:latest`. It does not
include this fork's two custom MCP tools. Upstream's Docker publish workflow
also skips forks. Set `OPEN_SEO_IMAGE=hackmamba-open-seo:0.1.24` in the server's
existing `.env`, preserving its other settings, before recreating the service.
Do not rely on an upstream image pull to deliver the fork's changes.

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
chmod 600 "$frodo_backup_dir/env"
docker compose stop open-seo
docker cp "$frodo_container":/app/.wrangler "$frodo_backup_dir/wrangler"
```

Confirm that the backup contains the database and storage state. Retain the
previous image and deployment configuration. With the new image pinned in
`.env`, recreate only the application service:

```sh
docker compose up -d --no-deps open-seo
docker compose ps
docker compose logs --tail 100 open-seo
docker compose exec -T open-seo node -e 'fetch("http://127.0.0.1:"+(process.env.PORT||3001)+"/api/health").then(async r=>{console.log(await r.text());process.exit(r.ok?0:1)}).catch(()=>process.exit(1))'
```

Expect version `0.1.24`, database status `ok`, successful migrations, and no
scheduler failures. Check existing client projects, saved keywords, reports,
and both custom MCP tools before enabling new schedules. Preserve the original
mount target `/app/.wrangler` and the D1/KV IDs in `wrangler.jsonc`.

## AI Visibility setup

The existing `DATAFORSEO_API_KEY` supplies answer collection.
`OPENROUTER_API_KEY` is needed by the UI setup/research flow and generated prompt
suggestions. Keep credentials in the server's existing secret configuration.
Open a client project, choose **AI Visibility → Prompt Tracking**, review its
market, prompts, engines and estimated cost, and enable the requested schedule.
Enabling tracking starts an initial paid check; verify it and the next scheduled
check with the operator's approval. See the [AI Visibility guide](../../web/content/docs/ai-visibility.md).

## Recovery

Do not remove the original volume. Do not run `docker compose down -v`.
Reverting the image does not reverse database migrations. If recovery requires
the old version, stop the new container and restore the matching pre-upgrade
data, environment and image together into the original deployment layout.

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
- Independent review found no confirmed integration defects. Production,
  provider-funded answer collection, and a due tracker completing a real
  scheduled check were not exercised.
