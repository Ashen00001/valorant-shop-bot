#!/bin/bash
# Run this once on the VM after cloning the repo.
# Usage: bash deploy/setup_vps.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="$REPO_DIR/deploy/shopbot.service"

echo "=== Valorant Shop Bot — VPS setup ==="
echo "Repo: $REPO_DIR"
echo

# Dependencies
echo "[1/4] Installing Python deps..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip
pip3 install -q -r "$REPO_DIR/requirements.txt"
echo "      Done."

# config.env
if [ ! -f "$REPO_DIR/config.env" ]; then
    cp "$REPO_DIR/config.env.example" "$REPO_DIR/config.env"
    echo
    echo "[2/4] config.env created — fill it in now:"
    echo "      nano $REPO_DIR/config.env"
    echo
    echo "      Add:"
    echo "        DISCORD_BOT_TOKEN=your_token_here"
    echo "        DISCORD_CHANNEL_ID=your_channel_id_here"
    echo "        RIOT_REGION=na"
    echo
    read -p "      Press Enter when done..."
else
    echo "[2/4] config.env already exists — skipping."
fi

# systemd service
echo "[3/4] Installing systemd service..."
sudo cp "$SERVICE_FILE" /etc/systemd/system/shopbot.service
sudo systemctl daemon-reload
sudo systemctl enable shopbot
echo "      Service installed and enabled."

# Accounts
echo
echo "[4/4] Add accounts (one per Discord user)."
echo "      You need each person's Discord user ID and their Riot credentials."
echo "      Run: python3 $REPO_DIR/setup_account.py <discord_user_id> [region]"
echo "      Example: python3 $REPO_DIR/setup_account.py 123456789012345678 na"
echo

read -p "Add an account now? [y/N] " yn
while [[ "$yn" =~ ^[Yy]$ ]]; do
    read -p "Discord user ID: " did
    read -p "Region (na/eu/ap/kr): " region
    python3 "$REPO_DIR/setup_account.py" "$did" "${region:-na}"
    read -p "Add another? [y/N] " yn
done

# Start
echo
echo "Starting bot..."
sudo systemctl start shopbot
sleep 2
sudo systemctl status shopbot --no-pager

echo
echo "=== Done ==="
echo "Useful commands:"
echo "  sudo systemctl status shopbot     — check if running"
echo "  sudo journalctl -u shopbot -f     — live logs"
echo "  sudo systemctl restart shopbot    — restart after changes"
echo "  sudo systemctl stop shopbot       — stop"
