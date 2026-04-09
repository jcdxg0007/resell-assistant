#!/bin/bash
set -e

DIR="$(dirname "$0")/sealos"

echo "=== Applying Sealos K8s Resources ==="

kubectl apply -f "$DIR/namespace.yaml"
kubectl apply -f "$DIR/secret.yaml"
kubectl apply -f "$DIR/configmap.yaml"
kubectl apply -f "$DIR/pvc.yaml"
kubectl apply -f "$DIR/backend-deployment.yaml"
kubectl apply -f "$DIR/celery-deployment.yaml"
kubectl apply -f "$DIR/frontend-deployment.yaml"
kubectl apply -f "$DIR/ingress.yaml"

echo ""
echo "=== Deployment Applied ==="
echo "Check status:"
echo "  kubectl -n resell-assistant get pods"
echo "  kubectl -n resell-assistant get svc"
echo "  kubectl -n resell-assistant logs -f deployment/resell-backend"
