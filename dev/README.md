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

## Demo reset

`python -m revel.demo.reset` (F10, Build Tasks #23) restores the three demo personas (`leo_walker`, `sam_demo`, `jordan_demo`) to the recording baseline. It is a dry run by default: it prints planned counts, request ids, the seeds and the scope-check result, and writes nothing.

```sh
# Dev, through the existing job (DATABASE_URL, PUBLIC_BASE_URL and OpenFGA come from the job):
gcloud run jobs execute revel-fga-admin --region us-central1 --wait \
  --args="-m,revel.demo.reset,--target,dev"                                   # dry run
gcloud run jobs execute revel-fga-admin --region us-central1 --wait \
  --args="-m,revel.demo.reset,--target,dev,--apply,--no-active-hold"          # apply
```

- `--target dev` must match `PUBLIC_BASE_URL` (`https://api.revel.gifts`). `--apply` also needs `--no-active-hold`: run it only when no verification hold is announced, because it changes shared test state.
- It refuses (exit 2, nothing written; the dry run exits 2 too) in these cases: a handle is missing, a handle resolves to a user other than the pinned id in `revel/demo/seed.py`, the Jordan→Leo Trusted Circle baseline is missing or at the wrong tier, or any row in scope names a user outside the three. Connection requests that involve anyone else are left alone and counted.
- With `--apply` it deletes the demo questions, the demo profiles' facts and every contribution policy version except the earliest. It clears requests among the demo users through the connections service, keeping Jordan's Trusted Circle access to Leo's profile (the baseline is never created). It then runs `fga_reconcile` (window 0, no prune), re-seeds six synthetic "prepared" facts through `add_facts`, and checks access: Sam is denied, and Jordan sees the Trusted Circle fact. Users, agents, delegations, delegation sessions, handles and profiles are never touched.
- Output carries counts and ids only. The JSON block between `BEGIN/END DEMO DISCLOSURE MANIFEST` lists each prepared fact's seed key, claim id and audience for #23. Facts Muse contributes during the recording are live.
- Exit codes: 0 success, 1 a phase or check failed (a database error prints only the phase and exception class, and that phase's transaction rolls back), 2 refused (nothing written).
- Tests: `REVEL_TEST_DATABASE_URL=… uv run pytest tests/demo` (Postgres from this compose file; OpenFGA is faked).
