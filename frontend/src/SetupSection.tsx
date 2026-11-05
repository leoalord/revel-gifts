import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { apiRequest } from "./api";
import { agentsApi, messageFor } from "./connectionsApi";
import type { ConnectedAgent } from "./connectionsApi";
import { formatTime } from "./format";

interface Setup {
  revision: number;
  enabled: boolean;
  delegation_id: string | null;
  timezone: string | null;
  check_hours: number[];
  host_task_id: string | null;
  schedule_state: string;
  background_verified: boolean;
  last_successful_check_at: string | null;
  last_check_item_count: number | null;
  last_successful_check_delegation_id?: string | null;
  host_action_required: boolean;
}

export function SetupSection() {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [agents, setAgents] = useState<ConnectedAgent[]>([]);
  const [delegation, setDelegation] = useState("");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [hours, setHours] = useState("6, 10, 14, 18");
  const [taskId, setTaskId] = useState("");
  const [approved, setApproved] = useState(false);
  const [reported, setReported] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [prompt, setPrompt] = useState<string | null>(null);

  function apply(result: Setup) {
    setSetup(result);
    setDelegation(result.delegation_id ?? "");
    if (result.timezone) setTimezone(result.timezone);
    setHours(result.check_hours.join(", "));
    setTaskId(result.host_task_id ?? "");
    setApproved(false); setReported(false);
  }
  useEffect(() => {
    let active = true;
    void Promise.all([apiRequest<Setup>("/api/setup"), agentsApi.list()]).then(([result, list]) => {
      if (!active) return;
      apply(result); setAgents(list.filter((a) => a.status === "active"));
    }, (e: unknown) => { if (active) setError(messageFor(e)); });
    void fetch("/guidance/revel-get-started.md").then(async (response) => {
      if (!response.ok) throw new Error();
      const text = await response.text();
      if (active) setPrompt(text);
    }).catch(() => { /* Guidance link remains available. */ });
    return () => { active = false; };
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!setup) return;
    const checkHours = hours.split(",").map((x) => Number(x.trim()));
    if (!hours.trim() || checkHours.some((x) => !Number.isInteger(x) || x < 0 || x > 23) || new Set(checkHours).size !== checkHours.length) {
      setError("Enter distinct hours from 0 to 23, separated by commas."); return;
    }
    await act(() => apiRequest<Setup>("/api/setup", { method: "PUT", body: { expected_revision: setup.revision, enabled: true, delegation_id: delegation, timezone, check_hours: checkHours } }), "Schedule preference saved. Ask the designated assistant to create or update one host task, then record its ID below.");
  }

  async function act(operation: () => Promise<Setup>, success: string) {
    setBusy(true); setError(null); setNotice(null);
    try { apply(await operation()); setNotice(success); }
    catch (e) { setError(messageFor(e)); }
    finally { setBusy(false); }
  }

  return <section className="panel" aria-labelledby="setup-heading">
    <p className="eyebrow">Start here, or pick up where you left off</p><h3 id="setup-heading">Build your profile. Keep your inbox checked.</h3>
    <p>Use Revel’s starter guidance in your assistant. It checks your existing setup, asks for contribution consent and builds useful, sourced preferences from context it can access. Additional assistants can fill useful gaps.</p>
    <p><a href="/guidance/revel-get-started.md">Read the starter guidance</a> · <a href="/app/policy">Review contribution consent</a> · <a href="/app/contributions">Review your profile</a></p>
    <button type="button" disabled={!prompt} onClick={() => { if (prompt) void navigator.clipboard.writeText(prompt).then(() => setNotice("Starter guidance copied. Paste it into your connected assistant."), () => setNotice("Open the starter guidance link and copy its text.")); }}>Copy starter prompt</button>
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    {setup && <>
      <h4>Scheduled inbox checks</h4>
      <p>Status: <strong>{setup.schedule_state.replaceAll("_", " ")}</strong>. Unattended pickup: <strong>not verified</strong>.</p>
      <p>Last successful inbox check: {setup.last_successful_check_at ? formatTime(setup.last_successful_check_at) : "not observed"}{setup.last_successful_check_at && ` · ${setup.last_check_item_count ?? 0} items`}. A server-observed check does not establish that it ran in the background.</p>
      {setup.last_successful_check_delegation_id && <p className="hint">Last check by {agents.find((a) => a.delegation_id === setup.last_successful_check_delegation_id)?.agent.label ?? "a previously connected assistant"} · {setup.last_successful_check_delegation_id.slice(0, 8)}. This may precede your current schedule.</p>}
      <p>Choose one assistant and confirm your local timezone. The proposed default is 6 am, 10 am, 2 pm and 6 pm. The assistant’s host must actually create the task with your approval. A saved preference or copied prompt does not create it.</p>
      <form onSubmit={(e) => void save(e)}>
        <div className="field"><label htmlFor="setup-agent">Designated assistant</label><select id="setup-agent" required value={delegation} onChange={(e) => { setDelegation(e.target.value); setApproved(false); }}><option value="">Choose an assistant</option>{agents.map((a) => <option key={a.delegation_id} value={a.delegation_id}>{a.agent.label} · {a.delegation_id.slice(0, 8)}</option>)}</select></div>
        <div className="field"><label htmlFor="setup-timezone">Local timezone (confirm before saving)</label><input id="setup-timezone" required value={timezone} onChange={(e) => { setTimezone(e.target.value); setApproved(false); }} placeholder="America/Los_Angeles" /></div>
        <div className="field"><label htmlFor="setup-hours">Daily hours (24-hour clock)</label><input id="setup-hours" required value={hours} onChange={(e) => { setHours(e.target.value); setApproved(false); }} /></div>
        <label className="checkbox-field"><input type="checkbox" checked={approved} onChange={(e) => setApproved(e.target.checked)} /> I approve this assistant, timezone and check schedule.</label>
        <button type="submit" disabled={busy || !approved || !delegation}>Save approved schedule preference</button>
      </form>
      <p className="hint">Before creating a task, ask your host to find and update any existing Revel inbox task. Each run calls get_updates once, finishes quietly when empty and follows Revel’s answer guidance when work is present.</p>
      {setup.enabled && <form onSubmit={(e) => { e.preventDefault(); void act(() => apiRequest<Setup>("/api/setup/host-task", { method: "POST", body: { expected_revision: setup.revision, host_task_id: taskId, disabled: false } }), "Host task recorded as reported. Unattended operation still needs a real client check."); }}>
        <div className="field"><label htmlFor="setup-task">Task ID from your host</label><input id="setup-task" required maxLength={200} value={taskId} onChange={(e) => { setTaskId(e.target.value); setReported(false); }} /></div>
        <label className="checkbox-field"><input type="checkbox" checked={reported} onChange={(e) => setReported(e.target.checked)} /> My host has actually created or updated this task with my approval.</label>
        <button type="submit" disabled={busy || !reported || !taskId.trim()}>Record host task</button>
      </form>}
      {setup.enabled && <button type="button" disabled={busy} onClick={() => void act(() => apiRequest<Setup>("/api/setup", { method: "PUT", body: { expected_revision: setup.revision, enabled: false, delegation_id: setup.delegation_id, timezone: setup.timezone, check_hours: setup.check_hours } }), "Revel preference disabled. You must also disable the recurring task in your assistant’s host.")}>Disable schedule preference</button>}
      {setup.host_action_required && !setup.enabled && <><p role="alert">Disable the recurring task in your host too. Revel cannot stop a host’s task.</p><button type="button" disabled={busy} onClick={() => void act(() => apiRequest<Setup>("/api/setup/host-task", { method: "POST", body: { expected_revision: setup.revision, disabled: true } }), "Host task disablement recorded as reported.")}>Confirm I disabled the host task</button></>}
      <p>Email fallback is not implemented. Native MCP Events and host scheduling support are separate capabilities.</p>
    </>}
  </section>;
}
