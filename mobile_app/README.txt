POCKETMATCH 2 — MULTI-USER, PUBLIC-HOSTING PACKAGE

WHAT CHANGED
- Individual usernames and passwords; no shared APP_PASSWORD.
- Private request history and results, enforced by the server on every read.
- Persistent seven-day login sessions in SQLite; cookies are HttpOnly and
  SameSite=Strict, and Secure when PUBLIC_ORIGIN uses HTTPS.
- Passwords use salted scrypt hashes. Session tokens are stored as hashes.
- Sign out revokes the current session. Password reset revokes all user sessions.
- One running job per user; two simultaneous jobs globally by default.
- Ten accepted assessments per user per UTC day by default. Failed submissions
  that started a job also count. This is a request limit, not a dollar budget.
- Login throttling, CSRF protection, body limits, and host/origin checks.
- Gunicorn and Caddy deployment with automatic HTTPS and persistent storage.

UPGRADE YOUR EXISTING COPY

Back up mobile_app/data/history.sqlite3 with the old server stopped. Keep your
TOKEN.txt, catalogue, and mobile_app/data directory. Replace the code/static files
from this package; do not overwrite your own catalogue with the sample unless wanted.
Install the new dependencies in your existing virtual environment:

  python3 -m pip install -r mobile_app/requirements.txt

Create your first personal account (prompts twice for its password):

  python3 mobile_app/server.py --create-user ilya

Old shared requests are preserved but hidden from all accounts until you explicitly
assign them to an owner. To give the old shared history to your account:

  python3 mobile_app/server.py --claim-legacy ilya

Start locally:

  python3 mobile_app/server.py

Open http://127.0.0.1:8001 and sign in with your username and password.
Use the exact configured host; localhost and 127.0.0.1 are different origins.
TOKEN.txt still lives beside product_matcher.py and is read only on the server.
No HF token is required for account-management commands.

ACCOUNT ADMINISTRATION

  python3 mobile_app/server.py --create-user colleague
  python3 mobile_app/server.py --reset-password colleague

Only someone with server access can create/reset accounts. There is deliberately
no public registration consuming the owner's HF credits, and no email-reset flow.
A new login rotates the session in that browser; other devices retain their own
sessions. Password reset revokes all devices. Catalogue access is shared across
users. Requests, results and history are private per account.

PUBLIC HTTPS DEPLOYMENT

The package is ready to deploy; it is NOT already deployed. You need a server
running Docker Compose, a domain/subdomain pointing at that server, and reachable
ports 80/443. Host/domain charges are separate from model usage.

From the package's parent directory on that server:

  cp .env.example .env

Edit .env and set DOMAIN to your real hostname, e.g. match.your-domain.com.
Do not include https:// in DOMAIN. Configure DNS A/AAAA records to that server;
remove an incorrect AAAA record if the server has no working IPv6.

Prepare the directory read by the hosted app:

  mkdir -p catalogue
  cp given_industrial_products.json catalogue/given_industrial_products.json

For production, edit catalogue/given_industrial_products.json. This directory is
mounted read-only into the app, and its current contents are read per submission.
The root JSON is the local-development file. Only use the one for your environment.

Put your existing TOKEN.txt beside compose.yaml. It becomes a runtime Docker secret;
it is not copied into the image. Start:

  docker compose up -d --build
  docker compose exec app python3 mobile_app/server.py --create-user ilya

Open https://YOUR_DOMAIN and sign in. Caddy obtains/renews certificates and redirects
HTTP to HTTPS. The backend has no public port mapping; Caddy is the entry point.
Never expose backend port 8001 or bypass Caddy when TRUST_CADDY_PROXY=1.

Add a user on the deployed server:

  docker compose exec app python3 mobile_app/server.py --create-user colleague

Reset a deployed password:

  docker compose exec app python3 mobile_app/server.py --reset-password colleague

Inspect startup errors:

  docker compose logs --tail=100 app caddy

Stop without deleting history:

  docker compose down

Do NOT add --volumes unless you intentionally want to delete the history and
certificate volumes. Maintain backups of the history volume. Local SQLite history
is not automatically uploaded to a new server. Existing accounts/data persist in
the Docker volume across normal restarts and image rebuilds.

HOSTING CONFIGURATION

PUBLIC_ORIGIN       Exact external HTTPS origin; HTTP allowed only for loopback.
HISTORY_DB          SQLite path; default mobile_app/data/history.sqlite3.
HF_TOKEN_FILE       Optional token file override; Compose uses a runtime secret.
CATALOGUE_FILE      Optional catalogue path override; Compose uses /catalogue/...
DAILY_REQUEST_LIMIT Default 10 accepted jobs/user/UTC day.
MAX_CONCURRENT_JOBS Default 2; allowed 1–8 globally, plus one job/user.
HF_PROVIDER         Optional HF provider for the chosen Qwen model.
TRUST_CADDY_PROXY   Set only behind the supplied private proxy. Caddy overwrites
                    X-PocketMatch-Client-IP before forwarding it.

This version supports many registered users on ONE server process with threads.
Keep Gunicorn --workers 1 and one application replica. Do not use --preload or
rolling overlapping processes: background-job accounting is process-local.
For horizontal scaling, move jobs to a shared queue and the database to a managed
multi-process store. That is not included in this version.

If the server stops, unfinished jobs are marked interrupted on next startup and
are not automatically retried. Accepted jobs and completed batches are saved.
No change has been made to the selected model or the catalogue-matching workflow.
Human review is still needed; numerical engineering checks are model-generated.

PHONE INSTALLATION

At the deployed HTTPS address:
- iPhone Safari: Share -> Add to Home Screen.
- Android Chrome: Install app / Add to Home Screen.
Offline startup can display the interface; private history and new jobs need the
running server. Signing in from another device shows that user's same history.

VERIFICATION

  python3 -m unittest discover -s mobile_app -p 'test_*.py'

Tests cover cross-user history and direct-ID access, persistent/expired/revoked
sessions, password hashing, password reset, CSRF, secure cookies, body size limits,
parallel users, per-user limits, legacy history, provider errors, and empty catalogues.
A separate real Gunicorn HTTP smoke test passed with temporary users and no model
calls. Compose configuration validated. Docker image build, real TLS provisioning,
phone installation and live HF calls have not been verified in this release.
