# AGX RSI Coach — change history

Prepared: **14 November 2026, 22:17, Asia/Shanghai**.

This document covers the work discussed from the original handoff through the FPS workflow rebuild. It was checked against `HANDOFF.md`, local Git history, the current implementation, and saved runtime state. Early changes are recorded retrospectively; they are not all new edits from the latest request. Runtime names and schedules below are a snapshot and can change as the enabled sessions continue.

## 1. Starting point and ownership

The project began this conversation as a local oMLX-driven improvement loop for AGX racing and FPS. The original handoff documented several weaknesses: repeated ineffective proposals, racing controller oscillation, weather overfitting, FPS movement problems, and local fixtures that did not reproduce actual match performance.

The standing division of responsibility was preserved:

- **oMLX authors game strategy values and FPS agent behavior.**
- The operator changes the harness, prompts, evidence, validation, scheduling, packaging and dashboard.
- The racing driver stays frozen. You separately authorized selecting track-matched numeric plan/mechanics assets.
- Keys and private training logs stay local. Submitted agents do not call the language model during matches.
- RSI here means revising strategies and code using feedback. It does **not** fine-tune the model's weights.

## 2. First harness and prompt repairs

### Proposal repetition and racing oscillation

The early work recorded in the handoff:

- Removed `hold_winning_pace`, which repeatedly overrode racing controller values and encouraged a `kv` oscillation.
- Corrected functional book comparison so equivalent numeric values such as `22` and `22.0` do not masquerade as changes.
- Added bounded retries for proposals that make no functional change.
- Required a focused experiment rather than changing unrelated settings together.
- Retained candidate and decision artifacts so rejected attempts can be inspected.

### Initial FPS movement diagnosis

Early detectors and prompts called attention to repeated movement requests without progress, crowding near common destinations, carrier positioning, and inadequate combat contribution hidden by team wins.

Those early measures included a prescribed jiggle peek and seat-based departure staggering. **The later FPS rebuild removed those prescriptions as mandatory requirements.** Movement symptoms remain evidence to investigate; a static source pattern or an unchanged position alone does not prove body-blocking.

### Service operation

- Kept one coach process for the shared state directory.
- Used detached service restarts and checked for active model calls before stopping the coach.
- Added cleanup of stale model-call records after interruptions.
- Kept the dashboard on loopback at `http://127.0.0.1:8765`.

Relevant implementation: `coach/loop.py`, `coach/prompts.py`, `coach/omlx.py`.

## 3. Racing corrections

### Track-matched assets

After your authorization, the harness gained track-matched `plan.npz` and `mechanics.npz` selection for Parkland, Highland and Crossover.

- Added distributed track assets, bindings and checksum validation.
- Added a runtime dispatcher to select the matching track data.
- Preserved the frozen underlying driver rather than opening racing code RSI.
- Avoided silently treating every track as Parkland.

The frozen driver SHA-256 verified during the rebuild was:

```text
75209f824f2bce550f651a420cac384138b3997eb9db5673f76ce81ece53ae74
```

### Wet/dry interpretation and simulation

The workflow now states that A/B are **baseline and experiment**, not separate wet/dry presets.

- Added separate dry and wet hypotheses for the same proposed book.
- Compared track, conditions, retirement status and asset binding before attributing a pace difference to setup.
- Added a fixed dry/damp/wet context grid rather than letting the latest race's weather define the comparison.
- Made simulation limitations explicit: the racing approximation does not consume the proposed fuel, wings or controller values, so its unchanged estimate cannot establish that those fields are ineffective.
- Required unavailable simulated strategy effects to remain unknown instead of inventing a predicted time gain.

### Fuel and wing exploration

- Added coverage tracking for played values and local trial values separately.
- Included fuel load, front wing and rear wing alongside controller gains in the model's alternatives.
- Added an exploration requirement when repeated controller changes leave primary car fields untested.
- Kept value selection model-authored and within the existing legal bounds.
- Used a fixed session baseline for independent racing trials instead of treating cumulative unplayed changes as demonstrated improvements.

Relevant implementation: `coach/racing_assets.py`, `coach/racing_workflow.py`, `coach/sim.py`, `coach/recording.py`, `runtime/racing/agent.py`, `assets/racing/`.

## 4. Per-game controls, local drafts and uploads

### Fixed cross-game requests

The FPS and racing controls now use separate game-scoped queues and request IDs.

- A manual FPS request is claimed for FPS; it does not request a racing improvement.
- Duplicate clicks are deduplicated.
- Conflicting queued/running actions are rejected rather than silently merged.
- Requests carry audit records and visible completion/error states.
- Interrupted jobs can be recovered instead of leaving a button permanently busy.

If both timed sessions are enabled, both can still run independently. That is different from one manual click accidentally requesting both games.

### Separated improvement from submission

- Local improvement runs in a working copy.
- Passing candidates become local drafts.
- Generating a revision does not itself submit it.
- **Upload to AGX** submits the selected snapshot without generating another proposal.
- Candidate IDs protect against uploading a stale or different snapshot.
- Upload rechecks the selected draft using current validation.
- Failed admission or validation retains the draft and exposes the failure.
- A failed candidate does not silently replace the selected version.

The controls subsequently evolved to your requested timed-session behavior: **Run sim + improve starts repeated local work and authorizes one scheduled release**. The separate manual upload button remains available.

Relevant implementation: `coach/manual_jobs.py`, `coach/loop.py`, `coach/ui.py`, `coach/web/app.js`.

## 5. Repeated improvement sessions and scheduling

Added a per-game session lifecycle:

1. Read AGX's published next round.
2. Run local experiments while time remains.
3. Retain a selected draft and reject repeated or regressing proposals.
4. Stop model work one minute before the release time.
5. Attempt one selected release **12 minutes before the round**.
6. Record the real result before the normal next-round rollover.

Additional controls:

- Three no-progress attempts stall generation instead of looping endlessly.
- Infrastructure failures use a separate retry/backoff path rather than consuming the experiment budget.
- Functional fingerprints detect repeated proposals despite renamed revisions.
- Release intent and selected ID are saved before network submission.
- Missed windows retain local drafts rather than uploading a newly chosen candidate late.
- Manual upload stops that game's timed session.
- Stop-session requests preserve the selected draft and disable scheduled uploading.
- Both games share deadline awareness so one model cycle should not consume the other's release window.

### Failed AGX rounds

Investigated `FREEZE_UNAVAILABLE` and distinguished a platform scheduling failure from a completed match loss. A failed scheduled round may have no match or training data.

The FPS rebuild added recovery from an observed void round: it can resume when AGX publishes a genuinely usable future window, without fabricating a match result or extrapolating an unpublished schedule.

### Operational limitation encountered

The FPS rebuild maintenance overran an earlier scheduled release window. That window was missed; the service retained the live version. Separately, AGX reported failed rounds with no new completed FPS match. The evidence does not establish that maintenance caused those AGX failures.

Time-reporting correction: **12:00 UTC is 20:00 Shanghai, 14:00 UTC is 22:00 Shanghai, and 16:00 UTC is 00:00 the following day.** An earlier explanation incorrectly associated 12:00 UTC with 22:00. The persisted schedule uses AGX's timezone-aware timestamps.

Relevant implementation: `coach/sessions.py`, `coach/loop.py`.

## 6. AGX training-data integration

Investigated the API and integrated authorized own-seat training exports into the feedback loop.

- Downloaded completed-match exports for the account's actual seat, which can change between matches.
- Handled export preparation, `202`, and `Retry-After`.
- Verified completion, seat identity, decision counts, byte counts and SHA-256 before trusting a file.
- Supported decision rows that omit a `type` field.
- Added streaming downloads to temporary files and a background worker for large exports.
- Cached verified summaries and invalidated stale summaries when their schema changes.
- Replayed recorded observations chronologically in a separate process to detect exceptions, invalid returns and swallowed errors.
- Added observed movement/control calibration while explicitly distinguishing it from identified engine physics.

The endpoint documented for the current installation is:

```text
GET /api/v1/python-submissions/jobs/{match_id}/training.jsonl?seat={zero_based_seat}
```

Public recordings and private decision exports remain separate. Other players' private training logs are not downloaded.

Relevant implementation: `coach/agx.py`, `coach/training.py`, `coach/replay_training.py`.

## 7. GitHub publication and configurable providers

Prepared the project for reuse under the repository you approved:

[CaiusL/agx-rsi-coach](https://github.com/CaiusL/agx-rsi-coach)

- Added setup instructions and `.env.example`.
- Added configurable oMLX, OpenAI, OpenRouter and OpenAI-compatible Chat Completions providers.
- Preserved compatibility with existing `OMLX_*` settings.
- Kept provider-specific request parameters separate, including token fields, sampling and oMLX extensions.
- Avoided automatic fallback from a hosted provider to a local model.
- Kept keys on the backend and excluded private state, logs, credentials and generated artifacts from Git.
- Included public assets, attribution/license files and clean-checkout tests.
- Added CI configuration.

**Publication boundary at the 22:17 snapshot:** the local published-project commit was `59832c4` (`Publish AGX RSI coach with configurable model providers`). At that snapshot, the later loss-accounting, KDA and FPS workflow changes were modified/untracked local files. Later publication status is recorded in Git history and the accompanying chat.

Relevant implementation: `coach/config.py`, `coach/omlx.py`, `.env.example`, `.gitignore`, `.github/workflows/tests.yml`, `README.md`, `LICENSE`, `NOTICE`.

## 8. Corrected loss-streak accounting

Added an idempotent result ledger independent of A/B transitions, proposals and uploads.

- Deduplicated matches by ID.
- Ordered results chronologically, including late arrivals.
- Recomputed the streak from completed eligible competition matches.
- Kept account scope and the known boundary result visible.
- Excluded platform failures and non-result events from completed-match losses.
- Distinguished a match loss from individual deaths and lost rounds.

A 0–13 FPS match contributes **one completed match loss**, not thirteen match losses. A prior team win resets that match-based streak even if the seat's own combat contribution was poor. The KDA display now provides that separate contribution evidence.

Relevant implementation: `coach/outcomes.py`, `coach/loop.py`, `tests/test_loss_streak.py`.

## 9. KDA, purchases and objective telemetry

### Attribution

- Counted confirmed kills using an explicit killer or a sole attacker.
- Kept unordered shared kill involvements separate.
- Left assists and derived KDA ratios unknown unless explicitly published.
- Added K/D, per-round combat metrics, published damage, opening kills/deaths and deaths before firing.
- Attached dashboard statistics to the version that actually played, rather than the latest unplayed draft.

### Corrected misleading evidence

- Distinguished fired-weapon lists from actual inventory and successful purchase events.
- Added actual completed purchases to avoid diagnosing “no gun bought” from “no weapon fired.”
- Separated plant attempts from completed plants and deduplicated completion/detail events.
- Labeled hit-event/fire-event ratios as event ratios rather than treating them as a complete ballistic accuracy model.
- Required carrier/opportunity evidence before interpreting zero plants as an objective failure.

### Earlier FPS selection approach, now superseded

The first paired evaluator tracked blocked-motion responses, objective responses and ready engagement windows. A candidate once increased covered windows from **5 to 15**.

**That was not proof of better shooting or KDA.** The later rebuild found that raw fire requests could improve this proxy while preserving ineffective trigger behavior. Those engagement-count metrics remain diagnostic; extra firing commands alone no longer establish a combat gain.

Relevant implementation: `coach/fps_combat.py`, `coach/recording.py`, `coach/fps_signals.py`, `coach/ui.py`, `coach/web/app.js`.

## 10. FPS workflow rebuild

### A. Replaced tactical mandates with evidence-driven experiments

Removed mandatory aggression, one-peek-per-round rules and prescribed seat-based departure delays from the FPS prompts/selection requirements. Preserved functional checks for legal actions, navigation, carrier planting, defusing, reloading and combat responses.

The evidence packet now prioritizes:

- The latest played version and its actual result.
- Verified own-seat observations and applied actions.
- Current source code and a current baseline audit.
- Recent experiment/rejection history.
- Public top-player observations with source links and limitations.

### B. Corrected fixture mechanics

- Expressed observation clocks in physics ticks: three physics ticks per 20 Hz decision.
- Separated yaw and pitch turn response; observed effective rates were approximately 3 and 2 radians/second for full input.
- Modeled pistol/USP trigger release instead of counting every held-fire request as a shot.
- Added a trigger-cycle fixture that requires repeated actual USP shots.
- Added strict action type, range and finiteness checks instead of silently coercing or clipping invalid actions inside the fixture.

A concrete own-log finding was **285 ready USP requests with the trigger already held and no ammo consumed**, versus five requests after a released trigger that consumed ammo. This supplied evidence for a trigger-state defect that the old fixture missed.

### C. Added independent control-quality validation

- Expanded evaluation to up to five verified match traces.
- Selected by known match chronology, not download recency.
- Reserved the two oldest traces as a separate validation split when enough data exists.
- Withheld those validation examples from model reports.
- Fitted effective turn response from adjacent observations and applied controls.
- Marked transferred calibration assumptions when a sparse trace lacked motion samples.
- Audited one-step aim error, aligned ready firing, ineffective trigger holds, movement while firing, reload/switch conflicts and protected objective/navigation responses.
- Rejected extra fire commands as a standalone gain.
- Kept regression checks per trace so one match's gain cannot conceal another's degradation.

These checks do **not** simulate changed-world trajectories, collisions, body-part raycasts, damage, survival or wins. The older validation matches have also been used by previous iterations; this split is an engineering regression check, not an untouched scientific test set.

### D. Changed candidate selection and recovery

- Started independent hypotheses from the live version instead of cumulatively stacking unplayed tactical revisions.
- Required a candidate to improve on an already selected v2 experiment before replacing it.
- Added an explicit **offline contract repair** classification when a failing functional contract is repaired without replay regressions.
- Allowed an action-equivalent, verified contract repair to supply a safe generation base without treating it as a tactical victory.
- Retained selected drafts and passing artifacts when later naming, transport or validation stages failed.
- Attached subsequent real match results to played-version experiment history without claiming one result proves causality.

### E. Refined model calls

Earlier diagnosis and editing calls explicitly disabled thinking for latency/structured output. The rebuild changed FPS behavior diagnosis to bounded thinking, with a separate small structured-decision call if reasoning consumes the response budget. Fixture repairs, implementation and naming use direct output.

Existing policies now use bounded replacement-function requests from the first attempt, avoiding repeated full-file generation. Candidate names still need to satisfy the existing never-admitted-name rule.

Relevant implementation: `coach/fps_workflow.py`, `coach/fps_quality.py`, `coach/fps_compare.py`, `coach/fps_evaluation.py`, `coach/gate_fps.py`, `coach/prompts.py`, `coach/loop.py`.

## 11. Public top-player research

Added bounded research using AGX's public leaderboard, completed match listings, public event streams and current game contract.

- Cached research for one hour.
- Limited each refresh to up to three public matches and leading-player samples.
- Included observed K/D, damage, firing, purchases and objective statistics.
- Preserved match URLs and noted that participant-to-leaderboard matching may be based only on display name.
- Kept opponents' private source, inventory, decision traces, routes and intentions unknown.

Examples retrieved during the rebuild:

- The rank-1 sample recorded **22 confirmed kills / 12 deaths** and **137.51 published damage per round** on Dust2. [Public match](https://agx.cool/#/matches/163c69a6-22da-490e-a4ab-4d3b61a37120)
- A top-five Borgo sample recorded **12 kills / 13 deaths** and **76.75 damage per round**. [Public match](https://agx.cool/#/matches/0e91b77c-d76a-4f69-a7db-d26057ae9cf2)

These observations are benchmarks for hypotheses. They do not reveal a “best player's strategy” that can be copied, and different teammates, opponents, versions and roles confound comparisons.

Relevant implementation: `coach/fps_reference.py`.

## 12. Snapshot after the rebuild

- Live FPS version: **`quick-peek`**.
- Latest completed FPS match: `fe1daa77-95fe-4fd6-898f-ed2fb246935b`, started **14 November at 18:00 Shanghai time**.
- Recorded result: **0 kills / 11 deaths / assists unknown**, K/D **0.00**, team rounds **0–13**.
- Selected local draft: **`stagger-jiggle`**, ready but not uploaded at the snapshot.
- Classification: **offline contract repair**, with no combat improvement claimed.
- Model-authored repair: changed `fight()` to keep the final recoil-adjusted action within legal bounds.
- Validation: functional checks passed; **132,031 recorded inputs** across five traces produced unchanged actions, with two validation traces. This supports compatibility/reliability, not improved kills.
- The earlier **`staggered-jiggle`** draft is a different candidate; it was held after the new trigger-cycle check exposed its single-shot behavior.
- FPS timed session was enabled, with release at **23:48 on 14 November**, ahead of the AGX-published **00:00 on 15 November** round. Release still depends on validation and admission.
- Racing's saved timed session showed `missed_window`; FPS void-round recovery should not be read as confirmation that racing had recovered or uploaded.

## 13. Verification and remaining limits

Last verified before this documentation change:

- **122 unit tests passed.**
- Racing and FPS bundle self-checks passed.
- JavaScript syntax checks passed.
- `git diff --check` passed.
- One coach and one dashboard process were confirmed running.
- The frozen racing driver checksum was unchanged.

Regression coverage includes queue isolation, draft/upload boundaries, provider parameters, training verification, loss accounting, kill/assist attribution, trigger cycles, control calibration, action legality, independent baselines, validation-example separation, reasoning-only response recovery and contract-repair classification.

The important unresolved performance question is still the real one: **does a newly played FPS version improve useful damage, kills, survival and objectives?** The rebuilt harness is designed to reject misleading local gains and preserve that distinction. It has not yet established a higher real-match win rate.

This request creates a change-history document only. It does not push the newer local code to GitHub, upload a game agent, or rewrite the original handoff. Existing background sessions may continue according to their saved settings.


## 14. Counted-series readiness audit and v1 reset

The user confirmed that counted games begin **15 November 2026 at 00:00 Shanghai time**, and requested local file versions restart from v1. Added `COUNTED_START_AT` configuration and persisted series metadata. Trial history, platform IDs, admitted-name history and training evidence are preserved. Local release numbering advances after confirmed publication, not after every model attempt. The dashboard distinguishes trial results from counted results and shows the counted-series loss streak separately.

Prepared **fps-v1** and **racing-v1** source files, deterministic submission ZIPs and checksum manifests in ignored local release folders. Preflight validated the selected FPS source against the current functional fixture, strict chronological replay and the five-trace comparative audit; racing packaging retained the frozen driver and checksum-checked track assets. Both reviewed snapshots are held fixed for the first counted round, with the existing **23:48 Shanghai** scheduled release. A new session can resume experimentation after real round feedback.

Additional release-path corrections:

- Reproducible ZIP metadata removes wall-clock and source-file timestamp differences from reviewed/submitted bytes.
- Prepared archives are checked against their content identities; changed reviewed artifacts are rejected rather than silently replaced.
- Due releases take priority over unrelated manual requests, even when a stale session has an older timestamp.
- Manual model work respects the shared scheduled-release cutoff.
- Deadline and uncertain publication errors restore the prior local live slot instead of leaving an unconfirmed candidate marked live.
- Bundle hash, admission ID, request idempotency keys, publication stage and confirmation are saved as the request progresses. Uncertain remote outcomes require reconciliation instead of blind retry.
- Recorded-input replay validates action values/types/ranges and fingerprints its action validator, invalidating stale permissive caches.
- An operating-system file lock prevents duplicate coach processes from owning the same state directory.

Added regression coverage for the v1 reset, trial/count boundary, confirmed-release numbering, exact archive bytes, artifact tampering, action validation, release priority, deadline rollback, uncertain publication and singleton locking. **132 unit tests passed**, along with both bundle self-checks, JavaScript syntax and whitespace checks.

At the API audit, AGX published the midnight round for both games, while FPS still reported **FREEZE_UNAVAILABLE**. Local readiness does not resolve or guarantee recovery of that platform scheduler error. No immediate game-agent upload was performed during preflight.


## 15. Combat-selection correction after the admission test

The immediate upload test admitted the v1 reliability repair. A subsequent focused, model-authored combat revision exposed a selection error: the harness compared ineffective-fire percentages using each policy's own firing-request count. On one trace, bad requests fell from 25 to 12 and useful aligned responses remained at 4, but the percentage rose because the total request count fell from 38 to 16. The old check incorrectly rejected that improvement.

Corrected selection to compare ineffective request counts over the same recorded observations, while preserving aligned ready-fire responses and checking aim, objective and runtime regressions. The changing-denominator percentage remains diagnostic only. Added a regression test for this exact case and documented the observed `trigger_state.pressed` field in the model's verified observation contract.

The model-authored `fan-peek` candidate then passed the functional gate, chronological replay and all five comparative traces. Ineffective firing requests fell from **376 to 51** in total; all **35 aligned ready-fire responses** remained covered. Neither validation trace regressed. This is evidence of changed firing-control behavior, not proof of higher real KDA or win rate. The complete suite passed **134 tests** before publication.


The v2 upload passed AGX admission but its first activation attempt returned HTTP 409 because the competition was paused. After the pause cleared, publication of the same admitted bundle succeeded before midnight; no duplicate bundle upload was needed. Added separate admission-versus-publication error reporting and a regression test. The final suite passed **135 tests**. The published combat version still requires real counted-match feedback to establish any KDA gain.
