---
name: revel-get-started
description: Resume Revel setup, build a sourced gifting profile with owner consent, and arrange truthful owner-approved inbox checks in a supported assistant host.
version: 0.8.2
status: active
---

# Set up or resume Revel Gifts

Help me set up Revel Gifts with this assistant. Resume what already exists; do not duplicate
my profile, approvals, or recurring tasks. Use only tools currently in your tool list.

## Resume and build a useful profile

1. Call `whoami`, `get_setup` and `get_profile` without a handle. Confirm that the connected
   Revel account is mine. If a required tool is absent, explain the missing capability and
   stop that part. Signing into Revel does not authorize every host capability.
2. Load `revel-manage-profile` using this host's supported prompt or resource path
   (revel://guidance/revel-manage-profile). Reading guidance is not proof of skill loading.
   Follow its owner confirmation and current policy rules. Before examining context, ask
   permission to use only context this assistant can legitimately access. Never claim access
   to saved memories or other assistants' conversations unless this host actually provides it.
3. If I have not approved a contribution policy, preview the exact proposed settings through
   `set_contribution_policy`, show the returned wording and obtain my explicit approval.
   Use its confirmation protocol. Public sharing needs my policy-level opt-in; do not infer
   consent from connecting an assistant or joining through an invitation.
4. Under the approved policy, contribute supported, useful interests, likes, dislikes,
   constraints and wishes with `add_facts`. Preserve provenance, qualitative confidence,
   evidence dates and uncertainty. Never upload raw conversations, third-party secrets,
   never collect home address or birth year. Respect tighter audiences and held reviews. Compare the
   existing profile first; avoid duplicate or contradictory assertions. Ask only consequential
   missing questions, often zero and no more than five initially.
5. Summarize what was added and who can see it, including pending/provisional items and
   correction options. Encourage connecting other personal assistants that may know useful
   different details. Additional assistants are optional; never broaden sharing automatically.

## Arrange one recurring inbox check

1. Read `get_setup` again. It contains current preferences, the designated delegation, and
   any host task reference. Confirm my local IANA timezone; never infer it from a server
   clock. Propose checks at 6 am, 10 am, 2 pm and 6 pm local time, using one designated
   assistant. Explain that scheduling is a capability of this assistant's host.
2. Check whether this host can actually create, inspect, update and disable recurring tasks.
   If it cannot, say scheduled checks are unavailable or unverified for this host. Do not
   claim setup succeeded from a prompt, tool description or a stored preference. Interactive
   inbox checks still work. Question email fallback is not implemented in this setup; do
   not present it as enabled or promise mail delivery. Native MCP Events is separate and
   not verified by polling setup.
3. Reconcile existing host tasks and the recorded task reference BEFORE creating anything.
   If another assistant is designated, have me choose which single assistant should run;
   retire the old host task before replacing it. If you cannot inspect it, ask me to confirm
   its state rather than create a duplicate blindly.
4. Use `configure_inbox_checks` with action schedule, the current revision, desired hours,
   timezone and designated delegation, initially without owner confirmation. Show the exact
   returned confirmation wording, obtain my approval, then repeat the same request with
   owner confirmation. A stale revision requires a fresh read and renewed approval.
5. Now actually create or update ONE recurring task through this host's scheduling tool,
   with my approval. Use the run instructions below. If it fails or is unavailable, leave
   setup awaiting the host task and report that accurately. Only after the host confirms
   the task exists, use `configure_inbox_checks` action report_task with the task reference
   and current revision. This is explicitly a host-reported task, not independent verification.
6. Show desired schedule, host task report, last successful Revel check, and background
   verification separately. A successful `get_updates` proves a check, not that it was
   unattended. Arrange a harmless unattended test with the owner and record the host's
   actual run evidence before claiming background operation; Revel currently leaves that
   verification unverified. Never manufacture a host run or subscription acknowledgment.

## Instructions for each host-scheduled run

Check that this is still the designated assistant and the recorded schedule remains enabled.
If the schedule is disabled or assigned elsewhere, disable this host task and finish quietly.
Call `get_updates` exactly once. If its items are empty, end quietly without a notification,
summary, user message or unnecessary profile changes. An error is not an empty inbox; preserve
it as a failed check and follow host error handling without claiming success.

For a pending question assignment, load `revel-answer-question`. Re-check authorization,
claim the assignment before working, preserve the lease version and deadline, and use
minimal shareable evidence. Claim the exclusive human-contact lease before asking me a
question, report actual contact, and reject stale or late completion. Never expose the
requester's private surprise purpose, bypass policy, or ask me twice because another
assistant already contacted me. Unknown item types need a supported handler; do not guess.

## Change or disable

Read setup and inspect the existing host task first. Confirm changes to hours, timezone or
designated assistant with me using a fresh revision and the exact confirmation preview.
Update the actual host task, then report its task reference again. Repeated setup with the
same preferences should reuse the existing task, not create another.

To disable, obtain my approval, record disabled preferences through `configure_inbox_checks`,
then actually disable or delete the recurring task in its host. Only after the host confirms
it stopped, report task disablement. Turning off Revel preferences cannot stop a host task
by itself. Keep outstanding host action visible if the host cannot be reached.
