import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock, MockInstance } from "vitest";

import { App } from "./App";
import { browserNavigation } from "./api";

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

let fetchMock: Mock<typeof fetch>;
let assignSpy: MockInstance<(url: string) => void>;
let reloadSpy: MockInstance<() => void>;

beforeEach(() => {
  fetchMock = vi.fn<typeof fetch>();
  vi.stubGlobal("fetch", fetchMock);
  assignSpy = vi.spyOn(browserNavigation, "assign").mockImplementation(() => {});
  reloadSpy = vi.spyOn(browserNavigation, "reload").mockImplementation(() => {});
  window.history.replaceState(null, "", "/app/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("session bootstrap", () => {
  it("shows the synthetic data footer while loading", () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => {}));
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.getByText("Synthetic demo data")).toBeInTheDocument();
  });

  it("requests /auth/session same-origin and shows the signed-in user", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, SESSION));
    render(<App />);

    expect(await screen.findByText("Signed in as Alex Example")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByText("Synthetic demo data")).toBeInTheDocument();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/auth/session");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.method).toBe("GET");
    expect(assignSpy).not.toHaveBeenCalled();
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

  it("shows a not-configured message on 503", async () => {
    fetchMock.mockResolvedValue(jsonResponse(503, { detail: "Browser sign-in not configured" }));
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in is not configured");
    expect(assignSpy).not.toHaveBeenCalled();
    expect(screen.getByText("Synthetic demo data")).toBeInTheDocument();
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
