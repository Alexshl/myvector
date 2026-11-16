#!/usr/bin/env bash
# Install myvec (Linux or macOS): uv, ollama + embeddinggemma, the tool, skill, hooks and the daemon. Idempotent.
# Usage: ./install.sh [--no-daemon] [--no-hooks]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"
DO_DAEMON=1
DO_HOOKS=1
for arg in "$@"; do
  case "$arg" in
    --no-daemon) DO_DAEMON=0 ;;
    --no-hooks) DO_HOOKS=0 ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

export PATH="$HOME/.local/bin:$PATH"

step "uv"
if command -v uv >/dev/null 2>&1; then
  echo "uv already installed: $(command -v uv)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

CFG="${MYVEC_HOME:-$HOME/MyVector}/config.toml"
step "ollama"
if [ -f "$CFG" ] && grep -Eq '^embed_api *= *"openai"' "$CFG"; then
  echo "embed_api = \"openai\" in $CFG: embeddings come from an OpenAI-compatible server (e.g. llama.cpp), skipping ollama"
  echo "llama-server unit: contrib/llama-embed/ (README: «Ембеддинги без Ollama»)"
  OLLAMA_SKIP=1
elif command -v ollama >/dev/null 2>&1; then
  echo "ollama already installed: $(command -v ollama)"
elif [ "$OS" = "Linux" ]; then
  curl -fsSL https://ollama.com/install.sh | sh
else
  brew install ollama
  brew services start ollama
fi
ollama_up=0
if [ "${OLLAMA_SKIP:-0}" = 0 ]; then
  echo "waiting for ollama (up to 30 s)"
  for _ in $(seq 1 30); do
    if curl -fs -o /dev/null http://localhost:11434/api/tags; then ollama_up=1; break; fi
    sleep 1
  done
fi
if [ "${OLLAMA_SKIP:-0}" = 1 ]; then
  :
elif [ "$ollama_up" = 1 ]; then
  models="$(ollama list)"
  if grep -q '^embeddinggemma' <<<"$models"; then
    echo "embeddinggemma already pulled"
  else
    ollama pull embeddinggemma
  fi
else
  warn "ollama did not answer on :11434; start it and run: ollama pull embeddinggemma"
fi

step "myvec (uv tool install --reinstall)"
uv tool install --reinstall --python 3.12 "$REPO"

step "Claude Code skill"
mkdir -p "$HOME/.claude/skills/myvector"
for f in "$REPO"/skill/myvector/*.md; do
  cp "$f" "$HOME/.claude/skills/myvector/"
  echo "$HOME/.claude/skills/myvector/$(basename "$f")"
done

if [ "$OS" = "Linux" ] && [ "$DO_DAEMON" = 1 ]; then  # before the daemon: its user unit must outlive ssh sessions
  step "linger (daemon without a login session)"
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" = "yes" ]; then
    echo "linger already enabled"
  elif sudo -n true 2>/dev/null; then
    sudo loginctl enable-linger "$USER"
    echo "linger enabled for $USER"
  else
    warn "sudo needs a password: run 'sudo loginctl enable-linger $USER' yourself"
  fi
fi

if [ "$DO_HOOKS" = 1 ]; then
  step "hooks"
  myvec install-hooks
fi

if [ "$DO_DAEMON" = 1 ]; then
  step "daemon"
  myvec install-daemon
fi

if [ "$OS" = "Linux" ]; then
  step "inotify limits"
  want=524288
  have="$(cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null || echo 0)"
  if [ "$have" -ge "$want" ]; then
    echo "max_user_watches=$have, ok"
  elif sudo -n true 2>/dev/null; then
    printf 'fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=1024\n' \
      | sudo tee /etc/sysctl.d/60-myvec-inotify.conf >/dev/null
    sudo sysctl --system >/dev/null
    echo "max_user_watches=$(cat /proc/sys/fs/inotify/max_user_watches)"
  else
    warn "max_user_watches=$have and sudo needs a password: set fs.inotify.max_user_watches=$want in /etc/sysctl.d/ yourself"
  fi
fi

step "status"
myvec status | head -5 || true
if [ "$DO_DAEMON" = 1 ]; then
  sleep 2
  printf 'daemon http: %s\n' "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7878/ || true)"
fi

cat <<'MSG'

Done. Next steps:
  - open a project folder, then run /myvector in Claude Code or `myvec init` there
  - to move an existing index from another machine: copy ~/MyVector, then
      myvec relocate --from /old/home --to /new/home
    and run `myvec sync` in each project
MSG
