import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { UnauthorizedError } from "./api";
import { humanize } from "./format";
import {
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
  PolicyResponse,
  PolicyUpdate,
} from "./profileApi";

type Flag = "public_opt_in" | "share_provisional" | "require_review_all" | "enrichment_enabled";

interface Draft {
  flags: Record<Flag, boolean>;
  categories: Record<string, { audience: string; require_review: boolean }>;
  touched: Set<string>;
}

/**
 * Shown before the first approval, when the server has no policy to read the flags from.
 * These mirror the contract's documented defaults and are sent explicitly on approval, so what
 * the owner sees is exactly what is approved. (Raised with F5: the GET could return them.)
 */
const FIRST_RUN_FLAGS: Record<Flag, boolean> = {
  public_opt_in: false,
  share_provisional: true,
  require_review_all: false,
  enrichment_enabled: true,
};

const FALLBACK_CAVEAT =
  "Provisional facts (inferred or low confidence) are shared with a caveat that they are not " +
  "yet confirmed by you.";

const FLAG_FIELDS: { flag: Flag; label: string; hint?: "caveat" | string }[] = [
  {
    flag: "share_provisional",
    label: "Share provisional facts",
    hint: "caveat",
  },
  {
    flag: "require_review_all",
    label: "Review every contribution before it is shared",
  },
  {
    flag: "enrichment_enabled",
    label: "Allow my assistants to add facts",
  },
  {
    flag: "public_opt_in",
    label: "Allow Public sharing for eligible categories",
    hint: "Turning this off withdraws Public sharing.",
  },
];

function draftFrom(data: PolicyResponse): Draft {
  const policy = data.policy;
  const flags: Record<Flag, boolean> = policy
    ? {
        public_opt_in: policy.public_opt_in,
        share_provisional: policy.share_provisional,
        require_review_all: policy.require_review_all,
        enrichment_enabled: policy.enrichment_enabled,
      }
    : { ...FIRST_RUN_FLAGS };
  const categories: Draft["categories"] = {};
  for (const c of data.categories) {
    categories[c.id] = { audience: c.effective_audience, require_review: c.require_review };
  }
  return { flags, categories, touched: new Set() };
}

function buildUpdate(policy: Policy | null, draft: Draft): PolicyUpdate {
  const update: PolicyUpdate = { expected_version: policy?.version ?? null, ...draft.flags };
  // Untouched category settings are omitted so the server carries them forward (and applies
  // its own rules, e.g. dropping Public audiences when the opt-in is withdrawn).
  if (draft.touched.size > 0) {
    const settings: Record<string, CategorySetting> = { ...(policy?.category_settings ?? {}) };
    for (const id of draft.touched) {
      const value = draft.categories[id];
      if (value) settings[id] = { audience: value.audience, require_review: value.require_review };
    }
    update.category_settings = settings;
  }
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

  if (loadError) return <p role="alert">{loadError}</p>;
  if (!data || !draft) return <p role="status">Loading policy…</p>;

  const firstRun = data.policy === null;

  const setFlag = (flag: Flag, value: boolean) =>
    setDraft({ ...draft, flags: { ...draft.flags, [flag]: value } });

  const setCategory = (category: Category, change: Partial<Draft["categories"][string]>) => {
    const current = draft.categories[category.id] ?? {
      audience: category.effective_audience,
      require_review: category.require_review,
    };
    const touched = new Set(draft.touched);
    touched.add(category.id);
    setDraft({
      ...draft,
      categories: { ...draft.categories, [category.id]: { ...current, ...change } },
      touched,
    });
  };

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
        setAlert("Your policy changed elsewhere. The current policy is shown; review it and save again.");
        if (conflict.categories) {
          apply({
            policy: conflict.policy,
            categories: conflict.categories,
            provisional_caveat: conflict.provisional_caveat ?? data.provisional_caveat,
          });
        } else {
          // Categories (allowed audiences) depend on the policy: re-read them, keep nothing stale.
          setData({ ...data, policy: conflict.policy });
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
      !(key in FIRST_RUN_FLAGS) &&
      !data.categories.some((c) => key.startsWith(`category_settings.${c.id}`)),
  );

  return (
    <section aria-labelledby="policy-heading">
      <h2 id="policy-heading">
        {firstRun ? "Review and approve your contribution policy" : "Contribution policy"}
      </h2>
      {firstRun ? (
        <p>
          These are the default settings. Nothing your assistants add is shared until you approve a
          policy. Adjust anything below, then approve.
        </p>
      ) : (
        <p>
          Version {data.policy?.version}
          {data.policy?.approved_at ? `, approved ${new Date(data.policy.approved_at).toLocaleString()}` : ""}.
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
        <fieldset>
          <legend>Sharing</legend>
          {FLAG_FIELDS.map(({ flag, label, hint }) => (
            <div className="field" key={flag}>
              <label>
                <input
                  type="checkbox"
                  name={flag}
                  checked={draft.flags[flag]}
                  onChange={(e) => setFlag(flag, e.target.checked)}
                  aria-invalid={errors[flag] ? true : undefined}
                  aria-describedby={errors[flag] ? fieldId(flag) : undefined}
                />{" "}
                {label}
              </label>
              {hint && (
                <p className="hint">
                  {hint === "caveat" ? (data.provisional_caveat ?? FALLBACK_CAVEAT) : hint}
                </p>
              )}
              {errorFor(flag)}
            </div>
          ))}
        </fieldset>

        <fieldset>
          <legend>Categories</legend>
          <table className="categories">
            <thead>
              <tr>
                <th scope="col">Category</th>
                <th scope="col">Who can see it</th>
                <th scope="col">Review first</th>
              </tr>
            </thead>
            <tbody>
              {data.categories.map((category) => {
                const value = draft.categories[category.id];
                const audienceKey = `category_settings.${category.id}.audience`;
                const rowKey = `category_settings.${category.id}`;
                const audienceError = errors[audienceKey] ? audienceKey : errors[rowKey] ? rowKey : null;
                const selectId = `audience-${category.id}`;
                return (
                  <tr key={category.id}>
                    <th scope="row">
                      <label htmlFor={selectId}>{category.label}</label>
                    </th>
                    <td>
                      <select
                        id={selectId}
                        value={value?.audience ?? category.effective_audience}
                        onChange={(e) => setCategory(category, { audience: e.target.value })}
                        aria-invalid={audienceError ? true : undefined}
                        aria-describedby={audienceError ? fieldId(audienceError) : undefined}
                      >
                        {category.allowed_audiences.map((audience) => (
                          <option key={audience} value={audience}>
                            {humanize(audience)}
                          </option>
                        ))}
                      </select>
                      {audienceError && errorFor(audienceError)}
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Review ${category.label} before sharing`}
                        checked={value?.require_review ?? category.require_review}
                        onChange={(e) => setCategory(category, { require_review: e.target.checked })}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </fieldset>

        <button type="submit" disabled={saving}>
          {firstRun ? "Approve policy" : "Save policy"}
        </button>
      </form>
    </section>
  );
}
