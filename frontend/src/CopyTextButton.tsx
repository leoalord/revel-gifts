import { useState } from "react";

export function CopyTextButton({ text, label }: { text: string; label: string }) {
  const [notice, setNotice] = useState("");
  const [fallback, setFallback] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setNotice("Copied"); setFallback(false);
    } catch {
      setNotice("Select and copy the text below."); setFallback(true);
    }
  }
  return <span className="copy-control">
    <button type="button" onClick={() => void copy()}>{label}</button>
    {notice && <span className="hint" role="status">{notice}</span>}
    {fallback && <textarea aria-label={`${label} text`} readOnly value={text} onFocus={(e) => e.currentTarget.select()} />}
  </span>;
}
