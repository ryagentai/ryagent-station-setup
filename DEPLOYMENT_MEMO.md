# Ryan's AI Station — Deployment Memo
# Generated: 2026-07-17 (~7 hour deployment session)

========================================
## OVERVIEW
========================================
Ubuntu 24.04 fresh install. All services recovered from old 22.04 backup + new deployments.
Base data on /media/ryan/UbuntuDATA/ (separate disk for AI models & projects).

========================================
## SERVICES (Running)
========================================

### 1. Hermes Agent Gateway (user service)
   Port: internal
   Description: Hermes Agent messaging gateway — Telegram, Discord, Slack integration
   Exec: python -m hermes_cli.main gateway run
   Config: ~/.hermes/config.yaml
   Venv: ~/.hermes/hermes-agent/venv
   Model: Qwen3.6-27B-UD-Q4_K_XL.gguf (via llama.cpp:8888)

### 2. SillyTavern (Node.js)
   Port: 9277
   Description: AI chat frontend, connects to llama.cpp backend
   Path: /media/ryan/UbuntuDATA/AI_PROJECTS/SillyTavern-home/
   Config: config.yaml (port 9277, CORS enabled, extensions enabled)
   Backend: llama.cpp at http://127.0.0.1:8889

### 3. llama.cpp Server #1 — GPU (Primary)
   Port: 8888
   Model: Qwen3.6-27B-UD-Q4_K_XL.gguf
   Args: -c 131072 -ngl 999 -t 8 --spec-type draft-mtp --cont-batching --reasoning-preserve
   Path: /media/ryan/UbuntuDATA/AI_PROJECTS/llama.cpp/build/bin/llama-server

### 4. llama.cpp Server #2 — CPU (Secondary)
   Port: 8889
   Model: Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf
   Args: -c 32768 -t 12 --cont-batching --reasoning-preserve --mlock
   Path: /media/ryan/UbuntuDATA/AI_PROJECTS/llama.cpp/build-cpu/bin/llama-server

### 5. ComfyUI (user service)
   Port: 8188
   Description: Stable Diffusion GUI for image generation
   Path: /media/ryan/UbuntuDATA/ComfyUI/
   Venv: /media/ryan/UbuntuDATA/ComfyUI/venv/

### 6. S2S Voice Assistant (user service)
   Port: 7860
   Description: Speech-to-Speech Voice AI — realtime voice interaction
   Path: /media/ryan/UbuntuDATA/AI_PROJECTS/s2s/hf-realtime-voice/
   Venv: /media/ryan/UbuntuDATA/AI_PROJECTS/s2s/venv/
   Features: DuckDuckGo search fallback, CUDA-accelerated

### 7. UbuntuConsole WebUI (user service)
   Port: 9002
   Description: Local services dashboard — monitors & manages all services
   Path: /media/ryan/UbuntuDATA/AI_PROJECTS/UbuntuConsole/
   Venv: ~/.hermes/hermes-agent/venv/

### 8. Camofox Browser (user service)
   Port: internal (CDP)
   Description: Anti-detection browser for Hermes Agent web automation
   Path: /media/ryan/UbuntuDATA/AI_PROJECTS/camofox-browser/
   Wrapper: /media/ryan/UbuntuDATA/bin/camofox-wrapper.sh
   Node: ~/.hermes/node/

### 9. RustDesk (system service)
   Port: internal
   Description: Remote desktop — unattended access to this machine + control of other devices
   Service: /etc/systemd/system/rustdesk.service

### 10. Docker Containers
    a. Firecrawl Stack (port 3002)
       - API server, Playwright scraper, Redis, RabbitMQ, Postgres
       - Web scraping / crawling engine
       - Config: /media/ryan/UbuntuDATA/AI_PROJECTS/firecrawl/fc-src/docker-compose.yaml
       - Uses pre-built ghcr.io images
    b. Telegram Bot API (port 9090)
       - aiogram/telegram-bot-api container
       - For Telegram bot integration

### 11. Docker Engine (system service)
    - docker.io + docker-compose-v2
    - User ryan in docker group (newgrp docker workaround needed)

========================================
## PORT MAP
========================================
  3002  Firecrawl API (Docker)
  5900  RustDesk VNC
  7860  S2S Voice Assistant
  8188  ComfyUI
  8888  llama.cpp GPU (Qwen3.6-27B)
  8889  llama.cpp CPU (Gemma-4-E4B)
  9002  UbuntuConsole Dashboard
  9090  Telegram Bot API (Docker)
  9277  SillyTavern

========================================
## HERMES AGENT CONFIG
========================================
  Profile: default
  Model: Qwen3.6-27B-UD-Q4_K_XL via llama.cpp:8888 (custom provider)
  Provider: custom (local GGUF inference)
  Web backend: firecrawl
  Browser: camofox (local)
  Skills: full skill tree loaded (autonomous-ai, creative, data-science, devops, mlops, etc.)
  Personalities: 15 built-in (helpful, concise, kawaii, pirate, shakespeare, etc.)

========================================
## DIRECTORY STRUCTURE
========================================
  /media/ryan/UbuntuDATA/     <-- ext4 数据盘（/dev/sda1），系统重装不丢
    AI_PROJECTS/
      SillyTavern-home/    — SillyTavern (port 9277)
      SillyTavern-voice-test/ — SillyTavern voice test (port 9001)
      llama.cpp/           — llama.cpp (GPU build + CPU build)
      firecrawl/fc-src/    — Firecrawl docker-compose
      s2s/                 — S2S voice assistant
      UbuntuConsole/       — Dashboard
      camofox-browser/     — Anti-detection browser
      RyAgent/             — GitHub repo mirror
      searxng/             — Search engine
    ai_models/             — GGUF model files
    bin/                   — Utility scripts (camofox-wrapper.sh)
    ComfyUI/               — ComfyUI main directory
    VM_Marvis/             — KVM VM disk (marvis-box Tiny11)
    backup/
      home-config/         <-- 系统重装前运行 backup-ryan-station.sh 备份到这里
        .ssh/              — SSH keys (GitHub + VPS + VM)
        ibus/              — Rime 输入法配置
        rustdesk/          — RustDesk 配置（ID, peers, 密码）
        gh/                — GitHub auth token
        gnome/             — GNOME 桌面设置
        .bashrc            — 终端配置
        .profile           — 登录配置
        .env               — Hermes 环境变量
  /home/ryan/.hermes/      — Hermes Agent config, skills, memories

========================================
## 重装恢复流程
========================================
1. 安装 Ubuntu 24.04 + 桌面
2. 挂载 /dev/sda1 到 /media/ryan/UbuntuDATA
3. 运行: `bash ~/setup-ryan-station.sh`
   — 自动恢复：NVIDIA 驱动 + CUDA、SSH key、Rime、RustDesk、
     GitHub auth、WiFi、GNOME 设置
   — 自动安装：所有系统包 + KVM + Docker + 所有服务
   — 自动编译：llama.cpp GPU + CPU
   — 自动注册：marvis-box KVM VM（磁盘在 UbuntuDATA）
   — 模型文件不需要下载（已在 UbuntuDATA/ai_models）

⚠ 重装前必须运行: `bash ~/backup-ryan-station.sh`
   把 ~/.ssh, ~/.config/ibus, ~/.config/rustdesk, .bashrc, .env
   全部备份到 /media/ryan/UbuntuDATA/backup/home-config/
