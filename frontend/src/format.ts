/** Presentation helpers. They format server values; they never decide access or visibility. */

import type { AudienceInfo, ObservedPrecision } from "./profileApi";

export type AudienceLabel = (id: string) => string;

/** Server audience labels when the policy response carries them, else id-derived labels. */
export function audienceLabeler(audiences: readonly AudienceInfo[] | undefined): AudienceLabel {
  const labels = new Map((audiences ?? []).map((a) => [a.id, a.label]));
  return (id) => labels.get(id) ?? humanize(id);
}

/** "trusted_circle" -> "Trusted circle". Used for server ids that have no server label. */
export function humanize(id: string): string {
  const text = id.replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : id;
}

export function formatObserved(observedAt: string | null, precision: ObservedPrecision): string {
  if (!observedAt || precision === "unknown") return "Date unknown";
  if (precision === "year") return observedAt.slice(0, 4);
  if (precision === "month") return observedAt.slice(0, 7);
  return observedAt.slice(0, 10);
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/**
 * Compact local time for scannable lists, e.g. "Nov 2, 7:18 PM" (the year is added when it is
 * not the current one). Use `formatTime` for the full value, e.g. in a `title`.
 */
export function formatShortTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const options: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  if (date.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
  return date.toLocaleString(undefined, options);
}

/**
 * F5 labels actors "you", "someone else" or "Your assistant" (no registered name); any other
 * actor/contributor string is the agent app's self-registered name, not a verified identity.
 */
const UNNAMED_ACTORS = new Set(["you", "someone else", "Your assistant"]);

export function isRegisteredAppName(name: string): boolean {
  return !UNNAMED_ACTORS.has(name);
}
