import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { UnauthorizedError } from "./api";
import { audienceLabeler } from "./format";
import {
  PUBLIC_AUDIENCE,
  conflictPolicy,
  fieldErrorsOf,
  getPolicy,
  messageOf,
  putPolicy,
} from "./profileApi";
import type {
  Category,
  CategorySetting,
  FieldErrors,
  Policy,
  PolicyFlags,
  PolicyResponse,
  PolicyUpdate,
} from "./profileApi";

type Flag = keyof PolicyFlags;

/** Only what the owner changed on a category row; untouched fields are never sent. */
interface CategoryChange {
  audience?: string;
  require_review?: boolean;
}

interface Draft {
  flags: PolicyFlags;
  /** Flags as loaded (saved policy, or first-run defaults), to show unsaved opt-in changes. */
  baseline: PolicyFlags;
  changes: Record<string, CategoryChange>;
}


/**
 * Before the first approval the server has no policy to read flags from. Prefer the server's
 * `defaults` (agreed F5 follow-up); fall back to the contract's documented defaults. Flags are
 * sent explicitly on approval, so what the owner sees is exactly what is approved.
 */
const DOCUMENTED_DEFAULTS: PolicyFlags = {
  public_opt_in: false,
  share_provisional: true,
  require_review_all: false,
  enrichment_enabled: true,
};

const FALLBACK_CAVEAT =
  "Provisional facts (inferred or low confidence) are shared with a caveat that they are not " +
  "yet confirmed by you.";

const ENABLING_NOTICE =
  "Categories eligible for Public sharing (marked) will be shared publicly after you save, " +
  "unless you choose a narrower audience.";
const WITHDRAWING_NOTICE = "Public sharing will be withdrawn after you save.";

const FLAG_FIELDS: { flag: Flag; label: string; hint?: string }[] = [
  { flag: "share_provisional", label: "Share provisional facts", hint: "caveat" },
  { flag: "require_review_all", label: "Review every contribution before it is shared" },
  { flag: "enrichment_enabled", label: "Allow my assistants to add facts" },
  {
    flag: "public_opt_in",
    label: "Allow Public sharing for eligible categories",
    hint: "Eligible categories default to Public unless you choose a narrower audience. This setting is separate from handle discoverability.",
  },
];

function flagsOf(data: PolicyResponse): PolicyFlags {
  const p = data.policy;
  if (!p) return { ...DOCUMENTED_DEFAULTS, ...(data.defaults ?? {}) };
  return {
    public_opt_in: p.public_opt_in,
    share_provisional: p.share_provisional,
    require_review_all: p.require_review_all,
    enrichment_enabled: p.enrichment_enabled,
  };
}

function draftFrom(data: PolicyResponse): Draft {
  const flags = flagsOf(data);
  return { flags, baseline: { ...flags }, changes: {} };
}

function explicitAudience(policy: Policy | null, id: string): string | null {
  return policy?.category_settings[id]?.audience ?? null;
}

function buildUpdate(policy: Policy | null, draft: Draft): PolicyUpdate {
  const update: PolicyUpdate = { expected_version: policy?.version ?? null, ...draft.flags };
  const changed = Object.keys(draft.changes);
  // With no category changes, settings are omitted and the server carries them forward
  // (applying its own rules, e.g. dropping Public audiences when the opt-in is withdrawn).
  if (changed.length === 0) return update;

  // Otherwise send the saved explicit settings plus only what the owner changed: an audience
  // the server derived (from the default or the opt-in) is never frozen into the policy.
  const settings: Record<string, CategorySetting> = {};
  for (const [id, saved] of Object.entries(policy?.category_settings ?? {})) {
    const value: CategorySetting = {};
    if (saved.audience != null) value.audience = saved.audience;
    if (saved.require_review != null) value.require_review = saved.require_review;
    settings[id] = value;
  }
  const withdrawing = draft.baseline.public_opt_in && !draft.flags.public_opt_in;
  for (const [id, value] of Object.entries(settings)) {
    // Mirror the server's carry-forward rule: a withdrawn opt-in drops saved Public audiences
    // the owner did not re-choose in this save.
    if (withdrawing && value.audience === PUBLIC_AUDIENCE && draft.changes[id]?.audience === undefined) {
      delete value.audience;
    }
  }
  for (const id of changed) {
    const change = draft.changes[id] ?? {};
    const value = { ...(settings[id] ?? {}) };
    if (change.audience !== undefined) value.audience = change.audience;
    if (change.require_review !== undefined) value.require_review = change.require_review;
    settings[id] = value;
  }
  for (const [id, value] of Object.entries(settings)) {
    if (Object.keys(value).length === 0) delete settings[id];
  }
  update.category_settings = settings;
  return update;
}

function fieldId(key: string): string {
  return `policy-error-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

export function PolicyScreen() {
  const [data, setData] = useState<PolicyResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [alert, setAlert] = useState<string | null>(null);
  const [errors, setErrors] = useState<FieldErrors>({});

  const apply = useCallback((next: PolicyResponse) => {
    setData(next);
    setDraft(draftFrom(next));
    setErrors({});
  }, []);

  const reload = useCallback(async () => {
    try {
      apply(await getPolicy());
      setLoadError(null);
    } catch (error) {
      if (error instanceof UnauthorizedError) return;
      setLoadError(messageOf(error));
    }
  }, [apply]);

  useEffect(() => {
    const controller = new AbortController();
    getPolicy(controller.signal).then(apply, (error: unknown) => {
      if (controller.signal.aborted || error instanceof UnauthorizedError) return;
      setLoadError(messageOf(error));
    });
    return () => controller.abort();
  }, [apply]);

  if (loadError || !data || !draft) {
    return (
      <section aria-labelledby="policy-heading">
        <h2 id="policy-heading">Contribution policy</h2>
        {loadError ? (
          <>
            <p role="alert" className="alert">{loadError}</p>
            <button type="button" onClick={() => {
              setLoadError(null);
              void reload();
            }}>Try again</button>
          </>
        ) : <p role="status">Loading policy…</p>}
      </section>
    );
  }

  const firstRun = data.policy === null;
  const label = audienceLabeler(data.audiences);
  const enabling = draft.flags.public_opt_in && !draft.baseline.public_opt_in;
  const withdrawing = !draft.flags.public_opt_in && draft.baseline.public_opt_in;
  const optInPending = enabling || withdrawing;

  const setFlag = (flag: Flag, value: boolean) =>
    setDraft({ ...draft, flags: { ...draft.flags, [flag]: value } });

  const setCategory = (id: string, change: CategoryChange) =>
    setDraft({
      ...draft,
      changes: { ...draft.changes, [id]: { ...(draft.changes[id] ?? {}), ...change } },
    });

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setNotice(null);
    setAlert(null);
    setErrors({});
    try {
      const saved = await putPolicy(buildUpdate(data.policy, draft));
      apply(saved);
      setNotice(
        firstRun
          ? "Contribution policy approved."
          : `Policy saved (version ${saved.policy?.version ?? "?"}).`,
      );
    } catch (error) {
      if (error instanceof UnauthorizedError) return;
      const conflict = conflictPolicy(error);
      if (conflict) {
        setAlert(
          "Your policy changed elsewhere. The current policy is shown; review it and save again.",
        );
        if (conflict.categories) {
          apply({
            ...conflict,
            categories: conflict.categories,
            provisional_caveat: conflict.provisional_caveat ?? data.provisional_caveat,
            audiences: conflict.audiences ?? data.audiences,
          });
        } else {
          // Categories (allowed audiences) depend on the policy: re-read them, keep nothing stale.
          setDraft(null);
          await reload();
        }
        return;
      }
      const fields = fieldErrorsOf(error);
      if (fields) {
        setErrors(fields);
        setAlert("Some settings could not be saved. See the messages below.");
      } else {
        setAlert(messageOf(error));
      }
    } finally {
      setSaving(false);
    }
  };

  const errorFor = (key: string) =>
    errors[key] ? (
      <p className="field-error" id={fieldId(key)}>
        {errors[key]}
      </p>
    ) : null;

  const unplacedErrors = Object.entries(errors).filter(
    ([key]) =>
      !(key in DOCUMENTED_DEFAULTS) &&
      !data.categories.some((c) => key.startsWith(`category_settings.${c.id}`)),
  );

  return (
    <section aria-labelledby="policy-heading">
      <h2 id="policy-heading">
        {firstRun ? "Review and approve your contribution policy" : "Contribution policy"}
      </h2>
      <p className="page-intro">Set what your assistants can add and how new details are reviewed and shared.</p>
      {firstRun ? (
        <p>
          These are the default settings. Nothing your assistants add is shared until you approve a
          policy. Adjust anything below, then approve.
        </p>
      ) : (
        <p>
          Version {data.policy?.version}
          {data.policy?.approved_at
            ? `, approved ${new Date(data.policy.approved_at).toLocaleString()}`
            : ""}
          .
        </p>
      )}
      {alert && (
        <p role="alert" className="alert">
          {alert}
        </p>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      {unplacedErrors.length > 0 && (
        <ul className="field-error">
          {unplacedErrors.map(([key, msg]) => (
            <li key={key}>{msg}</li>
          ))}
        </ul>
      )}

      <form onSubmit={onSubmit} noValidate>
        <fieldset className="policy-flags">
          <legend>Sharing</legend>
          {FLAG_FIELDS.map(({ flag, label: text, hint }) => {
            const id = `policy-flag-${flag}`;
            const hintId = hint ? `${id}-hint` : undefined;
            const describedBy = [hintId, errors[flag] ? fieldId(flag) : undefined]
              .filter(Boolean)
              .join(" ");
            return (
              <div className="field checkbox-field" key={flag}>
                <input
                  id={id}
                  type="checkbox"
                  name={flag}
                  checked={draft.flags[flag]}
                  onChange={(e) => setFlag(flag, e.target.checked)}
                  aria-invalid={errors[flag] ? true : undefined}
                  aria-describedby={describedBy || undefined}
                />
                <label htmlFor={id}>{text}</label>
                {hint && (
                  <p className="hint" id={hintId}>
                    {hint === "caveat" ? (data.provisional_caveat ?? FALLBACK_CAVEAT) : hint}
                  </p>
                )}
                {flag === "public_opt_in" && optInPending && (
                  <p className="pending-notice" role="status">
                    {enabling ? ENABLING_NOTICE : WITHDRAWING_NOTICE}
                  </p>
                )}
                {errorFor(flag)}
              </div>
            );
          })}
        </fieldset>

        <fieldset>
          <legend>Category defaults</legend>
          <p className="hint">
            These settings guide new contributions. Each detail has its own audience and review status.
          </p>
          {optInPending && <p className="hint">Public defaults update after saving.</p>}
          <ul className="policy-categories">
            {data.categories.map((category: Category) => {
              const change = draft.changes[category.id] ?? {};
              const audienceKey = `category_settings.${category.id}.audience`;
              const rowKey = `category_settings.${category.id}`;
              const audienceError = errors[audienceKey]
                ? audienceKey
                : errors[rowKey]
                  ? rowKey
                  : null;
              const selectId = `audience-${category.id}`;
              const markerId = `${selectId}-marker`;
              const implicit =
                category.public_eligible &&
                explicitAudience(data.policy, category.id) === null &&
                change.audience === undefined;
              // A saved explicit Public audience is dropped on withdrawal (contract rule).
              const explicitPublic =
                explicitAudience(data.policy, category.id) === PUBLIC_AUDIENCE &&
                change.audience === undefined;
              const marker =
                implicit && enabling
                  ? "Public after saving"
                  : (implicit || explicitPublic) && withdrawing
                    ? "No longer Public after saving"
                    : null;
              const options = [...category.allowed_audiences];
              if (enabling && category.public_eligible && !options.includes(PUBLIC_AUDIENCE)) {
                options.push(PUBLIC_AUDIENCE);
              }
              const describedBy = [
                marker ? markerId : undefined,
                audienceError ? fieldId(audienceError) : undefined,
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <li key={category.id}>
                  <label className="category-name" htmlFor={selectId}>{category.label}</label>
                  <div className="category-audience">
                    <select
                      id={selectId}
                      value={change.audience ?? category.effective_audience}
                      onChange={(e) => setCategory(category.id, { audience: e.target.value })}
                      aria-invalid={audienceError ? true : undefined}
                      aria-describedby={describedBy || undefined}
                    >
                      {options.map((audience) => (
                        <option key={audience} value={audience}>
                          {label(audience)}
                        </option>
                      ))}
                    </select>
                    {marker && (
                      <span className="marker" id={markerId}>
                        {" "}
                        {marker}
                      </span>
                    )}
                    {audienceError && errorFor(audienceError)}
                  </div>
                  <label className="category-review">
                    <input
                      type="checkbox"
                      aria-label={`Review ${category.label} before sharing`}
                      checked={change.require_review ?? category.require_review}
                      onChange={(e) =>
                        setCategory(category.id, { require_review: e.target.checked })
                      }
                    />
                    Review first
                  </label>
                </li>
              );
            })}
          </ul>
        </fieldset>

        <button type="submit" className="primary" disabled={saving}>
          {firstRun ? "Approve policy" : "Save policy"}
        </button>
      </form>
    </section>
  );
}
