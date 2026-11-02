import { useCallback, useEffect, useState } from "react";
import type { MouseEvent } from "react";

import {
  ApiError,
  UnauthorizedError,
  browserNavigation,
  clearLoginMarker,
  getSession,
  onSignInLoop,
  postLogout,
  startSignIn,
} from "./api";
import type { WhoAmI } from "./api";
import { ContributionsScreen } from "./ContributionsScreen";
import { PolicyScreen } from "./PolicyScreen";

type SessionState =
  | { kind: "loading" }
  | { kind: "signed-in"; session: WhoAmI }
  | { kind: "redirecting" }
  | { kind: "sign-in-loop" }
  | { kind: "unavailable"; detail: string | null }
  | { kind: "error"; message: string };

// Screens; Connections waits for its owning backend contract (F6).
const SCREENS = [
  { path: "/app/", label: "Home" },
  { path: "/app/policy", label: "Policy" },
  { path: "/app/contributions", label: "Contributions" },
  { path: "/app/connections", label: "Connections" },
] as const;

// F2's 503 detail when browser sign-in has no configuration (revel.auth.ui).
const NOT_CONFIGURED_DETAIL = "Browser sign-in is not configured";

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
  if (screen.path === "/app/policy") return <PolicyScreen />;
  if (screen.path === "/app/contributions") return <ContributionsScreen />;
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

  useEffect(() => onSignInLoop(() => setState({ kind: "sign-in-loop" })), []);

  useEffect(() => {
    const controller = new AbortController();
    getSession(controller.signal)
      .then((session) => {
        clearLoginMarker();
        setState({ kind: "signed-in", session });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof UnauthorizedError) {
          // The api client's unauthorized handler is navigating to sign-in, or has reported a
          // redirect loop (its listener already set "sign-in-loop").
          setState((s) => (s.kind === "sign-in-loop" ? s : { kind: "redirecting" }));
        } else if (error instanceof ApiError && error.status === 503) {
          // 503 covers both "not configured" and a temporarily unavailable identity store.
          const detail =
            error.detail === NOT_CONFIGURED_DETAIL ? NOT_CONFIGURED_DETAIL : null;
          setState({ kind: "unavailable", detail });
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
    case "unavailable":
      body = (
        <div role="alert">
          <p>Sign-in is temporarily unavailable.</p>
          {state.detail && <p>{state.detail}.</p>}
        </div>
      );
      break;
    case "sign-in-loop":
      body = (
        <div role="alert">
          <p>Sign-in didn&apos;t stick. Your browser may be blocking cookies for this site.</p>
          <button type="button" onClick={startSignIn}>
            Retry sign-in
          </button>
        </div>
      );
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
