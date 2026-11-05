import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { apiRequest } from "./api";
import { messageFor } from "./connectionsApi";
import { formatTime } from "./format";

interface Invitation {
  id: string;
  email: string;
  status: string;
  expires_at: string;
  delivery_status: string;
  invite_url?: string | null;
}

export function InvitationSection() {
  const [items, setItems] = useState<Invitation[]>([]);
  const [email, setEmail] = useState("");
  const [channel, setChannel] = useState<"copy" | "email">("copy");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const reload = () => apiRequest<Invitation[]>("/api/invitations").then(setItems);
  useEffect(() => { void reload().catch((e: unknown) => setError(messageFor(e))); }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(null); setNotice(null); setLink(null);
    try {
      const result = await apiRequest<Invitation>("/api/invitations", {
        method: "POST", body: { email, channel, owner_confirmed: confirmed },
      });
      setLink(result.invite_url ?? null);
      setNotice(channel === "email" ? `Delivery status: ${result.delivery_status}.` : "Your invitation link is ready. Share it only with the invited person.");
      await reload();
      setConfirmed(false);
    } catch (e) { setError(messageFor(e)); } finally { setBusy(false); }
  }

  async function cancel(id: string) {
    setBusy(true); setError(null); setNotice(null);
    try {
      await apiRequest(`/api/invitations/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      setLink(null); setNotice("Invitation cancelled. Its link can no longer be accepted.");
      await reload();
    } catch (e) { setError(messageFor(e)); } finally { setBusy(false); }
  }

  return <section className="panel connection-panel" aria-labelledby="invitations-heading">
    <h3 id="invitations-heading">Invite a friend</h3>
    <p>Invite someone by email, or create a link to share yourself. They must verify that email through WorkOS. Accepting an invitation does not share either person’s profile.</p>
    <form onSubmit={(e) => void submit(e)}>
      <div className="field"><label htmlFor="invite-email">Their email</label><input id="invite-email" type="email" required autoComplete="off" value={email} onChange={(e) => { setEmail(e.target.value); setConfirmed(false); }} /></div>
      <div className="field"><label htmlFor="invite-channel">How to invite</label><select id="invite-channel" value={channel} onChange={(e) => { setChannel(e.target.value as "copy" | "email"); setConfirmed(false); }}><option value="copy">Create a link to share</option><option value="email">Send an email invitation</option></select></div>
      {channel === "email" && <label className="checkbox-field"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /> I want Revel to send an invitation to {email || "the email above"}.</label>}
      <p className="hint">Email sending requires a configured mail provider. A copyable link works independently. Submitted means an email attempt was recorded, not that it reached the mailbox.</p>
      <button type="submit" disabled={busy || (channel === "email" && !confirmed)}>{channel === "copy" ? "Create invitation link" : "Send confirmed invitation"}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {link && <div className="field"><label htmlFor="invite-link">Invitation link (private)</label><input id="invite-link" readOnly value={link} onFocus={(e) => e.target.select()} /><button type="button" onClick={() => { void navigator.clipboard.writeText(link).then(() => setNotice("Invitation link copied."), () => setNotice("Select the link above and copy it.")); }}>Copy invitation link</button></div>}
    {items.length > 0 && <ul className="rows">{items.map((item) => <li className="row" key={item.id}><strong>{item.email}</strong><span className="badge">{item.status}</span><p className="hint">Expires {formatTime(item.expires_at)} · Email: {item.delivery_status}</p>{item.status === "pending" && <button type="button" disabled={busy} onClick={() => void cancel(item.id)}>Cancel invitation</button>}</li>)}</ul>}
  </section>;
}
