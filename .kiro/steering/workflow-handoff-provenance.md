# Delegation Handoff & Task Provenance

These rules govern how the orchestrator writes handoffs when delegating work to
a workflow or agent (`run_workflow`, `agent://…`, etc.). They exist because a
handoff can silently corrupt a task by duplicating it, by adding instructions
the user never gave, or by losing track of what the real task even was across a
relaunch.

## 0. Authority and accessibility override brevity

A delta-only handoff is only safe when the authoritative task source is both
stable and verifiably accessible to the target. Otherwise the handoff must be
self-contained (§4, option A). This precedence settles the tension between
"keep the handoff small" (§1) and "never point at a source the target cannot
read" (§4): accessibility wins over brevity, every time.

A source counts as **verifiably accessible** only when the orchestrator knows a
concrete identifier or context mechanism that the target can actually resolve in
its own session — a task ID it can open, an artifact path it can read, or an
input the runtime guarantees to deliver. "It was earlier in the chat" and "the
workflow should see it" do not qualify: the delegating session's history is not
the target's context. If you cannot name how the target reaches the source,
treat it as inaccessible.

## 1. Delta-only handoffs

When the authoritative request is verifiably accessible to the target (§0), the
handoff must NOT restate it. Pass only the delta:

- target repository / branch (when reliably known),
- the delegated scope,
- concrete constraints not already available to the target, both positive
  ("stay within the parser module") and negative ("do not commit", "do not
  touch generated files", "do not change the public API") — a prohibition is as
  load-bearing as a requirement and is lost just as silently,
- evidence or discoveries made by the delegating agent,
- unresolved questions.

Do not create a second normative copy of the task when an accessible
authoritative source already exists. Size is not the test: even a short, precise
requirement block is dangerous when it is maintained in parallel with the
authoritative source, because the two can drift apart.

## 2. Delegate outcomes, not execution mechanics

Delegate the required outcomes, constraints, risks, and acceptance criteria. Do
not prescribe execution mechanics — commit, push, open a PR, code review, run
the full test suite, add a planning phase — unless those mechanics are
themselves required by the user, repository policy, or the target agent's
execution policy.

Verification depth and the commit/test workflow are the target agent's decision
under its own policy. Hand that decision back; do not pre-empt it. The
distinction is between *what must hold* and *how to achieve it*:

- Correct (an acceptance criterion): "The regression at the parser boundary
  must be verified." "Verify the affected contracts per repository policy."
- Wrong (an execution mechanic): "Run `pytest tests/parser`, then the full
  suite." "Run the full test suite" — unless the user or policy demanded it.

A risk the orchestrator discovered is a legitimate acceptance criterion to pass
down; the procedure for discharging it is not.

## 3. Task provenance must survive delegation and relaunch

A workflow must distinguish:

- the authoritative **task specification** (what to build),
- later **amendments** to that task,
- **workflow-control** messages (abort, relaunch, pause),
- **meta-feedback** about the delegation mechanism itself.

Recency is not authority. Workflow-control or meta-feedback must NEVER replace
the underlying task specification merely because it was sent more recently.
"Abort and relaunch", "use the small handoff", or critique of a previous handoff
are instructions about HOW to transport the task — they are not the task.

Classify each user message by its clauses, not by its headline. A single message
can carry more than one kind of instruction; split it and route each clause to
its role.

Four states to recognize:

1. **First delegation** — authoritative task = original user request; handoff =
   delta context only.
2. **User amends the requirements** — authoritative task = original + explicit
   amendment; handoff = delta context.
3. **User gives only workflow / meta feedback** — authoritative task is
   UNCHANGED; the new message is a workflow-control instruction; regenerate the
   handoff but keep pointing at the same task specification.
4. **Mixed message** — a message carries both substantive task amendments and
   workflow/meta instructions ("relaunch with a smaller handoff, and also change
   X to Y"). Apply only the substantive clauses to the authoritative task
   specification; treat the rest as workflow-control. Relaunch against the
   updated specification.

When relaunching after meta-feedback (states 3 and 4), carry forward the SAME
task specification as the aborted run, plus any substantive amendment the latest
message added. Do not let the critique or the "abort and relaunch" become the
task. Prefer carrying forward a stable task identity across relaunches — a task
ID or canonical artifact (§4, option B) that amendments attach to — so you never
have to reconstruct "original + amendments" from conversation history, which is
itself a drift source.

## 4. Do not reference a source the target cannot read

A handoff may point at the original request as authoritative ONLY if the target
agent can actually retrieve that source in its own context (§0). A
human-readable reference like "follow sections 1–33 of the original request" is
unsafe if those sections are not present in what the target receives.

For a relaunch, use one of:

- **A — self-contained:** include the complete task specification exactly once
  in the handoff prompt, or
- **B — stable reference:** point at a task ID or artifact the target can
  actually open.

Never defer to an invisible or unverified source. If you cannot verify the
target can read the original, make the handoff self-contained (option A). Even
then, include the task specification only ONCE — self-contained is not a licence
to duplicate.
