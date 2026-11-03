# Frontend

The human UI: Vite + React + TypeScript, built to static files that FastAPI serves at `/app/` (`backend/src/revel/api/ui_static.py`). `GET /` redirects to `/app/`.

Use the [agreed stack](https://app.notion.com/p/4092bade7f31805191e7c4c884960908), [MVP scope](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) and [privacy rules](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31). Do not duplicate audience defaults or approval rules here. Server-side enforcement remains required regardless of UI behavior: the UI makes no authorization decisions and does no data filtering.

## Layout

- `src/api.ts` — the only HTTP client. Same-origin JSON requests with `credentials: "same-origin"`, typed `ApiError`/`UnauthorizedError`, and a 401 hook that sends the browser to `/auth/login?return_to=<current /app path>`. Every screen uses it.
- `src/App.tsx` — app shell: session bootstrap (`GET /auth/session`, with a sign-in redirect-loop guard), sign-out and navigation.
- `src/profileApi.ts`, `src/PolicyScreen.tsx`, `src/ContributionsScreen.tsx`, `src/FactItem.tsx` — the contribution policy and contributions screens over F5's `/api/ui/profile*` operations (`docs/contracts/f5-profiles.md`). Categories, labels, allowed audiences and details fields always come from the server.
- `src/connectionsApi.ts`, `src/ConnectionsScreen.tsx`, `src/AgentsSection.tsx` — handle, connection requests, tiers, blocks (F6 `/api/connections`, contract in `backend/src/revel/api/connections.py`) and connected assistants (F2 `/api/agents`, `backend/src/revel/auth/agents_router.py`). An assistant's label is its registered app name, not a verified identity.
- Server and agent strings render as React text only; lint forbids `dangerouslySetInnerHTML` and `innerHTML`.
- Browser auth routes and payloads are the F2 contract in `backend/src/revel/auth/router.py` and `backend/src/revel/auth/ui.py`.

## Commands

Requires Node 22.13+ and pnpm via corepack (the version is pinned by `packageManager` in `package.json`).

```sh
corepack enable
pnpm install --frozen-lockfile
pnpm typecheck
pnpm lint
pnpm test        # vitest + Testing Library (jsdom), fetch mocked
pnpm build       # tsc -b && vite build -> frontend/dist
```

Dependencies are pinned to exact versions (`.npmrc` sets `save-exact`); commit `pnpm-lock.yaml` with any change. Never commit `node_modules/` or `dist/`.

## Running with the backend

Browser sign-in needs WorkOS configured in the repository `.env` (see `.env.example`), with:

```sh
PUBLIC_BASE_URL=http://localhost:8000
WORKOS_REDIRECT_URI=http://localhost:8000/auth/callback
```

Without WorkOS configured the backend answers `/auth/session` with 503 and the UI shows "Sign-in is not configured".

**Full sign-in and sign-out (recommended):** use the built UI on the backend origin, so every request (including the same-origin-checked `POST /auth/logout`) comes from `http://localhost:8000`.

```sh
(cd frontend && pnpm build)          # or: pnpm exec vite build --watch
(cd backend && uv run uvicorn revel.app:app --port 8000)
open http://localhost:8000/app/
```

The backend serves `frontend/dist` by default; set `FRONTEND_DIST_DIR` to serve another build. If no build exists, `/app/` answers 503 `{"detail": "UI not built"}` and the rest of the API still works.

**Vite dev server (UI iteration only):** `pnpm dev` serves http://localhost:5173/app/ with hot reload and proxies `/auth`, `/api`, `/me` and `/health` to the backend on `:8000` (override with `REVEL_BACKEND_URL`). Sign in once through the `:8000` flow above; the session cookie is host-scoped, so proxied reads from `:5173` carry it. Writes are not supported from `:5173`: the backend's CSRF check compares `Origin` with `PUBLIC_BASE_URL`, so `POST /auth/logout` and other unsafe requests answer 403 there. Test those against the built UI on `:8000`.

In the container image the UI is built in a Node stage of the root `Dockerfile` and copied to `/app/frontend/dist` (`FRONTEND_DIST_DIR`).
