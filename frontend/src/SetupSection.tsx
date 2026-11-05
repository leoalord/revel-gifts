import { useEffect, useState } from "react";

/** Onboarding and scheduling consent take place in the user's assistant conversation. */
export function SetupSection() {
  const [prompt, setPrompt] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/guidance/revel-get-started.md", { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error();
      const text = await response.text();
      if (!controller.signal.aborted) setPrompt(text);
    }).catch(() => { /* The guidance link remains available when copying is unavailable. */ });
    return () => controller.abort();
  }, []);

  async function copyPrompt() {
    if (!prompt) return;
    try {
      await navigator.clipboard.writeText(
        "Connect me to Revel Gifts using https://api.revel.gifts/mcp. " +
        "Load and follow revel-get-started through your supported MCP prompt/resource path, " +
        "or read https://api.revel.gifts/guidance/revel-get-started.md. " +
        "Resume my setup, ask for required approvals in this conversation, and keep invitation links out of this chat.\n\n" +
        prompt,
      );
      setNotice("Starter guidance copied. Paste it into your connected assistant.");
    } catch {
      setNotice("Open the starter guidance link and copy its text.");
    }
  }

  return (
    <div className="starter-guidance">
      <p>Once connected, ask your assistant to follow Revel’s starter guidance. It checks your existing profile, asks for your permission and adds useful details from context it can access.</p>
      <div className="actions">
        <button type="button" className="primary" disabled={!prompt} onClick={() => void copyPrompt()}>Copy starter prompt</button>
        <a href="/guidance/revel-get-started.md">Read guidance</a>
      </div>
      {notice && <p role="status" className="hint">{notice}</p>}
    </div>
  );
}
