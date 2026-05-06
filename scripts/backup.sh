#!/bin/bash
# Phase A local backup: data/*.db + .env + tail of qa.log
# Stores 7 most recent backups in /home/qa/backups/
set -euo pipefail

QA_HOME="/home/qa/quantumalpha"
BACKUP_DIR="/home/qa/backups"
LOG_FILE="${BACKUP_DIR}/backup.log"
TIMESTAMP="$(date -u +%Y%m%d-%H%M)"
ARCHIVE="qa-backup-${TIMESTAMP}.tar.gz"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

log() {
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $*" >> "${LOG_FILE}"
}

log "START backup ${ARCHIVE}"

mkdir -p "${STAGE_DIR}/data" "${STAGE_DIR}/logs"

# Databases (use sqlite .backup if present? keep simple cp — bot uses WAL but
# files small and tarball captures the moment; restore is from-snapshot anyway)
cp "${QA_HOME}/data/funding.db" "${STAGE_DIR}/data/funding.db"
cp "${QA_HOME}/data/pnl.db"     "${STAGE_DIR}/data/pnl.db"

# Secrets (mode-700 dir, mode-600 archive — never leave $BACKUP_DIR)
cp "${QA_HOME}/.env" "${STAGE_DIR}/.env"

# Log tail (last 1000 lines — full log can grow to many MB)
tail -n 1000 "${QA_HOME}/logs/qa.log" > "${STAGE_DIR}/logs/qa.log.tail"

tar -czf "${BACKUP_DIR}/${ARCHIVE}" -C "${STAGE_DIR}" .
chmod 600 "${BACKUP_DIR}/${ARCHIVE}"

SIZE="$(du -h "${BACKUP_DIR}/${ARCHIVE}" | cut -f1)"
log "OK ${ARCHIVE} size=${SIZE}"

# Retention: keep newest 7
cd "${BACKUP_DIR}"
OLD_COUNT="$(ls -1t qa-backup-*.tar.gz 2>/dev/null | tail -n +8 | wc -l)"
if [ "${OLD_COUNT}" -gt 0 ]; then
    ls -1t qa-backup-*.tar.gz | tail -n +8 | xargs -r rm -f
    log "PRUNED ${OLD_COUNT} old backup(s)"
fi

log "DONE backup ${ARCHIVE}"
