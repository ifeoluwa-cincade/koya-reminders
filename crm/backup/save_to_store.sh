#!/usr/bin/env bash
# save_to_store.sh REMOTE BRANCH DAY FILE WORKDIR
# Puts FILE at backups/DAY.json.gz.enc on BRANCH of REMOTE and keeps only the last 30 days (DAY and the 29 before).
# The branch is rewritten as ONE commit holding only the kept files, so pruned backups also leave the history.
# Safe: a failed fetch stops before anything is pushed, and the push only replaces the exact commit that was read
# (--force-with-lease), so a concurrent save is never overwritten blindly. WORKDIR must not exist yet. Prints counts only.
set -euo pipefail
REMOTE="${1:?remote}"; BRANCH="${2:?branch}"; DAY="${3:?day}"; FILE="${4:?file}"; WORK="${5:?workdir}"
[[ "$DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "bad day"; exit 1; }
[ -e "$WORK" ] && { echo "workdir already exists"; exit 1; }
mkdir -p "$WORK" && cd "$WORK" && git init -q -b "$BRANCH"
git config user.name "backup-bot"; git config user.email "backup-bot@users.noreply.github.com"
git remote add origin "$REMOTE"
LEASE=()
before=0
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git fetch -q --depth 1 origin "$BRANCH"
  OLD=$(git rev-parse FETCH_HEAD)
  LEASE=("--force-with-lease=refs/heads/$BRANCH:$OLD")
  git checkout -q FETCH_HEAD -- .
  before=$(find backups -name '*.json.gz.enc' 2>/dev/null | wc -l | tr -d ' ')
else
  git ls-remote origin >/dev/null                 # the repository must answer; only the branch is new
fi
mkdir -p backups
cp "$FILE" "backups/$DAY.json.gz.enc"
CUT=$(python3 -c "import datetime,sys; print(datetime.date.fromisoformat(sys.argv[1]) - datetime.timedelta(days=29))" "$DAY")
pruned=0
for f in backups/*.json.gz.enc; do
  d=$(basename "$f" .json.gz.enc)
  if [[ "$d" < "$CUT" ]]; then git rm -q --cached --ignore-unmatch "$f" >/dev/null; unlink "$f"; pruned=$((pruned + 1)); fi
done
after=$(find backups -name '*.json.gz.enc' | wc -l | tr -d ' ')
printf 'Encrypted Supabase backups (openssl aes-256-cbc -pbkdf2, BACKUP_ENC_KEY; before 17 Oct 2026 ENC_CACHE_KEY), last 30 days.\nWritten by koya-reminders .github/workflows/supabase_backup.yml. Restore: crm/backup/supabase_backup.py there.\n' > README.txt
git add -A README.txt backups
TREE=$(git write-tree)
C=$(git commit-tree "$TREE" -m "Supabase backup $DAY")
git push -q ${LEASE[@]+"${LEASE[@]}"} origin "$C:refs/heads/$BRANCH"
echo "saved $DAY to branch $BRANCH: backups kept $after (had $before, pruned $pruned)"
