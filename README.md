# AGX RSI Coach

A local strategy-improvement harness for [AGX](https://agx.cool/) racing and FPS competitions. It reads your match feedback, asks a language model for a focused revision, validates the result, and keeps a local draft before releasing it to AGX.

Use **oMLX**, **OpenAI**, **OpenRouter**, or another **OpenAI-compatible Chat Completions API**. The submitted game agents never call the language model during a match.

## Setup

Requires **Python 3.11+** on **macOS or Linux**. Process locks and session deadline timers use POSIX APIs. oMLX is optional; hosted model APIs work on either OS.

```bash
git clone https://github.com/CaiusL/agx-rsi-coach.git
cd agx-rsi-coach
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Register for your competitions on AGX and put your own account API key in `.env` as `AGX_API_KEY`. Registration happens on the AGX website. Choose a model provider below, then check the installation:

```bash
python -m coach self-check
python -m unittest discover -s tests -q
```

The repository includes the public racing template and validated track assets. Checks do not require the original developer's account, local history, or `state/` directory.

## Choose a model provider

Edit `.env`, then restart the coach and dashboard. Change the base URL, model ID, and key together.

**oMLX:** start a compatible local server and choose a model installed on your machine.

```dotenv
LLM_PROVIDER=omlx
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=Qwen3.8-27B-4bit
LLM_API_KEY=
```

**OpenAI:** choose a Chat Completions model available to your account.

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=your-model-id
LLM_API_KEY=your-api-key
```

**OpenRouter:** use its provider/model identifier.

```dotenv
LLM_PROVIDER=openrouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=provider/model-id
LLM_API_KEY=your-api-key
```

For other services, use `LLM_PROVIDER=openai_compatible` with their Chat Completions base URL, model and API key. Direct Anthropic Messages endpoints need a compatible gateway.

Hosted adapters omit oMLX-only `top_k` and `chat_template_kwargs`. OpenAI uses `max_completion_tokens` and omits sampling by default for broad reasoning-model compatibility. Other compatible APIs use `max_tokens`. Override `LLM_TOKEN_FIELD` or `LLM_SEND_SAMPLING` only if your model supports those parameters. Hosted providers never automatically fall back to local Qwen models. Explicit optional fallbacks use comma-separated `LLM_FALLBACK_MODELS`.

Existing `OMLX_*` settings remain supported for oMLX installations. API keys stay on the backend and are not returned to the dashboard.

See the [OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat) and [OpenRouter reference](https://openrouter.ai/docs/api_reference/overview) for model-specific support. API connectivity alone does not guarantee a model can generate valid game agents.

## Run

Start one coach:

```bash
python -m coach run
```

In a second terminal, activate the same environment and start the dashboard:

```bash
python -m coach ui
```

Open **http://127.0.0.1:8765**. New installations start under manual control: starting the service alone does not generate proposals or upload submissions.

- **Run sim + improve** starts a per-game session. It repeats local revisions, retains validated drafts, and schedules one selected draft for release **12 minutes before the next round published by AGX**.
- Model work stops one minute before release. A whole-cycle deadline covers retries and network reads, keeping generation out of the shared release window.
- Three failed or functionally repeated proposals pause generation and retain the selected draft for release. There is no fixed successful-iteration limit. Network/model-service failures use separate backoff and do not consume the no-progress budget.
- **Stop session** stops after the current cycle and cancels scheduled uploading.
- **Upload to AGX** submits the exact selected snapshot immediately, without generating another proposal. It stops that game's timed session.

Each game has its own controls and queue. A completed timed session can start the next round's session after a new played result arrives. Missed release windows leave drafts local. Failed or interrupted admissions remain visible for review; the session does not silently select another candidate.

Starting a session authorizes its scheduled upload and repeated model calls. Hosted calls use your provider's billing. AGX quotas still apply; set a positive `AGX_MAX_UPLOADS_PER_DAY` for a local account cap.

Run only **one coach process** against a checkout's `state/` directory. Keep the dashboard on loopback.

Other commands:

```bash
python -m coach status       # local state and AGX schedule
python -m coach gate         # deterministic FPS fixture
python -m coach dry-run      # local book proposals, no upload
python -m coach rewrite      # local FPS rewrite, no upload
```

Pause the running coach before commands that modify strategy state, including `dry-run` and `rewrite`.

## Counted series and release files

Set `COUNTED_START_AT` to a timezone-aware timestamp to separate trial results from a counted period. For example, `2026-11-15T00:00:00+08:00` means midnight in Shanghai. Trial logs remain available for diagnosis; the dashboard's counted match totals and streak exclude earlier results. Local release numbering starts at v1 and advances after confirmed admission/publication, independently of AGX's assigned version IDs and the local build counter.

Reviewed snapshots are exported under ignored `state/releases/<series>/` folders, with versioned agent/strategy files, a submission ZIP and a checksum manifest. ZIP bytes are reproducible, and upload checks that they match the reviewed archive. These local files contain the selected agent/book; raw training logs and credentials are excluded from the ZIP and from Git. A readiness lock holds the first reviewed snapshot until its scheduled release; a new round's session resumes normal experimentation after its result.

Scheduled releases take priority over unrelated manual generation. Model work respects enabled sessions' cutoffs. A process lock prevents duplicate coaches. If release confirmation is interrupted, the previous local live slot is restored and the saved bundle ID, stage, hash and idempotency keys remain available for reconciliation; an uncertain remote result is not reported as a confirmed rejection or blindly retried.

## Evidence and validation

The coach reads public event summaries and downloads **your own authorized seat's** training export:

```text
GET /api/v1/python-submissions/jobs/{match_id}/training.jsonl?seat={zero_based_seat}
```

It handles preparation (`202` and `Retry-After`), verifies completion, seat identity, row/byte counts and the decision-byte SHA-256. Streaming decision rows may omit `type`; the parser and calibration tools support that encoding. Raw logs, summaries, model traces, generated candidates and account state stay in ignored `state/` files.

The FPS collector also downloads the public `frames`, `events`, and `static` exports under `/api/v1/python-submissions/recordings/{match_id}/`. It uses the same bulk endpoints as the AGX download buttons, keeps frames gzip-compressed on disk, and analyzes them in a background worker. The result compares the actual top killer with the own seat, purchases, movement, incoming bearings and deaths before visible contact. Public replay is post-match evidence; it is never an extra live-agent input.

Racing uses the same background export cache. Its analysis separates qualifying and race weather, compares the actual classified winner and published car designs, and records fuel, tyres, energy, incidents and samples preceding retirement. Own-log controls and version attribution remain separate from public replay truth. Racing diagnosis uses bounded thinking and must return measured evidence, a validation plan and uncertainty; the accepted plan appears with the strategy decision. After a DNF, completion evidence takes priority over forced exploration of an unused car field. Track-specific finish history uses actual played values rather than names. A race becoming wet after a car retired does not establish a wet-condition failure.

Set `state/retention.json` to `{"mode":"latest_per_game"}` to retain raw data only for the latest verified match of each game. Older cached match files are deleted only after a Markdown summary is saved under `state/match-history/<game>/`. The source files downloaded manually into another directory are untouched. In this mode, subsequent validation uses the latest match and closed-loop fixtures and explicitly reports **no historical held-out replay**. Markdown aggregates provide history, not replay inputs.

A `round_authority` reservation can pin an explicitly direct-authored draft until a round starts. It blocks model generation and rejects uploads of a different identity or author while retaining the scheduled release. Diagnosis and planning for future FPS and racing model runs enable bounded thinking and emit a concise evidence, plan and uncertainty summary. Artifact implementation and naming remain separate direct-output calls.

FPS candidates pass deterministic behavior fixtures. Verified observations are replayed chronologically in a separate subprocess to detect exceptions, invalid returns and errors swallowed into empty live actions. Own-log motion envelopes can calibrate the approximate route simulation. Large exports stream to temporary files in a background worker; only complete, verified logs enter evaluation.

FPS workflow v2 starts independent experiments from the live policy instead of stacking unplayed rewrites. Its evidence packet contains the latest versioned K/D/A, damage, actual purchases, completed objectives, verified own-seat observations, and source-linked public samples from leading players. Opponent source and private decisions remain unknown. Public research is bounded to three matches and cached for an hour. Diagnosis uses bounded thinking followed, when needed, by a small structured-decision call; implementation and naming use direct output. Existing policies use function edits from the first attempt.

Selection uses up to five verified traces ordered by match date rather than download time. With at least three traces, the two oldest form a separate validation split whose examples are withheld from prompts. Applied controls and adjacent observations identify effective yaw/pitch response; sparse validation traces can inherit mechanics measured in development matches, explicitly marked as transferred. A one-step angular-control and firing-discipline audit rejects aim regressions, reload/switch conflicts and ineffective held-trigger requests. Extra fire commands alone cannot pass. Ineffective firing is compared by count over identical observations; percentages using each policy's own request count are diagnostic, preventing a shrinking denominator from falsely rejecting fewer bad requests. Navigation/objective response checks and all-seat functional fixtures remain protected. The fixture uses physics-tick observations, separate yaw/pitch rates and pistol/USP trigger cycling, and no longer mandates a peek pattern or seat-specific spawn delay.

A candidate that repairs a failing functional contract can be staged as an **offline contract repair** if it passes the gate and the independent corpus shows no regressions. It need not invent a combat gain on inputs unaffected by the bug. Other combat candidates require a measured control or discipline gain. Passing selects an **offline-tested experiment**, not a proven champion. The candidate also has to improve on an already selected v2 experiment. Upload revalidates against the live policy and the current evaluator. Subsequent real results are attached by played version and match ID; one observed result does not establish a causal improvement. Missing validation evidence triggers a data retry. The approximate route search does not evaluate shooting or policy strength.

Objective diagnoses require carrier evidence. Static source patterns and zero plants by themselves do not establish body-blocking or routing failure. Completed sessions wait for a result created in their target round before starting the next cycle.

FPS regression checks also exercise a blocked source-adjacency shortcut with a traversable detour, collision jitter, crouched and partially visible targets, and sustained MP9 contact. The navigation check requires reaching the destination; a single recovery gesture cannot pass it. Combat fixtures use the published body sample heights, both players' stances, cooldowns and trigger state. Replay aim audits use the same visible-part geometry and `aim_punch`, and report rounds without contact and extended lack of net movement. A corrected camera cannot take effect on fixed recorded inputs, so fewer recorded firing responses are tolerated only when geometric control improves and all closed-loop combat regressions pass. These local geometry checks do not model engine raycasts or predict kills.

Racing keeps the controller frozen. The model proposes one setup/control lever at a time, with separate dry/wet hypotheses. Numeric plans/mechanics match Parkland, Highland and Crossover; checksums protect distributed assets. Timed sessions compare independent racing hypotheses against a fixed session baseline.

These are **local approximations**, not AGX's engine. FPS route estimates omit shooting and full 3D physics. Recorded-input replay cannot simulate how changed actions alter the world. Racing simulation does not model the causal effects of fuel, wing or controller-book changes. A selected draft is a validated hypothesis, not a proven best strategy or guaranteed win-rate improvement. This workflow changes strategies/code; it does not fine-tune model weights.

## Project layout

- `coach/`: model client, prompts, evidence, validation, timed sessions and dashboard.
- `runtime/`: packaged game agents and the racing asset dispatcher.
- `assets/racing/`: public frozen template, track plans, manifest and numeric asset builder.
- `agx-*-kit/`: AGX map readers, calibration, example agents and approximate simulations.
- `sim/params/`: example approximate parameters; per-user measured outputs are ignored.
- `tests/`: synthetic regression fixtures and clean-checkout checks.
- `state/`: private runtime data, created locally and excluded from Git.

## Combat-focused revisions and KDA

FPS summaries distinguish confirmed kills, deaths and explicitly published assists. Multi-attacker kill events have no guaranteed killer/assist ordering, so shared involvements remain separate and missing assists stay unknown. The dashboard labels recorded K/D/A with the version that played.

A requested combat goal uses per-round kills/deaths, published damage, opening deaths and deaths without firing as diagnostic evidence. Plant attempts and completed plants are separate; published purchase events are distinct from fired-weapon lists. Assists stay unknown if not explicitly attributed. Geometric control audits do not model ballistics, body-part raycasts, changed-world trajectories, survival or match wins. AGX's actual matches remain the performance test.

See [NOTICE](NOTICE) for third-party attribution and license scope.
