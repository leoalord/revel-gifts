import { useCallback, useEffect, useRef, useState } from "react";

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

const MCP_URL = "https://api.revel.gifts/mcp";
const SETUP_GUIDE = "https://revel-gifts-public.web.app/#connect";

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
    <div className="assistant-setup">
      <h4>{active.length ? "Connect another assistant" : "Connect an assistant"}</h4>
      <p>Different assistants may know different details you’ve shared. Connect another to fill useful gaps within the permissions you choose.</p>
      <p>MCP server URL: <code>{MCP_URL}</code></p>
      <details>
        <summary>Starter prompt</summary>
        <p>Connect me to Revel Gifts using {MCP_URL}, then load and follow the revel-get-started MCP prompt or resource revel://guidance/revel-get-started. If unavailable, read https://api.revel.gifts/guidance/revel-get-started.md. Resume my existing profile setup and ask for my approval before setting up one recurring inbox task in my confirmed local timezone. Keep invitation links out of this chat.</p>
      </details>
      {ordered.map((provider) => (
        <details key={provider.name}>
          <summary>
            <strong>{provider.name}</strong>
            {hasMatch(provider) && <span className="hint"> · Possibly already connected</span>}
          </summary>
          <p>{provider.steps}</p>
          <p>Follow the authorization link to sign in to Revel through WorkOS, then return to your assistant to start or update your profile.</p>
        </details>
      ))}
      <p><a href={SETUP_GUIDE}>Full setup guide and copyable starter prompt</a></p>
    </div>
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
                    <span className="identity-icon" aria-hidden="true">{home ? "✓" : "🤖"}</span>
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
          {home && <AssistantSetup active={active} />}
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
    </section>
  );
}
