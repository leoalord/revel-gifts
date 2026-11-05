import { useCallback, useEffect, useId, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { UnauthorizedError } from "./api";
import { AgentsSection } from "./AgentsSection";
import { InvitationSection } from "./InvitationSection";
import { ConfirmButton } from "./ConfirmButton";
import {
  TIERS,
  connectionsApi,
  errorCode,
  fieldErrorsFor,
  messageFor,
} from "./connectionsApi";
import type {
  BlockedEntry,
  ConnectionEntry,
  HandleInfo,
  IncomingRequest,
  OutgoingRequest,
  Person,
  PublicIdentity,
  Tier,
} from "./connectionsApi";
import { audienceLabeler, formatTime } from "./format";
import type { AudienceLabel } from "./format";
import { getPolicy } from "./profileApi";
import type { FieldErrors } from "./profileApi";

interface Data {
  handle: HandleInfo | null;
  incoming: IncomingRequest[];
  outgoing: OutgoingRequest[];
  connections: ConnectionEntry[];
  blocked: BlockedEntry[];
}

async function loadAll(): Promise<Data> {
  const [handle, incoming, outgoing, connections, blocked] = await Promise.all([
    connectionsApi.getHandle(),
    connectionsApi.incoming(),
    connectionsApi.outgoing(),
    connectionsApi.connections(),
    connectionsApi.blocked(),
  ]);
  return { handle, incoming, outgoing, connections, blocked };
}

function personName(p: PublicIdentity): string {
  return p.display_name || (p.handle ? `@${p.handle}` : "Unknown person");
}

function PersonView({ person }: { person: PublicIdentity | Person }) {
  const assistant = "assistant" in person ? person.assistant : null;
  const name = personName(person);
  const initials = (person.display_name || person.handle || "")
    .trim().split(/\s+/).slice(0, 2).map((part) => Array.from(part)[0] ?? "").join("");
  return (
    <span className="person">
      <span className="identity-icon" aria-hidden="true">{initials.toLocaleUpperCase() || "?"}</span>
      <strong>{name}</strong>
      {person.display_name && person.handle && <span className="handle"> (@{person.handle})</span>}
      {assistant && (
        <span className="hint">
          {" "}
          via {assistant.name || assistant.client_id}{" "}
          <span title="The assistant app's registered name, not a verified identity">
            (registered app name)
          </span>
        </span>
      )}
    </span>
  );
}

function Section({ id, title, icon, children }: { id: string; title: string; icon: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="panel connection-panel">
      <h3 id={id} tabIndex={-1}><span className="connection-icon" aria-hidden="true">{icon}</span> {title}</h3>
      {children}
    </section>
  );
}

function FieldError({ id, message }: { id: string; message?: string }) {
  return message ? (
    <p className="field-error" id={id}>
      {message}
    </p>
  ) : null;
}

function HandleForm({ current, onSaved }: { current: HandleInfo | null; onSaved: (h: HandleInfo) => void }) {
  const [handle, setHandle] = useState(current?.handle ?? "");
  const [discoverable, setDiscoverable] = useState(current?.discoverable ?? false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage(null);
    try {
      const body: { handle?: string; discoverable: boolean } = { discoverable };
      if (handle !== current?.handle) body.handle = handle;
      onSaved(await connectionsApi.setHandle(body));
    } catch (error) {
      if (error instanceof UnauthorizedError) return;
      const fields = fieldErrorsFor(error, "handle");
      if (fields) setErrors(fields);
      else if (errorCode(error) === "handle_unavailable") setErrors({ handle: messageFor(error) });
      else setMessage(messageFor(error));
    } finally {
      setBusy(false);
    }
  };

  const handleError = errors.handle ?? errors._;
  return (
    <form onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor="my-handle">Handle</label>
        <input
          id="my-handle"
          value={handle}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setHandle(e.target.value)}
          aria-invalid={handleError ? true : undefined}
          aria-describedby={handleError ? "my-handle-error" : "my-handle-hint"}
        />
        <p className="hint" id="my-handle-hint">
          Others send you a connection request with this exact handle.
        </p>
        <FieldError id="my-handle-error" message={handleError} />
      </div>
      <div className="field checkbox-field">
        <input
          id="my-discoverable"
          type="checkbox"
          checked={discoverable}
          onChange={(e) => setDiscoverable(e.target.checked)}
        />
        <label htmlFor="my-discoverable">Let people find me by my handle</label>
      </div>
      {message && (
        <p role="alert" className="alert">
          {message}
        </p>
      )}
      <button type="submit" className="primary" disabled={busy}>
        {current ? "Save handle" : "Set handle"}
      </button>
    </form>
  );
}

function RequestForm({ onSent }: { onSent: (notice: string) => void }) {
  const [handle, setHandle] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage(null);
    try {
      const result = await connectionsApi.requestByHandle(handle.trim().replace(/^@/, ""));
      setHandle("");
      onSent(
        result.created
          ? `Request sent to ${personName(result.target)}.`
          : `You already have a request with ${personName(result.target)}.`,
      );
    } catch (error) {
      if (error instanceof UnauthorizedError) return;
      const fields = fieldErrorsFor(error, "handle");
      if (fields) setErrors(fields);
      // Includes 404 target_not_found: the server's message is deliberately generic.
      else setMessage(messageFor(error));
    } finally {
      setBusy(false);
    }
  };

  const handleError = errors.handle ?? errors._;
  return (
    <form onSubmit={submit} noValidate className="inline-form">
      <div className="field">
        <label htmlFor="request-handle">Request by handle</label>
        <input
          id="request-handle"
          value={handle}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setHandle(e.target.value)}
          aria-invalid={handleError ? true : undefined}
          aria-describedby={handleError ? "request-handle-error" : undefined}
        />
        <FieldError id="request-handle-error" message={handleError} />
      </div>
      <button type="submit" className="primary" disabled={busy || !handle.trim()}>
        Send request
      </button>
      {message && (
        <p role="alert" className="alert">
          {message}
        </p>
      )}
    </form>
  );
}

function ApproveForm({
  request,
  tierLabel,
  busy,
  onApprove,
}: {
  request: IncomingRequest;
  tierLabel: AudienceLabel;
  busy: boolean;
  onApprove: (tier: Tier) => void;
}) {
  const name = useId();
  const [tier, setTier] = useState<Tier | null>(null);
  return (
    <form
      className="approve-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (tier) onApprove(tier);
      }}
    >
      <fieldset>
        <legend>Share my profile with {personName(request.requester)} as</legend>
        {TIERS.map((t) => (
          <span key={t} className="radio">
            <input
              id={`${name}-${t}`}
              type="radio"
              name={name}
              value={t}
              checked={tier === t}
              onChange={() => setTier(t)}
            />
            <label htmlFor={`${name}-${t}`}>{tierLabel(t)}</label>
          </span>
        ))}
      </fieldset>
      <button type="submit" className="primary" disabled={busy || !tier}>
        Approve
      </button>
    </form>
  );
}

function TierForm({
  entry,
  tierLabel,
  busy,
  onChange,
}: {
  entry: ConnectionEntry;
  tierLabel: AudienceLabel;
  busy: boolean;
  onChange: (tier: Tier) => void;
}) {
  const id = useId();
  const [tier, setTier] = useState(entry.tier);
  return (
    <form
      className="inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        onChange(tier as Tier);
      }}
    >
      <label htmlFor={id}>Tier for {personName(entry.person)}</label>
      <select id={id} value={tier} onChange={(e) => setTier(e.target.value)}>
        {[...TIERS, ...(TIERS.includes(entry.tier as Tier) ? [] : [entry.tier])].map((t) => (
          <option key={t} value={t}>
            {tierLabel(t)}
          </option>
        ))}
      </select>
      <button type="submit" disabled={busy || tier === entry.tier}>
        Change tier
      </button>
    </form>
  );
}

export function ConnectionsScreen() {
  const [data, setData] = useState<Data | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tierLabel, setTierLabel] = useState<AudienceLabel>(() => audienceLabeler(undefined));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [alert, setAlert] = useState<string | null>(null);

  const reload = useCallback(() => {
    loadAll().then(
      (next) => {
        setData(next);
        setLoadError(null);
      },
      (error: unknown) => {
        if (error instanceof UnauthorizedError) return;
        setLoadError(messageFor(error));
      },
    );
  }, []);

  useEffect(() => {
    reload();
    // Tier labels: the profile policy's server audience labels when available (presentation).
    getPolicy().then(
      (policy) => setTierLabel(() => audienceLabeler(policy.audiences)),
      () => {},
    );
  }, [reload]);

  const act = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    setNotice(null);
    setAlert(null);
    try {
      await action();
      setNotice(success);
      return true;
    } catch (error) {
      if (!(error instanceof UnauthorizedError)) setAlert(messageFor(error));
      return false;
    } finally {
      setBusy(false);
      // Re-read after every action, success or not (e.g. a 404/409 means the list is stale).
      reload();
    }
  };

  if (loadError || !data) {
    return (
      <section aria-labelledby="connections-heading">
        <h2 id="connections-heading">Connections</h2>
        {loadError ? (
          <>
            <p role="alert" className="alert">{loadError}</p>
            <button type="button" onClick={() => {
              setLoadError(null);
              reload();
            }}>Retry</button>
          </>
        ) : <p role="status">Loading connections…</p>}
      </section>
    );
  }

  return (
    <section aria-labelledby="connections-heading" className="connections">
      <h2 id="connections-heading">Connections</h2>
      <p className="page-intro">Manage who can access your gifting profile and which assistants can act for you.</p>
      {alert && (
        <p role="alert" className="alert">
          {alert}
        </p>
      )}
      <p role="status" className="notice">
        {notice}
      </p>

      <Section id="incoming-heading" icon="📥" title="Incoming requests">
        <p className="hint">Approval shares your eligible profile details with them. Access to their profile requires a separate approval.</p>
        {data.incoming.length === 0 ? (
          <p>No pending requests.</p>
        ) : (
          <ul className="rows">
            {data.incoming.map((r) => (
              <li key={r.id} className="row request-card">
                <p className="request-title">
                  <PersonView person={r.requester} /> wants to see your profile.{" "}
                  <span className="hint">Asked {formatTime(r.created_at)}.</span>
                </p>
                <ApproveForm
                  request={r}
                  tierLabel={tierLabel}
                  busy={busy}
                  onApprove={(tier) =>
                    void act(
                      () => connectionsApi.approve(r.id, tier),
                      `Approved ${personName(r.requester)} as ${tierLabel(tier)}.`,
                    )
                  }
                />
                <div className="actions">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void act(
                        () => connectionsApi.decline(r.id),
                        `Declined ${personName(r.requester)}.`,
                      )
                    }
                  >
                    Decline
                  </button>
                  <ConfirmButton
                    label="Block"
                    confirmLabel="Confirm block"
                    successFocusId="incoming-heading"
                    disabled={busy}
                    onConfirm={() =>
                      act(() => connectionsApi.block(r.id), `Blocked ${personName(r.requester)}.`)
                    }
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section id="people-heading" icon="👥" title="People who can see my profile">
        <p className="hint">Connections can see details shared with Connections. Trusted Circle can also see details reserved for that circle. Revoking access stops future access through this connection.</p>
        {data.connections.length === 0 ? (
          <p>No connections yet.</p>
        ) : (
          <ul className="rows">
            {data.connections.map((c) => (
              <li key={c.request_id} className="row">
                <p>
                  <PersonView person={c.person} />{" "}
                  <span className={`badge ${c.tier === "trusted_circle" ? "tone-info" : "tone-neutral"}`}>{tierLabel(c.tier)}</span>
                  {c.since && <span className="hint"> since {formatTime(c.since)}</span>}
                </p>
                <TierForm
                  key={c.tier}
                  entry={c}
                  tierLabel={tierLabel}
                  busy={busy}
                  onChange={(tier) =>
                    void act(
                      () => connectionsApi.setTier(c.request_id, tier),
                      `${personName(c.person)} is now ${tierLabel(tier)}.`,
                    )
                  }
                />
                <div className="actions">
                  <ConfirmButton
                    label="Revoke"
                    confirmLabel="Confirm revoke"
                    successFocusId="people-heading"
                    disabled={busy}
                    onConfirm={() =>
                      act(
                        () => connectionsApi.revoke(c.request_id),
                        `Revoked ${personName(c.person)}.`,
                      )
                    }
                  />
                  <ConfirmButton
                    label="Block"
                    confirmLabel="Confirm block"
                    successFocusId="people-heading"
                    disabled={busy}
                    onConfirm={() =>
                      act(
                        () => connectionsApi.block(c.request_id),
                        `Blocked ${personName(c.person)}.`,
                      )
                    }
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section id="outgoing-heading" icon="↗" title="My requests">
        <p className="hint">Request access to someone else’s gifting profile using their exact handle.</p>
        <RequestForm
          onSent={(message) => {
            setNotice(message);
            setAlert(null);
            reload();
          }}
        />
        {data.outgoing.length === 0 ? (
          <p>No outstanding requests.</p>
        ) : (
          <ul className="rows">
            {data.outgoing.map((r) => (
              <li key={r.id} className="row">
                <PersonView person={r.target} />{" "}
                <span className="badge">{r.status === "pending" ? "Pending" : "Approved"}</span>{" "}
                <span className="hint">sent {formatTime(r.created_at)}</span>
                {r.status === "pending" && (
                  <div className="actions">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void act(
                          () => connectionsApi.cancel(r.id),
                          `Cancelled the request to ${personName(r.target)}.`,
                        )
                      }
                    >
                      Cancel request
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section id="handle-heading" icon="@" title="My handle">
        <p className="hint">Your handle helps people find your identity. Gifting profile access is managed separately.</p>
        {data.handle && (
          <p>
            Your handle is <strong>@{data.handle.handle}</strong>
            {data.handle.discoverable ? " (discoverable)" : " (not discoverable)"}.
          </p>
        )}
        <HandleForm
          key={`${data.handle?.handle ?? ""}:${data.handle?.discoverable ?? ""}`}
          current={data.handle}
          onSaved={(h) => {
            setNotice(`Handle saved: @${h.handle}.`);
            setAlert(null);
            reload();
          }}
        />
      </Section>

      <Section id="blocked-heading" icon="⛔" title="Blocked">
        {data.blocked.length === 0 ? (
          <p>Nobody is blocked.</p>
        ) : (
          <ul className="rows">
            {data.blocked.map((b) => (
              <li key={b.request_id} className="row">
                <PersonView person={b.person} />
                {b.blocked_at && <span className="hint"> blocked {formatTime(b.blocked_at)}</span>}
                <div className="actions">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void act(
                        () => connectionsApi.unblock(b.request_id),
                        `Unblocked ${personName(b.person)}.`,
                      )
                    }
                  >
                    Unblock
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <InvitationSection />
      <AgentsSection />
    </section>
  );
}
