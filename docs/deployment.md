# Deployment implementation guidance

[Notion](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) owns architecture and hosting decisions; the [MVP page](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns acceptance. This file covers implementation considerations, not additional product scope. Nothing here establishes a verified integration or deployed service.

## Implementation considerations

- Pin and test dependency versions. Follow the pinned MCP SDK's mounting/lifespan guidance; configure public endpoint paths, OAuth discovery, host allowlists and browser origins at the deployed origin.
- Test the actual clients' protocol behavior across replicas and restarts. Avoid correctness depending on best-effort instance affinity. Persist durable business state outside application processes; follow SDK guidance for any cross-worker protocol state.
- Distinguish external user/agent authentication from infrastructure/service identity. Protect both transport entry points; validate credentials intended for this service. Do not use transport session IDs as identity or forward incoming credentials to unrelated services.
- Keep internal authorization services inaccessible to external assistants. Use least-privilege runtime identities, managed secrets and logs that exclude credentials/private payloads.
- Coordinate instance limits, database pools, timeouts and request limits. Measure before choosing sizing or minimum-instance settings. Apply migrations explicitly rather than on every replica startup.
- Include reconnect/retry, duplicate-write prevention, denied-access and revocation checks in feature acceptance. Use Notion's question lifecycle and contribution rules rather than inventing deployment-specific policy.

Record concrete commands, configuration and observed results here as the deployment is implemented. Recheck documentation against the versions being used.

## Concrete commands (development environment)

Terraform lives in `infra/terraform/`. [`infra/README.md`](../infra/README.md) is the runbook for plan/apply, state handling, image build/push, secrets and the domain. Summary, for project `revel-gifts` in `us-central1`:

- Plan/apply: `terraform plan -out=rg7.tfplan`, then `terraform apply rg7.tfplan` after approval. State is local and contains generated DB credentials; never commit it.
- Explicit migrations (Cloud Run jobs, never on replica start): `gcloud run jobs execute revel-openfga-migrate --region us-central1 --wait` and `gcloud run jobs execute revel-migrate --region us-central1 --wait`.
- Image: root `Dockerfile` (multi-stage uv, Python 3.12 slim, non-root, no `.env` in the image; `.dockerignore` enforces it). Build from the repo root: `docker buildx build --platform linux/amd64 -t "$IMAGE" --push .`.
- Health: use `/health` for Cloud Run checks; Cloud Run reserves some paths ending in `z`, so `/healthz` may not reach the app on `run.app` ([known issues](https://docs.cloud.google.com/run/docs/known-issues)). `revel-app` uses the default TCP startup probe.
- Hosts: Terraform sets `ALLOWED_HOSTS`/`ALLOWED_ORIGINS` to the deterministic `revel-app-<PROJECT_NUMBER>.<REGION>.run.app` host so the app works before DNS; the hash URL needs `app_extra_allowed_hosts` after the first apply.
- App rollout: build `linux/amd64` and push to `us-central1-docker.pkg.dev/revel-gifts/revel/revel-app:<sha>`, `gcloud run jobs update revel-migrate --image …`, run the migration job, then `gcloud run deploy revel-app --image …`.
- App runtime configuration: `APP_ENV=production`, `PUBLIC_BASE_URL` (default `https://api.revel.gifts`), `MCP_PATH=/mcp`, `OPENFGA_API_URL` (OpenFGA's canonical `run.app` URL, also the ID-token audience), `OPENFGA_GOOGLE_AUTH=true`, `DATABASE_URL` from Secret Manager (unix socket under `/cloudsql`), plus opted-in secrets listed in `app_secret_env`.
- OpenFGA is IAM-authenticated: only the `revel-app` service account may invoke it, and unauthenticated requests get 403.
- No deployment has been applied or verified by this document.

## Primary references

- [Google: host MCP servers on Cloud Run](https://docs.cloud.google.com/run/docs/host-mcp-servers)
- [Google deployment codelab](https://codelabs.developers.google.com/codelabs/cloud-run/how-to-deploy-a-secure-mcp-server-on-cloud-run)
- [Official Python SDK: ASGI mounting](https://py.sdk.modelcontextprotocol.io/run/asgi/)
- [Official Python SDK: deployment and scaling](https://py.sdk.modelcontextprotocol.io/run/deploy/)
- [MCP security guidance](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices)
- [Anthropic: MCP design patterns](https://claude.com/resources/articles/building-agents-that-reach-production-systems-with-mcp)
