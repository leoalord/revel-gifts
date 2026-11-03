import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsScreen } from "./ConnectionsScreen";
import type {
  BlockedEntry,
  ConnectedAgent,
  ConnectionEntry,
  HandleInfo,
  IncomingRequest,
  OutgoingRequest,
} from "./connectionsApi";
import { AUDIENCES, jsonResponse, mockRoutes, policyResponse } from "./test-fixtures";
import type { Call } from "./test-fixtures";

const SAM = { display_name: "Sam Example", handle: "sam", avatar_url: null };
const JO = { display_name: "Jo Example", handle: "jo", avatar_url: null };

let handle: HandleInfo | null;
let incoming: IncomingRequest[];
let outgoing: OutgoingRequest[];
let connections: ConnectionEntry[];
let blocked: BlockedEntry[];
let agents: ConnectedAgent[];
let agentGets: number;

const ok = (body: unknown = { request_id: "r", status: "ok", tier: null }) => () =>
  jsonResponse(200, body);
const err = (status: number, code: string, detail: string) => () =>
  jsonResponse(status, { detail, code });

function routes(extra: Record<string, (call: Call) => Response> = {}) {
  return mockRoutes({
    "GET /api/connections/handle": () => jsonResponse(200, handle),
    "GET /api/connections/requests/incoming": () => jsonResponse(200, incoming),
    "GET /api/connections/requests/outgoing": () => jsonResponse(200, outgoing),
    "GET /api/connections": () => jsonResponse(200, connections),
    "GET /api/connections/blocked": () => jsonResponse(200, blocked),
    "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(null)),
    "GET /api/agents": () => {
      agentGets += 1;
      return jsonResponse(200, agents);
    },
    ...extra,
  });
}

const posts = (calls: Call[]) =>
  calls.filter((c) => c.method !== "GET").map((c) => ({ method: c.method, url: c.url, body: c.body }));

function row(target: string | RegExp | HTMLElement): HTMLElement {
  const el = target instanceof HTMLElement ? target : screen.getByText(target);
  const li = el.closest("li");
  if (!li) throw new Error("no row");
  return li;
}

beforeEach(() => {
  handle = { handle: "alex", discoverable: true, avatar_url: null };
  incoming = [
    {
      id: "req-1",
      status: "pending",
      created_at: "2026-11-02T10:00:00Z",
      requester: { ...SAM, assistant: { name: "Muse", client_id: "client_muse" } },
    },
  ];
  outgoing = [{ id: "req-2", status: "pending", created_at: "2026-11-02T09:00:00Z", target: JO }];
  connections = [
    {
      request_id: "req-3",
      tier: "connections",
      since: "2026-11-01T09:00:00Z",
      person: { display_name: "Pat Example", handle: "pat", avatar_url: null, assistant: null },
    },
  ];
  blocked = [
    {
      request_id: "req-4",
      blocked_at: "2026-10-30T09:00:00Z",
      person: { display_name: "Blocked Person", handle: "bp", avatar_url: null },
    },
  ];
  agents = [
    {
      delegation_id: "d-1",
      agent: { name: "Muse", client_id: "client_muse", label: "Muse" },
      status: "active",
      connected_at: "2026-11-01T08:00:00Z",
      disconnected_at: null,
    },
    {
      delegation_id: "d-0",
      agent: { name: null, client_id: "client_old", label: "Old helper" },
      status: "disconnected",
      connected_at: "2026-10-01T08:00:00Z",
      disconnected_at: "2026-10-02T08:00:00Z",
    },
  ];
  agentGets = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("incoming requests", () => {
  it("approves with the chosen tier in two clicks", async () => {
    const { calls } = routes({ "POST /api/connections/requests/req-1/approve": ok() });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Sam Example"));
    const approve = within(li).getByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();

    await userEvent.click(within(li).getByRole("radio", { name: "Trusted Circle" }));
    await userEvent.click(approve);

    expect(await screen.findByText("Approved Sam Example as Trusted Circle.")).toBeInTheDocument();
    expect(posts(calls)).toEqual([
      {
        method: "POST",
        url: "/api/connections/requests/req-1/approve",
        body: { tier: "trusted_circle" },
      },
    ]);
  });

  it("uses id-derived tier labels without server audiences", async () => {
    routes({ "GET /api/ui/profile/policy": () => jsonResponse(503, { detail: "x" }) });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Sam Example"));
    expect(within(li).getByRole("radio", { name: "Trusted circle" })).toBeInTheDocument();
    expect(AUDIENCES.length).toBeGreaterThan(0);
  });

  it("shows the requester's assistant as a registered app name, as text", async () => {
    incoming[0]!.requester.assistant = { name: "<b>Muse</b>", client_id: "c" };
    const { container } = (routes(), render(<ConnectionsScreen />));
    expect(await screen.findByText(/via <b>Muse<\/b>/)).toBeInTheDocument();
    expect(container.querySelector(".person b")).toBeNull();
  });

  it("declines, and blocks after confirmation", async () => {
    const { calls } = routes({
      "POST /api/connections/requests/req-1/decline": ok(),
      "POST /api/connections/requests/req-1/block": ok(),
    });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Sam Example"));
    await userEvent.click(within(li).getByRole("button", { name: "Decline" }));
    await screen.findByText("Declined Sam Example.");
    await userEvent.click(within(row("Sam Example")).getByRole("button", { name: "Block" }));
    expect(posts(calls)).toHaveLength(1);
    await userEvent.click(within(row("Sam Example")).getByRole("button", { name: "Confirm block" }));
    await screen.findByText("Blocked Sam Example.");
    expect(posts(calls).map((c) => c.url)).toEqual([
      "/api/connections/requests/req-1/decline",
      "/api/connections/requests/req-1/block",
    ]);
  });

  it("shows 429 as a fixed message", async () => {
    routes({
      "POST /api/connections/requests/req-1/decline": err(429, "rate_limited", "raw server text"),
    });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Sam Example"));
    await userEvent.click(within(li).getByRole("button", { name: "Decline" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Too many requests, try again shortly.");
    expect(alert).not.toHaveTextContent("raw server text");
  });

  it("shows 503 as a generic retryable message", async () => {
    routes({
      "POST /api/connections/requests/req-1/decline": err(503, "unavailable", "internal detail"),
    });
    render(<ConnectionsScreen />);
    await userEvent.click(
      within(row(await screen.findByText("Sam Example"))).getByRole("button", { name: "Decline" }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("temporarily unavailable");
    expect(alert).not.toHaveTextContent("internal detail");
  });
});

describe("outgoing requests", () => {
  it("cancels a pending request", async () => {
    const { calls } = routes({ "POST /api/connections/requests/req-2/cancel": ok() });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Jo Example"));
    await userEvent.click(within(li).getByRole("button", { name: "Cancel request" }));
    await screen.findByText("Cancelled the request to Jo Example.");
    expect(posts(calls).map((c) => c.url)).toEqual(["/api/connections/requests/req-2/cancel"]);
  });

  it("requests by handle", async () => {
    const { calls } = routes({
      "POST /api/connections/requests": () =>
        jsonResponse(200, {
          target: JO,
          request: { id: "req-9", status: "pending", created_at: "2026-11-02T11:00:00Z" },
          created: true,
        }),
    });
    render(<ConnectionsScreen />);
    await userEvent.type(await screen.findByLabelText("Request by handle"), "@jo");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    await screen.findByText("Request sent to Jo Example.");
    expect(posts(calls)).toEqual([
      { method: "POST", url: "/api/connections/requests", body: { handle: "jo" } },
    ]);
  });

  it("shows the server's generic message for target_not_found", async () => {
    const generic = "No Revel user with that handle is available to connect with";
    routes({ "POST /api/connections/requests": err(404, "target_not_found", generic) });
    render(<ConnectionsScreen />);
    await userEvent.type(await screen.findByLabelText("Request by handle"), "nobody");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(generic);
  });
});

describe("RG-48 follow-ups", () => {
  it("gives the approve radios label-associated accessible names", async () => {
    routes();
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Sam Example"));
    expect(li).toHaveTextContent(/Sam Example \(@sam\).* wants to see your profile\./);
    for (const name of ["Connections", "Trusted Circle"]) {
      const radio = within(li).getByRole("radio", { name });
      expect(radio).toHaveAccessibleName(name);
      expect(li.querySelector(`label[for="${radio.id}"]`)).toHaveTextContent(name);
    }
  });

  it("blocks a current connection after confirmation", async () => {
    const { calls } = routes({ "POST /api/connections/requests/req-3/block": ok() });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Pat Example"));
    await userEvent.click(within(li).getByRole("button", { name: "Block" }));
    expect(posts(calls)).toEqual([]);
    await userEvent.click(within(li).getByRole("button", { name: "Confirm block" }));
    await screen.findByText("Blocked Pat Example.");
    expect(posts(calls).map((c) => c.url)).toEqual(["/api/connections/requests/req-3/block"]);
  });

  it("shows the server's 409 detail when a handle must be set first", async () => {
    const detail = "Set your handle before sending connection requests";
    routes({ "POST /api/connections/requests": err(409, "handle_required", detail) });
    render(<ConnectionsScreen />);
    await userEvent.type(await screen.findByLabelText("Request by handle"), "jo");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });
});

describe("connections and blocked", () => {
  it("changes tier", async () => {
    const { calls } = routes({ "POST /api/connections/requests/req-3/tier": ok() });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Pat Example"));
    await userEvent.selectOptions(within(li).getByLabelText("Tier for Pat Example"), "trusted_circle");
    await userEvent.click(within(li).getByRole("button", { name: "Change tier" }));
    await screen.findByText("Pat Example is now Trusted Circle.");
    expect(posts(calls)).toEqual([
      {
        method: "POST",
        url: "/api/connections/requests/req-3/tier",
        body: { tier: "trusted_circle" },
      },
    ]);
  });

  it("revokes after confirmation", async () => {
    const { calls } = routes({ "POST /api/connections/requests/req-3/revoke": ok() });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Pat Example"));
    await userEvent.click(within(li).getByRole("button", { name: "Revoke" }));
    expect(posts(calls)).toEqual([]);
    await userEvent.click(within(li).getByRole("button", { name: "Confirm revoke" }));
    await screen.findByText("Revoked Pat Example.");
    expect(posts(calls).map((c) => c.url)).toEqual(["/api/connections/requests/req-3/revoke"]);
  });

  it("unblocks", async () => {
    const { calls } = routes({ "POST /api/connections/requests/req-4/unblock": ok() });
    render(<ConnectionsScreen />);
    const li = row(await screen.findByText("Blocked Person"));
    await userEvent.click(within(li).getByRole("button", { name: "Unblock" }));
    await screen.findByText("Unblocked Blocked Person.");
    expect(posts(calls).map((c) => c.url)).toEqual(["/api/connections/requests/req-4/unblock"]);
  });
});

describe("handle", () => {
  it("sets a first handle and the discoverable flag", async () => {
    handle = null;
    const { calls } = routes({
      "PUT /api/connections/handle": () =>
        jsonResponse(200, { handle: "alex_w", discoverable: true, avatar_url: null }),
    });
    render(<ConnectionsScreen />);
    await userEvent.type(await screen.findByLabelText("Handle"), "alex_w");
    await userEvent.click(screen.getByLabelText("Let people find me by my handle"));
    await userEvent.click(screen.getByRole("button", { name: "Set handle" }));
    await screen.findByText("Handle saved: @alex_w.");
    expect(posts(calls)).toEqual([
      {
        method: "PUT",
        url: "/api/connections/handle",
        body: { handle: "alex_w", discoverable: true },
      },
    ]);
  });

  it.each([
    [409, "handle_unavailable", "That handle is unavailable"],
    [422, "invalid_handle", "Handles are 3-30 characters: lowercase letters, digits and underscores"],
  ])("shows %s %s inline on the handle field", async (status, code, detail) => {
    routes({ "PUT /api/connections/handle": err(status, code, detail) });
    render(<ConnectionsScreen />);
    const input = await screen.findByLabelText("Handle");
    await userEvent.clear(input);
    await userEvent.type(input, "Taken!");
    await userEvent.click(screen.getByRole("button", { name: "Save handle" }));
    await waitFor(() => expect(input).toHaveAttribute("aria-invalid", "true"));
    expect(input).toHaveAccessibleDescription(detail);
  });
});

describe("connected assistants", () => {
  it("lists active and past assistants with the label as text", async () => {
    agents[0]!.agent.label = "<img src=x onerror=alert(1)>";
    routes();
    const { container } = render(<ConnectionsScreen />);
    const active = await screen.findByRole("list", { name: "Active assistants" });
    expect(within(active).getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(within(active).getByText("(registered app name)")).toBeInTheDocument();
    expect(container.querySelector(".panel img")).toBeNull();
    const history = screen.getByRole("list", { name: "Disconnected assistants" });
    expect(within(history).getByText("Old helper")).toBeInTheDocument();
    expect(within(history).queryByRole("button")).toBeNull();
  });

  it("disconnects after confirmation", async () => {
    const { calls } = routes({
      "POST /api/agents/d-1/disconnect": () => {
        agents = agents.map((a) =>
          a.delegation_id === "d-1"
            ? { ...a, status: "disconnected", disconnected_at: "2026-11-02T12:00:00Z" }
            : a,
        );
        return jsonResponse(200, agents[0]);
      },
    });
    render(<ConnectionsScreen />);
    const active = await screen.findByRole("list", { name: "Active assistants" });
    await userEvent.click(within(active).getByRole("button", { name: "Disconnect" }));
    await userEvent.click(within(active).getByRole("button", { name: "Confirm disconnect" }));
    await screen.findByText("Disconnected Muse.");
    expect(posts(calls)).toEqual([
      { method: "POST", url: "/api/agents/d-1/disconnect", body: undefined },
    ]);
    await waitFor(() => expect(screen.getByText("No assistants are connected.")).toBeInTheDocument());
  });

  it("refreshes the list on 404", async () => {
    routes({
      "POST /api/agents/d-1/disconnect": () => {
        agents = agents.filter((a) => a.delegation_id !== "d-1");
        return jsonResponse(404, { detail: "Not found" });
      },
    });
    render(<ConnectionsScreen />);
    const active = await screen.findByRole("list", { name: "Active assistants" });
    expect(agentGets).toBe(1);
    await userEvent.click(within(active).getByRole("button", { name: "Disconnect" }));
    await userEvent.click(within(active).getByRole("button", { name: "Confirm disconnect" }));
    expect(await screen.findByText(/already disconnected/)).toBeInTheDocument();
    await waitFor(() => expect(agentGets).toBe(2));
    await waitFor(() => expect(screen.getByText("No assistants are connected.")).toBeInTheDocument());
  });
});
