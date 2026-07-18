#!/bin/bash
###############################################################################
# Ryan's AI Station — One-Click Setup Script
# Generated: 2026-07-17
# Target: Ubuntu 24.04 + NVIDIA GPU + /media/ryan/UbuntuDATA/ data disk
###############################################################################

set -euo pipefail

# Colors
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; NC='\033[0m'

log()  { echo -e "${G}[SETUP]${NC} $*"; }
warn() { echo -e "${Y}[WARN ]${NC} $*"; }
err()  { echo -e "${R}[ERROR]${NC} $*"; }
info() { echo -e "${B}[INFO ]${NC} $*"; }

DATA="/media/ryan/UbuntuDATA"
PROJECTS="$DATA/AI_PROJECTS"
MODELS="$DATA/ai_models"
HERMES="$HOME/.hermes"
NODE_DIR="$HOME/.hermes/node"

###############################################################################
# 0. Pre-flight
###############################################################################
preflight() {
    log "Preflight checks..."
    [[ -d "$DATA" ]] || { err "Data disk $DATA not found!"; exit 1; }
    [[ -f /etc/os-release ]] || { err "Not Ubuntu!"; exit 1; }
    nvidia-smi &>/dev/null || warn "No NVIDIA GPU detected — GPU builds will fail"
    log "User: $(whoami), Home: $HOME, Data: $DATA"
}

###############################################################################
# 1. System packages
###############################################################################
install_system_packages() {
    log "Installing system packages..."
    sudo apt update -qq
    sudo apt install -y -qq \
        docker.io docker-compose-v2 \
        git cmake build-essential \
        python3 python3-pip python3-venv python3-full \
        nodejs npm \
        rustdesk \
        curl wget ffmpeg \
        libsndfile1-dev portaudio19-dev
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    sudo systemctl enable docker
    sudo systemctl enable rustdesk
}

###############################################################################
# 2. Node.js (Hermes-specific version via nvm)
###############################################################################
install_node_hermes() {
    log "Setting up Hermes Node.js..."
    if [[ ! -d "$NODE_DIR" ]]; then
        mkdir -p "$NODE_DIR"
        # Install Node 22.x for Hermes
        curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
        export NVM_DIR="$HOME/.nvm"
        [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
        nvm install 22
        nvm alias default 22
        nvm use 22
        log "Node $(node --version) installed"
    fi
}

###############################################################################
# 3. llama.cpp — GPU build (port 8888)
###############################################################################
build_llama_gpu() {
    log "Building llama.cpp (GPU + CUDA)..."
    local LlamaDir="$PROJECTS/llama.cpp"
    if [[ ! -d "$LlamaDir" ]]; then
        git clone https://github.com/ggml-org/llama.cpp.git "$LlamaDir"
    fi
    cd "$LlamaDir" && git pull || true
    mkdir -p build && cd build
    cmake .. -DGGML_CUDA=ON -DLLAMA_CUDA_F16=ON
    make -j$(nproc)
    log "llama-server (GPU) built at $LlamaDir/build/bin/llama-server"
}

###############################################################################
# 4. llama.cpp — CPU build (port 8889)
###############################################################################
build_llama_cpu() {
    log "Building llama.cpp (CPU only)..."
    local LlamaDir="$PROJECTS/llama.cpp"
    mkdir -p "$LlamaDir/build-cpu" && cd "$LlamaDir/build-cpu"
    cmake .. -DGGML_CPU=ON
    make -j$(nproc)
    log "llama-server (CPU) built at $LlamaDir/build-cpu/bin/llama-server"
}

###############################################################################
# 5. Hermes Agent
###############################################################################
install_hermes() {
    log "Installing Hermes Agent..."
    if [[ ! -d "$HERMES/hermes-agent" ]]; then
        pipx install hermes-agent || {
            pip install --user hermes-agent
        }
    fi
    # Ensure venv exists
    if [[ ! -d "$HERMES/hermes-agent/venv" ]]; then
        python3 -m venv "$HERMES/hermes-agent/venv"
        "$HERMES/hermes-agent/venv/bin/pip" install hermes-agent
    fi
    # Activate gateway service
    if [[ ! -f "$HOME/.config/systemd/user/hermes-gateway.service" ]]; then
        log "Creating hermes-gateway.service..."
        cat > "$HOME/.config/systemd/user/hermes-gateway.service" <<'SVCEOF'
[Unit]
Description=Hermes Agent Gateway - Messaging Platform Integration
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/home/ryan/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
WorkingDirectory=/home/ryan/.hermes
Environment="PATH=/home/ryan/.hermes/hermes-agent/venv/bin:/home/ryan/.hermes/hermes-agent/node_modules/.bin:/home/ryan/.hermes/node/bin:/home/ryan/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=/home/ryan/.hermes/hermes-agent/venv"
Environment="HERMES_HOME=/home/ryan/.hermes"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
ExecStopPost=-/home/ryan/.hermes/hermes-agent/venv/bin/python -m gateway.cgroup_cleanup
TimeoutStopSec=90
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        systemctl --user enable hermes-gateway
    fi
}

###############################################################################
# 6. SillyTavern
###############################################################################
install_sillytavern() {
    log "Setting up SillyTavern..."
    local ST="$PROJECTS/SillyTavern-home"
    if [[ ! -d "$ST" ]]; then
        git clone https://github.com/SillyTavern/SillyTavern.git "$ST"
    fi
    cd "$ST" && git pull || true
    # Ensure port 9277 in config
    if [[ -f "$ST/config.yaml" ]]; then
        sed -i 's/^port:.*/port: 9277/' "$ST/config.yaml"
    fi
    npm install --prefix "$ST"
    log "SillyTavern ready at $ST (port 9277)"
}

###############################################################################
# 7. ComfyUI
###############################################################################
install_comfyui() {
    log "Setting up ComfyUI..."
    local CU="$DATA/ComfyUI"
    if [[ ! -d "$CU" ]]; then
        git clone https://github.com/comfyanonymous/ComfyUI.git "$CU"
    fi
    cd "$CU" && git pull || true
    python3 -m venv "$CU/venv"
    "$CU/venv/bin/pip" install -r "$CU/requirements.txt"
    log "ComfyUI ready at $CU (port 8188)"
}

###############################################################################
# 8. S2S Voice Assistant
###############################################################################
install_s2s() {
    log "Setting up S2S Voice Assistant..."
    local S2S="$PROJECTS/s2s"
    if [[ ! -d "$S2S" ]]; then
        # Clone S2S project (adjust URL as needed)
        warn "S2S repo not found — manual clone needed"
        return 0
    fi
    python3 -m venv "$S2S/venv"
    if [[ -f "$S2S/hf-realtime-voice/requirements.txt" ]]; then
        "$S2S/venv/bin/pip" install -r "$S2S/hf-realtime-voice/requirements.txt"
    fi
    log "S2S ready at $S2S (port 7860)"
}

###############################################################################
# 9. UbuntuConsole
###############################################################################
install_ubuntuconsole() {
    log "Setting up UbuntuConsole..."
    local UC="$PROJECTS/UbuntuConsole"
    if [[ ! -d "$UC" ]]; then
        warn "UbuntuConsole not found — cloning or creating..."
        return 0
    fi
    log "UbuntuConsole ready at $UC (port 9002)"
}

###############################################################################
# 10. Camofox Browser
###############################################################################
install_camofox() {
    log "Setting up Camofox Browser..."
    local CB="$PROJECTS/camofox-browser"
    if [[ ! -d "$CB" ]]; then
        git clone https://github.com/jo-inc/camofox-browser.git "$CB"
    fi
    cd "$CB"
    /home/ryan/.hermes/node/bin/npm install || true
    # Create wrapper script
    mkdir -p "$DATA/bin"
    cat > "$DATA/bin/camofox-wrapper.sh" <<'EOF'
#!/bin/bash
export PATH="/home/ryan/.hermes/node/bin:/home/ryan/.hermes/hermes-agent/venv/bin:$PATH"
exec /home/ryan/.hermes/node/bin/npm start \
  --prefix /media/ryan/UbuntuDATA/AI_PROJECTS/camofox-browser
EOF
    chmod +x "$DATA/bin/camofox-wrapper.sh"
    log "Camofox ready at $CB"
}

###############################################################################
# 11. Docker containers — Firecrawl + Telegram Bot API
###############################################################################
install_docker_containers() {
    log "Setting up Docker containers..."

    # Firecrawl
    local FC="$PROJECTS/firecrawl/fc-src"
    if [[ ! -d "$FC" ]]; then
        git clone https://github.com/mendableai/firecrawl.git "$PROJECTS/firecrawl"
        # The docker-compose is in the root, symlink or copy to fc-src
        mkdir -p "$(dirname "$FC")"
        cp -r "$PROJECTS/firecrawl" "$FC"
    fi
    cd "$FC"
    # Ensure pre-built images
    sed -i 's|# image: ghcr.io/firecrawl/firecrawl:latest|image: ghcr.io/firecrawl/firecrawl:latest|' docker-compose.yaml
    sed -i 's|image: ghcr.io/firecrawl/firecrawl:latest|image: ghcr.io/firecrawl/firecrawl:latest|' docker-compose.yaml 2>/dev/null || true
    newgrp docker -c "docker compose pull" 2>/dev/null || warn "Docker pull failed — will retry on first run"

    # Telegram Bot API
    log "Deploying Telegram Bot API..."
    # User needs to set BOT_TOKEN, API_ID, API_HASH
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        warn "Set TELEGRAM_BOT_TOKEN env var for Telegram Bot API deployment"
    else
        docker run -d --name telegram-bot-api -p 9090:8080 \
            -e TELEGRAM_API_ID="${TELEGRAM_API_ID:-}" \
            -e TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-}" \
            -e TELEGRAM_AUTH_STRING="${TELEGRAM_AUTH_STRING:-}" \
            -e TELEGRAM_SESSION_STRING="${TELEGRAM_SESSION_STRING:-}" \
            aiogram/telegram-bot-api:latest
    fi
}

###############################################################################
# 12. Start llama.cpp servers
###############################################################################
start_llama_servers() {
    log "Starting llama.cpp servers..."
    local GPU="$PROJECTS/llama.cpp/build/bin/llama-server"
    local CPU="$PROJECTS/llama.cpp/build-cpu/bin/llama-server"

    # GPU server — Qwen3.6-27B
    nohup "$GPU" \
        -m "$MODELS/Qwen3.6-27B-UD-Q4_K_XL.gguf" \
        --spec-type draft-mtp --spec-draft-n-max 2 \
        -c 131072 -ngl 999 -fa on -ctk q4_0 -ctv q4_0 \
        -b 512 -ub 512 --cont-batching --reasoning-preserve \
        --host 0.0.0.0 --port 8888 -t 8 \
        > /tmp/llama-gpu.log 2>&1 &
    log "  GPU server started on port 8888 (PID: $!)"

    # CPU server — Gemma-4-E4B
    nohup "$CPU" \
        -m "$MODELS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf" \
        -c 32768 -t 12 -tb 20 -b 512 -ub 512 \
        --cont-batching --reasoning-preserve \
        --host 0.0.0.0 --port 8889 --mlock \
        > /tmp/llama-cpu.log 2>&1 &
    log "  CPU server started on port 8889 (PID: $!)"
}

###############################################################################
# 13. Enable & start systemd services
###############################################################################
enable_services() {
    log "Enabling systemd user services..."
    for svc in hermes-gateway comfyui s2s ubuntuconsole-webui camofox-browser; do
        if systemctl --user is-enabled "$svc" &>/dev/null; then
            systemctl --user enable --now "$svc" 2>/dev/null || warn "  $svc already running or failed"
        else
            warn "  Service $svc not found — skipping"
        fi
    done
    log "Enabling system services..."
    sudo systemctl enable --now docker 2>/dev/null || true
    sudo systemctl enable --now rustdesk 2>/dev/null || true
}

###############################################################################
# 14. Firecrawl start
###############################################################################
start_firecrawl() {
    log "Starting Firecrawl stack..."
    local FC="$PROJECTS/firecrawl/fc-src"
    if [[ -d "$FC" ]]; then
        newgrp docker -c "cd $FC && docker compose up -d" 2>/dev/null || warn "Firecrawl start failed"
        log "Firecrawl available at http://localhost:3002"
    fi
}

###############################################################################
# 15. Verify
###############################################################################
verify() {
    log "Verifying all services..."
    sleep 5
    echo ""
    echo "========================================="
    echo "  PORT STATUS CHECK"
    echo "========================================="
    ss -tlnp | grep -E ':(3002|7860|8188|8888|8889|9002|9090|9277)\b' | while read -r line; do
        echo "  $line"
    done
    echo ""
    echo "========================================="
    echo "  DOCKER CONTAINERS"
    echo "========================================="
    newgrp docker -c "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'" 2>/dev/null || true
    echo ""
    echo "========================================="
    echo "  USER SYSTEMD SERVICES"
    echo "========================================="
    systemctl --user list-units --type=service --state=running | grep -E 'hermes|comfyui|s2s|ubuntuconsole|camofox' || true
    echo ""
}

###############################################################################
# MAIN
###############################################################################
main() {
    echo -e "${G}"
    echo "============================================="
    echo "  Ryan's AI Station — One-Click Setup"
    echo "  $(date)"
    echo "============================================="
    echo -e "${NC}"

    preflight
    install_system_packages
    install_node_hermes
    build_llama_gpu
    build_llama_cpu
    install_hermes
    install_sillytavern
    install_comfyui
    install_s2s
    install_ubuntuconsole
    install_camofox
    install_docker_containers
    enable_services
    start_llama_servers
    start_firecrawl
    verify

    echo -e "${G}============================================="
    echo "  SETUP COMPLETE!"
    echo "============================================="
    echo -e "${NC}"
    echo ""
    echo "Services:"
    echo "  8888  — llama.cpp GPU (Qwen3.6-27B)"
    echo "  8889  — llama.cpp CPU (Gemma-4-E4B)"
    echo "  9277  — SillyTavern"
    echo "  8188  — ComfyUI"
    echo "  7860  — S2S Voice"
    echo "  9002  — UbuntuConsole"
    echo "  3002  — Firecrawl (Docker)"
    echo "  9090  — Telegram Bot API (Docker)"
    echo ""
    echo "Dashboard: http://localhost:9002"
    echo "SillyTavern: http://localhost:9277"
    echo "ComfyUI: http://localhost:8188"
    echo "S2S Voice: http://localhost:7860"
    echo "Firecrawl: http://localhost:3002"
    echo ""
    echo "Restart all: bash ~/setup-ryan-station.sh"
    echo ""
}

main "$@"
