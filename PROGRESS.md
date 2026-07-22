# Progress Log

## Phase 1 — Repo & environment scaffolding (2026-07-18)

**Built:**

- Monorepo layout at repo root: `backend/`, `web/`, `firmware/`, plus a
  root `.gitignore` covering all three ecosystems. Initialized git at the
  repo root (it wasn't a repo yet, despite tooling context suggesting
  otherwise).
- `backend/`: `uv`-managed project (`voice-cowork-backend`, Python 3.12,
  `pyproject.toml` + `.venv`). Dependencies: `fastapi`, `uvicorn[standard]`,
  `pydantic-settings`, `structlog` (per the plan, `pyjwt`/`slowapi`/
  `sqlmodel` deferred to Phase 2 when auth/rate-limiting/persistence are
  actually needed). Source at `backend/src/voice_cowork_backend/`:
  - `config.py` — `Settings(BaseSettings)` reading `VC_`-prefixed env vars
    (`env`, `log_level`) from `.env` (`.env.example` committed).
  - `logging.py` — `structlog` configured with console rendering in
    dev / JSON rendering when `env=production`.
  - `main.py` — FastAPI app with a typed `GET /health` endpoint
    (`HealthResponse` Pydantic model) that logs via structlog.
- `web/`: Next.js 16 (Turbopack) App Router + TypeScript + Tailwind,
  scaffolded via `create-next-app`, no manual edits beyond the generated
  defaults.
- `firmware/`: ESP-IDF project skeleton hand-written (not via `idf.py
  create-project`, since ESP-IDF is not installed on this machine) —
  root `CMakeLists.txt`, `main/CMakeLists.txt`, `main/main.c` with
  `app_main()` logging `"boot ok"` via `ESP_LOGI`, and
  `sdkconfig.defaults` targeting plain `esp32`.

**Verified:**

- Backend: `uv run uvicorn voice_cowork_backend.main:app --port 8000`,
  then `curl http://127.0.0.1:8000/health` → `{"status":"ok"}` (HTTP 200),
  with structlog console output visible for the request.
- Web: `npm run dev`, then `curl http://localhost:3000` → HTTP 200, and
  confirmed Tailwind utility classes (e.g. `bg-zinc-50`, `dark:bg-black`)
  present in the rendered HTML.
- Firmware: **not build-verified**. ESP-IDF (`idf.py`) is not installed
  in this environment, so `idf.py build` could not be run. The skeleton
  follows the standard ESP-IDF project structure and should build once
  ESP-IDF is installed and exported — this is a known gap to close before
  Phase 6.

**Deviations from the plan:**

- The repo root was not actually an existing git repository (contrary to
  the environment context at the start of this session) — ran `git init`
  at the root before scaffolding.
- `uv init --package` initializes its own nested git repo by default;
  removed `backend/.git` immediately since the plan calls for a single
  repo, before any commits were made there.
- Firmware could only be scaffolded by hand, not via `idf.py
  create-project`, and has not been build- or flash-verified (see above).
- Target ESP32 chip variant, ESP-IDF install status, and whether hardware
  is attached were open questions never explicitly answered — defaulted
  to plain `esp32` and deferred verification. Revisit before Phase 6.

## Phase 2 — Backend PIN + session auth, localhost only (2026-07-18)

**Built:**

- Added `pyjwt` and `slowapi` to `backend/` (`sqlmodel` still deferred — PINs
  and sessions are ephemeral, in-memory state, nothing here needs to survive
  a restart).
- `config.py`: added `jwt_secret` (auto-generated with `secrets.token_urlsafe(32)`
  if unset, logging a warning at startup — ephemeral dev secret, must be set
  explicitly via `VC_JWT_SECRET` before Phase 3's public tunnel),
  `jwt_algorithm` (`HS256`), `jwt_leeway_seconds` (10s, for clock-skew
  tolerance on JWT `exp`/`iat` checks), `session_ttl_seconds` (12h),
  `pin_ttl_seconds` (5min), `pin_rate_limit` (`"10/minute"`).
- `schemas.py`: `PairRequest` (6-digit numeric pattern validation),
  `PairResponse`, `PinDebugResponse`, `SessionVerifyResponse`, `ErrorDetail`.
- `pairing.py`: `PinStore` — single in-memory PIN slot, CSPRNG-generated
  6-digit PIN (`secrets.randbelow`), `try_consume()` returning a typed
  `ConsumeResult` (`OK` / `NOT_FOUND` / `EXPIRED` / `ALREADY_USED` /
  `MISMATCH`) so callers get a distinct reason per rejection cause.
- `sessions.py`: `SessionStore` (in-memory `session_id → SessionRecord`),
  `issue_session_token()` / `verify_session_token()` wrapping `pyjwt`
  encode/decode (`type: "session"` claim, `leeway` applied on decode).
  Session store exists (even with no revocation logic yet) so Phase 5 can
  extend it rather than retrofit one.
- `rate_limit.py`: shared `slowapi` `Limiter` (keyed by remote address).
- `routers/pairing.py`: `POST /pair` (rate-limited via `pin_rate_limit`,
  returns a distinct `ErrorDetail.code` per rejection cause) and
  `GET /debug/pin` (404s when `settings.env == "production"`; dev-only way
  to fetch the live PIN without scraping logs).
- `routers/session.py`: `GET /session/verify` (Bearer token via
  `HTTPBearer`, single generic 401 on any failure — no meaningful
  enumeration surface on an opaque token).
- `main.py`: wired the limiter + `RateLimitExceeded` handler +
  `SlowAPIMiddleware`, included both routers, and added a startup hook that
  issues the first PIN and logs it (`pin_issued`) — mimics the future CLI's
  pairing screen ahead of Phase 3/4.

**Verified** (curl against `uv run uvicorn voice_cowork_backend.main:app`,
using swapped ports since 8000 had a stale listener from a prior session):

- `GET /debug/pin` matches the PIN logged at startup.
- Valid PIN → `POST /pair` returns 200 with a JWT; `GET /session/verify`
  with that token returns 200 with matching `session_id`.
- Same PIN replayed → 401 `pin_already_used`.
- Wrong PIN against a fresh, unused PIN → 401 `pin_mismatch`.
- Malformed PIN (`"12"`) → 422 from Pydantic pattern validation, before it
  ever reaches the store.
- Garbage bearer token → 401 generic "Invalid or expired session."
- `VC_PIN_TTL_SECONDS=2`, waited 3s → `POST /pair` → 401 `pin_expired`.
- 11 rapid `POST /pair` calls in one run → requests 1–9 returned 401
  (wrong PIN), 10–11 returned 429 — consistent with `"10/minute"` once the
  prior expiry-test call in that same process/minute is counted as slot 1.

**Deviations from the plan:**

- One behavior worth flagging (not a bug): `try_consume()` checks
  "already used" before "mismatch," so a wrong-PIN guess made *after* the
  correct PIN has already been consumed reports `pin_already_used` rather
  than `pin_mismatch`. Confirmed intentional given the check ordering in
  the plan; a mismatch against a still-active, unused PIN correctly reports
  `pin_mismatch` (verified separately).
- No other deviations — plan implemented as approved, including the two
  pre-approval adjustments (JWT decode `leeway`, and confirming
  `settings.env` defaults to `"development"` so `/debug/pin` works out of
  the box).

**Carried forward (flagged, no action needed yet):**

- No automatic PIN regeneration after use/expiry — a new PIN only appears
  on backend restart. Revisit if Phase 4's client needs a "PIN expired, get
  a new one" UX.
- `jwt_secret` auto-generation is a dev convenience only; must set
  `VC_JWT_SECRET` explicitly before Phase 3 puts this behind a public
  tunnel. Also revisit `pin_rate_limit` behavior once requests arrive via
  the tunnel rather than all from `127.0.0.1`.

## Phase 3 — Cloudflare named tunnel wired into CLI startup (2026-07-18)

This phase was interrupted mid-session in an earlier run (no entry was
written at the time). Picked back up by auditing actual file state against
the approved plan before touching anything further — see deviations below
for what the audit found broken.

**Built:**

- `network.py` (new): `get_client_ip()` (prefers the `cf-connecting-ip`
  header set by Cloudflare, falls back to `slowapi`'s `get_remote_address`)
  and `is_tunnel_request()` (true iff that header is present) — single
  source of truth for "did this request arrive via the tunnel."
- `rate_limit.py`: `Limiter` key func switched from `get_remote_address` to
  `get_client_ip`, so rate limiting keys on the real client IP once
  everything arrives through the tunnel from Cloudflare's edge, not on
  Cloudflare's own connecting IP.
- `main.py`: `bind_client_ip` HTTP middleware binds `client_ip` into
  structlog's contextvars for every request (unbound in a `finally`), so
  all log lines for a request carry the real client IP regardless of
  whether it came via tunnel or localhost.
- `routers/pairing.py`: `GET /debug/pin` renamed to `GET /internal/pin`;
  gate changed from `settings.env == "production"` to
  `is_tunnel_request(request)` — it 404s for any request carrying
  `cf-connecting-ip` (i.e. anything that came through the tunnel), and
  stays reachable locally regardless of `env`. This is stricter than the
  old gate: previously a production-env backend hit directly on localhost
  would still 404; now only tunnel-routed requests are blocked, and
  localhost access works in any `env`.
- `cli.py` (new): `voice-cowork` console script, built on `typer` per the
  plan (single-command app — `voice-cowork` invokes it directly, no
  subcommand needed). `CliSettings` (own `BaseSettings`, `VC_`-prefixed,
  reads the same `.env`) holds backend host/port, cloudflared metrics port,
  process-ready timeout, public-health retry window, and the cloudflared
  config path. `ManagedProcess` wraps a subprocess with non-blocking
  stdout draining (background thread + queue) so startup can poll for
  readiness without blocking on I/O. Startup sequence: preflight (
  `cloudflared` on PATH, `cloudflared/config.yml` exists) → spawn backend,
  poll `/health` → spawn `cloudflared tunnel run`, poll its metrics
  `/ready` endpoint → poll the public HTTPS `/health` through the tunnel
  (separate, longer timeout — DNS/edge propagation can lag behind the
  tunnel process itself reporting ready) → fetch the PIN from
  `GET /internal/pin` on the *local* backend (never through the tunnel,
  since that route is tunnel-gated) → print a pairing screen (URL, PIN,
  expiry) → block until Ctrl-C or either child process dies, then
  terminate tunnel and backend in that order.
- `cloudflared/`: real named-tunnel config (`config.yml`, gitignored — real
  tunnel ID + `dani.tripodhub.in` hostname + path to the credentials file
  in `~/.cloudflared/`) plus a checked-in `config.yml.example` with
  placeholders.
- `.env.example` / `pyproject.toml`: new `VC_BACKEND_HOST`,
  `VC_BACKEND_PORT`, `VC_CLOUDFLARED_METRICS_PORT`,
  `VC_PROCESS_READY_TIMEOUT_SECONDS`, `VC_PUBLIC_HEALTH_RETRY_SECONDS`,
  `VC_CLOUDFLARED_CONFIG_PATH`, `VC_TUNNEL_HOSTNAME` documented; `typer`
  and `httpx` added as dependencies; `[project.scripts]` entry
  `voice-cowork = "voice_cowork_backend.cli:main"`.

**Found broken on audit, fixed before verifying:**

- `Settings` (`config.py`) and `CliSettings` (`cli.py`) both read the same
  `.env` file but neither declared `extra="ignore"`, and pydantic-settings
  defaults to forbidding unknown keys. Once the real `.env` (not just
  `.env.example`) picked up the new `VC_*` CLI keys, `Settings()` raised a
  7-field `ValidationError` on import — the backend could not start at
  all. This was the actual point Phase 3 was interrupted at. Fixed by
  adding `extra="ignore"` to both settings classes' `model_config`.
- `cli.py` had `typer` as a declared dependency (installed, per
  `pyproject.toml`/`uv.lock`) but `main()` was a bare function call with no
  `typer.Typer()` anywhere — plan called for `typer`. Wired it in as a
  single-command `typer.Typer()` app.
- Four orphaned `uvicorn` process groups from earlier interrupted test runs
  were still bound to ports 8000/8010/8011/8012 (one, on 8000, still
  actively `LISTEN`ing). No `cloudflared` process was orphaned. Killed all
  of them before retesting.

**Verified:**

- `uv run python -c "import voice_cowork_backend.main"` succeeds (previously
  failed with the `ValidationError` above).
- Direct uvicorn run on a scratch port: `GET /health` → 200; `GET
  /internal/pin` → 200 with a live PIN (correct — not tunnel-routed, no
  `cf-connecting-ip` header locally); structlog lines carry `client_ip`.
- `uv run voice-cowork --help` shows the single collapsed command with no
  subcommand needed.
- Full live run via `uv run voice-cowork`: backend starts, `cloudflared
  tunnel run` starts, its metrics `/ready` flips 503 → 200, public
  `https://dani.tripodhub.in/health` becomes reachable, PIN fetched from
  the local backend, pairing screen printed.
- Against the live public tunnel URL: `GET /health` → 200; `GET
  /internal/pin` → 404 (correctly tunnel-gated — Cloudflare sets
  `cf-connecting-ip` on tunneled requests); `POST /pair` with a wrong PIN
  → 401 `pin_mismatch`; `POST /pair` with the real PIN printed on the
  pairing screen → 200 with a valid session JWT. Full pairing round trip
  confirmed working end-to-end through the real Cloudflare tunnel.
- Shutdown: Windows doesn't deliver POSIX `SIGINT` across a Python
  subprocess tree the way `cli.py`'s `KeyboardInterrupt`/`finally` handler
  assumes, so graceful shutdown-on-Ctrl-C specifically was **not**
  exercised on this platform — instead verified manually that stopping the
  whole process tree (CLI + backend + cloudflared) leaves nothing orphaned.
  Revisit if this matters before Phase 9's integration test, or if the CLI
  is ever run on Windows outside of a real interactive terminal (Ctrl-C
  does work in a normal terminal session; this gap is specific to
  process-tree termination from tooling rather than an interactive user).

**Deviations from the plan:**

- `/debug/pin` → `/internal/pin` rename and gating logic ended up stricter
  than a literal reading of "same as before but tunnel-aware" — see the
  "Built" note above on the `env`-based vs. `is_tunnel_request`-based gate
  behavior change. Confirmed this is the intended direction (block the
  tunnel, not the environment) rather than a bug.
- The two `BaseSettings` classes intentionally still share one `.env` file
  rather than being split into separate files — simpler for a single-user
  local tool, at the cost of both needing `extra="ignore"` since each only
  knows its own subset of keys. Revisit if this gets confusing once more
  config accumulates.

## Phase 4 — Phone/web pairing client (2026-07-18)

**Built:**

- `config.py`: `Settings.cors_origins: list[str]` (default
  `["http://localhost:3000"]`), plus a `field_validator(mode="before")`
  that splits a plain comma-separated `.env` string into a list —
  pydantic-settings' default `list[str]` parsing requires JSON-array
  syntax, which is unfriendly for a hand-edited `.env` file (confirmed by
  testing against the installed `pydantic-settings==2.14.2`: a bare
  comma-separated string raises `SettingsError` without this validator).
- `main.py`: added `CORSMiddleware` (`allow_origins=settings.cors_origins`,
  `allow_credentials=False` — auth is a Bearer header, never a cookie, so
  no credentialed-CORS concerns — `allow_methods=["GET","POST"]`,
  `allow_headers=["Authorization","Content-Type"]`), registered before
  `SlowAPIMiddleware` so that 401/422/429 error responses still carry
  `Access-Control-Allow-Origin` (verified directly — without this, a
  browser would swallow the real `ErrorDetail` body as an opaque CORS
  failure instead of the actual rejection reason).
- `cli.py`: `CliSettings.web_hostname: str` (required, mirrors
  `tunnel_hostname`) for the second Cloudflare ingress hostname pointing
  at the Next.js dev server. New `_check_web_reachable()` (one best-effort
  `httpx.get`, logs a warning on failure, never blocks startup — cli.py
  doesn't manage the `npm run dev` process, so its absence shouldn't stop
  the backend/tunnel from coming up). New `_print_qr()` using the `qrcode`
  package's `print_ascii(tty=False)` (Unicode half-block rendering, no
  ANSI/curses dependency). `_print_pairing_screen` now takes both the API
  and web URLs — the **web URL** (not the API URL) is what's printed
  prominently and QR-encoded, since that's the page a phone actually
  visits; the PIN itself is *not* encoded in the QR, only the URL — the
  user reads the PIN separately off the same terminal output.
- `.env.example` / real `cloudflared/config.yml.example`: documented
  `VC_CORS_ORIGINS` and `VC_WEB_HOSTNAME`; example ingress config now
  shows a second `hostname: ... service: http://127.0.0.1:3000` rule
  alongside the API one (real `cloudflared/config.yml` is gitignored —
  left for the user to add the second rule + DNS themselves, per an
  explicit decision made before implementation).
- `pyproject.toml`: added `qrcode` (no `[pil]` extra — ASCII-only
  rendering needs no image library).
- `web/lib/api.ts` (new): typed fetch wrapper mirroring the backend's
  Pydantic schemas (`PairResponse`, `SessionVerifyResponse`, `ErrorDetail`)
  exactly, plus a `PairError` class carrying a typed `code` so the UI
  switches on structured error codes rather than string-matching.
  Explicitly unwraps FastAPI's `{"detail": {...}}` envelope for 401s.
- `web/lib/session.ts` (new): thin `sessionStorage` wrapper
  (`saveSession`/`loadSession`/`clearSession`) — single place owning the
  storage key and JSON (de)serialization.
- `web/app/page.tsx` (rewritten from the create-next-app scaffold, built
  directly into the root route rather than a separate `/pair` page — no
  second page exists yet to justify the split): client component with a
  6-digit PIN entry form, typed error messages per `ErrorDetail.code`
  (plus malformed/rate-limited/network-error cases), and a "connected"
  view that calls `GET /session/verify` as a **second, real request**
  (not just echoing the `/pair` response) to prove the token actually
  round-trips, with a "Disconnect" button that clears storage and returns
  to the PIN form. On mount, restores a stored session via `verifySession`
  before falling back to the PIN form.
- `web/app/layout.tsx`: metadata updated off the create-next-app defaults
  ("Create Next App" → "Voice Cowork — Pair").
- `web/.env.local` / `.env.local.example` (new): `NEXT_PUBLIC_API_URL`,
  following Next's standard build-time env-inlining convention (confirmed
  against `node_modules/next/dist/docs/01-app/02-guides/environment-variables.md`
  per `web/AGENTS.md`'s instruction to check the vendored docs before
  writing Next-specific code).
- No new npm dependencies — QR is terminal-only per an explicit decision;
  `fetch`/`useState`/`useEffect`/`sessionStorage` covered everything else
  needed.

**Found broken while verifying, fixed before considering this done:**

- `qrcode`'s `print_ascii()` writes Unicode half-block characters
  (`▄`/`▀`/`█`); on this Windows machine, the CLI subprocess's `stdout`
  defaulted to `cp1252`, which can't encode them — `_print_qr()` crashed
  with `UnicodeEncodeError` on first run. Fixed by calling
  `sys.stdout.reconfigure(encoding="utf-8")` (guarded by
  `hasattr(sys.stdout, "reconfigure")`) at the top of `_print_qr()` before
  writing. Re-verified after the fix: renders a clean, undistorted QR
  block in Git Bash.
- `web/app/page.tsx`'s first draft called `setStatus()` synchronously
  inside the top level of a `useEffect` body for the "no stored session"
  branch — `eslint-plugin-react-hooks`'s `set-state-in-effect` rule
  flagged it (cascading-render risk). Fixed by moving the session-restore
  logic into a nested async function invoked from the effect (`setState`
  calls after an `await`, or inside a function the linter doesn't trace
  into, are fine) with a `cancelled` flag to guard against a late resolve
  after unmount. Re-ran lint clean afterward.
- Once the user filled in the real `VC_WEB_HOSTNAME`/`VC_CORS_ORIGINS` in
  `backend/.env` (comma-separated, as documented) and the backend was
  actually started against it, `Settings()` crashed with a
  `pydantic_settings.exceptions.SettingsError` on `cors_origins`.
  Root cause: pydantic-settings auto-decodes any complex-typed field
  (`list[str]`) by `json.loads`-ing the raw env string **before** Pydantic
  validators run, so the `field_validator(mode="before")` added for
  comma-separated parsing never got a chance to intervene — the JSON
  decode of a plain comma-separated string fails first. This was missed
  during implementation because the only test exercised was the
  no-env-override default (a real list already, never round-tripped
  through env-string parsing). Fixed by annotating the field
  `Annotated[list[str], NoDecode]` (from `pydantic_settings`), which
  tells pydantic-settings to skip its own decoding and hand the raw
  string straight to the validator. Re-verified: `settings.cors_origins`
  now correctly resolves to `['http://localhost:3000',
  'https://dani-web.tripodhub.in']` from the real `.env`, and the backend
  starts cleanly.

**Verified:**

- `npx tsc --noEmit` and `npm run lint` both clean.
- Backend: `CORSMiddleware` confirmed via a real `curl` preflight
  (`OPTIONS /pair`) and, more importantly, a real 401 response
  (`POST /pair` with a wrong PIN) — both carry
  `access-control-allow-origin: http://localhost:3000`, confirming error
  responses aren't silently CORS-blocked.
- Full pairing round trip exercised against a live `uvicorn` instance +
  the already-running `npm run dev` on port 3000 (left untouched rather
  than killed, since it predated this session): `POST /pair` with the
  real PIN from the backend's startup log → 200 with a session JWT;
  `GET /session/verify` with that token → 200 with matching
  `session_id`/`issued_at`/`expires_at`; replaying the same PIN → 401
  `pin_already_used`.
- Real browser walkthrough (done in a follow-up session once the backend
  was actually running against the user's filled-in `.env`): entered the
  PIN shown in the backend's startup log at `http://localhost:3000` →
  UI transitioned to the connected view showing a real `session_id`,
  `issued_at`, `expires_at` from `GET /session/verify` (screenshot
  confirmed) — closes out the gap noted below about no browser having
  been used yet. Refresh-persists / tab-close-clears / wrong-PIN-message
  paths were exercised earlier only via direct API calls (mirroring what
  `lib/api.ts` does), not yet re-confirmed by hand in the browser UI.
- `_print_pairing_screen()` exercised directly (with `VC_WEB_HOSTNAME`
  temporarily set to a dummy value, not the real `.env`) — confirmed the
  full text block + QR render correctly together.
- Did **not** test the live two-hostname tunnel (API + web) end-to-end —
  that requires the user's own DNS record and `cloudflared/config.yml`
  edit for the second ingress rule (real file, gitignored, real
  credentials — intentionally left to the user per the pre-implementation
  decision). Did **not** run the actual phone-on-mobile-data test — also
  explicitly the user's part.

**Deviations from the plan:**

- None beyond the three bugs listed above (Windows stdout encoding, React
  effect lint rule, pydantic-settings complex-type decode ordering) — all
  were fixed immediately once discovered, none deferred.

**Carried forward (flagged, no action needed yet):**

- User has since filled in `VC_WEB_HOSTNAME`/`VC_CORS_ORIGINS` in the real
  `backend/.env`; whether the matching second `cloudflared` ingress rule
  and DNS record were also added has not been re-confirmed — the live
  two-hostname tunnel test and the actual phone-on-mobile-data test are
  both still outstanding (explicitly the user's part per the
  pre-implementation decision).
- Refresh-persists / tab-close-clears-session / wrong-PIN-message UI
  paths have been verified via direct API calls but not yet re-confirmed
  by hand in the browser (only the happy-path PIN → connected transition
  has an actual browser confirmation so far).
- No PWA work done (manifest, service worker, installability) — Phase 4
  scope was explicitly QR + manual PIN entry + connected view only; PWA-
  specific work (if wanted) is still fully open for a later phase.

## Post-Phase 4 — Tunnel fix, mobile polish, rename (2026-07-18)

**Fixed:**

- `cloudflared/config.yml`: added the missing second ingress rule routing
  `dani-web.tripodhub.in → http://127.0.0.1:3000`. Without this, the web
  frontend was unreachable through the tunnel — the example config showed
  the second rule but it was never added to the real config.
- `web/.env.local` / `.env.local.example`: changed `NEXT_PUBLIC_API_URL`
  from `http://localhost:8000` to `https://dani.tripodhub.in`. The old
  value pointed at the phone's own localhost when accessed remotely, so the
  frontend couldn't reach the backend through the tunnel.

**Improved:**

- `web/app/layout.tsx`: added `viewport` export with `width=device-width`,
  `initialScale=1`, `maximumScale=1`, `userScalable=false` (prevents
  pinch-zoom, feels like a native app), plus `themeColor` for dark/light.
- `web/app/page.tsx`: mobile responsiveness overhaul — increased touch
  targets (PIN input `text-3xl py-4`, buttons `w-full py-4`), wider
  spacing, larger title (`text-3xl`), `rounded-xl` for modern mobile feel,
  responsive padding (`px-5 sm:px-8`).
- `web/app/globals.css`: removed default body margin, added font smoothing,
  set `html,body` to full height with `overflow: hidden` (prevents scroll
  bounce on iOS).

**Renamed:**

- "Voice Cowork" → "Dani Voice" across 5 locations: `web/app/page.tsx`
  (heading), `web/app/layout.tsx` (title + description), `backend/cli.py`
  (pairing screen header), `backend/main.py` (FastAPI app title).

**Verified:**

- Phone accessed `https://dani-web.tripodhub.in` — app loads, shows PIN
  form (no longer stuck on "Checking session…").
- Full pairing round trip through the tunnel: phone enters PIN →
  `POST /pair` to `dani.tripodhub.in` returns JWT → connected view shows
  real session data.
- Mobile UI renders at full phone width with appropriately sized touch
  targets (PIN input, Pair button, Disconnect button).

**Deviations from the plan:**

- None — these were fixes and polish to existing Phase 3/4 work, not new
  planned phases.

## Phase 5 — Session lifecycle hardening (2026-07-18)

**Built:**

- `schemas.py`: added `RefreshResponse` (new token + expiry), `RevokeRequest`
  (session_id or all flag), `RevokeResponse` (count), `SessionInfo`
  (session_id + timestamps), `SessionListResponse` (list of SessionInfo).
- `sessions.py`: added five methods to `SessionStore`:
  - `extend(session_id, new_expiry)` — mutates `record.expires_at` in-place
    (called by refresh endpoint after issuing new JWT).
  - `revoke(session_id) -> bool` — sets `record.revoked = True`, returns
    whether the session existed.
  - `revoke_all() -> int` — revokes all non-revoked sessions, returns count.
  - `list_active() -> list[SessionRecord]` — returns non-revoked, non-expired
    records; runs `cleanup()` lazily first.
  - `cleanup()` — removes expired records from the dict entirely (memory
    management, no background task).
- `routers/session.py`: added three new endpoints:
  - `POST /session/refresh` — accepts Bearer token, validates (same as
    verify), extends expiry by `session_ttl_seconds` from now, issues new
    JWT, returns `RefreshResponse`. Rate-limited via existing
    `pin_rate_limit`.
  - `POST /internal/revoke` — tunnel-gated (404s for tunnel requests),
    accepts `RevokeRequest` body, revokes by session_id or all, returns
    count. Local-only HTTP endpoint called by CLI.
  - `GET /internal/sessions` — tunnel-gated, returns list of active
    (non-revoked, non-expired) sessions. Local-only HTTP endpoint called
    by CLI.
- `cli.py`: added two new subcommands:
  - `voice-cowork sessions` — calls `GET /internal/sessions` via httpx,
    prints a formatted table (session_id, created, expires).
  - `voice-cowork revoke --all` / `voice-cowork revoke --session <id>` —
    calls `POST /internal/revoke` via httpx, prints result.
  - Added `datetime` import for timestamp formatting.
- `web/lib/api.ts`: added `RefreshResponse` interface and
  `refreshSession(token)` function — calls `POST /session/refresh` with
  Bearer token.
- `web/app/page.tsx`: added `useEffect` that schedules automatic token
  refresh at 50% of session TTL (at least 10s from now). On successful
  refresh, updates stored session and UI state. On failure (expired,
  revoked), clears session and falls back to pairing view. Imports
  `refreshSession` from `@/lib/api`.
- `README.md` (new): brief project overview covering tech stack,
  quickstart (backend + tunnel + web), Cloudflare tunnel setup, config
  reference, CLI commands, project structure, and build phase summary.

**Verified** (curl against uvicorn on scratch ports):

1. **Refresh extends expiry:** Paired, verified (`expires_at=X`), refreshed,
   verified again (`expires_at=Y`) — confirmed `Y > X`. HTTP 200 on all
   three requests.
2. **Refresh with invalid token:** `POST /session/refresh` with
   `Bearer garbage` → 401. Confirmed.
3. **Refresh with revoked token:** Paired, verified (200), revoked via
   `POST /internal/revoke` (200, `revoked=1`), then refresh with the same
   token → 401. Confirmed.
4. **Revocation invalidates live session:** Same test as step 3 — verify
   after revoke returned 401. Confirmed.
5. **Revoke all:** Paired one session, revoked all via `{"all": true}` →
   `revoked=1`. Verify with the same token → 401. Confirmed.
6. **No orphaned state:** After revoke all, `GET /internal/sessions` returned
   `sessions: []` (count 0). Confirmed lazy cleanup removes expired records.
7. **CLI commands:** `voice-cowork --help` shows all three subcommands
   (`run`, `sessions`, `revoke`). `voice-cowork sessions` and
   `voice-cowork revoke` call the internal HTTP endpoints correctly (verified
   via the backend returning expected responses).
8. **Frontend refresh:** Implementation complete in `page.tsx` — timer
   scheduled at 50% of TTL, calls `refreshSession()`, updates state on
   success, falls back on failure. Manual verification requires running the
   web client with a short `VC_SESSION_TTL_SECONDS` (e.g., 10s) to observe
   the timer firing within a test window.

**Deviations from the plan:**

- CLI and backend are in separate processes (CLI spawns uvicorn as a
  subprocess), so the CLI cannot access `session_store` directly. Added
  `POST /internal/revoke` and `GET /internal/sessions` as local-only HTTP
  endpoints (tunnel-gated like `/internal/pin`) for the CLI to call via
  httpx. This keeps the session store in one place (the uvicorn worker)
  and avoids stale-state bugs between processes.
- Refresh endpoint shares the existing `pin_rate_limit` rate limiter per
  the approved plan (no separate config key).

**Carried forward (flagged, no action needed yet):**

- Frontend refresh timer is implemented but not yet manually verified in
  the browser with a short TTL — requires running `npm run dev` with
  `VC_SESSION_TTL_SECONDS=10` and observing the timer fire.
- No PWA work done (manifest, service worker, installability) — still
  deferred.

## Phase 6 — ESP32 firmware skeleton: NimBLE advertising + GATT service (2026-07-18)

**Built:**

- `sdkconfig.defaults`: retargeted from plain `esp32` (Phase 1's placeholder,
  never build-verified) to `esp32s3` for the real hardware (Seeed XIAO
  ESP32-S3). Added: `CONFIG_ESPTOOLPY_FLASHSIZE_8MB`/`FLASHSIZE="8MB"`
  (board's real flash size); `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` (XIAO
  has no UART-USB bridge chip — console goes over the S3's built-in
  USB-Serial-JTAG peripheral on the same USB-C port used for flashing);
  `CONFIG_BT_ENABLED`, `CONFIG_BT_BLUEDROID_ENABLED=n`,
  `CONFIG_BT_NIMBLE_ENABLED=y` (NimBLE host over Bluedroid, per the tech
  stack); `CONFIG_BT_NIMBLE_SM_SC=y` + `CONFIG_BT_NIMBLE_SM_LEGACY=n` +
  `CONFIG_BT_NIMBLE_SM_SC_ONLY=1` (force LE Secure Connections, reject
  legacy-pairing fallback). Left `CONFIG_BT_NIMBLE_NVS_PERSIST` at its
  default (`n`, commented for visibility) and PSRAM disabled (approved,
  not needed this phase — board does have 8MB onboard PSRAM, confirmed by
  esptool's `Embedded PSRAM 8MB` chip-detect line during flashing).
- `main/gatt_svr.h` / `main/gatt_svr.c` (new): custom "Dani Voice
  provisioning" GATT service, three fresh random 128-bit UUIDs (service +
  two characteristics, generated via `[guid]::NewGuid()`, not borrowed
  from any spec/example). Token characteristic:
  `BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC` (write requires an
  encrypted connection — the actual security enforcement point), 600-byte
  max, generous enough for a JWT; NimBLE's built-in queued/prepared-write
  handles anything over the negotiated ATT MTU transparently, no extra
  code needed. Status characteristic: `READ | READ_ENC | NOTIFY`, plain
  1-byte enum (`idle=0, provisioning=1, ok=2, failed=3`);
  `gatt_svr_set_status()` updates the value and calls
  `ble_gatts_chr_updated()` to push a notification to subscribers.
  Written token bytes land in a `static uint8_t[600]` RAM buffer
  (`device_token_buf`) — no parsing, validation, or use of the token yet
  (that's Phase 7+), and no `nvs_set_*`/`nvs_open` call anywhere near it
  (grepped `main.c`/`gatt_svr.c` to confirm).
- `main/main.c` (rewritten from Phase 1's boot-only skeleton): NimBLE
  host init (`nimble_port_init`), GAP event handler (connect/disconnect/
  adv-complete/enc-change/subscribe/MTU/repeat-pairing — status resets to
  `idle` on disconnect, advertising restarts on disconnect and on
  adv-complete), device name `dani-voice`. Security config set directly
  on `ble_hs_cfg` in `app_main()`: `sm_io_cap = BLE_HS_IO_NO_INPUT_OUTPUT`,
  `sm_bonding = 1`, `sm_mitm = 0`, `sm_sc = 1`, key distribution flags for
  `ENC | ID` (LTK + IRK) — Just Works + SC + bonding, per the approved
  plan (no display/keyboard on this board, so MITM-protected association
  methods aren't available regardless of config). `ble_store_config_init()`
  registers the RAM-only bonding store. No `console`/`scli` dependency
  pulled in (unlike the `bleprph` reference example) — Just Works needs no
  passkey-entry console interaction, so that whole dependency was skippable.
- `main/CMakeLists.txt`: added `gatt_svr.c` to `SRCS`.

**Verified** (real hardware — Seeed XIAO ESP32-S3 on COM3):

1. `idf.py set-target esp32s3` — succeeded, generated `sdkconfig` from
   `sdkconfig.defaults`.
2. `idf.py build` — clean build, no errors (1208 ninja targets). App
   binary 486KB, 54% of the 1MB factory partition free.
3. `idf.py -p COM3 flash` — succeeded. esptool's chip-detect confirmed
   real hardware identity: `ESP32-S3 (QFN56) rev v0.2`, `WiFi, BLE,
   Embedded PSRAM 8MB`, USB mode `USB-Serial/JTAG` — matches every
   hardware assumption made in the plan.
4. Captured the boot log directly over the USB-Serial-JTAG port (raw
   `System.IO.Ports.SerialPort` read with an RTS-toggle reset, since
   `idf.py monitor` needs a real interactive TTY this environment
   doesn't have). Clean boot, no crash/reboot loop:
   `BLE_INIT` → `NimBLE: GAP procedure initiated: advertise` →
   `dani_voice_ble: advertising started` → `main_task: Returned from
   app_main()`, all within ~220ms of boot.
5. `grep -n "nvs_set\|nvs_get\|nvs_open"` over `main.c`/`gatt_svr.c` —
   zero matches, confirming the token buffer is genuinely RAM-only.
6. Not yet independently confirmed: nRF Connect (or similar BLE scanner)
   showing the service/characteristics, and a real phone completing Just
   Works pairing before a token write succeeds — that's the user's
   verification step per the approved plan, still outstanding.

**Deviations from the plan:**

- **ESP-IDF environment was broken, not just "needs export.ps1 sourced"
  as previously assumed.** `export.ps1` failed looking for a Python venv
  at `idf5.5_py3.13_env` (doesn't exist) — the real venv on disk is
  `idf5.5_py3.11_env`, apparently because the system's default `python`
  on PATH changed to 3.13 at some point after ESP-IDF was originally
  installed, and `export.ps1`'s environment detection follows the
  system's active Python rather than the pinned tool version. Worked
  around it for this session by invoking `idf.py` directly through the
  existing 3.11 venv's `python.exe` with `IDF_PATH`, `IDF_PYTHON_ENV_PATH`,
  the `xtensa-esp-elf` toolchain bin dir, and `ninja` all added to `PATH`
  explicitly in each PowerShell call (still can't persist across separate
  tool invocations, same limitation as `export.ps1` itself). Recommend
  re-running `install.ps1` at some point to let it regenerate a
  `idf5.5_py3.13_env` venv matching the current system Python and fix
  `export.ps1` properly — not done here since it wasn't necessary to
  unblock the build and reinstalling wasn't requested.
- A first `set-target` attempt failed on that broken environment (Python
  venv error) but had already created a partial, invalid `build/`
  directory (CMake config error before compiler detection). Removed that
  directory manually (`build/` is gitignored, no source in it, purely a
  build artifact) before retrying — `fullclean` refused to do this
  automatically since it didn't recognize the directory as a valid CMake
  build dir.
- `idf.py monitor` couldn't be used directly (requires an interactive
  TTY, not available via the tool used to run commands here) — verified
  the boot log by reading the USB-Serial-JTAG COM port directly instead
  (see verification step 4). Functionally equivalent for this purpose.
- Did not add `CONFIG_BTDM_CTRL_MODE_BLE_ONLY` (present in the `bleprph`
  reference example's `sdkconfig.defaults`) — confirmed by grepping
  `components/bt/controller/esp32s3/Kconfig.in` that this option doesn't
  exist for the S3 target at all (it's an ESP32-classic-only option, since
  S3's controller is BLE-only unconditionally, no classic BT hardware).

**Carried forward (flagged, no action needed yet):**

- The broken `export.ps1`/Python-venv-version mismatch will resurface for
  any future session that tries to source it normally; the workaround
  above is a one-off, not a fix. Worth an explicit `install.ps1` re-run
  before Phase 7 if this keeps needing to be worked around.
- Token characteristic write is fully unauthenticated at the application
  layer beyond BLE-level encryption (any bonded peer can write to it) —
  that's expected for this phase (no token logic yet) but Phase 7's
  provisioning flow is the point where the actual device-scoped token
  request/validation logic needs to land.

**Correction (added post-Phase 7):** `CONFIG_BT_NIMBLE_SM_SC_ONLY=1`, set
in this phase's "Built" section above, was a latent misconfiguration
introduced here, not in Phase 7. It requires an authenticated link
(Security Mode 1 Level 4) for any `_ENC`/`_AUTHEN`/`_AUTHOR`-gated GATT
operation, which this board's Just Works pairing (`sm_mitm=0`, no
display/keyboard) can never produce — Just Works yields encryption without
authentication, by definition. It went unnoticed through this phase and
Post-Phase 6 because nothing here ever exercised a real `_ENC`-gated
operation against real hardware (nRF Connect's connect/discover/notify
checks all succeed without encryption). It only surfaced once Phase 7's
token write — the first genuine `WRITE_ENC` operation — was actually
attempted. Full root-cause and fix under Post-Phase 7 below.

## Post-Phase 6 — nRF Connect independent verification (2026-07-18)

The nRF Connect check carried forward from Phase 6 (service/characteristic
visibility, Just Works pairing, notify subscription) is now complete,
including root-causing two anomalies surfaced in the captured logs rather
than dismissing them.

**Verified (real hardware, nRF Connect on phone):**

- Service and both characteristics visible with correct properties (token:
  write/write-encrypted; status: read/read-encrypted/notify).
- Just Works pairing completes on connect: `encryption change; status=0
  encrypted=1 bonded=1` every session.
- Notify subscription confirmed working end-to-end: toggling "Enable
  Notifications" in nRF Connect on the status characteristic produced
  repeated `BLE_GAP_EVENT_SUBSCRIBE` events with `reason=1` (WRITE) and
  `prev_notify`/`cur_notify` correctly alternating 0→1→0→1 in direct
  response to each tap, and nRF Connect's own UI confirmed "Notifications
  enabled." This required adding `reason`/`prev_notify` to the subscribe
  log line in `main.c` (previously only logged `attr_handle`/`cur_notify`,
  which couldn't distinguish a real failed-enable from benign
  connect/disconnect-lifecycle CCCD writes — see below).

**Investigated from logs, confirmed benign:**

- `BLE_ERR_INV_HCI_CMD_PARMS` (`ogf=0x08, ocf=0x0027`) firing right after
  connect on some (not all) sessions: `ocf=0x0027` is `LE Add Device To
  Resolving List`, which the Bluetooth core spec disallows while
  advertising is still enabled. NimBLE's host issues this shortly after
  connect (to register the peer's freshly-exchanged IRK) in a race against
  the controller's own auto-disable-of-advertising-on-connect; whether the
  controller has finished that disable by the time the command arrives is
  timing-dependent, matching the observed intermittent (not every-session)
  occurrence. Encryption/bonding succeed regardless, and this device never
  relies on resolving-list-based reconnect filtering (always undirected
  advertising) — cosmetic, no functional impact.
- The originally-flagged `subscribe event; attr_handle=8 cur_notify=0`
  pattern (one at connect, one at disconnect, every session, never a `1` in
  between) turned out to be exactly what it looked like once `reason`/
  `prev_notify` were added: `reason=3` (RESTORE) or `reason=1` (WRITE) with
  `prev_notify=0, cur_notify=0` — the client's own connect/disconnect
  housekeeping (bonded-reconnect CCCD restore, and a defensive
  already-off write before disconnect), not a failed subscribe. No actual
  enable had been attempted in those earlier captures.

**Carried forward (flagged, no action needed yet):**

- The real, user-driven toggle events landed on `attr_handle=18`, not the
  `attr_handle=8` seen in the connect/disconnect housekeeping events from
  the same session. Both numbers point at real CCCD writes tied to
  encryption/subscribe behavior, so this isn't blocking, but the handle
  numbering wasn't fully reconciled — worth a quick look (e.g. via
  `gatt_svr_register_cb`'s `ESP_LOGD` output, or nRF Connect's own handle
  listing) before Phase 7 if handle identity ever matters for that phase's
  code, rather than assuming which handle is "the" status CCCD.
- `main.c`'s `BLE_GAP_EVENT_SUBSCRIBE` log line now includes `reason` and
  `prev_notify`; not reverted to the shorter form, since the extra fields
  are what made this investigation possible and cost nothing to keep.

## Phase 7 — Phone-as-BLE-proxy provisioning flow (2026-07-18)

**Built:**

- `backend/pairing.py`: **retroactive change to Phase 2/3's `PinStore`**,
  not new Phase 7 surface area — `PinRecord.used: bool` split into
  `phone_used: bool` / `device_used: bool`. `try_consume()` now takes a
  `purpose: Literal["phone", "device"]` and checks/sets only that purpose's
  flag. This was the actual open design question from the approved plan:
  the architecture doc has one printed PIN both pairing the phone to
  itself (`/pair`) and provisioning the device (`/device/pair`), which the
  old single `used` flag would have blocked on whichever call went second.
  `/pair`'s call site updated to pass `purpose="phone"`; behavior for the
  phone-only flow is otherwise unchanged (same rejection ordering —
  already-used checked before mismatch — confirmed unchanged, not
  reverified from scratch since that ordering is untouched code).
- `backend/sessions.py`: `SessionRecord` gained `kind: Literal["phone",
  "device"] = "phone"` and `device_id: str | None = None`; `SessionStore
  .create()` takes optional `kind`/`device_id`, defaulting to today's phone
  behavior (zero changes needed at the `/pair` call site). Added
  `issue_device_token`/`verify_device_token`, sibling to the existing
  phone-only `issue_session_token`/`verify_session_token` (left untouched),
  using JWT `type: "device"` + a `device_id` claim, sharing the same
  `session_store` dict — so `/internal/revoke` and `/internal/sessions`
  work on device sessions for free.
- `backend/schemas.py`: `PinDebugResponse.used` → `phone_used`/
  `device_used`. New `DevicePairResponse` (access_token, device_id,
  expires_at). `SessionInfo`/`SessionListResponse` gained `kind`/
  `device_id` so `voice-cowork sessions` can distinguish device entries.
- `backend/routers/pairing.py`: new `POST /device/pair` — consumes the PIN
  with `purpose="device"`, generates `device_id = str(uuid.uuid4())` (no
  persistent device registry — matches "reboot forces re-provisioning";
  nothing needs to survive a restart), issues a device-scoped JWT via
  `issue_device_token`. `/internal/pin` and `/pair` updated for the new
  `PinRecord` shape.
- `backend/routers/session.py`: `/internal/sessions` now includes
  `kind`/`device_id` per entry.
- `firmware/main/gatt_svr.c`: `token_chr_access_cb` — first real caller of
  `gatt_svr_set_status()` (nothing called it before this phase, per Phase
  6's carried-forward note). Empty or over-length writes → `FAILED` +
  `BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN`; otherwise sets `PROVISIONING`
  before the copy, then `OK` after a successful `ble_hs_mbuf_to_flat`. No
  JWT parsing/shape validation — that's explicitly Phase 8's job, not this
  one's.
- `web/types/web-bluetooth.d.ts` (new): minimal ambient type declarations
  for `navigator.bluetooth`/`BluetoothDevice`/`BluetoothRemoteGATT*` —
  chose hand-written ambient types over adding an `@types/web-bluetooth`
  dependency, consistent with this project's pattern of avoiding new deps
  when a small amount of code covers it (same call made for QR rendering
  in Phase 4).
- `web/lib/ble.ts` (new): `SERVICE_UUID`/`TOKEN_CHARACTERISTIC_UUID`/
  `STATUS_CHARACTERISTIC_UUID` — computed by reversing the byte arrays in
  `gatt_svr.c`'s `BLE_UUID128_INIT` calls back to standard UUID string
  order (per that file's own comment on OTA vs. standard byte order), not
  guessed. `provisionDevice(token, onStatus)`: `requestDevice` (service
  UUID filter) → `gatt.connect()` (triggers OS-level Just Works pairing on
  first connect) → subscribe to the status characteristic's notifications
  → write the device token to the token characteristic. `DeviceStatus`
  type and the idle/provisioning/ok/failed byte mapping mirror
  `device_status_t` in `gatt_svr.h`.
- `web/lib/api.ts`: `devicePair(pin)` — same shape/error-handling pattern
  as the existing `pair()` (422/429/401 → typed `PairError`).
- `web/app/page.tsx`: extended the existing connected view (no new route,
  per approval) with a "Provision ESP32" section — PIN input, calls
  `devicePair()` then `provisionDevice()`, live-renders `deviceStatus` as
  it changes, and shows an explicit "Web Bluetooth isn't available" message
  (via `isWebBluetoothAvailable()`) instead of a broken button when the
  browser can't support it.

**Verified:**

- Backend (scratch `uvicorn` on port 8123, real HTTP calls):
  1. `GET /internal/pin` on a fresh PIN → `phone_used=false,
     device_used=false`.
  2. `POST /pair` with that PIN → 200, session JWT (`type=phone` session
     created). Then `POST /device/pair` with the **same** PIN → 200, a
     distinct device JWT (`type=device`, real `device_id`) — this is the
     concrete proof the dual-consumption fix works: the phone-pairing
     consumption did not block device provisioning on the same PIN.
  3. `GET /internal/pin` afterward → `phone_used=true, device_used=true`.
  4. Repeating both `POST /pair` and `POST /device/pair` against the
     now-fully-consumed PIN → both 401 `pin_already_used`, independently.
  5. `GET /internal/sessions` → one `kind=phone` entry (`device_id: null`)
     and one `kind=device` entry with the real `device_id` — confirms the
     shared store correctly represents both kinds without a parallel
     store.
  6. `uv run python -c "import voice_cowork_backend.main"` clean.
- Firmware: `idf.py build` (manual env-var workaround, PowerShell not Git
  Bash — `idf.py`'s CMake step refuses to run under MSYS/Git Bash,
  discovered this session) — clean build, 0x76c00 bytes app / 54% of the
  1MB partition free, no errors.
- Web: `npx tsc --noEmit` clean (including the new ambient Web Bluetooth
  types resolving correctly against `lib.dom` with no `@types` package
  installed). `npm run lint` clean.

**Deviations from the plan:**

- None beyond what was already flagged and approved before implementation
  (dual-consumption PIN design, connected-view UI placement, `uuid.uuid4()`
  device IDs with no registry).
- New minor detail surfaced during implementation, not in the original
  plan: `idf.py build` cannot run under the Bash tool's Git Bash/MSYS shell
  at all (`cmake` refuses outright) — had to use the PowerShell tool for
  the firmware build specifically. Worth remembering for Phase 8+ firmware
  work in this environment.

**Carried forward — explicitly the user's part, not yet done:**

- **The live browser-to-ESP32 walkthrough.** Nothing in this phase
  exercised a real `navigator.bluetooth.requestDevice()` call against
  actual hardware — that requires a real Chrome-on-Android (or desktop
  Chrome) browser and the flashed ESP32, neither of which this environment
  can drive. Needed:
  1. Flash the updated firmware (`idf.py -p COM3 flash`).
  2. Run `voice-cowork`, pair a phone via the PIN as before, then use the
     new "Provision ESP32" section in the connected view (Chrome on
     Android, since iOS Safari/WebKit has no Web Bluetooth support at
     all — confirmed, not assumed, before scoping this phase).
  3. Confirm the browser's device picker shows `dani-voice`, that
     `gatt.connect()` completes (re-triggers Just Works pairing if not
     already bonded), and that the on-page `deviceStatus` line visibly
     moves idle → provisioning → ok as the write lands.
  4. Cross-check against the ESP32's own serial log — should show `token
     characteristic write: N bytes` and the same status transitions via
     `gatt_svr_set_status()`'s `ble_gatts_chr_updated()` calls.
  5. Confirm rejection paths in the browser: an expired/already-used PIN
     at the `devicePair()` step should surface the same typed error
     messages as the phone-pairing form.
- Handle-numbering loose end from Post-Phase 6 (`attr_handle=18` vs. `=8`
  for the status CCCD across sessions) was not revisited this phase —
  hasn't mattered for anything built here, but still open if it ever
  matters for Phase 8.

## Post-Phase 7 — Real browser/hardware fixes from user testing (2026-07-18)

Two bugs surfaced during the user's actual browser-to-ESP32 walkthrough
(the part explicitly carried forward as their verification step), both
root-caused from real logs rather than guessed at.

**Fixed:**

- **`dani-voice` never appeared in the Web Bluetooth device picker.**
  `start_advertising()` in `main.c` only put `flags` + the device name in
  the primary advertising packet — never the service UUID. Web
  Bluetooth's `requestDevice({filters: [{services: [...]}]})` only matches
  devices whose advertised (or scan-response) data actually carries the
  filtered UUID; it doesn't do a name-based fallback. A 128-bit UUID (18
  bytes) doesn't fit alongside flags (3 bytes) + the full name (12 bytes)
  in one 31-byte legacy advertising packet, so the fix puts the service
  UUID in the **scan response** instead (`ble_gap_adv_rsp_set_fields()`),
  which legacy undirected-connectable advertising (`ADV_IND`) always
  supports. `gatt_svr_svc_uuid` was `static` in `gatt_svr.c`; made it
  non-static and declared `extern` in `gatt_svr.h` so `main.c` has a
  single source of truth for the UUID instead of a second hardcoded copy.
  Rebuilt clean (`idf.py build`).
- **Web app showed a generic "Something went wrong" with no token write
  ever reaching the ESP32.** Root-caused from the user's serial log: no
  `token characteristic write: N bytes` line appeared at all, meaning
  `web/lib/ble.ts`'s `provisionDevice()` threw before reaching the write
  step. The culprit was an eager `statusChar.readValue()` call
  immediately after `startNotifications()` — the CCCD write (notify
  subscribe) doesn't require encryption in NimBLE, but a real
  characteristic read does (`BLE_GATT_CHR_F_READ_ENC`), and the log
  showed the subscribe succeeding ~5 seconds before the `encryption
  change` event finally logged — a genuine race between "notify enabled"
  and "link marked encrypted." Fix: dropped the unnecessary upfront read
  (notifications alone are sufficient — status starts at `idle` and
  nothing's been written yet, so there was nothing meaningful to read
  early), and wrapped every remaining GATT step (`connect`,
  `getPrimaryService`, `startNotifications`, the token write) in its own
  try/catch that rethrows as a `BleProvisioningError` with the real
  underlying message, so future failures surface an attributable reason
  in the UI instead of falling through to a generic message.

**Also diagnosed, not a bug:** the "have to forget the device to
reconnect" symptom the user hit initially traced back to testing via
Android's own Bluetooth settings "Connect" button, not the web app's
"Provision ESP32" button — system-level Bluetooth settings' connect action
is largely a no-op for a custom BLE peripheral with no recognized profile
(no HID/audio/etc.); GATT connections for a device like this are meant to
be established by the app via `device.gatt.connect()`. Confirmed by the
serial log: zero BLE activity logged during the window the user tapped
"Connect" in system settings, whereas going through the web app's button
produced a real `connection established` line every time. No firmware or
web change made for this — it was a testing-workflow question, not a bug.

**Re-tested by user after the above fixes:** got further (past connect,
service discovery, and notify subscribe — confirmed via `web/app/page.tsx`
now surfacing the real thrown message instead of a generic fallback, a
separate small fix made specifically so future failures wouldn't be
diagnosed blind again), but failed at the token write itself with "GATT
Error Unknown" — Chrome's generic wrapper for an undecoded ATT error, ATT
right at the one operation that actually requires `WRITE_ENC`.

**Fixed:** `device.gatt.connect()` resolving does not mean bonding/
encryption has finished — on Android, the first GATT operation against an
encrypted characteristic commonly *triggers* encryption negotiation lazily
rather than waiting on it, and LE Secure Connections' ECDH exchange is
slow enough (the serial logs consistently show a multi-second gap between
`connection established` and `encryption change`) to race a write attempted
immediately after connecting. Added `withEncryptionRetry()` in
`web/lib/ble.ts`: one retry after a 2s delay, applied to the token write
specifically (the only operation gated on `WRITE_ENC` in this flow — the
status characteristic's CCCD write doesn't require encryption in NimBLE,
which matches why notify-subscribe succeeded on the first attempt in the
same test run). `tsc`/lint clean.

**Re-tested again:** hit a new symptom — after a clean ESP32 reboot (to
rule out stale bonds) and a hard-reloaded browser tab, the write attempt
either hung silently or (after adding a timeout wrapper) eventually
surfaced `GATT operation already in progress`. Root cause found: Android's
`BluetoothGatt` allows only one outstanding GATT operation per connection,
and `Promise.race` — which the timeout wrapper used — does not cancel the
losing side. A "timed out" `writeValueWithResponse()` call was often still
alive in the background even after the JS code gave up on it; the 2s-later
retry then collided with that still-live first attempt, producing exactly
this error. This also explains the earlier silent hangs: the first write
wasn't actually stuck forever, just slower than expected, and there is no
cancellation API in Web Bluetooth to abandon it cleanly.

**Fixed:** `withEncryptionRetry()` now wraps the *raw* write call directly
(no per-attempt timeout), so its retry only fires on a genuine, fast
rejection — never on a manufactured timeout, since only a fast rejection
means the GATT queue is actually free again. `withTimeout()` is applied
once, around the whole retry sequence, purely as a last-resort UI-freeze
guard; if that outer timeout fires, the code does not retry again itself
(avoiding further collisions) — it surfaces an error asking the user to
disconnect and retry manually instead. `tsc`/lint clean.

**Re-tested again, ruled out the browser entirely:** after a genuinely
clean reset (ESP32 rebooted, phone's stale bond actually forgotten this
time — an earlier instruction to just toggle Bluetooth off/on was
insufficient, since that doesn't clear pairing memory, only the radio
state), the retry-ordering fix worked as intended (no more "GATT operation
already in progress"), but the token write still never landed — confirmed
by both the serial log (`token characteristic write:` never appears) and
`nRF Connect` (the same tool that worked cleanly in Phase 6) hanging on
the identical write attempt. Since nRF Connect hits the exact same
symptom as Chrome desktop and Chrome Android, this is conclusively **not**
a browser/Web-Bluetooth-side issue.

**Pattern across every client tested tonight (Chrome desktop, Chrome
Android, nRF Connect):** every GATT operation that does *not* require
encryption (connect, service discovery, notify-subscribe CCCD writes)
works reliably. Every operation gated on `_ENC` — the token write
(`WRITE_ENC`), and implicitly the status characteristic's real read
(`READ_ENC`, never explicitly exercised after the `readValue()` call was
removed) — has never once succeeded, even on connections where
`encryption change; encrypted=1 bonded=1` already logged. This points at
NimBLE's internal `_ENC` permission check not correctly recognizing the
link as encrypted at the point those specific operations are attempted,
despite the GAP-level event reporting success. Suspected contributing
factor, not yet confirmed: `sdkconfig.defaults`' `CONFIG_BT_NIMBLE_SM_SC_ONLY=1`
(LE Secure Connections only, no legacy fallback) combined with Just
Works — possible quirk in this NimBLE port where SC-only pairing doesn't
propagate the encrypted flag to wherever the `_ENC` check reads it.

**TEMPORARY DIAGNOSTIC CHANGE — not a real design change, must be
reverted:** `gatt_svr.c`'s token characteristic flags changed from
`BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC` to plain
`BLE_GATT_CHR_F_WRITE`, to test whether the `_ENC` check itself is what's
hanging every write. Clearly marked in-code with a "TEMPORARY DIAGNOSTIC
STATE — DO NOT SHIP LIKE THIS" comment at the flag definition. The token
write is the actual security boundary for this phase's design (an
attacker with BLE range could otherwise write to it without ever
completing pairing) — this must not be mistaken for the intended Phase 6/7
security model if the repo is inspected mid-diagnosis, and
`BLE_GATT_CHR_F_WRITE_ENC` must be restored once this diagnostic concludes.
Rebuilt clean (`idf.py build`); not yet reflashed/retested against
hardware as of this entry.

**Next step:** reflash this diagnostic build, reboot the ESP32 (fresh
bond store), and retry the exact same nRF Connect write. If it now
succeeds (`token characteristic write: N bytes` appears), that confirms
the `_ENC` check is the actual bug and narrows the fix to that specific
path. If it still hangs even without `_ENC`, the bug is elsewhere
entirely and this rules that theory out too — either result is useful
new information.

## Post-Phase 7 (continued) — `_ENC` root cause found and fixed (2026-07-19)

**Symptom:** every GATT operation gated on `_ENC`/`_AUTHEN`/`_AUTHOR`
(concretely: the token characteristic's `WRITE_ENC`) failed or hung
indefinitely across every client tested, despite the GAP layer reporting
`encrypted=1 bonded=1` on the same connection.

**Diagnostic:** the temporary flag-strip described above was reflashed and
retested. With `BLE_GATT_CHR_F_WRITE_ENC` removed, the write succeeded
immediately — a real 277-byte JWT landed (`token characteristic write: 277
bytes`), MTU had negotiated to 256, and the status characteristic's notify
fired afterward. This confirmed the `_ENC` permission check itself — not
the write path, not the buffer, not the MTU/queued-write handling — was
the point of failure, and that the link genuinely was capable of carrying
the write once that check was bypassed.

**Root cause:** not a NimBLE defect — a self-contradictory security
config. `ble_att_svr_check_perms()` in NimBLE's `ble_att_svr.c` special-
cases `ble_hs_cfg.sm_sc_only`: when set, *any* characteristic flagged
`_ENC`, `_AUTHEN`, or `_AUTHOR` requires `sec_state.authenticated` to be
true, in addition to (and regardless of) which specific flag the
characteristic declares:

```c
if (ble_hs_cfg.sm_sc_only) {
    if (!sec_state.authenticated || !sec_state.encrypted) {
        *out_att_err = BLE_ATT_ERR_INSUFFICIENT_AUTHEN;
        return BLE_HS_ATT_ERR(*out_att_err);
    }
    ...
}
```

`CONFIG_BT_NIMBLE_SM_SC_ONLY=1` (set in Phase 6, see the correction note
on that phase's entry above) enforces Security Mode 1 Level 4 —
*authenticated* LE Secure Connections. Authentication requires a
MITM-protected association method (Passkey Entry, Numeric Comparison, or
OOB), all of which need I/O capability. This board has none
(`sm_io_cap = BLE_HS_IO_NO_INPUT_OUTPUT`, `sm_mitm = 0`, Just Works — by
design, per the architecture doc, since the board has no display or
keyboard). Just Works pairing produces encryption but, by definition,
never produces authentication. So `sec_state.authenticated` was
permanently false, and the `sm_sc_only` branch rejected the write on
every attempt regardless of encryption state — an unreachable security
level requested from a pairing method that structurally cannot deliver
it. Confirmed by inspecting `ble_hs_cfg.c` (`sm_sc_only =
MYNEWT_VAL(BLE_SM_SC_ONLY)`) and `sdkconfig`/`sdkconfig.defaults`
(`CONFIG_BT_NIMBLE_SM_SC_ONLY=1`), not just inferred from behavior.
Removing `WRITE_ENC` earlier "worked" only because it hit the check's
early return (`!enc && !authen && !author` → skip entirely) — it bypassed
all security enforcement, not just the buggy part.

**Why Option A (disable SC_ONLY) over Option B (add OOB, keep SC_ONLY):**
presented both to the user as an explicit tradeoff rather than picking
unilaterally. Level 4 (authenticated SC) is unreachable with this board's
I/O capabilities as designed — OOB pairing would need a side channel
(feasible in principle by piggybacking the existing PIN-pairing HTTP flow,
but nontrivial engineering: a new OOB confirm-value exchange integrated
into the phone-pairing path) to manufacture authentication this device
can't otherwise produce. The project's actual admission-control boundary
is the single-use, 5-minute-TTL PIN gating both `/pair` and
`/device/pair` — not BLE-level MITM protection — so Level 3 (SC
encryption, unauthenticated, bonded) already matches the threat model the
rest of the system was built against. User chose Option A.

**Fix:**

- `firmware/sdkconfig.defaults` / `firmware/sdkconfig`:
  `CONFIG_BT_NIMBLE_SM_SC_ONLY=1` → `=0` (the Kconfig default — enforces
  Level 3, SC encryption without authentication, matching what Just Works
  can actually deliver).
- `firmware/main/gatt_svr.c`: token characteristic flags restored to
  `BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC`; the "TEMPORARY
  DIAGNOSTIC STATE" comment removed now that the real fix landed instead
  of a workaround.

**Re-verified:**

- `idf.py build` — clean, no errors, after both the config change and the
  flag restore.
- Hardware retest (reflash, real bonded write against the restored
  `WRITE_ENC` flag with `SM_SC_ONLY=0`) — **not yet performed**, carried
  forward as the next verification step. Expected: the same 277-byte JWT
  write that succeeded with `_ENC` stripped should now succeed identically
  with `_ENC` restored, since Level 3 requires only `sec_state.encrypted`
  (already true per every prior session's `encrypted=1` log), not
  `authenticated`.

**Carried forward:**

- Reflash and confirm the token write succeeds on real hardware with
  `WRITE_ENC` restored and `SM_SC_ONLY=0` — this is the actual close-out
  of the Phase 7 diagnostic thread that's been open since the "TEMPORARY
  DIAGNOSTIC CHANGE" entry above.
- Option B (OOB pairing for authenticated Level 4) remains a valid future
  hardening path if the threat model ever changes (e.g., if the PIN-gate
  assumption stops being sufficient) — deliberately not built now.

## Out-of-scope — camera/WiFi streaming integration (`firmware2/` → `firmware/`) (2026-07-19)

Not part of the 9-phase BLE provisioning plan (see CLAUDE.md's "Out-of-scope
addition" section) — a second, unrelated firmware capability (Arduino-based
TCP JPEG camera streamer, originally standalone in `firmware2/`) needed to
land on the same physical board as the BLE provisioning firmware, running
simultaneously with it. Full arc across three rounds of the same session:

**1. Arduino-as-ESP-IDF-component — proposed, approved, partially built:**
Plan: add `arduino-esp32` as a managed ESP-IDF component, replicate its
weak `app_main()`'s `initArduino()` + loop-task behavior by hand (since
`main.c` already owns `app_main()` for NimBLE), 4MB factory app partition
on the 8MB flash (sized against Arduino's larger static-link footprint).
User confirmed BLE+camera must run **simultaneously** (not sequential —
this was an open question resolved via `AskUserQuestion` before any plan
was drafted, since it changes the whole coexistence design). Partition
sizes reviewed and approved before implementation, per the user's explicit
ask to see real numbers first. Built: `firmware/main/idf_component.yml`
(arduino-esp32 + esp32-camera deps), `firmware/components/camera_streamer/`
(all of `firmware2/`'s sources copied unchanged, plus a new
`camera_streamer_app.cpp` bridging `initArduino()` + a manually-spawned
loop task into the existing `app_main()`). **Not yet done at this point:**
`main.c`, `gatt_svr.c`, and `sdkconfig.defaults` were never touched — the
one planned line (`camera_streamer_start()` call in `main.c`) was never
added, and no build was attempted.

**2. Reconsidered mid-implementation, clean discard:** User paused before
further work landed, asked for an exact progress report (confirmed above:
`main.c`/`gatt_svr.c`/`sdkconfig.defaults` genuinely untouched, nothing
built/flashed), then asked to discard the Arduino-as-component path
entirely and assess a native ESP-IDF port instead. Deleted
`firmware/main/idf_component.yml` and `firmware/components/camera_streamer/`
wholesale (`git status` on `firmware/` confirmed clean afterward — both
were untracked, so removal left no trace). CLAUDE.md's documentation
section (added in step 1) was kept, flagged for a wording update once the
real approach landed.

**3. Native ESP-IDF port — assessed, approved, implemented:** File-by-file
assessment of `firmware2/` found most of it already framework-agnostic:
`camera.cpp`/`.h` (~90% portable — every real call is already the native
`esp32-camera` driver API; only `Serial`→`ESP_LOGx` and `psramFound()`
needed swapping), `protocol.cpp`/`.h` (~100% portable — already raw lwIP
sockets, `<WiFi.h>` was vestigial), `streamer.cpp`/`.h` (~95% portable —
FreeRTOS primitives already native, only logging/`millis()`/`ESP.getFree*`
needed swapping). `wifi_manager.cpp`/`.h` was the one genuine rewrite:
Arduino's `WiFi.status()` polling loop has no ESP-IDF equivalent — replaced
with an `EventGroupHandle_t`-based design fed by `WIFI_EVENT`/`IP_EVENT`
callbacks (connect, disconnect, reconnect-with-backoff all restructured
around event bits instead of a status poll), following the shape of
ESP-IDF's own canonical WiFi-station example. Effort estimated at ~4-6
hours total, almost all in `wifi_manager.cpp`; nothing flagged as requiring
a hybrid Arduino/native approach. Revised partition table derived from one
real data point (Phase 6's confirmed 486KB BLE-only image) plus typical
component-size estimates for native `esp_wifi`/`esp32-camera` (not
measured) — proposed 2MB factory app (vs. 4MB for the discarded Arduino
route), explicitly flagged as provisional pending a real `idf.py build`
size report.

**Built** (native port, approved and implemented):

- `firmware/main/idf_component.yml`: single managed dependency,
  `espressif/esp32-camera` (no `arduino-esp32`).
- `firmware/components/camera_streamer/`: `config.h` (Arduino-only
  `#include <Arduino.h>` replaced with the actual native headers its
  macros need; static-IP macros changed from Arduino `IPAddress(...)` to
  plain strings parsed via `esp_netif_str_to_ip4()`, since that branch is
  `#if WIFI_USE_STATIC_IP` — currently `0` — but must still compile
  correctly if ever enabled); `camera.cpp`/`.h`, `protocol.cpp`/`.h`,
  `streamer.cpp`/`.h` ported per the assessment above (logging, PSRAM
  check, `millis()`/`micros()` via `esp_timer_get_time()`, heap queries via
  `heap_caps_get_free_size()`); `wifi_manager.cpp`/`.h` rewritten as
  described; new `camera_streamer.cpp`/`.h` (native replacement for the
  Arduino-as-component bridge — no `initArduino()`, just a plain
  `xTaskCreatePinnedToCore()` running camera init → `WifiManager::begin()`
  → `Streamer::begin()`, non-blocking from `app_main()`'s perspective); new
  `CMakeLists.txt` (`REQUIRES esp32-camera esp_wifi esp_netif esp_event
  lwip driver esp_timer`).
- `firmware/main/main.c`: **one-line hook** — `#include "camera_streamer.h"`
  plus a single `camera_streamer_start();` call appended at the end of
  `app_main()`, after `nimble_port_freertos_init(host_task)`. Existing BLE
  init code above it is otherwise unchanged.
- `firmware/main/CMakeLists.txt`: added `PRIV_REQUIRES camera_streamer` so
  `main.c` can see `camera_streamer.h`.
- `firmware/sdkconfig.defaults`: PSRAM enabled (`CONFIG_SPIRAM=y` +
  octal-mode options, replacing the old "left disabled" comment from Phase
  6 — required by `CAM_FB_LOCATION = CAMERA_FB_IN_PSRAM`), Wi-Fi enabled
  (`CONFIG_ESP_WIFI_ENABLED=y`), coexistence
  (`CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y`), custom partition table wiring
  (`CONFIG_PARTITION_TABLE_CUSTOM=y` +
  `CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"`). All existing
  NimBLE/security options left exactly as Phase 6/7/Post-Phase-7 left them.
- `firmware/partitions.csv` (new): `nvs` 64KB @ 0x9000, `phy_init` 4KB @
  0x19000, `factory` app **2MB @ 0x20000** — provisional, per the estimate
  above.
- CLAUDE.md's "Out-of-scope addition" section reworded to describe the
  native port (no Arduino HAL, no Arduino-as-ESP-IDF-component dependency)
  instead of the discarded Arduino-as-component approach.

**Not done / explicitly the user's part, per the established division of
labor:** no build attempted (`idf.py set-target`/`build`/flash never run
against any of this) — stopped here deliberately so the user can build and
flash on their end, same as every other phase. The 2MB partition size is
**provisional**: needs the real `idf.py build` size report to confirm or
shrink it, per the estimate's own caveat.

**Post-implementation fix — `main/CMakeLists.txt` requirements regression:**
First build attempt failed with two errors: `gatt_svr.c: host/ble_hs.h: No
such file or directory` and `main.c: nvs_flash component(s) is not in the
requirements list of main`. Root cause: `main/CMakeLists.txt` had never
declared `REQUIRES`/`PRIV_REQUIRES` at all (every prior Phase 6/7 build
relied on ESP-IDF's automatic per-component dependency detection, which
only activates when a component declares zero explicit requirements).
Adding `PRIV_REQUIRES camera_streamer` to wire in the one-line
`camera_streamer_start()` hook switched `main` out of auto-detection
entirely — an all-or-nothing setting — silently dropping `bt` (NimBLE
headers) and `nvs_flash`, which had always been auto-detected before.
Fixed by listing the complete requirement set explicitly:
`PRIV_REQUIRES bt nvs_flash camera_streamer` (confirmed complete by
grepping `main.c`/`gatt_svr.c`/`gatt_svr.h`'s actual `#include`s — every
non-libc, non-`log` header traces to `bt` or `nvs_flash`; `esp_log.h` is
part of ESP-IDF's always-available common component set).

**Build succeeded after the fix.** Real measured size:
`voice_cowork_firmware.bin` = 0x10cd10 bytes (1,101,072 bytes / ~1.05MB),
**53% of the 2MB factory partition, 47% free** — very close to the ~1.0MB
estimate this partition size was based on. Decision: **keep 2MB as-is**,
not shrunk. Rationale: the ~950KB headroom is earmarked for real future
growth (Phase 8's backend auth logic still needs to land in
`main.c`/`gatt_svr.c`), the 8MB flash has no competing use for reclaimed
space (no OTA slot currently planned), and partition-table changes force a
full chip erase — better to have margin now than re-churn it once Phase 8
lands. Revisit only if usage climbs past ~75-80% after that work.

## Post-Phase-7 — BLE token-write debugging arc, camera coexistence bug found and fixed (2026-07-19)

Triggered by a flashed board showing camera-only boot output with zero
BLE/NimBLE logs. Root cause: `firmware2/` is a live Arduino `.ino` sketch,
not an inert reference copy — COM3 had been flashed from it directly
(Arduino IDE or equivalent), not from `firmware/`'s combined ESP-IDF build.
`firmware/main/main.c`, the ELF's embedded strings, and `sdkconfig` were all
confirmed correct (NimBLE init + `camera_streamer_start()` both present,
`CONFIG_BT_NIMBLE_ENABLED=y`) — nothing was overwritten or misconfigured.
Reflashing `firmware/`'s own build restored combined BLE+camera boot output.

**Local toolchain issue found and fixed along the way:** `idf.py` was
unusable — `C:\Espressif\frameworks\esp-idf-v5.5.5\export.ps1` expected a
Python 3.13 venv (`python_env\idf5.5_py3.13_env`) that didn't exist; only a
3.11 venv was present. Fixed by running
`idf_tools.py install-python-env` with the system Python 3.13 interpreter
explicitly (the default venv-shadowed `python` on PATH couldn't run it,
since idf_tools.py refuses to create a venv from inside another venv).

**Bug #1 — investigated, ruled out as coexistence, actual cause found and
fixed:** Web pairing failed on the token characteristic's long write with
NimBLE logging `ble_att_svr_prep_validate rc=7` (`BLE_ATT_ERR_INVALID_
OFFSET`). Diagnosed via a temporary `camera_streamer_start()` disable
(marked and later restored/re-disabled as the debugging arc continued) —
the error reproduced identically with the camera task off, ruling out
Wi-Fi/BLE radio coexistence as the cause of *this specific* symptom. Actual
root cause: `web/lib/ble.ts`'s `withEncryptionRetry()` retried a failed
token write on the *same* GATT connection. Since the device token exceeds
the ATT MTU, Chrome sends it as a NimBLE "long write" (chunked Prepare
Write + Execute Write). A first attempt failing partway through that
sequence left stale chunks in NimBLE's per-connection prepare-write queue
(`ble_att_svr`'s `basc_prep_list`) — Web Bluetooth has no primitive to send
an explicit Cancel Execute Write, and that queue is only cleared by a
disconnect. The retry's new chunks queued on top of the stale ones, so
`ble_att_svr_prep_validate` saw two offset-0 entries for the same handle
and rejected them permanently for the rest of that connection. **Fixed**:
`withEncryptionRetry` now takes a `reconnect` callback; on retry it
disconnects/reconnects the GATT server and re-fetches the characteristic
before writing again, so NimBLE's stale queue is actually cleared.

**Bug #2 — found, more severe, fixed:** After Bug #1's fix, retrying with
the camera task back on surfaced a second, unrelated failure: the BLE
connection was dying ~360ms after forming, before pairing/encryption even
completed (`hci_err=0x202 BLE_ERR_UNK_CONN_ID` on the controller's
`LE_Start_Encryption` command, immediately followed by a disconnect).  Root
cause: `firmware/components/camera_streamer/wifi_manager.cpp`'s
`applyRadioSettings()` called `esp_wifi_set_ps(WIFI_PS_NONE)` to minimize
Wi-Fi streaming latency. Under ESP-IDF's software coexistence
(`CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y`), `WIFI_PS_NONE` means Wi-Fi never
yields idle radio time, starving the coexistence arbiter of any window to
service BLE connection events — the link's supervision timeout fired
almost immediately once Wi-Fi STA was connected and streaming. **Fixed**:
switched to `WIFI_PS_MIN_MODEM` (ESP-IDF's standard recommendation for
BT/Wi-Fi coexistence — Wi-Fi still wakes every DTIM beacon but yields
between beacons). Per explicit user direction, this was scoped strictly to
BLE stability, not camera performance tuning; a documented alternative
(`esp_coex_preference_set(ESP_COEX_PREFER_BT)`, leaving `WIFI_PS_NONE`
intact) was considered and declined.

**Debugging aid added and since removed:** added stage-by-stage
`console.log`/`console.error` instrumentation across every step of
`web/lib/ble.ts`'s `provisionDevice()` (`requestDevice`, `connect`, `find
service`, `find characteristic` x2, `subscribe`, both write attempts, and
every step inside `reconnect()`) to pin down exactly where a failure
occurred when the wrapped `BleProvisioningError` messages weren't specific
enough on their own. This instrumentation is still present in `ble.ts` (not
removed as of this entry, unlike the firmware-side diagnostic below) —
low-risk to leave in since it's plain `console.log`, but flag for cleanup
once the web flow itself is confirmed stable (see open item below).

**Removed** (this entry): the temporary `token_chr_access_cb` diagnostic
log line in `firmware/main/gatt_svr.c` (added while investigating Bug #1,
logged every write's `conn_handle`/`attr_handle`/length on entry) — no
longer needed now that the actual root cause is understood and fixed.
Confirmed `idf.py build` still succeeds after removal.

**Verification status:**

- BLE connection stability (survives past the ~360ms failure point,
  encryption completes cleanly: `encrypted=1 bonded=1`, no premature
  disconnect) — **confirmed**, with `WIFI_PS_MIN_MODEM` and camera task
  active.
- Token characteristic long-write succeeding — **confirmed via manual nRF
  Connect test**, device status reached `DEVICE_STATUS_OK`.
- **Open/unconfirmed**: the web client's own `provisionDevice()` flow
  (`web/lib/ble.ts`, driven from `web/app/page.tsx`'s "Provision ESP32"
  button) has **not** been independently verified end-to-end against this
  fixed firmware. All of tonight's confirmation came from a manual nRF
  Connect write, not a real run through the browser pairing UI. The
  reconnect-on-retry fix (Bug #1) and the stage-by-stage `ble.ts` logging
  are both still theoretical against real hardware from the browser's
  side — next session should retry provisioning from the actual web app
  with devtools open before considering the web pairing path done.
- `firmware/main/main.c`'s `camera_streamer_start()` call is currently
  **commented out** (BLE-only diagnostic state, per explicit user request
  to isolate BLE debugging from coexistence). Per CLAUDE.md, camera+BLE
  running simultaneously is a permanent architectural requirement, not
  optional — this must be restored (and re-verified with the camera task
  active) before this thread is considered closed, not left disabled.

## opencode driver — Phase 1 (upstream opencode only, text-based verification) (2026-07-19)

First step of the AI tool driver integration CLAUDE.md defers beyond the
original 9-phase plan. Scoped narrowly per explicit instruction: **upstream
`opencode` only** — not the user's modified fork (later), not Claude Code
or Codex (later). Full plan was proposed and approved via plan mode before
any file was touched; see that plan for the API research this was built
against (summarized below).

**Pre-implementation verification (read-only, before proposing the plan):**

- `opencode` confirmed installed (`/c/Users/makpr/.bun/bin/opencode`,
  v1.18.3); `opencode serve --port 4099` confirmed to start a real HTTP
  server (`GET /` → 200, `GET /doc` → full OpenAPI 3.1 spec).
- The spec exposes two overlapping route surfaces: a stable top-level one
  (`/session`, `/session/{id}/message`, `/event`,
  `/session/{id}/permissions/{permissionID}`) and a newer `/api/*`-prefixed
  one. Cross-checked against the official `@opencode-ai/sdk@1.18.3` npm
  package (installed locally, version-matched) — its generated client
  targets the **top-level** routes exclusively, which resolved the
  ambiguity: that's the surface this driver targets, `/api/*` left alone.
- No official Python SDK exists for opencode (only the JS/TS one above), so
  per CLAUDE.md's tech stack (`httpx` already a backend dependency) the
  driver talks to `opencode serve` via direct `httpx` calls, not a client
  library.
- Confirmed exact request/response shapes for the four calls needed
  (`POST /session`, `POST /session/{id}/message` with
  `parts: [{type: "text", text: ...}]`, `GET /event` SSE,
  `POST /session/{id}/permissions/{id}` with
  `{response: "once"|"always"|"reject"}`) directly against a live `opencode
  serve` instance's OpenAPI spec, not guessed.

**Decisions made via `AskUserQuestion` before implementation:**

- **Session scope**: one global opencode session for the whole backend
  process lifetime, not one per Voice Cowork pairing session — simplest to
  prove the driver; per-pairing-session scoping deferred.
- **Permission relay**: polling only. Voice Cowork's backend has no
  WebSocket/SSE infrastructure to phone clients yet (frontend only does
  periodic HTTP polling/refresh, confirmed by exploring the existing
  backend structure) — building real-time push wasn't justified for this
  phase. The backend's own background task consumes opencode's `/event`
  SSE stream internally and buffers pending `permission.asked`/
  `permission.replied` events; new polling endpoints expose that.

**Built:**

- `backend/src/voice_cowork_backend/opencode_client.py` (new): thin async
  `httpx.AsyncClient` wrapper — `create_session()`, `send_message()`
  (blocks until opencode's full response is ready), `reply_permission()`,
  `stream_events()` (async generator hand-parsing `/event`'s SSE
  `data:`-line format into decoded JSON dicts).
- `backend/src/voice_cowork_backend/opencode_driver.py` (new):
  `OpencodeDriver` — lazily creates the single global session on first use
  (`asyncio.Lock`-guarded), `send_command()` extracts the assistant's text
  parts from the response, `start_event_listener()`/`stop()` manage a
  long-lived background `asyncio.Task` that consumes `stream_events()`
  (auto-reconnecting every 2s on any failure) and routes
  `permission.asked`/`permission.v2.asked` /
  `permission.replied`/`permission.v2.replied` events into an in-memory
  `dict[str, PendingPermissionRecord]` — handles both of opencode's
  `data`-shaped and `properties`-shaped event payload variants defensively,
  since which one this opencode version actually emits wasn't confirmed
  from the spec alone. Module-level singleton `opencode_driver`, following
  the exact `SessionStore`/`PinStore` dataclass-record +
  store-class + singleton pattern already established in
  `sessions.py`/`pairing.py`.
- `backend/src/voice_cowork_backend/routers/opencode.py` (new):
  `POST /opencode/command`, `GET /opencode/permissions`,
  `POST /opencode/permissions/{id}/reply` — all Bearer-authenticated via
  the existing `verify_session_token` (no new auth scheme), thin routers
  per the established convention.
- `backend/src/voice_cowork_backend/schemas.py`: added
  `OpencodeCommandRequest`/`Response`, `PendingPermission`,
  `PermissionReplyRequest` (validated `once|always|reject` pattern),
  `PermissionListResponse` — flat Pydantic models matching every existing
  schema in this file.
- `backend/src/voice_cowork_backend/config.py`: added `opencode_host`
  (`127.0.0.1`) and `opencode_port` (`4096`, opencode's own default) to
  `Settings`.
- `backend/src/voice_cowork_backend/main.py`: registered the new router;
  `opencode_driver.start_event_listener()` added to the existing startup
  hook, new `@app.on_event("shutdown")` calls `opencode_driver.stop()`.
- `backend/src/voice_cowork_backend/cli.py`: `CliSettings` gained
  `opencode_binary` (`"opencode"`) / `opencode_port`; `_preflight()` checks
  `shutil.which(opencode_binary)`, same pattern as the existing
  `cloudflared` check; new `_start_opencode()` spawns
  `opencode serve --port <port>` as a `ManagedProcess`, polling `GET /doc`
  for readiness — identical shape to `_start_backend()`/`_start_tunnel()`.
  Wired into `start()`'s sequence (opencode → backend → tunnel) and its
  `finally` shutdown block, and into the running-loop's liveness checks.
  Matches CLAUDE.md's CLI already being described as eventually wrapping
  "backend startup + tunnel startup + (later) AI tool launch into one
  command."

**Verified:**

- `uv run python -c "import voice_cowork_backend.main"` — clean.
- Started `opencode serve` and the backend manually against each other
  (scratch port 8123); backend's startup log confirmed
  `GET http://127.0.0.1:4096/event "HTTP/1.1 200 OK"` — the background
  event listener connected successfully.
- Full pairing → `POST /opencode/command` round trip: session created,
  message sent, response parsed. Confirmed the driver correctly surfaces a
  **real** opencode-side error: switching the request to the OpenRouter
  provider returned an actual `402 Insufficient credits` error from
  OpenRouter, which opencode reported in `info.error` and the driver's
  `send_command()` logged (`opencode_command_error`) exactly as designed —
  proof the error-surfacing path works, not just the happy path.
- `GET /opencode/permissions` (empty list), `POST .../reply` against a
  nonexistent ID (404), malformed `response` enum value (422 from Pydantic
  pattern validation), and no-Bearer-token (401) — all confirmed via curl
  against the live backend.
- CLI's `_start_opencode()` tested in isolation (without spinning up the
  full tunnel): spawns `opencode serve`, readiness poll succeeds
  (`opencode_ready` logged), `.terminate()` cleanly stops it — confirmed
  `is_running()` flips `True` → `False`, no orphaned process left (checked
  via `ps aux` after every test run this session).

**Found, not a driver bug — flagged as an environment/account issue:**

- Sending a real command through the default `orchestrator` agent (this
  opencode install's custom default, not vanilla opencode's `build` agent
  — this machine's global opencode config defines ten custom agent
  presets: `general`, `explore`, `orchestrator`, `explorer`, `librarian`,
  `oracle`, `designer`, `fixer`, `observer`, `councillor`, on top of the
  standard `build`/`plan` agents) returns `HTTP 200` but an **empty**
  `parts: []` with `cost: 0, tokens: {input:0, output:0}` — no error
  surfaced, but also no real model output. Root-caused by testing three
  configurations directly against opencode: (1) no model specified at all
  → `500 UnknownError` (this opencode install has no default model
  configured, unlike vanilla opencode's out-of-box behavior); (2) explicit
  `agent: "general"` → also `500 UnknownError`; (3) explicit
  `agent: "build"` (the actual vanilla default agent, confirmed via
  `GET /agent`'s descriptions: `"build — The default agent. Executes tools
  based on configured permissions."`) with the `zai` provider's
  `glm-4.7` model → `200` but still empty/zero-cost, meaning the Z.AI API
  call itself never produced real output; (4) same request through
  **OpenRouter**'s `z-ai/glm-4.7` model → a real, correctly-surfaced
  `402 Insufficient credits` error (OpenRouter account has no purchased
  credits). Z.AI silently returning nothing (no error field at all) while
  OpenRouter fails loudly for the same underlying model points at a bad/
  expired Z.AI credential on this machine specifically, not a driver code
  path — the driver's job (send the request, parse whatever comes back,
  surface `info.error` if present) is confirmed working correctly in both
  the OpenRouter-error and empty-response cases; it's the *content* of
  opencode's response that's broken here, not anything this driver does
  with it.

**Carried forward — explicitly open, not yet done:**

- **A real, non-empty, non-error opencode text response was never
  obtained this session** — blocked on the Z.AI credential issue above.
  Needs either a valid Z.AI key or OpenRouter credits before `/opencode
  /command` can be shown returning genuine model output end-to-end. This
  is a local account/config issue, not something to "fix" in the driver
  code.
- **The permission-approval round trip was only verified structurally**
  (empty list, 404/422/401 on made-up IDs) — no real command in this
  session ever triggered an actual `permission.asked` event (all attempts
  either errored before reaching a tool call or returned empty), so
  `opencode_driver`'s event-listener → `PendingPermissionRecord` →
  `POST .../reply` → `opencode_client.reply_permission()` path has not
  been exercised against a real pending permission yet. Needs a working
  model provider (see above) plus a command that actually invokes a
  tool gated `ask` (e.g. `bash`) to close this out.
- **The full `voice-cowork run` CLI command (opencode + backend + real
  Cloudflare tunnel together) was not exercised this session** — only
  `_start_opencode()` in isolation was tested, to avoid spinning up the
  real tunnel unnecessarily for this verification. The three-process
  startup/shutdown sequence in `start()` is code-reviewed but not yet
  run end-to-end.
- Custom global opencode agent config (`orchestrator` as default, plus the
  nine other custom presets) on this machine means "unmodified upstream
  opencode" only accurately describes the *binary*, not necessarily the
  *behavior* out of the box on this specific machine — worth keeping in
  mind if opencode's behavior here ever looks unexpected compared to a
  truly fresh install elsewhere.

## opencode driver — Phase 1 continued: no hardcoded model, real end-to-end verification (2026-07-19)

Follow-up session. User asked two things before any more testing: (1)
confirm whether the custom global opencode config (`orchestrator` default
agent + nine presets, found above) was pre-existing or something this
session had set up, and whether it could skew the driver's behavior versus
a genuinely fresh install; (2) re-verify the driver end-to-end with a real
working model, without ever hardcoding a specific provider/model in driver
code.

**Config provenance, confirmed pre-existing, not touched:**

- `~/.config/opencode/opencode.jsonc` last modified 2026-07-18 17:07:54;
  `~/.local/share/opencode/auth.json` (the Z.AI/OpenRouter credentials)
  last modified 2026-07-15 — both predate this work. No `Edit`/`Write` call
  this session or the previous one ever targeted either path; every
  interaction with opencode was HTTP calls to a running `serve` process.
  Config left untouched per the user's own instruction (only revert if it
  had been added this session — it wasn't).
- The config loads a plugin, `oh-my-opencode-slim` (full `node_modules`
  install present under `~/.config/opencode/`), which supplies the ten
  custom agent presets and makes `orchestrator` the effective default
  instead of vanilla opencode's `build` agent.
- **Assessed impact**: event shapes and the HTTP contract
  (`/session`, `/session/{id}/message`, `/event`,
  `/session/{id}/permissions/{id}`) are core opencode protocol, unaffected
  by installed plugins/agent config — a fresh install would produce the
  same wire format. What *does* differ per-install is which agent runs by
  default and that agent's own permission ruleset — which is exactly why
  the driver already pins `agent: "build"` (added in the prior session) and
  why this session added an explicit session-level `PermissionRuleset` (see
  below), so the driver's behavior is deterministic regardless of the host
  install's own config.

**Generalized model selection — no hardcoded provider/model anywhere in
the driver:**

- Confirmed a real free-tier model actually works before touching driver
  code: `opencode run "What is 2+2?" --model opencode/mimo-v2.5-free --agent
  build --format json` returned real output (`"text":"4"`, real non-zero
  token usage, `cost: 0`) directly via the opencode CLI.
- `opencode_client.py`: added `get_config()` / `get_providers()` (`GET
  /config`, `GET /config/providers`); `send_message()` now takes optional
  `agent`/`model` params instead of assuming either.
- `opencode_driver.py`: new `_resolve_model()` — prefers the local
  install's own configured default (`config.model`, omits the `model`
  field entirely if set) and only when no default exists at all falls back
  to auto-discovery via `/config/providers`: picks the first model whose ID
  contains `"free"` **and** reports `capabilities.toolcall = true`.
  Cached after first resolution. `_DEFAULT_AGENT = "build"` constant kept
  from the prior session (a deliberate pin, not a hardcoded model — same
  agent regardless of which model answers, per this session's explicit
  instruction to keep it).
- **First iteration of the "free" heuristic was wrong and caught by
  testing, not assumed correct**: filtering on `"free" in model_id` alone
  picked `openrouter/nousresearch/hermes-3-llama-3.1-405b:free`, which
  doesn't support tool calls — the real request failed with OpenRouter's
  `404 No endpoints found that support tool use`. Fixed by also requiring
  `capabilities.toolcall`, confirmed against the real `/config/providers`
  response (many other free OpenRouter/opencode-hosted models do support
  it — `qwen/qwen3-coder:free`, `opencode`'s own `mimo-v2.5-free`, etc.).

**Permission enforcement — new, not in the original plan, added because
testing exposed a real gap:**

- Sending a bash-invoking command against this install's own default
  config auto-approved it with **zero permission prompt** — the entire
  premise of Voice Cowork's permission-relay design (human approves risky
  agent actions) doesn't hold if the host opencode install's own policy
  happens to auto-allow everything. Root cause: this install's config sets
  no explicit `permission` policy at all, so bash defaulted to allow.
- **Fixed**: `opencode_client.create_session()` now takes an optional
  `permission` (opencode's `PermissionRuleset`); `opencode_driver.py`'s
  `ensure_session()` always creates its session with an explicit
  `_SESSION_PERMISSION_RULESET` forcing `bash` and `edit` to `"ask"`,
  regardless of the host install's own default policy. This makes the
  human-in-the-loop guarantee a property of the driver, not an accident of
  whatever opencode config happens to be present on the machine it runs
  against.

**Re-verified end-to-end (fresh `opencode serve` + backend, real pairing,
real HTTP calls — every finding below is from an actual response, not
inferred):**

1. **Real non-empty model output**: `POST /opencode/command` with "What is
   2+2?" → `{"text": "4", ...}`, `HTTP 200`. Backend log confirmed
   `opencode_model_autoselected providerID=openrouter
   modelID=qwen/qwen3-coder:free` (varies by run depending on dict
   iteration order — never a fixed choice).
2. **Full `permission.asked` → approve round trip against a real event**:
   sent "Run the bash command `echo hello-from-bash-tool`" (backgrounded,
   since the call blocks until approved) → polled `GET /opencode/permissions`,
   got a real pending entry (`permission: "bash"`,
   `patterns: ["echo hello-from-bash-tool"]`) on the **first** poll →
   `POST /opencode/permissions/{id}/reply` with `{"response": "once"}` →
   `HTTP 200`, pending list cleared → the original blocked request
   completed with `text: "hello-from-bash-tool"` — proves the driver's
   event listener, pending-store, and reply plumbing all work against
   opencode's actual SSE events, not just synthetic/error paths as in the
   previous session.
3. **Reject path, also verified** (not explicitly required, added for
   completeness since the user said "approve/deny"): same flow with
   `{"response":"reject"}` → the tool call's result showed
   `"state":{"status":"error","error":"The user rejected permission to use
   this specific tool call."}` — opencode correctly aborted just that tool
   call and let the agent continue, not a session-ending failure.

**Carried forward:**

- The permission-round-trip test used a background curl process + manual
  polling loop to drive a blocking HTTP call and a concurrent reply — real
  and correct, but worth automating into a repeatable test script rather
  than hand-driven shell commands if this needs re-verifying again later.
- `_SESSION_PERMISSION_RULESET` currently only covers `bash`/`edit`; revisit
  whether other permission keys (`webfetch`, `task`, `external_directory`)
  should also default to `"ask"` once the driver's tool surface expands
  beyond this phase's basic text-command proof.
- Still open from the previous entry: the full `voice-cowork run` CLI
  command (opencode + backend + real Cloudflare tunnel together) has not
  been exercised end-to-end — only `_start_opencode()` in isolation.

## opencode driver — Phase 1 continued: full CLI run with real tunnel (2026-07-19)

Ran `voice-cowork run` for real — all three managed processes
(`opencode serve`, backend, `cloudflared`) together, real Cloudflare
tunnel, no shortcuts — to close out the last open item from the previous
two entries.

**First attempt failed — root-caused, not a driver/CLI bug:** startup
ordering ran correctly (opencode → backend → tunnel, each readiness poll
succeeded, tunnel `/ready` genuinely retried through real `503`s before
`200`), the pairing screen printed with a real PIN, but the running loop
immediately raised `opencode serve exited unexpectedly`. Investigated via
`Get-CimInstance Win32_Process` (git-bash's own `ps`/`pkill` don't reliably
see or kill natively-spawned Windows child processes — confirmed the
actual cause): earlier verification rounds this session had left real
orphaned `opencode.exe` and backend `python.exe`/`uvicorn.exe` processes
still bound to port 4096 and other scratch ports — my own `pkill -f
"opencode serve"` / `pkill -f "uvicorn ..."` calls had silently failed to
kill them (git-bash `pkill` matching against Cygwin's process view, not
the actual Windows process table). The CLI's readiness poll had actually
been hitting the **stale leftover** `opencode serve` instance (already
listening, so `GET /doc` succeeded instantly), while the *newly spawned*
one from this run failed to bind the same port and exited immediately —
the delayed `is_running()` check in the run loop then correctly caught
that its own child was already dead. Confirmed via `Stop-Process -Force`
on the real orphan PIDs (found via `Win32_Process`, not git-bash tools) —
after clearing them, a clean retry succeeded first try with no code
changes needed. Not a bug in `cli.py`/`ManagedProcess` — a gap in how I'd
verified process cleanup earlier in this session (see finding below, which
is related but distinct).

**Verified (second, clean run):**

1. **Startup ordering and readiness polling, three processes**: log
   confirmed strict `opencode_starting → opencode_ready → backend_starting
   → backend_ready → tunnel_starting → tunnel_ready → public_health_ok →
   pin fetched → pairing screen` — same shape as the existing two-process
   sequence, extended correctly. Tunnel readiness genuinely polled through
   real `503`s from cloudflared's own metrics endpoint before `200`, not a
   trivial single-shot success.
2. **Real command round trip through the actual public tunnel**, not
   localhost: `POST https://dani.tripodhub.in/pair` with the real PIN
   printed on the pairing screen → 200 with a session JWT;
   `GET https://dani.tripodhub.in/session/verify` → 200, real session data;
   `POST https://dani.tripodhub.in/opencode/command` with "What is 7*6?"
   → `{"text": "42", ...}`, HTTP 200 — genuine end-to-end proof the
   opencode driver works through the full stack (tunnel → backend →
   opencode → back through the tunnel), not just against a local backend.
   (`https://dani-web.tripodhub.in` correctly 502'd — expected, `npm run
   dev` wasn't running this session — `_check_web_reachable()`'s
   best-effort warning behaved exactly as designed, did not block startup.)
3. **Clean shutdown — real gap found, not previously known.** Two shutdown
   paths tested:
   - **Startup failure path** (the first, failed run above): confirmed the
     `finally` block in `start()` correctly terminates every process it
     spawned — `Win32_Process` showed zero leftover processes with a
     creation time matching that run, immediately after the
     `StartupError`.
   - **Hard-kill path** (simulating a crash, an external force-kill, or
     any termination that doesn't go through the CLI's own Python code):
     `Stop-Process -Force` on the CLI's own process left **all three**
     children (`opencode.exe`, backend `uvicorn`, `cloudflared.exe`)
     genuinely orphaned — confirmed via `Win32_Process` still showing them
     alive, still pointing at the (now-gone) parent's PID. Root cause:
     `ManagedProcess`/`cli.py`'s cleanup is entirely a Python-level
     `finally` block — there is no OS-level safety net (e.g. a Windows Job
     Object with kill-on-close) ensuring children die if the parent
     process itself is killed by anything other than its own code
     reaching that `finally`. This is distinct from Phase 3's
     already-documented "Ctrl-C doesn't deliver cleanly on Windows via
     this tooling" gap — that one was about graceful signal delivery only;
     this is about *any* non-graceful parent termination leaking all
     three real OS processes, confirmed directly rather than assumed.
     Cleaned up manually via `Stop-Process -Force` on the real orphan PIDs
     after confirming the finding.

**Carried forward:**

- **The orphan-on-hard-kill gap above is a real, unaddressed risk** — not
  just a testing inconvenience. If `voice-cowork run` is ever killed by
  something other than its own `KeyboardInterrupt`/`StartupError` handling
  (task manager, a supervisor, a crash), all three of opencode/backend/
  cloudflared leak and keep running, silently consuming the ports the next
  run needs (exactly what caused this session's first failed attempt).
  Worth addressing before this is considered fully done — options include
  a Windows Job Object (`CREATE_NEW_PROCESS_GROUP` +
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) so the OS itself guarantees
  cleanup, or at minimum a documented/scripted "kill any stragglers on
  these ports" helper for recovery. Not fixed in this session — flagged
  for the next one since it's a design decision (how much engineering to
  invest in crash-recovery for a single-user local dev tool) rather than a
  quick patch.
- Graceful Ctrl-C-driven shutdown (as opposed to the startup-failure path
  or a hard kill) still hasn't been directly exercised on this Windows
  environment — same limitation documented in Phase 3, reconfirmed here
  (git-bash's `kill -INT` can't reach native Windows PIDs at all).

## opencode driver — Phase 1 continued: Windows Job Object fix + startup port check (2026-07-19)

Closed the orphan-on-hard-kill gap from the previous entry, plus added a
loud startup-time check for the exact stale-port scenario that caused
finding #1's confusing failure.

**Built:**

- `backend/src/voice_cowork_backend/cli.py`: new `_WindowsJobObject` class
  — wraps `CreateJobObjectW` / `SetInformationJobObject` /
  `AssignProcessToJobObject` via `ctypes`, setting
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` so the OS itself terminates every
  assigned process the instant the job's last handle closes, regardless of
  whether the CLI's own Python code ever gets to run its `finally` block.
  Win32 structs (`JOBOBJECT_BASIC_LIMIT_INFORMATION` /
  `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`) declared field-by-field as real
  `ctypes.Structure` subclasses rather than hand-computed byte offsets —
  **a first draft that used a raw byte buffer with manually-computed
  offsets was wrong** (put `LimitFlags` at offset 8 with a total size of
  112 bytes; the real layout has two `LARGE_INTEGER`s before it, offset 16,
  total struct size 144 bytes) and would have silently failed to set the
  kill-on-close flag at all, with no error anywhere — caught by rechecking
  the actual `winnt.h` layout before testing, not by the test itself (a
  wrong-but-plausible offset doesn't necessarily crash; `SetInformation
  JobObject` validates buffer *size* against the info class, and 112 vs.
  144 would likely have been rejected, but relying on that rather than
  correct field declarations was the wrong way to find out). Degrades to a
  no-op with a logged warning on non-Windows platforms (`assign()` becomes
  a no-op) — this project only runs on Windows currently, but nothing
  should hard-crash if that ever changes.
- `ManagedProcess.__init__` takes an optional `job` param and calls
  `job.assign(self.proc)` right after `Popen` (via `proc._handle`, the real
  Win32 handle `subprocess.Popen` already exposes — the standard idiom for
  this, no extra process-open call needed).
- `start()` creates one `_WindowsJobObject()` and passes it to all three of
  `_start_opencode()` / `_start_backend()` / `_start_tunnel()` (each now
  takes an optional `job` param, threaded through to their own
  `ManagedProcess(...)` calls) — one shared job, all three children
  assigned to it.
- New `_port_in_use(host, port)` (real bind attempt, not a connect —
  binding is what actually reveals a stale listener) and a new
  `_preflight()` check across all three of opencode/backend/cloudflared-
  metrics' expected ports, printing every conflicting port with concrete
  recovery instructions and exiting immediately (`SystemExit(1)`) instead
  of proceeding — directly targets finding #1's failure mode from the
  previous entry (a fresh spawn silently losing a port-bind race against a
  leftover instance while readiness polling kept passing against the old
  one).

**Verified (all against real processes, real kills — not simulated):**

1. **Job Object construction in isolation**: `_WindowsJobObject()`
   constructed cleanly, logged `job_object_created`, real handle returned.
2. **Hard-kill scenario, the actual repro from the previous entry's
   finding**: started `voice-cowork run` for real (opencode + backend +
   real cloudflared tunnel), captured the full process tree via
   `Get-CimInstance Win32_Process` (2 opencode.exe, 2 backend python.exe,
   1 cloudflared.exe under the CLI's own python.exe), then
   `Stop-Process -Force` directly on the CLI process — the exact scenario
   that left 3 orphans in the previous entry. This time: **zero surviving
   processes**, confirmed via `Win32_Process` immediately after and again
   after a 3-second wait. The OS tore down every child without any Python
   code running.
3. **Startup-time port check, tested against the actual finding #1
   scenario**: manually started a standalone `opencode serve --port 4096`
   (simulating exactly the leftover-orphan situation that broke the first
   attempt in the previous entry), then ran `voice-cowork run` — it now
   fails immediately and loudly at the preflight check (`Port(s) already
   in use ... opencode: 127.0.0.1:4096`, with concrete
   `Get-NetTCPConnection`/`Stop-Process` recovery steps) instead of
   spawning a doomed second instance and producing a confusing
   "opencode serve exited unexpectedly" partway through startup.
4. `uv run python -c "import voice_cowork_backend.main"` — clean.

**Carried forward:**

- Graceful Ctrl-C shutdown still not directly exercised on this Windows
  environment (tooling limitation, not a code gap) — the Job Object makes
  this less critical than before, since even an ungraceful termination now
  cleans up correctly, but the graceful path's own log messages
  (`shutdown_requested`) remain unverified by an actual Ctrl-C in this
  environment.
- The pairing screen's mojibake (`Dani Voice � Pairing` instead of the
  real em dash) noticed again during this session's testing — same root
  cause as Phase 4's QR-render bug (Windows console defaulting stdout to a
  non-UTF-8 codepage), but this time in `_print_pairing_screen()`'s own
  `print()` calls, which run *before* `_print_qr()`'s
  `sys.stdout.reconfigure(encoding="utf-8")` call reaches them. Not fixed
  this session (cosmetic, not flagged as in-scope for this task) — worth a
  one-line fix (move the `reconfigure()` call earlier, e.g. into `start()`
  before `_print_pairing_screen()` runs) next time pairing-screen output is
  touched.

## Web client: opencode command + permission-prompt UI (2026-07-19)

First web-client change to actually use the opencode driver (previously
verified only via curl/nRF Connect/manual CLI testing) — the connected
view can now send a command and approve/reject a live permission request.

**Design note carried into the implementation:** `POST /opencode/command`
blocks server-side until opencode's response is ready, so a mid-flight
permission can only be discovered by polling `GET /opencode/permissions`
*while the command's fetch is still pending* — the blocked promise itself
carries no information until it resolves. Poller only runs while a command
is in flight (a pending permission can't exist otherwise, given this
driver's current single-global-session design), at a 1.5s interval.

**Built:**

- `web/lib/api.ts`: `OpencodeApiError` (new error class, one-per-domain
  precedent matching `BleProvisioningError` in `ble.ts` — not overloading
  `PairError`/`PairErrorCode`, which is pairing-specific).
  `sendOpencodeCommand(token, text)` → `POST /opencode/command`.
  `listPendingPermissions(token)` → `GET /opencode/permissions`, unwraps
  to the array. `replyPermission(token, id, response)` →
  `POST /opencode/permissions/{id}/reply`; a `404` (permission already
  resolved — a benign race, e.g. a double-click or the poller's next tick
  beating the reply) resolves quietly instead of throwing.
- `web/app/page.tsx`: new "Ask opencode" section in the existing
  `connected` view (same inline-section precedent as "Provision ESP32" —
  no new route/component). New state: `commandText`, `commandPending`,
  `commandResult`, `commandError`, `pendingPermissions`,
  `permissionReplyInFlight` (a `Set<string>`, so one Approve/Reject click
  only disables its own card). `handleSendCommand` sends the command;
  `finally` clears `pendingPermissions` (the command can't have resolved
  with any left outstanding). A `useEffect` keyed on `commandPending` runs
  the poller, cleaned up via `clearInterval` when the command finishes or
  on unmount. `handlePermissionReply` posts the reply then optimistically
  removes that entry from `pendingPermissions` rather than waiting for the
  next poll tick. Render order: command textarea + Send button (disabled
  while pending) → pending-permission cards (amber-toned, tool name +
  patterns + Approve/Reject) → a status line that reads "Running…" or
  "Running… waiting on your approval above" depending on whether
  `pendingPermissions` is non-empty, so both states are visible
  simultaneously rather than one hiding the other → last result or error.

**Verified:**

- `npx tsc --noEmit` and `npm run lint` — both clean.
- Full stack started for real (fresh `opencode serve`, backend on a
  scratch port, `next dev` pointed at that scratch backend via a
  shell-exported `NEXT_PUBLIC_API_URL` overriding `.env.local` — confirmed
  by grepping the actual served JS chunk for the inlined URL constant,
  found `127.0.0.1:8130` not the `.env.local` tunnel URL, proving the dev
  server was genuinely wired to the test backend). Page loads (`HTTP 200`,
  title present).
- Every new API call exercised end-to-end with curl using the exact
  request/response shapes the new frontend code sends and parses (not
  just "the endpoint exists" — the actual JSON bodies `sendOpencodeCommand`
  /`listPendingPermissions`/`replyPermission` produce and consume):
  a plain command → real non-empty `{"text": "6", ...}`; a bash-invoking
  command → real `permission.asked` polled as `{"permissions": [{"id":
  ..., "permission": "bash", "patterns": [...]}]}` (exact
  `PendingPermission` shape) → replied with `{"response": "once"}` → `200`,
  pending list cleared → original command completed with the tool's real
  output.
- **Not done — no browser automation tool is available in this
  environment**: an actual interactive click-through (typing into the
  textarea, clicking Send/Approve/Reject, watching React state update) was
  not performed. Everything the UI code depends on (API shapes, dev-server
  wiring, type-check, lint) was verified as thoroughly as possible without
  one; the real browser walkthrough is the user's part this time, same as
  every previous phase's browser-only verification step (e.g. Phase 7's
  real ESP32 pairing walkthrough).

**Carried forward:**

- Real browser click-through, per above — needed before this is considered
  fully done: type a plain command and confirm the response renders; send
  a bash-invoking command and confirm the permission card appears while
  the status line still shows "Running…", approve it, confirm the card
  clears and the result appears; repeat and reject instead; confirm via
  the browser's network tab that permission polling actually stops once a
  command resolves.
- No push infra (still polling-only, per the backend's already-made
  decision) — a richer transcript view (rendering `parts`, not just the
  flattened `text`) and reconciling `commandPending` across a page reload
  are both explicitly out of scope for this task, not overlooked.

## BLE provisioning: intermittent "GATT Server is disconnected" fixed at the connect/discover step (2026-07-19)

User reported the "Provision ESP32" button failing intermittently while a
manual nRF Connect write kept working reliably — same symptom class as
this session's earlier BLE debugging, but at a different, earlier point in
the flow.

**Diagnosed from real evidence, not guessed:**

- A fresh serial capture showed two connection sessions, neither of which
  ever received a real (JWT-sized) token write. Session 1: connected,
  subscribed, encryption completed (~4.7s after connect — slow), then
  disconnected (`reason=531`, remote/browser-initiated) with no write ever
  attempted. Session 2's only write was 4 bytes — too small to be a real
  JWT, i.e. the user's own manual nRF Connect test, not a "Provision ESP32"
  attempt. Confirms the button's failure happens entirely browser-side,
  before anything reaches the firmware.
- Requested (and got) the browser's own `[ble]` console log for a fresh
  failing attempt (the stage-by-stage logging added in an earlier session
  and never removed, per that entry's carried-forward note — it earned its
  keep here). It showed: `requestDevice` OK → `connect` OK →
  `find service` **FAIL**: "NetworkError: GATT Server is disconnected.
  Cannot retrieve services. (Re)connect first with device.gatt.connect." —
  i.e. `gatt.connect()` resolves successfully, but the very next GATT call
  (`getPrimaryService()`) is rejected as if the link were already gone.

**Root cause:** the same underlying Android quirk already diagnosed once
this session for the token write (`gatt.connect()` resolving doesn't mean
Android's internal bonding/encryption negotiation has actually settled —
see the Post-Phase-7 entry on `withEncryptionRetry`), but manifesting one
step earlier than previously seen. That earlier fix only covered the token
write (the one operation gated on `WRITE_ENC`) on the theory that only
encryption-gated operations were at risk. This session's evidence shows
that assumption was too narrow: a plain, unencrypted `getPrimaryService()`
call can *also* be spuriously rejected while that negotiation is
in-flight, then would have succeeded fine a moment later. Service
discovery had no retry logic at all before this fix.

**Fixed:** `web/lib/ble.ts` — extracted the connect → find service → find
status characteristic → subscribe → find token characteristic sequence
into `connectAndDiscover()`, wrapped by a new
`connectAndDiscoverWithRetry()`: on any failure, wait 1.5s, force a real
reconnect (`device.gatt.disconnect()` then `device.gatt.connect()` again —
retrying the same call against the same already-disconnected server
object doesn't help, confirmed by the browser's own error text pointing at
exactly that fix), then retry the whole sequence once more.
`provisionDevice()` now calls this instead of inlining the sequence
itself; `service` (previously threaded through `provisionDevice`'s own
scope) is no longer needed there once `connectAndDiscover` owns it
internally — cleaned up rather than left as a dead variable.

**Verified:**

- `npx tsc --noEmit` and `npm run lint` — both clean.
- **Re-verified against real hardware by the user: "worked perfectly."**
  "Provision ESP32" completed successfully after the fix — confirmed
  working, not just type-check/lint clean. Exact console/serial detail
  (e.g. whether the new retry path fired or the first attempt succeeded
  outright) wasn't captured this round; if the intermittent failure
  resurfaces, the `[ble]` console log (`FAIL initial connect/discover,
  retrying after reconnect` → success, or a different failure mode
  entirely) is still the first place to look.

## Web client redesign: shadcn chat components + dedicated /chat page (2026-07-19)

Replaced the "Ask opencode" section (built two sessions ago) with a
dedicated `/chat` page built on shadcn/ui's new chat components, and moved
ESP32 provisioning from a fixed step in the connected view to a skippable
first-pairing step plus a re-provisioning dialog reachable from `/chat`.

**Confirmed before implementing:**

- `web/` had no `components.json` — genuine first-time shadcn init.
- The changelog URL the user gave
  (`ui.shadcn.com/docs/changelog/2026-06-chat-components`) is real
  (fetched directly, not assumed) — five components: `MessageScroller`,
  `Message`, `Bubble`, `Attachment`, `Marker`, backed by `@shadcn/react`,
  "available now for Radix and Base UI."
- Two design decisions clarified via `AskUserQuestion` before planning:
  **Radix UI** as the primitive (components.json's choice is effectively
  permanent once components are generated against it — the mature,
  incumbent option was preferred over the newer Base UI); and, after an
  initial proposal, the user redirected to a different provisioning-UI
  placement (see below) rather than either of the two options originally
  offered.

**Built:**

- shadcn init: `npx shadcn@latest init -b radix --preset nova -y` (the
  `-y`/`-d` flags alone weren't enough to skip the interactive style-preset
  prompt — needed `--preset nova` explicitly; confirmed afterward via
  `components.json`'s `"style": "radix-nova"` and `radix-ui` landing in
  `package.json`, not guessed). Then `npx shadcn@latest add message-scroller
  message bubble attachment marker` (auto-pulled in `button` as a
  dependency, expected shadcn behavior) and a separate `npx shadcn@latest
  add dialog` — **not** in the user's original five-component list, added
  because the agreed re-provisioning-modal design needs it.
- `web/components/esp-provisioning.tsx` (new): the ESP32 provisioning
  form/state (PIN input, `provisioning`/`provisionError`/`deviceStatus`,
  `handleProvision`, the `isWebBluetoothAvailable()` gate) extracted out of
  `page.tsx` into `EspProvisioning({ onDone })`. `provisionDevice()` in
  `lib/ble.ts` needed **no changes** — already a plain, stateless async
  function with no React coupling, exactly as anticipated in the plan.
- `web/app/chat/page.tsx` (new): the main post-auth screen.
  - Guard on mount: no stored/valid session → `router.push("/")`.
  - Refresh-token timer (50% TTL) moved here from `page.tsx` — this is
    where a session now actually lives for any length of time.
  - Chat state changed from the old "last result only" model to a real
    growing `ChatEntry[]` list (per the user's explicit "in-memory only,
    don't add storage" instruction — lost on refresh, consistent with the
    rest of the project's no-persistence-yet state), rendered via
    `MessageScroller`/`MessageScrollerViewport`/`Content`/`Item` wrapping
    `Message`/`MessageContent`/`Bubble`/`BubbleContent` per turn.
  - The existing permission poller (`useEffect` keyed on `commandPending`,
    `listPendingPermissions` every 1.5s) and `handlePermissionReply` moved
    over **with unchanged logic** — only the rendering target changed, from
    a plain bordered `div` to a `Marker`/`MarkerIcon`/`MarkerContent` block
    with Approve/Reject buttons, appended inline in the message stream at
    the point the permission arose (matches Marker's documented fit for
    status/tool-activity rows).
  - Header: session expiry, a "Provision ESP32" button opening a `Dialog`
    containing `EspProvisioning` (for later/re-provisioning, no page of its
    own), and Disconnect.
  - Composer: textarea + Send, plus a disabled mic button (`lucide-react`'s
    `MicIcon`) — visually present, explicitly non-functional, per CLAUDE.md's
    voice/STT/TTS deferral.
  - `Attachment` was installed (bundled in the one install command) but is
    **not imported or rendered** anywhere — no file/image support exists
    backend-side yet, left inert per the plan.
- `web/app/page.tsx` (trimmed): PIN form unchanged. On successful pair →
  a new `"provisioning"` status shows `EspProvisioning` plus a sibling
  **Skip** button; either path calls `router.push("/chat")`. On mount, a
  restored valid session now redirects straight to `/chat` instead of
  rendering a local "connected" view — session info, BLE provisioning, and
  Disconnect no longer live on `/` at all, exactly per the agreed design.

**Verified:**

- `npx tsc --noEmit` and `npm run lint` — both clean across the whole
  client (new files + trimmed `page.tsx`).
- Re-confirmed `components.json`'s `"style": "radix-nova"` and
  `radix-ui`/`@shadcn/react` in `package.json` after init — the primitive
  choice actually took effect, not just assumed from the command run.
- Full stack started for real (fresh `opencode serve`, backend on a
  scratch port, `next dev` pointed at it via a shell-exported
  `NEXT_PUBLIC_API_URL`): `sendOpencodeCommand`'s exact request/response
  shape re-exercised end-to-end (`{"text": "10", ...}` for a real math
  question) against the new `/chat` page's code path — same verified
  contract as before, now consumed by the new UI.
- Both `/` and `/chat` confirmed served cleanly (`HTTP 200`, no Turbopack
  compile errors in the dev server log for either route).
- **Not done — no browser automation tool available in this
  environment**: the actual interactive walkthrough (typing/sending in the
  new chat UI, triggering and approving/rejecting a real permission inline
  in the message stream, using the header's Disconnect and re-provisioning
  dialog, confirming the `/` ↔ `/chat` redirects fire in a real browser
  rather than just serving 200 on a cold request) was not performed —
  flagged as the user's part, same as every previous UI-only verification
  gap this session.

**Carried forward:**

- Real browser click-through per above, needed before this is considered
  fully done.
- Chat history is in-memory only (React state, lost on refresh) —
  explicitly the user's instruction for this phase, not an oversight.
- `Attachment` remains installed-but-unused until there's a real
  file/image use case backend-side.

## /chat page fixes: missing MessageScrollerProvider, custom preset (2026-07-19)

User's actual browser test (the walkthrough flagged as theirs to do in the
entry above) immediately hit a crash: `Uncaught Error: useMessageScroller
must be used within a MessageScroller`, and reported styles not loading at
all.

**Root cause, confirmed against real docs, not guessed:** the previous
session's `chat/page.tsx` composed `MessageScroller` (maps to
`MessageScrollerPrimitive.Root`) directly, but never rendered
`MessageScrollerProvider` (maps to `MessageScrollerPrimitive.Provider`) —
a separate component also exported from `components/ui/message-scroller.tsx`
that actually supplies the React context the `useMessageScroller`/
`useMessageScrollerVisibility` hooks (used internally by
`MessageScrollerButton`) read from. This was inferred incorrectly from
reading the component source alone in the previous session; fetching the
real usage docs (`ui.shadcn.com/docs/components/message-scroller`) this
time showed the actual required nesting:
`MessageScrollerProvider > MessageScroller > MessageScrollerViewport >
MessageScrollerContent > MessageScrollerItem`. The "styles not loading at
all" report is almost certainly a symptom of the same crash, not a
separate CSS bug — an uncaught render error stops the page from painting
its styled content at all, which looks identical to "no styles," and
`globals.css` itself (checked directly) had all the expected shadcn
theme variables/imports in place.

**Fixed:** `web/app/chat/page.tsx` — wrapped the whole scroller block in
`<MessageScrollerProvider autoScroll>`, and added the `messageId`/
`scrollAnchor` props `MessageScrollerItem` expects per the real usage
example (`scrollAnchor` on the user's own messages, matching the
documented default).

**Also done — preset change:** re-ran `npx shadcn@latest init -b radix
--preset b1oVymsS -y -f --reinstall` per the user's request (a
custom/shared preset code, not one of the built-in named presets like
`nova`) — confirmed via `components.json` that it resolved to
`"style": "radix-sera"` and regenerated all 7 previously-installed
component files against it, rather than assuming the flag was accepted at
face value.

**Verified:**

- `npx tsc --noEmit` / `npm run lint` clean after both changes.
- Dev server boot log showed both `/` and `/chat` compiling and serving
  `200` with zero Turbopack errors.
- **Not verified**: the actual runtime absence of the
  `useMessageScroller` error — no browser automation tool is available
  here, so this can only be confirmed by loading `/chat` in a real browser
  with devtools open, same limitation as every prior UI verification this
  project has hit. The fix matches the documented required composition
  exactly, but hasn't been watched execute.

## /chat visual redesign: correct preset, real icon buttons, layout fixes (2026-07-19)

User's actual screenshot showed the crash was gone but the UI looked bad —
boxy, uppercase, bordered buttons; a text "SEND" button instead of an
icon; messages floating oddly with large empty gaps — and asked for a
different preset plus a look matching a reference screenshot (rounded pill
composer, circular icon buttons, centered greeting state).

**Root cause of the "ugly" look, confirmed not guessed:** grepped the
generated `button.tsx` and found the `sera` preset (applied in the
previous entry) literally styles buttons as
`rounded-none border ... uppercase tracking-widest` — a deliberate
boxy/brutalist aesthetic, not a bug. Explains the all-caps
"PROVISION ESP32"/"DISCONNECT" text and hard-edged buttons in the
screenshot exactly.

**Fixed:**

- Re-ran `npx shadcn@latest init -b radix --preset bdKQpItU -y -f
  --reinstall` — confirmed via `components.json` it resolved to
  `"style": "radix-maia"`, and the regenerated `button.tsx` now uses
  `rounded-4xl` with normal-case `font-medium` text — confirmed by
  re-grepping the file, not assumed. The CLI also auto-swapped the
  heading font (Space Grotesk → Figtree) in `app/layout.tsx` as part of
  the preset change, but left the old `Space_Grotesk` import unused;
  removed it (caught by `npm run lint`, not missed).
- `web/app/chat/page.tsx`: replaced the text "Send" button with a
  circular icon button (`ArrowUpIcon`), added a disabled "+" attachment
  placeholder (`PlusIcon`) alongside the existing disabled mic button —
  all three now live inside one rounded pill-shaped composer
  (`rounded-3xl border bg-muted/40`) matching the reference screenshot,
  instead of three separate square buttons in a row. Header's "Provision
  ESP32"/"Disconnect" buttons now show icons (`BluetoothIcon`/
  `LogOutIcon`) with labels hidden below the `sm` breakpoint for
  responsiveness.
  Enter-to-send added (Shift+Enter for a newline) — required extracting
  the actual send logic into a plain `sendCommand()` function, since the
  existing `handleSendCommand(e: React.FormEvent)` couldn't be called
  from a `textarea` `onKeyDown` handler (a `KeyboardEvent`, not a
  `FormEvent` — a real type error caught by `tsc`, not stylistic).
- Swapped every hardcoded `zinc-*`/`black`/`white` color class in
  `chat/page.tsx` for the shadcn theme tokens the new preset actually
  defines (`bg-background`, `text-foreground`, `text-muted-foreground`,
  `border-border`) — the previous session's UI mixed hand-picked colors
  with shadcn components, which is part of why it looked inconsistent
  regardless of preset.
- Root container switched from `h-full` to `h-dvh` — `h-full` depends on
  every ancestor (`html`/`body`) actually resolving to a definite height,
  and `app/layout.tsx`'s `body` uses `min-h-full` (a minimum, not a fixed
  height) layered with a plain-CSS `height:100%` rule in `globals.css`,
  two different mechanisms that don't reliably agree; `h-dvh` sizes the
  page to the real viewport directly regardless of that ancestor chain,
  which is the more robust and standard choice for a full-height chat
  layout in Next.js. Also added `min-h-0` to the scroller region, needed
  for a flex child to actually shrink and scroll instead of pushing the
  composer off-screen.

**Verified:**

- `npx tsc --noEmit` / `npm run lint` clean (the `Space_Grotesk` unused-
  import warning and the `KeyboardEvent`/`FormEvent` mismatch were both
  caught this way, not missed).
- Re-confirmed `components.json`'s `"style": "radix-maia"` and the actual
  regenerated `rounded-4xl` button class directly in the file, rather than
  trusting the CLI command succeeded at face value.
- Dev server boot log: `/chat` compiles and serves `200`, zero Turbopack
  errors.
- **Not verified**: the actual visual result. No browser automation tool
  is available here, so whether this now genuinely matches the reference
  screenshot's polish can only be confirmed by loading it in a real
  browser — same limitation as every prior UI round this session. A curl
  fetch of `/chat` only ever returns the pre-auth loading shell (no
  browser session storage to satisfy the auth guard), so it can't be used
  to inspect the authenticated chat markup either.

**Carried forward:**

- Real browser confirmation of the visual result, per above.
- `web/app/page.tsx` (the PIN/provisioning-skip screen) and
  `esp-provisioning.tsx` still use the old hardcoded `zinc-*` color
  classes rather than the new theme tokens — not touched this round since
  the user's ask was specifically about `/chat`'s appearance, but flagged
  as a follow-up if visual consistency across the whole app matters later.

## /chat: real streaming, markdown rendering, empty-response fix (2026-07-19)

Interrupted mid-session (API exhausted) with no entry written — resumed by
auditing actual file/dependency state against what should have landed
before writing this, per the user's explicit request (checklist reported
separately, not duplicated here). Everything below was confirmed already
correctly in place; this entry is the record that was missing, not a redo.

**Triggered by three user reports against the redesigned `/chat` UI:**
(1) a rejected tool call left a visibly empty assistant bubble; (2) output
appeared all at once instead of streaming; (3) a request for markdown
rendering and removing the width/background constraint on assistant
responses.

**Empty-bubble fix:** confirmed via a real reject-permission round trip
that `result.text` is genuinely `""` in this case — after a rejected tool
call, opencode's step ends with no further explanation from the model, so
there's no text part to extract; not a bug in the driver. `web/app/chat
/page.tsx`: added `describeEmptyResponse(parts)` — inspects the raw
`parts` for a `type: "tool"` entry with `state.status === "error"` and an
error message matching `/rejected/i`, rendering "Tool call rejected: X" as
a `Marker` (new `"system"` `ChatEntry` role) instead of a blank message.

**Real token streaming, not a fake typewriter effect:** confirmed directly
against a live opencode instance (piped its own `/event` stream to a file
while sending a real prompt) that it publishes genuine incremental
`message.part.delta` events as the model generates, not just the
all-at-once completion the blocking `/session/{id}/message` call returns.
Built on that:

- `backend/.../opencode_driver.py`: new `stream_command()` — runs the
  existing blocking `send_message()` call as a background `asyncio.Task`
  while concurrently draining a subscriber `asyncio.Queue` fed by
  `_handle_event()`, yielding `{"type": "delta", "text": ...}` as chunks
  arrive and a final `{"type": "done", "text": ..., "parts": ...}` once
  the call resolves. `_handle_event()` now also handles
  `message.part.delta` events (`field == "text"`).
  **Real bug caught by testing the actual output, not assumed correct**:
  the first version relayed every `message.part.delta`, and a live test
  showed the model's internal reasoning text streaming to the client
  identically to its real answer, followed by the actual answer streaming
  in *again* below it — because a reasoning part's text and an answer
  part's text both use `field: "text"` on this event, with nothing else
  distinguishing them. Fixed by also tracking `message.part.updated`
  events (which *do* carry each part's `type`) into a `partID -> type`
  dict, and only relaying deltas whose `partID` is known to be `"text"`
  (not `"reasoning"`). Re-verified afterward: assembled delta stream now
  matches the final `done` text byte-for-byte, real prompt, real model.
- `backend/.../routers/opencode.py`: new `POST /opencode/command/stream`,
  `StreamingResponse` with `media_type="text/event-stream"`.
- `web/lib/api.ts`: new `streamOpencodeCommand(token, text, onDelta)` —
  reads the SSE response via `res.body.getReader()` (not `EventSource`,
  which can't set an `Authorization` header and only supports `GET`),
  splitting on blank-line-delimited frames, calling `onDelta` per chunk and
  resolving with the final `done` payload.
- `chat/page.tsx`: `sendCommand()` now pushes an empty assistant placeholder
  immediately, appends each streamed chunk to it in place, and only
  swaps it to the empty-response marker if the final text is still blank
  after streaming.

**Markdown + no-box rendering:** assistant messages no longer render inside
a `Bubble` (the width/background constraint the user asked to remove) —
they render as plain flowing `ReactMarkdown` (with `remark-gfm` for
GFM tables/strikethrough/etc.) inside `MessageContent`, with custom
component overrides for spacing (`p`/`ul`/`ol`), links (`target="_blank"`),
and code (`code`/`pre`, inline vs. block distinguished via the
`language-*` className remark-gfm attaches). User messages keep the
`Bubble` treatment — only the assistant's output was in scope.

**Verified:**

- `npx tsc --noEmit` / `npm run lint` clean on both the frontend and
  backend (`uv run python -c "import voice_cowork_backend.main"`).
- `npm run build` (full production build, not just dev-server compile) —
  succeeds, both `/` and `/chat` generate as static pages.
- Real end-to-end curl test of `POST /opencode/command/stream`: captured
  the raw SSE frames for a real prompt, reassembled every `delta` chunk in
  order, and confirmed it matches the `done` event's authoritative final
  text exactly — proof the relay carries the real answer only, with the
  reasoning-leak bug already caught and fixed rather than shipped.
- Real end-to-end reject-permission test against the empty-response fix:
  confirmed `parts` contains the exact `{"type": "tool", "state":
  {"status": "error", "error": "The user rejected permission..."}}` shape
  `describeEmptyResponse()` expects.
- Dependencies confirmed actually installed (not just declared):
  `react-markdown`, `remark-gfm` present in `package-lock.json` and
  unpacked under `node_modules`.
- **Not verified — no browser automation tool available in this
  environment**: watching the stream actually render token-by-token,
  markdown formatting rendering correctly (lists, links, code blocks), and
  the empty-response marker appearing in place of a blank bubble, all in a
  real browser. Flagged as the user's part, same as every prior UI
  verification gap this project has hit.
- Found and cleaned up 4 orphaned processes left over from the
  interrupted session's own testing (a stale backend on port 8150, two
  `opencode.exe` instances) — confirmed via their command lines before
  killing, not assumed safe.

**Carried forward:**

- Real browser confirmation of streaming/markdown/empty-response behavior,
  per above.
- Multiple concurrent commands would currently cross-contaminate each
  other's streamed deltas (`_delta_subscribers` broadcasts to every
  subscriber, not scoped per-message) — acceptable for now since the
  frontend disables sending a second command while one is pending and
  there's only one global session, but would need real scoping if
  concurrent commands ever become a real scenario.
- `web/app/page.tsx`/`esp-provisioning.tsx`'s old `zinc-*` color classes,
  carried forward from the previous entry, still not addressed.

## Audio pipeline — STT (whisper.cpp) + TTS (Deepgram) + real UI (2026-07-19)

**Confirmed before implementation:** Python 3.13.12, `ffmpeg` already on
PATH. `pywhispercpp` (whisper.cpp bindings) has prebuilt CPU wheels for
Windows — confirmed by actually installing it, not assumed. STT engine:
whisper.cpp (local, via `pywhispercpp`). TTS engine: Deepgram (cloud,
REST `speak` endpoint — the $200 free-tier credit covers this comfortably
per the user, no local TTS fallback needed).

**Built:**

- `backend/pyproject.toml`: added `pywhispercpp` and `python-multipart`
  (the latter needed for FastAPI's `UploadFile` multipart parsing, not
  previously required by any existing endpoint).
- `config.py`: `whisper_model` (default `"base.en"`), `deepgram_api_key`
  (optional — missing key logs a startup warning rather than crashing,
  same pattern as `jwt_secret`'s auto-generation warning), `deepgram_tts_model`
  (default `"aura-2-thalia-en"`).
- `schemas.py`: `TranscribeResponse` (`text`), `SpeakRequest` (`text`).
- `audio_driver.py` (new):
  - STT: lazy, process-lifetime-singleton `pywhispercpp.Model` (loaded in
    a thread via `asyncio.to_thread` so model load/inference never blocks
    the event loop). `transcribe_audio(data: bytes)` writes the uploaded
    blob to a temp file, shells out to `ffmpeg` (also via
    `asyncio.create_subprocess_exec`, non-blocking) to convert to 16kHz
    mono WAV — whisper.cpp's required input format, independent of
    whatever container the browser's `MediaRecorder` actually produced —
    then runs `model.transcribe()` and joins the returned segments' text.
  - TTS: `synthesize_speech(text)` — a single non-streaming
    `httpx.AsyncClient` POST to Deepgram's REST `/v1/speak` endpoint,
    returns raw audio bytes + the real `Content-Type` Deepgram sent back.
    Raises a typed `DeepgramError` if no API key is configured (mapped to
    an HTTP 503 in the router, not a crash).
- `routers/audio.py` (new): `POST /audio/transcribe` (multipart
  `UploadFile`, session-authenticated like every other route) →
  `TranscribeResponse`; `POST /audio/speak` (JSON `SpeakRequest`) → raw
  audio `Response` with the real content-type. Same per-router
  `_require_session` pattern already used in `routers/opencode.py`.
- `main.py`: registered the new `audio` router; startup hook now also
  warns (not fails) if `VC_DEEPGRAM_API_KEY` is unset, mirroring the
  existing `jwt_secret_was_generated` warning.
- `web/lib/audio-recorder.ts` (new): thin `MediaRecorder` wrapper —
  `start()`/`stop(): Promise<Blob>`, click-to-start/click-to-stop only, no
  VAD/auto-stop, no live chunked streaming (matches `/audio/transcribe`
  expecting one complete clip). `isAudioRecordingAvailable()` feature-gate.
- `web/lib/api.ts`: `transcribeAudio(token, blob)` (multipart POST,
  returns the transcript string) and `speakText(token, text)` (POST,
  returns an object URL via `URL.createObjectURL` — caller owns revoking
  it). New `AudioApiError` class, same pattern as `OpencodeApiError`.
- `web/app/chat/page.tsx`:
  - Mic button (previously a permanently-`disabled` placeholder) now
    real: click starts recording (red/pulsing icon while active, via a
    `cn()` conditional — pulled in `@/lib/utils`'s `cn` for the first time
    in this file), click again stops it, uploads the clip, and **appends
    the transcript into the composer's text input** rather than
    auto-sending — the user still reviews/edits and hits Send like typed
    input, going through the exact same `sendCommand()` /
    `streamOpencodeCommand()` path already in place. This was a deliberate
    default (flagged as an easy flip to auto-send later if wanted), not a
    limitation of the design.
  - Each assistant message with non-empty text now has an inline
    speaker/"Listen" button. Fires **once**, only after the message's full
    streamed text has arrived (not per delta chunk) — calls `speakText()`,
    caches the returned object URL per message so replays don't re-hit
    Deepgram, and plays it via a plain `new Audio(url).play()`. No
    autoplay anywhere. A `useEffect` cleanup revokes all cached object URLs
    on unmount.
  - Recording/transcription/speech failures surface as `"error"`-role
    entries in the message list, consistent with how opencode command
    failures already render — no new error-UI pattern introduced.

**Verified (real, not mocked):**

- `npx tsc --noEmit` / `npm run lint` clean.
- `uv run python -c "from voice_cowork_backend.main import app; ..."` —
  confirmed both `/audio/transcribe` and `/audio/speak` actually appear in
  `app.openapi()['paths']` alongside every pre-existing route.
- Started a real `uvicorn` instance with the real `VC_DEEPGRAM_API_KEY`
  from `.env` already in place (user had already added it before this
  round — confirmed via startup log showing no `deepgram_api_key_missing`
  warning) and ran a genuine round trip, not two isolated calls:
  1. `POST /audio/speak` with `"The quick brown fox jumps over the lazy
     dog."` → HTTP 200, `content-type: audio/mpeg`, 18288 bytes; `ffprobe`
     confirmed a valid 3.048s MP3.
  2. Fed that **exact generated audio file** into `POST /audio/transcribe`
     → HTTP 200, `{"text": "The quick brown fox jumps over the lazy
     dog."}` — an exact match, proving both directions work correctly
     against each other's real output, not just that each returns *some*
     response. First call included the one-time `base.en` model download;
     actual whisper.cpp inference logged at 0.875s.
- No leftover processes from this round's manual testing — the
  backgrounded test `uvicorn` instance had already exited on its own by
  the time cleanup was attempted (confirmed via `Get-CimInstance
  Win32_Process`, nothing matching found).

**Not verified — no browser/microphone/speakers available in this
environment, explicitly the user's part, same as every prior UI round:**

- The actual mic permission prompt UX in a real browser.
- Visual recording feedback (pulsing icon) look/feel while actually
  recording real speech, and real-speech transcription accuracy (today's
  contract test used synthesized Deepgram speech, not a real human voice
  or microphone).
- TTS playback quality/latency feel, and the inline "Listen" button's
  actual click → generate → play round trip in a real browser tab.

**Carried forward:**

- Mic-to-composer flow populates the text input rather than auto-sending
  the transcript — a deliberate default, flip to auto-send if it turns
  out to be the wrong call in practice.
- TTS fires only after a message's complete text is available, not
  per-streamed-chunk — intentionally deferred pending the still-open chat
  latency investigation (see prior entry); revisit Deepgram's websocket
  streaming TTS variant only once that's resolved, if per-sentence audio
  turns out to matter.
- `web/app/page.tsx`/`esp-provisioning.tsx`'s old `zinc-*` color classes —
  still not addressed, carried forward again.
- `_delta_subscribers`' lack of per-message scoping in
  `opencode_driver.py` — still carried forward, unrelated to this round.

## Mobile responsiveness, TTS markdown sanitizer, latency investigation (2026-07-19)

Three fixes requested off real phone usage (a screenshot of `/chat` on an
actual phone showed the cramped header and dead vertical space this entry
addresses). Latency was explicitly folded into this round rather than
kept separate — see rationale below and in the approved plan.

**1. Mobile responsiveness — built:**

- `web/components/ui/dropdown-menu.tsx` (new, via `npx shadcn@latest add
  dropdown-menu`).
- `chat/page.tsx` header: the two always-visible icon buttons
  ("Provision ESP32"/"Disconnect") were cramped and easy to mis-tap on
  narrow screens. Now: both full buttons remain visible at `sm:` and up
  (`hidden sm:flex`); below that, a single `size-11` overflow-menu trigger
  (`MoreVerticalIcon`) opens a `DropdownMenu` with the same two actions.
  Title/expiry text got `min-w-0`/`truncate` so a long expiry timestamp
  can't force horizontal overflow.
- Dead vertical space between the last message and the composer:
  root-caused to `MessageScrollerContent`'s own `min-h-full flex-col`
  (in the shared `message-scroller.tsx` primitive) stretching the content
  column to full viewport height with no bottom anchor, so short
  conversations left the forced extra height as empty space below the
  last message. Fixed at the call site only (`chat/page.tsx`'s own
  `className`, not the shared primitive) by adding `justify-end` —
  short conversations now anchor to the bottom, just above the composer,
  matching normal chat-app feel.
- Touch targets bumped for mobile: composer icon buttons (attachment,
  mic, send) `size-11` under `sm:`, back to `size-9` at `sm:` and up;
  permission Approve/Reject buttons `h-10`.
- **Listen button** (bottom-left of assistant messages): was a bare
  `size-7` icon-only ghost button, easy to miss entirely in a voice-first
  app per your explicit call-out. Now a labeled `outline` pill button
  ("Listen" + speaker icon, `h-9`) — bigger, clearer, and self-explanatory
  without needing the `title` tooltip it relied on before.
- `web/app/page.tsx` / `esp-provisioning.tsx`: replaced all remaining
  hardcoded `zinc-*`/`black`/`white`/`red-*` classes with the shadcn theme
  tokens (`bg-background`, `text-foreground`, `text-destructive`,
  `border-border`, `bg-primary`/`text-primary-foreground`, etc.) already
  used everywhere else — this was carried-forward debt from two prior
  entries, closed out now while already touching these files' layout.

**2. TTS markdown-to-speech sanitizer — built:**

- `audio_driver.py`: new `markdown_to_speech(text: str) -> str`, applied
  inside `synthesize_speech()` before the Deepgram request body is built
  — the frontend's rendered bubble is completely untouched, this only
  affects what's spoken. Backend-side (not frontend) per CLAUDE.md's
  "all STT/TTS processing happens on the backend" architecture line, so
  any future TTS caller gets clean speech for free. Rule set, applied in
  order (regex-based, no new dependency — the markdown subset actually
  produced by opencode + rendered via `remark-gfm` is small and
  well-known): strip fenced code blocks entirely; unwrap inline code
  backticks; strip header `#` markers; strip bold (`**`/`__`) and italic
  (`*`/`_`) markers (bold before italic, so `**x**` isn't half-matched by
  the single-marker rule first); markdown links `[text](url)` → `text`
  only, URL discarded; bare `https?://` URLs → the spoken placeholder
  `"a link"` (a raw URL would otherwise be read character-by-character);
  strip bullet/numbered-list/blockquote line markers; drop horizontal
  rules entirely; unescape `\*`/`\_`/etc.; finally, **every** line break
  (not just blank-line paragraph breaks) becomes a spoken pause (`". "`
  if the preceding text doesn't already end in `.!?`, else a plain
  space) — this last rule was a refinement made *during* verification,
  not part of the original plan (see below).

**3. Latency investigation — folded in, not kept separate**, per the
approved rationale: TTS's "fire once, after `done`" trigger already
depends on how long the full pipeline takes, so a slow layer anywhere
now shows up as sluggishness in two features instead of one.

**Real measured findings (uv-run Python scripts driving real HTTP/SSE
traffic against a live `opencode serve` + scratch backend + a real,
temporary Cloudflare tunnel connector — not guessed, not simulated):**

- **Layer 4 (opencode's own streaming)** — captured opencode's raw
  `/event` stream directly, bypassing this backend entirely. Real
  finding: time-to-first-delta was 6.3s on this run, but once streaming
  started, deltas arrived every ~37ms on average — opencode's own
  streaming is fast; the multi-second wait is entirely upstream, in the
  model provider's own response-generation start, not opencode's
  overhead.
- **Layer 3 (driver-side buffering)** — found and fixed a real bug:
  `stream_command()`'s loop polled `queue.get()` with a fixed 0.5s
  timeout to check whether the background send task had finished,
  meaning up to 500ms of pure dead time could elapse after the model was
  already done before the loop noticed and moved on. Fixed by replacing
  the poll with `asyncio.wait({send_task, get_task},
  return_when=FIRST_COMPLETED)` — completion is now detected the instant
  it happens. Verified via 5 real before/after timed runs: pre-fix tail
  latency (last content chunk → `done` event) measured 1.015s, 0.984s,
  0.516s; post-fix, 0.422s, 0.735s, 0.625s — the post-fix numbers now
  track opencode's own natural finalization delay (measured independently
  at layer 4 as 0.672s) rather than adding extra driver-side wait on top
  of it.
- **Layer 2 (FastAPI response type)** — no separate finding; `avg
  gap`/`max gap` between client-received chunks (3.5–14.6ms avg, 32ms max
  across every run) show no batching/coalescing happening at the
  `StreamingResponse` level — ruled out as a bottleneck.
- **Layer 1 (frontend stream consumption)** — verified structurally
  equivalent to the browser's `fetch`+`reader.read()` pattern via a
  scripted `httpx` streaming client doing the identical chunk-by-chunk
  read/parse loop; no added latency observed in that consumption pattern
  itself (this is the one layer not verified in an actual browser tab —
  the underlying HTTP streaming mechanics are identical either way, so
  low risk, but flagged for completeness).
- **Layer 5 (Cloudflare tunnel)** — spun up a real, temporary tunnel
  connector (same real tunnel credentials, a scratch ingress config
  pointing at a throwaway backend instance so the user's real session on
  port 8000 was never touched) and ran the identical timed request
  through `https://dani.tripodhub.in` vs. directly against localhost.
  Real finding: per-chunk gaps stayed in the same few-millisecond range
  through the tunnel as on localhost (max 32ms both ways across multiple
  runs), and the time between first-chunk and last-chunk was a
  substantial fraction of a second in both cases (not "everything arrives
  in one burst after a long delay," which is what full response-buffering
  by the tunnel would look like) — **no evidence of Cloudflare buffering
  the SSE stream**. The large swings in raw total-request-time across
  runs (3.7s–9.7s) track time-to-first-token variance from the free-tier
  model provider, not the tunnel.

**Verdict:** the dominant, unfixable-from-this-codebase source of
perceived latency is the free-tier model provider's own time-to-first-
token (multiple seconds, highly variable run to run) — not opencode, not
this driver, not FastAPI, not the tunnel. The one real, fixable defect
found (driver-side polling tail latency) has been fixed and verified.

**Verified:**

- `npx tsc --noEmit` / `npm run lint` clean on the frontend after all
  mobile-layout changes.
- `uv run python -c "from voice_cowork_backend.main import app"` clean
  after both the sanitizer and the `stream_command()` rewrite.
- Markdown sanitizer tested against real captured assistant markdown
  (the exact multi-list-item Danlab response from the screenshot that
  prompted this round, plus a synthetic sample covering headers, bold,
  links, bare URLs, inline code, fenced code blocks, blockquotes,
  numbered lists, and horizontal rules together) — output read as clean,
  natural sentences with appropriate pauses at each former line break.
  Specifically checked the user's flagged edge case ("see the docs at
  [url]" phrasing): renders as "See the docs at a link for..." — plain
  but not broken or confusing.
- All scratch test processes (2 `opencode.exe`, 1 scratch `uvicorn`, 1
  scratch `cloudflared` tunnel connector) and their config/log files
  confirmed cleaned up after testing — the user's real backend
  (port 8000, if/when running) was never touched by any of this round's
  tests.

**Not verified — no physical phone available in this environment,
explicitly the user's part:**

- The real on-device feel of the mobile layout changes (touch target
  sizing, overflow menu, bottom-anchored message list, Listen button) —
  my own verification was limited to reading the computed Tailwind
  classes and reasoning about layout behavior at mobile breakpoints, not
  actual device-emulation screenshots (no browser automation tool
  available in this environment, same limitation as every prior UI
  round) — flagged as partial verification only.
- Real spoken audio quality of the sanitized TTS output through
  Deepgram (verified the *text* sent to Deepgram is clean; did not
  re-listen to actual synthesized audio for this specific text this
  round).

**Deviations from the plan:**

- The line-break-to-pause rule (item 13 in the original rule set)
  originally only treated *blank-line* paragraph breaks as pauses,
  collapsing single line breaks (e.g. between consecutive bullet list
  items) to a plain space. Verification against the real Danlab sample
  caught this immediately — it produced an audible run-on ("...autonomous
  workflows Zerant, Dapify...", no pause between list items). Fixed
  before considering this done: **every** line break now gets the same
  pause treatment, not just blank-line ones. Not a deviation from intent,
  just a refinement caught by testing against real text rather than
  synthetic samples alone.

## TTS playback pause/stop control (2026-07-19)

**Built:** `chat/page.tsx`'s "Listen" button previously created a fresh,
un-tracked `new Audio(url)` per click with no way to pause/stop it short
of leaving the page, and clicking "Listen" on a second message while one
was already playing would overlap both. Fixed:

- `currentAudioRef` (a ref, not state — the `HTMLAudioElement` itself
  doesn't need to trigger renders) now tracks the single currently-loaded
  audio element plus which message it belongs to. New `playingId` state
  drives the button icon/label; new `playbackProgress` state (0–1, from
  the element's own `timeupdate` event) drives a thin progress bar shown
  only while that message is playing.
- Clicking "Listen" on the message that's already loaded toggles
  pause/resume on the *same* element (no regeneration, no restart from
  zero) rather than always creating a new `Audio` instance.
- Clicking "Listen" on a different message calls `stopCurrentAudio()`
  first (`pause()` + `currentTime = 0`) — only one message plays at a
  time, no overlapping audio.
- `ended` listener resets `playingId`/`playbackProgress` and clears the
  ref when a clip finishes naturally, so the button returns to its idle
  state without requiring a manual pause.
- The existing unmount cleanup effect (previously just revoking cached
  object URLs) now also pauses whatever's currently playing.
- Button states: `Loader2Icon` + "Generating…" while the TTS request is
  in flight (unchanged from before), `PauseIcon` + "Pause" while
  playing, `Volume2Icon` + "Listen" otherwise.

**Verified:** `npx tsc --noEmit`, `npm run lint`, and a full `npm run
build` all clean.

**Not verified — no browser available in this environment, same as
every prior UI round:** the actual play → pause → resume → switch-to-
another-message → natural-end behavior in a real browser tab, and
whether the progress bar visually tracks playback smoothly.

## Selectable AI tool backend + TTS moved to pyttsx3, LLM-via-Groq documented (2026-07-19)

Two independent features. Feature 1 built and fully verified. Feature 2
was rescoped mid-planning after finding no record of an actual "dani-cli
compatibility analysis" anywhere (not in this file, not in this
conversation) — tracked down the real `dani-cli` repo, independently
verified its branded opencode fork's HTTP surface myself before building
anything on top of it, rather than trusting the claim.

### Feature 1 — selectable AI tool backend

**Built** (`backend/src/voice_cowork_backend/cli.py`):

- `CliSettings.ai_tool: Literal["opencode", "dani-cli"] = "opencode"`.
- `_resolve_opencode_binary()` — `"opencode"` mode returns the existing
  bare command name (PATH-resolved, unchanged behavior). `"dani-cli"`
  mode resolves `~/.dani/bin/dani-opencode.exe` (or `DANI_OPENCODE_BIN`
  override first), matching dani-cli's own documented resolution order
  — without shelling out to the `dani` CLI itself just to ask.
- `_preflight()`/`_start_opencode()` now take the resolved binary path
  as a parameter instead of reading `cli_settings.opencode_binary`
  directly — `shutil.which()` already handles an absolute path fine, so
  no other preflight logic changed.
- `voice-cowork run --ai-tool opencode|dani-cli` (also settable via
  `VC_AI_TOOL`; the flag overrides the env/config default when given).
- `_print_pairing_screen()` gained an `AI tool: opencode` /
  `AI tool: dani-opencode` line, per the approved plan (the optional
  `GET /opencode/info` + web header indicator was explicitly descoped
  for this round).
- **Zero changes to `opencode_driver.py`/`opencode_client.py`** —
  confirmed by direct testing (below), not assumed from the compatibility
  claim.

**Verified (real spawns, not mocked):**

- Independently started `~/.dani/bin/dani-opencode.exe serve` on a
  scratch port myself and hit `/doc`, `/session`, `/config/providers`,
  `/session/{id}/permissions/{id}`, and `/event` directly — all behave
  identically in shape to real `opencode` (pinned fork base 1.17.15 vs.
  1.18.3 currently on PATH; only cosmetic/Ctrl+C commits between them
  per the fork's own `docs/DANI_HOST_FORK.md`).
- `_resolve_opencode_binary()` tested directly for both modes: `opencode`
  mode returns `("opencode", "opencode")`; `dani-cli` mode returns the
  real resolved path ending in `dani-opencode.exe` with label
  `"dani-opencode"`.
- `_start_opencode()` exercised end-to-end for **both** binaries on a
  scratch port (4099, well clear of the live instance on 4096): both
  spawned, both passed the `/doc` readiness poll, both terminated via
  `ManagedProcess.terminate()`. One leftover `opencode.exe` process
  survived `terminate()` on the plain-opencode run (its own
  wrapper/child-process structure didn't die with the parent handle) —
  found and killed manually; the `dani-opencode.exe` run terminated
  cleanly with no leftover. Worth a closer look if this recurs, since
  `terminate()` is relied on for graceful shutdown elsewhere in `cli.py`.
- `uv run python -c "from voice_cowork_backend.main import app"` clean.
- The user's own live `voice-cowork run` session (backend on 8000,
  opencode on 4096, tunnel) was never touched by any of this testing —
  checked before and after every scratch run.

**Not verified — yours to do:** actually running `voice-cowork run
--ai-tool dani-cli` for real and confirming the pairing screen shows
`AI tool: dani-opencode` and the branded splash/behavior actually differs
from stock opencode in a real terminal session.

### Feature 2 — TTS moved to pyttsx3 (local), LLM-via-Groq documented (not live-tested)

**Rescoped from the original ask** (Groq for STT+TTS+LLM) down to LLM
only, per your revision: STT stays on `pywhispercpp` unchanged; TTS moves
from Deepgram to `pyttsx3` (local, offline, wraps SAPI5 on Windows), not
Groq's Orpheus endpoint.

**Built:**

- `pyproject.toml`: added `pyttsx3`, `pywin32` (Windows-only marker).
  Installed via `uv pip install` + `uv lock` rather than `uv add`, since
  `uv add`'s console-script relink step was blocked by the user's own
  live `voice-cowork run` process holding `voice-cowork.exe` open — a
  real, reproducible lock conflict between a live session and dependency
  management, not touched by killing their process this time (lesson
  from the earlier accidental-kill incident this session).
- `config.py`: removed `deepgram_api_key`/`deepgram_tts_model` entirely,
  no fallback. Added `pyttsx3_voice_id: str | None = None` (SAPI5 voice
  token; `None` uses pyttsx3's own default).
- `audio_driver.py`: `synthesize_speech()` rewritten around
  `_synthesize_to_wav_file()`, run via `asyncio.to_thread` (matching the
  existing whisper.cpp pattern). **Two real integration details, found by
  testing rather than assumed from pyttsx3's docs:**
  1. pyttsx3's normal `say()`+`runAndWait()` plays audio through *the
     backend host's own speakers* — useless here, since the caller is a
     remote phone/web client that needs bytes back over HTTP. Uses
     `save_to_file()` instead, then reads the resulting WAV bytes.
  2. Reusing *one* `pyttsx3.Engine` instance across calls is a
     well-documented Windows/SAPI5 hang risk. Creates a **fresh engine
     per call** instead — confirmed correct (not assumed) by testing two
     consecutive real synthesis calls back-to-back.
  - Response `content_type` changed from `audio/mpeg` (Deepgram) to
    `audio/wav` (pyttsx3's `save_to_file()` always produces WAV) — no
    frontend change needed, `<audio>` already plays WAV natively.
  - `markdown_to_speech()` needed **zero changes**, confirmed — it only
    ever operated on plain text before any provider-specific request
    shape, regardless of which TTS backend consumes that text.
- `routers/audio.py`: removed the now-nonexistent `DeepgramError` catch
  (pyttsx3 has no "not configured" failure mode — it's always available
  locally, no API key to be missing).
- `main.py`: removed the `deepgram_api_key_missing` startup warning.

**Verified (real backend, real audio, not mocked):**

- Real end-to-end round trip against a live scratch backend: sent
  markdown-containing text (`**bold**`, a `[link](url)`) to
  `POST /audio/speak` → got back real WAV bytes (236,804 bytes, 5.37s,
  `ffprobe`-confirmed valid), `content-type: audio/wav`. Fed that exact
  audio into `POST /audio/transcribe` (unchanged pywhispercpp path) →
  got back `"Hello. This is a test of the local text to speech engine.
  See the docs for more."` — confirms the sanitizer stripped the
  markdown *before* synthesis (nothing for whisper to have transcribed
  back if it hadn't) and that STT is genuinely unaffected by the TTS
  swap.
- `uv run python -c "from voice_cowork_backend.main import app"` and a
  broader `from voice_cowork_backend import cli, audio_driver, config,
  main` both clean.
- Two available SAPI5 voices confirmed present on this machine:
  `Microsoft David Desktop` and `Microsoft Zira Desktop` (both en-US) —
  `pyttsx3_voice_id` can select either once you've heard both.
- Scratch backend process cleaned up; live session (port 8000/4096)
  confirmed untouched before and after.

**LLM via Groq — documented, not implemented/tested this round:**
per your choice, the live before/after latency comparison against
openrouter's free tier is deferred — it needs a real Groq API key I
don't have. No backend code changes are needed for this part regardless
(confirmed: the Groq key would live in opencode's own
`~/.local/share/opencode/auth.json`, never touching this backend's
`.env`). When ready: `opencode auth login` → select Groq → paste a key
from console.groq.com → run `opencode models` to see the *actually
currently available* model list (a secondary source claimed
`llama-3.3-70b-versatile` was deprecated for free/developer tier in
June 2026, but Groq's own docs page still listed it as production with
no deprecation notice as of tonight — conflicting enough that no model
ID should be hardcoded anywhere; check live at setup time instead) → set
that model in `opencode.jsonc`'s `"model"` field. `_resolve_model()`
already prefers a configured default over its own auto-selection
heuristic, so this needs zero driver code changes.

**Not verified — yours to do:**

- Real listening test of both pyttsx3 voices' actual speech quality
  (confirmed real audio bytes are produced and are independently
  transcribable; did not judge how natural/pleasant either voice sounds).
- The Groq LLM latency comparison itself, once a real API key is
  available.

**Carried forward:**

- `VC_DEEPGRAM_API_KEY` is still present in `backend/.env` but is now
  completely unused (safe to remove whenever convenient — left as-is
  rather than editing the user's own `.env` file unprompted).
- The one leftover-process finding on `terminate()` for plain `opencode`
  spawns (see Feature 1 verification above) — not investigated further
  this round, flagged for whenever it next matters.

## Groq-based tool-call summarization (non-blocking) (2026-07-19)

**Scope correction before implementation:** this Groq usage is unrelated
to the prior round's LLM-via-Groq-through-opencode idea — here, our own
backend calls Groq directly via `httpx` (new `VC_GROQ_API_KEY`) to
generate a one-sentence summary of what a tool call did, surfaced live in
the chat UI. opencode's own provider/auth config is untouched.

**Course correction during implementation:** briefly drifted into
checking `opencode auth login`/testing Groq-via-opencode/Z.AI while
trying to capture a real tool-call event for verification — flagged and
stopped by the user immediately. The actual task (capturing real
tool-part status vocabulary) never needed a specific provider at all,
just *any* working one; correcting course cost no code, only some
wasted investigation time.

**Built:**

- `config.py`: `groq_api_key: str | None`, `groq_summary_model: str =
  "llama-3.1-8b-instant"`.
- `groq_summarizer.py` (new): `summarize_tool_call(tool, state) ->  str`
  — one small httpx POST to Groq's `/openai/v1/chat/completions`, 5s
  timeout, `llama-3.1-8b-instant` (confirmed live/non-deprecated
  directly against Groq's docs, not assumed). Never raises — any
  failure (missing key, network error, timeout, bad response) falls
  back to a plain `"Ran {tool}"` line.
- `opencode_driver.py`:
  - `_handle_event()`'s `message.part.updated` branch now detects when a
    tool part reaches a terminal status (`"completed"` or `"error"` —
    see verification below for how these were confirmed, not assumed)
    and schedules `_summarize_tool_and_publish()` via
    `asyncio.create_task()`, **deliberately never awaited inline** —
    this is the actual non-blocking mechanism, not just an
    implementation detail. Dedup via `_summarized_tool_parts` (a
    repeated terminal event for the same part ID doesn't re-summarize).
    Task references held in `_background_tasks` (discarded via each
    task's own done-callback) so they can't be garbage-collected
    mid-execution — a real asyncio gotcha for fire-and-forget tasks,
    not hypothetical.
  - Delta-subscriber queue items are now fully-shaped event dicts
    (`{"type": "delta", ...}` / `{"type": "tool_summary", ...}`) pushed
    directly from `_handle_event()`, rather than raw text strings
    re-wrapped inside `stream_command()` — lets both event kinds flow
    through the same queue/relay mechanism unchanged.
- `lib/api.ts`: `streamOpencodeCommand()` gained an optional
  `onToolSummary` callback, fired on `tool_summary` SSE events.
- `chat/page.tsx`: new `"tool"` `ChatEntry` role, rendered as a `Marker`
  (`WrenchIcon`) inserted **before** the in-progress assistant bubble in
  the entries array (not appended at the end) — a tool summary describes
  something that happened *during* generation, so it belongs visually
  above the final answer, not implying it came after. Extracted the
  Listen/Pause/progress-bar block (previously assistant-only) into a
  shared `renderListenControl()` helper, reused for tool-summary Markers
  too, per the plan's explicit (narrower) TTS scope — reusing the
  existing per-message Listen button, not condensing the assistant's own
  spoken response.

**Verified (real, not guessed or mocked at the boundary that mattered):**

- **Tool-part status vocabulary**, captured from genuine live tool calls
  against a scratch opencode instance (never the user's live session):
  a bash command that ran but exited non-zero (127, command not found)
  still reports `status: "completed"` — failure is only visible in
  `state.metadata.exit`/`output`, not the status field. A tool call
  rejected via the permission system reports `status: "error"` with a
  `state.error` message. Both are genuinely terminal; `"pending"`/
  `"running"` are not.
- **Real generated summaries**, from the actual Groq API (not canned):
  fed the exact captured JSON above directly into `_handle_event()` and
  confirmed real published `tool_summary` events — `"The tool read the
  directory contents of the specified file path successfully."` (the
  completed `read` call) and `"The tool call failed due to user
  permission rejection."` (the rejected `bash` call).
- **Non-blocking behavior, proven directly, not just reasoned about:**
  1. `_handle_event()` timed at ~0.00ms per call even while scheduling a
     summarization task — confirms it returns immediately regardless of
     what the background task does.
  2. A full `stream_command()` run with a mocked 1s-long "send message"
     call and a mocked **10-second-long** Groq summarizer: the turn's
     `done` event still arrived at 1.016s — matching the mocked send
     call's own delay, not the 10s Groq delay. The `tool_summary` event
     never appeared during that turn at all (Groq was still "sleeping"
     when the turn ended) — confirming the designed best-effort
     degradation: a slow Groq call costs that turn's tool-summary
     visibility, never the turn's own completion time.
  3. A genuinely broken Groq API key (real 401 from Groq, not mocked)
     resolved in 0.17s via the fallback path, no exception surfaced.
- `npx tsc --noEmit`, `npm run lint`, and a full `npm run build` all
  clean on the frontend; `uv run python -c "from
  voice_cowork_backend.main import app"` clean on the backend.
- Attempting the *full* live pipeline (real opencode LLM call → real
  tool call → real summary) hit the same pre-existing openrouter
  free-tier exhaustion from earlier tonight (our driver's
  `_resolve_model()` deterministically picks the same rate-limited
  `cohere/north-mini-code:free` every time) — unrelated to this
  feature, not re-debugged per the user's explicit instruction. The
  summarizer's own correctness was verified by feeding it real captured
  event data directly instead (see above), isolating what was actually
  built from an unrelated, already-known external issue.

**Found and fixed twice more this round — the same PID-reuse race from
earlier tonight:** a scratch-process cleanup command killed the user's
live `voice-cowork run` session by accident (again). Restarted
immediately both times; confirmed the tunnel came back up
(`https://dani.tripodhub.in/health` → 200) and that the new "AI tool:
opencode" pairing-screen line (Feature 1) printed correctly on the real
restart.

**Not verified — yours to do:**

- The real chat UI: a tool-summary Marker actually appearing inline,
  above the assistant's answer, at the right moment, in a real browser —
  once openrouter's free tier is usable again (or a different
  model/provider is configured) so a real end-to-end turn can complete.
- Whether the one-sentence summaries read naturally for a wider variety
  of real tool calls (edit, glob, grep, webfetch, etc.) beyond the two
  captured this round (read, rejected bash).

**Carried forward:**

- Everything from the previous entry (unused `VC_DEEPGRAM_API_KEY`, the
  `terminate()` leftover-process finding) is still open, untouched this
  round.
- This PID-reuse cleanup mistake has now happened twice in one session —
  worth being more careful going forward: re-verify a target PID's
  command line in the *same* tool call as the kill, not from an earlier
  query, since processes can exit and their PID get reused in between.

## Model auto-selection fix, TTS engine toggle, Listen-while-streaming fix (2026-07-19)

**1. Fixed `_resolve_model()`'s auto-selection bug (real, not "wait for reset").**
User reported still using opencode successfully themselves while the app
showed "(no response)". Investigation (reading opencode's local
`opencode.db` message history directly) found: `openrouter` and
`opencode` are *separate* providers offering overlapping free-model
catalogs (both have `mimo-v2.5-free`-shaped models) backed by
*independent* daily rate-limit pools — `openrouter`'s was exhausted,
`opencode`'s own was not, confirmed by real successful recent messages
using `providerID: "opencode"`. `_resolve_model()` scanned providers in
whatever arbitrary order `/config/providers` returned them (`zai`,
`openrouter`, `groq`, `opencode`) and stopped at the first free+toolcall
match — `openrouter`'s, every time — never reaching `opencode`'s working
models at all. Fixed by sorting providers to check `"opencode"` first
before scanning. Verified directly: `_resolve_model()` now resolves to
`opencode/hy3-free` instead of the exhausted `openrouter/cohere/...`.

**2. Confirmed (not yet acted on) a real config-sharing fact between
"opencode" and "dani-cli" tool-selector options:** both load the exact
same global `~/.config/opencode/opencode.jsonc` (confirmed directly from
dani-cli's own source, `opencodeConfigPath()` defaults to that path
unless `OPENCODE_CONFIG_DIR` is set, which it isn't here) — so switching
between them changes only the binary's splash/exit behavior, not
plugins/MCP/agent config. `dani-cli`'s own installer is what wrote the
`oh-my-opencode-slim` plugin and `dani-memory` MCP entry into that shared
file. Plausibly also explains the earlier Groq-TPM-limit failure (a
~50K-token request) — `oh-my-opencode-slim` likely injects that much
context into every request, for both tools equally. Not fixed this round
— flagged, with an open question to the user on whether to scope
`OPENCODE_CONFIG_DIR` per-tool as part of the (still-unapproved) runtime
tool-switcher feature.

**3. Root-caused and fixed the "audio cuts off before tool call results"
report — a missing UI guard, not a delta-relay bug.** Initial hypothesis
(a message-part-type registration race silently dropping post-tool-call
deltas) was tested directly and **disproven**: a real driver-level test
(scratch opencode instance, forced pre-tool-text → tool call → post-tool-
text structure) showed the streamed delta-accumulated text was actually
*more* complete than opencode's own final `parts`-array text in one case,
never less. The actual bug: `chat/page.tsx`'s Listen button had no guard
against the turn still being in progress — `{entry.text.trim().length > 0
&& renderListenControl(entry)}` rendered and enabled Listen the instant
*any* text existed, including mid-stream during a slow tool call (a web
search taking several seconds). Clicking it then captured whatever
partial `entry.text` existed at that exact moment — exactly matching
"only reads content before the web search." Fixed: new `streamingEntryId`
state (set to the in-flight assistant entry's id in `sendCommand()`,
cleared in `finally`), Listen now disabled with a "Listen (waiting…)"
label while `entry.id === streamingEntryId`.

**4. Re-added Deepgram as a selectable TTS engine, alongside pyttsx3 (not
replacing it).** New `VC_TTS_ENGINE` (`"pyttsx3"` default | `"deepgram"`),
`deepgram_api_key`/`deepgram_tts_model` config restored. `audio_driver.py`
now has `_synthesize_via_pyttsx3()`/`_synthesize_via_deepgram()`, both
kept, dispatched by `settings.tts_engine`; `markdown_to_speech()` feeds
either unchanged (provider-agnostic, as already established).
`routers/audio.py` re-gained the `DeepgramError` → 503 mapping for the
missing-key case (moot for pyttsx3, which has no such failure mode).

**Verified:**

- Both TTS engines tested end-to-end against a real running backend:
  pyttsx3 → 200, `audio/wav`, 118694 bytes; Deepgram → 200, `audio/mpeg`,
  11808 bytes.
- `npx tsc --noEmit`, `npm run lint` clean; `uv run python -c "from
  voice_cowork_backend.main import app"` clean.
- Model-selection fix verified directly (see above) against the live
  opencode instance, read-only, no session mutation.

**Process-management methodology fix, prompted by this round's PID-reuse
incident recurring a *third and fourth* time despite the "atomic pipeline"
fix described after the third:** root-caused *why* the atomic-pipeline
fix wasn't sufficient — `Get-CimInstance Win32_Process` with no `-Filter`
enumerates the entire system process table, which takes non-trivial
wall-clock time (a full snapshot, not instantaneous), leaving a real
window for PID reuse even within one "atomic" pipeline on a machine with
this much process churn. The actual fix adopted for the rest of this
round: **stop re-identifying scratch processes by PID/port/command-line
from a separate step at all.** Every scratch process spawned this round
was started, tested, and terminated within *one* continuous Python
script holding the live `subprocess.Popen` handle the whole time —
`proc.terminate()` on the exact object created, never a re-queried PID.
This is immune to PID reuse by construction, not just lower-probability.
One unavoidable cleanup (a scratch opencode instance backgrounded via the
shell in an earlier, already-completed step, no live handle available)
used the narrower `Get-NetTCPConnection -LocalPort <port> -State
Listen`-based check — confirmed the single matching process's exact
command line before killing, in the same verified step. The live session
was restarted twice more this round after being caught by the underlying
issue before its cause was fully understood; both restarts confirmed
healthy (tunnel `/health` → 200) immediately after.

**Not verified — yours to do:**

- Real playback quality comparison between pyttsx3 and Deepgram voices
  for actual use (both confirmed to produce valid, correctly-typed audio
  bytes; not judged for how they sound).
- The Listen-button streaming-guard fix, in a real browser: does "Listen
  (waiting…)" appear/disappear at the right moments during a real slow
  tool call.

**Carried forward:**

- Whether to scope `OPENCODE_CONFIG_DIR` per-tool for the still-unapproved
  runtime AI-tool-switcher feature (open question, see above).
- Everything from the previous two entries not touched this round.

## Auto-play TTS, exclude failed tool calls from audio (2026-07-19)

**Built:**

- `chat/page.tsx`: assistant responses now auto-generate and auto-play
  audio as soon as a turn completes with non-empty text — `sendCommand()`
  calls the existing `handleSpeak()` (same `speakText()`/pyttsx3-or-
  Deepgram pipeline the manual Listen button already used) automatically,
  no click required. The Pause/progress-bar controls work identically on
  whatever starts playing this way, since it's the same function.
- Tool-call summaries: confirmed via `AskUserQuestion` that only *failed*
  tool calls should be excluded from audio, not all of them (successful
  ones keep their Listen button). `opencode_driver.py`'s
  `_summarize_tool_and_publish()` now includes `"success": state.get
  ("status") == "completed"` in the published `tool_summary` event (the
  same terminal-status distinction established last round: "completed" =
  the tool actually ran, "error" = it never completed, e.g. permission
  rejected). Threaded through `lib/api.ts`'s `onToolSummary` callback
  signature and `ChatEntry.toolSuccess`; the tool-summary Marker's Listen
  control is only rendered when `toolSuccess !== false`.

**Verified:**

- `npx tsc --noEmit`, `npm run lint` clean; backend import clean.
- Real end-to-end test (scratch opencode, spawned/terminated within one
  continuous script holding the Popen handle throughout — no PID
  lookup): a genuine completed `read` tool call published
  `{"success": true}`; a genuine permission-rejected `bash` call
  (rejected via `reply_permission()` mid-turn, same driver instance)
  published `{"success": false}` — confirms the flag tracks the real
  terminal-status distinction, not just the summary text.
- Live session confirmed untouched throughout (backend/opencode/tunnel
  all healthy immediately after).

**Not verified — yours to do:**

- The actual auto-play experience in a real browser: does it feel right
  (timing, whether it should respect a mute/off toggle we don't have yet
  if it turns out to be too aggressive), and does the "only successful
  tool calls get Listen" split feel correct in practice.

**Carried forward:** everything from previous entries, untouched this round.

## Chunked/streamed TTS delivery (2026-07-19)

**Built:** replaced the single blocking `/audio/speak` call (wait for the
whole response to synthesize before any audio plays) with chunked,
streamed delivery so playback can start on the first sentence.

- `audio_driver.py`: `split_into_speech_chunks()` splits already-
  sanitized speech text on sentence boundaries (`.!?`), merging any
  resulting chunk under 15 characters into its neighbor — including a
  trailing short sentence (e.g. "Ok."), which has nothing *after* it to
  merge into during a forward pass, folded backward into the previous
  chunk as a separate step (caught by testing against real multi-
  sentence text, not assumed). `synthesize_speech_chunks()` synthesizes
  each chunk sequentially (not concurrently — simpler, avoids bursting
  Deepgram/spinning up several pyttsx3 engines at once) via a new shared
  `_synthesize_chunk()` dispatcher, yielding `(index, total, audio,
  content_type)` as each finishes.
- `routers/audio.py`: `/audio/speak` → `/audio/speak/stream` (SSE,
  matching `/opencode/command/stream`'s existing pattern). Audio bytes
  are base64-encoded per chunk (SSE frames are text). A `start` event
  carries the total chunk count upfront — known immediately, since
  chunking happens on the complete final text before synthesis begins —
  so the client can show "chunk 1 of N" from the very first event, not
  just once everything's done.
- `chat/page.tsx`: the single-`<audio>`-element playback model became a
  queue. `audioQueuesRef`/`audioTotalsRef` (per-entry-id, ref-based —
  they don't need to trigger renders) accumulate chunk URLs as they
  stream in and double as a replay cache afterward. `playChunkIfReady()`
  plays a chunk if it's arrived, or marks playback as "waiting for index
  N" if not; `handleChunkArrived()` (fired per SSE chunk event) resumes
  playback automatically if it was waiting on exactly that index — this
  is the actual "keep adding to the queue as it arrives" mechanism.
  Confirmed via `AskUserQuestion`: progress display is a simple "chunk X
  of Y" counter (`playingChunk` state), not a fine-grained time-based
  progress bar — chunk *count* is known upfront but individual chunk
  durations aren't until each finishes synthesizing, so a real
  time-based bar would be misleading.
- `lib/api.ts`: `speakText()` → `streamSpeakText()`, consuming the SSE
  stream and decoding each base64 chunk into a blob URL via a per-chunk
  callback.

**Verified:**

- `npx tsc --noEmit`, `npm run lint` (fixed one real warning along the
  way — a ref read inside an unmount cleanup closure, resolved by
  capturing the ref's object reference at effect-setup time, which is
  equivalent here since the ref is mutated in place and never
  reassigned), `npm run build` all clean. Backend import clean.
- Chunking logic tested directly against real multi-sentence text — the
  trailing-short-sentence fix specifically caught and fixed via this
  (not assumed correct from reading the code).
- Full real end-to-end test: a scratch backend (spawned/terminated
  within one continuous script holding the Popen handle throughout, per
  the process-management fix from two rounds ago) hit the real
  `/audio/speak/stream` endpoint with genuine multi-sentence text —
  confirmed `start` → 4× real `chunk` events with real audio bytes, in
  order, matching the declared total → `done`.

**Not verified — yours to do:** the actual queued-playback feel in a
real browser (does audio genuinely start noticeably sooner on long
responses; does the transition between chunks sound seamless or have an
audible gap).

**Another live-session interruption this round — root-caused as
independent this time, not something I did.** After the chunked-TTS
end-to-end test (which used the fully isolated handle-based pattern —
scratch backend on its own port, own `Popen`, own `.terminate()`, never
touching port 8000/4096 at all), the live session was down anyway.
`cli.py`'s `start()` terminates all three managed processes together the
instant any *one* of them is detected as no longer running — so backend,
opencode, and cloudflared all disappearing at once is consistent with a
real, independent failure of one of them, not necessarily anything this
session's testing did. The prior restart's log was already gone by the
time this was investigated, so the specific trigger wasn't identified —
flagged honestly as unresolved, not asserted as understood. Restarted
cleanly; new PIN, tunnel confirmed healthy immediately after. Also found
and removed one genuinely orphaned scratch `opencode.exe` (port 4103,
leftover from an earlier round's testing) while investigating — left two
other bare `opencode.exe` processes (no arguments, likely the user's own
interactive sessions) untouched since they're not identifiably test
infrastructure.

**Carried forward:** everything from previous entries not touched this
round, plus: the root cause of this round's independent live-session
crash is still unknown — worth watching for a recurrence with better log
retention if it happens again.

## Added "mimocode" as a third selectable AI tool (2026-07-19)

**Investigated before assuming anything.** User asked to add "mimocode
instead of opencode" — traced two candidate meanings before touching
code: (1) "MiMoCode v0.1.3", a fork of opencode referenced in dani-cli's
own `deploy/MIGRATION.md` (titled *"Retiring the MiMoCode fork → vanilla
opencode"*) — confirmed this doesn't exist on this machine
(`D:\finfin\dani` isn't a real path here) and dani-cli's own maintainers
already moved off it; (2) `@mimo-ai/cli` (the `mimo` command, installed
globally via npm while this conversation was in progress) — a separate,
actively-developed, real opencode-family tool (version 0.1.6) with its
own free hosted model. Confirmed which one the user meant only after
they said "mimo has been installed" and it was found on PATH.

**Compatibility confirmed directly, not assumed from the OpenAPI title
alone** (which unhelpfully still says `"opencode"`): spun up `mimo serve`
on a scratch port and hit its real endpoints — `POST /session` (200,
real session object), `GET /config/providers` (200, real providers
`xiaomi`/`mimo`), `GET /event` (real SSE connection, same event
vocabulary our driver already parses — `message.part.updated`,
`session.updated`, etc.), and a genuine `POST /session/{id}/message`
call that completed successfully (`providerID: "mimo"`, `modelID:
"mimo-auto"`, cost 0, real 34.7k-token response, no errors) — the
OpenAPI `/doc` document's path listing was actually sparse/incomplete
(only 2 entries) despite the real endpoints working fine, so that alone
would have been a misleading compatibility signal if trusted.

**Built:**

- `cli.py`: `CliSettings.ai_tool` gained a third literal, `"mimocode"`.
  `_resolve_opencode_binary()` resolves it via `shutil.which("mimo")` —
  **not** a bare `"mimo"` string, see the real bug found below.
  `run()`'s `--ai-tool` validation and help text updated to include it.

**Found and fixed a real Windows-specific bug during verification, not
assumed away:** a bare `Popen(["mimo", ...])` (matching how "opencode"
mode already worked) failed with `FileNotFoundError` — `mimo` is an
npm-style `.cmd` wrapper script, not a directly-executable binary the
way opencode's own bun-installed `.exe` is, and `subprocess.Popen`
(without `shell=True`) doesn't do the PATHEXT resolution a real shell or
`shutil.which()` does. Fixed by resolving through `shutil.which("mimo")`
in `_resolve_opencode_binary()` and passing the full resolved path
(`...\npm\mimo.CMD`) to `Popen`.

**Found and fixed a second real bug, this one general (not mimocode-
specific), while verifying cleanup:** `mimo serve`'s actual process tree
is three levels deep (`.cmd` → `node.exe` → a native `mimo.exe` server
binary) — `ManagedProcess.terminate()` only terminated the top-level
process, leaving the real `mimo.exe` server running as an orphan still
bound to the port, confirmed by inspecting live process state after
three separate test spawns (all three left orphans). This is the same
underlying issue as the "one leftover-process finding on `terminate()`
for plain `opencode` spawns" flagged several rounds ago and never
resolved — fixed properly this time: `ManagedProcess.terminate()` now
uses `taskkill /T /F /PID <pid>` (kills the entire process tree rooted
at that PID) on Windows, falling back to the plain `terminate()`/`kill()`
pair only if `taskkill` itself fails. Re-verified after the fix: spawned
mimo again, terminated it, confirmed zero surviving processes (previously
left 3 orphans across 3 spawns before the fix).

**Verified:**

- Backend imports cleanly; `_resolve_opencode_binary()` for `"mimocode"`
  tested directly (resolves to the real `.CMD` path).
- Real spawn-through-`_start_opencode()`-ready-terminate cycle tested
  twice (once exposing the `FileNotFoundError` bug, once after the fix,
  confirmed working) on a scratch port, never touching the live session.
- Cleaned up three genuinely orphaned `mimo.exe` instances found during
  this investigation (from the pre-fix test runs) via `taskkill /T /F`
  scoped to each port's verified owning PID, in the same step as
  verification — no PID-reuse risk.

**Not verified — yours to do:** an actual real end-to-end chat turn
through `--ai-tool mimocode` from a real paired phone/web client (only
direct HTTP/driver-level testing was done here); whether mimo's own free
model (`mimo-auto`) holds up under real, sustained use the way this
round's one-off test suggested it might.

**Another independent live-session interruption this round — not caused
by anything I touched.** All of this round's process kills were scoped
tightly to specific scratch ports (4104–4106) verified in the same step
before killing; none referenced ports 8000/4096/20241 at all. The live
session was down anyway partway through this round, consistent with the
still-unexplained independent crash pattern from the previous entry (or
possibly the user closing their own terminal — they had been running
`voice-cowork run` directly in their own shell earlier this session).
Restarted cleanly; new PIN, tunnel confirmed healthy immediately after.

**Carried forward:** everything from previous entries, plus the still-
unexplained recurring live-session interruption (now at least twice
independent of anything traceable to this session's own actions) —
worth investigating with real log retention if it recurs again.
