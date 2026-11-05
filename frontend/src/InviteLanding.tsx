import { useEffect, useState } from "react";
import { ApiError, browserNavigation, loginUrl, postLogout } from "./api";
import type { WhoAmI } from "./api";
import { clearInvitation, pendingInvitation } from "./inviteContext";
import { messageFor } from "./connectionsApi";
import { formatTime } from "./format";

interface Preview { sender_name: string | null; expires_at: string; status: string; sharing_notice: string }

// This public landing never redirects automatically: the person chooses to sign in.
export function InviteLanding() {
  const [session, setSession] = useState<WhoAmI | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [token] = useState(pendingInvitation);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const response = await fetch("/auth/session", { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (response.status === 401) return;
        if (!response.ok) throw new Error("Sign-in is temporarily unavailable.");
        const who = await response.json() as WhoAmI;
        if (!active) return;
        setSession(who);
        if (!token) return;
        const result = await invitePost<Preview>("preview", { token });
        if (active) setPreview(result);
      } catch (e) { if (active) setError(e instanceof Error ? e.message : "Unable to open this invitation."); }
      finally { if (active) setLoading(false); }
    }
    void load();
    return () => { active = false; };
  }, [token]);

  async function respond(action: "accept" | "decline") {
    setBusy(true); setError(null);
    try {
      await invitePost("respond", { token, action });
      clearInvitation();
      setOutcome(action === "accept" ? "Invitation accepted. Your connection request is ready to review in Connections. No profile access has been granted." : "Invitation declined. No profile access has been granted.");
      setPreview(null);
    } catch (e) { setError(messageFor(e)); } finally { setBusy(false); }
  }

  async function signOut() {
    setBusy(true); setError(null);
    try {
      const { logout_url } = await postLogout();
      if (logout_url) browserNavigation.assign(logout_url);
      else { setSession(null); setPreview(null); }
    } catch (e) { setError(messageFor(e)); } finally { setBusy(false); }
  }

  return <div className="shell"><header className="app-header"><h1 className="brand">Revel Gifts</h1></header><main id="main-content">
    <section className="panel" aria-labelledby="invite-title"><p className="eyebrow">A more thoughtful way to give</p><h2 id="invite-title">You’re invited to Revel.</h2>
      <p>Build a useful gifting profile with the assistants that already know you. You choose what they can add and who can see it.</p>
      {!token && !outcome && <p role="alert">Open the complete invitation link from the person who invited you.</p>}
      {loading ? <p role="status">Checking sign-in…</p> : <>
        {session && <p>Signed in as {session.user.email ?? session.user.display_name ?? "your Revel account"}.</p>}
        {!session && token && <><p>Sign in or create an account through WorkOS using the invited email, then explicitly accept or decline.</p><button type="button" onClick={() => browserNavigation.assign(loginUrl("/app/invite"))}>Continue with WorkOS</button></>}
        {error && <p role="alert">{error}</p>}
        {session && error && <><p>If this is a different account, sign out and reopen your invitation link with the invited email.</p><button type="button" disabled={busy} onClick={() => void signOut()}>Sign out to change account</button></>}
        {preview && <><p>{preview.sender_name || "A Revel member"} invited you. Expires {formatTime(preview.expires_at)}.</p><p>{preview.sharing_notice}</p><div className="actions"><button type="button" disabled={busy} onClick={() => void respond("accept")}>Accept invitation</button><button type="button" disabled={busy} onClick={() => void respond("decline")}>Decline invitation</button></div></>}
        {outcome && <p role="status">{outcome}</p>}
      </>}
      <p><a href="/app/">Continue to your Revel account</a> · <a href="/app/connections">Review connections and sharing</a></p>
    </section>
    <section className="panel"><h3>Set up your assistant</h3><p>Connect to <code>https://api.revel.gifts/mcp</code> through a supported assistant. Muse and Claude have verified Revel prompt/resource guidance; Dots and Grok Bot have verified MCP connection evidence. Scheduling support needs a separate check in your host.</p><p>Use the same verified WorkOS account in your browser and assistant. Return here to finish accepting your invitation. Keep the invitation link out of assistant prompts.</p><p><a href="/guidance/revel-get-started.md">Read Revel’s setup guidance</a> · <a href="https://revel-gifts-public.web.app/#connect" rel="noreferrer">Assistant connection instructions</a></p><p>You can set up an assistant later. Your browser account remains usable.</p></section>
  </main></div>;
}

async function invitePost<T>(operation: string, body: unknown): Promise<T> {
  const response = await fetch(`/api/invitations/${operation}`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) });
  const result: unknown = await response.json();
  if (!response.ok) throw new ApiError(response.status, result && typeof result === "object" && "detail" in result ? result.detail : "Unable to use this invitation.", result);
  return result as T;
}
