# HCI Migration Tool

Migration tool for moving Virtual Machines from Nutanix AHV to Harvester HCI.

## Overview

This tool provides an end-to-end workflow for migrating VMs between hypervisors with minimal downtime. It handles disk export, transfer, format conversion, and VM recreation while preserving network configuration and OS settings.

### Supported Guest OS

| OS Type | Status | Features |
|---------|--------|----------|
| Windows Server | ✅ Supported | Pre-migration checks, VirtIO driver injection, post-migration network reconfiguration |
| Linux | 🚧 Planned | Coming soon |

## Prerequisites

### Infrastructure Requirements

#### NFS Staging Server
A dedicated NFS server is required for disk transfer and conversion. This server acts as an intermediary between Nutanix and Harvester.

**Requirements:**
- Linux server with NFS server installed
- Sufficient storage for VM disks (virtual size × number of disks being migrated)
- Network connectivity to both Nutanix cluster and Harvester cluster
- NFS export accessible from Harvester nodes

**NFS Export Configuration Example:**
```bash
# /etc/exports
/mnt/data    10.0.0.0/8(rw,sync,no_subtree_check,no_root_squash)
```

**Required packages on NFS server:**
```bash
apt install qemu-utils nfs-kernel-server
```

#### Nutanix Cluster
- Prism Central or Prism Element API access
- Credentials with VM read and snapshot permissions
- Network access from migration server to Nutanix API (port 9440)

#### Harvester Cluster
- Harvester API access (kubeconfig or direct API)
- Namespace with appropriate permissions
- Storage class configured (Longhorn-based)
- Network access from Harvester pods to NFS server

### Migration Server Requirements

The server running this tool needs:

```bash
# Python 3.8+
apt install python3 python3-pip

# Required Python packages
pip install pyyaml requests urllib3

# QEMU utilities (for disk analysis)
apt install qemu-utils

# Kerberos (for Windows AD integration - optional)
apt install krb5-user

# WinRM support (for Windows pre/post migration)
pip install pywinrm
```

### Network Requirements

| Source | Destination | Port | Purpose |
|--------|-------------|------|---------|
| Migration server | Nutanix API | 9440 | API calls |
| Migration server | Harvester API | 6443 | Kubernetes API |
| Migration server | Windows VMs | 5985/5986 | WinRM |
| Migration server | NFS server | 2049 | Disk operations |
| Harvester nodes | NFS server | 2049 | Disk import |

## Installation

```bash
# Clone or extract the tool
unzip hci-migration-tool.zip
cd hci-migration-tool

# Copy and edit configuration
cp config.yaml.example config.yaml
vim config.yaml
```

## Configuration

### config.yaml

```yaml
# Nutanix connection
nutanix:
  host: prism.example.com
  username: admin
  # password: from-vault-or-env
  verify_ssl: false

# Harvester connection  
harvester:
  host: harvester.example.com
  # Uses kubeconfig or token authentication
  namespace: harvester-public
  verify_ssl: false

# Transfer/staging configuration
transfer:
  nfs_server: 10.0.0.100
  nfs_path: /mnt/data
  staging_path: /mnt/data/migrations

# Optional: HashiCorp Vault integration
vault:
  enabled: false
  url: https://vault.example.com
  # Stores credentials securely
```

## Usage

### Interactive Menu

```bash
python3 migrate.py
```

Provides a guided menu for all operations.

### CLI Commands

#### List VMs

```bash
# List Nutanix VMs
python3 migrate.py list

# List Harvester VMs
python3 migrate.py list-harvester

# Show VM details
python3 migrate.py show <vmname>
```

#### List Resources

```bash
# List Harvester images
python3 migrate.py list-images

# List Harvester networks
python3 migrate.py list-networks

# List staged disks (on NFS)
python3 migrate.py list-staging
```

#### Import Disks

```bash
# Import specific disk
python3 migrate.py import <vmname> --disk <N>

# Import all disks for a VM
python3 migrate.py import <vmname>

# With custom namespace/storage
python3 migrate.py import <vmname> --disk 0 \
    --namespace my-namespace \
    --storage-class harvester-longhorn-dual-node
```

## Migration Workflow

### Windows VM Migration

#### Phase 1: Pre-Migration (Source VM Running)

1. **Connectivity Check** - Verify WinRM access to Windows VM
2. **Compatibility Check** - Validate OS version, disk configuration
3. **VirtIO Driver Check** - Ensure VirtIO drivers are installed (or inject them)
4. **Network Documentation** - Capture current IP configuration
5. **Preparation** - Disable automatic startup services if needed

#### Phase 2: Disk Export & Transfer

1. **Snapshot Creation** - Create consistent snapshot on Nutanix
2. **Disk Export** - Export disks as QCOW2 to NFS staging
3. **Verification** - Validate exported disk integrity

#### Phase 3: Import to Harvester

1. **Disk Analysis** - Analyze sparse structure with `qemu-img map`
2. **PVC Creation** - Create Longhorn volume with appropriate size
3. **Sparse Import** - Copy only data segments (not zeros)
4. **Cleanup** - Remove temporary files

#### Phase 4: VM Creation

1. **VM Definition** - Create VirtualMachine resource in Harvester
2. **Network Attachment** - Configure VM networks
3. **Boot Configuration** - Set UEFI/BIOS mode based on source

#### Phase 5: Post-Migration (Target VM)

1. **First Boot** - Start VM in Harvester
2. **Network Reconfiguration** - Restore IP settings via WinRM
3. **Validation** - Verify services and connectivity
4. **Cleanup** - Remove source VM snapshot (optional)

### Linux VM Migration

🚧 **Coming Soon**

Linux migration will support:
- Common distributions (Ubuntu, RHEL, Debian, CentOS)
- SSH-based pre/post migration configuration
- Cloud-init integration
- Network configuration preservation (netplan, NetworkManager, legacy)

## Technical Details

### Sparse Disk Import

The tool uses an optimized sparse-aware import process that dramatically reduces transfer time for thin-provisioned disks.

**Traditional approach (slow):**
```
QCOW2 → RAW → dd conv=sparse → Block Device
         ↑
    Reads entire virtual size
```

**Optimized approach (fast):**
```
QCOW2 → qemu-img map → Identify data segments
      → RAW sparse file
      → Copy ONLY data segments → Block Device
```

**Benefits:**
- A 1TB disk with 50MB data: copies 50MB instead of reading 1TB
- Preserves sparseness on target volume
- Reduces NFS traffic by orders of magnitude

### Volume Creation

Volumes are created as independent Longhorn PVCs with no backing image dependency. This ensures:
- Full portability
- No single point of failure
- Standard Kubernetes volume semantics

### Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Nutanix AHV   │     │   NFS Staging   │     │    Harvester    │
│                 │     │                 │     │                 │
│  ┌───────────┐  │     │  ┌───────────┐  │     │  ┌───────────┐  │
│  │    VM     │  │────▶│  │  QCOW2    │  │────▶│  │  Longhorn │  │
│  │  (disks)  │  │     │  │  files    │  │     │  │    PVC    │  │
│  └───────────┘  │     │  └───────────┘  │     │  └───────────┘  │
│                 │     │                 │     │        │        │
└─────────────────┘     └─────────────────┘     │  ┌─────▼─────┐  │
                                                │  │    VM     │  │
        Migration Server                        │  │ (KubeVirt)│  │
       ┌─────────────┐                          │  └───────────┘  │
       │ migrate.py  │──────────────────────────│                 │
       │             │   API calls              └─────────────────┘
       └─────────────┘
```

## Troubleshooting

### Common Issues

#### NFS Mount Failures in Importer Pod
```bash
# Check NFS export is accessible from Harvester nodes
showmount -e <nfs_server>

# Verify no firewall blocking port 2049
nc -zv <nfs_server> 2049
```

#### PVC Not Binding
```bash
# Check storage class exists
kubectl get sc

# Check Longhorn health
kubectl -n longhorn-system get pods
```

#### Import Pod Stuck
```bash
# Check pod logs
kubectl logs -n <namespace> importer-<vmname>-disk<N>

# Check pod status
kubectl describe pod -n <namespace> importer-<vmname>-disk<N>
```

#### Windows WinRM Connection Failed
```bash
# Verify WinRM is enabled on Windows
# On Windows: winrm quickconfig

# Test connectivity
curl -v http://<windows_ip>:5985/wsman
```

### Cleanup Commands

```bash
# Delete stuck importer pod
kubectl delete pod -n <namespace> importer-<name> --force --grace-period=0

# Delete failed PVC
kubectl delete pvc -n <namespace> <pvc-name>

# Clean staging files
rm -rf /mnt/data/migrations/<vmname>/
```

## File Structure

```
hci-migration-tool/
├── migrate.py              # Main entry point
├── config.yaml             # Configuration (create from example)
├── config.yaml.example     # Configuration template
├── lib/
│   ├── __init__.py
│   ├── nutanix.py          # Nutanix API client
│   ├── harvester.py        # Harvester/Kubernetes API client
│   ├── actions.py          # Migration workflow actions
│   ├── windows.py          # Windows WinRM operations
│   ├── vault.py            # HashiCorp Vault integration
│   └── utils.py            # Utilities and formatting
└── README.md
```

## License

Internal tool - Wyss Center for Bio and Neuroengineering

## Contributing

For issues or feature requests, contact the IT Infrastructure team.
