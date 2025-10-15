# Price Publisher Kubernetes Deployment

This document describes the deployment setup for the Price Publisher application to your k3s cluster.

## Architecture Overview

The deployment follows these best practices:
- **Namespace Isolation**: Uses dedicated `price-publisher` namespace
- **Kustomize Structure**: Base configuration with dev/prod overlays
- **Leader Election**: Supports multiple replicas with leader election for high availability
- **Security**: Non-root containers, minimal privileges, security contexts
- **Monitoring**: Health checks and resource limits
- **Service Discovery**: Internal ClusterIP and external NodePort services

## Cluster Information

- **Master Node**: k3s.ahurix.com
- **Worker Node**: dev.ahurix.com
- **Namespace**: price-publisher
- **External Access**: NodePort 30555

## Directory Structure

```
k8s/
├── base/                          # Base Kubernetes manifests
│   ├── namespace.yaml            # Namespace definition
│   ├── rbac.yaml                 # ServiceAccount and RBAC for leader election
│   ├── configmap.yaml            # Application configuration
│   ├── deployment.yaml           # Main deployment manifest
│   ├── service.yaml              # Service definitions
│   └── kustomization.yaml        # Base kustomization
└── overlays/
    ├── dev/                      # Development environment
    │   ├── kustomization.yaml    # Dev overlay configuration
    │   └── deployment-patch.yaml # Dev-specific patches
    └── prod/                     # Production environment
        ├── kustomization.yaml    # Prod overlay configuration
        └── deployment-patch.yaml # Prod-specific patches
```

## Environment Configurations

### Development (dev.ahurix.com)
- **Replicas**: 1
- **Resources**: 64Mi RAM, 50m CPU (requests), 256Mi RAM, 200m CPU (limits)
- **Node Selection**: Targets worker node (dev.ahurix.com)
- **Image Tag**: latest

### Production (k3s.ahurix.com)
- **Replicas**: 2 (High Availability)
- **Resources**: 128Mi RAM, 100m CPU (requests), 512Mi RAM, 500m CPU (limits)
- **Anti-Affinity**: Spreads pods across nodes
- **Image Tag**: Specific version (1.0.0)

## Prerequisites

1. **kubectl** configured with access to your k3s cluster
2. **kustomize** (included in kubectl v1.14+)
3. GitLab CI variables configured:
   - `KUBE_CONFIG_DEV`: Base64-encoded kubeconfig for development
   - `KUBE_CONFIG_PROD`: Base64-encoded kubeconfig for production

## Setting Up GitLab CI Variables

1. Get your kubeconfig from the k3s master node:
   ```bash
   # On k3s.ahurix.com
   sudo cat /etc/rancher/k3s/k3s.yaml
   ```

2. Update the server URL in the kubeconfig to use the external IP:
   ```yaml
   server: https://k3s.ahurix.com:6443
   ```

3. Base64 encode the kubeconfig:
   ```bash
   cat kubeconfig.yaml | base64 -w 0
   ```

4. Add to GitLab CI/CD variables:
   - Variable: `KUBE_CONFIG_DEV`
   - Value: [base64-encoded kubeconfig]
   - Type: Variable
   - Protected: Yes
   - Masked: Yes

## Deployment Methods

### 1. GitLab CI/CD (Recommended)

The deployment happens automatically through GitLab CI/CD:

- **Development**: Triggered manually on `develop` or `main` branches
- **Production**: Triggered manually on `main` branch only

Deployments are manual by default for safety.

### 2. Manual Deployment using Makefile

```bash
# Validate manifests
make k8s-validate

# Deploy to development
make deploy-dev

# Deploy to production  
make deploy-prod

# Check status
make k8s-status

# View logs
make k8s-logs
```

### 3. Direct kubectl Commands

```bash
# Deploy to development
kubectl apply -k k8s/overlays/dev

# Deploy to production
kubectl apply -k k8s/overlays/prod

# Check deployment status
kubectl rollout status deployment/price-publisher -n price-publisher

# View pods
kubectl get pods -n price-publisher -o wide

# View logs
kubectl logs -f deployment/price-publisher -n price-publisher
```

## Services and Endpoints

### Internal Service (ClusterIP)
- **Name**: `price-publisher`
- **Port**: 5555
- **Type**: ClusterIP
- **Purpose**: Internal cluster communication

### External Service (NodePort)
- **Name**: `price-publisher-external`
- **Port**: 5555
- **NodePort**: 30555
- **Type**: NodePort
- **Access**: 
  - Development: `dev.ahurix.com:30555`
  - Production: `k3s.ahurix.com:30555`

## Configuration

Application configuration is managed through the `price-publisher-config` ConfigMap:

```yaml
EXCHANGE_NAME: "bingx"
ZMQ_PUSH_ENDPOINT: "tcp://127.0.0.1:6001"
TRADING_PAIRS: "XAUT-USDT,DOGE-USDT,ETH-USDT,BTC-USDT"
BINGX_SPOT_PAIRS: "USDC-USDT,EUR-USDT"
URL_BINGX: "wss://open-api-swap.bingx.com/swap-market"
TZ: "Asia/Tehran"
```

To update configuration:
1. Edit `k8s/base/configmap.yaml`
2. Apply changes: `kubectl apply -k k8s/overlays/[env]`
3. Restart deployment: `kubectl rollout restart deployment/price-publisher -n price-publisher`

## Leader Election

The application supports leader election for high availability:
- Uses Kubernetes `coordination.k8s.io/v1` leases
- Only one pod publishes data at a time
- Automatic failover if leader pod fails
- Requires RBAC permissions (included in `rbac.yaml`)

## Monitoring and Health Checks

### Health Checks
- **Liveness Probe**: Checks if main process is running
- **Readiness Probe**: Checks if application is ready to serve traffic
- **Probe Method**: Process check via shell command

### Resource Monitoring
- **CPU and Memory Limits**: Prevents resource starvation
- **Resource Requests**: Ensures proper scheduling
- **Restart Policy**: Always restart on failure

## Troubleshooting

### Check Pod Status
```bash
kubectl get pods -n price-publisher
kubectl describe pod <pod-name> -n price-publisher
```

### View Logs
```bash
kubectl logs -f deployment/price-publisher -n price-publisher
kubectl logs <pod-name> -n price-publisher --previous  # Previous container logs
```

### Check Events
```bash
kubectl get events -n price-publisher --sort-by='.lastTimestamp'
```

### Debug Configuration
```bash
kubectl get configmap price-publisher-config -n price-publisher -o yaml
kubectl describe deployment price-publisher -n price-publisher
```

### Service Connectivity
```bash
# Test internal service
kubectl run test-pod --rm -i --tty --image=busybox -- sh
# Inside the pod:
nc -v price-publisher.price-publisher.svc.cluster.local 5555

# Test external service
nc -v dev.ahurix.com 30555  # or k3s.ahurix.com 30555
```

## Security Considerations

1. **Non-root containers**: Runs as user 1000
2. **Read-only root filesystem**: Prevents container modification
3. **Dropped capabilities**: Minimal container privileges
4. **Security contexts**: Enforces security policies
5. **Network policies**: Consider implementing network segmentation
6. **RBAC**: Minimal permissions for leader election

## Scaling

### Horizontal Scaling
```bash
# Scale development
kubectl scale deployment price-publisher --replicas=2 -n price-publisher

# Scale production (already configured for 2 replicas)
kubectl scale deployment price-publisher --replicas=3 -n price-publisher
```

### Vertical Scaling
Update resource limits in the overlay patches and redeploy.

## Backup and Recovery

### Configuration Backup
```bash
kubectl get all,configmap,secret -n price-publisher -o yaml > price-publisher-backup.yaml
```

### Recovery
```bash
kubectl apply -f price-publisher-backup.yaml
```

## Updates and Rollbacks

### Rolling Update
```bash
# Update image tag in kustomization.yaml, then:
kubectl apply -k k8s/overlays/prod
kubectl rollout status deployment/price-publisher -n price-publisher
```

### Rollback
```bash
kubectl rollout undo deployment/price-publisher -n price-publisher
kubectl rollout status deployment/price-publisher -n price-publisher
```

## Support

For issues with the deployment:
1. Check pod logs and events
2. Verify configuration in ConfigMap
3. Ensure proper RBAC permissions
4. Validate network connectivity
5. Check resource availability on nodes