/**
 * The one HTTP client every screen uses to talk to the Revel backend.
 *
 * - Same-origin only: requests use relative paths and `credentials: "same-origin"`, so the
 *   F2 session cookie is sent and nothing is ever sent cross-origin.
 * - JSON in, JSON out. Non-2xx responses throw `ApiError` (with the server's `detail`).
 * - 401 means "not signed in": the unauthorized handler runs (default: navigate to
 *   `/auth/login?return_to=<current /app path>`) and `UnauthorizedError` is thrown.
 *
 * The server is authoritative for authentication and authorization; this module performs no
 * client-side access decisions or data filtering.
 */

/** `GET /auth/session` payload (backend `revel.services.identity.WhoAmI`, owned by F2). */
export interface WhoAmIUser {
  id: string;
  workos_user_id: string;
  email: string | null;
  display_name: string | null;
}

export interface WhoAmIAgent {
  agent_id: string;
  delegation_id: string;
  client_id: string;
  name: string | null;
}

export interface WhoAmI {
  user: WhoAmIUser;
  agent: WhoAmIAgent | null;
  via: "mcp" | "api" | "ui";
}

/** `POST /auth/logout` payload. */
export interface LogoutResult {
  logout_url: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Thrown after a 401; the unauthorized handler has already been invoked. */
export class UnauthorizedError extends ApiError {
  constructor(detail: unknown) {
    super(401, detail);
    this.name = "UnauthorizedError";
  }
}

const APP_BASE = "/app/";

/** Current location as a same-origin `/app/...` path (plus query) for `return_to`. */
export function currentAppPath(): string {
  const { pathname, search } = window.location;
  if (pathname === "/app" || pathname.startsWith(APP_BASE)) {
    return pathname + search;
  }
  return APP_BASE;
}

export function loginUrl(returnTo: string = currentAppPath()): string {
  return `/auth/login?return_to=${encodeURIComponent(returnTo)}`;
}

/** Browser navigation, behind an object so tests can observe it (jsdom cannot). */
export const browserNavigation = {
  assign(url: string): void {
    window.location.assign(url);
  },
  reload(): void {
    window.location.reload();
  },
};

export function redirectToLogin(): void {
  browserNavigation.assign(loginUrl());
}

type UnauthorizedHandler = () => void;
let unauthorizedHandler: UnauthorizedHandler = redirectToLogin;

/** Replace the 401 hook (tests, or a future "session expired" prompt). Returns the previous. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler): UnauthorizedHandler {
  const previous = unauthorizedHandler;
  unauthorizedHandler = handler;
  return previous;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function detailOf(body: unknown): unknown {
  if (body && typeof body === "object" && "detail" in body) {
    return (body as { detail: unknown }).detail;
  }
  return body;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("apiRequest only accepts same-origin absolute paths");
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = {
    method: options.method ?? "GET",
    credentials: "same-origin",
    headers,
    signal: options.signal,
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }

  const response = await fetch(path, init);
  const body = await readBody(response);
  if (response.status === 401) {
    unauthorizedHandler();
    throw new UnauthorizedError(detailOf(body));
  }
  if (!response.ok) {
    throw new ApiError(response.status, detailOf(body));
  }
  return body as T;
}

export function getSession(signal?: AbortSignal): Promise<WhoAmI> {
  return apiRequest<WhoAmI>("/auth/session", { signal });
}

export function postLogout(): Promise<LogoutResult> {
  return apiRequest<LogoutResult>("/auth/logout", { method: "POST" });
}
