import { useCallback, useEffect, useRef, useState } from "react";

import { UnauthorizedError } from "./api";
import { ConfirmButton } from "./ConfirmButton";
import { agentsApi, isStatus, messageFor } from "./connectionsApi";
import type { ConnectedAgent } from "./connectionsApi";
import { formatTime } from "./format";
import { SetupSection } from "./SetupSection";

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

const MCP_URL = "https://api.revel.gifts/mcp";
const SETUP_GUIDE = "https://revel.gifts/#connect";

const PROVIDERS = [
  { name: "Muse", labels: ["muse"], steps: "Paste the starter prompt into a Muse chat." },
  { name: "Grok Bot", labels: ["grok bot", "cursor"], steps: "Paste the starter prompt into a Grok Bot chat." },
  { name: "OpenAI Dots + ChatGPT", labels: ["chatgpt.com"], steps: "In ChatGPT Desktop, open Settings → Plugins → Add → Add MCP server. Select Streamable HTTP and enter the MCP URL." },
  { name: "Anthropic Duet + Claude", labels: ["claude.ai", "claude"], steps: "In Claude Desktop, open Settings → Customize → Connectors → + Add → Add custom connector. Enter the MCP URL." },
  { name: "Perplexity Computer", labels: ["perplexity computer", "www.perplexity.ai"], steps: "Open Plugins → + Add custom → Connector → Add MCP connector. Enter the MCP URL and choose OAuth if prompted." },
  { name: "Cursor", labels: ["cursor"], steps: "Add Revel Gifts as a remote server using the MCP URL in your Cursor MCP configuration." },
];

function AssistantSetup({ active }: { active: ConnectedAgent[] }) {
  // Names are self-reported. Use them only to order setup options, never to verify a provider.
  // Grok Bot has registered as Cursor, so either setup option may already be covered.
  const labels = new Set(active.map(({ agent }) => agent.label.trim().toLowerCase()));
  const hasMatch = (provider: typeof PROVIDERS[number]) =>
    provider.labels.some((label) => labels.has(label));
  const ordered = [...PROVIDERS].sort((a, b) => Number(hasMatch(a)) - Number(hasMatch(b)));

  return (
    <aside className="assistant-setup" aria-labelledby="connect-assistant-heading">
      <p className="eyebrow">Another perspective</p>
      <h3 id="connect-assistant-heading">{active.length ? "Connect another assistant" : "Connect an assistant"}</h3>
      <p>Your everyday chats, research and creative projects can each reveal different preferences. Connect the assistants you use to build a fuller picture. You choose what they may contribute and share.</p>
      <p className="hint">Choose an assistant for connection steps.</p>
      {ordered.map((provider) => (
        <details key={provider.name}>
          <summary>
            <strong>{provider.name}</strong>
            {hasMatch(provider) && <span className="hint"> · Possibly already connected</span>}
          </summary>
          <p>{provider.steps}</p>
          <p className="server-address">Server URL: <code>{MCP_URL}</code></p>
          <p>Follow the authorization link to sign in to Revel through WorkOS, then return to your assistant to start or update your profile.</p>
        </details>
      ))}
      <p className="hint">Registered app names can differ from the assistant you use. “Possibly already connected” is a name match, not verified provider identity.</p>
      <SetupSection />
      <p className="full-guide"><a href={SETUP_GUIDE}>Full connection guide <span aria-hidden="true">↗</span></a></p>
    </aside>
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
                        <span className="identity-icon" aria-hidden="true">{a.agent.label.trim().slice(0, 1).toLocaleUpperCase() || "A"}</span>
                        <div>
                          <AgentLabel agent={a.agent} />
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
