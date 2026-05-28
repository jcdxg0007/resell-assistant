#!/usr/bin/env bash
# 一键备份 resell-assistant 全项目快照（源码 + Postgres 数据库）。
#
# 用法（在 Sealos devbox 上）：
#   bash scripts/backup_now.sh
#
# 输出：
#   /home/devbox/project/backups/resell-backup-YYYYMMDD-HHMMSS.tar.gz
#
# 下载到家里 Windows PC：
#   方法 A（Cursor 文件树）：左侧文件树展开 backups/ → 右键 .tar.gz → Download
#   方法 B（命令行）：如果有 SSH，scp 即可
#
# 频率建议：每周跑一次，重大变更前手动跑一次。
# 数据安全：备份里包含数据库明文（accounts 表有 cookie），不要上传公开云盘。

set -euo pipefail

# ─── 配置 ───────────────────────────────────────────────────────────────
NS="ns-3zn44u6p"
PG_POD="resell-manager-postgresql-0"
PG_CONTAINER="postgresql"
PG_DATABASE="postgres"
PROJECT_ROOT="/home/devbox/project"
BACKUP_ROOT="${PROJECT_ROOT}/backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
STAGE_DIR="${BACKUP_ROOT}/.staging-${TIMESTAMP}"
TARBALL="${BACKUP_ROOT}/resell-backup-${TIMESTAMP}.tar.gz"

mkdir -p "${BACKUP_ROOT}"
mkdir -p "${STAGE_DIR}"

# 备份完成或失败都清 stage 目录
trap "rm -rf '${STAGE_DIR}'" EXIT

echo "================================================"
echo "  Resell Assistant 完整备份"
echo "  时间戳: ${TIMESTAMP}"
echo "  输出:   ${TARBALL}"
echo "================================================"
echo

# ─── 1. 源码 ─────────────────────────────────────────────────────────────
echo "[1/4] 源码（docs + backend + worker + deploy + frontend src）..."
SOURCE_TGZ="${STAGE_DIR}/source.tar.gz"
tar -czf "${SOURCE_TGZ}" \
    --exclude='**/node_modules' \
    --exclude='**/__pycache__' \
    --exclude='**/*.pyc' \
    --exclude='**/.venv' \
    --exclude='**/playwright_states' \
    --exclude='**/backups' \
    --exclude='frontend/dist' \
    --exclude='backend/celerybeat-schedule.db' \
    --exclude='backend/logs' \
    -C "${PROJECT_ROOT}" \
    docs backend worker deploy scripts .gitignore .cursor 2>/dev/null || true
# frontend 单独压（很多文件，加个进度提示）
tar -czf "${STAGE_DIR}/frontend-src.tar.gz" \
    --exclude='**/node_modules' \
    --exclude='**/dist' \
    --exclude='**/*.log' \
    -C "${PROJECT_ROOT}" \
    frontend 2>/dev/null || true
echo "  → source.tar.gz       $(du -h ${SOURCE_TGZ} | cut -f1)"
echo "  → frontend-src.tar.gz $(du -h ${STAGE_DIR}/frontend-src.tar.gz | cut -f1)"
echo

# ─── 2. Postgres pg_dump ──────────────────────────────────────────────────
echo "[2/4] Postgres pg_dump（流式 → gzip）..."
DUMP_FILE="${STAGE_DIR}/postgres-${PG_DATABASE}.sql.gz"
# pg_dump 在 postgres pod 内跑，输出到 stdout，通过 kubectl exec 流回 devbox，
# 本地 gzip 压缩。这样不在 pod 内落盘，省 pod 临时空间。
# -Fc 自定义格式更紧凑且支持选择性恢复，但需要 pg_restore；这里用纯 SQL plain
# 格式方便用 psql 直接 import。
if kubectl -n "${NS}" exec "${PG_POD}" -c "${PG_CONTAINER}" -- \
        pg_dump -U postgres -d "${PG_DATABASE}" --no-owner --no-privileges 2>/dev/null \
        | gzip -6 > "${DUMP_FILE}"; then
    echo "  → $(du -h ${DUMP_FILE} | cut -f1)"
else
    echo "  ❌ pg_dump 失败，写一个空文件占位"
    : > "${DUMP_FILE}"
fi
echo

# ─── 3. backend 运行时环境变量（DB 连接串、密钥等）─────────────────────
echo "[3/4] backend 运行时 env（敏感，备份不上云）..."
ENV_FILE="${STAGE_DIR}/backend-runtime.env"
if kubectl -n "${NS}" exec deploy/backend -- env 2>/dev/null \
        | grep -iE '^(DATABASE_URL|REDIS_URL|RESELL_|PDD_WORKER_|JWT_|SECRET_|CRYPT_|API_|ALIBABA_|TAOBAO_|XIANYU_)' \
        | sort > "${ENV_FILE}"; then
    line_count=$(wc -l < "${ENV_FILE}")
    echo "  → backend-runtime.env (${line_count} 个 env 变量)"
else
    echo "  ❌ 拉 backend env 失败"
    : > "${ENV_FILE}"
fi
echo

# ─── 4. Git 状态快照（哪个 commit、有没有未提交改动）───────────────────
echo "[4/4] Git 状态 + 当前 commit..."
META_FILE="${STAGE_DIR}/git-meta.txt"
{
    echo "=== 备份元信息 ==="
    echo "Timestamp:    ${TIMESTAMP}"
    echo "Hostname:     $(hostname)"
    echo "Backup user:  $(whoami)"
    echo
    echo "=== Git ==="
    cd "${PROJECT_ROOT}"
    echo "Current branch: $(git branch --show-current 2>/dev/null || echo 'unknown')"
    echo "Head commit:    $(git rev-parse HEAD 2>/dev/null || echo 'unknown')"
    echo "Head message:   $(git log -1 --format='%s' 2>/dev/null || echo 'unknown')"
    echo "Remote:         $(git remote get-url origin 2>/dev/null | sed 's|//[^@]*@|//<token>@|' || echo 'unknown')"
    echo
    echo "=== 未提交改动 (git status -s) ==="
    git status -s 2>/dev/null || echo '(无法读取 git 状态)'
    echo
    echo "=== Sealos pods 状态 ==="
    kubectl -n "${NS}" get pods 2>&1 | grep -v Warning
} > "${META_FILE}"
echo "  → $(wc -l < ${META_FILE}) 行元信息"
echo

# ─── 写顶层 README ──────────────────────────────────────────────────────
cat > "${STAGE_DIR}/README.txt" <<EOF
================================================================
  Resell Assistant 完整备份  ${TIMESTAMP}
================================================================

内容清单：
  source.tar.gz        docs + backend + worker + deploy + scripts + .gitignore
  frontend-src.tar.gz  frontend src（不含 node_modules，重建时 npm install）
  postgres-postgres.sql.gz  Postgres 数据库 pg_dump（全部表 schema + 数据）
  backend-runtime.env  backend pod 当前的运行时 env（含 DATABASE_URL/密钥）
  git-meta.txt         本次备份时的 git commit + pod 状态快照

【恢复步骤】

1) 解压到任意目录：
     tar -xzf resell-backup-${TIMESTAMP}.tar.gz
     cd .staging-${TIMESTAMP}

2) 恢复源码：
     tar -xzf source.tar.gz -C /path/to/restore
     tar -xzf frontend-src.tar.gz -C /path/to/restore

3) 恢复数据库（先停 backend + celery 防写入冲突）：
     gunzip -c postgres-postgres.sql.gz | psql -U postgres -d postgres

4) 拿回 env：
     cat backend-runtime.env 找 DATABASE_URL / WORKER_TOKEN 等粘到 backend/.env

【安全须知】

⚠️ backend-runtime.env 含明文密钥
⚠️ postgres-postgres.sql.gz 含 accounts 表（cookie/手机号/账号绑定信息）

绝不要：
  - 上传到公开云盘（公司 git / 公开 GitHub / 微信群文件 / QQ 邮箱临时网盘）
  - 通过未加密渠道传输（除非接收端是你自己的设备）

推荐：
  - 下载到家里 PC 后立刻加密：7z a -p<密码> -mhe=on encrypted.7z resell-backup-*.tar.gz
  - 加密后再考虑放阿里云盘等家用云
EOF

# ─── 打包 ───────────────────────────────────────────────────────────────
echo "[final] 打包 → ${TARBALL}..."
tar -czf "${TARBALL}" -C "${BACKUP_ROOT}" ".staging-${TIMESTAMP}"
echo
echo "================================================"
echo "  ✅ 备份完成"
echo "================================================"
echo "  文件: ${TARBALL}"
echo "  大小: $(du -h ${TARBALL} | cut -f1)"
echo
echo "  下载方式："
echo "    Cursor 文件树 → backups/ → 右键 .tar.gz → Download"
echo
echo "  历史备份："
ls -lh "${BACKUP_ROOT}"/*.tar.gz 2>/dev/null | awk '{print "    " $9 "  " $5 "  " $6 " " $7 " " $8}'
echo
echo "  下载到家里后建议加密保管："
echo "    7z a -p<password> -mhe=on encrypted.7z resell-backup-${TIMESTAMP}.tar.gz"
