import { useState } from "react";
import type { MouseEvent } from "react";
import type { ConnectedAgent } from "./connectionsApi";
import { CopyTextButton } from "./CopyTextButton";
import { ProviderMarks } from "./ProviderMarks";
import type { ProviderId } from "./ProviderMarks";
import { SetupSection } from "./SetupSection";

const MCP_URL = "https://api.revel.gifts/mcp";
const STARTER = `Connect me to Revel Gifts using ${MCP_URL}, then load and follow the revel-get-started MCP prompt or resource revel://guidance/revel-get-started. If unavailable, read https://api.revel.gifts/guidance/revel-get-started.md. Resume my existing setup and ask for required approvals in this conversation. Keep invitation links out of this chat.`;
const CURSOR_CONFIG = JSON.stringify({ mcpServers: { "revel-gifts": { url: MCP_URL } } }, null, 2);

// Connection steps mirror the landing page; they do not assert host scheduling support.
const PROVIDERS: { id: ProviderId; name: string; labels: string[]; subtitle: string; steps: [string, string, string]; copy: "prompt" | "config" | "url" }[] = [
  { id: "muse", name: "Muse", labels: ["muse"], subtitle: "Paste into chat", steps: ["Copy the starter prompt", "Paste into a Muse chat", "Return to Muse to start or update your profile"], copy: "prompt" },
  { id: "grok", name: "Grok Bot", labels: ["grok bot", "cursor"], subtitle: "Paste into chat", steps: ["Copy the starter prompt", "Paste into a Grok Bot chat", "Return to Grok Bot to start or update your profile"], copy: "prompt" },
  { id: "cursor", name: "Cursor", labels: ["cursor"], subtitle: "Add a remote MCP server", steps: ["Copy the Revel MCP configuration", "Add the Revel server configuration", "Return to Cursor and paste the starter prompt"], copy: "config" },
  { id: "openai", name: "OpenAI Dots + ChatGPT", labels: ["chatgpt.com"], subtitle: "Add an MCP server in ChatGPT Desktop", steps: ["In ChatGPT Desktop, open Settings → Plugins → Add → Add MCP server", "Name it Revel Gifts, select Streamable HTTP, paste the MCP URL, and select Save", "Enable Revel in chat and paste the starter prompt"], copy: "url" },
  { id: "claude", name: "Anthropic Duet + Claude", labels: ["claude.ai", "claude"], subtitle: "Add a custom connector", steps: ["In Claude Desktop, open Settings → Customize → Connectors → + Add", "Select Add custom connector, name it Revel Gifts, paste the MCP URL, and select Continue", "Enable Revel in chat and paste the starter prompt"], copy: "url" },
  { id: "perplexity", name: "Perplexity Computer", labels: ["perplexity computer", "www.perplexity.ai"], subtitle: "Add a custom MCP connection", steps: ["In Perplexity Computer, open Plugins → + Add custom → Connector → Add MCP connector", "Paste the MCP URL and choose OAuth if prompted", "Return to Perplexity and paste the starter prompt"], copy: "url" },
];

export function AssistantSetup({ active, welcome = false }: { active: ConnectedAgent[]; welcome?: boolean }) {
  const [open, setOpen] = useState<ProviderId | null>(null);
  const labels = new Set(active.map(({ agent }) => agent.label.trim().toLowerCase()));
  const hasMatch = (provider: typeof PROVIDERS[number]) => provider.labels.some((label) => labels.has(label));
  const ordered = [...PROVIDERS].sort((a, b) => Number(hasMatch(a)) - Number(hasMatch(b)));

  function jump(event: MouseEvent<HTMLAnchorElement>, id: ProviderId) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    // Open synchronously so native anchor navigation can reach the revealed steps.
    const detail = document.getElementById(`connect-${id}`) as HTMLDetailsElement | null;
    if (detail) detail.open = true;
    setOpen(id);
    detail?.querySelector("summary")?.focus();
  }

  return <aside className="assistant-setup" aria-labelledby="connect-assistant-heading">
    <div className="setup-intro">
      <div><p className="eyebrow">{welcome ? "Or start with your assistant" : "Another perspective"}</p>
        <h3 id="connect-assistant-heading">{active.length ? "Connect another assistant" : "Connect an assistant"}</h3>
        <p>Your everyday chats, research and creative projects reveal different sides of you. Bring those perspectives together. You choose what each assistant may contribute and share.</p>
      </div>
      <img className="setup-illustration" src="/app/revel-context.svg" alt="" />
    </div>
    <nav className="provider-jump" aria-label="Choose an assistant">
      {ordered.map((provider) => <a key={provider.id} href={`#connect-${provider.id}`} onClick={(event) => jump(event, provider.id)}><ProviderMarks provider={provider.id} /><span>{provider.name}</span></a>)}
    </nav>
    <div className="mcp-address"><span><strong>Revel MCP URL</strong> <code>{MCP_URL}</code></span><CopyTextButton text={MCP_URL} label="Copy URL" /></div>
    <div className="provider-guides">
      {ordered.map((provider) => <details key={provider.id} id={`connect-${provider.id}`} open={open === provider.id}
        onToggle={(event) => { const expanded = event.currentTarget.open; setOpen((current) => expanded ? provider.id : current === provider.id ? null : current); }}>
        <summary><ProviderMarks provider={provider.id} /><span className="provider-heading"><strong>{provider.name}</strong><span className="hint">{provider.subtitle}</span></span>{hasMatch(provider) && <span className="provider-match">Possibly connected</span>}<span className="provider-chevron" aria-hidden="true" /></summary>
        <div className="provider-detail">
          <ol className="connection-steps">
            <li><span className="step-number" aria-hidden="true">1</span><strong>{provider.steps[0]}</strong>{provider.copy === "prompt" && <CopyTextButton text={STARTER} label="Copy starter prompt" />}</li>
            <li><span className="step-number" aria-hidden="true">2</span><strong>{provider.steps[1]}</strong>{provider.copy !== "prompt" && <CopyTextButton text={provider.copy === "config" ? CURSOR_CONFIG : MCP_URL} label={provider.copy === "config" ? "Copy configuration" : "Copy MCP URL"} />}</li>
            <li className="authorization-step"><span className="step-number" aria-hidden="true">3</span><strong>Sign in or create a Revel account</strong><span>Open the authorization link from your assistant. Secure sign-in via WorkOS.</span></li>
            <li><span className="step-number" aria-hidden="true">4</span><strong>{provider.steps[2]}</strong>{provider.copy !== "prompt" && <CopyTextButton text={STARTER} label="Copy starter prompt" />}</li>
          </ol>
          <p className="hint">{welcome ? "Sign in using the email that received your invitation." : "Use the same Revel account you’re signed into here."} Your assistant will ask for permission before contributing or arranging supported inbox checks.</p>
        </div>
      </details>)}
    </div>
    {!welcome && <p className="hint registration-note">“Possibly connected” is based on registered app names, which can differ from the assistant you use. It is not identity verification.</p>}
    {!welcome && <SetupSection />}
    {!welcome && <p className="full-guide"><a href="https://revel.gifts/#connect">Full connection guide <span aria-hidden="true">↗</span></a></p>}
  </aside>;
}
