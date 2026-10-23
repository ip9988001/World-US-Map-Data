#!/usr/bin/env bash
# 一仓 NAD 本地矩阵：分批点火（52 州大矩阵在 GitHub 队列里会一直 pending，小批量立即调度）
# 用法：nohup bash ops_local_matrix_batches.sh >> /root/pi-work/logs/local_matrix_batches.log 2>&1 &
set -u
R=ip9988001/World-US-Map-Data
BATCH=9
mapfile -t arr < <(gh release list --repo "$R" --limit 200 --json tagName -q '.[].tagName|select(startswith("raw-US-"))')
total=${#arr[@]}
echo "$(date -u +%FT%TZ) 共 $total 个 tag，每批 $BATCH"
i=0
while [ "$i" -lt "$total" ]; do
  B="${arr[*]:$i:$BATCH}"
  # 等队列彻底空闲（最多等 60 分钟）
  for _ in $(seq 1 60); do
    n=$(gh api "repos/$R/actions/runs?per_page=20" -q '[.workflow_runs[]|select(.status=="in_progress" or .status=="queued" or .status=="pending")]|length' 2>/dev/null || echo 99)
    [ "$n" = "0" ] && break
    sleep 60
  done
  echo "$(date -u +%FT%TZ) [$((i+1))..$((i+BATCH))/$total] dispatch: $B"
  gh workflow run nad-local-matrix.yml --repo "$R" -f only="$B" 2>&1 | tail -1
  sleep 45
  # 等这批完成（最多 70 分钟）
  for _ in $(seq 1 70); do
    st=$(gh run list --repo "$R" --workflow nad-local-matrix.yml --limit 1 --json status -q '.[0].status' 2>/dev/null || echo unknown)
    [ "$st" = "completed" ] && break
    sleep 60
  done
  echo "$(date -u +%FT%TZ) 批次结束 status=$st"
  i=$((i+BATCH))
done
echo "$(date -u +%FT%TZ) 全部批次派发完毕"
