import { useId, useState } from "react";
import type { FormEvent } from "react";

import { UnauthorizedError } from "./api";
import { ActorName } from "./ActorName";
import { formatObserved, formatShortTime, formatTime, humanize } from "./format";
import type { AudienceLabel } from "./format";
import {
  OBSERVED_PRECISIONS,
  PUBLIC_AUDIENCE,
  conflictFact,
  fieldErrorsOf,
  messageOf,
} from "./profileApi";
import type {
  Category,
  DetailsSchema,
  Fact,
  FactPatch,
  FieldErrors,
  ObservedPrecision,
} from "./profileApi";

export type FactOutcome =
  | { kind: "updated"; fact: Fact; notice: string; replacedId?: string }
  | { kind: "conflict"; fact: Fact }
  | { kind: "gone" };

export interface FactActions {
  patch: (id: string, patch: FactPatch) => Promise<Fact>;
  remove: (id: string) => Promise<Fact>;
  approve: (id: string) => Promise<Fact>;
  reject: (id: string) => Promise<Fact>;
}

interface Props {
  fact: Fact;
  category: Category | undefined;
  actions: FactActions;
  onOutcome: (outcome: FactOutcome) => void;
  audienceLabel?: AudienceLabel;
  /** The server's `audiences` ids, narrowest first (absent with an older server). */
  audienceIds?: readonly string[];
}

/**
 * Owner per-fact audience options (F5 contract: a fact may sit outside its category's policy
 * bounds). The server's audience list; Public only when the category's `allowed_audiences`
 * includes it (the server's eligibility/opt-in signal); always the fact's current audience.
 * The server validates every change; nothing else is decided here.
 */
function factAudienceOptions(
  current: string,
  allowed: readonly string[],
  audienceIds: readonly string[] | undefined,
): string[] {
  const options = (audienceIds ?? allowed).filter(
    (a) => a === current || a !== PUBLIC_AUDIENCE || allowed.includes(PUBLIC_AUDIENCE),
  );
  if (!options.includes(current)) options.push(current);
  return options;
}

// Owner actions are offered for live and pending facts only (presentation; the server enforces).
const EDITABLE_STATUSES: readonly Fact["status"][] = ["active", "provisional", "pending_review"];

function statusBadge(fact: Fact): { text: string; note?: string } {
  if (fact.status === "pending_review") {
    return { text: "Pending your review", note: "Not shared until you approve it." };
  }
  if (fact.status === "provisional" || fact.provisional) {
    return { text: "Provisional", note: "Shared with a caveat: not yet confirmed by you." };
  }
  return { text: humanize(fact.status) };
}

function FieldError({ id, message }: { id: string; message?: string }) {
  return message ? (
    <p className="field-error" id={id}>
      {message}
    </p>
  ) : null;
}

function DetailsFields({
  schema,
  values,
  onChange,
  errors,
  idPrefix,
}: {
  schema: DetailsSchema;
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  errors: FieldErrors;
  idPrefix: string;
}) {
  const required = new Set(schema.required ?? []);
  return (
    <fieldset>
      <legend>Details</legend>
      {Object.entries(schema.properties).map(([name, prop]) => {
        const id = `${idPrefix}-details-${name}`;
        const errorKey = `details.${name}`;
        return (
          <div className="field" key={name}>
            <label htmlFor={id}>
              {humanize(name)}
              {required.has(name) ? " (required)" : ""}
            </label>
            <input
              id={id}
              type={prop.type === "integer" ? "number" : "text"}
              min={prop.minimum}
              max={prop.maximum}
              maxLength={prop.maxLength}
              value={values[name] ?? ""}
              onChange={(e) => onChange({ ...values, [name]: e.target.value })}
              aria-invalid={errors[errorKey] ? true : undefined}
              aria-describedby={errors[errorKey] ? `${id}-error` : undefined}
            />
            <FieldError id={`${id}-error`} message={errors[errorKey]} />
          </div>
        );
      })}
      <FieldError id={`${idPrefix}-details-error`} message={errors.details} />
    </fieldset>
  );
}

function detailsToStrings(details: Record<string, unknown> | null): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(details ?? {})) {
    if (value !== null && value !== undefined) out[key] = String(value);
  }
  return out;
}

function detailsFromStrings(
  schema: DetailsSchema,
  values: Record<string, string>,
): Record<string, unknown> | null {
  const out: Record<string, unknown> = {};
  for (const [name, prop] of Object.entries(schema.properties)) {
    const raw = (values[name] ?? "").trim();
    if (!raw) continue;
    const number = Number(raw);
    out[name] = prop.type === "integer" && Number.isInteger(number) ? number : raw;
  }
  return Object.keys(out).length ? out : null;
}

/** Key-order-independent comparison of flat details objects (null and {} are equal). */
function sameDetails(
  a: Record<string, unknown> | null,
  b: Record<string, unknown> | null,
): boolean {
  const left = Object.entries(a ?? {});
  const right = b ?? {};
  return (
    left.length === Object.keys(right).length &&
    left.every(([key, value]) => Object.is(right[key], value))
  );
}

function CorrectionForm({
  fact,
  category,
  onSubmit,
  onCancel,
  errors,
  busy,
}: {
  fact: Fact;
  category: Category | undefined;
  onSubmit: (patch: FactPatch) => void;
  onCancel: () => void;
  errors: FieldErrors;
  busy: boolean;
}) {
  const prefix = useId();
  const [statement, setStatement] = useState(fact.statement);
  const [details, setDetails] = useState(() => detailsToStrings(fact.details));
  const [observedAt, setObservedAt] = useState(fact.observed_at ?? "");
  const [precision, setPrecision] = useState<ObservedPrecision>(fact.observed_precision);
  const schema = category?.details_schema ?? null;

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const patch: FactPatch = {};
    if (statement !== fact.statement) patch.statement = statement;
    if (schema) {
      const next = detailsFromStrings(schema, details);
      if (!sameDetails(next, fact.details)) patch.details = next;
    }
    const nextDate = observedAt || null;
    const nextPrecision = nextDate ? precision : "unknown";
    if (nextDate !== fact.observed_at || nextPrecision !== fact.observed_precision) {
      patch.observed_at = nextDate;
      patch.observed_precision = nextPrecision;
    }
    onSubmit(patch);
  };

  const ids = {
    statement: `${prefix}-statement`,
    date: `${prefix}-observed-at`,
    precision: `${prefix}-precision`,
  };
  return (
    <form className="correction" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor={ids.statement}>Statement</label>
        <textarea
          id={ids.statement}
          value={statement}
          maxLength={500}
          rows={2}
          onChange={(e) => setStatement(e.target.value)}
          aria-invalid={errors.statement ? true : undefined}
          aria-describedby={errors.statement ? `${ids.statement}-error` : undefined}
        />
        <FieldError id={`${ids.statement}-error`} message={errors.statement} />
      </div>
      {schema && (
        <DetailsFields
          schema={schema}
          values={details}
          onChange={setDetails}
          errors={errors}
          idPrefix={prefix}
        />
      )}
      <div className="field-row">
        <div className="field">
          <label htmlFor={ids.date}>Observed on</label>
          <input
            id={ids.date}
            type="date"
            value={observedAt}
            onChange={(e) => {
              const next = e.target.value;
              // A newly added date defaults to day precision; the owner can change it.
              if (next && !observedAt && precision === "unknown") setPrecision("day");
              setObservedAt(next);
            }}
            aria-invalid={errors.observed_at ? true : undefined}
            aria-describedby={errors.observed_at ? `${ids.date}-error` : undefined}
          />
          <FieldError id={`${ids.date}-error`} message={errors.observed_at} />
        </div>
        <div className="field">
          <label htmlFor={ids.precision}>Date precision</label>
          <select
            id={ids.precision}
            value={observedAt ? precision : "unknown"}
            disabled={!observedAt}
            onChange={(e) => setPrecision(e.target.value as ObservedPrecision)}
            aria-invalid={errors.observed_precision ? true : undefined}
            aria-describedby={errors.observed_precision ? `${ids.precision}-error` : undefined}
          >
            {OBSERVED_PRECISIONS.map((p) => (
              <option key={p} value={p}>
                {humanize(p)}
              </option>
            ))}
          </select>
          <FieldError id={`${ids.precision}-error`} message={errors.observed_precision} />
        </div>
      </div>
      <FieldError id={`${prefix}-form-error`} message={errors._ ?? errors.body} />
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          Save correction
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function FactItem({
  fact,
  category,
  actions,
  onOutcome,
  audienceLabel = humanize,
  audienceIds,
}: Props) {
  const audienceId = useId();
  const editable = EDITABLE_STATUSES.includes(fact.status);
  const [audience, setAudience] = useState(fact.audience);
  const [mode, setMode] = useState<"view" | "correct" | "confirm-remove">("view");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [audienceError, setAudienceError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const badge = statusBadge(fact);
  const options = factAudienceOptions(fact.audience, category?.allowed_audiences ?? [], audienceIds);
  const label = category?.label ?? humanize(fact.category);

  /** Runs an action; returns field errors (400/422) when the server refused it that way. */
  const run = async (
    action: () => Promise<Fact>,
    notice: (result: Fact) => string,
    replaces = false,
  ): Promise<FieldErrors | null> => {
    setBusy(true);
    setErrors({});
    setAudienceError(null);
    setMessage(null);
    try {
      const result = await action();
      setMode("view");
      onOutcome({
        kind: "updated",
        fact: result,
        notice: notice(result),
        replacedId: replaces && result.id !== fact.id ? fact.id : undefined,
      });
      return null;
    } catch (error) {
      if (error instanceof UnauthorizedError) return null;
      const current = conflictFact(error);
      if (current) {
        setMode("view");
        onOutcome({ kind: "conflict", fact: current });
        return null;
      }
      if (error && typeof error === "object" && "status" in error && error.status === 404) {
        onOutcome({ kind: "gone" });
        return null;
      }
      const fields = fieldErrorsOf(error);
      if (fields) setErrors(fields);
      else setMessage(messageOf(error));
      return fields;
    } finally {
      setBusy(false);
    }
  };

  const changeAudience = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const fields = await run(
      () => actions.patch(fact.id, { audience }),
      (f) => `Audience changed to ${audienceLabel(f.audience)}.`,
    );
    if (fields) {
      // The server refused the change: show why on this fact and keep the current audience.
      setErrors({});
      setAudienceError(fields.audience ?? fields._ ?? fields.body ?? Object.values(fields)[0] ?? null);
      setAudience(fact.audience);
    }
  };

  const observedKnown = Boolean(fact.observed_at) && fact.observed_precision !== "unknown";
  const ownerActions =
    !editable || mode === "correct" ? null : mode === "confirm-remove" ? (
      <div className="actions" role="group" aria-label="Confirm removal">
        <span>Remove this fact?</span>
        <button
          type="button"
          className="danger"
          disabled={busy}
          onClick={() => void run(() => actions.remove(fact.id), () => "Fact removed.")}
        >
          Confirm remove
        </button>
        <button type="button" disabled={busy} onClick={() => setMode("view")}>
          Cancel
        </button>
      </div>
    ) : (
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => setMode("correct")}>
          Correct
        </button>
        <button type="button" disabled={busy} onClick={() => setMode("confirm-remove")}>
          Remove
        </button>
      </div>
    );

  return (
    <li className="fact" aria-busy={busy || undefined}>
      <div className="fact-head">
        <p className="statement">{fact.statement}</p>
        <span className={`badge badge-${fact.status}`}>{badge.text}</span>
      </div>
      {/* Secondary metadata: one muted line, labels kept for assistive technology. */}
      <dl className="fact-meta">
        <div>
          <dt className="visually-hidden">Category</dt>
          <dd>{label}</dd>
        </div>
        <div>
          <dt className="visually-hidden">Audience</dt>
          <dd>{audienceLabel(fact.audience)}</dd>
        </div>
        <div>
          <dt className="visually-hidden">Evidence</dt>
          <dd>
            {humanize(fact.evidence_type)}, {fact.confidence} confidence
          </dd>
        </div>
        {observedKnown && (
          <div>
            <dt>Observed</dt>
            <dd>{formatObserved(fact.observed_at, fact.observed_precision)}</dd>
          </div>
        )}
        <div>
          <dt>Added by</dt>
          <dd title={`Contributed via ${fact.contributed_via}, ${formatTime(fact.created_at)}`}>
            <ActorName name={fact.contributor} />, {formatShortTime(fact.created_at)}
          </dd>
        </div>
      </dl>
      {badge.note && <p className="caveat">{badge.note}</p>}

      {message && (
        <p role="alert" className="alert">
          {message}
        </p>
      )}

      {(editable || fact.status === "pending_review") && (
        <div className="fact-footer">
          {editable && (
            <form className="audience-form" onSubmit={(e) => void changeAudience(e)} noValidate>
              <label htmlFor={audienceId}>Who can see it</label>
              <select
                id={audienceId}
                value={audience}
                onChange={(e) => {
                  setAudience(e.target.value);
                  setAudienceError(null);
                }}
                aria-invalid={audienceError ? true : undefined}
                aria-describedby={audienceError ? `${audienceId}-error` : undefined}
              >
                {options.map((a) => (
                  <option key={a} value={a}>
                    {audienceLabel(a)}
                  </option>
                ))}
              </select>
              <button type="submit" disabled={busy || audience === fact.audience}>
                Change audience
              </button>
              <FieldError id={`${audienceId}-error`} message={audienceError ?? undefined} />
            </form>
          )}
          <div className="footer-actions">
            {fact.status === "pending_review" && (
              <div className="actions">
                <button
                  type="button"
                  className="primary"
                  disabled={busy}
                  onClick={() => void run(() => actions.approve(fact.id), () => "Fact approved.")}
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => actions.reject(fact.id), () => "Fact rejected.")}
                >
                  Reject
                </button>
              </div>
            )}
            {ownerActions}
          </div>
        </div>
      )}

      {editable && mode === "correct" && (
        <CorrectionForm
          fact={fact}
          category={category}
          errors={errors}
          busy={busy}
          onCancel={() => {
            setErrors({});
            setMode("view");
          }}
          onSubmit={(patch) =>
            void run(() => actions.patch(fact.id, patch), () => "Correction saved.", true)
          }
        />
      )}
      {mode !== "correct" && errors._ && <FieldError id={`${audienceId}-form-error`} message={errors._} />}
    </li>
  );
}
