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
- Three failed or functionally repeated proposals pause generation and retain the selected draft for release. There is no fixed successful-iteration limit.
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

## Evidence and validation

The coach reads public event summaries and downloads **your own authorized seat's** training export:

```text
GET /api/v1/python-submissions/jobs/{match_id}/training.jsonl?seat={zero_based_seat}
```

It handles preparation (`202` and `Retry-After`), verifies completion, seat identity, row/byte counts and the decision-byte SHA-256. Streaming decision rows may omit `type`; the parser and calibration tools support that encoding. Raw logs, summaries, model traces, generated candidates and account state stay in ignored `state/` files.

FPS candidates pass deterministic behavior fixtures. Verified observations are replayed chronologically in a separate subprocess to detect exceptions, invalid returns and errors swallowed into empty live actions. Own-log motion envelopes can calibrate the approximate route simulation.

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

See [NOTICE](NOTICE) for third-party attribution and license scope.
