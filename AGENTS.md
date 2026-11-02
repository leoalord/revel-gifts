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
| [Onboarding](https://app.notion.com/p/40a2bade7f3181eab40de24dfbe88adb) | Agent guidance and client capability evidence |
| [Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) | Audiences, contribution policy, connections and access rules |
| [Questions](https://app.notion.com/p/40a2bade7f3181bf9975f7fa9a1f597d) | Routing, evidence, lifecycle, leases and deadlines |
| [Research](https://app.notion.com/p/40b2bade7f3181cf9709deca51417f33) | Supporting evidence and historical proposals, not automatic requirements |
| [Hackathon](https://app.notion.com/p/4092bade7f3180909d7cec4d1b230844) | Event context and submission requirements/uncertainties |

Repository READMEs describe layout and operation; original research files are historical. Neither overrides approved Notion decisions. Task records refine scope without silently changing product policy. If sources conflict materially, identify the conflict, continue unaffected work, and obtain a decision before implementing the disputed behavior. If Notion is unavailable, report the missing source and avoid guessing affected requirements.

## Orchestrator

When authorized to execute build work, coordinate builders and reviewers under this process. Creating or reading this file alone does not launch implementation. Use features as ownership units, phases for delivery order, and tasks for independently verifiable work; follow the actual Notion structure rather than creating a competing tracker.

**Kickoff and coordination.** The user is the coordinator. The user kicks off each orchestrator for a feature, or for a feature slice (that feature's tasks within the current phase), and may run several orchestrators in parallel when their work is independent. Features interleave across phases, so follow Build Tasks Order and Depends on rather than finishing a whole feature at once. Parallel orchestrators message each other directly to coordinate dependencies, shared contracts, merge order and handoffs; escalate to the user only when they cannot agree or need a decision or a user-only step. At the end of a slice, report to the user with what was delivered, its evidence and what is unblocked next.

**Task numbering.** Build Tasks "Depends on" refers to the Order number (`#5`); branches use the task's RG ID shown in its ID field. They are different numbers.

1. Read the selected feature record, its tasks, relevant specifications and prerequisite evidence. Inspect the database schemas before changing records; use their actual fields/statuses. Do not invent missing records.
2. Maintain a dependency-aware work queue. Dispatch tasks whose prerequisites and shared contracts are ready, applying the Phase gates rules below rather than requiring every earlier exit check to pass. Before dispatch, record whether the task depends on behavior an outstanding gate must verify; block only that affected work and the next phase's exit check. Parallelize independent work with non-overlapping ownership; serialize overlapping edits, migration ordering, shared contracts and integration. Do not mark a dependency satisfied merely because a builder reports finishing.
   A dependency explicitly naming a readiness checkpoint may use that checkpoint's reviewer-verified evidence even while the parent task remains open. Record its scope, evidence, verifier and time in the prerequisite task, and link it from dependent work. This does not complete the parent task or waive a phase gate; an unqualified dependency still requires the whole task to pass.
3. Assign each task a builder and a separate reviewer. Give both the task/feature links, owning specifications, acceptance criteria, dependencies, base revision, owned paths, exclusions and required checks. Use isolated branches/worktrees when concurrent edits could conflict; otherwise allocate explicit file ownership. Respect available agent capacity.
4. Track progress, blockers and evidence in the existing task records (Status, Builder, Reviewer, Revision, Review evidence). Coordinate integration, route findings back to builders and repeat review after fixes. Escalate product decisions and external prerequisites to the user without stopping independent work.
5. Once every required MVP task for a feature has passed review and its changes are integrated, run the feature-level gate below. Deferred stretch tasks do not block MVP feature completion; track their scope separately. Only then may the orchestrator record the feature complete in its feature record.

### Tracking and review evidence

- Notion is the execution tracker: task scope, dependencies, assignments, status, acceptance evidence and final review decision; feature records hold the feature gate. GitHub PRs hold code diffs, line-specific findings and fix discussions. Link them in both directions rather than copying entire discussions.
- Builders normally open one PR per reviewable code task, linking the Notion task and reporting behavior, checks/results and limitations. Small, closely related setup tasks may share a PR when the orchestrator defines the batch upfront. Each task still needs its own reviewed revision, acceptance result and explicit PASS or CHANGES REQUIRED in Notion's Review evidence. Account/setup or user-run verification tasks without code use Notion evidence and need no artificial PR.
- Reviewers put actionable code findings on the PR and summarize the decision, reviewed commit, checks and remaining blockers in the Notion task. Builders address findings on the same PR; reviewers verify fixes before approval. If PR access is unavailable, record findings against an exact diff/revision in Notion and report the limitation; do not claim a PR was reviewed or merged.
- Use In progress → In review → Changes requested (when needed) → In review → Done, with Blocked for an unresolved dependency. Done requires reviewer acceptance and required checks, not just opening a PR. PR approval does not replace feature-level review. Feature reviewers record final decisions/evidence in the Features record and link code findings to the relevant PRs.

### Deadline and scope

Follow the submission deadline, target times and phase targets on the MVP page. If behind, cut in this order: UI polish, then MCP-delivered skills (website Markdown is enough), then an elaborate gift-quality comparison. Retain the minimum small comparison or human review required by MVP acceptance. Never cut real authentication, authorization, persistence or question exchange to save time. Stretch tasks start only after Phase 5.

### Phase gates

Each phase ends with a task marked **Phase exit check**. Later-phase tasks whose Depends on tasks are done may be built, reviewed and merged while an exit check is pending, so work does not stall waiting for the user to run Muse. What a pending gate blocks: the next phase's exit check, and any work that relies on the behavior the gate verifies (for example, assumptions about how Muse handles authorization or tool calls before Phase 1 passes). Exit checks require real evidence against the deployed service with Muse; tests and mocks alone do not pass a gate. Only the user can operate Muse, so prepare the exact steps and expected results, ask the user to run them, and record the observed outcome.

If an exit check fails, fixing it takes priority over new later-phase work. Notify affected orchestrators and pause dependent work already underway, including its merges or completion claims, until the failed assumptions are assessed. Identify affected tasks and revisions, then fix, revalidate and renew relevant reviews before resuming them. Independent work may continue; earlier evidence invalidated by the failure must not be reused as a pass.

A phase passes when all deliverables required for that phase have passed task review and its exit check passes. For a task explicitly split into phase-scoped readiness checkpoints, require the checkpoints assigned to that phase, each with its own reviewed revision, reviewer, result and evidence; later-phase checkpoints may remain pending. Do not mark the whole task Done until all its required checkpoints pass. For example, Phase 1 requires task #5's MCP-auth readiness, while Phase 2 requires its browser-session readiness before task #10. A phase does not wait for whole-feature completion where a feature spans later phases; feature completion is a separate gate covering its required MVP task set.

### Shared contracts and parallel orchestrators

Several feature orchestrators may run at once. Each feature record lists its **owned shared contracts**; only the owning feature changes them, and other features request changes by messaging that feature's orchestrator. If the owning feature has no running orchestrator, ask the user to start one or to approve a scoped change.

- All database migrations go through F3, which keeps a single Alembic head. Other features specify the schema change they need; F3 adds and orders the migration.
- F2 owns the identity dependency, F4 owns the OpenFGA model and shared authorization service, F1 owns app composition, configuration, dependencies and infrastructure.
- If two orchestrators need the same file or contract at once, the owning feature's orchestrator decides the order; escalate to the user if they cannot agree.

### Branches, merging and deployment

- Builders work on a task branch (`<feature>/<rg-id>-<slug>`, e.g. `f2/rg-10-workos-oauth`) in an isolated worktree when edits could conflict.
- The orchestrator owns merge approval for its feature's tasks. After task review passes, it tests a merge candidate against current `main` in an isolated branch/worktree, re-reviews behavior-changing integration edits, then merges through the task's PR so the PR shows as merged. Serialize merges with other orchestrators and revalidate if `main` changes. Record the final integrated revision.
- The orchestrator may deploy reviewed `main` to the development Cloud Run environment and run model/integration tests without asking each time.
- Serialize deployments across orchestrators. Before requesting a user-run Muse exit check, record the commit, deployed Cloud Run revision and relevant configuration/migration versions in the exit task, and announce a verification hold to the other orchestrators. Keep the checked endpoint, relevant configuration and shared test state stable until the user finishes or the check is explicitly cancelled. Later code may merge independently, but queue deployments or changes that would alter that verification environment. Record the observed result against the held revision before releasing the hold; if the environment changes mid-check, invalidate the affected evidence and rerun against a recorded stable revision.
- If a hold has waited more than about one hour without the user starting the check, ask the user whether to keep or release it; never let a hold sit unattended.
- During a hold, orchestrators may deploy their work as tagged Cloud Run revisions that receive no live traffic and run agent-driven integration tests against those revision URLs. Muse cannot use them (WorkOS and MCP metadata are bound to the real domain), so they never count as exit-check evidence. Migrations and changes to shared database or test state still wait until the hold is released.
- Obtain explicit user approval for Terraform applies that create or change infrastructure, DNS changes, WorkOS dashboard configuration, GitHub repository settings and the hackathon submission, unless the user has already authorized that specific action. Prepare changes and plans for review before requesting approval. Local implementation and checks continue while approval is pending.

### Steps only the user can do

Escalate these as soon as a task needs them; tasks that need one are marked **Needs user** in Build Tasks. Creating the GCP project and billing; WorkOS account and dashboard settings; DNS for `revel.gifts`; GitHub repository ownership; Muse accounts and connecting Muse to Revel; API keys for Muse Spark; the hackathon rules check; recording the video and submitting.

## Task build and review loop

- The builder implements the assigned scope, runs appropriate checks and hands off the exact revision/diff, changed paths, acceptance evidence and remaining limitations. A builder's completion message means ready for review, not done.
- The reviewer must be a different agent who did not author the implementation being reviewed. Inspect the actual code and relevant callers, permissions, migrations and tests; do not approve from the builder's summary alone. Independently run or verify the important acceptance checks where feasible. Report unverified checks explicitly.
- Findings identify the affected file/behavior, consequence, requirement and concrete validation needed. Distinguish blocking defects/acceptance gaps from optional improvements. Avoid unrelated scope expansion.
- Return fixes to the builder. The reviewer verifies the changed revision and relevant regressions, then gives an explicit pass or requests further changes. Never self-approve implementation.
- A task is complete only when its reviewer passes the final task revision, required acceptance checks pass, and changes are integrated or verifiably included in the feature review candidate. Integration changes affecting that task require renewed review.

## Mandatory feature completion gate

Review effort follows the feature record's **Review tier**: F2 authentication, F4 authorization, F6 connections and F7 questions require two feature reviewers; other features require one. Small, low-risk setup tasks may be reviewed in a batch by one task reviewer, with a separate explicit result for each task. Migration-only changes are not automatically low risk: destructive changes and access-related or lease/concurrency constraints require individual scrutiny. Batch review never waives required acceptance checks.

After all required MVP tasks pass, assign the independent feature reviewer(s) required by the review tier. No feature reviewer may have authored implementation in that feature. A task reviewer may serve if they meet this condition. Each reviews the full integrated feature against current Notion requirements, not merely their earlier task slices. Give all required reviewers the same final revision, task evidence and acceptance checklist. For two-reviewer features, use two distinct agents; they may focus on complementary risks, but each must assess overall feature completion.

Each reviewer returns an explicit **PASS** or **CHANGES REQUIRED**, identifying the reviewed revision, acceptance coverage, checks performed, limitations and findings. Check cross-task behavior, shared contracts, failure cases and scope; include security/privacy, concurrency, migrations and real integration evidence where relevant.

For any blocking finding or unmet required check, keep the feature open, assign fixes and repeat affected task review and validation. After changes to the feature or its dependencies that affect reviewed behavior, **all reviewers required by the review tier must re-confirm PASS on the same final integrated revision**, considering the full feature and the new changes. Unrelated changes to `main` do not automatically reopen a completed feature; assess impact and record any required renewed review. Review disagreements return to the specifications/evidence; the orchestrator cannot count one approval twice or overrule a missing approval. Optional improvements may be recorded separately when they do not violate acceptance.

**Never call a feature complete until all its required MVP tasks are accepted, required feature checks pass, and all independent reviewers required by its review tier explicitly confirm completion on the final revision.** Mark Reviewer B as Not required only for one-reviewer features; never count that as a second PASS. Required external checks that cannot run leave acceptance pending; mocks do not establish real client/deployment compatibility. Record the review results, final revision and validation evidence in the feature record. Feature completion does not itself authorize or prove deployment beyond the development environment, submission or release.

## Implementation conventions

- Follow the stack and scope on the main/MVP pages. Keep HTTP and MCP adapters thin, with shared service behavior and enforcement. Consult official documentation for the pinned versions rather than assuming SDK/client capabilities.
- Follow directory READMEs and `docs/deployment.md` for implementation boundaries. Keep product policy in Notion; runtime instructions, executable authorization models and tests must implement and trace to those decisions.
- Use the configured project commands and lockfiles. Run targeted tests and relevant lint/type/build checks; broaden checks when integration or risk warrants it. Do not claim tests passed when dependencies, commands or required environments are absent. Add/update setup commands as tooling is implemented.
- Treat authentication, authorization, retries, leases, policy changes and revocation as behavior to verify, not model assurances. Use synthetic fixtures and keep secrets/private payloads out of Git, logs and review handoffs.
- Preserve other agents' and the user's work. Do not revert unrelated changes. Keep the repository private and avoid committing secrets, local credentials, Terraform state or generated environment files.
- Keep handoffs concise: task/feature, exact revision, changed behavior, checks/results, unresolved issues and next dependency. Do not create duplicate review documents or invent commits, deployments or completion evidence.
