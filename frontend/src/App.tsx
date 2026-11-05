import { useCallback, useEffect, useRef, useState } from "react";
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
import { AgentsSection } from "./AgentsSection";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { ContributionsScreen } from "./ContributionsScreen";
import { PolicyScreen } from "./PolicyScreen";

type SessionState =
  | { kind: "loading" }
  | { kind: "signed-in"; session: WhoAmI }
  | { kind: "redirecting" }
  | { kind: "sign-in-loop" }
  | { kind: "unavailable"; detail: string | null }
  | { kind: "error"; message: string };

// Screens, each over its owning backend contract (F5 profile, F6 connections, F2 agents).
const SCREENS = [
  { path: "/app/", label: "Home" },
  { path: "/app/contributions", label: "Contributions" },
  { path: "/app/connections", label: "Connections" },
  { path: "/app/policy", label: "Policy" },
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

function followAppLink(
  event: MouseEvent<HTMLAnchorElement>,
  path: string,
  onNavigate: (path: string) => void,
) {
  if (
    event.defaultPrevented || event.button !== 0 ||
    event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
  ) return;
  event.preventDefault();
  onNavigate(path);
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
              onClick={(event) => followAppLink(event, screen.path, onNavigate)}
            >
              {screen.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

const HOME_LINKS = [
  { path: "/app/contributions", label: "Review contributions", description: "See what your assistants added, what needs review, and who can see each detail." },
  { path: "/app/connections", label: "Manage connections", description: "Review requests and manage who can access your shared gifting preferences." },
  { path: "/app/policy", label: "Manage sharing policy", description: "Choose what assistants can add and which details can be shared." },
];

function Home({ onNavigate }: { onNavigate: (path: string) => void }) {
  return (
    <section aria-labelledby="home-heading">
      <div className="home-hero">
        <div>
          <p className="eyebrow">Your gifting dashboard</p>
          <h2 id="home-heading">Make it personal.</h2>
          <p>See what your assistants have added for gifting, review anything that needs your attention, and decide who can see it.</p>
        </div>
        <svg className="gift-art" viewBox="0 0 230 180" fill="none" aria-hidden="true">
          <circle cx="116" cy="92" r="75" fill="currentColor" opacity=".08" />
          <g stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M64 88h104v66H64zM56 67h120v21H56zM105 67v87m22-87v87" />
            <path d="M116 67c-40 0-53-13-45-25 10-15 31-1 45 25Zm0 0c40 0 53-13 45-25-10-15-31-1-45 25Z" />
            <path d="M45 43v18m-9-9h18m139 61v16m-8-8h16m-18-82v10m-5-5h10" />
          </g>
        </svg>
      </div>
      <ul className="home-links">
        {HOME_LINKS.map(({ path, label, description }) => (
          <li key={path}>
            <a href={path} onClick={(event) => followAppLink(event, path, onNavigate)}>
              <span className="home-link-title">{label} <span aria-hidden="true">→</span></span>
              <span className="home-link-description">{description}</span>
            </a>
          </li>
        ))}
      </ul>
      <AgentsSection home />
    </section>
  );
}

function Screen({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const screen = SCREENS.find((s) => normalizePath(s.path) === path);
  if (!screen || screen.path === "/app/") {
    return <Home onNavigate={onNavigate} />;
  }
  if (screen.path === "/app/policy") return <PolicyScreen />;
  if (screen.path === "/app/contributions") return <ContributionsScreen />;
  return <ConnectionsScreen />;
}

export function App() {
  const [state, setState] = useState<SessionState>({ kind: "loading" });
  const [path, setPath] = useState(() => normalizePath(window.location.pathname));
  const [signingOut, setSigningOut] = useState(false);
  const main = useRef<HTMLElement>(null);
  const focusOnRouteChange = useRef(false);
  const routeLabel = SCREENS.find((screen) => normalizePath(screen.path) === path)?.label ?? "Home";

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
    const onPop = () => {
      focusOnRouteChange.current = true;
      setPath(normalizePath(window.location.pathname));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    document.title = `${routeLabel} | Revel Gifts`;
    if (focusOnRouteChange.current) {
      focusOnRouteChange.current = false;
      main.current?.focus();
    }
  }, [path, routeLabel]);

  const navigate = useCallback((to: string) => {
    const nextPath = normalizePath(to);
    if (nextPath === path) {
      main.current?.focus();
      return;
    }
    focusOnRouteChange.current = true;
    window.history.pushState(null, "", to);
    setPath(nextPath);
  }, [path]);

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
        <Screen path={path} onNavigate={navigate} />
      );
      break;
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="app-header">
        <h1 className="brand">Revel Gifts</h1>
        {state.kind === "signed-in" && (
          <>
            <Nav current={path} onNavigate={navigate} />
            <div className="account">
              <span className="account-name">Signed in as {displayNameOf(state.session)}</span>
              <button type="button" onClick={signOut} disabled={signingOut}>
                Sign out
              </button>
            </div>
          </>
        )}
      </header>
      <main
        id="main-content"
        ref={main}
        tabIndex={-1}
        aria-label={state.kind === "signed-in" ? routeLabel : undefined}
      >
        {body}
      </main>
    </div>
  );
}
