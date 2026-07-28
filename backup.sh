#!/usr/bin/env bash
# lifeplanner backup — daily off-machine tarball of data/ (disk-death insurance + history).
# set LIFEPLANNER_BACKUP_HOST to an ssh host (key auth) and run this on a daily timer;
# keeps the newest $LIFEPLANNER_BACKUP_KEEP (default 14) tarballs. no host set = no-op.
set -uo pipefail
HOST="${LIFEPLANNER_BACKUP_HOST:-}"
[ -z "$HOST" ] && exit 0
DIR="${LIFEPLANNER_BACKUP_DIR:-backups/lifeplanner}"
KEEP="${LIFEPLANNER_BACKUP_KEEP:-14}"
cd "$(dirname "$0")" || exit 1
TAR="$(mktemp -t lifeplanner-XXXXXX.tar.gz)"
tar -czf "$TAR" data || { rm -f "$TAR"; exit 1; }
ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" \
  "mkdir -p '$DIR' && chmod 700 '$DIR'" || { rm -f "$TAR"; exit 1; }
scp -o ConnectTimeout=8 -o BatchMode=yes -q "$TAR" "$HOST:$DIR/lifeplanner-$(date +%F).tar.gz" \
  || { rm -f "$TAR"; exit 1; }
ssh -o BatchMode=yes "$HOST" \
  "ls -t '$DIR'/lifeplanner-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --"
rm -f "$TAR"
