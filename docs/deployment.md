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
browser/MCP continuity and recipient acceptance separately before declaring adoption ready.
