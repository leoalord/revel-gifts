import { useCallback, useEffect, useState } from "react";

import { UnauthorizedError } from "./api";
import { ConfirmButton } from "./ConfirmButton";
import { agentsApi, isStatus, messageFor } from "./connectionsApi";
import type { ConnectedAgent } from "./connectionsApi";
import { formatTime } from "./format";

/** `agent.label` is the app's registered name (F2), not a verified identity: plain text only. */
function AgentLabel({ agent }: { agent: ConnectedAgent["agent"] }) {
  return (
    <span>
      <strong>{agent.label}</strong>{" "}
      <span className="hint" title="The name the app registered with; not a verified identity">
        (registered app name)
      </span>
    </span>
  );
}

export function AgentsSection() {
  const [agents, setAgents] = useState<ConnectedAgent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [alert, setAlert] = useState<string | null>(null);

  const reload = useCallback(() => {
    agentsApi.list().then(
      (list) => {
        setAgents(list);
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
  }, [reload]);

  const disconnect = async (item: ConnectedAgent) => {
    setBusy(true);
    setNotice(null);
    setAlert(null);
    try {
      await agentsApi.disconnect(item.delegation_id);
      setNotice(`Disconnected ${item.agent.label}.`);
      return true;
    } catch (error) {
      if (error instanceof UnauthorizedError) return false;
      if (isStatus(error, 404)) {
        setNotice("That assistant was already disconnected. The list has been refreshed.");
        return true;
      }
      setAlert(messageFor(error));
      return false;
    } finally {
      setBusy(false);
      reload();
    }
  };

  const active = (agents ?? []).filter((a) => a.status === "active");
  const history = (agents ?? []).filter((a) => a.status !== "active");

  return (
    <section aria-labelledby="agents-heading" className="panel connection-panel">
      <h3 id="agents-heading" tabIndex={-1}><span className="connection-icon" aria-hidden="true">🤖</span> Connected assistants</h3>
      {loadError ? (
        <div role="alert">
          <p>{loadError}</p>
          <button type="button" onClick={reload}>
            Retry
          </button>
        </div>
      ) : agents === null ? (
        <p role="status">Loading assistants…</p>
      ) : (
        <>
          {alert && (
            <p role="alert" className="alert">
              {alert}
            </p>
          )}
          <p role="status" className="notice">
            {notice}
          </p>
          {active.length === 0 ? (
            <p>No assistants are connected.</p>
          ) : (
            <ul className="rows" aria-label="Active assistants">
              {active.map((a) => (
                <li key={a.delegation_id} className="row assistant-row">
                  <div className="assistant-identity">
                    <span className="identity-icon" aria-hidden="true">🤖</span>
                    <div>
                      <AgentLabel agent={a.agent} />
                      <div className="hint">Connected {formatTime(a.connected_at)}</div>
                    </div>
                  </div>
                  <div className="actions">
                    <ConfirmButton
                      label="Disconnect"
                      confirmLabel="Confirm disconnect"
                      successFocusId="agents-heading"
                      disabled={busy}
                      onConfirm={() => disconnect(a)}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
          {history.length > 0 && (
            <>
              <h4>History</h4>
              <ul className="rows" aria-label="Disconnected assistants">
                {history.map((a) => (
                  <li key={a.delegation_id} className="row">
                    <AgentLabel agent={a.agent} />{" "}
                    <span className="hint">
                      connected {formatTime(a.connected_at)}
                      {a.disconnected_at ? `, disconnected ${formatTime(a.disconnected_at)}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </section>
  );
}
