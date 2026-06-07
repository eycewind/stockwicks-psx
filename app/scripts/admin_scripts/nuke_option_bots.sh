#!/bin/bash

echo "🔥 Nuking all option bots (bot config + open + history)..."
python3 /var/www/stockwicks/app/utils/db_cleanup_paper_bot.py \
  nuke-all \
  --type option \
  --mode bot-only \
  --yes \
