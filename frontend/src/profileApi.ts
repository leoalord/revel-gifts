/**
 * Browser calls for F5's profile and contribution operations (#9, RG-18; contract
 * "F5 profiles and contributions: operation contract v1", provisional, with F8 amendments).
 *
 * Category ids, labels, allowed audiences and details schemas always come from the server.
 * The only values typed here are the contract's own enums (status, evidence, precision).
 */

import { ApiError, apiRequest } from "./api";

export type Audience = string; // server-defined ids, e.g. "only_me"; never enumerated here
export type ClaimStatus = "active" | "provisional" | "pending_review" | "superseded" | "removed";
export type ObservedPrecision = "day" | "month" | "year" | "unknown";

export const CLAIM_STATUSES: readonly ClaimStatus[] = [
  "active",
  "provisional",
  "pending_review",
  "superseded",
  "removed",
];
export const OBSERVED_PRECISIONS: readonly ObservedPrecision[] = ["day", "month", "year", "unknown"];

/**
 * The contract's Public audience id. Used only to follow server signals: Public is offered when
 * a category's `allowed_audiences` includes it, and previewed for unsaved opt-in changes.
 */
export const PUBLIC_AUDIENCE: Audience = "public";

export interface DetailsPropertySchema {
  type: "integer" | "string";
  minimum?: number;
  maximum?: number;
  maxLength?: number;
}

export interface DetailsSchema {
  type: "object";
  properties: Record<string, DetailsPropertySchema>;
  required?: string[];
  minProperties?: number;
  additionalProperties?: boolean;
}

export interface CategorySetting {
  audience?: Audience | null;
  require_review?: boolean | null;
}

export interface Policy {
  version: number;
  approved_at: string;
  public_opt_in: boolean;
  share_provisional: boolean;
  require_review_all: boolean;
  enrichment_enabled: boolean;
  category_settings: Record<string, CategorySetting>;
}

export interface Category {
  id: string;
  label: string;
  default_audience: Audience;
  public_eligible: boolean;
  effective_audience: Audience;
  allowed_audiences: Audience[];
  require_review: boolean;
  details_schema: DetailsSchema | null;
}

export interface PolicyFlags {
  public_opt_in: boolean;
  share_provisional: boolean;
  require_review_all: boolean;
  enrichment_enabled: boolean;
}

export interface AudienceInfo {
  id: Audience;
  label: string;
}

export interface PolicyResponse {
  policy: Policy | null;
  categories: Category[];
  /** Server-provided caveat for provisional sharing (present in F5's draft implementation). */
  provisional_caveat?: string;
  /** Flag defaults while `policy` is null (agreed F5 follow-up; optional until it lands). */
  defaults?: PolicyFlags | null;
  /** Audience ids with display labels (agreed F5 follow-up; optional until it lands). */
  audiences?: AudienceInfo[];
}

export interface PolicyUpdate {
  expected_version: number | null;
  public_opt_in: boolean;
  share_provisional: boolean;
  require_review_all: boolean;
  enrichment_enabled: boolean;
  category_settings?: Record<string, CategorySetting>;
}

export interface Fact {
  id: string;
  category: string;
  statement: string;
  details: Record<string, unknown> | null;
  audience: Audience;
  status: ClaimStatus;
  provisional: boolean;
  evidence_type: string;
  confidence: string;
  observed_at: string | null;
  observed_precision: ObservedPrecision;
  contributed_via: string;
  contributor: string;
  supersedes_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface RecentEvent {
  claim_id: string;
  action: string;
  category: string | null;
  audience: Audience | null;
  acceptance: string | null;
  actor: string;
  occurred_at: string;
}

export interface Summary {
  since: string;
  counts: { by_status: Record<string, number>; by_audience: Record<string, number> };
  pending_review: number;
  recent: RecentEvent[];
}

export interface Profile {
  profile_id: string | null;
  policy_version: number | null;
  policy: Policy | null;
  defaults?: PolicyFlags | null;
  facts: Fact[];
  summary: Summary;
}

export interface FactPatch {
  audience?: Audience;
  statement?: string;
  details?: Record<string, unknown> | null;
  observed_at?: string | null;
  observed_precision?: ObservedPrecision;
}

const BASE = "/api/ui/profile";

export function getPolicy(signal?: AbortSignal): Promise<PolicyResponse> {
  return apiRequest<PolicyResponse>(`${BASE}/policy`, { signal });
}

export function putPolicy(update: PolicyUpdate): Promise<PolicyResponse> {
  return apiRequest<PolicyResponse>(`${BASE}/policy`, { method: "PUT", body: update });
}

export function getProfile(statuses: readonly ClaimStatus[] = [], signal?: AbortSignal) {
  const query = new URLSearchParams();
  for (const status of statuses) query.append("status", status);
  const qs = query.toString();
  return apiRequest<Profile>(qs ? `${BASE}?${qs}` : BASE, { signal });
}

const factPath = (id: string) => `${BASE}/facts/${encodeURIComponent(id)}`;

export function patchFact(id: string, patch: FactPatch): Promise<Fact> {
  return apiRequest<Fact>(factPath(id), { method: "PATCH", body: patch });
}

export function removeFact(id: string): Promise<Fact> {
  return apiRequest<Fact>(factPath(id), { method: "DELETE" });
}

export function approveFact(id: string): Promise<Fact> {
  return apiRequest<Fact>(`${factPath(id)}/approve`, { method: "POST" });
}

export function rejectFact(id: string): Promise<Fact> {
  return apiRequest<Fact>(`${factPath(id)}/reject`, { method: "POST" });
}

// --- error helpers ---------------------------------------------------------------------

export const GENERIC_ERROR = "Something went wrong. Please try again.";
export const FORBIDDEN_ERROR = "You are not allowed to do that.";
export const UNAVAILABLE_ERROR = "The service is temporarily unavailable. Please try again.";

/** Field errors keyed by dotted location without the leading "body" (e.g. "statement"). */
export type FieldErrors = Record<string, string>;

/** 400 (contract) or 422 (FastAPI request validation) field errors, else null. */
export function fieldErrorsOf(error: unknown): FieldErrors | null {
  if (!(error instanceof ApiError) || (error.status !== 400 && error.status !== 422)) {
    return null;
  }
  const errors: FieldErrors = {};
  if (Array.isArray(error.detail)) {
    for (const item of error.detail as unknown[]) {
      if (!item || typeof item !== "object") continue;
      const { loc, msg } = item as { loc?: unknown; msg?: unknown };
      const parts = Array.isArray(loc) ? loc.map(String) : [];
      if (parts[0] === "body") parts.shift();
      const key = parts.join(".") || "_";
      if (typeof msg === "string" && !(key in errors)) errors[key] = msg;
    }
  } else if (typeof error.detail === "string") {
    errors._ = error.detail;
  }
  if (Object.keys(errors).length === 0) errors._ = "Please check the form and try again.";
  return errors;
}

/** A user-facing message for non-field errors. Never echoes raw server bodies. */
export function messageOf(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return FORBIDDEN_ERROR;
    if (error.status === 503) return UNAVAILABLE_ERROR;
  }
  return GENERIC_ERROR;
}

function objectBody(error: ApiError): Record<string, unknown> | null {
  return error.body && typeof error.body === "object"
    ? (error.body as Record<string, unknown>)
    : null;
}

/** The current fact carried by a fact-level 409, if any. */
export function conflictFact(error: unknown): Fact | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const fact = objectBody(error)?.fact;
  return fact && typeof fact === "object" ? (fact as Fact) : null;
}

export type PolicyConflict = Partial<PolicyResponse> & { policy: Policy | null };

/** The current policy response carried by a policy 409 (categories etc. when sent). */
export function conflictPolicy(error: unknown): PolicyConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const body = objectBody(error);
  if (!body || !("policy" in body)) return null;
  const result: PolicyConflict = { policy: (body.policy as Policy | null) ?? null };
  if (Array.isArray(body.categories)) result.categories = body.categories as Category[];
  if (Array.isArray(body.audiences)) result.audiences = body.audiences as AudienceInfo[];
  if (typeof body.provisional_caveat === "string") {
    result.provisional_caveat = body.provisional_caveat;
  }
  if (body.defaults && typeof body.defaults === "object") {
    result.defaults = body.defaults as PolicyFlags;
  }
  return result;
}
