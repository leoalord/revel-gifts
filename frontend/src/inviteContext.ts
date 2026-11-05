// Capabilities stay browser-local. Never put an invitation token in a login query or prompt.
const KEY = "revel.pendingInvitation";
let memoryToken: string | null = null;

export function captureInvitation(): void {
  if (window.location.pathname.replace(/\/$/, "") !== "/app/invite") return;
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get("token");
  if (token) {
    memoryToken = token;
    try { sessionStorage.setItem(KEY, token); } catch { /* Same-page fallback remains available. */ }
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

export function pendingInvitation(): string | null {
  try { return sessionStorage.getItem(KEY) ?? memoryToken; } catch { return memoryToken; }
}

export function clearInvitation(): void {
  memoryToken = null;
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}
