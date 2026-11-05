import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock, MockInstance } from "vitest";

import { App } from "./App";
import { LOGIN_MARKER_KEY, browserNavigation } from "./api";

const SESSION = {
  user: {
    id: "00000000-0000-4000-8000-000000000001",
    workos_user_id: "user_synthetic",
    email: "alex@example.test",
    display_name: "Alex Example",
  },
  agent: null,
  via: "ui",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let agentsFetchMock: Mock<typeof fetch>;
let fetchMock: Mock<typeof fetch>;
let assignSpy: MockInstance<(url: string) => void>;
let reloadSpy: MockInstance<() => void>;

beforeEach(() => {
  fetchMock = vi.fn<typeof fetch>();
  agentsFetchMock = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse(200, []));
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
    (input === "/api/agents" ? agentsFetchMock : fetchMock)(input, init),
  );
  assignSpy = vi.spyOn(browserNavigation, "assign").mockImplementation(() => {});
  reloadSpy = vi.spyOn(browserNavigation, "reload").mockImplementation(() => {});
  window.history.replaceState(null, "", "/app/");
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("session bootstrap", () => {
  it("shows the session loading state", () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => {}));
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });

  it("requests /auth/session same-origin and shows the signed-in user", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, SESSION));
    render(<App />);

    expect(await screen.findByText("Signed in as Alex Example")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Make it personal." })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic demo data")).not.toBeInTheDocument();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/auth/session");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.method).toBe("GET");
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("shows active assistant registrations and prioritizes setup without assuming Cursor identity", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, SESSION));
    agentsFetchMock.mockResolvedValueOnce(jsonResponse(200, [
      { delegation_id: "d1", agent: { label: "Cursor" }, status: "active", connected_at: "2026-11-02T00:00:00Z" },
      { delegation_id: "d2", agent: { label: "Muse" }, status: "revoked", connected_at: "2026-11-02T00:00:00Z" },
    ]));
    render(<App />);

    const active = await screen.findByRole("list", { name: "Active assistants" });
    expect(within(active).getByText("Cursor")).toBeInTheDocument();
    expect(within(active).queryByText("Muse")).not.toBeInTheDocument();
    expect(within(active).queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
    const setup = screen.getByRole("heading", { name: "Connect another assistant" }).parentElement!;
    const summaries = [...setup.querySelectorAll("details summary")].map((element) => element.textContent);
    expect(summaries[1]).toBe("Muse");
    expect(summaries.slice(-2)).toEqual(["Grok Bot · Possibly already connected", "Cursor · Possibly already connected"]);
  });

  it("falls back to the email when there is no display name", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { ...SESSION, user: { ...SESSION.user, display_name: null } }),
    );
    render(<App />);
    expect(await screen.findByText("Signed in as alex@example.test")).toBeInTheDocument();
  });

  it("redirects to sign-in with the current /app path on 401", async () => {
    window.history.replaceState(null, "", "/app/policy?tab=audiences");
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Not signed in" }));
    render(<App />);

    await waitFor(() => expect(assignSpy).toHaveBeenCalledTimes(1));
    expect(assignSpy).toHaveBeenCalledWith(
      "/auth/login?return_to=" + encodeURIComponent("/app/policy?tab=audiences"),
    );
    expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();
  });

  it("shows the not-configured detail on a 503 from F2's configuration check", async () => {
    fetchMock.mockResolvedValue(jsonResponse(503, { detail: "Browser sign-in is not configured" }));
    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in is temporarily unavailable.");
    expect(alert).toHaveTextContent("Browser sign-in is not configured.");
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("shows a generic unavailable message on other 503s, without server text", async () => {
    fetchMock.mockResolvedValue(jsonResponse(503, { detail: "Identity store unavailable" }));
    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in is temporarily unavailable.");
    expect(alert).not.toHaveTextContent("Identity store");
  });

  it("records a sign-in marker before redirecting and clears it once signed in", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "Not signed in" }));
    const { unmount } = render(<App />);
    await waitFor(() => expect(assignSpy).toHaveBeenCalledTimes(1));
    expect(window.sessionStorage.getItem(LOGIN_MARKER_KEY)).not.toBeNull();
    unmount();

    fetchMock.mockResolvedValueOnce(jsonResponse(200, SESSION));
    render(<App />);
    await screen.findByText("Signed in as Alex Example");
    expect(window.sessionStorage.getItem(LOGIN_MARKER_KEY)).toBeNull();
  });

  it("stops a sign-in redirect loop and offers a manual retry", async () => {
    window.sessionStorage.setItem(LOGIN_MARKER_KEY, String(Date.now() - 5_000));
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Not signed in" }));
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in didn't stick");
    expect(assignSpy).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Retry sign-in" }));
    expect(assignSpy).toHaveBeenCalledWith("/auth/login?return_to=" + encodeURIComponent("/app/"));
  });

  it("redirects again when the last sign-in attempt is older than the loop window", async () => {
    window.sessionStorage.setItem(LOGIN_MARKER_KEY, String(Date.now() - 60_000));
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Not signed in" }));
    render(<App />);
    await waitFor(() => expect(assignSpy).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/didn't stick/)).toBeNull();
  });

  it("still redirects when sessionStorage throws", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Not signed in" }));
    render(<App />);
    await waitFor(() => expect(assignSpy).toHaveBeenCalledTimes(1));
    getItem.mockRestore();
    setItem.mockRestore();
  });

  it("renders the user's name as text, never HTML", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { ...SESSION, user: { ...SESSION.user, display_name: "<b>Alex</b>" } }),
    );
    const { container } = render(<App />);
    expect(await screen.findByText("Signed in as <b>Alex</b>")).toBeInTheDocument();
    expect(container.querySelector("b")).toBeNull();
  });
});

describe("sign out", () => {
  it("posts /auth/logout and navigates to the WorkOS logout URL", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, SESSION))
      .mockResolvedValueOnce(jsonResponse(200, { logout_url: "https://auth.example.test/logout" }));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    await waitFor(() =>
      expect(assignSpy).toHaveBeenCalledWith("https://auth.example.test/logout"),
    );
    const [url, init] = fetchMock.mock.calls[1]!;
    expect(url).toBe("/auth/logout");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("same-origin");
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it("reloads when there is no logout URL", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, SESSION))
      .mockResolvedValueOnce(jsonResponse(200, { logout_url: null }));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(reloadSpy).toHaveBeenCalledTimes(1));
    expect(assignSpy).not.toHaveBeenCalled();
  });
});
