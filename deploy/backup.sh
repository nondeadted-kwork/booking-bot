#!/usr/bin/env bash
# Бэкап SQLite без остановки бота. В cron: 0 4 * * * /opt/booking-bot/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
sqlite3 data/bot.db ".backup 'backups/bot-$(date +%F).db'"
find backups -name 'bot-*.db' -mtime +14 -delete
