#!/bin/bash
set -e

REGISTRY="crpi-ryxzfb3l96vqk28a.cn-shenzhen.personal.cr.aliyuncs.com/resell-assistant"
TAG="${1:-latest}"

echo "=== 构建并推送镜像到阿里云 ACR ==="
echo "Registry: $REGISTRY"
echo "Tag: $TAG"
echo ""

echo "[1/2] 构建后端镜像..."
cd "$(dirname "$0")/../backend"
docker build -t "$REGISTRY/backend:$TAG" .
echo "✓ 后端镜像构建完成"

echo ""
echo "[2/2] 构建前端镜像..."
cd "$(dirname "$0")/../frontend"
docker build -t "$REGISTRY/frontend:$TAG" .
echo "✓ 前端镜像构建完成"

echo ""
echo "=== 推送镜像 ==="
docker push "$REGISTRY/backend:$TAG"
docker push "$REGISTRY/frontend:$TAG"

echo ""
echo "=== 完成！==="
echo "后端: $REGISTRY/backend:$TAG"
echo "前端: $REGISTRY/frontend:$TAG"
echo ""
echo "接下来执行: bash deploy/apply-sealos.sh"
