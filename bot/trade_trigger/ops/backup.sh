#!/bin/bash
# QuantumAlpha — daily SQLite backup
#
# Backs up all critical SQLite DBs using sqlite3 .backup (atomic, WAL-safe).
# Keeps 30 days of local copies. Can be extended for off-site (Hetzner Storage Box).
#
# Install:
#   chmod +x ops/backup.sh
#   sudo cp ops/qa-backup.timer ops/qa-backup.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now qa-backup.timer
#   systemctl list-timers qa-backup.timer    # verify schedule
#
# Test once manually:
#   /home/qa/quantumalpha/ops/backup.sh
#
# Restore example:
#   cp /home/qa/backups/20260506_030000/funding.db /home/qa/quantumalpha/data/

set -euo pipefail

PROJECT=/home/qa/quantumalpha
BACKUP_ROOT=/home/qa/backups
DATE=$(date -u +%Y%m%d_%H%M%SZ)
DEST="$BACKUP_ROOT/$DATE"
LOG="$BACKUP_ROOT/backup.log"

mkdir -p "$DEST"
mkdir -p "$BACKUP_ROOT"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"
}

log "===== Backup START → $DEST ====="

# Each DB. sqlite3 .backup is atomic and safe with WAL writers.
for db in funding.db pnl.db trade_trigger.db; do
    src="$PROJECT/data/$db"
    if [[ -f "$src" ]]; then
        dst="$DEST/$db"
        if sqlite3 "$src" ".backup '$dst'"; then
            size=$(du -h "$dst" | awk '{print $1}')
            log "  ✓ $db → $size"
        else
            log "  ✗ $db FAILED"
        fi
    else
        log "  - $db not found, skipping"
    fi
done

# .env (sensitive — chmod 600 immediately)
if [[ -f "$PROJECT/.env" ]]; then
    cp "$PROJECT/.env" "$DEST/.env"
    chmod 600 "$DEST/.env"
    log "  ✓ .env"
fi

# Git HEAD reference (so we know what code state was running)
if [[ -d "$PROJECT/.git" ]]; then
    (cd "$PROJECT" && git rev-parse HEAD > "$DEST/git_head.txt") || true
    (cd "$PROJECT" && git status -s > "$DEST/git_status.txt") || true
fi

# Cleanup: keep last 30 days
PRUNED=$(find "$BACKUP_ROOT" -maxdepth 1 -type d -name '20*' -mtime +30 -exec rm -rf {} + -print | wc -l)
if [[ "$PRUNED" -gt 0 ]]; then
    log "Pruned $PRUNED old backups (>30 days)"
fi

# Total size
TOTAL=$(du -sh "$BACKUP_ROOT" | awk '{print $1}')
log "===== Backup OK. Total local backups: $TOTAL ====="

# OPTIONAL: off-site sync to Hetzner Storage Box (rclone or rsync)
# Uncomment after configuring rclone:
#
# if command -v rclone >/dev/null; then
#     rclone copy "$DEST" hetzner-box:qa-backups/$DATE/ --quiet
#     log "Off-site sync OK"
# fi
