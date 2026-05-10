#!/bin/bash
# Deploy peepos-reclaimer to VPS
# Usage: ./deploy.sh

set -e

VPS="root@187.77.215.240"
REMOTE_DIR="/opt/peepos-reclaimer"
SERVICE="peepos-reclaimer"

echo "📦 Syncing files..."
rsync -av --exclude='.git' --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' --exclude='venv' \
  ./ "$VPS:$REMOTE_DIR/"

echo "📥 Installing dependencies..."
ssh "$VPS" "cd $REMOTE_DIR && venv/bin/pip install -r requirements.txt -q"

echo "🔄 Restarting service..."
ssh "$VPS" "systemctl restart $SERVICE"

echo "✅ Checking status..."
ssh "$VPS" "systemctl is-active $SERVICE && echo 'Bot is running!' || echo 'WARNING: Service failed to start'"
