import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { UnauthorizedError } from "./api";
import { ActorName } from "./ActorName";
import { FactItem } from "./FactItem";
import type { FactActions, FactOutcome } from "./FactItem";
import { audienceLabeler, formatShortTime, formatTime, humanize } from "./format";
import type { AudienceLabel } from "./format";
import {
  CLAIM_STATUSES,
  approveFact,
  getPolicy,
  getProfile,
  messageOf,
  patchFact,
  rejectFact,
  removeFact,
} from "./profileApi";
import type { AudienceInfo, Category, ClaimStatus, Profile, Summary } from "./profileApi";

const ACTIONS: FactActions = {
  patch: patchFact,
  remove: removeFact,
  approve: approveFact,
  reject: rejectFact,
};

/** How many recent events show before "Show all" (display only; the server sends the list). */
const RECENT_VISIBLE = 6;

const ACTION_TONES: Record<string, string> = {
  created: "info",
  auto_accepted: "ok",
  approved: "ok",
  restored: "ok",
  rejected: "danger",
  removed: "danger",
  audience_changed: "info",
  updated: "info",
  status_changed: "neutral",
  superseded: "neutral",
};

function Counts({
  title,
  counts,
  label = humanize,
}: {
  title: string;
  counts: Record<string, number>;
  label?: AudienceLabel;
}) {
  const entries = Object.entries(counts);
  return (
    <div className="glance-counts">
      <h4>{title}</h4>
      {entries.length === 0 ? (
        <p className="hint">None</p>
      ) : (
        <dl className="counts">
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{label(key)}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function SummaryView({
  summary,
  audienceLabel,
  onShowPending,
}: {
  summary: Summary;
  audienceLabel: AudienceLabel;
  onShowPending: () => void;
}) {
  const pending = summary.pending_review > 0;
  return (
    <section aria-labelledby="summary-heading" className="summary">
      <h3 id="summary-heading">At a glance</h3>
      <div className="glance">
        <div className={pending ? "glance-pending has-pending" : "glance-pending"}>
          <p>
            <span className="big-number">{summary.pending_review}</span> waiting for your review.
          </p>
          {pending && (
            <button type="button" className="primary" onClick={onShowPending}>
              Review pending facts
            </button>
          )}
        </div>
        <Counts
          title="Facts by assigned audience"
          counts={summary.counts.by_audience}
          label={audienceLabel}
        />
        <Counts title="By status" counts={summary.counts.by_status} />
      </div>
    </section>
  );
}

function RecentActivity({
  summary,
  labels,
  audienceLabel,
}: {
  summary: Summary;
  labels: Map<string, string>;
  audienceLabel: AudienceLabel;
}) {
  const [expanded, setExpanded] = useState(false);
  const recent = summary.recent;
  // Presentation only: collapse the already-fetched list; the server decides what is in it.
  const shown = expanded ? recent : recent.slice(0, RECENT_VISIBLE);
  // Columns that carry no information for any event are left out.
  const hasCategory = recent.some((e) => e.category);
  const hasAudience = recent.some((e) => e.audience);
  const hasAcceptance = recent.some((e) => e.acceptance);
  return (
    <section aria-labelledby="activity-heading" className="activity">
      <h3 id="activity-heading">Recent activity</h3>
      <p className="hint">Since {formatShortTime(summary.since)}.</p>
      {recent.length === 0 ? (
        <p className="hint">No recent activity.</p>
      ) : (
        <>
          <div className="table-scroll">
            <table className="recent" id="recent-activity">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Action</th>
                  {hasCategory && <th scope="col">Category</th>}
                  <th scope="col">Who</th>
                  {hasAudience && <th scope="col">Audience</th>}
                  {hasAcceptance && <th scope="col">Acceptance</th>}
                </tr>
              </thead>
              <tbody>
                {shown.map((event, i) => (
                  <tr key={`${event.claim_id}-${event.occurred_at}-${i}`}>
                    <td className="when" title={formatTime(event.occurred_at)}>
                      {formatShortTime(event.occurred_at)}
                    </td>
                    <td>
                      <span className={`badge badge-sm tone-${ACTION_TONES[event.action] ?? "neutral"}`}>
                        {humanize(event.action)}
                      </span>
                    </td>
                    {hasCategory && (
                      <td>
                        {event.category ? (
                          (labels.get(event.category) ?? humanize(event.category))
                        ) : (
                          <span className="empty">—</span>
                        )}
                      </td>
                    )}
                    <td className="who">
                      <ActorName name={event.actor} />
                    </td>
                    {hasAudience && (
                      <td>
                        {event.audience ? (
                          audienceLabel(event.audience)
                        ) : (
                          <span className="empty">—</span>
                        )}
                      </td>
                    )}
                    {hasAcceptance && (
                      <td className="secondary">
                        {event.acceptance ? (
                          humanize(event.acceptance)
                        ) : (
                          <span className="empty">—</span>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {recent.length > RECENT_VISIBLE && (
            <button
              type="button"
              className="link-button"
              aria-expanded={expanded}
              aria-controls="recent-activity"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "Show fewer" : `Show all (${recent.length})`}
            </button>
          )}
        </>
      )}
    </section>
  );
}

export function ContributionsScreen() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [audiences, setAudiences] = useState<AudienceInfo[] | undefined>(undefined);
  const [filter, setFilter] = useState<ClaimStatus | "">("");
  const [loadedFilter, setLoadedFilter] = useState<ClaimStatus | "" | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const request = useRef(0);
  const currentFilter = useRef<ClaimStatus | "">("");

  const load = useCallback((status: ClaimStatus | "") => {
    const id = ++request.current;
    Promise.all([getProfile(status ? [status] : []), getPolicy()]).then(
      ([nextProfile, policy]) => {
        if (id !== request.current) return;
        setProfile(nextProfile);
        setLoadedFilter(status);
        setCategories(policy.categories);
        setAudiences(policy.audiences);
        setLoadError(null);
      },
      (error: unknown) => {
        if (id !== request.current || error instanceof UnauthorizedError) return;
        setLoadError(messageOf(error));
      },
    );
  }, []);

  useEffect(() => {
    load(filter);
  }, [load, filter]);

  const byId = useMemo(() => new Map(categories.map((c) => [c.id, c])), [categories]);
  const labels = useMemo(() => new Map(categories.map((c) => [c.id, c.label])), [categories]);
  const audienceLabel = useMemo(() => audienceLabeler(audiences), [audiences]);
  const audienceIds = useMemo(() => audiences?.map((a) => a.id), [audiences]);

  const changeFilter = (status: ClaimStatus | "") => {
    if (status === filter) return;
    currentFilter.current = status;
    setLoadError(null);
    setFilter(status);
  };

  const onOutcome = (outcome: FactOutcome) => {
    if (outcome.kind === "gone") {
      setNotice("That fact is no longer available. The list has been refreshed.");
      load(currentFilter.current);
      return;
    }
    if (outcome.kind === "conflict") {
      setProfile((p) =>
        p && {
          ...p,
          facts: p.facts.map((f) => (f.id === outcome.fact.id ? outcome.fact : f)),
        },
      );
      setNotice("This fact changed elsewhere. Its current state is shown; please try again.");
      return;
    }
    const { fact, replacedId } = outcome;
    setProfile((p) => {
      if (!p) return p;
      const targetId = replacedId ?? fact.id;
      return { ...p, facts: p.facts.map((f) => (f.id === targetId ? fact : f)) };
    });
    setNotice(
      replacedId ? `${outcome.notice} Replacement: “${fact.statement}”.` : outcome.notice,
    );
    // Re-read so the list and summary reflect the server's view (status filters, counts).
    load(currentFilter.current);
  };

  const retry = () => {
    setLoadError(null);
    load(filter);
  };

  if (!profile) {
    return (
      <section aria-labelledby="contributions-heading">
        <h2 id="contributions-heading">Contributions</h2>
        {loadError ? (
          <>
            <p role="alert" className="alert">{loadError}</p>
            <button type="button" onClick={retry}>Try again</button>
          </>
        ) : <p role="status">Loading contributions…</p>}
      </section>
    );
  }

  return (
    <section aria-labelledby="contributions-heading">
      <h2 id="contributions-heading">Contributions</h2>
      <p className="page-intro">See what was added to your gifting profile and review each detail’s source and audience.</p>
      {profile.policy === null && (
        <p className="alert">
          You have not approved a contribution policy yet, so nothing is shared.{" "}
          <a href="/app/policy">Review your policy</a>.
        </p>
      )}
      <p role="status" className="notice">
        {notice}
      </p>

      <SummaryView
        summary={profile.summary}
        audienceLabel={audienceLabel}
        onShowPending={() => {
          // Presentation only: the server filters via ?status=.
          changeFilter("pending_review");
          document.getElementById("status-filter")?.focus();
        }}
      />

      <section aria-labelledby="facts-heading" className="facts-section">
        <div className="section-head">
          <h3 id="facts-heading">Facts</h3>
          <div className="field inline-field">
            <label htmlFor="status-filter">Show</label>
            <select
              id="status-filter"
              value={filter}
              onChange={(e) => changeFilter(e.target.value as ClaimStatus | "")}
            >
              <option value="">Live and pending review</option>
              {CLAIM_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </select>
          </div>
        </div>
        {loadError ? (
          <>
            <p role="alert" className="alert">{loadError}</p>
            <button type="button" onClick={retry}>Try again</button>
          </>
        ) : loadedFilter !== filter ? (
          <p role="status">Loading facts…</p>
        ) : profile.facts.length === 0 ? (
          <p className="hint">No facts to show.</p>
        ) : (
          <ul className="facts">
            {profile.facts.map((fact) => (
              <FactItem
                key={`${fact.id}:${fact.updated_at}:${fact.audience}:${fact.status}`}
                fact={fact}
                category={byId.get(fact.category)}
                actions={ACTIONS}
                onOutcome={onOutcome}
                audienceLabel={audienceLabel}
                audienceIds={audienceIds}
              />
            ))}
          </ul>
        )}
      </section>

      <RecentActivity summary={profile.summary} labels={labels} audienceLabel={audienceLabel} />
    </section>
  );
}
