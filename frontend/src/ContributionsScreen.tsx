import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { UnauthorizedError } from "./api";
import { FactItem } from "./FactItem";
import type { FactActions, FactOutcome } from "./FactItem";
import { audienceLabeler, formatTime, humanize } from "./format";
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
    <div>
      <h4>{title}</h4>
      {entries.length === 0 ? (
        <p>None</p>
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
  labels,
  audienceLabel,
}: {
  summary: Summary;
  labels: Map<string, string>;
  audienceLabel: AudienceLabel;
}) {
  return (
    <section aria-labelledby="summary-heading" className="summary">
      <h3 id="summary-heading">Summary</h3>
      <p>
        <strong>{summary.pending_review}</strong> waiting for your review. Since{" "}
        {formatTime(summary.since)}.
      </p>
      <div className="counts-row">
        <Counts title="By status" counts={summary.counts.by_status} />
        <Counts title="By audience" counts={summary.counts.by_audience} label={audienceLabel} />
      </div>
      <h4>Recent activity</h4>
      {summary.recent.length === 0 ? (
        <p>No recent activity.</p>
      ) : (
        <table className="recent">
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Who</th>
              <th scope="col">Action</th>
              <th scope="col">Category</th>
              <th scope="col">Audience</th>
              <th scope="col">Acceptance</th>
            </tr>
          </thead>
          <tbody>
            {summary.recent.map((event, i) => (
              <tr key={`${event.claim_id}-${event.occurred_at}-${i}`}>
                <td>{formatTime(event.occurred_at)}</td>
                <td>{event.actor}</td>
                <td>{humanize(event.action)}</td>
                <td>
                  {event.category ? (labels.get(event.category) ?? humanize(event.category)) : "—"}
                </td>
                <td>{event.audience ? audienceLabel(event.audience) : "—"}</td>
                <td>{event.acceptance ? humanize(event.acceptance) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function ContributionsScreen() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [audiences, setAudiences] = useState<AudienceInfo[] | undefined>(undefined);
  const [filter, setFilter] = useState<ClaimStatus | "">("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const request = useRef(0);

  const load = useCallback((status: ClaimStatus | "") => {
    const id = ++request.current;
    Promise.all([getProfile(status ? [status] : []), getPolicy()]).then(
      ([nextProfile, policy]) => {
        if (id !== request.current) return;
        setProfile(nextProfile);
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

  const onOutcome = (outcome: FactOutcome) => {
    if (outcome.kind === "gone") {
      setNotice("That fact is no longer available. The list has been refreshed.");
      load(filter);
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
    load(filter);
  };

  if (loadError) return <p role="alert">{loadError}</p>;
  if (!profile) return <p role="status">Loading contributions…</p>;

  return (
    <section aria-labelledby="contributions-heading">
      <h2 id="contributions-heading">Contributions</h2>
      {profile.policy === null && (
        <p className="alert">
          You have not approved a contribution policy yet, so nothing is shared.{" "}
          <a href="/app/policy">Review your policy</a>.
        </p>
      )}
      <p role="status" className="notice">
        {notice}
      </p>

      <SummaryView summary={profile.summary} labels={labels} audienceLabel={audienceLabel} />

      <section aria-labelledby="facts-heading">
        <h3 id="facts-heading">Facts</h3>
        <div className="field">
          <label htmlFor="status-filter">Show</label>
          <select
            id="status-filter"
            value={filter}
            onChange={(e) => setFilter(e.target.value as ClaimStatus | "")}
          >
            <option value="">Live and pending review</option>
            {CLAIM_STATUSES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </div>
        {profile.facts.length === 0 ? (
          <p>No facts to show.</p>
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
    </section>
  );
}
