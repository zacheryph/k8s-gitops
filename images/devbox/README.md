# devbox

A workspace image for running coding agents in the cluster. By default it
runs [bb](https://getbb.app) (server and host daemon in one container), which
serves a web UI usable from phone, browser and desktop, and drives Pi, Claude
Code and Codex. Deployed by [`services/development/bb.yaml`](../../services/development/bb.yaml)
at `https://bb.${CLUSTER_DOMAIN}`.

Built by [`.github/workflows/devbox.yml`](../../.github/workflows/devbox.yml)
on every push to `main` touching this directory. The workflow smoke-tests the
image, pushes `ghcr.io/zacheryph/devbox:sha-<commit>` and opens a PR that
pins the new digest in `bb.yaml`. Renovate bumps the base image and every
`ARG *_VERSION` (each has a `# renovate:` comment).

## What's in it

Ubuntu 26.04, user `dev` (uid 1000) with passwordless sudo.

| Kind      | Baked in                                                                                         |
| --------- | ------------------------------------------------------------------------------------------------ |
| Languages | Node.js 24 (+ corepack: pnpm, yarn), Bun, Go, Python 3 + uv                                      |
| Agents    | bb, Pi, Claude Code, Codex                                                                       |
| CLIs      | gh, git, git-lfs, kubectl, helm, flux, sops, yq, jq, ripgrep, fd, fzf, tmux, vim, sqlite3, psql |
| Build     | build-essential, pkg-config, libssl-dev, python3-dev, shellcheck                                 |
| Installer | mise (any other language or CLI)                                                                 |

## What persists

`/home/dev` is a Longhorn volume; everything else is the image and resets on
restart. Installers are pointed at `$HOME` and come first on `PATH`, so a
runtime install both survives restarts and overrides the image's copy:

| Install with                            | Lands in                     |
| --------------------------------------- | ---------------------------- |
| `mise use -g rust@latest`, `java`, …    | `~/.local/share/mise`        |
| `npm i -g <pkg>` (`NPM_CONFIG_PREFIX`)  | `~/.local`                   |
| `bun add -g <pkg>`                      | `~/.bun`                     |
| `uv tool install <pkg>`                 | `~/.local`                   |
| `go install <pkg>@latest`               | `~/go/bin`                   |
| `cargo install <pkg>` (after mise rust) | `~/.cargo/bin`               |
| `devbox-apt add <pkg>`                  | reinstalled at every boot    |

Prefer mise for tools: `mise use -g` writes `~/.config/mise/config.toml`, and
the entrypoint runs `mise install` at boot, which only fetches what's missing.
mise's `aqua:`/`ubi:`/`github:` backends cover most single-binary CLIs. apt
packages can't live on the volume, so `devbox-apt` installs them now and lists
them in `~/.config/devbox/apt-packages` for the entrypoint to reinstall on the
next boot. Plain `sudo apt install` works too, but is gone after a restart.

`~/.config/devbox/startup.sh`, if present, runs as `dev` on every boot before
bb starts (clone repos, start a tmux session, …). Failures in any boot step are
logged and skipped, never fatal.

Logins persist too: `gh auth login`, Pi/Claude/Codex sign-ins (bb's onboarding
offers a **Sign in** button for each), `~/.ssh`, bb's own data in `~/.bb`.

bb itself always runs on the image's Node (`/usr/local/bin/node`), so a
`mise use -g node@…` changes the agents' and your Node, not bb's.

## First deploy

1. In pocket-id, create an OIDC client for bb with callback URL
   `https://bb.${CLUSTER_DOMAIN}/oauth2/callback`, then store its credentials:
   `scripts/cluster-secrets set OIDC_BB_CLIENT_ID` and
   `scripts/cluster-secrets set OIDC_BB_CLIENT_SECRET`.
2. Merge. The workflow builds the image and opens the digest-bump PR; until
   that merges the pod sits in `ImagePullBackOff` on the placeholder tag.
3. Make the `devbox` package public in GitHub (Packages → devbox → Package
   settings), as with `oidc-policy-controller`: the cluster pulls without
   credentials. GHCR makes new packages private.
4. Merge the bump PR. Open `https://bb.${CLUSTER_DOMAIN}`, sign in, and sign
   the agents in from bb's onboarding.
5. In a bb thread's terminal (or `kubectl -n development exec -it deploy/bb
   -- su - dev`; a plain exec is root): `gh auth login`, then set up the Pi
   bundle and its bb plugin:

   ```sh
   git clone https://github.com/zacheryph/pi ~/src/pi
   cd ~/src/pi && npm run setup:bb
   ```

## Security

bb's API is unauthenticated and runs commands. In the cluster it is reachable
only through the Envoy gateway, behind pocket-id (the route's
`cluster.routine.sh/oidc-credentials` label), and a NetworkPolicy admits only
the Envoy proxy pods to port 38886. Never expose the port any other way. The
pod has no service account token; give it a kubeconfig in `$HOME` if agents
should reach the cluster.

## Limits

- **bb's native mobile app** can't pass the OIDC login, so on a phone use the
  web UI in the browser.
- **The `bb` CLI from your own machine** can't either: use it inside the pod,
  or `kubectl -n development port-forward deploy/bb 38886` and
  `BB_SERVER_URL=http://127.0.0.1:38886 bb …`.
- When the OIDC session expires, the page's WebSocket stops reconnecting;
  reload to sign in again.

## Local build

```sh
docker build -t devbox images/devbox
docker run --rm -p 38886:38886 -v devbox-home:/home/dev devbox
```

Then open `http://localhost:38886`. The default command binds `0.0.0.0`
inside the container; don't publish the port beyond localhost.
