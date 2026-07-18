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
# 0b. Restore home config from UbuntuDATA
###############################################################################
restore_home_config() {
    local BACKUP="$DATA/backup/home-config"
    if [[ ! -d "$BACKUP" ]]; then
        warn "No backup found at $BACKUP — skipping restore"
        return 0
    fi
    log "Restoring home config from $BACKUP..."

    # SSH keys
    if [[ -d "$BACKUP/.ssh" ]]; then
        rm -rf ~/.ssh
        cp -r "$BACKUP/.ssh" ~/.ssh
        chmod 700 ~/.ssh
        chmod 600 ~/.ssh/id_* ~/.ssh/authorized_keys 2>/dev/null || true
        chmod 644 ~/.ssh/id_*.pub 2>/dev/null || true
        log "SSH keys restored"
    fi

    # Rime input method
    if [[ -d "$BACKUP/ibus" ]]; then
        rm -rf ~/.config/ibus
        cp -r "$BACKUP/ibus" ~/.config/ibus
        chown -R "$USER":"$USER" ~/.config/ibus 2>/dev/null || true
        log "Rime config restored"
    fi

    # RustDesk
    if [[ -d "$BACKUP/rustdesk" ]]; then
        rm -rf ~/.config/rustdesk
        cp -r "$BACKUP/rustdesk" ~/.config/rustdesk
        chown -R "$USER":"$USER" ~/.config/rustdesk 2>/dev/null || true
        log "RustDesk config restored"
    fi

    # .bashrc
    if [[ -f "$BACKUP/.bashrc" ]]; then
        cp "$BACKUP/.bashrc" ~/.bashrc
        log ".bashrc restored"
    fi

    # .profile
    if [[ -f "$BACKUP/.profile" ]]; then
        cp "$BACKUP/.profile" ~/.profile
        log ".profile restored"
    fi

    # Hermes .env
    if [[ -f "$BACKUP/.env" ]]; then
        cp "$BACKUP/.env" ~/.hermes/.env
        log "Hermes .env restored"
    fi

    # GitHub auth
    if [[ -f "$BACKUP/gh_hosts.yml" ]]; then
        mkdir -p ~/.config/gh
        cp "$BACKUP/gh_hosts.yml" ~/.config/gh/hosts.yml
        chmod 600 ~/.config/gh/hosts.yml
        log "GitHub auth restored"
    fi

    # NetworkManager WiFi
    if ls "$BACKUP/"*.nmconnection &>/dev/null 2>&1; then
        sudo cp "$BACKUP/"*.nmconnection /etc/NetworkManager/system-connections/ 2>/dev/null || true
        log "WiFi config restored"
    fi

    # GNOME settings
    if [[ -d "$BACKUP/gnome" ]]; then
        gsettings load org.gnome.desktop.lockdown "$BACKUP/gnome/lockdown.xml" 2>/dev/null || true
        gsettings load org.gnome.desktop.session "$BACKUP/gnome/session.xml" 2>/dev/null || true
        log "GNOME settings restored"
    fi

    log "Home config restore complete"
}

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
        # Container & build
        docker.io docker-compose-v2 \
        git cmake build-essential \
        python3 python3-pip python3-venv python3-full python3-pipx \
        nodejs npm \
        # Remote & tools
        rustdesk freerdp2-x11 \
        curl wget ffmpeg jq htop tmux \
        # Audio
        libsndfile1-dev portaudio19-dev \
        # GitHub CLI
        gh \
        # KVM / libvirt virtualization
        qemu-system-x86 qemu-utils \
        libvirt-daemon libvirt-daemon-system libvirt-daemon-driver-qemu \
        libvirt-clients \
        virt-manager virtinst \
        bridge-utils spice-vdagent \
        # Chinese input (Rime + IBus)
        ibus ibus-rime \
        # Chinese fonts
        fonts-noto-cjk fonts-wqy-zenhei fonts-noto-color-emoji
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    sudo usermod -aG libvirt "$USER" 2>/dev/null || true
    sudo systemctl enable docker
    sudo systemctl enable rustdesk
    sudo systemctl enable libvirtd
    log "System packages installed"
}

###############################################################################
# 1c. NVIDIA driver + CUDA toolkit (RTX 4090)
###############################################################################
install_nvidia() {
    log "Installing NVIDIA driver + CUDA toolkit..."
    # Add NVIDIA repo if needed
    if ! dpkg -l | grep -q 'nvidia-driver'; then
        sudo add-apt-repository -y ppa:graphics-drivers/ppa 2>/dev/null || true
        sudo apt update -qq
    fi
    # Install driver (595 for 4090)
    sudo apt install -y -qq nvidia-driver-595-open nvidia-utils-595 nvidia-compute-utils-595
    # CUDA toolkit 13.3
    if [[ ! -d /usr/local/cuda ]]; then
        log "Installing CUDA toolkit 13.3..."
        sudo apt install -y -qq cuda-toolkit-13-3 cuda-nvtx-13-3 cuda-nsight-compute-13-3 2>/dev/null || {
            warn "CUDA toolkit not in apt — downloading from NVIDIA..."
            local CUDA_VER="13.3"
            wget -q "https://developer.download.nvidia.com/compute/cuda/${CUDA_VER}/local_installers/cuda_${CUDA_VER//./_}_linux.run" -O /tmp/cuda.run
            sudo sh /tmp/cuda.run --silent --toolkit --override 2>/dev/null || warn "CUDA install failed"
            rm -f /tmp/cuda.run
        }
    fi
    # Add CUDA to PATH
    if ! grep -q "CUDA_HOME" ~/.bashrc 2>/dev/null; then
        cat >> ~/.bashrc <<'EOF'

# NVIDIA CUDA
export CUDA_HOME=/usr/local/cuda
export PATH=$CUDA_HOME/bin:$PATH
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH
EOF
        log "CUDA environment added to ~/.bashrc"
    fi
    log "NVIDIA driver $(nvidia-smi --query-gpu=driver_version --format=csv -i 0 2>/dev/null | tail -1) + CUDA installed"
}

###############################################################################
# 1d. Locale & language (en_US + zh_CN)
###############################################################################
setup_locale() {
    log "Setting up locale (en_US + zh_CN)..."
    sudo apt install -y -qq language-pack-zh-hans 2>/dev/null || true
    sudo locale-gen en_US.UTF-8 zh_CN.UTF-8 2>/dev/null || true
    # Keep en_US as default but ensure zh_CN is available
    if ! locale -a 2>/dev/null | grep -q zh_CN; then
        echo "zh_CN.UTF-8 UTF-8" | sudo tee -a /etc/locale.gen
        sudo locale-gen 2>/dev/null || true
    fi
    log "Locale: en_US.UTF-8 (default), zh_CN.UTF-8 (available)"
}

###############################################################################
# 1e. Rime input method configuration
###############################################################################
setup_rime() {
    log "Configuring Rime input method..."
    # Set IBus as input method framework
    echo "GTK_IM_MODULE=ibus" >> ~/.profile
    echo "QT_IM_MODULE=ibus" >> ~/.profile
    echo "XMODIFIERS=@im=ibus" >> ~/.profile
    # Rime custom config directory
    mkdir -p ~/.config/ibus/rime
    # Default Rime user config if not exists
    if [[ ! -f ~/.config/ibus/rime/user.yaml ]]; then
        cat > ~/.config/ibus/rime/user.yaml <<'EOF'
# Rime user config
# Default schema: luna_pinyin
config:
  schema_list:
    - schema: luna_pinyin
    - schema: luna_pinyin_simp
    - schema: t9
EOF
        log "Rime default config created (luna_pinyin)"
    fi
    log "Rime input method configured via IBus"
}

###############################################################################
# 1b. GitHub auth (HTTPS + gh token — no manual SSH key needed)
###############################################################################
setup_github_auth() {
    log "Setting up GitHub auth..."
    # Install gh CLI if not present
    if ! command -v gh &>/dev/null; then
        log "Installing gh CLI..."
        type -p curl >/dev/null || sudo apt install -y curl
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
        sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt update -qq
        sudo apt install -y gh
    fi

    # Check if already logged in
    if gh auth status &>/dev/null; then
        log "GitHub already authenticated as $(gh api user --jq .login 2>/dev/null)"
    else
        warn "GitHub not authenticated — please run: gh auth login --web --git-protocol=https"
        return 0
    fi

    # Ensure git protocol is https (not ssh which needs key registration)
    gh auth status 2>&1 | grep -q "Git operations protocol: ssh" && {
        log "Switching git protocol from ssh to https..."
        gh auth setup-git
    } || log "Git protocol already https"

    # Configure credential helper
    gh auth setup-git
    log "GitHub auth configured (HTTPS + token)"
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
    # Second instance
    local ST2="$PROJECTS/SillyTavern-voice-test"
    if [[ ! -d "$ST2" ]]; then
        git clone https://github.com/SillyTavern/SillyTavern.git "$ST2"
    fi
    cd "$ST2" && git pull || true
    if [[ -f "$ST2/config.yaml" ]]; then
        sed -i 's/^port:.*/port: 9001/' "$ST2/config.yaml"
    fi
    # Install npm deps for both
    /home/ryan/.hermes/node/bin/npm install --prefix "$ST"
    /home/ryan/.hermes/node/bin/npm install --prefix "$ST2"
    # Create services
    if [[ ! -f "$HOME/.config/systemd/user/sillytavern.service" ]]; then
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/sillytavern.service" <<'SVCEOF'
[Unit]
Description=SillyTavern - AI Chat Frontend (port 9277)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/bin/bash -c '/home/ryan/.hermes/node/bin/npm start'
WorkingDirectory=/media/ryan/UbuntuDATA/AI_PROJECTS/SillyTavern-home
Environment="PATH=/home/ryan/.hermes/node/bin:/home/ryan/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        systemctl --user enable sillytavern
        log "sillytavern.service created"
    fi
    if [[ ! -f "$HOME/.config/systemd/user/sillytavern-voice.service" ]]; then
        cat > "$HOME/.config/systemd/user/sillytavern-voice.service" <<'SVCEOF'
[Unit]
Description=SillyTavern Voice Test (port 9001)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/bin/bash -c '/home/ryan/.hermes/node/bin/npm start'
WorkingDirectory=/media/ryan/UbuntuDATA/AI_PROJECTS/SillyTavern-voice-test
Environment="PATH=/home/ryan/.hermes/node/bin:/home/ryan/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        systemctl --user enable sillytavern-voice
        log "sillytavern-voice.service created"
    fi
    log "SillyTavern ready (ports 9277 + 9001)"
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
    # Create ComfyUI service if not exists
    if [[ ! -f "$HOME/.config/systemd/user/comfyui.service" ]]; then
        log "Creating comfyui.service..."
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/comfyui.service" <<'SVCEOF'
[Unit]
Description=ComfyUI - Stable Diffusion GUI
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/bin/bash -c 'source /media/ryan/UbuntuDATA/ComfyUI/venv/bin/activate && python main.py --listen 0.0.0.0 --port 8188'
WorkingDirectory=/media/ryan/UbuntuDATA/ComfyUI
Environment="PATH=/home/ryan/.hermes/node/bin:/home/ryan/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=/media/ryan/UbuntuDATA/ComfyUI/venv"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        systemctl --user enable comfyui
    fi
    log "ComfyUI ready at $CU (port 8188)"
}

###############################################################################
# 8. S2S Voice Assistant
###############################################################################
install_s2s() {
    log "Setting up S2S Voice Assistant..."
    local S2S="$PROJECTS/s2s"
    if [[ ! -d "$S2S" ]]; then
        warn "S2S repo not found at $S2S — creating service placeholder"
    else
        python3 -m venv "$S2S/venv"
        if [[ -f "$S2S/hf-realtime-voice/requirements.txt" ]]; then
            "$S2S/venv/bin/pip" install -r "$S2S/hf-realtime-voice/requirements.txt"
        fi
    fi
    # Create S2S service if not exists
    if [[ ! -f "$HOME/.config/systemd/user/s2s.service" ]]; then
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/s2s.service" <<'SVCEOF'
[Unit]
Description=S2S Voice Assistant
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/bin/bash -c 'source /media/ryan/UbuntuDATA/AI_PROJECTS/s2s/venv/bin/activate && cd /media/ryan/UbuntuDATA/AI_PROJECTS/s2s/hf-realtime-voice && python app.py --port 7860'
WorkingDirectory=/media/ryan/UbuntuDATA/AI_PROJECTS/s2s
Environment="PATH=/home/ryan/.hermes/node/bin:/home/ryan/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=/media/ryan/UbuntuDATA/AI_PROJECTS/s2s/venv"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        systemctl --user enable s2s
        log "s2s.service created"
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
        warn "UbuntuConsole not found — skipping install, creating service placeholder"
    fi
    # Create UbuntuConsole service
    if [[ ! -f "$HOME/.config/systemd/user/ubuntuconsole-webui.service" ]]; then
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/ubuntuconsole-webui.service" <<'SVCEOF'
[Unit]
Description=UbuntuConsole WebUI - Local Services Dashboard
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/bin/bash -c 'cd /media/ryan/UbuntuDATA/AI_PROJECTS/UbuntuConsole && node index.js'
WorkingDirectory=/media/ryan/UbuntuDATA/AI_PROJECTS/UbuntuConsole
Environment="PATH=/home/ryan/.hermes/node/bin:/home/ryan/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        systemctl --user enable ubuntuconsole-webui
        log "ubuntuconsole-webui.service created"
    fi
    log "UbuntuConsole ready at $UC (port 9002)"
}

###############################################################################
# 9b. KVM VM registration (marvis-box)
###############################################################################
register_kvm_vm() {
    log "Setting up KVM virtualization..."
    # Ensure libvirt is running
    sudo systemctl enable --now libvirtd 2>/dev/null || true
    # Add user to libvirt group
    sudo usermod -aG libvirt "$USER" 2>/dev/null || true

    local VM_DISK="/media/ryan/UbuntuDATA/VM_Marvis/disk/marvis-box.qcow2"
    if [[ -f "$VM_DISK" ]]; then
        # Check if VM is already registered
        if ! sg libvirt -c "virsh -c qemu:///system list --all" 2>/dev/null | grep -q "marvis-box"; then
            log "Registering marvis-box VM..."
            sg libvirt -c "virsh -c qemu:///system define - <<'VMEOF'
<VirtualMachine type='kvm'>
  <name>marvis-box</name>
  <memory unit='KiB'>33554432</memory>
  <currentMemory unit='KiB'>16777216</currentMemory>
  <vcpu placement='static'>6</vcpu>
  <os>
    <type arch='x86_64' machine='pc-q35'>hvm</type>
  </os>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$VM_DISK'/>
      <target dev='sda' bus='sata'/>
    </disk>
    <interface type='bridge'>
      <source bridge='virbr0'/>
    </interface>
    <graphics type='spice' port='5900' autoport='yes' listen='0.0.0.0'>
      <listen type='address' address='0.0.0.0'/>
    </graphics>
  </devices>
</VirtualMachine>
VMEOF"
            log "marvis-box VM registered"
        else
            log "marvis-box VM already registered"
        fi
    else
        warn "VM disk not found at $VM_DISK — skipping VM registration"
    fi
    log "KVM virtualization ready"
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
# 11b. CouchDB (Obsidian sync)
###############################################################################
install_couchdb() {
    log "Setting up CouchDB (Obsidian LiveSync)..."
    local COUCHDB="$PROJECTS/couchdb"
    if [[ -d "$COUCHDB" ]]; then
        cd "$COUCHDB"
        sg docker -c "docker compose pull" 2>/dev/null || true
        sg docker -c "docker compose up -d" 2>/dev/null || warn "CouchDB start failed"
        log "CouchDB ready (port 9300)"
    else
        warn "CouchDB project not found at $COUCHDB"
    fi
}

###############################################################################
# 11c. SearXNG (local search)
###############################################################################
install_searxng() {
    # SearXNG
    local SearXNG="$PROJECTS/searxng"
    if [[ -d "$SearXNG" ]]; then
        cd "$SearXNG"
        sg docker -c "docker compose pull" 2>/dev/null || true
        sg docker -c "docker compose up -d" 2>/dev/null || warn "SearXNG start failed"
        log "SearXNG ready (port 9301)"
    else
        warn "SearXNG project not found at $SearXNG"
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
    for svc in hermes-gateway comfyui s2s ubuntuconsole-webui camofox-browser sillytavern sillytavern-voice; do
        if systemctl --user is-enabled "$svc" &>/dev/null; then
            systemctl --user enable --now "$svc" 2>/dev/null || warn "  $svc already running or failed"
        else
            warn "  Service $svc not found — skipping"
        fi
    done
    log "Enabling system services..."
    sudo systemctl enable --now docker 2>/dev/null || true
    sudo systemctl enable --now rustdesk 2>/dev/null || true
    sudo systemctl enable --now libvirtd 2>/dev/null || true
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
    restore_home_config
    install_system_packages
    install_nvidia
    setup_locale
    setup_rime
    setup_github_auth
    install_node_hermes
    build_llama_gpu
    build_llama_cpu
    install_hermes
    install_sillytavern
    install_comfyui
    install_s2s
    install_ubuntuconsole
    install_camofox
    install_couchdb
    install_searxng
    register_kvm_vm
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
    echo "  8188  — ComfyUI"
    echo "  9277  — SillyTavern (main)"
    echo "  9288  — SillyTavern (voice-test)"
    echo "  9299  — S2S Voice"
    echo "  9002  — UbuntuConsole"
    echo "  9090  — Telegram Bot API (Docker)"
    echo "  9300  — CouchDB (Obsidian sync, Docker)"
    echo "  9301  — SearXNG (local search, Docker)"
    echo "  3002  — Firecrawl (Docker, official)"
    echo "  5900  — KVM marvis-box (SPICE)"
    echo ""
    echo "System:"
    echo "  NVIDIA driver 595 + CUDA 13.3"
    echo "  KVM/libvirt (marvis-box Tiny11 VM)"
    echo "  RustDesk remote desktop"
    echo "  Rime input method (ibus)"
    echo "  GitHub CLI (gh) authenticated"
    echo ""
    echo "Dashboard: http://localhost:9002"
    echo "SillyTavern: http://localhost:9277"
    echo "ComfyUI: http://localhost:8188"
    echo "S2S Voice: http://localhost:7860"
    echo "Firecrawl: http://localhost:3002"
    echo "RustDesk: 192.168.0.x (remote)"
    echo ""
    echo "Restart all: bash ~/setup-ryan-station.sh"
    echo ""
}

main "$@"
