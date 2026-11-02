# Infrastructure

Minimal Terraform for the development deployment of Revel Gifts on GCP (task RG-7 / #4, feature F1). Follow the [agreed deployment foundation](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) and [implementation guidance](../docs/deployment.md). Nothing in this file proves that resources exist; record applied revisions and observed results in the Notion task.

Applying Terraform, DNS changes and domain verification need explicit user approval (see `AGENTS.md`).

## Layout

`infra/terraform/` (project `revel-gifts`, region `us-central1`):

| File | Contents |
| --- | --- |
| `versions.tf` | Terraform >= 1.9, `hashicorp/google ~> 8.9`, `hashicorp/random ~> 3.9`; local state |
| `variables.tf` / `terraform.tfvars.example` | Inputs; copy the example to `terraform.tfvars` (gitignored) |
| `apis.tf` | Enables run, sqladmin, secretmanager, artifactregistry, iam, cloudresourcemanager (`disable_on_destroy = false`) |
| `registry.tf` | Artifact Registry `revel` (our images) and `dockerhub` (Docker Hub pull-through remote repository) |
| `iam.tf` | Service accounts `revel-app`, `revel-openfga`; `cloudsql.client`; invoker bindings |
| `database.tf` | Cloud SQL PostgreSQL 16 `revel-db` (db-f1-micro, Enterprise, zonal, 10 GB SSD), databases `revel` and `openfga`, users `revel_app` and `openfga` |
| `secrets.tf` | `DATABASE_URL`, `OPENFGA_DATASTORE_URI` (generated) and empty placeholders `WORKOS_API_KEY`, `WORKOS_COOKIE_PASSWORD`, `FIREWORKS_API_KEY`, `META_API_KEY`; per-secret accessor grants. `WORKOS_CLIENT_ID` is not a secret (plain variable, see [WorkOS configuration](#workos-configuration)) |
| `run.tf` | Cloud Run services `revel-app`, `revel-openfga`; jobs `revel-migrate`, `revel-openfga-migrate`, `revel-fga-admin` (adopted with an `import` block); optional domain mapping; `google_project` data source (project number for the run.app `ALLOWED_HOSTS`) |
| `outputs.tf` | URLs, connection name, repository path, service accounts, DNS record |

### Network and access design

- **Cloud SQL** has a public IP with **no authorized networks**, so it is reachable only through the Cloud SQL connector (IAM `cloudsql.client` plus database password, encrypted). Cloud Run mounts the socket at `/cloudsql/<connection-name>`. This avoids a VPC, private service access and their cost.
- **OpenFGA** (`revel-openfga`) has ingress "all" but requires IAM authentication: there is no `allUsers` binding, and only the `revel-app` service account has `roles/run.invoker`. Requests without a Google ID token for that identity get 403 from Cloud Run before reaching OpenFGA. Only the HTTP API port (8080) is exposed; gRPC, metrics and the playground are disabled or not routable. The app calls the canonical `run.app` URL (`OPENFGA_API_URL`) with an ID token whose audience is that URL (`OPENFGA_GOOGLE_AUTH=true`). It can send the token in `X-Serverless-Authorization` if `Authorization` is needed for something else.
  - Alternative considered: internal-only ingress, which would also need Direct VPC egress from `revel-app` (a VPC, subnet and egress settings). That would add network isolation but more resources and setup. It was rejected for MVP scope; IAM authentication already keeps OpenFGA from being publicly usable.
- **revel-app** is public (`allUsers` invoker). User and agent authentication is the application's job (WorkOS), not Cloud Run IAM.
- **Database separation limit:** users created through the Cloud SQL API join `cloudsqlsuperuser`, so `revel_app` could technically connect to the `openfga` database (and vice versa). Each runtime only receives its own credentials. Tightening this needs SQL `REVOKE`/`GRANT` statements and is not part of the MVP.
- **Connection budget:** db-f1-micro allows about 25 connections. OpenFGA is capped at 1 instance x 5 connections; with `app_max_instances = 3`, keep the app's SQLAlchemy pool at 5 or fewer per instance (including overflow). Raise `db_tier` to `db-g1-small` if this is too tight.

## State

State is **local** (`infra/terraform/terraform.tfstate`, ignored by `.gitignore` along with `.terraform/`, `*.tfplan` and `*.tfvars`). It contains the generated database passwords and the `DATABASE_URL`/`OPENFGA_DATASTORE_URI` secret payloads. Keep it on the operator's machine, never commit or share it, and back it up privately. Only one operator should apply at a time. `.terraform.lock.hcl` is committed.

## Plan and apply

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # first time only
terraform init
terraform fmt -check && terraform validate
terraform plan -out=rg7.tfplan
terraform show -no-color rg7.tfplan > plan-output.txt   # gitignored; for review
terraform apply rg7.tfplan                              # only after approval
```

ADC must be authenticated as an account allowed to administer the project (`gcloud auth application-default login`). The first apply enables the APIs and then creates everything else. Service creation waits on the API enablement, which can take a minute or two to propagate; if a create fails with "API not enabled", wait and re-run `terraform apply`.

### Recovering from a partial first apply

If an apply fails partway (API propagation, quota, billing), a resource can exist in GCP without being recorded in state. The next apply then fails with **409 / "already exists"** for that resource. Either import it into state and re-plan, or delete the orphan (only if it holds nothing you need) and re-apply. Examples:

```sh
terraform import google_sql_database_instance.main projects/revel-gifts/instances/revel-db
terraform import google_service_account.app projects/revel-gifts/serviceAccounts/revel-app@revel-gifts.iam.gserviceaccount.com
terraform import google_cloud_run_v2_service.app projects/revel-gifts/locations/us-central1/services/revel-app
terraform import google_artifact_registry_repository.revel projects/revel-gifts/locations/us-central1/repositories/revel
terraform plan -out=rg7.tfplan   # re-check before applying again
```

The import ID format for each resource is in the provider docs ("Import" section). Do not import a database instance created by someone else without checking it first.

### Destroy

Before a deliberate `terraform destroy`, apply with `deletion_protection = false` first.

A deleted Cloud SQL instance name (`revel-db`) **cannot be reused for up to about a week** after deletion. Recreating the instance during that window fails; change the instance name in `database.tf` instead (the connection name, `DATABASE_URL` and `OPENFGA_DATASTORE_URI` follow it automatically).

## Initial rollout order

1. `terraform apply` (creates the services with the placeholder `hello` app image and the pinned OpenFGA image).
2. Run the OpenFGA datastore migration (OpenFGA's `/healthz` reports not serving until it has run):
   ```sh
   gcloud run jobs execute revel-openfga-migrate --region us-central1 --wait
   ```
3. Build and push the app image, then run the app migration and deploy it (below).
4. Bootstrap the OpenFGA store and model with `revel-fga-admin`, set `openfga_store_id` and `openfga_authorization_model_id`, and `terraform apply` ([OpenFGA bootstrap](#openfga-bootstrap)).
5. Add secret versions, then list those secret IDs in `app_secret_env` and `terraform apply`.
6. Set the WorkOS variables (`workos_authkit_domain`, later `workos_client_id`) and `terraform apply` ([WorkOS configuration](#workos-configuration)). Until `workos_authkit_domain` is set, `/mcp` and `/me` return 401.
7. Custom domain (below).

## Images

**Application**: the `Dockerfile` at the repository root (build context is the repo root, so `backend/` and `authorization/` are both available). It is a multi-stage uv build (`ghcr.io/astral-sh/uv:0.10.2`, `python:3.12-slim-bookworm`), `uv sync --locked --no-dev --no-editable`, runs as non-root `revel` (uid 10001) with `WORKDIR /app/backend` and the venv on `PATH`, so `uvicorn` and `alembic` resolve from there. It listens on `$PORT` (default 8080) with `--proxy-headers`. `.dockerignore` excludes every `.env`/`.env.*`, `.git`, `.claude/`, `infra/` (state, plans, tfvars) and research notes; settings come only from the environment. The image must be `linux/amd64`:

```sh
SHA=$(git rev-parse --short HEAD)
IMAGE=us-central1-docker.pkg.dev/revel-gifts/revel/revel-app:$SHA
gcloud auth configure-docker us-central1-docker.pkg.dev   # once
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .   # from the repo root
```

Local smoke test (pick a free host port):

```sh
docker buildx build --platform linux/amd64 --load -t revel-app:test .
docker run -d --rm --name revel-test -p 18080:8080 \
  -e APP_ENV=development -e PUBLIC_BASE_URL=http://localhost:8080 -e PORT=8080 revel-app:test
curl -fsS http://localhost:18080/health
npx -y @modelcontextprotocol/inspector --cli http://localhost:18080/mcp --transport http --method tools/list
docker stop revel-test
```

With `APP_ENV=production` the app refuses to start unless `PUBLIC_BASE_URL` is `https://`.

Terraform ignores image changes on `revel-app`, `revel-migrate` and `revel-fga-admin` after creation, so roll out with gcloud (other settings stay as Terraform set them):

```sh
gcloud run jobs update revel-migrate --region us-central1 --image "$IMAGE"
gcloud run jobs execute revel-migrate --region us-central1 --wait     # explicit migration step
gcloud run deploy revel-app --region us-central1 --image "$IMAGE"
gcloud run jobs update revel-fga-admin --region us-central1 --image "$IMAGE"
```

Change the migration command through the `migrate_command` variable (default `alembic upgrade head`; F3 owns the final command). Migrations never run on replica start.

**OpenFGA** uses `openfga/openfga:v1.22.1` through the `dockerhub` remote repository (`us-central1-docker.pkg.dev/revel-gifts/dockerhub/openfga/openfga:v1.22.1`). Artifact Registry pulls and caches it from Docker Hub on first use, so no manual mirror step is needed. To upgrade, change `openfga_image_tag` (keep it in step with `docker-compose.yml`), apply, then re-run `revel-openfga-migrate`.

## OpenFGA bootstrap

`revel-fga-admin` is a Cloud Run job that runs F4's OpenFGA admin CLIs in the app image:
- It runs as the `revel-app` service account, the only OpenFGA invoker.
- It uses the app's OpenFGA settings: `OPENFGA_API_URL` and `OPENFGA_GOOGLE_AUTH=true`.
- `DATABASE_URL` comes from Secret Manager over the Cloud SQL socket.
- `OPENFGA_STORE_ID` and `OPENFGA_AUTHORIZATION_MODEL_ID` are set once the variables are.

The job's command is `python`. Its default args are the read-only `--check` probe. Pass other args per execution with `--args`, which replaces the job's args for that run only and leaves the job spec unchanged. Terraform manages the default args and resets them to `--check` if they change, so don't change them with `gcloud run jobs update --args`. The job was first created with gcloud and is adopted through the `import` block in `run.tf`. The import's first apply replaces the gcloud-set bootstrap args with `--check`.

Every run writes only IDs, counts or a health word to the logs, never tokens, tuples or credentials. Run these after `revel-openfga-migrate` and with the current app image (see [Images](#images)).

1. **Bootstrap** creates or finds the store (`revel-gifts`) and writes the model when it differs. The model is `/app/authorization/model.json` in the image, which is found by default.

   ```sh
   gcloud run jobs execute revel-fga-admin --region us-central1 --wait \
     --args="-m,revel.auth.fga_bootstrap"
   ```

   stdout is just `OPENFGA_STORE_ID=…` and `OPENFGA_AUTHORIZATION_MODEL_ID=…`, and stderr notes whether each was created, written or unchanged. Read the IDs from the execution's logs:

   ```sh
   gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="revel-fga-admin"' \
     --limit 20 --format='value(textPayload)'
   ```

   Put both IDs (26-character ULIDs, not secrets) in `terraform.tfvars` as `openfga_store_id` and `openfga_authorization_model_id`. Then plan and apply after approval. This sets them on `revel-app` and `revel-fga-admin`. Re-running the bootstrap is idempotent. A new model ID appears only when `model.json` changed, and then the variable must be updated and applied again.

2. **Check** is a read-only probe and the default args:

   ```sh
   gcloud run jobs execute revel-fga-admin --region us-central1 --wait
   ```

   It prints `unconfigured` while either ID is unset and `healthy` when both exist on the server. Otherwise it exits non-zero and the execution fails.

3. **Reconcile** backfills and repairs owner and delegation tuples from PostgreSQL. It is idempotent and needs both IDs configured. Add `--prune` to delete orphan agent tuples.

   ```sh
   gcloud run jobs execute revel-fga-admin --region us-central1 --wait \
     --args="-m,revel.auth.fga_reconcile"
   ```

   It prints a count summary on stderr.

## Secrets

Terraform generates `DATABASE_URL` (`postgresql+psycopg://revel_app:…@/revel?host=/cloudsql/<connection-name>`) and `OPENFGA_DATASTORE_URI` (`postgres://openfga:…@/openfga?host=/cloudsql/<connection-name>&sslmode=disable`). It creates the other secrets without versions and never holds their values. Add a version from your local `.env` without printing it:

Pitfalls with a naive `grep | cut` pipeline:

- **Quoted values**: `KEY="abc"` would store the quotes as part of the secret.
- **Missing or empty keys**: an absent line pipes an empty string, and gcloud stores an empty secret version, which only fails later at runtime.
- **Duplicates, `\r` line endings, `export KEY=…`** lines also change what is stored.

Use this helper (bash or zsh) from the directory holding `.env`. It reads one key (the last occurrence, as dotenv does), strips one pair of surrounding quotes and a trailing `\r`, refuses missing or empty values, and never prints the value (do not run it under `set -x`):

```sh
add_secret() {  # usage: add_secret WORKOS_API_KEY
  local name="$1" line value
  line=$(grep -E "^${name}=" .env | tail -n 1)
  if [ -z "$line" ]; then echo "$name: not found in .env; skipped" >&2; return 1; fi
  value=${line#*=}
  value=${value%$'\r'}
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  if [ -z "$value" ]; then echo "$name: empty in .env; skipped" >&2; return 1; fi
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null \
    && echo "$name: version added"
}
add_secret WORKOS_API_KEY
```

Values with inline comments (`KEY=abc # note`) are not handled; keep `.env` lines to `KEY=value`.

Then add the secret ID to `app_secret_env` in `terraform.tfvars` and apply. Cloud Run refuses to deploy a revision that references a secret with no versions, so only list secrets that have one. The env var name equals the secret ID.

## WorkOS configuration

Names match `backend/src/revel/settings.py`. Non-secret values are Terraform variables (plain env vars on `revel-app`); secrets go through Secret Manager and `app_secret_env`.

| Env var on `revel-app` | Source | Default | Used by |
| --- | --- | --- | --- |
| `WORKOS_AUTHKIT_DOMAIN` | variable `workos_authkit_domain` (set only when non-empty) | unset | MCP auth: token issuer and JWKS |
| `WORKOS_TOKEN_AUDIENCE` | always `public_base_url` + `mcp_path` | `https://api.revel.gifts/mcp` | MCP auth: expected token audience (`Settings.mcp_resource_url`) and RFC 9728 resource metadata |
| `WORKOS_API_KEY` | secret, via `app_secret_env` | not wired | MCP auth: profile lookup on a user's first sign-in |
| `WORKOS_CLIENT_ID` | variable `workos_client_id` (set only when non-empty) | unset | Browser session (F2, in progress) |
| `WORKOS_REDIRECT_URI` | variable `workos_redirect_uri`; empty means `public_base_url` + `/auth/callback` | `https://api.revel.gifts/auth/callback` | Browser session (F2, in progress); register it in WorkOS |
| `WORKOS_COOKIE_PASSWORD` | secret, via `app_secret_env` | not wired | Browser session (F2, in progress) |

**`/mcp` fails closed until the AuthKit domain is set.** While `WORKOS_AUTHKIT_DOMAIN` is unset (or a placeholder), every `/mcp` and `/me` request gets 401. `WORKOS_TOKEN_AUDIENCE` is set explicitly so the audience stays `https://api.revel.gifts/mcp` even when the app is reached on its `run.app` host; it must equal the resource indicator configured in WorkOS. Changing `public_base_url` or `mcp_path` changes it too.

Set the AuthKit domain (public, from the WorkOS dashboard, e.g. `https://<tenant>.authkit.app`, no path) in `terraform.tfvars` and re-apply. Plan fails validation if `workos_authkit_domain` or `workos_client_id` still contains the example `replace-with-...` placeholder:

```hcl
workos_authkit_domain = "https://<tenant>.authkit.app"
```

```sh
terraform plan -out=rg7.tfplan    # expect revel-app to update in place with WORKOS_AUTHKIT_DOMAIN
terraform apply rg7.tfplan        # only after approval
```

Set `workos_client_id = "client_..."` the same way when the browser session needs it.

**Secrets.** Add versions first (Cloud Run rejects a revision that references a secret with no versions), then list them in `app_secret_env` and re-apply:

```sh
add_secret WORKOS_API_KEY        # helper from the Secrets section; reads .env without printing
# Cookie password: random, 32+ characters, never printed or stored locally
openssl rand -base64 48 | tr -d '\n' | gcloud secrets versions add WORKOS_COOKIE_PASSWORD --data-file=- >/dev/null \
  && echo "WORKOS_COOKIE_PASSWORD: version added"
```

```hcl
app_secret_env = ["WORKOS_API_KEY", "WORKOS_COOKIE_PASSWORD"]
```

List only secrets that have a version (for example `["WORKOS_API_KEY"]` alone for MCP auth before the browser session is ready). `openssl rand -base64 48` produces 64 characters. After rotating a secret, deploy a new revision so every instance reads the new `latest` version.

## Custom domain (api.revel.gifts)

Needs user steps; DNS is not pre-approved.

1. The account that applies Terraform verifies ownership of `revel.gifts` in [Google Search Console](https://search.google.com/search-console) (`gcloud domains verify revel.gifts` opens the flow).
2. Set `enable_domain_mapping = true`, plan, and apply after approval.
3. Create the DNS record shown in the `dns_instructions` output. For a subdomain mapping this is `api CNAME ghs.googlehosted.com.` (for `api.revel.gifts`).
4. Wait for the managed certificate (often 15 minutes to a few hours), then check:
   ```sh
   gcloud beta run domain-mappings describe --domain api.revel.gifts --region us-central1
   curl -fsS https://api.revel.gifts/health
   ```

## Health checks and allowed hosts

Cloud Run reserves some URL paths ending in `z`, and recommends avoiding all such paths ([known issues: Reserved URL paths](https://docs.cloud.google.com/run/docs/known-issues)). `/healthz` can therefore be intercepted on `*.run.app` before reaching the app. The app serves the same `200 {"status":"ok"}` on `/health` and `/healthz`; use **`/health`** for every Cloud Run check:

```sh
curl -fsS "$(terraform output -raw app_url)/health"
```

`revel-app` keeps Cloud Run's default **TCP** startup probe on port 8080 (no HTTP probe), which works for both the placeholder image and ours. If an HTTP probe is added later, point it at `/health`.

**Allowed hosts.** The MCP transport rejects (421) any `Host` other than the `PUBLIC_BASE_URL` host (`api.revel.gifts`) and `ALLOWED_HOSTS`. Before DNS exists the app is reached on `run.app`, so Terraform sets:

- `ALLOWED_HOSTS` = the deterministic URL host `revel-app-<PROJECT_NUMBER>.<REGION>.run.app` (format from [Cloud Run HTTPS URLs](https://docs.cloud.google.com/run/docs/triggering/https-request); the project number comes from the `google_project` data source; for this project `revel-app-1034867470814.us-central1.run.app`), plus any `app_extra_allowed_hosts`.
- `ALLOWED_ORIGINS` = `https://` plus each of those hosts.

The non-deterministic URL (`revel-app-<hash>-uc.a.run.app`) contains a random hash and cannot be predicted. If you use it, after the first apply:

```sh
terraform output app_urls            # lists both URLs
# add the hash host (no scheme) to app_extra_allowed_hosts in terraform.tfvars, then
terraform plan -out=rg7.tfplan && terraform apply rg7.tfplan
```

Prefer the Terraform variable over `gcloud run services update --update-env-vars`, which the next apply would revert.

## Verifying OpenFGA is not public

```sh
curl -s -o /dev/null -w '%{http_code}\n' "$(terraform output -raw openfga_url)/stores"    # expect 403
gcloud run services get-iam-policy revel-openfga --region us-central1                      # only revel-app SA
```
