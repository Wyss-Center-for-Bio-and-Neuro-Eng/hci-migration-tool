# HCI Migration Tool

VM migration tool from Nutanix AHV to Harvester HCI.

## Features

- **Windows Pre-Check**: Collect VM config, install VirtIO drivers & QEMU Guest Agent
- **Fast NFS Export**: Direct NFS copy from Nutanix storage (420-500 MB/s) - **5x faster than API**
- **Fallback API Export**: aria2c multi-connection download when NFS unavailable
- **Conversion**: RAW → QCOW2 with compression
- **Sparse Import**: Intelligent import using `qemu-img map` - copies only data segments
- **VM Creation**: Automatic configuration from Nutanix specs with multi-NIC mapping
- **Dissociation**: Clone volumes to remove image dependency
- **Post-Migration**: Auto-reconfigure network, start services
- **Vault**: Secure credential storage with `pass` + GPG
- **CLI Commands**: Direct command-line import for automation

## Architecture Overview

```
┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│    NUTANIX      │   NFS   │  STAGING NFS    │   NFS   │   HARVESTER     │
│    CVM          │ ──────► │  SERVER         │ ──────► │   CLUSTER       │
│                 │  Export │  (Debian)       │  Import │                 │
└─────────────────┘         └─────────────────┘         └─────────────────┘
        │                          │                           │
   Raw vDisks               QCOW2 Files                  PVC Volumes
   /container/              /mnt/data/                   Longhorn
   .acropolis/vmdisk/       migrations/<vm>/
```

## Prerequisites

### System Packages (Debian/Ubuntu)

```bash
sudo apt install -y \
    python3 python3-pip \
    qemu-utils \
    sshpass \
    pass gnupg2 \
    krb5-user libkrb5-dev \
    realmd sssd sssd-tools adcli \
    aria2 \
    nfs-common \
    nfs-kernel-server \
    jq

# Python packages
pip install pyyaml requests pywinrm[kerberos] --break-system-packages
```

### Required Tools Summary

| Tool | Purpose | Install |
|------|---------|---------|
| `python3` | Script runtime | `apt install python3` |
| `qemu-img` | RAW → QCOW2 conversion, disk analysis | `apt install qemu-utils` |
| `nfs-common` | NFS client for Nutanix export | `apt install nfs-common` |
| `nfs-kernel-server` | NFS server for Harvester import | `apt install nfs-kernel-server` |
| `jq` | JSON parsing for sparse import | `apt install jq` |
| `aria2c` | Fallback multi-connection download | `apt install aria2` |
| `pass` | Secure credential vault | `apt install pass gnupg2` |
| `kinit` | Kerberos authentication | `apt install krb5-user` |

### NFS Configuration

The tool uses NFS at two stages:

#### 1. Export: Nutanix → Staging (Read-Only)

Whitelist migration server IP on Nutanix containers:

1. **Prism Element** → Storage → Table view
2. Select container (e.g., `container01`)
3. **Update** → Filesystem Whitelist
4. Add migration server IP: `10.16.22.x`
5. Save

This enables: `mount -t nfs <CVM_IP>:/<container> /mnt/nutanix`

#### 2. Import: Staging → Harvester (Read-Write)

The staging NFS server must be accessible from Harvester pods:

1. **NFS Server Setup** (on staging server):
```bash
# Install NFS server
apt install nfs-kernel-server

# Configure exports (adjust IP range for your Harvester cluster)
echo "/mnt/data 10.16.16.0/24(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports

# Apply
exportfs -ra
systemctl restart nfs-kernel-server

# Verify
exportfs -v
```

2. **Firewall Rules** (if ufw enabled):
```bash
# Allow NFS from Harvester cluster network
ufw allow from 10.16.16.0/24 to any port 2049
ufw allow from 10.16.16.0/24 to any port 111
```

3. **Test from Harvester node**:
```bash
# On any Harvester node
showmount -e 10.16.22.240
mount -t nfs 10.16.22.240:/mnt/data /mnt/test
ls /mnt/test/migrations/
umount /mnt/test
```

**⚠️ IMPORTANT**: The `no_root_squash` option is required because the importer pod runs as root.

## Installation

```bash
# Extract the tool
unzip hci-migration-tool.zip
cd hci-migration-tool

# Copy and edit config
cp config.yaml.example config.yaml
nano config.yaml
```

## Configuration

### config.yaml

```yaml
nutanix:
  prism_ip: "10.16.22.46"
  username: "admin"
  password: "your_password"
  cvm_ip: "10.16.22.46"           # CVM IP for NFS mount
  nfs_mount_path: "/mnt/nutanix"  # Local mount point

harvester:
  api_url: "https://10.16.16.130:6443"
  token: "your_bearer_token"
  namespace: "harvester-public"
  verify_ssl: false

transfer:
  staging_mount: "/mnt/data"        # Local staging directory
  nfs_server: "10.16.22.240"        # NFS server IP (must be accessible from Harvester pods)
  nfs_path: "/mnt/data"             # NFS export path (must match staging_mount)
  convert_to_qcow2: true
  compress: true

windows:
  domain: "AD.YOURDOMAIN.COM"
  use_kerberos: true
  vault_backend: "pass"
  vault_path: "migration/windows"
  winrm_port: 5985
  winrm_transport: "kerberos"
```

### Get Harvester Token

```bash
# Via kubectl on Harvester cluster
kubectl -n cattle-system get secret \
  $(kubectl -n cattle-system get sa rancher -o jsonpath='{.secrets[0].name}') \
  -o jsonpath='{.data.token}' | base64 -d
```

## Usage

### Interactive Mode

```bash
python3 migrate.py
```

### CLI Commands

```bash
# List VMs
python3 migrate.py list                    # Nutanix VMs
python3 migrate.py list-harvester          # Harvester VMs
python3 migrate.py list-staging            # Staging files

# Show VM details
python3 migrate.py show <vmname>

# Import disks to Harvester
python3 migrate.py import <vmname> --disk 0      # Import specific disk
python3 migrate.py import <vmname> --disk 1      # Import disk 1
python3 migrate.py import <vmname>               # Import all disks

# Import with options
python3 migrate.py import <vmname> --disk 0 \
    --namespace harvester-public \
    --storage-class harvester-longhorn-dual-node

# Test Harvester connection
python3 migrate.py test-harvester
```

---

# Migration Workflow

## Complete Windows VM Migration

### PHASE 1 - PREPARATION (Source VM on Nutanix)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 1.1 | Windows (4) | 2 | **Pre-migration check** → Collect config |
| 1.2 | Windows (4) | 2 | **Install QEMU Guest Agent** → Only QEMU GA! |
| 1.3 | Windows (4) | 5 | **Stop services** → Optional |

**⚠️ IMPORTANT:** Do NOT install VirtIO drivers before migration!

### PHASE 2 - EXPORT (Nutanix → Staging)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 2.1 | Nutanix (1) | 3 | **Select VM** |
| 2.2 | Nutanix (1) | 5 | **Power OFF** the VM |
| 2.3 | Migration (3) | 4 | **Export VM** → NFS or API |
| 2.4 | Migration (3) | 5 | **Convert RAW → QCOW2** |

### PHASE 3 - IMPORT (Staging → Harvester)

```bash
# CLI command (recommended)
python3 migrate.py import <vmname> --disk 0
```

The import uses `qemu-img map` for sparse-aware copying:

```
=== Step 1/4: Analyzing disk structure ===
   Virtual size: 1000 GB
   Found 12 data segments totaling 69 MB
   Efficiency: copying 69 MB instead of reading 1000 GB

=== Step 2/4: Converting QCOW2 to sparse RAW ===
   Conversion complete - sparse RAW file: 69M on disk

=== Step 3/4: Copying data segments to block device ===
   Segment 1/12: offset 0, size 64KB
   ...
   All segments copied successfully

=== IMPORT COMPLETED SUCCESSFULLY ===
```

### PHASE 4 - CREATE VM & POST-MIGRATION

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 4.1 | Migration (3) | 7 | **Create VM** (SATA bus) |
| 4.2 | Windows (4) | 8 | **Post-migration auto** |

---

## Performance Comparison

### Export Speed

| Method | Speed | Time for 1 TB |
|--------|-------|---------------|
| **NFS Direct** | **420-530 MB/s** | **~33 min** |
| aria2c API | ~100 MB/s | ~2h45 |

### Import Speed

| Method | 1 TB Sparse (69 MB) | 40 GB Normal (11 GB) |
|--------|---------------------|----------------------|
| **Sparse Import** | **~10 sec** | **~2-3 min** |
| dd conv=sparse | ~3+ hours | ~10 min |

---

## Staging Directory Structure

```
/mnt/data/                              # Staging NFS (exported to Harvester)
├── tools/                              # VirtIO/QEMU tools
│   ├── virtio-win.iso
│   └── qemu-ga-x86_64.msi
├── migrations/                         # Per-VM data
│   └── <hostname>/
│       ├── vm-config.json
│       ├── <hostname>-disk0.qcow2
│       └── <hostname>-disk1.qcow2
```

---

## Troubleshooting

### NFS Import to Harvester Fails

```bash
# Check NFS server
systemctl status nfs-kernel-server
exportfs -v

# Test from Harvester node
ssh root@harvester-node
mount -t nfs 10.16.22.240:/mnt/data /mnt/test
```

### Import Pod Fails with "Permission Denied"

```bash
# Ensure no_root_squash is set
cat /etc/exports
# Should show: /mnt/data 10.16.16.0/24(rw,sync,no_subtree_check,no_root_squash)
exportfs -ra
```

### Cleanup Failed Import

```bash
kubectl delete pod importer-<vmname>-disk0 -n harvester-public --force --grace-period=0
kubectl delete pvc <vmname>-disk0 -n harvester-public
rm -f /mnt/data/migrations/<vmname>/*.tmp.raw
```

---

## License

MIT License - Wyss Center for Bio and Neuro Engineering
