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
    fireEvent.click(await screen.findByRole("button", { name: "Continue with WorkOS" }));
    expect(assign).toHaveBeenCalledWith("/auth/login?return_to=%2Fapp%2Finvite");
  });

  it("never consumes an invitation merely by loading it", async () => {
    window.history.replaceState(null, "", "/app/invite#token=synthetic-private-capability"); captureInvitation();
    const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/auth/session") return jsonResponse(200, { user: { email: "alex@example.test" } });
      if (url.endsWith("/preview")) return jsonResponse(200, { sender_name: "Sam", expires_at: "2026-12-01T12:00:00Z", status: "pending", sharing_notice: "No profile access is granted." });
      expect(JSON.parse(String(options?.body))).toEqual({ token: "synthetic-private-capability", action: "decline" });
      return jsonResponse(200, { status: "declined" });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InviteLanding />);
    await screen.findByRole("button", { name: "Accept invitation" });
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/respond"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Decline invitation" }));
    await screen.findByText(/Invitation declined/);
    expect(pendingInvitation()).toBeNull();
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
