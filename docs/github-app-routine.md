# Routine — GitHub App for Hermes Agent

Routine is a GitHub App that gives Hermes Agent its own bot identity on GitHub.
It acts as `routine[bot]` in comments, commits, and PRs — separate from any
personal account.

## App Manifest

```yaml
# github-app-manifest.yaml — use this to create or recreate the app
name: Routine
description: Hermes Agent bot for PRs, issues, and repo operations
url: https://hermes.zro.io
hook_attributes:
  url: https://hermes-webhook.zro.io/webhooks/github
  active: false  # webhooks are managed separately via Hermes subscriptions
public: false
default_permissions:
  contents: read
  issues: write
  pull_requests: write
  metadata: read
default_events: []
```

## Creating the App (one-time setup)

### Option A: GitHub UI

1. Go to https://github.com/settings/apps/new
2. Fill in:
   - **GitHub App name**: `Routine`
   - **Homepage URL**: `https://hermes.zro.io`
   - **Webhook URL**: `https://hermes-webhook.zro.io/webhooks/github`
   - **Webhook secret**: (generate a random string, or leave blank — Hermes manages
     its own HMAC per subscription)
3. Under **Permissions**, set:
   - **Contents**: Read-only
   - **Issues**: Read & write
   - **Pull requests**: Read & write
   - **Metadata**: Read-only (auto-selected)
4. Under **Where can this GitHub App be installed?**, select **Any account**
5. Click **Create GitHub App**
6. After creation, scroll to **Private keys** and click **Generate a private key**.
   Save the `.pem` file.
7. Note the **App ID** (top of the page).

### Option B: Manifest flow (API)

```bash
# 1. Navigate to the manifest creation URL (opens in browser):
#    https://github.com/settings/apps/new?state=abc123

# 2. Paste the manifest YAML above into the text area.

# 3. After creation, generate a private key and note the App ID.
```

### Installing on Repos

1. Go to the app's page: `https://github.com/apps/routine`
2. Click **Install** (or `https://github.com/apps/routine/installations/new`)
3. Choose **Only select repositories** and pick the repos Routine should access:
   - `zacheryph/*` — personal repos
   - `ecogistics/*` — contract repos
4. You can install on multiple accounts/orgs. Each installation gets its own
   Installation ID, but you don't need to record them — Routine discovers the
   correct one at runtime (see below).

### How Installation IDs Work

When you install a GitHub App on an account or org, GitHub assigns an
**Installation ID**. If you install Routine on both `zacheryph` and `ecogistics`,
you'll have two installation IDs — one per account.

**You do not need to store these.** The GitHub API provides a dynamic lookup:

```
GET /repos/{owner}/{repo}/installation
```

Given any repo the app is installed on, this returns the correct Installation ID.
Routine authenticates with its App JWT (generated from the App ID + private key),
calls this endpoint, and gets back the right Installation ID for the repo it's
about to operate on.

This means you only need to store two things:
- **App ID** — public identifier for the app
- **Private key** — the secret used to sign JWTs

### Storing Credentials in k8s-gitops

Run the setup script from the repo root:

```bash
scripts/routine-github-app setup
```

The script prompts for:
- **App ID** — numeric, from the app's settings page
- **Private key path** — path to the `.pem` file downloaded from GitHub

It stores them across two files (both SOPS-encrypted):

| File | Value | Why separate |
|---|---|---|
| `config/secrets.yaml` | `GITHUB_ROUTINE_APP_ID` | Single-line, safe for `sops --set` |
| `config/routine-github-app-key.secret.yaml` | PEM private key | Multi-line block scalar — `sops --set` can't handle newlines in JSON values |

The PEM key lands in a dedicated Kubernetes Secret (`routine-github-app-key`)
and is mounted at `/etc/routine-github-app/key.pem` with mode `0o600`.

After setup, commit and push:

```bash
git add config/secrets.yaml config/routine-github-app-key.secret.yaml
git commit -m "chore(secrets): add Routine GitHub App credentials"
git push
```

Flux will reconcile and the Hermes pod will pick up the new secret on restart.

## Verification

```bash
# Check the secret exists in cluster
kubectl get secret routine-github-app-key -n automation

# Check the App ID env var is set in the Hermes pod
kubectl exec -n automation deploy/hermes-agent -- env | grep GITHUB_ROUTINE_APP_ID

# Check the PEM key is mounted
kubectl exec -n automation deploy/hermes-agent -- ls -la /etc/routine-github-app/key.pem

# Test gh auth as the bot (from inside the pod)
kubectl exec -n automation deploy/hermes-agent -- \
  gh auth status 2>&1 || echo "gh not configured for bot yet"
```

## How Auth Works

1. **Sign a JWT** using the App ID + private key (`/etc/routine-github-app/key.pem`)
2. **Discover installation ID** via `GET /repos/{owner}/{repo}/installation` (JWT-auth'd)
3. **Exchange** JWT for an installation token via `POST /app/installations/{id}/access_tokens`
4. **Use the token** like a PAT for API calls (expires in 1 hour)

## Rotating the Private Key

1. Generate a new private key on the app's settings page
2. Run: `scripts/routine-github-app update-key /path/to/new-key.pem`
3. Commit and push
4. The old key continues working until the app revokes it (GitHub allows multiple
   active keys).

## Installing on Additional Orgs Later

Just install the app on the new org via `https://github.com/apps/routine/installations/new`.
No credential changes needed — the dynamic installation ID lookup handles it
automatically.

## Troubleshooting

| Problem | Fix |
|---|---|
| `gh auth status` shows personal account, not bot | The `GITHUB_TOKEN` env var or `gh` host config is overriding the app. The app credentials are available as `GITHUB_ROUTINE_APP_ID` env var and `/etc/routine-github-app/key.pem` — the Hermes GitHub skill needs to be updated to use them. |
| Private key PEM has wrong permissions | The `extraVolumes` mount sets `defaultMode: 0o600`. If you see permission errors, check the Secret's `stringData` preserves the PEM format (no extra escaping). |
| Installation token expired | Installation tokens expire after 1 hour. The app/client must refresh by generating a new JWT and exchanging it. This is handled by the GitHub API client, not the secret itself. |
| 404 on installation lookup | The app isn't installed on that repo's account. Install it at `https://github.com/apps/routine/installations/new`. |