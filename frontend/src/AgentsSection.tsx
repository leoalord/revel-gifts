import { useCallback, useEffect, useRef, useState } from "react";

import { UnauthorizedError } from "./api";
import { ConfirmButton } from "./ConfirmButton";
import { agentsApi, isStatus, messageFor } from "./connectionsApi";
import type { ConnectedAgent } from "./connectionsApi";
import { formatTime } from "./format";
import { AssistantSetup } from "./AssistantSetup";
import { RegisteredAssistantMark } from "./ProviderMarks";

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

export function AgentsSection({ home = false }: { home?: boolean }) {
  const [agents, setAgents] = useState<ConnectedAgent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [alert, setAlert] = useState<string | null>(null);

  const latestRequest = useRef(0);
  const reload = useCallback(() => {
    const request = ++latestRequest.current;
    agentsApi.list().then(
      (list) => {
        if (request !== latestRequest.current) return;
        setAgents(list);
        setLoadError(null);
      },
      (error: unknown) => {
        if (request !== latestRequest.current || error instanceof UnauthorizedError) return;
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
    <section aria-labelledby="agents-heading" className={home ? "panel connection-panel home-assistants" : "panel connection-panel"}>
      <div className={home ? "home-assistant-layout" : undefined}>
        <div className="assistant-list">
          <div className="section-head"><h3 id="agents-heading" tabIndex={-1}>Connected assistants</h3></div>
          {home && <p className="hint">These assistants can contribute within the permissions you approve.</p>}
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
                <div className="assistant-empty"><p>No assistants are connected.</p><p className="hint">Start with an assistant you already use. It can help build your profile with your permission.</p></div>
              ) : (
                <ul className="rows" aria-label="Active assistants">
                  {active.map((a) => (
                    <li key={a.delegation_id} className="row assistant-row">
                      <div className="assistant-identity">
                        <RegisteredAssistantMark label={a.agent.label} />
                        <div>
                          <AgentLabel agent={a.agent} />
                          {a.agent.label.trim().toLowerCase() === "cursor" && <div className="hint">Could be Cursor or Grok Bot</div>}
                          <div className="hint">Connected {formatTime(a.connected_at)}</div>
                        </div>
                      </div>
                      {!home && <div className="actions">
                        <ConfirmButton
                          label="Disconnect"
                          confirmLabel="Confirm disconnect"
                          successFocusId="agents-heading"
                          disabled={busy}
                          onConfirm={() => disconnect(a)}
                        />
                      </div>}
                    </li>
                  ))}
                </ul>
              )}
              {home && (
                <div className="actions">
                  <button type="button" onClick={reload}>Refresh assistants</button>
                  {active.length > 0 && <a href="/app/connections#agents-heading">Manage connected assistants</a>}
                </div>
              )}
              {!home && history.length > 0 && (
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
        </div>
        {home && <AssistantSetup active={active} />}
      </div>
    </section>
  );
}
