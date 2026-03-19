#!/bin/bash
# Generate a kubeconfig for the sysop-bot ServiceAccount
# Usage: KUBECONFIG=/path/to/admin.yaml ./generate-kubeconfig.sh

set -euo pipefail

SA_NAME="sysop-bot"
SA_NAMESPACE="default"
OUTPUT="${1:-sysop-bot.kubeconfig}"

# Get cluster info from current context
CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA_DATA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

# Create a long-lived token secret (required since k8s 1.24+)
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: sysop-bot-token
  namespace: ${SA_NAMESPACE}
  annotations:
    kubernetes.io/service-account.name: ${SA_NAME}
type: kubernetes.io/service-account-token
EOF

# Wait for the token to be populated
echo "Waiting for token..."
for i in $(seq 1 10); do
  TOKEN=$(kubectl get secret sysop-bot-token -n "${SA_NAMESPACE}" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d) && break
  sleep 1
done

if [ -z "${TOKEN:-}" ]; then
  echo "ERROR: Failed to get token" >&2
  exit 1
fi

# Write kubeconfig
cat > "${OUTPUT}" <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ${CA_DATA}
    server: ${SERVER}
  name: ${CLUSTER_NAME}
contexts:
- context:
    cluster: ${CLUSTER_NAME}
    user: ${SA_NAME}
  name: ${SA_NAME}@${CLUSTER_NAME}
current-context: ${SA_NAME}@${CLUSTER_NAME}
users:
- name: ${SA_NAME}
  user:
    token: ${TOKEN}
EOF

echo "Kubeconfig written to ${OUTPUT}"
echo "Test with: KUBECONFIG=${OUTPUT} kubectl get nodes"
