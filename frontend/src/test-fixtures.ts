/** Typed fixtures and a fetch router for screen tests (synthetic data only). */

import { vi } from "vitest";
import type { Mock } from "vitest";

import type { Category, Fact, Policy, PolicyResponse, Profile } from "./profileApi";

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export const CATEGORIES: Category[] = [
  {
    id: "interest",
    label: "Interests and hobbies",
    default_audience: "connections",
    public_eligible: true,
    effective_audience: "connections",
    allowed_audiences: ["only_me", "trusted_circle", "connections"],
    require_review: false,
    details_schema: null,
  },
  {
    id: "size",
    label: "Sizes",
    default_audience: "trusted_circle",
    public_eligible: false,
    effective_audience: "trusted_circle",
    allowed_audiences: ["only_me", "trusted_circle"],
    require_review: false,
    details_schema: {
      type: "object",
      properties: {
        item: { type: "string", maxLength: 100 },
        size: { type: "string", maxLength: 100 },
      },
      required: ["item", "size"],
      additionalProperties: false,
    },
  },
  {
    id: "sensitive",
    label: "Sensitive (volunteered)",
    default_audience: "only_me",
    public_eligible: false,
    effective_audience: "only_me",
    allowed_audiences: ["only_me"],
    require_review: true,
    details_schema: null,
  },
];

export const POLICY_V3: Policy = {
  version: 3,
  approved_at: "2026-11-01T10:00:00Z",
  public_opt_in: false,
  share_provisional: true,
  require_review_all: false,
  enrichment_enabled: true,
  category_settings: { interest: { audience: "trusted_circle" } },
};

export const CAVEAT = "Provisional facts are shared with a caveat (server text).";

export function policyResponse(policy: Policy | null, categories = CATEGORIES): PolicyResponse {
  return { policy, categories, provisional_caveat: CAVEAT };
}

export function fact(overrides: Partial<Fact> = {}): Fact {
  return {
    id: "f-1",
    category: "interest",
    statement: "Enjoys bouldering",
    details: null,
    audience: "connections",
    status: "active",
    provisional: false,
    evidence_type: "explicit",
    confidence: "high",
    observed_at: "2026-10-20",
    observed_precision: "day",
    contributed_via: "mcp",
    contributor: "Muse",
    supersedes_id: null,
    created_at: "2026-10-20T12:00:00Z",
    updated_at: "2026-10-20T12:00:00Z",
    ...overrides,
  };
}

export function profile(facts: Fact[], overrides: Partial<Profile> = {}): Profile {
  return {
    profile_id: "p-1",
    policy_version: 3,
    policy: POLICY_V3,
    facts,
    summary: {
      since: "2026-10-26T00:00:00Z",
      counts: { by_status: { active: 2, pending_review: 1 }, by_audience: { connections: 3 } },
      pending_review: 1,
      recent: [
        {
          claim_id: "f-1",
          action: "added",
          category: "interest",
          audience: "connections",
          acceptance: "automatic",
          actor: "Muse",
          occurred_at: "2026-10-20T12:00:00Z",
        },
      ],
    },
    ...overrides,
  };
}

export interface Call {
  method: string;
  url: string;
  body: unknown;
}

type Handler = (call: Call) => Response | Promise<Response>;

/** Route fetch calls by "METHOD /path" (query string ignored for matching). */
export function mockRoutes(routes: Record<string, Handler>): { fetch: Mock<typeof fetch>; calls: Call[] } {
  const calls: Call[] = [];
  const mock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    const call = { method, url, body };
    calls.push(call);
    const handler = routes[`${method} ${url.split("?")[0]}`];
    if (!handler) return jsonResponse(500, { detail: `no mock for ${method} ${url}` });
    return handler(call);
  });
  vi.stubGlobal("fetch", mock);
  return { fetch: mock, calls };
}
