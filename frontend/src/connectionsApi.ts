/**
 * Browser calls for F6 connections (`/api/connections`, contract in the module docstring of
 * backend/src/revel/api/connections.py) and F2 connected assistants (`/api/agents`, contract in
 * backend/src/revel/auth/agents_router.py). All writes go through the same-origin client.
 * The server decides access; nothing here filters or authorizes.
 */

import { ApiError, apiRequest } from "./api";
import { fieldErrorsOf } from "./profileApi";
import type { FieldErrors } from "./profileApi";

/** The contract's tier values (F6 `TierBody`). Labels come from the server where available. */
export type Tier = "connections" | "trusted_circle";
export const TIERS: readonly Tier[] = ["connections", "trusted_circle"];

export interface HandleInfo {
  handle: string;
  discoverable: boolean;
  avatar_url: string | null;
}

export interface PublicIdentity {
  display_name: string | null;
  handle: string | null;
  avatar_url: string | null;
}

export interface Person extends PublicIdentity {
  assistant: { name: string | null; client_id: string } | null;
}

export interface RequestResult {
  target: PublicIdentity;
  request: { id: string; status: string; created_at: string };
  created: boolean;
}

export interface IncomingRequest {
  id: string;
  status: string;
  created_at: string;
  requester: Person;
}

export interface OutgoingRequest {
  id: string;
  status: string;
  created_at: string;
  target: PublicIdentity;
}

export interface ConnectionEntry {
  request_id: string;
  tier: string;
  since: string | null;
  person: Person;
}

export interface BlockedEntry {
  request_id: string;
  blocked_at: string | null;
  person: PublicIdentity;
}

export interface ResponseResult {
  request_id: string;
  status: string;
  tier: string | null;
}

export interface ConnectedAgent {
  delegation_id: string;
  agent: { name: string | null; client_id: string; label: string };
  status: string;
  connected_at: string;
  disconnected_at: string | null;
}

const BASE = "/api/connections";
const req = (id: string) => `${BASE}/requests/${encodeURIComponent(id)}`;

export const connectionsApi = {
  getHandle: () => apiRequest<HandleInfo | null>(`${BASE}/handle`),
  setHandle: (body: { handle?: string; discoverable?: boolean }) =>
    apiRequest<HandleInfo>(`${BASE}/handle`, { method: "PUT", body }),
  requestByHandle: (handle: string) =>
    apiRequest<RequestResult>(`${BASE}/requests`, { method: "POST", body: { handle } }),
  incoming: () => apiRequest<IncomingRequest[]>(`${BASE}/requests/incoming`),
  outgoing: () => apiRequest<OutgoingRequest[]>(`${BASE}/requests/outgoing`),
  connections: () => apiRequest<ConnectionEntry[]>(BASE),
  blocked: () => apiRequest<BlockedEntry[]>(`${BASE}/blocked`),
  approve: (id: string, tier: Tier) =>
    apiRequest<ResponseResult>(`${req(id)}/approve`, { method: "POST", body: { tier } }),
  setTier: (id: string, tier: Tier) =>
    apiRequest<ResponseResult>(`${req(id)}/tier`, { method: "POST", body: { tier } }),
  decline: (id: string) => apiRequest<ResponseResult>(`${req(id)}/decline`, { method: "POST" }),
  block: (id: string) => apiRequest<ResponseResult>(`${req(id)}/block`, { method: "POST" }),
  unblock: (id: string) => apiRequest<ResponseResult>(`${req(id)}/unblock`, { method: "POST" }),
  cancel: (id: string) => apiRequest<ResponseResult>(`${req(id)}/cancel`, { method: "POST" }),
  revoke: (id: string) => apiRequest<ResponseResult>(`${req(id)}/revoke`, { method: "POST" }),
};

export const agentsApi = {
  list: () => apiRequest<ConnectedAgent[]>("/api/agents"),
  disconnect: (delegationId: string) =>
    apiRequest<ConnectedAgent>(`/api/agents/${encodeURIComponent(delegationId)}/disconnect`, {
      method: "POST",
    }),
};

// --- errors ------------------------------------------------------------------------------

export const RATE_LIMITED = "Too many requests, try again shortly.";
export const TEMPORARILY_UNAVAILABLE =
  "This is temporarily unavailable; nothing was changed. Please try again.";
export const GENERIC = "Something went wrong. Please try again.";

/** The F6 error code (`{detail, code}`), if any. */
export function errorCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.body || typeof error.body !== "object") return null;
  const code = (error.body as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

export function isStatus(error: unknown, status: number): boolean {
  return error instanceof ApiError && error.status === status;
}

/**
 * A user-facing message. 429 and 503 use fixed text; other errors show the server's fixed
 * `detail` string (e.g. the deliberately identical target-not-found message), never raw bodies.
 */
export function messageFor(error: unknown): string {
  if (!(error instanceof ApiError)) return GENERIC;
  if (error.status === 429) return RATE_LIMITED;
  if (error.status === 503) return TEMPORARILY_UNAVAILABLE;
  return typeof error.detail === "string" ? error.detail : GENERIC;
}

/**
 * 422 errors as field errors: F6's coded 422 (`invalid_handle`, ...) maps to `field`;
 * FastAPI's own 422 maps by `loc`.
 */
export function fieldErrorsFor(error: unknown, field: string): FieldErrors | null {
  if (!isStatus(error, 422)) return null;
  const code = errorCode(error);
  if (code && error instanceof ApiError && typeof error.detail === "string") {
    return { [field]: error.detail };
  }
  return fieldErrorsOf(error);
}
