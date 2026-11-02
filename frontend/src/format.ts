/** Presentation helpers. They format server values; they never decide access or visibility. */

import type { ObservedPrecision } from "./profileApi";

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
