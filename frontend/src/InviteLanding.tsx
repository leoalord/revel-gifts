import { useEffect, useState } from "react";
import { ApiError, browserNavigation, loginUrl, postLogout } from "./api";
import type { WhoAmI } from "./api";
import { clearInvitation, pendingInvitation } from "./inviteContext";
import { messageFor } from "./connectionsApi";
import { AssistantSetup } from "./AssistantSetup";

interface Preview { sender_name: string | null; status: string }
const JOIN_INTENT = "revel.invitationJoinIntent";

// A page view never joins. Only the primary action (including its auth return) does.
export function InviteLanding() {
  const [session, setSession] = useState<WhoAmI | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
        if (!active) return;
        setPreview(result);
        let intended = false;
        try { intended = sessionStorage.getItem(JOIN_INTENT) === token; } catch { /* Manual continue remains available. */ }
        if (intended) {
          setBusy(true);
          await invitePost("join", { token });
          clearInvitation();
          try { sessionStorage.removeItem(JOIN_INTENT); } catch { /* ignore */ }
          if (active) browserNavigation.assign("/app/");
        }
      } catch (e) {
        if (active) { setError(messageFor(e)); setBusy(false); }
        try { sessionStorage.removeItem(JOIN_INTENT); } catch { /* ignore */ }
      } finally { if (active) setLoading(false); }
    }
    void load();
    return () => { active = false; };
  }, [token]);

  async function continueToRevel() {
    setBusy(true); setError(null);
    if (!session) {
      if (token) {
        try { sessionStorage.setItem(JOIN_INTENT, token); } catch { /* Return page offers Continue. */ }
      }
      browserNavigation.assign(loginUrl(token ? "/app/invite" : "/app/"));
      return;
    }
    try {
      if (token) await invitePost("join", { token });
      clearInvitation();
      try { sessionStorage.removeItem(JOIN_INTENT); } catch { /* ignore */ }
      browserNavigation.assign("/app/");
    } catch (e) { setError(messageFor(e)); setBusy(false); }
  }

  async function signOut() {
    setBusy(true); setError(null);
    try {
      const { logout_url } = await postLogout();
      if (logout_url) browserNavigation.assign(logout_url);
      else { setSession(null); setPreview(null); }
    } catch (e) { setError(messageFor(e)); } finally { setBusy(false); }
  }

  return <div className="shell invite-welcome"><header className="app-header"><a className="brand" href="https://revel.gifts/">Revel Gifts</a></header><main id="main-content">
    <section className="invite-intro" aria-labelledby="invite-title">
      <div><p className="eyebrow">A more thoughtful way to give</p><h1 id="invite-title">You’re invited to Revel.</h1>
        <p className="invite-description">{preview?.sender_name ? `${preview.sender_name} invited you to Revel. ` : ""}Bring together what your assistants know about you, so the people in your life can give gifts that feel like you.</p>
        {loading ? <p role="status">Checking sign-in…</p> : <>
          {session && <p className="hint">Signed in as {session.user.email ?? session.user.display_name ?? "your Revel account"}.</p>}
          {error && <div className="invite-error"><p role="alert">{error}</p>{session && <><p>After signing out, reopen the invitation from your email and sign in with the email that received it.</p><button type="button" disabled={busy} onClick={() => void signOut()}>Use a different account</button></>}</div>}
          <button className="primary" type="button" disabled={busy || Boolean(session && token && !preview)} onClick={() => void continueToRevel()}>{busy ? "Opening Revel…" : session ? "Continue to Revel" : "Create Revel account"}</button>
          <p className="hint">{session ? "Your profile and sharing stay in your control." : "Already have an account? You can sign in on the next screen."}</p>
        </>}
      </div>
      <img className="invite-illustration" src="/app/revel-context.svg" alt="" />
    </section>
    <AssistantSetup active={[]} welcome />
  </main></div>;
}

async function invitePost<T>(operation: string, body: unknown): Promise<T> {
  const response = await fetch(`/api/invitations/${operation}`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) });
  const result: unknown = await response.json();
  if (!response.ok) throw new ApiError(response.status, result && typeof result === "object" && "detail" in result ? result.detail : "Unable to use this invitation.", result);
  return result as T;
}
