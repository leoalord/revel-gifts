import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { InviteLanding } from "./InviteLanding";
import { captureInvitation } from "./inviteContext";
import "./index.css";

captureInvitation();

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

createRoot(root).render(
  <StrictMode>
    {window.location.pathname.replace(/\/$/, "") === "/app/invite" ? <InviteLanding /> : <App />}
  </StrictMode>,
);
