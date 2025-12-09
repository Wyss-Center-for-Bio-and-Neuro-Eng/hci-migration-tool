# HCI Migration Tool

VM migration tool from Nutanix AHV to Harvester HCI.

## Features

- **Windows Pre-Check**: Collect VM config, install VirtIO drivers & QEMU Guest Agent
- **Fast NFS Export**: Direct NFS copy from Nutanix storage (420-500 MB/s) - **5x faster than API**
- **Fallback API Export**: aria2c multi-connection download when NFS unavailable
- **Conversion**: RAW → QCOW2 with compression
- **Import**: Upload to Harvester (HTTP or virtctl)
- **VM Creation**: Automatic configuration from Nutanix specs with multi-NIC mapping
- **Dissociation**: Clone volumes to remove image dependency
- **Post-Migration**: Auto-reconfigure network, start services
- **Vault**: Secure credential storage with `pass` + GPG

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
    nfs-common  # For NFS fast export

# Python packages
pip install pyyaml requests pywinrm[kerberos] --break-system-packages
```

### Required Tools Summary

| Tool | Purpose | Install |
|------|---------|---------|
| `python3` | Script runtime | `apt install python3` |
| `qemu-img` | RAW → QCOW2 conversion | `apt install qemu-utils` |
| `nfs-common` | NFS mount for fast export | `apt install nfs-common` |
| `aria2c` | Fallback multi-connection download | `apt install aria2` |
| `pass` | Secure credential vault | `apt install pass gnupg2` |
| `kinit` | Kerberos authentication | `apt install krb5-user` |
| `virtctl` | (Optional) Direct upload to Harvester | Manual install |

### NFS Whitelist Configuration (Nutanix)

For fast NFS export, the migration server IP must be whitelisted on Nutanix containers:

1. **Prism Element** → Storage → Table view
2. Select container (e.g., `container01`)
3. **Update** → Filesystem Whitelist
4. Add migration server IP: `10.16.22.x`
5. Save

This enables direct NFS mount: `mount -t nfs <CVM_IP>:/<container> /mnt/nutanix`

### Active Directory Domain Join (for Kerberos)

```bash
# Join the domain
sudo realm join -U administrator AD.YOURDOMAIN.COM

# Verify
realm list
id your_user@ad.yourdomain.com
```

### Vault Setup (pass)

```bash
# Generate GPG key
gpg --batch --gen-key <<EOF
Key-Type: RSA
Key-Length: 4096
Name-Real: HCI Migration Tool
Name-Email: migration@yourdomain.com
Expire-Date: 0
%no-protection
%commit
EOF

# Initialize pass
pass init "migration@yourdomain.com"

# Add Windows credentials (multiline format)
pass insert -m migration/windows/local-admin
# Enter password, then on new line: username: svc_run_script@ad.wysscenter.ch
```

### Kerberos Authentication

```bash
# Get a ticket (valid 10h)
kinit your_admin@AD.YOURDOMAIN.COM

# Verify ticket
klist

# Renew if expired
kinit -R
```

## Installation

```bash
# Clone the repo
git clone https://github.com/your-org/hci-migration-tool.git
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
  cvm_ip: "10.16.22.46"      # CVM IP for NFS mount (usually same as prism_ip)
  nfs_mount_path: "/mnt/nutanix"  # Local mount point for NFS

harvester:
  api_url: "https://10.16.16.130:6443"
  token: "your_bearer_token"
  namespace: "harvester-public"
  verify_ssl: false

transfer:
  staging_mount: "/mnt/data"
  http_server_ip: "10.16.16.167"  # Your Debian IP for Harvester imports
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

```bash
python3 migrate.py
```

---

# Migration Workflow

## Complete Windows VM Migration

### PHASE 1 - PREPARATION (Source VM on Nutanix)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 1.1 | Windows (4) | 2 | **Pre-migration check** → Connect via WinRM, collect config |
| 1.2 | Windows (4) | 2 | **Install prerequisites** → VirtIO drivers + QEMU Guest Agent |
| 1.3 | Windows (4) | 2 | **Reboot** → Activate drivers, verify installation |
| 1.4 | Windows (4) | 5 | **Stop services** → Stop application services (optional) |

**Result:** VM config saved to `/mnt/data/migrations/<hostname>/vm-config.json`

### PHASE 2 - EXPORT (Nutanix → Staging)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 2.1 | Nutanix (1) | 3 | **Select VM** |
| 2.2 | Nutanix (1) | 5 | **Power OFF** the VM |
| 2.3 | Migration (3) | 4 | **Export VM** → Choose NFS (fast) or API (fallback) |
| 2.4 | Migration (3) | 5 | **Convert RAW → QCOW2** (auto-proposed after export) |

**Export Options:**
- **NFS Direct (recommended)**: 420-530 MB/s - requires container whitelist
- **API Download**: ~100 MB/s - no special config needed

**Result:** QCOW2 file(s) in `/mnt/data/<vm>-disk0.qcow2`

### PHASE 3 - IMPORT (Staging → Harvester)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 3.1 | Migration (3) | 6 | **Import image** → Upload QCOW2 to Harvester |
| 3.2 | Migration (3) | 7 | **Create VM** → Uses saved config (CPU, RAM, disks, network) |

**Result:** VM created in Harvester (powered off)

### PHASE 4 - POST-MIGRATION (Target VM on Harvester)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 4.1 | Harvester (2) | 2 | **Start VM** on Harvester |
| 4.2 | Windows (4) | 8 | **Auto-configure network** → Apply static IP from saved config |
| 4.3 | Windows (4) | 6 | **Start services** → Restart application services |
| 4.4 | - | - | **Uninstall Nutanix tools** (TODO - manual for now) |

**Result:** VM running on Harvester with original IP address!

### PHASE 5 - CLEANUP (Optional)

| Step | Menu | Option | Description |
|------|------|--------|-------------|
| 5.1 | Harvester (2) | 5 | **Dissociate from image** → Clone volume to remove dependency |
| 5.2 | Harvester (2) | 7 | **Delete Harvester image** |
| 5.3 | Nutanix (1) | 7 | **Delete Nutanix export image** |
| 5.4 | Migration (3) | 8 | **Delete staging files** (RAW/QCOW2) |

---

## Detailed Menus

### Main Menu

```
╔══════════════════════════════════════════════════════════════╗
║              NUTANIX → HARVESTER MIGRATION TOOL              ║
╠══════════════════════════════════════════════════════════════╣
║  1. Nutanix                                                  ║
║  2. Harvester                                                ║
║  3. Migration                                                ║
║  4. Windows Tools                                            ║
║  5. Configuration                                            ║
║  q. Quit                                                     ║
╚══════════════════════════════════════════════════════════════╝
```

### Nutanix Menu (1)

| Option | Description |
|--------|-------------|
| 1 | List VMs |
| 2 | VM details |
| 3 | Select VM |
| 4 | Power ON VM |
| 5 | Power OFF VM |
| 6 | List images |
| 7 | Delete image |

### Harvester Menu (2)

| Option | Description |
|--------|-------------|
| 1 | List VMs |
| 2 | Start VM |
| 3 | Stop VM |
| 4 | Delete VM |
| 5 | **Dissociate VM from image** |
| 6 | List images |
| 7 | Delete image |
| 8 | List volumes |
| 9 | Delete volume |
| 10 | List networks |
| 11 | List storage classes |

### Migration Menu (3)

| Option | Description |
|--------|-------------|
| 1 | Check staging |
| 2 | List staging disks |
| 3 | Disk image details |
| 4 | **Export VM** (NFS fast copy or API fallback) |
| 5 | Convert RAW → QCOW2 |
| 6 | **Import image to Harvester** (HTTP or Upload) |
| 7 | Create VM in Harvester |
| 8 | Delete staging file |
| 9 | Full migration (TODO) |

### Windows Tools Menu (4)

| Option | Description |
|--------|-------------|
| 1 | Check WinRM/Prerequisites |
| 2 | **Pre-migration check** (collect config + install VirtIO/QEMU-GA) |
| 3 | View VM config |
| 4 | Download virtio/qemu-ga tools |
| 5 | **Stop services** (pre-migration) |
| 6 | **Start services** (post-migration) |
| 7 | Generate post-migration script |
| 8 | **Post-migration auto-configure** |
| 9 | Vault management |

---

## Export Speed Comparison

| Method | Speed | Time for 1 TB | Notes |
|--------|-------|---------------|-------|
| **NFS Direct (default)** | **420-530 MB/s** | **~33 min** | Requires whitelist config |
| aria2c (fallback) | ~100 MB/s | ~2h45 | 16 parallel connections |
| Python requests | ~100 MB/s | ~2h45 | Single connection |

The tool automatically:
1. Tries **NFS direct copy** first (fastest)
2. Falls back to **API download** if NFS unavailable

### NFS vs API Performance

```
NFS Direct:  ████████████████████████████████████ 500 MB/s
API (aria2): ████████                              100 MB/s
```

**5x faster with NFS!** A 1 TB disk exports in ~33 minutes instead of ~3 hours.

---

## Multi-NIC Support

When creating a VM with multiple network interfaces, the tool maps each source NIC to a Harvester network:

```
🌐 Network Mapping (2 NIC(s)):

   --- Source NIC 0: Ethernet ---
      MAC: 50:6B:8D:D7:26:A6
      IP:  10.16.16.113/23 (Static)
      GW:  10.16.16.1
      DNS: 10.16.16.101, 10.16.16.102

   Available Harvester networks:
     1. vlan-16 (harvester-public) (VLAN 16)
     2. vlan-20 (harvester-public) (VLAN 20)
   
   Network for NIC 0 > 1
   Keep MAC 50:6B:8D:D7:26:A6? (y/n) [y] > y
```

---

## Image Dissociation

### Problem

Harvester uses "backing images" for thin provisioning. Volumes created from an image remain linked to it, preventing image deletion.

### Solution

The "Dissociate VM from image" option (Menu 2 → Option 5):

1. Clones VM volume(s) via CSI
2. Updates VM to use clones
3. Deletes old volumes
4. Image can now be deleted

```
Before: VM → Volume → Backing Image (linked)
After:  VM → Volume Clone (independent)
```

---

## Staging Directory Structure

```
/mnt/nutanix/                           # NFS mount (Nutanix containers)
├── container01/
│   └── .acropolis/vmdisk/              # Raw VM disks
└── Nutanix_nxf_ctr/
    └── .acropolis/vmdisk/

/mnt/data/                              # Local staging
├── tools/                              # VirtIO/QEMU tools
│   ├── virtio-win.iso
│   └── qemu-ga-x86_64.msi
│
├── migrations/                         # Per-VM configs
│   └── <hostname>/
│       ├── vm-config.json              # Collected config
│       └── reconfig-network.ps1        # Post-migration script
│
├── <vm>-disk0.raw                      # Exported disks (temporary)
└── <vm>-disk0.qcow2                    # Converted disks (for import)
```

---

## vm-config.json Format

```json
{
  "collected_at": "2025-12-08T10:30:00Z",
  "source_platform": "nutanix",
  "hostname": "SRV-APP01",
  "os_name": "Microsoft Windows Server 2022 Standard",
  "cpu_cores": 4,
  "memory_mb": 8192,
  "boot_type": "UEFI",
  "network": {
    "interfaces": [
      {
        "name": "Ethernet",
        "mac": "50:6B:8D:AA:BB:CC",
        "dhcp": false,
        "ip": "10.16.16.113",
        "prefix": 23,
        "gateway": "10.16.16.1",
        "dns": ["10.16.16.101", "10.16.16.102"]
      }
    ]
  },
  "agents": {
    "ngt_installed": true,
    "virtio_fedora": true,
    "qemu_guest_agent": true
  },
  "migration_ready": true,
  "missing_prerequisites": []
}
```

---

## Troubleshooting

### NFS Mount Fails

```bash
# Check if NFS is accessible
showmount -e 10.16.22.46

# If "access denied", add IP to whitelist:
# Prism Element → Storage → Container → Update → Filesystem Whitelist
```

### NFS Export Slow or Fails

- Ensure migration server is on same subnet as CVM (avoid firewall routing)
- Check network: `iperf3` between migration server and CVM
- Verify container whitelist includes correct IP

### aria2c "authentication required"

The tool passes credentials automatically. If it fails, check:
```bash
# Test manually
aria2c --http-user=admin --http-passwd=PASSWORD \
  "https://10.16.22.46:9440/api/nutanix/v3/images/UUID/file"
```

### WinRM "Access Denied" Error

```bash
# Check Kerberos ticket
klist

# Renew if expired
kinit your_admin@AD.YOURDOMAIN.COM
```

### Kerberos requires FQDN, not IP

```
Windows hostname (FQDN) > 10.16.16.113
⚠️  IP address detected but Kerberos requires hostname (FQDN)

# Use FQDN instead:
Windows hostname (FQDN) > servername.ad.yourdomain.com
```

### VirtIO drivers not detected

Check the debug log on Windows:
```
C:\temp\virtio-debug.log
```

### QEMU Guest Agent Not Reporting IP

- Ensure QEMU-GA service is running in Windows
- Check VM has network connectivity (DHCP)
- Wait 1-2 minutes after boot

### Harvester Image Cannot Be Deleted

Image is used by a volume (backing image). Solutions:
1. Use "Dissociate VM from image" (Menu 2 → Option 5)
2. Or delete manually: VM → Volume → Image

---

## Technical Notes

### Nutanix API for NFS Export

The tool uses Nutanix API v2 `/vms/?include_vm_disk_config=true` to retrieve:
- `vmdisk_uuid`: Actual filename on NFS storage
- `ndfs_filepath`: Full NFS path (e.g., `/container01/.acropolis/vmdisk/<uuid>`)

**Important**: API v3 returns `device_uuid` which is different from the NFS filename. Only API v2 with `include_vm_disk_config=true` returns the correct `vmdisk_uuid`.

### vDisk Storage Layout

```
/mnt/nutanix/                          # NFS mount point
└── <container>/
    └── .acropolis/
        └── vmdisk/
            └── <vmdisk_uuid>          # Raw disk file
```

---

## Security Notes

- Nutanix credentials are stored in plaintext in `config.yaml`
- Windows credentials are encrypted in `pass` vault
- Kerberos tickets expire after 10h (configurable)
- Use service accounts with minimal privileges
- The migration server should have restricted access

---

## TODO / Future Improvements

- [ ] Uninstall Nutanix tools post-migration (automated)
- [ ] Full migration option (single command)
- [ ] Linux VM support
- [ ] Parallel disk export for multi-disk VMs
- [ ] Progress dashboard / web UI
- [x] ~~NFS direct export~~ ✅ Implemented (420-530 MB/s)

---

## License

MIT License - Wyss Center for Bio and Neuro Engineering

## Contributors

- Infrastructure Team @ Wyss Center
