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
| `secrets.tf` | `DATABASE_URL`, `OPENFGA_DATASTORE_URI` (generated) and empty placeholders `WORKOS_API_KEY`, `WORKOS_CLIENT_ID`, `WORKOS_COOKIE_PASSWORD`, `FIREWORKS_API_KEY`, `META_API_KEY`; per-secret accessor grants |
| `run.tf` | Cloud Run services `revel-app`, `revel-openfga`; jobs `revel-migrate`, `revel-openfga-migrate`; optional domain mapping |
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

Before a deliberate `terraform destroy`, apply with `deletion_protection = false` first.

## Initial rollout order

1. `terraform apply` (creates the services with the placeholder `hello` app image and the pinned OpenFGA image).
2. Run the OpenFGA datastore migration (OpenFGA's `/healthz` reports not serving until it has run):
   ```sh
   gcloud run jobs execute revel-openfga-migrate --region us-central1 --wait
   ```
3. Build and push the app image, then run the app migration and deploy it (below).
4. Add secret versions, then list those secret IDs in `app_secret_env` and `terraform apply`.
5. Custom domain (below).

## Images

**Application** (the Dockerfile is a later step; the image must be `linux/amd64`):

```sh
SHA=$(git rev-parse --short HEAD)
IMAGE=us-central1-docker.pkg.dev/revel-gifts/revel/revel-app:$SHA
gcloud auth configure-docker us-central1-docker.pkg.dev   # once
docker buildx build --platform linux/amd64 -t "$IMAGE" --push backend
```

Terraform ignores image changes on `revel-app` and `revel-migrate` after creation, so roll out with gcloud (other settings stay as Terraform set them):

```sh
gcloud run jobs update revel-migrate --region us-central1 --image "$IMAGE"
gcloud run jobs execute revel-migrate --region us-central1 --wait     # explicit migration step
gcloud run deploy revel-app --region us-central1 --image "$IMAGE"
```

Change the migration command through the `migrate_command` variable (default `alembic upgrade head`; F3 owns the final command). Migrations never run on replica start.

**OpenFGA** uses `openfga/openfga:v1.22.1` through the `dockerhub` remote repository (`us-central1-docker.pkg.dev/revel-gifts/dockerhub/openfga/openfga:v1.22.1`). Artifact Registry pulls and caches it from Docker Hub on first use, so no manual mirror step is needed. To upgrade, change `openfga_image_tag` (keep it in step with `docker-compose.yml`), apply, then re-run `revel-openfga-migrate`.

## Secrets

Terraform generates `DATABASE_URL` (`postgresql+psycopg://revel_app:…@/revel?host=/cloudsql/<connection-name>`) and `OPENFGA_DATASTORE_URI` (`postgres://openfga:…@/openfga?host=/cloudsql/<connection-name>&sslmode=disable`). It creates the other secrets without versions and never holds their values. Add a version from your local `.env` without printing it:

```sh
# Example for one key; repeat per secret. Reads only that line; nothing is echoed.
grep '^WORKOS_API_KEY=' .env | cut -d= -f2- | tr -d '\n' \
  | gcloud secrets versions add WORKOS_API_KEY --data-file=-
```

Then add the secret ID to `app_secret_env` in `terraform.tfvars` and apply. Cloud Run refuses to deploy a revision that references a secret with no versions, so only list secrets that have one. The env var name equals the secret ID.

## Custom domain (api.revel.gifts)

Needs user steps; DNS is not pre-approved.

1. The account that applies Terraform verifies ownership of `revel.gifts` in [Google Search Console](https://search.google.com/search-console) (`gcloud domains verify revel.gifts` opens the flow).
2. Set `enable_domain_mapping = true`, plan, and apply after approval.
3. Create the DNS record shown in the `dns_instructions` output. For a subdomain mapping this is `api CNAME ghs.googlehosted.com.` (for `api.revel.gifts`).
4. Wait for the managed certificate (often 15 minutes to a few hours), then check:
   ```sh
   gcloud beta run domain-mappings describe --domain api.revel.gifts --region us-central1
   curl -fsS https://api.revel.gifts/healthz
   ```

## Verifying OpenFGA is not public

```sh
curl -s -o /dev/null -w '%{http_code}\n' "$(terraform output -raw openfga_url)/healthz"   # expect 403
gcloud run services get-iam-policy revel-openfga --region us-central1                      # only revel-app SA
```
