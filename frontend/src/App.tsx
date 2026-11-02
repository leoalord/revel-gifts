import { useCallback, useEffect, useState } from "react";
import type { MouseEvent } from "react";

import { ApiError, UnauthorizedError, browserNavigation, getSession, postLogout } from "./api";
import type { WhoAmI } from "./api";

type SessionState =
  | { kind: "loading" }
  | { kind: "signed-in"; session: WhoAmI }
  | { kind: "redirecting" }
  | { kind: "not-configured" }
  | { kind: "error"; message: string };

// Placeholder screens; each will be built against its owning backend contract.
const SCREENS = [
  { path: "/app/", label: "Home" },
  { path: "/app/policy", label: "Policy" },
  { path: "/app/contributions", label: "Contributions" },
  { path: "/app/connections", label: "Connections" },
] as const;

function normalizePath(pathname: string): string {
  return pathname === "/app" ? "/app/" : pathname.replace(/\/+$/, "") || "/app/";
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && typeof error.detail === "string") return error.detail;
  return "Something went wrong. Please try again.";
}

function displayNameOf(session: WhoAmI): string {
  return session.user.display_name || session.user.email || "your account";
}

function Footer() {
  return (
    <footer className="footer">
      <small>Synthetic demo data</small>
    </footer>
  );
}

function Nav({ current, onNavigate }: { current: string; onNavigate: (path: string) => void }) {
  return (
    <nav aria-label="Main">
      <ul className="nav">
        {SCREENS.map((screen) => (
          <li key={screen.path}>
            <a
              href={screen.path}
              aria-current={normalizePath(screen.path) === current ? "page" : undefined}
              onClick={(event: MouseEvent<HTMLAnchorElement>) => {
                event.preventDefault();
                onNavigate(screen.path);
              }}
            >
              {screen.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Screen({ path }: { path: string }) {
  const screen = SCREENS.find((s) => normalizePath(s.path) === path);
  if (!screen || screen.path === "/app/") {
    return <p>Welcome to Revel Gifts.</p>;
  }
  return (
    <section>
      <h2>{screen.label}</h2>
      <p>Coming soon.</p>
    </section>
  );
}

export function App() {
  const [state, setState] = useState<SessionState>({ kind: "loading" });
  const [path, setPath] = useState(() => normalizePath(window.location.pathname));
  const [signingOut, setSigningOut] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getSession(controller.signal)
      .then((session) => setState({ kind: "signed-in", session }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof UnauthorizedError) {
          // The api client's unauthorized handler is already navigating to sign-in.
          setState({ kind: "redirecting" });
        } else if (error instanceof ApiError && error.status === 503) {
          setState({ kind: "not-configured" });
        } else {
          setState({ kind: "error", message: errorMessage(error) });
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const onPop = () => setPath(normalizePath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: string) => {
    window.history.pushState(null, "", to);
    setPath(normalizePath(to));
  }, []);

  const signOut = useCallback(async () => {
    setSigningOut(true);
    try {
      const { logout_url } = await postLogout();
      if (logout_url) {
        // Ends the WorkOS/AuthKit session; the POST only cleared Revel's cookie.
        browserNavigation.assign(logout_url);
      } else {
        browserNavigation.reload();
      }
    } catch (error: unknown) {
      setSigningOut(false);
      if (!(error instanceof UnauthorizedError)) {
        setState({ kind: "error", message: errorMessage(error) });
      }
    }
  }, []);

  let body;
  switch (state.kind) {
    case "loading":
    case "redirecting":
      body = <p role="status">Loading…</p>;
      break;
    case "not-configured":
      body = <p role="alert">Sign-in is not configured.</p>;
      break;
    case "error":
      body = <p role="alert">{state.message}</p>;
      break;
    case "signed-in":
      body = (
        <>
          <div className="account">
            <span>Signed in as {displayNameOf(state.session)}</span>
            <button type="button" onClick={signOut} disabled={signingOut}>
              Sign out
            </button>
          </div>
          <Nav current={path} onNavigate={navigate} />
          <main>
            <Screen path={path} />
          </main>
        </>
      );
      break;
  }

  return (
    <div className="shell">
      <header>
        <h1>Revel Gifts</h1>
      </header>
      {body}
      <Footer />
    </div>
  );
}
