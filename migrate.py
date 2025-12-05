#!/usr/bin/env python3
"""
Nutanix to Harvester VM Migration Tool
======================================
Interactive menu to migrate VMs from Nutanix to Harvester
"""

import os
import sys
import yaml
import argparse

# Add lib to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import Colors, colored, format_size, format_timestamp
from lib import NutanixClient, HarvesterClient, MigrationActions


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


class MigrationTool:
    """Migration tool with interactive menu."""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        self.nutanix = None
        self.harvester = None
        self.actions = None
        self._selected_vm = None
    
    def clear_screen(self):
        os.system('clear' if os.name == 'posix' else 'cls')
    
    def print_header(self):
        self.clear_screen()
        print(colored("=" * 70, Colors.CYAN))
        print(colored("   NUTANIX → HARVESTER MIGRATION TOOL", Colors.BOLD + Colors.CYAN))
        print(colored("=" * 70, Colors.CYAN))
        if self._selected_vm:
            print(colored(f"   Selected VM: {self._selected_vm}", Colors.YELLOW))
        print()
    
    def print_menu(self, title: str, options: list):
        print(colored(f"\n{title}", Colors.BOLD))
        print(colored("-" * 40, Colors.BLUE))
        for key, desc in options:
            print(f"  {colored(key, Colors.GREEN)}. {desc}")
        print()
    
    def input_prompt(self, prompt: str = "Choice") -> str:
        return input(colored(f"{prompt} > ", Colors.YELLOW)).strip()
    
    def pause(self):
        input(colored("\nPress Enter to continue...", Colors.CYAN))
    
    # === Connection Methods ===
    
    def connect_nutanix(self) -> bool:
        try:
            print("Connecting to Nutanix Prism...")
            self.nutanix = NutanixClient(self.config['nutanix'])
            vms = self.nutanix.list_vms()
            print(colored(f"✅ Connected! {len(vms)} VMs found", Colors.GREEN))
            return True
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
            return False
    
    def connect_harvester(self) -> bool:
        try:
            print("Connecting to Harvester...")
            self.harvester = HarvesterClient(self.config['harvester'])
            nodes = self.harvester.get_nodes()
            print(colored(f"✅ Connected! {len(nodes)} nodes", Colors.GREEN))
            return True
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
            return False
    
    def init_actions(self):
        if not self.actions:
            self.actions = MigrationActions(self.config, self.nutanix, self.harvester)
    
    # === Nutanix Display Methods ===
    
    def list_nutanix_vms(self):
        if not self.nutanix and not self.connect_nutanix():
            return
        
        vms = self.nutanix.list_vms()
        
        print(f"\n{'='*110}")
        print(f"{'#':<4} {'VM Name':<35} {'State':<8} {'vCPU':<6} {'RAM':<10} {'Disks':<18}")
        print(f"{'='*110}")
        
        sorted_vms = sorted(vms, key=lambda x: x.get('spec', {}).get('name', '').lower())
        
        for idx, vm in enumerate(sorted_vms, 1):
            info = NutanixClient.parse_vm_info(vm)
            
            name = info['name'][:34] if info['name'] else 'N/A'
            state = info['power_state'] or 'N/A'
            vcpu = info['vcpu']
            ram = format_size(info['memory_mb'] * 1024 * 1024)
            
            disk_count = len(info['disks'])
            total_size = sum(d['size_bytes'] for d in info['disks'])
            disk_info = f"{disk_count}x ({format_size(total_size)})"
            
            state_color = Colors.GREEN if state == 'ON' else Colors.RED
            print(f"{idx:<4} {name:<35} {colored(state, state_color):<17} {vcpu:<6} {ram:<10} {disk_info:<18}")
        
        print(f"{'='*110}")
        print(f"Total: {len(vms)} VMs")
    
    def show_vm_details(self, vm_name: str = None):
        if not self.nutanix and not self.connect_nutanix():
            return
        
        if not vm_name:
            vm_name = self.input_prompt("VM name")
        
        if not vm_name:
            print(colored("No name specified", Colors.RED))
            return
        
        vm = self.nutanix.get_vm_by_name(vm_name)
        if not vm:
            print(colored(f"VM '{vm_name}' not found", Colors.RED))
            return
        
        info = NutanixClient.parse_vm_info(vm)
        
        print(colored(f"\n{'='*60}", Colors.CYAN))
        print(colored(f" VM: {info['name']}", Colors.BOLD))
        print(colored(f"{'='*60}", Colors.CYAN))
        
        print(colored("\n📋 General:", Colors.BOLD))
        print(f"   UUID: {info['uuid']}")
        state_color = Colors.GREEN if info['power_state'] == 'ON' else Colors.RED
        print(f"   State: {colored(info['power_state'], state_color)}")
        print(f"   vCPU: {info['vcpu']} ({info['num_sockets']} sockets x {info['num_vcpus_per_socket']} cores)")
        print(f"   RAM: {format_size(info['memory_mb'] * 1024 * 1024)}")
        print(f"   Boot: {info['boot_type']}")
        
        print(colored("\n💾 Disks:", Colors.BOLD))
        for i, disk in enumerate(info['disks']):
            print(f"   [{i}] {disk['adapter']}.{disk['index']} - {format_size(disk['size_bytes'])}")
            print(f"       UUID: {disk['uuid']}")
        
        print(colored("\n🌐 Network:", Colors.BOLD))
        for i, nic in enumerate(info['nics']):
            print(f"   [{i}] {nic['subnet']}")
            print(f"       MAC: {nic['mac']}, IP: {nic['ip'] or 'DHCP'}")
        
        self._selected_vm = info['name']
    
    def select_vm(self):
        vm_name = self.input_prompt("VM name to select")
        if vm_name:
            self._selected_vm = vm_name
            print(colored(f"✅ VM '{vm_name}' selected", Colors.GREEN))
    
    def list_nutanix_images(self):
        if not self.nutanix and not self.connect_nutanix():
            return
        
        images = self.nutanix.list_images()
        
        print(f"\n{'='*90}")
        print(f"{'Image Name':<40} {'Type':<15} {'Size':<15} {'State'}")
        print(f"{'='*90}")
        
        for img in sorted(images, key=lambda x: x.get('spec', {}).get('name', '').lower()):
            spec = img.get('spec', {})
            status = img.get('status', {})
            name = spec.get('name', 'N/A')[:39]
            img_type = spec.get('resources', {}).get('image_type', 'N/A')
            size = status.get('resources', {}).get('size_bytes', 0)
            state = status.get('state', 'N/A')
            
            print(f"{name:<40} {img_type:<15} {format_size(size):<15} {state}")
        
        print(f"{'='*90}")
        print(f"Total: {len(images)} images")
    
    def delete_nutanix_image(self):
        """Delete a Nutanix image (for cleanup after export)."""
        if not self.nutanix and not self.connect_nutanix():
            return
        
        images = self.nutanix.list_images()
        
        if not images:
            print(colored("❌ No images found", Colors.YELLOW))
            return
        
        print("\nAvailable images:")
        sorted_images = sorted(images, key=lambda x: x.get('spec', {}).get('name', '').lower())
        for i, img in enumerate(sorted_images, 1):
            name = img.get('spec', {}).get('name', 'N/A')
            size = img.get('status', {}).get('resources', {}).get('size_bytes', 0)
            print(f"  {i}. {name} ({format_size(size)})")
        
        choice = self.input_prompt("Image number to delete")
        try:
            idx = int(choice) - 1
            selected = sorted_images[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        image_name = selected.get('spec', {}).get('name')
        image_uuid = selected.get('metadata', {}).get('uuid')
        
        confirm = self.input_prompt(f"Delete '{image_name}'? (yes to confirm)")
        if confirm.lower() == 'yes':
            try:
                self.nutanix.delete_image(image_uuid)
                print(colored(f"✅ Deleted: {image_name}", Colors.GREEN))
            except Exception as e:
                print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
    # === Harvester Display Methods ===
    
    def list_harvester_vms(self):
        if not self.harvester and not self.connect_harvester():
            return
        
        vms = self.harvester.list_all_vms()
        
        try:
            vmis = self.harvester.list_all_vmis()
            running_vms = {vmi.get('metadata', {}).get('name') for vmi in vmis}
        except:
            running_vms = set()
        
        print(f"\n{'='*100}")
        print(f"{'VM Name':<35} {'Namespace':<15} {'Status':<12} {'CPU':<6} {'RAM':<10}")
        print(f"{'='*100}")
        
        for vm in sorted(vms, key=lambda x: x.get('metadata', {}).get('name', '').lower()):
            info = HarvesterClient.parse_vm_info(vm)
            name = info['name'][:34] if info['name'] else 'N/A'
            namespace = info['namespace'][:14] if info['namespace'] else 'N/A'
            
            # Check actual running status
            is_running = name in running_vms or info['status'] == 'Running'
            
            if is_running:
                status_str = colored("Running", Colors.GREEN)
            elif info['status'] and info['status'] != 'Unknown':
                status_str = colored(info['status'], Colors.YELLOW)
            else:
                status_str = colored("Stopped", Colors.RED)
            
            cpu = info['cpu_cores'] or 'N/A'
            memory = info['memory'] or 'N/A'
            
            print(f"{name:<35} {namespace:<15} {status_str:<21} {cpu:<6} {memory:<10}")
        
        print(f"{'='*100}")
        print(f"Total: {len(vms)} VMs ({len(running_vms)} running)")
    
    def list_harvester_images(self):
        if not self.harvester and not self.connect_harvester():
            return
        
        images = self.harvester.list_all_images()
        
        print(f"\n{'='*80}")
        print(f"{'Image Name':<40} {'Namespace':<20} {'Size':<15}")
        print(f"{'='*80}")
        
        for img in images:
            name = img.get('metadata', {}).get('name', 'N/A')[:39]
            ns = img.get('metadata', {}).get('namespace', 'N/A')[:19]
            size = img.get('status', {}).get('size', 0)
            print(f"{name:<40} {ns:<20} {format_size(size):<15}")
        
        print(f"{'='*80}")
        print(f"Total: {len(images)} images")
    
    def list_harvester_networks(self):
        if not self.harvester and not self.connect_harvester():
            return
        
        networks = self.harvester.list_all_networks()
        
        print(f"\n{'='*60}")
        print(f"{'Network Name':<40} {'Namespace'}")
        print(f"{'='*60}")
        
        for net in networks:
            name = net.get('metadata', {}).get('name', 'N/A')
            ns = net.get('metadata', {}).get('namespace', 'N/A')
            print(f"{name:<40} {ns}")
        
        print(f"{'='*60}")
    
    def list_harvester_storage(self):
        if not self.harvester and not self.connect_harvester():
            return
        
        scs = self.harvester.list_storage_classes()
        
        print(f"\n{'='*70}")
        print(f"{'Storage Class':<40} {'Provisioner':<30}")
        print(f"{'='*70}")
        
        for sc in scs:
            name = sc.get('metadata', {}).get('name', 'N/A')
            provisioner = sc.get('provisioner', 'N/A')
            annotations = sc.get('metadata', {}).get('annotations', {})
            default = "(default)" if annotations.get('storageclass.kubernetes.io/is-default-class') == 'true' else ""
            print(f"{name:<40} {provisioner:<30} {default}")
        
        print(f"{'='*70}")
    
    # === Migration Methods ===
    
    def check_staging(self):
        self.init_actions()
        result = self.actions.check_staging()
        
        print(f"\n📁 Staging: {result['path']}")
        
        if result['mounted']:
            print(colored("✅ Mounted", Colors.GREEN))
            if 'error' in result:
                print(colored(f"❌ Error: {result['error']}", Colors.RED))
            else:
                print(f"   Files: {len(result['files'])}")
                print(f"   Total size: {format_size(result['total_size'])}")
        else:
            print(colored("❌ Not mounted", Colors.RED))
            ceph_ip = self.config.get('ceph', {}).get('mon_ip', '10.16.16.140')
            print(f"   Mount command:")
            print(f"   mount -t ceph {ceph_ip}:6789:/volumes/_nogroup/migration-staging {result['path']} -o name=admin,secretfile=/etc/ceph/admin.secret")
    
    def list_staging_disks(self):
        """List all disk images in staging."""
        self.init_actions()
        
        if not self.actions.is_staging_mounted():
            print(colored(f"❌ Staging not mounted: {self.actions.staging_path}", Colors.RED))
            return
        
        files = self.actions.list_staging_files()
        
        if not files:
            print(colored("\n❌ No files in staging", Colors.YELLOW))
            return
        
        print(f"\n{'='*90}")
        print(f"{'#':<4} {'Filename':<40} {'Size':<15} {'Modified':<20} {'Type'}")
        print(f"{'='*90}")
        
        total_size = 0
        for idx, f in enumerate(files, 1):
            name = f['name'][:39]
            size = format_size(f['size'])
            total_size += f['size']
            mtime = format_timestamp(f['mtime'])
            
            # Detect type by extension
            if f['name'].endswith('.raw'):
                ftype = colored("RAW", Colors.YELLOW)
            elif f['name'].endswith('.qcow2'):
                ftype = colored("QCOW2", Colors.GREEN)
            elif f['name'].endswith('.vmdk'):
                ftype = colored("VMDK", Colors.BLUE)
            elif f['name'].endswith('.vhd') or f['name'].endswith('.vhdx'):
                ftype = colored("VHD", Colors.BLUE)
            elif f['name'].endswith('.iso'):
                ftype = colored("ISO", Colors.CYAN)
            else:
                ftype = "Other"
            
            print(f"{idx:<4} {name:<40} {size:<15} {mtime:<20} {ftype}")
        
        print(f"{'='*90}")
        print(f"Total: {len(files)} files, {format_size(total_size)}")
    
    def show_disk_info(self):
        """Show detailed info about a disk image."""
        self.init_actions()
        
        if not self.actions.is_staging_mounted():
            print(colored(f"❌ Staging not mounted", Colors.RED))
            return
        
        files = self.actions.list_staging_files()
        if not files:
            print(colored("❌ No files in staging", Colors.YELLOW))
            return
        
        # Show files and prompt for selection
        print("\nAvailable files:")
        for idx, f in enumerate(files, 1):
            print(f"  {idx}. {f['name']} ({format_size(f['size'])})")
        
        choice = self.input_prompt("File number")
        try:
            idx = int(choice) - 1
            selected = files[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        # Get detailed info
        info = self.actions.get_file_info(selected['name'])
        if not info:
            print(colored("Could not get file info", Colors.RED))
            return
        
        print(colored(f"\n{'='*60}", Colors.CYAN))
        print(colored(f" File: {info['name']}", Colors.BOLD))
        print(colored(f"{'='*60}", Colors.CYAN))
        
        print(f"   Path: {info['path']}")
        print(f"   Size on disk: {format_size(info['size'])}")
        print(f"   Modified: {format_timestamp(info['mtime'])}")
        
        if 'format' in info:
            print(colored("\n💾 Disk Image Info:", Colors.BOLD))
            print(f"   Format: {info['format']}")
            if 'virtual_size' in info:
                print(f"   Virtual size: {format_size(info['virtual_size'])}")
            if 'actual_size' in info:
                print(f"   Actual size: {format_size(info['actual_size'])}")
                if info['virtual_size']:
                    ratio = (1 - info['actual_size'] / info['virtual_size']) * 100
                    print(f"   Sparse savings: {ratio:.1f}%")
    
    def convert_disk(self):
        self.init_actions()
        
        raw_files = self.actions.list_raw_files()
        
        if not raw_files:
            print(colored("❌ No .raw files found in staging", Colors.RED))
            return
        
        print("\nAvailable RAW files:")
        for i, f in enumerate(raw_files, 1):
            print(f"  {i}. {f['name']} ({format_size(f['size'])})")
        
        choice = self.input_prompt("File number to convert (or 'all')")
        
        if choice.lower() == 'all':
            files_to_convert = raw_files
        else:
            try:
                idx = int(choice) - 1
                files_to_convert = [raw_files[idx]]
            except:
                print(colored("Invalid choice", Colors.RED))
                return
        
        for f in files_to_convert:
            print(f"\n🔄 Converting: {f['name']}")
            
            confirm = self.input_prompt("Start conversion? (y/n)")
            if confirm.lower() != 'y':
                continue
            
            def progress(pct):
                print(f"\r   Progress: {pct:.1f}%", end='', flush=True)
            
            result = self.actions.convert_raw_to_qcow2(f['path'], compress=True, progress_callback=progress)
            print()  # New line after progress
            
            if result['success']:
                print(colored(f"✅ Done: {format_size(result['size_before'])} → {format_size(result['size_after'])} ({result['reduction_pct']:.1f}% reduction)", Colors.GREEN))
                
                delete = self.input_prompt("Delete RAW file? (y/n)")
                if delete.lower() == 'y':
                    if self.actions.delete_file(f['path']):
                        print(colored("✅ RAW file deleted", Colors.GREEN))
            else:
                print(colored(f"❌ Error: {result['error']}", Colors.RED))
    
    def delete_staging_file(self):
        """Delete a file from staging."""
        self.init_actions()
        
        if not self.actions.is_staging_mounted():
            print(colored(f"❌ Staging not mounted", Colors.RED))
            return
        
        files = self.actions.list_staging_files()
        if not files:
            print(colored("❌ No files in staging", Colors.YELLOW))
            return
        
        print("\nFiles in staging:")
        for idx, f in enumerate(files, 1):
            print(f"  {idx}. {f['name']} ({format_size(f['size'])})")
        
        choice = self.input_prompt("File number to delete")
        try:
            idx = int(choice) - 1
            selected = files[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        confirm = self.input_prompt(f"Delete '{selected['name']}'? (yes to confirm)")
        if confirm.lower() == 'yes':
            if self.actions.delete_file(selected['path']):
                print(colored(f"✅ Deleted: {selected['name']}", Colors.GREEN))
            else:
                print(colored("❌ Failed to delete file", Colors.RED))
        else:
            print("Cancelled")
    
    def export_vm(self):
        if not self._selected_vm:
            print(colored("❌ No VM selected. Use 'Select VM' first.", Colors.RED))
            return
        
        print(colored(f"\n🚧 Export of '{self._selected_vm}' - Under development", Colors.YELLOW))
        print("\nPlanned steps:")
        print("  1. Create images from VM disks (via acli)")
        print("  2. Download images to staging")
        print("  3. Convert to QCOW2 sparse")
    
    def import_to_harvester(self):
        self.init_actions()
        
        if not self.harvester and not self.connect_harvester():
            return
        
        qcow2_files = self.actions.list_qcow2_files()
        
        if not qcow2_files:
            print(colored("❌ No .qcow2 files found in staging", Colors.RED))
            return
        
        print("\nAvailable QCOW2 files:")
        for i, f in enumerate(qcow2_files, 1):
            print(f"  {i}. {f['name']} ({format_size(f['size'])})")
        
        choice = self.input_prompt("File number to import")
        try:
            idx = int(choice) - 1
            selected_file = qcow2_files[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        image_name = self.input_prompt(f"Image name [{selected_file['name'].replace('.qcow2', '')}]")
        if not image_name:
            image_name = selected_file['name'].replace('.qcow2', '')
        
        # Get available namespaces
        namespaces = self.get_harvester_namespaces()
        
        print("\nAvailable namespaces:")
        for i, ns in enumerate(namespaces, 1):
            print(f"  {i}. {ns}")
        
        choice = self.input_prompt("Namespace number [1]")
        try:
            idx = int(choice) - 1 if choice else 0
            namespace = namespaces[idx]
        except:
            namespace = namespaces[0]
        
        print(f"\n🚀 Starting HTTP server...")
        http_url = self.actions.start_http_server(8080)
        print(colored(f"✅ Server running at {http_url}", Colors.GREEN))
        
        print(f"\n📤 Creating image in Harvester ({namespace})...")
        try:
            result = self.actions.create_harvester_image(image_name, selected_file['name'], http_url, namespace)
            print(colored(f"✅ Image created: {image_name} in {namespace}", Colors.GREEN))
            print("   Monitor progress in Harvester UI")
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
        
        self.input_prompt("Press Enter when image download is complete to stop HTTP server")
        self.actions.stop_http_server()
        print(colored("✅ HTTP server stopped", Colors.GREEN))
    
    def get_harvester_namespaces(self) -> list:
        """Get list of namespaces from Harvester."""
        try:
            result = self.harvester._request("GET", "/api/v1/namespaces")
            namespaces = []
            # System namespace prefixes to exclude
            exclude_prefixes = ('kube-', 'cattle-', 'fleet-', 'local', 'longhorn-', 'harvester-system')
            
            for ns in result.get('items', []):
                name = ns.get('metadata', {}).get('name', '')
                # Keep harvester-public, exclude other system namespaces
                if name == 'harvester-public':
                    namespaces.append(name)
                elif not any(name.startswith(p) for p in exclude_prefixes):
                    namespaces.append(name)
            return sorted(namespaces) if namespaces else ['default']
        except:
            return ['default']
    
    def create_harvester_vm(self):
        """Create a VM in Harvester from Nutanix specs."""
        if not self.harvester and not self.connect_harvester():
            return
        
        # Get Nutanix VM specs if selected
        vm_info = None
        if self._selected_vm and self.nutanix:
            print(f"\n📋 Getting specs from Nutanix VM: {self._selected_vm}")
            vm = self.nutanix.get_vm_by_name(self._selected_vm)
            if vm:
                vm_info = NutanixClient.parse_vm_info(vm)
                print(colored(f"   vCPU: {vm_info['vcpu']}, RAM: {format_size(vm_info['memory_mb'] * 1024 * 1024)}, Boot: {vm_info['boot_type']}", Colors.GREEN))
        
        # VM Name
        default_name = self._selected_vm or ""
        vm_name = self.input_prompt(f"VM name [{default_name}]")
        if not vm_name:
            vm_name = default_name
        if not vm_name:
            print(colored("❌ VM name required", Colors.RED))
            return
        
        # Get available namespaces
        namespaces = self.get_harvester_namespaces()
        
        print("\nAvailable namespaces:")
        for i, ns in enumerate(namespaces, 1):
            print(f"  {i}. {ns}")
        
        choice = self.input_prompt("Namespace number [1]")
        try:
            idx = int(choice) - 1 if choice else 0
            namespace = namespaces[idx]
        except:
            namespace = namespaces[0]
        
        # Get available images
        images = self.harvester.list_all_images()
        active_images = [img for img in images if img.get('status', {}).get('progress', 0) == 100 or 
                         img.get('metadata', {}).get('state') == 'Active']
        
        if not active_images:
            print(colored("❌ No active images available", Colors.RED))
            return
        
        print("\nAvailable images:")
        for i, img in enumerate(active_images, 1):
            name = img.get('metadata', {}).get('name', 'N/A')
            ns = img.get('metadata', {}).get('namespace', 'N/A')
            size = img.get('status', {}).get('size', 0)
            print(f"  {i}. {name} ({ns}) - {format_size(size)}")
        
        choice = self.input_prompt("Image number")
        try:
            idx = int(choice) - 1
            selected_image = active_images[idx]
            image_name = selected_image.get('metadata', {}).get('name')
            image_ns = selected_image.get('metadata', {}).get('namespace')
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        # Get available networks
        networks = self.harvester.list_all_networks()
        
        print("\nAvailable networks:")
        for i, net in enumerate(networks, 1):
            name = net.get('metadata', {}).get('name', 'N/A')
            ns = net.get('metadata', {}).get('namespace', 'N/A')
            print(f"  {i}. {name} ({ns})")
        
        choice = self.input_prompt("Network number")
        try:
            idx = int(choice) - 1
            selected_net = networks[idx]
            network_name = f"{selected_net.get('metadata', {}).get('namespace')}/{selected_net.get('metadata', {}).get('name')}"
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        # Get storage classes
        storage_classes = self.harvester.list_storage_classes()
        
        print("\nAvailable storage classes:")
        for i, sc in enumerate(storage_classes, 1):
            name = sc.get('metadata', {}).get('name', 'N/A')
            default = "(default)" if sc.get('metadata', {}).get('annotations', {}).get('storageclass.kubernetes.io/is-default-class') == 'true' else ""
            print(f"  {i}. {name} {default}")
        
        choice = self.input_prompt("Storage class number [1]")
        try:
            idx = int(choice) - 1 if choice else 0
            storage_class = storage_classes[idx].get('metadata', {}).get('name')
        except:
            storage_class = storage_classes[0].get('metadata', {}).get('name')
        
        # CPU, RAM, Boot type
        default_cpu = vm_info['vcpu'] if vm_info else 2
        default_ram = vm_info['memory_mb'] // 1024 if vm_info else 4
        default_boot = vm_info['boot_type'] if vm_info else 'BIOS'
        default_disk_size = vm_info['disks'][0]['size_bytes'] // (1024**3) if vm_info and vm_info['disks'] else 50
        
        cpu = self.input_prompt(f"CPU cores [{default_cpu}]")
        cpu = int(cpu) if cpu else default_cpu
        
        ram = self.input_prompt(f"RAM in GB [{default_ram}]")
        ram = int(ram) if ram else default_ram
        
        disk_size = self.input_prompt(f"Disk size in GB [{default_disk_size}]")
        disk_size = int(disk_size) if disk_size else default_disk_size
        
        boot = self.input_prompt(f"Boot type (BIOS/UEFI) [{default_boot}]")
        boot = boot.upper() if boot else default_boot
        
        # Summary
        print(colored(f"\n📋 VM Configuration:", Colors.BOLD))
        print(f"   Name: {vm_name}")
        print(f"   Namespace: {namespace}")
        print(f"   Image: {image_name} ({image_ns})")
        print(f"   Network: {network_name}")
        print(f"   Storage: {storage_class}")
        print(f"   CPU: {cpu} cores")
        print(f"   RAM: {ram} GB")
        print(f"   Disk: {disk_size} GB")
        print(f"   Boot: {boot}")
        
        confirm = self.input_prompt("\nCreate VM? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        # Build manifest
        print("\n🚀 Creating VM...")
        try:
            manifest = {
                "apiVersion": "kubevirt.io/v1",
                "kind": "VirtualMachine",
                "metadata": {
                    "name": vm_name,
                    "namespace": namespace,
                    "labels": {
                        "harvesterhci.io/creator": "harvesterhci"
                    }
                },
                "spec": {
                    "running": False,
                    "template": {
                        "metadata": {
                            "labels": {
                                "harvesterhci.io/vmName": vm_name
                            }
                        },
                        "spec": {
                            "domain": {
                                "cpu": {
                                    "cores": cpu,
                                    "sockets": 1,
                                    "threads": 1
                                },
                                "memory": {
                                    "guest": f"{ram}Gi"
                                },
                                "devices": {
                                    "disks": [
                                        {
                                            "name": "disk-0",
                                            "disk": {
                                                "bus": "virtio"
                                            },
                                            "bootOrder": 1
                                        }
                                    ],
                                    "interfaces": [
                                        {
                                            "name": "nic-0",
                                            "bridge": {}
                                        }
                                    ]
                                },
                                "machine": {
                                    "type": "q35"
                                }
                            },
                            "networks": [
                                {
                                    "name": "nic-0",
                                    "multus": {
                                        "networkName": network_name
                                    }
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "disk-0",
                                    "dataVolume": {
                                        "name": f"{vm_name}-disk-0"
                                    }
                                }
                            ]
                        }
                    },
                    "dataVolumeTemplates": [
                        {
                            "metadata": {
                                "name": f"{vm_name}-disk-0",
                                "annotations": {
                                    "harvesterhci.io/imageId": f"{image_ns}/{image_name}"
                                }
                            },
                            "spec": {
                                "pvc": {
                                    "accessModes": ["ReadWriteMany"],
                                    "resources": {
                                        "requests": {
                                            "storage": f"{disk_size}Gi"
                                        }
                                    },
                                    "storageClassName": storage_class
                                },
                                "source": {
                                    "blank": {}
                                }
                            }
                        }
                    ]
                }
            }
            
            # Add UEFI if needed
            if boot == "UEFI":
                manifest['spec']['template']['spec']['domain']['firmware'] = {
                    "bootloader": {
                        "efi": {
                            "secureBoot": False
                        }
                    }
                }
            
            result = self.harvester.create_vm(manifest)
            print(colored(f"✅ VM created: {vm_name} in {namespace}", Colors.GREEN))
            print("   Start it from Harvester UI or wait for disk provisioning")
            
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    # === Menus ===
    
    def menu_nutanix(self):
        while True:
            self.print_header()
            self.print_menu("NUTANIX", [
                ("1", "List VMs"),
                ("2", "VM details"),
                ("3", "Select VM"),
                ("4", "List images"),
                ("5", "Delete image (cleanup)"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.list_nutanix_vms()
                self.pause()
            elif choice == "2":
                self.show_vm_details()
                self.pause()
            elif choice == "3":
                self.select_vm()
                self.pause()
            elif choice == "4":
                self.list_nutanix_images()
                self.pause()
            elif choice == "5":
                self.delete_nutanix_image()
                self.pause()
            elif choice == "0":
                break
    
    def menu_harvester(self):
        while True:
            self.print_header()
            self.print_menu("HARVESTER", [
                ("1", "List VMs"),
                ("2", "List images"),
                ("3", "List networks"),
                ("4", "List storage classes"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.list_harvester_vms()
                self.pause()
            elif choice == "2":
                self.list_harvester_images()
                self.pause()
            elif choice == "3":
                self.list_harvester_networks()
                self.pause()
            elif choice == "4":
                self.list_harvester_storage()
                self.pause()
            elif choice == "0":
                break
    
    def menu_migration(self):
        while True:
            self.print_header()
            self.print_menu("MIGRATION", [
                ("1", "Check staging"),
                ("2", "List staging disks"),
                ("3", "Disk image details"),
                ("4", "Export VM (Nutanix → Staging)"),
                ("5", "Convert RAW → QCOW2"),
                ("6", "Import image to Harvester"),
                ("7", "Create VM in Harvester"),
                ("8", "Delete staging file"),
                ("9", "Full migration"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.check_staging()
                self.pause()
            elif choice == "2":
                self.list_staging_disks()
                self.pause()
            elif choice == "3":
                self.show_disk_info()
                self.pause()
            elif choice == "4":
                self.export_vm()
                self.pause()
            elif choice == "5":
                self.convert_disk()
                self.pause()
            elif choice == "6":
                self.import_to_harvester()
                self.pause()
            elif choice == "7":
                self.create_harvester_vm()
                self.pause()
            elif choice == "8":
                self.delete_staging_file()
                self.pause()
            elif choice == "9":
                print(colored("\n🚧 Full migration - Under development", Colors.YELLOW))
                self.pause()
            elif choice == "0":
                break
    
    def menu_config(self):
        self.print_header()
        print(colored("\n⚙️  CURRENT CONFIGURATION", Colors.BOLD))
        print(colored("-" * 40, Colors.BLUE))
        
        print(f"\nNutanix:")
        print(f"   Prism IP: {self.config['nutanix']['prism_ip']}")
        print(f"   Username: {self.config['nutanix']['username']}")
        
        print(f"\nHarvester:")
        print(f"   API URL: {self.config['harvester']['api_url']}")
        print(f"   Namespace: {self.config['harvester'].get('namespace', 'default')}")
        
        print(f"\nCeph:")
        print(f"   Mon IP: {self.config.get('ceph', {}).get('mon_ip', 'N/A')}")
        
        print(f"\nTransfer:")
        print(f"   Staging: {self.config.get('transfer', {}).get('staging_mount', '/mnt/staging')}")
        
        self.pause()
    
    def main_menu(self):
        self.print_header()
        print("Initializing...")
        self.connect_nutanix()
        self.connect_harvester()
        self.pause()
        
        while True:
            self.print_header()
            self.print_menu("MAIN MENU", [
                ("1", "Nutanix"),
                ("2", "Harvester"),
                ("3", "Migration"),
                ("4", "Configuration"),
                ("q", "Quit")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.menu_nutanix()
            elif choice == "2":
                self.menu_harvester()
            elif choice == "3":
                self.menu_migration()
            elif choice == "4":
                self.menu_config()
            elif choice.lower() == "q":
                print(colored("\nGoodbye! 👋", Colors.CYAN))
                break


def main():
    parser = argparse.ArgumentParser(description="Nutanix to Harvester Migration Tool")
    parser.add_argument("-c", "--config", default="config.yaml", help="Configuration file")
    parser.add_argument("command", nargs="?", help="Direct command (optional)")
    parser.add_argument("args", nargs="*", help="Arguments")
    
    args = parser.parse_args()
    
    tool = MigrationTool(args.config)
    
    if not args.command:
        tool.main_menu()
    else:
        # Direct command mode
        if args.command == "list":
            tool.list_nutanix_vms()
        elif args.command == "list-harvester":
            tool.list_harvester_vms()
        elif args.command == "list-images":
            tool.list_harvester_images()
        elif args.command == "list-networks":
            tool.list_harvester_networks()
        elif args.command == "list-staging":
            tool.list_staging_disks()
        elif args.command == "show":
            vm_name = args.args[0] if args.args else None
            tool.show_vm_details(vm_name)
        elif args.command == "test-harvester":
            tool.connect_harvester()
        else:
            print(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
