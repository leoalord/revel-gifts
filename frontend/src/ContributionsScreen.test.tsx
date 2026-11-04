import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContributionsScreen } from "./ContributionsScreen";
import type { Fact, RecentEvent } from "./profileApi";
import {
  CATEGORIES,
  POLICY_V3,
  fact,
  jsonResponse,
  legacyPolicyResponse,
  mockRoutes,
  policyResponse,
  profile,
} from "./test-fixtures";
import type { Call } from "./test-fixtures";

const ACTIVE = fact();
const PENDING = fact({
  id: "f-2",
  category: "size",
  statement: "Wears a medium jacket",
  details: { item: "jacket", size: "M" },
  audience: "trusted_circle",
  status: "pending_review",
  evidence_type: "inferred",
  confidence: "low",
  observed_at: "2026-10-01",
  observed_precision: "month",
});

let facts: Fact[];
let profileGets: number;

function routes(extra: Record<string, (call: Call) => Response> = {}) {
  return mockRoutes({
    "GET /api/ui/profile": () => {
      profileGets += 1;
      return jsonResponse(200, profile(facts));
    },
    "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
    ...extra,
  });
}

function item(statement: string): HTMLElement {
  const li = screen.getByText(statement).closest("li");
  if (!li) throw new Error(`no fact item for ${statement}`);
  return li;
}

beforeEach(() => {
  facts = [ACTIVE, PENDING];
  profileGets = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ContributionsScreen at a glance", () => {
  it("leads with pending count and jumps to pending facts via ?status=", async () => {
    const { calls } = routes();
    render(<ContributionsScreen />);
    expect(await screen.findByRole("heading", { name: "At a glance" })).toBeInTheDocument();
    expect(screen.getByText("Facts by assigned audience")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Category group"), "tastes");
    expect(screen.getByText("Enjoys bouldering")).toBeInTheDocument();
    expect(screen.queryByText("Wears a medium jacket")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review pending facts" }));
    expect(screen.getByLabelText("Category group")).toHaveValue("");
    await waitFor(() =>
      expect(calls.some((c) => c.url === "/api/ui/profile?status=pending_review")).toBe(true),
    );
    expect(screen.getByLabelText("Show")).toHaveValue("pending_review");
  });
});

describe("ContributionsScreen", () => {
  it("shows the summary and facts with status, evidence, date and contributor", async () => {
    routes();
    render(<ContributionsScreen />);

    const pending = await screen.findByText("Wears a medium jacket");
    expect(screen.getByText("waiting for your review.", { exact: false })).toBeInTheDocument();
    const recent = screen.getByRole("table");
    expect(within(recent).getByText("Muse")).toBeInTheDocument();
    expect(within(recent).getByText("Automatic")).toBeInTheDocument();

    const li = pending.closest("li")!;
    expect(within(li).getByText("Sizes")).toBeInTheDocument();
    expect(within(li).getByText("Pending your review")).toBeInTheDocument();
    expect(within(li).getByText("Inferred, low confidence")).toBeInTheDocument();
    expect(within(li).getByText("2026-10")).toBeInTheDocument();
    expect(within(li).getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(within(item("Enjoys bouldering")).queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("renders statements as text, never as HTML", async () => {
    facts = [fact({ statement: "<img src=x onerror=alert(1)>", contributor: "<b>Muse</b>" })];
    routes();
    const { container } = render(<ContributionsScreen />);
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector(".facts img, .facts b")).toBeNull();
  });

  it("filters by status through the ?status= query", async () => {
    const { calls } = routes();
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    await userEvent.selectOptions(screen.getByLabelText("Show"), "pending_review");
    await waitFor(() =>
      expect(calls.some((c) => c.url === "/api/ui/profile?status=pending_review")).toBe(true),
    );
  });

  it("changes the audience with PATCH {audience}, options limited to the server list", async () => {
    const { calls } = routes({
      "PATCH /api/ui/profile/facts/f-1": (call) => {
        const updated = { ...ACTIVE, audience: "only_me", updated_at: "2026-11-02T00:00:00Z" };
        facts = [updated, PENDING];
        expect(call.body).toEqual({ audience: "only_me" });
        return jsonResponse(200, updated);
      },
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    const select = within(li).getByLabelText(/^Change audience for /);
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value),
    ).toEqual(CATEGORIES[0]!.allowed_audiences);

    await userEvent.selectOptions(select, "only_me");
    await userEvent.click(within(li).getByRole("button", { name: "Change audience" }));

    expect(await screen.findByText("Audience changed to Only me.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ audience: "only_me" });
  });

  it("corrects a fact and shows the replacement", async () => {
    const replacement = fact({
      id: "f-3",
      statement: "Enjoys bouldering and sport climbing",
      supersedes_id: "f-1",
      contributor: "you",
      contributed_via: "ui",
    });
    const { calls } = routes({
      "PATCH /api/ui/profile/facts/f-1": () => {
        facts = [replacement, PENDING];
        return jsonResponse(200, replacement);
      },
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    await userEvent.click(within(li).getByRole("button", { name: "Correct" }));
    const statement = within(li).getByLabelText("Statement");
    await userEvent.clear(statement);
    await userEvent.type(statement, "Enjoys bouldering and sport climbing");
    await userEvent.click(within(li).getByRole("button", { name: "Save correction" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Correction saved. Replacement: “Enjoys bouldering and sport climbing”.",
    );
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
      statement: "Enjoys bouldering and sport climbing",
    });
    expect(screen.queryByText("Enjoys bouldering")).toBeNull();
    expect(item("Enjoys bouldering and sport climbing")).toBeInTheDocument();
  });

  it("renders details fields from details_schema in the correction form", async () => {
    const { calls } = routes({
      "PATCH /api/ui/profile/facts/f-2": () => jsonResponse(200, { ...PENDING, id: "f-4" }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Wears a medium jacket");
    const li = item("Wears a medium jacket");
    await userEvent.click(within(li).getByRole("button", { name: "Correct" }));
    const size = within(li).getByLabelText("Size (required)");
    expect(size).toHaveValue("M");
    expect(within(item("Enjoys bouldering")).queryByText("Details")).toBeNull();
    await userEvent.clear(size);
    await userEvent.type(size, "L");
    await userEvent.click(within(li).getByRole("button", { name: "Save correction" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
        details: { item: "jacket", size: "L" },
      }),
    );
  });

  it("shows 400 field errors inline on a correction", async () => {
    routes({
      "PATCH /api/ui/profile/facts/f-1": () =>
        jsonResponse(400, { detail: [{ loc: ["statement"], msg: "statement looks like a street address" }] }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    await userEvent.click(within(li).getByRole("button", { name: "Correct" }));
    await userEvent.type(within(li).getByLabelText("Statement"), " at 12 Main Street");
    await userEvent.click(within(li).getByRole("button", { name: "Save correction" }));

    const message = await within(li).findByText("statement looks like a street address");
    expect(within(li).getByLabelText("Statement")).toHaveAttribute("aria-describedby", message.id);
    expect(within(li).getByLabelText("Statement")).toHaveAttribute("aria-invalid", "true");
  });

  it("removes a fact after confirmation with DELETE", async () => {
    const { calls } = routes({
      "DELETE /api/ui/profile/facts/f-1": () => {
        facts = [PENDING];
        return jsonResponse(200, { ...ACTIVE, status: "removed" });
      },
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    await userEvent.click(within(li).getByRole("button", { name: "Remove" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(li).getByRole("button", { name: "Confirm remove" }));

    expect(await screen.findByText("Fact removed.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Enjoys bouldering")).toBeNull());
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.url)).toEqual([
      "/api/ui/profile/facts/f-1",
    ]);
  });

  it.each([
    ["Approve", "approve", "active", "Fact approved."],
    ["Reject", "reject", "removed", "Fact rejected."],
  ] as const)("%s a pending fact", async (button, path, status, notice) => {
    const { calls } = routes({
      [`POST /api/ui/profile/facts/f-2/${path}`]: () => {
        facts = status === "active" ? [ACTIVE, { ...PENDING, status }] : [ACTIVE];
        return jsonResponse(200, { ...PENDING, status });
      },
    });
    render(<ContributionsScreen />);
    await screen.findByText("Wears a medium jacket");
    await userEvent.click(within(item("Wears a medium jacket")).getByRole("button", { name: button }));
    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.url === `/api/ui/profile/facts/f-2/${path}`)).toBe(
      true,
    );
  });

  it("on a fact 409 replaces the fact with the returned one and shows a notice", async () => {
    const current = { ...PENDING, status: "active" as const, updated_at: "2026-11-02T01:00:00Z" };
    routes({
      "POST /api/ui/profile/facts/f-2/approve": () =>
        jsonResponse(409, { detail: "fact_not_pending", fact: current }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Wears a medium jacket");
    await userEvent.click(within(item("Wears a medium jacket")).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("status")).toHaveTextContent("This fact changed elsewhere");
    const li = item("Wears a medium jacket");
    expect(within(li).queryByText("Pending your review")).toBeNull();
    expect(within(li).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByText("fact_not_pending")).toBeNull();
  });

  it("on a fact 404 refreshes the list", async () => {
    routes({
      "DELETE /api/ui/profile/facts/f-1": () => {
        facts = [PENDING];
        return jsonResponse(404, { detail: "fact_not_found" });
      },
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    expect(profileGets).toBe(1);
    const li = item("Enjoys bouldering");
    await userEvent.click(within(li).getByRole("button", { name: "Remove" }));
    await userEvent.click(within(li).getByRole("button", { name: "Confirm remove" }));

    expect(await screen.findByText(/no longer available/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Enjoys bouldering")).toBeNull());
    expect(profileGets).toBe(2);
  });

  it("shows a generic message on 403 without server text", async () => {
    routes({
      "POST /api/ui/profile/facts/f-2/approve": () => jsonResponse(403, { detail: "not_allowed" }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Wears a medium jacket");
    const li = item("Wears a medium jacket");
    await userEvent.click(within(li).getByRole("button", { name: "Approve" }));
    expect(await within(li).findByRole("alert")).toHaveTextContent("You are not allowed to do that.");
  });

  it("offers no owner actions on superseded or removed facts", async () => {
    facts = [
      fact({ id: "f-5", statement: "Old statement", status: "superseded" }),
      fact({ id: "f-6", statement: "Removed statement", status: "removed" }),
    ];
    routes();
    render(<ContributionsScreen />);
    for (const statement of ["Old statement", "Removed statement"]) {
      const li = (await screen.findByText(statement)).closest("li")!;
      expect(within(li).queryByRole("button")).toBeNull();
      expect(within(li).queryByLabelText(/^Change audience for /)).toBeNull();
    }
  });

  it("defaults the precision to day when a date is added to an undated fact", async () => {
    facts = [fact({ observed_at: null, observed_precision: "unknown" })];
    const { calls } = routes({
      "PATCH /api/ui/profile/facts/f-1": () => jsonResponse(200, { ...ACTIVE, id: "f-7" }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    await userEvent.click(within(li).getByRole("button", { name: "Correct" }));
    const precision = within(li).getByLabelText("Date precision");
    expect(precision).toBeDisabled();
    await userEvent.type(within(li).getByLabelText("Observed on"), "2026-09-14");
    expect(precision).toHaveValue("day");
    expect(precision).toBeEnabled();
    await userEvent.selectOptions(precision, "month");
    await userEvent.click(within(li).getByRole("button", { name: "Save correction" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
        observed_at: "2026-09-14",
        observed_precision: "month",
      }),
    );
  });

  it("maps a 400 at loc [body, statement] to the statement field", async () => {
    routes({
      "PATCH /api/ui/profile/facts/f-1": () =>
        jsonResponse(400, {
          detail: [{ loc: ["body", "statement"], msg: "statement must be 1-500 characters" }],
        }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    await userEvent.click(within(li).getByRole("button", { name: "Correct" }));
    await userEvent.clear(within(li).getByLabelText("Statement"));
    await userEvent.click(within(li).getByRole("button", { name: "Save correction" }));
    const statement = within(li).getByLabelText("Statement");
    await waitFor(() => expect(statement).toHaveAttribute("aria-invalid", "true"));
    expect(statement).toHaveAccessibleDescription("statement must be 1-500 characters");
  });

  it("does not resend details whose server key order differs", async () => {
    facts = [{ ...PENDING, status: "active", details: { size: "M", item: "jacket" } }];
    const { calls } = routes({
      "PATCH /api/ui/profile/facts/f-2": () => jsonResponse(200, { ...PENDING, id: "f-12" }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Wears a medium jacket");
    const li = item("Wears a medium jacket");
    await userEvent.click(within(li).getByRole("button", { name: "Correct" }));
    await userEvent.type(within(li).getByLabelText("Statement"), "!");
    await userEvent.click(within(li).getByRole("button", { name: "Save correction" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
        statement: "Wears a medium jacket!",
      }),
    );
  });

  describe("per-fact audience (B3)", () => {
    const optionValues = (li: HTMLElement) =>
      within(within(li).getByLabelText(/^Change audience for /))
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value);

    it("shows a fact outside its category's policy bounds with its current audience selected", async () => {
      facts = [fact({ id: "f-8", category: "size", statement: "Shoe size 42", audience: "connections" })];
      routes();
      render(<ContributionsScreen />);
      const li = (await screen.findByText("Shoe size 42")).closest("li")!;
      const select = within(li).getByLabelText(/^Change audience for /);
      expect(select).toHaveValue("connections");
      expect(within(select).getByRole("option", { selected: true })).toHaveTextContent("Connections");
      // size's allowed_audiences lacks Public: the server's list without Public.
      expect(optionValues(li)).toEqual(["only_me", "trusted_circle", "connections"]);
    });

    it.each([
      ["only_me", "trusted_circle"],
      ["trusted_circle", "connections"],
    ])("widens a size fact from %s to %s with PATCH {audience}", async (from, to) => {
      facts = [fact({ id: "f-9", category: "size", statement: "Hat size M", audience: from })];
      const { calls } = routes({
        "PATCH /api/ui/profile/facts/f-9": (call) => {
          const updated = { ...facts[0]!, audience: to, updated_at: "2026-11-02T03:00:00Z" };
          facts = [updated];
          return jsonResponse(200, { ...updated, ...(call.body as object) });
        },
      });
      render(<ContributionsScreen />);
      const li = (await screen.findByText("Hat size M")).closest("li")!;
      await userEvent.selectOptions(within(li).getByLabelText(/^Change audience for /), to);
      await userEvent.click(within(li).getByRole("button", { name: "Change audience" }));
      await screen.findByText(/^Audience changed to/);
      expect(calls.filter((c) => c.method === "PATCH").map((c) => c.body)).toEqual([{ audience: to }]);
    });

    it("offers Public only when the category's allowed_audiences includes it", async () => {
      const optedIn = CATEGORIES.map((c) =>
        c.id === "interest" ? { ...c, allowed_audiences: [...c.allowed_audiences, "public"] } : c,
      );
      facts = [ACTIVE, PENDING];
      mockRoutes({
        "GET /api/ui/profile": () => jsonResponse(200, profile(facts)),
        "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3, optedIn)),
      });
      render(<ContributionsScreen />);
      await screen.findByText("Enjoys bouldering");
      expect(optionValues(item("Enjoys bouldering"))).toContain("public");
      expect(optionValues(item("Wears a medium jacket"))).not.toContain("public");
    });

    it("falls back to the category's allowed_audiences plus the current one without `audiences`", async () => {
      facts = [fact({ id: "f-8", category: "size", statement: "Shoe size 42", audience: "connections" })];
      mockRoutes({
        "GET /api/ui/profile": () => jsonResponse(200, profile(facts)),
        "GET /api/ui/profile/policy": () => jsonResponse(200, legacyPolicyResponse(POLICY_V3)),
      });
      render(<ContributionsScreen />);
      const li = (await screen.findByText("Shoe size 42")).closest("li")!;
      expect(within(li).getByLabelText(/^Change audience for /)).toHaveValue("connections");
      expect(optionValues(li)).toEqual(["only_me", "trusted_circle", "connections"]);
    });

    it("shows a server refusal inline and reverts the select", async () => {
      facts = [
        fact({
          id: "f-10",
          category: "sensitive",
          statement: "Volunteered note",
          audience: "only_me",
          status: "active",
        }),
      ];
      const { calls } = routes({
        "PATCH /api/ui/profile/facts/f-10": () =>
          jsonResponse(400, {
            detail: [{ loc: ["body", "audience"], msg: "sensitive facts are always Only me" }],
          }),
      });
      render(<ContributionsScreen />);
      const li = (await screen.findByText("Volunteered note")).closest("li")!;
      const select = within(li).getByLabelText(/^Change audience for /);
      // The server's list (minus Public): widening is offered and validated by the server.
      expect(optionValues(li)).toEqual(["only_me", "trusted_circle", "connections"]);
      await userEvent.selectOptions(select, "connections");
      await userEvent.click(within(li).getByRole("button", { name: "Change audience" }));

      const error = await within(li).findByText("sensitive facts are always Only me");
      expect(select).toHaveValue("only_me");
      expect(select).toHaveAttribute("aria-describedby", error.id);
      expect(calls.filter((c) => c.method === "PATCH").map((c) => c.body)).toEqual([
        { audience: "connections" },
      ]);
    });

    it("shows a form-level (loc [body]) refusal on the audience control too", async () => {
      facts = [fact({ id: "f-11", category: "sensitive", statement: "Note two", audience: "only_me" })];
      routes({
        "PATCH /api/ui/profile/facts/f-11": () =>
          jsonResponse(400, { detail: [{ loc: ["body"], msg: "audience not allowed" }] }),
      });
      render(<ContributionsScreen />);
      const li = (await screen.findByText("Note two")).closest("li")!;
      await userEvent.selectOptions(within(li).getByLabelText(/^Change audience for /), "trusted_circle");
      await userEvent.click(within(li).getByRole("button", { name: "Change audience" }));
      expect(await within(li).findByText("audience not allowed")).toBeInTheDocument();
      expect(within(li).getByLabelText(/^Change audience for /)).toHaveValue("only_me");
    });
  });

  it("uses server audience labels when the policy response carries them", async () => {
    mockRoutes({
      "GET /api/ui/profile": () => jsonResponse(200, profile(facts)),
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, {
          ...policyResponse(POLICY_V3),
          audiences: [
            { id: "only_me", label: "Only Me" },
            { id: "trusted_circle", label: "Trusted Circle" },
            { id: "connections", label: "My Connections" },
          ],
        }),
    });
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    expect(within(li).getByText("My Connections", { selector: "dd" })).toBeInTheDocument();
    expect(within(li).getByRole("option", { name: "Only Me" })).toBeInTheDocument();
  });
});

describe("ContributionsScreen readability (RG-58)", () => {
  function event(i: number, overrides: Partial<RecentEvent> = {}): RecentEvent {
    return {
      claim_id: `f-${i}`,
      action: "auto_accepted",
      category: "interest",
      audience: null,
      acceptance: "automatic",
      actor: "Muse",
      occurred_at: `2026-10-${String(10 + i).padStart(2, "0")}T12:00:00Z`,
      ...overrides,
    };
  }

  function withRecent(recent: RecentEvent[]) {
    const base = profile(facts);
    return mockRoutes({
      "GET /api/ui/profile": () =>
        jsonResponse(200, { ...base, summary: { ...base.summary, recent } }),
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
    });
  }

  it("orders the page: at a glance, then facts, then recent activity", async () => {
    routes();
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const headings = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(["At a glance", "Facts", "Recent activity"]);
  });

  it("shows the latest six events and expands to the full fetched list with Show all", async () => {
    const recent = Array.from({ length: 9 }, (_, i) => event(i));
    const { calls } = withRecent(recent);
    render(<ContributionsScreen />);
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 6);

    const toggle = screen.getByRole("button", { name: "Show all (9)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const before = calls.length;
    await userEvent.click(toggle);
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 9);
    expect(screen.getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
    // Display-only: expanding re-requests nothing.
    expect(calls.length).toBe(before);

    await userEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 6);
  });

  it("offers no Show all when every event already fits", async () => {
    withRecent([event(1), event(2)]);
    render(<ContributionsScreen />);
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /^Show all/ })).toBeNull();
  });

  it("hides activity columns that carry no information and shows the action as a badge", async () => {
    withRecent([event(1), event(2, { acceptance: null, action: "created" })]);
    render(<ContributionsScreen />);
    const table = await screen.findByRole("table");
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(["When", "Action", "Category", "Who", "Acceptance"]);
    expect(within(table).getByText("Created")).toHaveClass("badge");
  });

  it("marks agent names as registered app names (O1) but not the owner's own entries", async () => {
    facts = [ACTIVE, fact({ id: "f-20", statement: "Owner note", contributor: "you", contributed_via: "ui" })];
    withRecent([event(1), event(2, { actor: "you", acceptance: "explicit", action: "approved" })]);
    render(<ContributionsScreen />);

    await screen.findByText("Enjoys bouldering");
    const agentFact = item("Enjoys bouldering");
    const qualifier = within(agentFact).getByText("(registered app name)");
    expect(qualifier).toHaveAttribute("title", expect.stringContaining("not a verified identity"));
    expect(within(agentFact).getByText("Muse")).toBeInTheDocument();
    expect(within(item("Owner note")).queryByText("(registered app name)")).toBeNull();

    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("(registered app name)")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("you")).toBeInTheDocument();
    expect(within(rows[1]!).queryByText("(registered app name)")).toBeNull();
  });

  it("omits an unknown observed date and keeps the provisional caveat", async () => {
    facts = [fact({ observed_at: null, observed_precision: "unknown", status: "provisional", provisional: true })];
    routes();
    render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    const li = item("Enjoys bouldering");
    expect(within(li).queryByText("Date unknown")).toBeNull();
    expect(within(li).queryByText("Observed")).toBeNull();
    expect(within(li).getByText("Provisional")).toHaveClass("badge");
    expect(within(li).getByText(/Shared with a caveat/)).toBeInTheDocument();
  });

  it("keeps the pending callout prominent only when facts wait for review", async () => {
    routes();
    const { container, unmount } = render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    expect(container.querySelector(".glance-pending.has-pending")).not.toBeNull();
    unmount();

    const base = profile(facts);
    mockRoutes({
      "GET /api/ui/profile": () =>
        jsonResponse(200, { ...base, summary: { ...base.summary, pending_review: 0 } }),
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
    });
    const second = render(<ContributionsScreen />);
    await screen.findByText("Enjoys bouldering");
    expect(second.container.querySelector(".glance-pending.has-pending")).toBeNull();
    expect(screen.queryByRole("button", { name: "Review pending facts" })).toBeNull();
  });
});
