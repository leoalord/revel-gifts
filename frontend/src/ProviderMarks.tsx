import { useState } from "react";

// Reuse the unchanged logo artwork from landing/public; no remote image requests.
const ROOT = "/app/providers/";
export type ProviderId = "muse" | "grok" | "cursor" | "openai" | "claude" | "perplexity";
const ART: Record<ProviderId, [string, string]> = {
  muse: ["provider-muse-v1.png", "provider-meta-color-v1.svg"],
  grok: ["provider-grok-bot-v1.png", "provider-x-v1.svg"],
  cursor: ["provider-cursor-v1.svg", "provider-x-v1.svg"],
  openai: ["provider-dots-v1.png", "provider-openai-v1.svg"],
  claude: ["provider-duet-v1.png", "provider-anthropic-v1.svg"],
  perplexity: ["provider-computer-v1.png", "provider-perplexity-color-v1.svg"],
};

function GenericMark() {
  return <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><rect x="5" y="8" width="22" height="18" rx="5" /><path d="M16 8V4m-5 12h1m8 0h1m-9 5h8" strokeLinecap="round" /></svg>;
}

function Logo({ file, product, dots }: { file: string; product?: boolean; dots?: boolean }) {
  const [failed, setFailed] = useState(false);
  return <span className={`provider-logo ${product ? "provider-product" : "provider-company"}${dots ? " provider-dots" : ""}`}>
    {failed ? <GenericMark /> : <img src={ROOT + file} alt="" onError={() => setFailed(true)} />}
  </span>;
}

export function ProviderMarks({ provider }: { provider: ProviderId }) {
  const [product, company] = ART[provider];
  return <span className="provider-marks" aria-hidden="true"><Logo file={product} product dots={provider === "openai"} /><Logo file={company} /></span>;
}

export function RegisteredAssistantMark({ label }: { label: string }) {
  const name = label.trim().toLowerCase();
  // A registration named Cursor can be either Cursor or Grok Bot. Show both cues,
  // with explicit accompanying copy; never infer a verified provider from a label.
  if (name === "cursor") return <span className="provider-marks" aria-hidden="true"><Logo file={ART.cursor[0]} product /><Logo file={ART.grok[0]} product /></span>;
  const provider = name === "muse" ? "muse" : name === "grok bot" ? "grok" :
    ["perplexity computer", "www.perplexity.ai"].includes(name) ? "perplexity" :
    name === "chatgpt.com" ? "openai" : ["claude.ai", "claude"].includes(name) ? "claude" : null;
  return provider ? <ProviderMarks provider={provider} /> : <span className="provider-logo provider-product" aria-hidden="true"><GenericMark /></span>;
}
