# Working on Revel Gifts

## Sources of truth

Current user instructions govern the work. Notion owns approved product decisions, architecture, MVP scope and acceptance. Read the relevant owning pages before planning, implementing or reviewing a task; do not rely on a chat summary or copied policy in this repository.

| Source | Owns |
| --- | --- |
| [Main project](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) | Overall direction, agreed stack, architecture and open decisions |
| [MVP](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) | Required scope, deferred work, demonstration and definition of done |
| [Build Tasks](https://app.notion.com/p/c9c3f5bcaf954df1a330e0176b96fe81) | Task records: phase, feature, dependencies, builder/reviewer, revision, review evidence and status |
| [Features](https://app.notion.com/p/820a5452d5d541cdafeb8c3d4ecb6e95) | Feature records: orchestrator, owned shared contracts, review tier and feature gate results |
| [Workflows](https://app.notion.com/p/40b2bade7f31810da053cac31d2d9785) | User and agent journeys |
| [Onboarding](https://app.notion.com/p/40a2bade7f3181eab40de24dfbe88adb) | Guidance for users' assistants and client capability evidence (not build-agent process) |
| [Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) | Audiences, contribution policy, connections and access rules |
| [Questions](https://app.notion.com/p/40a2bade7f3181bf9975f7fa9a1f597d) | Routing, evidence, lifecycle, leases and deadlines |
| [Research](https://app.notion.com/p/40b2bade7f3181cf9709deca51417f33) | Supporting evidence and historical proposals, not automatic requirements |
| [Hackathon](https://app.notion.com/p/4092bade7f3180909d7cec4d1b230844) | Event context and submission requirements/uncertainties |

Repository READMEs describe layout and operation; original research files are historical. Neither overrides approved Notion decisions. Task records refine scope without silently changing product policy. If sources conflict materially, identify the conflict, continue unaffected work, and obtain a decision before implementing the disputed behavior. If Notion is unavailable, report the missing source and avoid guessing affected requirements.

## Roles

Creating or reading this file does not launch implementation; build work starts only when the user authorizes it.

- **User:** the coordinator. Kicks off orchestrators, makes product decisions and performs the user-only steps below.
- **Orchestrator:** one per feature or feature slice (that feature's tasks within the current phase). Dispatches tasks, tracks them in Notion, merges, deploys and runs gates.
- **Builder:** implements one assigned task and hands it off for review.
- **Reviewer:** an agent that did not author the work. Task reviewers accept or reject one task revision; feature reviewers accept or reject a whole integrated feature.

Builders and reviewers report to and escalate through their orchestrator. Orchestrators message each other directly and escalate to the user only when they cannot agree, need a decision or need a user-only step.

## Rules for every role

**Tracking.** Notion is the execution tracker: task scope, dependencies, assignments, status, acceptance evidence and final review decisions; feature records hold the feature gate. GitHub PRs hold code diffs, line-specific findings and fix discussions. Link them in both directions rather than copying discussions. Follow the actual Notion structure and fields; do not invent records or create a competing tracker.

**Statuses.** In progress → In review → Changes requested (when needed) → In review → Done, with Blocked for an unresolved dependency. Done requires reviewer acceptance of the final revision, passing required checks and integration, not just an open PR. A builder's completion message means ready for review, not done. PR approval does not replace feature-level review.

**Task numbering.** Build Tasks "Depends on" refers to the Order number (`#5`); branches use the task's RG ID shown in its ID field. They are different numbers.

**Shared contracts.** Each feature record lists its owned shared contracts; only the owning feature changes them. Others request changes through that feature's orchestrator; if it has no running orchestrator, ask the user to start one or approve a scoped change. If two orchestrators need the same file or contract at once, the owner decides the order.

- All database migrations go through F3, which keeps a single Alembic head. Other features specify the schema change they need; F3 adds and orders the migration.
- F1 owns app composition, configuration, dependencies and infrastructure. F2 owns the identity dependency. F4 owns the OpenFGA model and shared authorization service.

**User-only steps.** Escalate these as soon as a task needs them; such tasks are marked **Needs user** in Build Tasks: creating the GCP project and billing; WorkOS account and dashboard settings; DNS for `revel.gifts`; GitHub repository ownership; Muse accounts and connecting Muse to Revel; API keys for Muse Spark; the hackathon rules check; recording the video and submitting.

**Approvals.** Get explicit user approval for Terraform applies that create or change infrastructure, DNS changes, WorkOS dashboard configuration, GitHub repository settings and the hackathon submission, unless the user already authorized that specific action. Prepare changes and plans for review first; local work and checks continue while approval is pending.

**Evidence.** Never invent commits, deployments, check results or completion evidence. Do not claim tests passed when dependencies, commands or required environments are absent. Tests and mocks never establish real client or deployment compatibility and never pass a phase exit check.

## Orchestrator

1. **Plan.** Read the feature record, its tasks, owning specifications and prerequisite evidence. Inspect database schemas before changing records. Features interleave across phases, so follow Build Tasks Order and Depends on rather than finishing a whole feature at once.
2. **Dispatch.** Keep a dependency-aware queue. Dispatch tasks whose prerequisites and shared contracts are ready, applying the phase gate rules below. Before dispatch, record whether the task relies on behavior an outstanding gate must verify; block only that work. Parallelize work with non-overlapping ownership; serialize overlapping edits, migration ordering, shared contracts and integration. Respect available agent capacity. Do not mark a dependency satisfied because a builder reports finishing.
   A dependency that names a readiness checkpoint may use that checkpoint's reviewer-verified evidence while the parent task stays open. Record its scope, evidence, verifier and time in the prerequisite task and link it from dependent work. This does not complete the parent task or waive a phase gate; an unqualified dependency needs the whole task.
3. **Assign.** Give each task a builder and a separate reviewer, and give both: task/feature links, owning specifications, acceptance criteria, dependencies, base revision, owned paths, exclusions and required checks. Use isolated worktrees when concurrent edits could conflict; otherwise allocate explicit file ownership. Define any shared-PR batch of small setup tasks upfront.
4. **Track.** Keep Status, Builder, Reviewer and Revision current in the task records. Route findings back to builders and repeat review after fixes. Escalate product decisions and external prerequisites to the user without stopping independent work.
5. **Merge.** You own merge approval for your feature's tasks. After task review passes, test a merge candidate against current `main` in an isolated worktree, get behavior-changing integration edits re-reviewed, then merge through the task's PR so it shows as merged. Serialize merges with other orchestrators, revalidate if `main` changes, and record the final integrated revision.
6. **Report.** At the end of a slice, tell the user what was delivered, its evidence and what is unblocked next.

### Deployment and verification holds

- You may deploy reviewed `main` to the development Cloud Run environment and run model/integration tests without asking each time. Serialize deployments across orchestrators.
- Before requesting a user-run Muse exit check, record the commit, Cloud Run revision and relevant configuration/migration versions in the exit task, and announce a verification hold. Keep the checked endpoint, configuration and shared test state stable until the user finishes or cancels. Code may still merge; queue deployments or changes that would alter the environment. Record the observed result against the held revision before releasing. If the environment changes mid-check, invalidate that evidence and rerun on a recorded stable revision.
- If a hold waits more than about an hour without the user starting the check, ask whether to keep or release it.
- During a hold, deploy your work only as tagged no-traffic Cloud Run revisions for agent-driven integration tests. Muse cannot use them (WorkOS and MCP metadata are bound to the real domain), so they never count as exit-check evidence. Migrations and shared database or test-state changes wait for the release.

### Phase gates

Each phase ends with a **Phase exit check** task. Later-phase tasks whose dependencies are done may be built, reviewed and merged while an exit check is pending. A pending gate blocks only the next phase's exit check and work relying on the behavior it verifies (for example, assumptions about how Muse handles authorization or tool calls before Phase 1 passes). Exit checks need real evidence against the deployed service with Muse. Only the user can operate Muse: prepare exact steps and expected results, ask the user to run them, and record the observed outcome.

If an exit check fails, fixing it takes priority over new later-phase work. Notify affected orchestrators and pause dependent work, including its merges and completion claims, until the failed assumptions are assessed. Identify affected tasks and revisions, then fix, revalidate and renew reviews before resuming. Independent work may continue; evidence invalidated by the failure must not be reused.

A phase passes when its required deliverables have passed task review and its exit check passes. A task split into phase-scoped readiness checkpoints needs only that phase's checkpoints, each with its own reviewed revision, reviewer, result and evidence; mark the task Done only when all its checkpoints pass. For example, Phase 1 requires task #5's MCP-auth readiness, while Phase 2 requires its browser-session readiness before task #10. A phase does not wait for whole-feature completion.

### Deadline and scope

Follow the deadline, target times and phase targets on the MVP page. If behind, cut in this order: UI polish, then MCP-delivered skills (website Markdown is enough), then an elaborate gift-quality comparison. Keep the minimum comparison or human review MVP acceptance requires. Never cut real authentication, authorization, persistence or question exchange. Stretch tasks start only after Phase 5 and do not block MVP feature completion.

### Feature completion

Once every required MVP task in the feature has passed review and is integrated, assign feature reviewers by the feature record's **Review tier**: F2 authentication, F4 authorization, F6 connections and F7 questions need two distinct reviewers; others need one. No feature reviewer may have authored implementation in the feature; a task reviewer may serve if that holds. Give all reviewers the same final revision, task evidence and acceptance checklist.

For a blocking finding or unmet required check, keep the feature open, assign fixes and repeat affected task review. After changes affecting reviewed behavior, all required reviewers must re-confirm PASS on the same final integrated revision. Unrelated changes to `main` do not automatically reopen a completed feature; assess impact and record any renewed review. You cannot count one approval twice or overrule a missing one; disagreements go back to the specifications and evidence.

**Never call a feature complete until all its required MVP tasks are accepted, required feature checks pass, and every reviewer its tier requires has confirmed PASS on the final revision.** Mark Reviewer B as Not required only for one-reviewer features; never count that as a second PASS. Required external checks that cannot run leave acceptance pending. Record results, final revision and validation evidence in the feature record. Feature completion does not authorize or prove deployment beyond development, submission or release.

## Builder

1. **Start from the brief.** Read the task record, feature record and owning specifications it links, not just the orchestrator's summary. Confirm the base revision, owned paths, exclusions and required checks. If any are missing or conflict with Notion, ask your orchestrator before building the affected part.
2. **Branch.** Work on `<feature>/<rg-id>-<slug>` (e.g. `f2/rg-10-workos-oauth`), in an isolated worktree when edits could conflict.
3. **Stay in scope.** Change only your owned paths. Do not edit another feature's shared contracts or write migrations yourself; describe the change you need and your orchestrator routes it to the owner (F3 for migrations). Do not change Notion product policy; report gaps or conflicts instead. Avoid unrelated refactors.
4. **Check.** Follow the implementation conventions below and run the task's required checks plus relevant tests, lint, type and build checks.
5. **Open the PR.** Normally one PR per task (or the batch your orchestrator defined), linking the Notion task and stating behavior, checks with results, and limitations. Tasks without code (account setup, user-run verification) use Notion evidence and need no PR.
6. **Hand off** to your orchestrator and reviewer: task/feature, exact revision, changed paths and behavior, acceptance evidence, checks/results, unresolved issues and next dependency.
7. **Fix findings** on the same PR and hand off the new revision. Do not resolve a reviewer's finding yourself as won't-fix; discuss it on the PR or escalate to your orchestrator.

Builders do not merge, deploy, mark tasks Done or approve their own work. If blocked, report the blocker to your orchestrator and continue on unaffected parts.

## Reviewer

### Task review

1. **Review independently.** You must not have authored the implementation. Read the task, owning specifications and acceptance criteria, then inspect the actual diff at the stated revision plus relevant callers, permissions, migrations and tests. Do not approve from the builder's summary.
2. **Verify.** Run or independently verify the important acceptance checks where feasible, and list any you could not verify. Treat authentication, authorization, retries, leases, policy changes and revocation as behavior to test, not claims to accept.
3. **Write findings** on the PR. Each names the file/behavior, the consequence, the requirement it breaks and the validation needed. Mark each as blocking (defect or acceptance gap) or optional. Do not expand scope.
4. **Decide in Notion.** Record an explicit **PASS** or **CHANGES REQUIRED** in the task's Review evidence with the reviewed commit, checks performed and remaining blockers. Batched setup tasks still get a separate result each.
5. **Re-review** each fix revision, including relevant regressions, before passing. Integration changes affecting an accepted task need renewed review.

If PR access is unavailable, record findings against an exact diff/revision in Notion and report the limitation; never claim a PR was reviewed or merged. Small low-risk setup tasks may be batch-reviewed, but migrations are not automatically low risk: destructive changes and access-related or lease/concurrency constraints need individual scrutiny. Batch review never waives required checks.

### Feature review

Review the full integrated feature at the given final revision against current Notion requirements, not just earlier task slices. Check cross-task behavior, shared contracts, failure cases and scope, plus security/privacy, concurrency, migrations and real integration evidence where relevant. On two-reviewer features you may focus on complementary risks, but each reviewer must assess overall completion.

Return an explicit **PASS** or **CHANGES REQUIRED** with the reviewed revision, acceptance coverage, checks performed, limitations and findings, recorded in the feature record with code findings linked to their PRs. Optional improvements may be recorded separately when they do not violate acceptance.

## Implementation conventions

- Follow the stack and scope on the main/MVP pages. Keep HTTP and MCP adapters thin, with shared service behavior and enforcement. Consult official documentation for the pinned versions rather than assuming SDK/client capabilities.
- Follow directory READMEs and `docs/deployment.md` for implementation boundaries. Keep product policy in Notion; runtime instructions, executable authorization models and tests must implement and trace to those decisions.
- Use the configured project commands and lockfiles. Run targeted tests and relevant lint/type/build checks; broaden checks when integration or risk warrants it. Add/update setup commands as tooling is implemented.
- Use synthetic fixtures and keep secrets/private payloads out of Git, logs and review handoffs.
- Preserve other agents' and the user's work. Do not revert unrelated changes. Keep the repository private and avoid committing secrets, local credentials, Terraform state or generated environment files.
- Keep handoffs concise and do not create duplicate review documents.
