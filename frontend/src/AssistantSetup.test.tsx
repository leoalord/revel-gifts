import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AssistantSetup } from "./AssistantSetup";
import { CopyTextButton } from "./CopyTextButton";
import { RegisteredAssistantMark } from "./ProviderMarks";

vi.mock("./SetupSection", () => ({ SetupSection: () => null }));
afterEach(() => vi.unstubAllGlobals());

it("opens the chosen provider and offers its numbered connection steps", () => {
  render(<AssistantSetup active={[]} />);
  fireEvent.click(screen.getByRole("link", { name: "OpenAI Dots + ChatGPT" }));
  const detail = document.getElementById("connect-openai") as HTMLDetailsElement;
  expect(detail.open).toBe(true);
  expect(detail.querySelectorAll("ol > li")).toHaveLength(4);
  expect(detail.querySelector("summary")).toHaveFocus();
  expect(detail.textContent).toContain("Sign in or create a Revel account");
});

it("copies connection data and provides selectable text if the clipboard fails", async () => {
  const writeText = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("denied"));
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<CopyTextButton text="https://api.revel.gifts/mcp" label="Copy URL" />);
  fireEvent.click(screen.getByRole("button", { name: "Copy URL" }));
  await screen.findByText("Copied");
  expect(writeText).toHaveBeenCalledWith("https://api.revel.gifts/mcp");
  fireEvent.click(screen.getByRole("button", { name: "Copy URL" }));
  expect(await screen.findByRole("textbox", { name: "Copy URL text" })).toHaveValue("https://api.revel.gifts/mcp");
});

it("keeps Cursor registration ambiguous and falls back when an image fails", async () => {
  const { container } = render(<RegisteredAssistantMark label="Cursor" />);
  const logos = [...container.querySelectorAll("img")];
  expect(logos.map((img) => img.src)).toEqual([expect.stringContaining("provider-cursor"), expect.stringContaining("provider-grok")]);
  fireEvent.error(logos[0]!);
  await waitFor(() => expect(container.querySelectorAll("img")).toHaveLength(1));
  expect(container.querySelector("svg")).toBeInTheDocument();
});
