# HCI Migration Tool

VM migration tool from Nutanix AHV to Harvester HCI.

## Features

- **Export**: Extract VM disks from Nutanix (via acli)
- **Conversion**: RAW → QCOW2 with compression
- **Import**: Upload to Harvester (HTTP or virtctl)
- **VM Creation**: Automatic configuration from Nutanix specs with multi-NIC mapping
- **Dissociation**: Clone volumes to remove image dependency
- **Windows Tools**: Pre-check, network config collection, auto post-migration
- **Vault**: Secure credential storage with `pass` + GPG

## Prerequisites

### System (Debian/Ubuntu)

```bash
# System packages
sudo apt install -y \
    python3 python3-pip \
    qemu-utils \
    sshpass \
    pass gnupg2 \
    krb5-user libkrb5-dev \
    realmd sssd sssd-tools adcli

# Python packages
pip install pyyaml requests pywinrm[kerberos] --break-system-packages
```

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

# Add Windows credentials (for workgroup machines)
pass insert migration/windows/local-admin
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

harvester:
  api_url: "https://10.16.16.130:6443"
  token: "your_bearer_token"
  namespace: "harvester-public"
  verify_ssl: false

transfer:
  staging_mount: "/mnt/data"
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

### Main Menu

```
╔══════════════════════════════════════════════════════════════╗
║              NUTANIX → HARVESTER MIGRATION TOOL              ║
╠══════════════════════════════════════════════════════════════╣
║                         MAIN MENU                            ║
╠══════════════════════════════════════════════════════════════╣
║  1. Nutanix                                                  ║
║  2. Harvester                                                ║
║  3. Migration                                                ║
║  4. Windows Tools                                            ║
║  5. Configuration                                            ║
║  q. Quit                                                     ║
╚══════════════════════════════════════════════════════════════╝
```

## Migration Workflow

### Standard Migration

1. **Select source VM** (Menu 1 → Option 3)
2. **Power off source VM** (Menu 1 → Option 5)
3. **Create Nutanix image** (via Prism or acli)
4. **Export disk** (Menu 3 → Option 4)
5. **Convert RAW → QCOW2** (Menu 3 → Option 5)
6. **Import to Harvester** (Menu 3 → Option 6)
7. **Create VM** (Menu 3 → Option 7)
8. **Start and test** (Menu 2 → Option 2)
9. **Dissociate from image** (Menu 2 → Option 5) - Optional
10. **Cleanup**: Delete images and staging files

### Windows Migration (with network reconfiguration)

#### Pre-migration

1. **Download tools** (Menu 4 → Option 4)
   - virtio-win.iso
   - virtio-win-gt-x64.msi
   - qemu-ga-x86_64.msi

2. **Run pre-migration check** (Menu 4 → Option 2)
   - Connects via WinRM + Kerberos
   - Collects: IP, DNS, Gateway, Hostname, Agents
   - Auto-installs QEMU Guest Agent if missing
   - Saves to `/mnt/data/migrations/<hostname>/vm-config.json`

3. **Verify readiness** (Menu 4 → Option 3)
   - Displays JSON configuration
   - Shows missing prerequisites

#### Post-migration

1. **Start migrated VM** on Harvester (Menu 2 → Option 2)
2. **Auto-configure network** (Menu 4 → Option 6)
   - Gets temporary DHCP IP via QEMU Guest Agent
   - Connects via WinRM (NTLM)
   - Applies static IP configuration from vm-config.json
3. VM is now reachable at its original IP!

## Detailed Menus

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
| 4 | Export VM (Nutanix → Staging) |
| 5 | Convert RAW → QCOW2 |
| 6 | **Import image to Harvester** (HTTP or Upload) |
| 7 | Create VM in Harvester |
| 8 | Delete staging file |
| 9 | Full migration |

### Windows Tools Menu (4)

| Option | Description |
|--------|-------------|
| 1 | Check WinRM/Prerequisites |
| 2 | Pre-migration check (collect config + install agents) |
| 3 | View VM config |
| 4 | Download virtio/qemu-ga tools |
| 5 | Generate post-migration script |
| 6 | **Post-migration auto-configure** |
| 7 | Vault management |

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

## Staging Directory Structure

```
/mnt/data/
├── tools/                          # Tools to deploy
│   ├── virtio-win.iso
│   ├── virtio-win-gt-x64.msi
│   └── qemu-ga-x86_64.msi
│
├── migrations/                     # Per-VM configs
│   └── <hostname>/
│       ├── vm-config.json          # Collected config
│       └── reconfig-network.ps1    # Post-migration script
│
├── <vm>-disk0.raw                  # Exported disks
└── <vm>-disk0.qcow2                # Converted disks
```

## vm-config.json Format

```json
{
  "collected_at": "2025-12-08T10:30:00Z",
  "source_platform": "nutanix",
  "system": {
    "hostname": "SRV-APP01",
    "os_name": "Microsoft Windows Server 2022 Standard",
    "os_version": "10.0.20348",
    "architecture": "64-bit",
    "domain": "AD.YOURDOMAIN.COM",
    "domain_joined": true
  },
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

## Troubleshooting

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

### "422 Unprocessable Entity" on Harvester

Start/stop operations use KubeVirt subresources API:
```
PUT /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/start
PUT /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/stop
```

### Harvester Image Cannot Be Deleted

Image is used by a volume (backing image). Solutions:
1. Use "Dissociate VM from image" (Menu 2 → Option 5)
2. Or delete manually: VM → Volume → Image

### QEMU Guest Agent Not Reporting IP

- Ensure QEMU-GA service is running in Windows
- Check VM has network connectivity (DHCP)
- Wait 1-2 minutes after boot

### Vault Errors

Verify `pass` is initialized with correct GPG key:
```bash
pass init "migration@yourdomain.com"
```

## Security Notes

- Nutanix credentials are stored in plaintext in `config.yaml`
- Windows credentials are encrypted in `pass` vault
- Kerberos tickets expire after 10h (configurable)
- Use service accounts with minimal privileges
- The migration server should have restricted access

## License

MIT License - Wyss Center for Bio and Neuro Engineering

## Contributors

- Infrastructure Team @ Wyss Center

## Known Issues & Future Improvements

### NFS Direct Access (To Investigate)

**Goal:** Mount Nutanix container directly via NFS for faster vDisk export (~500+ MB/s vs 107 MB/s HTTP)

**Current Status:** NOT WORKING

**Configuration Done:**
- Filesystem whitelist: Added Debian IP (10.16.16.167/255.255.255.255)
- Container whitelist: Inherited from filesystem
- Sophos XGS firewall: Rules created for NFS (TCP/UDP 111, 2049, 20048)
- CVM iptables: Rules exist for 10.16.16.167 on port 2049 (CUSTOM chain)

**Test Results:**
- Ping to CVM: ✅ OK
- SSH (22) to CVM: ✅ OK  
- API (9440) to CVM: ✅ OK
- NFS (2049) to CVM: ❌ Connection timeout
- Port 111 (portmapper): ✅ OK
- Port 20048 (mountd): ❌ Connection timeout

**CVM IPs:**
- nxchgvaimgt1: 10.16.22.81
- nxchgvaimgt2: 10.16.22.82
- nxchgvaimgt3: 10.16.22.83

**iptables on CVM shows:**
```
ACCEPT tcp -- eth0 * 10.16.16.167 0.0.0.0/0 tcp dpt:2049
```
But packet counter stays at 0 - packets not reaching CVM despite firewall logs showing "Allowed"

**Possible Causes:**
1. Sophos XGS Application Control or IPS blocking NFS protocol
2. NAT issue between VLANs (Debian: 10.16.16.x, CVMs: 10.16.22.x)
3. Nutanix NFS only exposed on internal interface (eth1/eth2), not external (eth0)

**Workaround:** Use HTTP API download (working at ~107 MB/s)

**Future Investigation:**
- Check Sophos IPS/Application Control settings
- Test with Debian on same VLAN as CVMs (no firewall)
- Contact Nutanix support about NFS external access requirements
- Consider SCP via SSH (requires key management)
