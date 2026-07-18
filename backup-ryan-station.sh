#!/bin/bash
###############################################################################
# Backup home config to UbuntuDATA (run before reformatting)
###############################################################################
set -euo pipefail
G='\033[0;32m'; Y='\033[1;33m'; NC='\033[0m'
BACKUP="/media/ryan/UbuntuDATA/backup/home-config"

log() { echo -e "${G}[BACKUP]${NC} $*"; }
warn() { echo -e "${Y}[WARN ]${NC} $*"; }

mkdir -p "$BACKUP"

log "Backing up SSH keys..."
cp -r ~/.ssh "$BACKUP/" 2>/dev/null || warn "No .ssh found"

log "Backing up Rime input config..."
cp -r ~/.config/ibus "$BACKUP/" 2>/dev/null || warn "No ibus config found"

log "Backing up RustDesk config..."
cp -r ~/.config/rustdesk "$BACKUP/" 2>/dev/null || warn "No rustdesk config found"

log "Backing up .bashrc..."
cp ~/.bashrc "$BACKUP/" 2>/dev/null || true

log "Backing up .profile..."
cp ~/.profile "$BACKUP/" 2>/dev/null || true

log "Backing up Hermes .env..."
cp ~/.hermes/.env "$BACKUP/" 2>/dev/null || warn "No .hermes/.env found"

log "Backing up GitHub auth..."
mkdir -p "$BACKUP/gh"
cp ~/.config/gh/hosts.yml "$BACKUP/gh/" 2>/dev/null || warn "No gh auth found"

log "Backing up NetworkManager WiFi..."
sudo cp /etc/NetworkManager/system-connections/* "$BACKUP/" 2>/dev/null || warn "No WiFi config or need sudo"

log "Backing up GNOME settings..."
mkdir -p "$BACKUP/gnome"
gsettings dump org.gnome.desktop.lockdown > "$BACKUP/gnome/lockdown.xml" 2>/dev/null || true
gsettings dump org.gnome.desktop.session > "$BACKUP/gnome/session.xml" 2>/dev/null || true

log "All backed up to $BACKUP"
ls -la "$BACKUP"