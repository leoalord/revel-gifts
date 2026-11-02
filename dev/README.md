# Local development

Local service configuration and synthetic fixtures belong here.

## Services

From the repository root:

```sh
cp .env.example .env          # placeholders only; never commit .env
docker compose up -d          # Postgres 16 (5432) and OpenFGA (8080 HTTP, 8081 gRPC)
curl localhost:8080/healthz   # {"status":"SERVING"}
docker compose down           # stop (add -v to delete the database volume)
```

Postgres (`revel`/`revel`) hosts the application database `revel` and a separate `openfga`
database, created by `postgres-init/` on first volume initialization. A one-shot
`openfga-migrate` service migrates it before OpenFGA starts. If a host port is taken, override
it, e.g. `POSTGRES_HOST_PORT=55432 docker compose up -d` (and adjust `DATABASE_URL`).

The [MVP page](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns the test network and demonstration requirements. Keep fixtures aligned with it; prepared data must not replace real authorization checks. Exclude real secrets and private user context from fixtures.
