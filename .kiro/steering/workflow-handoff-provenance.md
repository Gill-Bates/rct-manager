# Delegation Handoff & Task Provenance

These rules govern how the orchestrator writes handoffs when delegating work to
a workflow or agent (`run_workflow`, `agent://…`, etc.). They exist because a
handoff can silently corrupt a task by duplicating it, by adding instructions
the user never gave, or by losing track of what the real task even was across a
relaunch.

## 1. Delta-only handoffs

A delegated handoff must NOT restate the complete user request when the
authoritative request is already reliably available to the target agent.

Pass only:

- target repository / branch (when reliably known),
- the delegated scope,
- concrete constraints not already available to the target,
- evidence or discoveries made by the delegating agent,
- unresolved questions.

Never duplicate a large requirements document that the target already has. Two
copies of the same requirement are two sources that can drift apart.

## 2. Never invent workflow actions

Never add workflow actions — commit, push, open a PR, code review, run the full
test suite, add a planning phase — unless they are required by the user,
repository policy, or the target agent's execution policy.

Verification depth and commit/test workflow are the target agent's decision
under its own policy. Hand that decision back; do not pre-empt it. "Verify the
affected contracts per repository policy" is correct; "run the full test suite"
is not, unless the user or policy demanded it.

## 3. Task provenance must survive delegation and relaunch

A workflow must distinguish:

- the authoritative **task specification** (what to build),
- later **amendments** to that task,
- **workflow-control** messages (abort, relaunch, pause),
- **meta-feedback** about the delegation mechanism itself.

Workflow-control or meta-feedback must NEVER replace the underlying task
specification merely because it was sent more recently. "Abort and relaunch",
"use the small handoff", or critique of a previous handoff are instructions
about HOW to transport the task — they are not the task.

Three states to recognize:

1. **First delegation** — authoritative task = original user request; handoff =
   delta context only.
2. **User amends the requirements** — authoritative task = original + explicit
   amendment; handoff = delta context.
3. **User gives only workflow / meta feedback** — authoritative task is
   UNCHANGED; the new message is a workflow-control instruction; regenerate the
   handoff but keep pointing at the same original task specification.

When relaunching after meta-feedback (state 3), carry forward the SAME task
specification as the aborted run. Do not let the latest user messages (the
critique, the "abort and relaunch") become the task.

## 4. Do not reference a source the target cannot read

A handoff may point at the original request as authoritative ONLY if the target
agent can actually retrieve that source in its own context. A human-readable
reference like "follow sections 1–33 of the original request" is unsafe if those
sections are not present in what the target receives.

For a relaunch, prefer one of:

- **A — self-contained:** include the complete task specification exactly once
  in the handoff prompt, or
- **B — stable reference:** point at a task ID or artifact the target can
  actually open.

Never defer to an invisible or unverified source. If you cannot verify the
target can read the original, make the handoff self-contained (option A). Even
then, include the task specification only ONCE — self-contained is not a licence
to duplicate.
