# Aiscelapeus operator runbook

This is the supported download-to-running path for the demonstration build. It
does not make the application a clinically validated medical device.

## Fast path: Docker Compose

Prerequisites: Docker Desktop (or Docker Engine with Compose v2), a LiveKit Cloud
project, a Moss project, and Deepgram and Gemini API keys.

```bash
git clone <repository-url> aiscelapeus
cd aiscelapeus
cp .env.example .env.local
```

Fill in `.env.local`, including different high-entropy responder and clinician
access codes, then validate and start:

```bash
docker compose --env-file .env.local config --quiet
docker compose --env-file .env.local up --build -d
docker compose ps
```

The responder UI is at <http://localhost:3000>; the clinician UI is at
<http://localhost:3000/doctor>. Readiness is available at
<http://localhost:3000/api/health>. The endpoint returns only readiness and the
names of missing settings, never credential values.

The judge-safe path is <http://localhost:3000/demo>. It needs no credentials,
microphone, or LiveKit room and is visibly labeled as a recorded simulation. It
replays checked wire fixtures through the production parser/reducer; it is not a
separate mock dashboard.

Stop without deleting images:

```bash
docker compose down
```

`compose.yaml` reads one root `.env.local` into both containers. That file, every
other `.env*` variant, Python/Next build output, local settings, remembered data,
and vendored checkouts are excluded from both Git and the Docker build context.
Only the blank `.env.example` may be committed.

## Native development path

Requires Python 3.11–3.13 and Node.js 20 or newer.

```bash
cp .env.example .env.local
cp .env.example web/.env.local
cd agent
python -m venv .venv
# Windows: .venv/Scripts/python -m pip install -e ".[dev]"
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m aiscelapeus.main download-files
.venv/bin/python scripts/seed_moss.py
.venv/bin/python -m aiscelapeus.main dev
```

In a second terminal:

```bash
cd web
npm ci
npm run dev
```

## Verification before a release

```bash
cd agent
python -m pip install -e ".[dev]"
python -m mypy aiscelapeus
python scripts/render_docs.py --check
python -m pytest -q
python -m aiscelapeus.harness --headless scenarios/

cd ../web
npm ci
npm test
npm run typecheck
npm run lint
npm run build

cd ..
docker compose --env-file .env.local config --quiet
docker compose --env-file .env.local build
```

A tag matching `v*` runs `.github/workflows/release.yml`. It publishes versioned
agent and web images to GitHub Container Registry and uploads a Python wheel plus
a standalone web-server archive to the workflow run. No credentials are used at
image build time or embedded in either artifact.

## Live demonstration

1. Open the responder page and select **Start emergency call**.
2. Copy the incident ID displayed on the responder page.
3. Open `/doctor` in a second browser or private window, paste the ID, and join.
4. The doctor joins muted. Select the microphone control deliberately to speak.
5. If the clinician disconnects after escalation, the responder must see that
   clinician presence was lost; the UI must not continue claiming a bridge.
6. Selecting **End call** closes that incident. A later start shows a different incident
   ID so an archived audit trail and its media binding are never silently reused.

## Hosted judge experience

- Import the repository into Vercel with **Root Directory** set to `web/`, deploy, and
  make `/demo` the zero-friction judging link. The replay itself is static and needs no
  credentials.
- Run the `agent` image on a long-running container host such as Railway,
  Render, Fly.io, Azure Container Apps, or Kubernetes. A Vercel function is not
  the right lifecycle for a persistent LiveKit worker.
- Keep the live responder and clinician routes available behind a separate
  **Try live** action. They require the hosted web environment to contain the
  LiveKit settings plus separate role access codes, and the agent host to
  contain the full runtime set. The codes are a private-demo boundary, not a
  substitute for clinician identity or RBAC.
- Share one primary URL ending in `/demo`; include the live URL and an incident
  walkthrough as optional evidence, not as the only way to understand the work.

For the optional live routes, configure the Vercel project with
`NEXT_PUBLIC_LIVEKIT_URL`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`,
`RESPONDER_ACCESS_CODE`, and `CLINICIAN_ACCESS_CODE`. Configure the worker host with
the LiveKit, Moss, Deepgram, and Gemini settings from `.env.example`. Never put those
values in a Vercel build argument, Dockerfile, repository file, or replay fixture.

Recommended judge flow: replay the poolside-arrest scenario first, switch to the minor
cut to show the counterfactual non-escalation path, then use **Try live** only if the
worker health and microphone permission have been rehearsed. This lets the core system
remain understandable even when venue Wi-Fi, browser permission, or a provider is down.

## Recovery

- `GET /api/health` returns 503: add the named LiveKit or role-access setting to
  `.env.local` and recreate the web container.
- Agent exits during startup: inspect `docker compose logs agent`; configuration
  validation reports all missing agent keys together.
- Protocol retrieval is unavailable: rerun `scripts/seed_moss.py`. The output
  gate fails closed; do not describe an uncited model response as guidance.
- Browser cannot use the microphone: use HTTPS outside localhost and confirm the
  browser permission. The responder cannot run hands-free without microphone
  access.
- Clinician is not auto-paged: external dispatch is not in this build. Share the
  incident ID out of band and keep the UI at **requested / waiting** until the
  participant actually joins.
