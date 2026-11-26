#!/bin/sh
# Deletes the files n8n keeps in executions/temp/binary_data once they are older than 90 minutes.
#
# A run started by a trigger or a schedule (Gmail trigger, Drive listing, re-check) stores its
# downloaded attachments there, and n8n never moves or deletes them: deleting or pruning the run
# removes only executions/<id>/. Nothing reads them once the run is over, except the editor
# showing that old run's attachment (the core keeps its own copy of every processed file).
#
# On the server it runs every 15 minutes from cron (docs/deployment.md):
#   scripts/purge_n8n_temp_files.sh
set -eu
cd "$(dirname "$0")/.."

docker compose exec -T n8n find /home/node/.n8n/storage/workflows \
  -path '*/executions/temp/binary_data/*' -type f -mmin +90 -print -delete | wc -l \
  | xargs printf '%s n8n temp files deleted (older than 90 minutes)\n'
