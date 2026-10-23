#!/usr/bin/env bash
# 本机临时残留清理 + 安全护栏自检（只读检查 + 删临时文件，不动任何仓库数据）
#
# 背景：2026-10-23 NAD 核查在本机跑过 12 GB（破例），脚本被 kill 时可能有残留包占着磁盘。
# 用法：bash tools/cleanup_tmp.sh          # 只看，不删
#       bash tools/cleanup_tmp.sh --apply  # 真删
set -u
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

echo "=== 磁盘 ==="
df -h / | tail -1
echo "=== 内存 ==="
free -m | head -2
echo "=== 本机残留候选（>10MB 的下载包/临时目录）==="
find /tmp -maxdepth 2 \( -name "NAD-US-*.ndjson.gz" -o -name "OVERTURE-*.parquet" \
      -o -name "OA-US-*.zip" -o -name "TIGER-US-*.zip" -o -name "*.parquet" \) \
      -size +10M 2>/dev/null | head -20
echo "--- 空/小临时目录 ---"
find /tmp -maxdepth 1 -type d -name "tmp*" -mtime +0 2>/dev/null | head -10
echo "=== 体积 TOP ==="
du -sh /tmp/* 2>/dev/null | sort -rh | head -8

if [ "$APPLY" = "1" ]; then
  echo "=== 执行清理 ==="
  find /tmp -maxdepth 2 \( -name "NAD-US-*.ndjson.gz" -o -name "OVERTURE-*.parquet" \
        -o -name "OA-US-*.zip" -o -name "TIGER-US-*.zip" \) -size +10M -print -delete 2>/dev/null | head -20
  find /tmp -maxdepth 1 -type d -name "tmp*" -mtime +0 -exec rm -rf {} + 2>/dev/null
  echo "清理完成"; df -h / | tail -1
else
  echo
  echo "（未删除任何文件；确认后加 --apply）"
fi

echo "=== STOP 开关 ==="
if [ -e /root/pi-work/STOP ]; then
  echo "⚠️  /root/pi-work/STOP 存在 —— 所有带护栏的脚本都会拒绝启动"
  echo "   删除它才会恢复：rm -f /root/pi-work/STOP"
else
  echo "✅ 未设置（正常）"
  echo "   需要紧急停机时：touch /root/pi-work/STOP"
fi
