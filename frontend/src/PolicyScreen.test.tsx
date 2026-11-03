import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PolicyScreen } from "./PolicyScreen";
import {
  CATEGORIES,
  CAVEAT,
  POLICY_V3,
  jsonResponse,
  mockRoutes,
  policyResponse,
} from "./test-fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

const lastPut = (calls: { method: string; body: unknown }[]) =>
  calls.filter((c) => c.method === "PUT").at(-1)?.body as Record<string, unknown>;

describe("PolicyScreen", () => {
  it("first run shows defaults and approves with expected_version null", async () => {
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(null)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, version: 1, category_settings: {} })),
    });
    render(<PolicyScreen />);

    expect(
      await screen.findByRole("heading", { name: "Review and approve your contribution policy" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Share provisional facts")).toBeChecked();
    expect(screen.getByLabelText("Allow Public sharing for eligible categories")).not.toBeChecked();
    expect(screen.getByText(CAVEAT)).toBeInTheDocument();
    expect(screen.getByLabelText("Sizes")).toHaveValue("trusted_circle");

    await userEvent.click(screen.getByRole("button", { name: "Approve policy" }));

    expect(await screen.findByText("Contribution policy approved.")).toBeInTheDocument();
    expect(lastPut(calls)).toEqual({
      expected_version: null,
      public_opt_in: false,
      share_provisional: true,
      require_review_all: false,
      enrichment_enabled: true,
    });
    expect(screen.getByRole("heading", { name: "Contribution policy" })).toBeInTheDocument();
  });

  it("limits each audience select to the server's allowed_audiences", async () => {
    mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
    });
    render(<PolicyScreen />);

    for (const category of CATEGORIES) {
      const select = await screen.findByLabelText(category.label);
      const values = within(select)
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value);
      expect(values).toEqual(category.allowed_audiences);
      expect(values).not.toContain("public");
    }
  });

  it("saves with expected_version and keeps untouched category settings", async () => {
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, version: 4 })),
    });
    render(<PolicyScreen />);

    await userEvent.selectOptions(await screen.findByLabelText("Sizes"), "only_me");
    await userEvent.click(screen.getByLabelText("Review Sizes before sharing"));
    await userEvent.click(screen.getByLabelText("Allow my assistants to add facts"));
    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));

    expect(await screen.findByText("Policy saved (version 4).")).toBeInTheDocument();
    expect(lastPut(calls)).toEqual({
      expected_version: 3,
      public_opt_in: false,
      share_provisional: true,
      require_review_all: false,
      enrichment_enabled: false,
      category_settings: {
        interest: { audience: "trusted_circle" },
        size: { audience: "only_me", require_review: true },
      },
    });
  });

  it("on 409 loads the returned policy and saves against its version", async () => {
    const current = { ...POLICY_V3, version: 5, public_opt_in: true };
    const withPublic = CATEGORIES.map((c) =>
      c.id === "interest"
        ? { ...c, allowed_audiences: [...c.allowed_audiences, "public"], effective_audience: "public" }
        : c,
    );
    let puts = 0;
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
      "PUT /api/ui/profile/policy": () => {
        puts += 1;
        return puts === 1
          ? jsonResponse(409, {
              detail: "policy_version_conflict",
              policy: current,
              categories: withPublic,
            })
          : jsonResponse(200, policyResponse({ ...current, version: 6 }, withPublic));
      },
    });
    render(<PolicyScreen />);

    await userEvent.click(await screen.findByLabelText("Allow my assistants to add facts"));
    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Your policy changed elsewhere");
    // Nothing stale: the draft change is gone and the server's policy is shown.
    expect(screen.getByLabelText("Allow my assistants to add facts")).toBeChecked();
    expect(screen.getByLabelText("Allow Public sharing for eligible categories")).toBeChecked();
    expect(screen.getByLabelText("Interests and hobbies")).toHaveValue("public");

    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await screen.findByText("Policy saved (version 6).");
    expect(lastPut(calls).expected_version).toBe(5);
  });

  it("on 409 without categories re-reads the policy", async () => {
    let gets = 0;
    mockRoutes({
      "GET /api/ui/profile/policy": () => {
        gets += 1;
        return jsonResponse(
          200,
          policyResponse(gets === 1 ? POLICY_V3 : { ...POLICY_V3, version: 7, require_review_all: true }),
        );
      },
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(409, { detail: "policy_version_conflict", policy: { ...POLICY_V3, version: 7 } }),
    });
    render(<PolicyScreen />);
    await userEvent.click(await screen.findByRole("button", { name: "Save policy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Your policy changed elsewhere");
    await waitFor(() =>
      expect(screen.getByLabelText("Review every contribution before it is shared")).toBeChecked(),
    );
    expect(gets).toBe(2);
  });

  it("shows 400 field errors inline", async () => {
    mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(400, {
          detail: [
            { loc: ["category_settings", "size", "audience"], msg: "audience not allowed for this category" },
          ],
        }),
    });
    render(<PolicyScreen />);
    await userEvent.selectOptions(await screen.findByLabelText("Sizes"), "only_me");
    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));

    const message = await screen.findByText("audience not allowed for this category");
    const select = screen.getByLabelText("Sizes");
    expect(select).toHaveAttribute("aria-invalid", "true");
    expect(select).toHaveAttribute("aria-describedby", message.id);
  });

  it("shows a generic message on 403 and 503", async () => {
    for (const status of [403, 503]) {
      mockRoutes({
        "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
        "PUT /api/ui/profile/policy": () => jsonResponse(status, { detail: "raw internal text" }),
      });
      const { unmount } = render(<PolicyScreen />);
      await userEvent.click(await screen.findByRole("button", { name: "Save policy" }));
      const alert = await screen.findByRole("alert");
      expect(alert).not.toHaveTextContent("raw internal text");
      unmount();
    }
  });

  it("gives the sharing checkboxes accessible names", async () => {
    mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
    });
    render(<PolicyScreen />);
    for (const name of [
      "Share provisional facts",
      "Review every contribution before it is shared",
      "Allow my assistants to add facts",
      "Allow Public sharing for eligible categories",
    ]) {
      expect(await screen.findByRole("checkbox", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("checkbox", { name: "Share provisional facts" })).toHaveAccessibleDescription(
      CAVEAT,
    );
  });

  it("uses server defaults and audience labels when the response carries them", async () => {
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, {
          ...policyResponse(null),
          defaults: {
            public_opt_in: false,
            share_provisional: false,
            require_review_all: true,
            enrichment_enabled: true,
          },
          audiences: [
            { id: "only_me", label: "Only Me" },
            { id: "trusted_circle", label: "Trusted Circle" },
            { id: "connections", label: "Connections" },
          ],
        }),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, version: 1 })),
    });
    render(<PolicyScreen />);
    expect(await screen.findByRole("checkbox", { name: "Share provisional facts" })).not.toBeChecked();
    expect(
      within(screen.getByLabelText("Sizes")).getByRole("option", { name: "Trusted Circle" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve policy" }));
    await screen.findByText("Contribution policy approved.");
    expect(lastPut(calls)).toMatchObject({ share_provisional: false, require_review_all: true });
  });
});

// --- B1: only owner-changed (or already explicit) category values are sent ------------------

const OPTED_IN: typeof POLICY_V3 = { ...POLICY_V3, public_opt_in: true, category_settings: {} };
const OPTED_IN_CATEGORIES = CATEGORIES.map((c) =>
  c.public_eligible
    ? { ...c, effective_audience: "public", allowed_audiences: [...c.allowed_audiences, "public"] }
    : c,
);

describe("PolicyScreen category settings", () => {
  it("a review-only change sends no audience", async () => {
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, version: 4 })),
    });
    render(<PolicyScreen />);
    await userEvent.click(await screen.findByLabelText("Review Sizes before sharing"));
    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await screen.findByText("Policy saved (version 4).");

    expect(lastPut(calls).category_settings).toEqual({
      interest: { audience: "trusted_circle" }, // already explicit in the saved policy
      size: { require_review: true },
    });
  });

  it("withdrawing the opt-in with a review change sends no audience for an implicitly Public category", async () => {
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse(OPTED_IN, OPTED_IN_CATEGORIES)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, version: 4, category_settings: {} })),
    });
    render(<PolicyScreen />);
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Allow Public sharing for eligible categories" }),
    );
    await userEvent.click(screen.getByLabelText("Review Interests and hobbies before sharing"));
    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await screen.findByText("Policy saved (version 4).");

    expect(lastPut(calls)).toEqual({
      expected_version: 3,
      public_opt_in: false,
      share_provisional: true,
      require_review_all: false,
      enrichment_enabled: true,
      category_settings: { interest: { require_review: true } },
    });
  });

  it("withdrawing drops a saved Public audience the owner did not re-choose", async () => {
    const saved = { ...OPTED_IN, category_settings: { interest: { audience: "public" } } };
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse(saved, OPTED_IN_CATEGORIES)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, version: 4, category_settings: {} })),
    });
    render(<PolicyScreen />);
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Allow Public sharing for eligible categories" }),
    );
    await userEvent.click(screen.getByLabelText("Review Sizes before sharing"));
    await userEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await screen.findByText("Policy saved (version 4).");
    expect(lastPut(calls).category_settings).toEqual({ size: { require_review: true } });
  });

  it("first approval with opt-in and one review toggle sends no audience for that category", async () => {
    const { calls } = mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(null)),
      "PUT /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...OPTED_IN, version: 1 }, OPTED_IN_CATEGORIES)),
    });
    render(<PolicyScreen />);
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Allow Public sharing for eligible categories" }),
    );
    await userEvent.click(screen.getByLabelText("Review Interests and hobbies before sharing"));
    await userEvent.click(screen.getByRole("button", { name: "Approve policy" }));
    await screen.findByText("Contribution policy approved.");

    expect(lastPut(calls)).toEqual({
      expected_version: null,
      public_opt_in: true,
      share_provisional: true,
      require_review_all: false,
      enrichment_enabled: true,
      category_settings: { interest: { require_review: true } },
    });
  });
});

// --- B2: unsaved opt-in changes are previewed, not computed --------------------------------

describe("PolicyScreen opt-in preview", () => {
  it("enabling shows the notice, marks eligible rows and offers Public there only", async () => {
    mockRoutes({
      "GET /api/ui/profile/policy": () => jsonResponse(200, policyResponse(POLICY_V3)),
    });
    render(<PolicyScreen />);
    const optIn = await screen.findByRole("checkbox", {
      name: "Allow Public sharing for eligible categories",
    });
    expect(screen.queryByText(/after you save/)).toBeNull();
    expect(screen.queryByText("(updates after saving)")).toBeNull();

    await userEvent.click(optIn);

    expect(
      screen.getByText(
        "Categories eligible for Public sharing (marked) will be shared publicly after you save, unless you choose a narrower audience.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("(updates after saving)")).toBeInTheDocument();

    const optionValues = (label: string) =>
      within(screen.getByLabelText(label))
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value);
    // interest has an explicit saved audience (trusted_circle): not marked, but may choose Public.
    expect(optionValues("Interests and hobbies")).toContain("public");
    expect(screen.getByLabelText("Interests and hobbies")).not.toHaveAccessibleDescription(
      /Public after saving/,
    );
    // size is not public-eligible: no Public option, no marker.
    expect(optionValues("Sizes")).not.toContain("public");
    expect(screen.getByLabelText("Sizes")).not.toHaveAccessibleDescription(/Public/);

    await userEvent.click(optIn);
    expect(screen.queryByText(/after you save/)).toBeNull();
    expect(optionValues("Interests and hobbies")).not.toContain("public");
  });

  it("enabling marks implicit eligible rows 'Public after saving'", async () => {
    mockRoutes({
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse({ ...POLICY_V3, category_settings: {} })),
    });
    render(<PolicyScreen />);
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Allow Public sharing for eligible categories" }),
    );
    expect(screen.getByLabelText("Interests and hobbies")).toHaveAccessibleDescription(
      "Public after saving",
    );
    // Choosing an audience explicitly removes the marker for that row.
    await userEvent.selectOptions(screen.getByLabelText("Interests and hobbies"), "only_me");
    expect(screen.getByLabelText("Interests and hobbies")).not.toHaveAccessibleDescription(
      /Public after saving/,
    );
  });

  it("withdrawing also marks rows with an explicit saved Public audience", async () => {
    const saved = { ...OPTED_IN, category_settings: { interest: { audience: "public" } } };
    mockRoutes({
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse(saved, OPTED_IN_CATEGORIES)),
    });
    render(<PolicyScreen />);
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Allow Public sharing for eligible categories" }),
    );
    expect(screen.getByLabelText("Interests and hobbies")).toHaveAccessibleDescription(
      "No longer Public after saving",
    );
    // Re-choosing an audience for that row removes the marker.
    await userEvent.selectOptions(screen.getByLabelText("Interests and hobbies"), "connections");
    expect(screen.getByLabelText("Interests and hobbies")).not.toHaveAccessibleDescription(
      /Public after saving/,
    );
  });

  it("withdrawing shows the notice and marks implicitly Public rows", async () => {
    mockRoutes({
      "GET /api/ui/profile/policy": () =>
        jsonResponse(200, policyResponse(OPTED_IN, OPTED_IN_CATEGORIES)),
    });
    render(<PolicyScreen />);
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Allow Public sharing for eligible categories" }),
    );
    expect(screen.getByText("Public sharing will be withdrawn after you save.")).toBeInTheDocument();
    expect(screen.getByText("(updates after saving)")).toBeInTheDocument();
    expect(screen.getByLabelText("Interests and hobbies")).toHaveAccessibleDescription(
      "No longer Public after saving",
    );
    expect(screen.getByLabelText("Sizes")).not.toHaveAccessibleDescription(/Public/);
  });
});
