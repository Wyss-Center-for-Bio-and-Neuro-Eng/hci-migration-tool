# HCI Migration Tool

**Nutanix AHV to SUSE Harvester VM Migration Tool**

A Python-based CLI tool for migrating virtual machines from Nutanix AHV to SUSE Harvester HCI.

## Features

- **Nutanix Management**
  - List VMs with status, vCPU, RAM, disk count
  - View detailed VM specifications
  - Power on/off VMs (experimental)
  - List and delete images

- **Harvester Management**
  - List VMs with running status
  - Start/stop/delete VMs
  - List images, networks, storage classes

- **Migration Operations**
  - Export VM disks from Nutanix (via Prism API)
  - Convert RAW images to QCOW2 with compression (70-90% size reduction)
  - Import images to Harvester via HTTP server
  - Create VMs with original specifications:
    - CPU cores and sockets
    - Memory
    - Boot type (BIOS/UEFI)
    - Disk bus (SATA/virtio/SCSI)
    - Network with MAC address preservation
  - Cleanup: delete staging files, Nutanix export images, Harvester images

## Requirements

- Python 3.8+
- `requests` library
- `pyyaml` library
- `qemu-utils` (for qemu-img)
- Access to:
  - Nutanix Prism API
  - Harvester Kubernetes API
  - Staging storage (CephFS, NFS, or local)

## Installation

```bash
# Clone or extract the tool
cd hci-migration-tool

# Install dependencies
pip install requests pyyaml

# Install qemu-utils (Debian/Ubuntu)
apt install qemu-utils
```

## Configuration

Copy and edit `config.yaml`:

```yaml
nutanix:
  prism_ip: "10.16.22.46"
  username: "admin"
  password: "YOUR_PASSWORD"
  verify_ssl: false

harvester:
  api_url: "https://10.16.16.130:6443"
  namespace: "default"
  # From kubeconfig - base64 encoded
  certificate_authority_data: "LS0tLS1C..."
  client_certificate_data: "LS0tLS1C..."
  client_key_data: "LS0tLS1C..."
  verify_ssl: false

transfer:
  staging_mount: "/mnt/staging"
  convert_to_qcow2: true
  compress: true
```

### Getting Harvester Credentials

Extract from your kubeconfig:

```bash
# Get cluster certificate
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'

# Get client certificate
kubectl config view --raw -o jsonpath='{.users[0].user.client-certificate-data}'

# Get client key
kubectl config view --raw -o jsonpath='{.users[0].user.client-key-data}'
```

## Usage

### Interactive Mode

```bash
python3 migrate.py
```

Navigate through menus:
- **1. Nutanix** - Manage source VMs and images
- **2. Harvester** - Manage target VMs and resources
- **3. Migration** - Perform migration operations
- **4. Configuration** - View current settings

### Command Line

```bash
# List Nutanix VMs
python3 migrate.py list

# List Harvester VMs
python3 migrate.py list-harvester

# List Harvester images
python3 migrate.py list-images

# List Harvester networks
python3 migrate.py list-networks

# List staging files
python3 migrate.py list-staging

# Show VM details
python3 migrate.py show <vm_name>

# Test Harvester connection
python3 migrate.py test-harvester
```

## Migration Workflow

### 1. Prepare Source VM

1. **Power off** the VM in Nutanix (recommended for consistency)
2. **Create disk images** via Nutanix acli:
   ```bash
   # On Nutanix CVM
   acli image.create <vm_name>-disk0 clone_from_vmdisk=<vm_name>:scsi.0 image_type=kDiskImage
   ```

### 2. Export to Staging

Download the image to staging storage:

```bash
# Via curl (manual)
curl -k -u admin "https://<prism_ip>:9440/api/nutanix/v3/images/<image_uuid>/file" \
  -o /mnt/staging/<vm_name>-disk0.raw
```

Or use **Menu 3 → Option 4** in the tool.

### 3. Convert to QCOW2

Use **Menu 3 → Option 5** to convert RAW to compressed QCOW2:

- Typical compression: 70-90% size reduction
- Windows VMs: ~70% reduction
- Linux VMs: ~85-90% reduction

### 4. Import to Harvester

Use **Menu 3 → Option 6**:

1. Select QCOW2 file
2. Choose target namespace
3. Tool starts HTTP server
4. Creates image in Harvester
5. Wait for download to complete (monitor in Harvester UI)

### 5. Create VM

Use **Menu 3 → Option 7**:

1. Select source VM in Nutanix (Menu 1 → Option 3) to auto-fill specs
2. Choose imported image
3. Select network (MAC address can be preserved)
4. Configure disk bus:
   - **SATA** (recommended for migration) - no drivers needed
   - **virtio** - best performance, requires drivers
5. Confirm and create

### 6. Post-Migration

1. **Boot VM** and verify functionality
2. **Install virtio drivers** (Windows):
   - Download: https://fedorapeople.org/groups/virt/virtio-win/
   - Install `virtio-win-gt-x64.msi`
3. **Change disk bus** to virtio (optional, for better performance)
4. **Cleanup**:
   - Delete Harvester source image
   - Delete staging files
   - Delete Nutanix export image

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  NUTANIX CLUSTER                                            │
│  ┌──────────────┐         ┌──────────────┐                 │
│  │ Source VMs   │ ──────▶ │ Disk Images  │                 │
│  └──────────────┘  acli   └──────┬───────┘                 │
└──────────────────────────────────│─────────────────────────┘
                                   │ Prism API
                                   ▼
┌─────────────────────────────────────────────────────────────┐
│  STAGING (Migration VM)                                     │
│  ┌──────────────┐         ┌──────────────┐                 │
│  │ RAW Image    │ ──────▶ │ QCOW2 Image  │                 │
│  │ (40 GB)      │ qemu-img│ (10 GB)      │                 │
│  └──────────────┘         └──────┬───────┘                 │
└──────────────────────────────────│─────────────────────────┘
                                   │ HTTP Server
                                   ▼
┌─────────────────────────────────────────────────────────────┐
│  HARVESTER CLUSTER                                          │
│  ┌──────────────┐         ┌──────────────┐                 │
│  │ VM Image     │ ──────▶ │ Virtual      │                 │
│  │ (imported)   │         │ Machine      │                 │
│  └──────────────┘         └──────────────┘                 │
└─────────────────────────────────────────────────────────────┘
```

## Project Structure

```
hci-migration-tool/
├── README.md           # This file
├── config.yaml         # Configuration file
├── migrate.py          # Main CLI interface
└── lib/
    ├── __init__.py     # Module exports
    ├── utils.py        # Utilities (colors, formatting)
    ├── nutanix.py      # Nutanix Prism API client
    ├── harvester.py    # Harvester/KubeVirt API client
    └── actions.py      # Migration operations
```

## Troubleshooting

### VM boots to "No bootable device"
- **Cause**: BIOS/UEFI mismatch
- **Fix**: Set correct boot type (UEFI if source was UEFI)

### Windows BSOD "INACCESSIBLE BOOT DEVICE"
- **Cause**: Missing disk drivers
- **Fix**: Change disk bus from `virtio` to `sata`

### Network not working after migration
- **Cause**: New MAC address = Windows sees new NIC
- **Fix**: 
  - Reconfigure IP on new adapter, OR
  - Use MAC preservation option during VM creation

### Image import stuck at 0%
- **Cause**: HTTP server not accessible
- **Fix**: Check firewall, ensure staging VM is reachable from Harvester

### Slow transfer speeds
- **Cause**: Network bottleneck between source and staging
- **Tip**: Place staging VM in same network as source for faster export, only transfer compressed QCOW2 to target

## Optimization Tips

### Faster Migration

Place a staging VM in the Nutanix datacenter:
1. Export locally (10 Gbps internal)
2. Convert RAW → QCOW2 locally
3. Transfer only compressed QCOW2 to Harvester

```
Nutanix (10 Gbps) → Staging VM → QCOW2 (1 Gbps) → Harvester
```

### Multiple VMs

Use the "Full migration" option (Menu 3 → Option 9) for batch operations (under development).

## Known Limitations

- Power on/off via API may not work on all Nutanix/Harvester versions
- Multi-disk VMs require separate image creation for each disk
- Guest customization (cloud-init) not yet implemented

## License

Internal use - Wyss Center for Bio and Neuroengineering

## Contributing

Contact: IT Infrastructure Team
