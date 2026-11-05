import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { InvitationSection } from "./InvitationSection";
import { InviteLanding } from "./InviteLanding";
import { SetupSection } from "./SetupSection";
import { captureInvitation, clearInvitation, pendingInvitation } from "./inviteContext";
import { browserNavigation } from "./api";
import { jsonResponse } from "./test-fixtures";

beforeEach(() => {
  vi.restoreAllMocks();
  clearInvitation();
  sessionStorage.removeItem("revel.invitationJoinIntent");
  window.history.replaceState(null, "", "/app/");
});

describe("invitation capabilities and consent", () => {
  it("removes the capability from the URL and keeps login URLs token-free", async () => {
    window.history.replaceState(null, "", "/app/invite#token=synthetic-private-capability");
    captureInvitation();
    expect(window.location.hash).toBe("");
    expect(pendingInvitation()).toBe("synthetic-private-capability");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { detail: "Sign in" })));
    const assign = vi.spyOn(browserNavigation, "assign").mockImplementation(() => {});
    render(<InviteLanding />);
    fireEvent.click(await screen.findByRole("button", { name: "Create Revel account" }));
    expect(assign).toHaveBeenCalledWith("/auth/login?return_to=%2Fapp%2Finvite");
  });

  it("joins only after the primary action and opens the app without a connection decision", async () => {
    window.history.replaceState(null, "", "/app/invite#token=synthetic-private-capability"); captureInvitation();
    const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/auth/session") return jsonResponse(200, { user: { email: "alex@example.test" } });
      if (url.endsWith("/preview")) return jsonResponse(200, { sender_name: "Sam", expires_at: null, status: "pending" });
      expect(url).toBe("/api/invitations/join");
      expect(JSON.parse(String(options?.body))).toEqual({ token: "synthetic-private-capability" });
      return jsonResponse(200, { status: "joined" });
    });
    vi.stubGlobal("fetch", fetchMock);
    const assign = vi.spyOn(browserNavigation, "assign").mockImplementation(() => {});
    render(<InviteLanding />);
    const next = await screen.findByRole("button", { name: "Continue to Revel" });
    await waitFor(() => expect(next).toBeEnabled());
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/join"))).toBe(false);
    expect(screen.queryByRole("button", { name: "Accept invitation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline invitation" })).not.toBeInTheDocument();
    fireEvent.click(next);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/app/"));
    expect(pendingInvitation()).toBeNull();
  });

  it("completes the signup return without a second acceptance screen", async () => {
    window.history.replaceState(null, "", "/app/invite#token=synthetic-private-capability"); captureInvitation();
    sessionStorage.setItem("revel.invitationJoinIntent", "synthetic-private-capability");
    vi.stubGlobal("fetch", vi.fn(async (url: string) => jsonResponse(200,
      url === "/auth/session" ? { user: { email: "alex@example.test" } } : { status: "joined" })));
    const assign = vi.spyOn(browserNavigation, "assign").mockImplementation(() => {});
    render(<InviteLanding />);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/app/"));
    expect(pendingInvitation()).toBeNull();
    expect(sessionStorage.getItem("revel.invitationJoinIntent")).toBeNull();
  });

  it("keeps the invitation through wrong-account sign-out and explains recovery", async () => {
    window.history.replaceState(null, "", "/app/invite#token=synthetic-private-capability"); captureInvitation();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/auth/session") return jsonResponse(200, { user: { email: "wrong@example.test" } });
      if (url === "/auth/logout") return jsonResponse(200, { logout_url: "https://auth.example.test/logout" });
      return jsonResponse(404, { detail: "Invitation unavailable" });
    }));
    const assign = vi.spyOn(browserNavigation, "assign").mockImplementation(() => {});
    render(<InviteLanding />);
    fireEvent.click(await screen.findByRole("button", { name: "Use a different account" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://auth.example.test/logout"));
    expect(screen.getByText(/reopen the invitation from your email/)).toBeInTheDocument();
    expect(pendingInvitation()).toBe("synthetic-private-capability");
  });

  it("requires destination-specific confirmation before sending email", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, []));
    vi.stubGlobal("fetch", fetchMock);
    render(<InvitationSection />);
    fireEvent.change(screen.getByLabelText("Their email"), { target: { value: "alex@example.test" } });
    fireEvent.change(screen.getByLabelText("How to invite"), { target: { value: "email" } });
    expect(screen.getByRole("button", { name: "Send confirmed invitation" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Send confirmed invitation" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Their email"), { target: { value: "sam@example.test" } });
    expect(screen.getByRole("button", { name: "Send confirmed invitation" })).toBeDisabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });
});

it("keeps onboarding in the assistant and never configures a schedule on the web", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("Canonical starter guidance", { status: 200 }));
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  vi.stubGlobal("fetch", fetchMock);
  render(<SetupSection />);
  const copy = screen.getByRole("button", { name: "Copy starter prompt" });
  await waitFor(() => expect(copy).toBeEnabled());
  fireEvent.click(copy);
  await screen.findByRole("status");
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("https://api.revel.gifts/mcp"));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("Canonical starter guidance"));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]![0]).toBe("/guidance/revel-get-started.md");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Read guidance" })).toHaveAttribute("href", "/guidance/revel-get-started.md");
  vi.unstubAllGlobals();
});

it("keeps a readable onboarding link when guidance cannot be fetched", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  render(<SetupSection />);
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  expect(screen.getByRole("button", { name: "Copy starter prompt" })).toBeDisabled();
  expect(screen.getByRole("link", { name: "Read guidance" })).toBeInTheDocument();
  vi.unstubAllGlobals();
});
