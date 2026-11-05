# Deployment implementation guidance

[Notion](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) owns architecture and hosting decisions; the [MVP page](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns acceptance. Use the [infrastructure runbook](../infra/README.md) for GCP commands and configuration, and the [local setup guide](../infra/local/README.md) for Compose.

## Implementation considerations

- Pin and test dependency versions. Follow the pinned MCP SDK's mounting/lifespan guidance; configure public endpoint paths, OAuth discovery, host allowlists and browser origins at the deployed origin.
- Test the actual clients' protocol behavior across replicas and restarts. Avoid correctness depending on best-effort instance affinity. Persist durable business state outside application processes; follow SDK guidance for any cross-worker protocol state.
- Distinguish external user/agent authentication from infrastructure/service identity. Protect both transport entry points; validate credentials intended for this service. Do not use transport session IDs as identity or forward incoming credentials to unrelated services.
- Keep internal authorization services inaccessible to external assistants. Use least-privilege runtime identities, managed secrets and logs that exclude credentials/private payloads.
- Coordinate instance limits, database pools, timeouts and request limits. Measure before choosing sizing or minimum-instance settings. Apply migrations explicitly rather than on every replica startup.
- Include reconnect/retry, duplicate-write prevention, denied-access and revocation checks in feature acceptance. Use Notion's question lifecycle and contribution rules rather than inventing deployment-specific policy.

Record the deployed revision, configuration and verification results in the owning Notion task.

## Invitation mail (RG-66)

Invitation delivery defaults to disabled. Copyable email-bound invitation links work without a
mail provider. RG-65 question email fallback is separate and is not enabled by these settings.

The optional adapter uses authenticated SMTP over implicit TLS with certificate verification
(default port 465). Before enabling it, the user must approve the provider account, sender and
any necessary DNS/provider/infrastructure changes. Configure `INVITATION_MAIL_PROVIDER=smtp`,
`INVITATION_SMTP_HOST`, `INVITATION_SMTP_PORT`, `INVITATION_SMTP_USERNAME`, and
`INVITATION_FROM_EMAIL`; inject `INVITATION_SMTP_PASSWORD` from a managed secret. SMTP credentials
must belong to an approved transactional sender. `PUBLIC_BASE_URL` must be the HTTPS public
browser origin. Existing `WORKOS_COOKIE_PASSWORD` seals invitation capabilities at rest;
rotating it invalidates the ability to copy/resend existing invitations (redemption still uses
the token hash). Keep it stable through an acceptance hold.

Prepare a plan for those secret/env additions using the normal Terraform workflow; do not apply
infrastructure, DNS or provider changes without explicit approval. No provider or sender has
been provisioned or verified by this implementation. Test only an owner-approved mailbox.

The sender confirms the exact destination before sending. A database reservation permits one
send attempt per invitation, across replicas. SMTP has no delivery idempotency guarantee; after
a timeout or crash the status is `unknown` or `sending`, and Revel never automatically sends
again. The public state `submitted` means a delivery request was recorded, not mailbox delivery.
Internal `sent` means SMTP acceptance; internal suppression and uncertain delivery share the
same public state so block/account existence cannot be inferred from the send result. Cancellation
invalidates redemption even if an already reserved email subsequently arrives. Invite links use
URL fragments; do not put tokens in server path/query strings, logs, prompts or third-party
analytics. Verify actual mailbox delivery, WorkOS verified-email binding, new/existing-account
browser/MCP continuity and recipient signup attribution separately before declaring adoption ready.


SMTP enablement also requires `INVITATION_BACKGROUND_DELIVERY_READY=true`, which defaults to
false and is an explicit operator attestation after approved runtime configuration. Read-only
inspection of development `revel-app` found `run.googleapis.com/cpu-throttling=true` and
`minScale=0`; that configuration must not enable detached SMTP delivery. Approve and verify
instance-based CPU (`cpu_idle=false`) or replace the dispatcher with approved durable worker
infrastructure before setting the gate. Provider/DNS/runtime plans remain approval work.

The application lifespan owns the in-process mail task group and drains jobs on graceful
shutdown. HTTP and MCP return `submitted` after a durable once-only reservation, before any
private WorkOS lookup, block suppression or SMTP. Both blocked and unblocked requests take
the same response path. The worker rechecks current delegation, cancellation and
blocks before starting an attempt. Once an attempt starts, a later block or cancellation
cannot recall it. Process crashes or suspended CPU may leave `sending`/`unknown`; no automatic
replay occurs. This is not a durable delivery queue and never proves mailbox delivery. The
runtime gate and real approved-mailbox acceptance are required before enabling email.

## Selected provider: native Resend API

Leo selected Resend for RG-66. Set `INVITATION_MAIL_PROVIDER=resend`, an approved
`INVITATION_FROM_EMAIL`, and inject `RESEND_API_KEY` from a managed secret only after approval
and verified runtime readiness. Provider defaults remain disabled; the existing
`INVITATION_BACKGROUND_DELIVERY_READY` defaults false and applies to both SMTP and Resend.
SMTP remains compatible as an alternative. No provider account, key, sending domain, DNS or
infrastructure has been created by this code.

Use a Resend key with Sending access restricted to the approved verified sending domain;
full-access keys are unnecessary. Keep keys out of repository files, logs, prompts and PRs.
The adapter sends one fixed-origin HTTPS request to `https://api.resend.com/emails`, with
redirects disabled, a bounded timeout and the invitation UUID as `Idempotency-Key`. Resend
retains idempotency keys for 24 hours; this does not change Revel's stronger once-only
reservation/no-automatic-retry behavior for failures, timeouts, restarts or uncertain outcomes.
A validated provider message UUID is stored privately after API acceptance. It never appears
in public invitation responses, and API acceptance does not prove mailbox delivery. No
webhooks or RG-65 question-email delivery are included.

Before enabling: approve the provider account/domain/sender and necessary DNS changes; create
and securely configure the domain-scoped sending key; review the optional secret/environment
and CPU policy plan; explicitly approve any infrastructure apply; verify runtime readiness;
then send only to an explicitly approved test mailbox after sender confirmation. Preserve
WorkOS verified-email redemption and new/existing-account browser/MCP acceptance checks.

Implementation references: [send email](https://resend.com/docs/api-reference/emails/send-email),
[idempotency keys](https://resend.com/docs/dashboard/emails/idempotency-keys), and
[API key permissions](https://resend.com/docs/api-reference/api-keys/create-api-key).

Keep domain-level click and open tracking disabled. Tracking can rewrite links; the adapter
has no per-email override and does not need a domain-administration key. During approved
mailbox acceptance, inspect the actual received link and verify its original `#token=`
fragment survives unchanged before redeeming it. Do not give the runtime full-access domain
administration credentials to manage tracking settings.


### Nonexpiring signup invitations (RG-66 owner revision)

Migration `0009_invitation_join` makes expiry nullable and clears the deadline on all existing
pending invitations, including already-sent links; tokens and send/provider records are
preserved. Cancelled, declined and historically expired records remain terminal. New invites
have no expiry. Downgrade refuses nonexpiring/joined rows rather than inventing deadlines or
silently losing attribution.

The primary browser signup/continue action posts the capability in the body to
`POST /api/invitations/join`. It requires same-origin browser authentication and fresh verified
invited-email binding, rechecks both-direction blocks/cancellation, and records `joined`
attribution only. A same-recipient retry returns the same result and does not create a
connection request, sharing tier or profile. Opening/previewing a link never joins by itself.
The historical accept/decline endpoint remains compatibility-only; new clients use join.
Browser-local primary-button intent may resume after sign-in; assistant setup never receives
an invitation token, and browser revisit can complete attribution later.

Deployment must treat 0009 as runtime-incompatible with the old image: old code compares
expiry directly and does not understand joined status. Build and verify the compatible
revision without traffic first, apply the migration, then immediately route to that ready
revision. Do not route an old image over null-expiry/joined data. Use a forward fix or another
compatible runtime for rollback; schema downgrade intentionally refuses incompatible data.
