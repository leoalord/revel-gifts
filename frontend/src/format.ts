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
