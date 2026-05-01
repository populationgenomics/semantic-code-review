#!/usr/bin/env bash
# install.sh — one-shot installer for the `scr` (Semantic Code Review) CLI.
#
# Usage (pin to a release tag — never pipe `main` into bash):
#     curl -fsSL https://raw.githubusercontent.com/populationgenomics/semantic-code-review/<tag>/install.sh | bash
#
# What this does:
#   1. Confirms uv, gcloud, and python3 are present.
#   2. Confirms Application Default Credentials (ADC) are configured.
#   3. Installs the `keyring` CLI plus the GCP Artifact Registry backend
#      as a uv tool (so it's on PATH for uv's --keyring-provider=subprocess).
#   4. Drops a thin `scr` wrapper into ~/.local/bin that runs the published
#      package from CPG's Artifact Registry.
#
# Override SCR_INSTALL_DIR to install the wrapper somewhere other than
# ~/.local/bin. Re-running is idempotent.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants. Update AR_* if the registry repo moves.
# ---------------------------------------------------------------------------
AR_LOCATION="australia-southeast1"
AR_PROJECT="aasgard-dev"
AR_REPO="scr-python"
# The `oauth2accesstoken` username is a placeholder GCP recognises; uv will
# only invoke the keyring backend when the URL contains a username, so this
# is what flips the auth path on. The actual access token is fetched from
# ADC by `keyrings.google-artifactregistry-auth` at request time.
INDEX_URL="https://oauth2accesstoken@${AR_LOCATION}-python.pkg.dev/${AR_PROJECT}/${AR_REPO}/simple/"
WRAPPER_PATH="${SCR_INSTALL_DIR:-$HOME/.local/bin}/scr"

# ---------------------------------------------------------------------------
# Pretty output. Colours skipped if stdout isn't a TTY (e.g. inside `tee`).
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"; N="\033[0m"
else
    G=""; Y=""; R=""; N=""
fi
say()  { printf "${G}✓${N} %s\n" "$*"; }
warn() { printf "${Y}!${N} %s\n" "$*" >&2; }
die()  { printf "${R}✗${N} %s\n" "$*" >&2; exit 1; }

# Refuse to run as root: every artefact lands in $HOME, so root would only
# misroute the install. Catches the classic `curl | sudo bash` mistake.
[ "${EUID:-$(id -u)}" -ne 0 ] || die "do not run as root; this installer writes to your home directory."

echo "scr installer — Semantic Code Review CLI"
echo "----------------------------------------"

# 1. Preflight
command -v uv      >/dev/null || die "uv not found. Install it first: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v gcloud  >/dev/null || die "gcloud not found. Install the Google Cloud SDK."
command -v python3 >/dev/null || die "python3 not found. Install Python 3.11+."
say "uv $(uv --version 2>/dev/null | awk '{print $2}'), gcloud, python3 present"

# 2. ADC
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    warn "Application Default Credentials not configured."
    echo "  Running: gcloud auth application-default login"
    gcloud auth application-default login
fi
account=$(gcloud config get-value account 2>/dev/null || true)
say "ADC ready${account:+ for $account}"

# 3. keyring + GCP Artifact Registry backend.
#    Installed as a uv tool so the `keyring` binary lands on PATH without
#    polluting any project venv. --force makes the step idempotent.
uv tool install --force --with keyrings.google-artifactregistry-auth keyring >/dev/null
say "keyring + Artifact Registry auth backend installed"

# 4. Wrapper.
mkdir -p "$(dirname "$WRAPPER_PATH")"
cat >"$WRAPPER_PATH" <<EOF
#!/usr/bin/env bash
# scr — wrapper installed by install.sh. Runs the published 'scr' tool
# from CPG's Artifact Registry. Auth via ADC + keyring backend.
exec uv tool run \\
    --index "$INDEX_URL" \\
    --index-strategy unsafe-best-match \\
    --keyring-provider subprocess \\
    --from semantic-code-review \\
    scr "\$@"
EOF
chmod +x "$WRAPPER_PATH"
say "wrapper installed at $WRAPPER_PATH"

# 5. PATH check.
case ":$PATH:" in
    *":$(dirname "$WRAPPER_PATH"):"*)
        say "$(dirname "$WRAPPER_PATH") is already on PATH"
        ;;
    *)
        warn "$(dirname "$WRAPPER_PATH") is not on PATH. Add to your shell rc:"
        printf '    export PATH="%s:$PATH"\n' "$(dirname "$WRAPPER_PATH")" >&2
        ;;
esac

cat <<EOF

Done. Try:
    scr --help
    scr review HEAD~1
    scr pr populationgenomics/<repo>
EOF
