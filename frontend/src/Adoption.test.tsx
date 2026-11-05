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

it("keeps host reports and server checks visibly unverified, and requires approval to change schedule", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url === "/api/setup") return jsonResponse(200, { revision: 2, enabled: true, delegation_id: "agent-1", timezone: "America/Los_Angeles", check_hours: [6,10,14,18], host_task_id: "host-task-1", schedule_state: "host_reported", background_verified: false, last_successful_check_at: "2026-11-04T18:00:00Z", last_check_item_count: 0, host_action_required: false });
    if (url === "/api/agents") return jsonResponse(200, [{ delegation_id: "agent-1", status: "active", agent: { label: "Test assistant" } }]);
    return new Response("Canonical starter guidance", { status: 200 });
  }));
  render(<SetupSection />);
  await screen.findByText("host reported");
  expect(screen.getByText("not verified")).toBeVisible();
  expect(screen.getByText(/does not establish that it ran in the background/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Save approved schedule preference" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText("I approve this assistant, timezone and check schedule."));
  expect(screen.getByRole("button", { name: "Save approved schedule preference" })).toBeEnabled();
  fireEvent.change(screen.getByLabelText("Local timezone (confirm before saving)"), { target: { value: "Europe/London" } });
  expect(screen.getByRole("button", { name: "Save approved schedule preference" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Record host task" })).toBeDisabled();
});
