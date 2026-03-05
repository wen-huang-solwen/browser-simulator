#!/usr/bin/env bash
# Setup script for deploying the scraper on a Linux server (Ubuntu/Debian).
# Run as root or with sudo.
set -euo pipefail

echo "=== Installing system dependencies ==="

# Update package list
apt-get update

# Install Xvfb (virtual display for Chrome)
apt-get install -y xvfb

# Install Chrome (if not already installed)
if ! command -v google-chrome &> /dev/null; then
    echo "=== Installing Google Chrome ==="
    apt-get install -y wget gnupg2
    wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
    apt-get update
    apt-get install -y google-chrome-stable
else
    echo "Google Chrome already installed: $(google-chrome --version)"
fi

# Install Python dependencies
echo "=== Installing Python dependencies ==="
pip install -r requirements.txt
playwright install-deps
playwright install chromium

echo ""
echo "=== Setup complete ==="
echo "Chrome: $(google-chrome --version 2>/dev/null || echo 'not found')"
echo "Xvfb:   $(Xvfb -version 2>&1 | head -1 || echo 'not found')"
echo ""
echo "Next steps:"
echo "  1. Copy your TikTok session from local machine:"
echo "     scp .auth/tk_session.json user@server:$(pwd)/.auth/"
echo "  2. Run the scraper:"
echo "     python main.py username --platform tiktok --max-reels 50"
