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
        
        print("\nAvailable images (Enter to cancel):")
        sorted_images = sorted(images, key=lambda x: x.get('spec', {}).get('name', '').lower())
        for i, img in enumerate(sorted_images, 1):
            name = img.get('spec', {}).get('name', 'N/A')
            size = img.get('status', {}).get('resources', {}).get('size_bytes', 0)
            print(f"  {i}. {name} ({format_size(size)})")
        
        choice = self.input_prompt("Image number to delete")
        if not choice:
            return
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
    
    def power_on_nutanix_vm(self):
        """Power on a Nutanix VM."""
        if not self.nutanix and not self.connect_nutanix():
            return
        
        vms = self.nutanix.list_vms()
        
        # Filter OFF VMs
        off_vms = [vm for vm in vms if NutanixClient.parse_vm_info(vm).get('power_state') == 'OFF']
        
        if not off_vms:
            print(colored("❌ No powered off VMs found", Colors.YELLOW))
            return
        
        print("\nPowered OFF VMs (Enter to cancel):")
        sorted_vms = sorted(off_vms, key=lambda x: x.get('spec', {}).get('name', '').lower())
        for i, vm in enumerate(sorted_vms, 1):
            info = NutanixClient.parse_vm_info(vm)
            print(f"  {i}. {info['name']}")
        
        choice = self.input_prompt("VM number to power ON")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_vms[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        info = NutanixClient.parse_vm_info(selected)
        vm_name = info['name']
        vm_uuid = info['uuid']
        
        confirm = self.input_prompt(f"Power ON '{vm_name}'? (y/n)")
        if confirm.lower() == 'y':
            try:
                print(f"🚀 Starting {vm_name}...")
                self.nutanix.power_on_vm(vm_uuid)
                print(colored(f"✅ Power ON request sent for: {vm_name}", Colors.GREEN))
            except Exception as e:
                print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
    def power_off_nutanix_vm(self):
        """Power off a Nutanix VM."""
        if not self.nutanix and not self.connect_nutanix():
            return
        
        vms = self.nutanix.list_vms()
        
        # Filter ON VMs
        on_vms = [vm for vm in vms if NutanixClient.parse_vm_info(vm).get('power_state') == 'ON']
        
        if not on_vms:
            print(colored("❌ No powered on VMs found", Colors.YELLOW))
            return
        
        print("\nPowered ON VMs (Enter to cancel):")
        sorted_vms = sorted(on_vms, key=lambda x: x.get('spec', {}).get('name', '').lower())
        for i, vm in enumerate(sorted_vms, 1):
            info = NutanixClient.parse_vm_info(vm)
            print(f"  {i}. {info['name']}")
        
        choice = self.input_prompt("VM number to power OFF")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_vms[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        info = NutanixClient.parse_vm_info(selected)
        vm_name = info['name']
        vm_uuid = info['uuid']
        
        confirm = self.input_prompt(f"Power OFF '{vm_name}'? (y/n)")
        if confirm.lower() == 'y':
            try:
                print(f"🛑 Stopping {vm_name}...")
                self.nutanix.power_off_vm(vm_uuid)
                print(colored(f"✅ Power OFF request sent for: {vm_name}", Colors.GREEN))
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
    
    def delete_harvester_image(self):
        """Delete a Harvester image."""
        if not self.harvester and not self.connect_harvester():
            return
        
        images = self.harvester.list_all_images()
        
        if not images:
            print(colored("❌ No images found", Colors.YELLOW))
            return
        
        print("\nAvailable images (Enter to cancel):")
        sorted_images = sorted(images, key=lambda x: x.get('metadata', {}).get('name', '').lower())
        for i, img in enumerate(sorted_images, 1):
            name = img.get('metadata', {}).get('name', 'N/A')
            ns = img.get('metadata', {}).get('namespace', 'N/A')
            size = img.get('status', {}).get('size', 0)
            print(f"  {i}. {name} ({ns}) - {format_size(size)}")
        
        choice = self.input_prompt("Image number to delete")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_images[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        image_name = selected.get('metadata', {}).get('name')
        image_ns = selected.get('metadata', {}).get('namespace')
        
        confirm = self.input_prompt(f"Delete '{image_name}' from {image_ns}? (yes to confirm)")
        if confirm.lower() == 'yes':
            try:
                self.harvester.delete_image(image_name, image_ns)
                print(colored(f"✅ Deleted: {image_name}", Colors.GREEN))
            except Exception as e:
                error_msg = str(e)
                if "422" in error_msg or "being used" in error_msg.lower():
                    print(colored(f"❌ Cannot delete: Image is being used by a volume", Colors.RED))
                    print(colored(f"   → First delete the volume (Menu 8), then retry", Colors.YELLOW))
                else:
                    print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
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
    
    def list_harvester_volumes(self):
        """List all volumes (PVCs) in Harvester."""
        if not self.harvester and not self.connect_harvester():
            return
        
        pvcs = self.harvester.list_all_pvcs()
        
        print(f"\n{'='*90}")
        print(f"{'Volume Name':<45} {'Namespace':<20} {'Size':<12} {'Status'}")
        print(f"{'='*90}")
        
        for pvc in sorted(pvcs, key=lambda x: x.get('metadata', {}).get('name', '').lower()):
            name = pvc.get('metadata', {}).get('name', 'N/A')[:44]
            ns = pvc.get('metadata', {}).get('namespace', 'N/A')[:19]
            size = pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
            status = pvc.get('status', {}).get('phase', 'N/A')
            print(f"{name:<45} {ns:<20} {size:<12} {status}")
        
        print(f"{'='*90}")
        print(f"Total: {len(pvcs)} volumes")
    
    def delete_harvester_volume(self):
        """Delete a Harvester volume (PVC)."""
        if not self.harvester and not self.connect_harvester():
            return
        
        pvcs = self.harvester.list_all_pvcs()
        
        if not pvcs:
            print(colored("❌ No volumes found", Colors.YELLOW))
            return
        
        print("\nAvailable volumes (Enter to cancel):")
        sorted_pvcs = sorted(pvcs, key=lambda x: x.get('metadata', {}).get('name', '').lower())
        for i, pvc in enumerate(sorted_pvcs, 1):
            name = pvc.get('metadata', {}).get('name', 'N/A')
            ns = pvc.get('metadata', {}).get('namespace', 'N/A')
            size = pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
            print(f"  {i}. {name} ({ns}) - {size}")
        
        choice = self.input_prompt("Volume number to delete")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_pvcs[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        vol_name = selected.get('metadata', {}).get('name')
        vol_ns = selected.get('metadata', {}).get('namespace')
        
        confirm = self.input_prompt(f"Delete volume '{vol_name}' from {vol_ns}? (yes to confirm)")
        if confirm.lower() == 'yes':
            try:
                self.harvester.delete_pvc(vol_name, vol_ns)
                print(colored(f"✅ Deleted: {vol_name}", Colors.GREEN))
            except Exception as e:
                print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
    def dissociate_vm_from_image(self):
        """Clone VM volume to dissociate it from the source image."""
        if not self.harvester and not self.connect_harvester():
            return
        
        print(colored("\n🔗 Dissociate VM from Image", Colors.BOLD))
        print("   This will clone the VM's volume(s) to remove the backing image dependency.")
        print("   After this, you can delete the Harvester image.\n")
        
        # List VMs
        vms = self.harvester.list_all_vms()
        stopped_vms = []
        
        # Check VMIs for running status
        vmis = self.harvester.list_all_vmis()
        running_names = {vmi.get('metadata', {}).get('name') for vmi in vmis}
        
        for vm in vms:
            vm_name = vm.get('metadata', {}).get('name')
            if vm_name not in running_names:
                stopped_vms.append(vm)
        
        if not stopped_vms:
            print(colored("❌ No stopped VMs found. Stop the VM first!", Colors.YELLOW))
            return
        
        print("Stopped VMs (Enter to cancel):")
        for i, vm in enumerate(stopped_vms, 1):
            name = vm.get('metadata', {}).get('name', 'N/A')
            ns = vm.get('metadata', {}).get('namespace', 'N/A')
            print(f"  {i}. {name} ({ns})")
        
        choice = self.input_prompt("VM number")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected_vm = stopped_vms[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        vm_name = selected_vm.get('metadata', {}).get('name')
        vm_ns = selected_vm.get('metadata', {}).get('namespace')
        
        # Get VM volumes
        spec = selected_vm.get('spec', {})
        template_spec = spec.get('template', {}).get('spec', {})
        volumes = template_spec.get('volumes', [])
        data_volume_templates = spec.get('dataVolumeTemplates', [])
        
        # Find volumes linked to images
        volumes_to_clone = []
        for dvt in data_volume_templates:
            dvt_name = dvt.get('metadata', {}).get('name')
            annotations = dvt.get('metadata', {}).get('annotations', {})
            image_id = annotations.get('harvesterhci.io/imageId')
            if image_id:
                volumes_to_clone.append({
                    'name': dvt_name,
                    'image_id': image_id
                })
        
        if not volumes_to_clone:
            print(colored("✅ VM has no image-linked volumes. Nothing to dissociate.", Colors.GREEN))
            return
        
        print(f"\n📋 Found {len(volumes_to_clone)} volume(s) linked to images:")
        for vol in volumes_to_clone:
            print(f"   - {vol['name']} → {vol['image_id']}")
        
        confirm = self.input_prompt("\nClone these volumes to dissociate from images? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        # Clone each volume
        cloned_volumes = []
        for vol in volumes_to_clone:
            old_name = vol['name']
            new_name = f"{old_name}-standalone"
            
            print(f"\n🔄 Cloning {old_name} → {new_name}...")
            try:
                self.harvester.clone_pvc(old_name, new_name, vm_ns)
                print(colored(f"   ✅ Clone created: {new_name}", Colors.GREEN))
                cloned_volumes.append({
                    'old': old_name,
                    'new': new_name
                })
            except Exception as e:
                print(colored(f"   ❌ Clone failed: {e}", Colors.RED))
                return
        
        # Wait for clones to be ready
        print("\n⏳ Waiting for clones to be ready...")
        import time
        for _ in range(60):  # Wait up to 60 seconds
            all_ready = True
            for vol in cloned_volumes:
                try:
                    pvc = self.harvester.get_pvc(vol['new'], vm_ns)
                    phase = pvc.get('status', {}).get('phase', '')
                    if phase != 'Bound':
                        all_ready = False
                        break
                except:
                    all_ready = False
                    break
            
            if all_ready:
                print(colored("   ✅ All clones ready!", Colors.GREEN))
                break
            time.sleep(2)
            print("   .", end='', flush=True)
        else:
            print(colored("\n   ⚠️  Timeout waiting for clones. Check Harvester UI.", Colors.YELLOW))
        
        # Update VM to use cloned volumes
        print("\n🔧 Updating VM to use cloned volumes...")
        try:
            for vol in cloned_volumes:
                self.harvester.update_vm_volume(vm_name, vol['old'], vol['new'], vm_ns)
            print(colored(f"   ✅ VM updated to use standalone volumes", Colors.GREEN))
        except Exception as e:
            print(colored(f"   ❌ Error updating VM: {e}", Colors.RED))
            print(colored("   You may need to update the VM manually in Harvester UI", Colors.YELLOW))
            return
        
        # Offer to delete old volumes
        delete_old = self.input_prompt("\nDelete old image-linked volumes? (y/n)")
        if delete_old.lower() == 'y':
            for vol in cloned_volumes:
                try:
                    self.harvester.delete_pvc(vol['old'], vm_ns)
                    print(colored(f"   ✅ Deleted: {vol['old']}", Colors.GREEN))
                except Exception as e:
                    print(colored(f"   ⚠️  Could not delete {vol['old']}: {e}", Colors.YELLOW))
        
        print(colored("\n✅ VM is now dissociated from images!", Colors.GREEN))
        print(colored("   You can now delete the Harvester images (Menu → Delete image)", Colors.CYAN))
    
    def power_on_harvester_vm(self):
        """Power on a Harvester VM."""
        if not self.harvester and not self.connect_harvester():
            return
        
        vms = self.harvester.list_all_vms()
        vmis = self.harvester.list_all_vmis()
        running_names = {vmi.get('metadata', {}).get('name') for vmi in vmis}
        
        # Filter stopped VMs (not in VMIs list)
        stopped_vms = [vm for vm in vms if vm.get('metadata', {}).get('name') not in running_names]
        
        if not stopped_vms:
            print(colored("❌ No stopped VMs found", Colors.YELLOW))
            return
        
        print("\nStopped VMs (Enter to cancel):")
        sorted_vms = sorted(stopped_vms, key=lambda x: x.get('metadata', {}).get('name', '').lower())
        for i, vm in enumerate(sorted_vms, 1):
            name = vm.get('metadata', {}).get('name', 'N/A')
            ns = vm.get('metadata', {}).get('namespace', 'N/A')
            print(f"  {i}. {name} ({ns})")
        
        choice = self.input_prompt("VM number to start")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_vms[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        vm_name = selected.get('metadata', {}).get('name')
        vm_ns = selected.get('metadata', {}).get('namespace')
        
        confirm = self.input_prompt(f"Start '{vm_name}'? (y/n)")
        if confirm.lower() == 'y':
            try:
                print(f"🚀 Starting {vm_name}...")
                self.harvester.start_vm(vm_name, vm_ns)
                print(colored(f"✅ Start request sent for: {vm_name}", Colors.GREEN))
            except Exception as e:
                print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
    def power_off_harvester_vm(self):
        """Power off a Harvester VM."""
        if not self.harvester and not self.connect_harvester():
            return
        
        vms = self.harvester.list_all_vms()
        vmis = self.harvester.list_all_vmis()
        running_names = {vmi.get('metadata', {}).get('name') for vmi in vmis}
        
        # Filter running VMs (present in VMIs list)
        running_vms = [vm for vm in vms if vm.get('metadata', {}).get('name') in running_names]
        
        if not running_vms:
            print(colored("❌ No running VMs found", Colors.YELLOW))
            return
        
        print("\nRunning VMs (Enter to cancel):")
        sorted_vms = sorted(running_vms, key=lambda x: x.get('metadata', {}).get('name', '').lower())
        for i, vm in enumerate(sorted_vms, 1):
            name = vm.get('metadata', {}).get('name', 'N/A')
            ns = vm.get('metadata', {}).get('namespace', 'N/A')
            print(f"  {i}. {name} ({ns})")
        
        choice = self.input_prompt("VM number to stop")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_vms[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        vm_name = selected.get('metadata', {}).get('name')
        vm_ns = selected.get('metadata', {}).get('namespace')
        
        confirm = self.input_prompt(f"Stop '{vm_name}'? (y/n)")
        if confirm.lower() == 'y':
            try:
                print(f"🛑 Stopping {vm_name}...")
                self.harvester.stop_vm(vm_name, vm_ns)
                print(colored(f"✅ Stop request sent for: {vm_name}", Colors.GREEN))
            except Exception as e:
                print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
    def delete_harvester_vm(self):
        """Delete a Harvester VM."""
        if not self.harvester and not self.connect_harvester():
            return
        
        vms = self.harvester.list_all_vms()
        vmis = self.harvester.list_all_vmis()
        running_names = {vmi.get('metadata', {}).get('name') for vmi in vmis}
        
        if not vms:
            print(colored("❌ No VMs found", Colors.YELLOW))
            return
        
        print("\nAll VMs (Enter to cancel):")
        sorted_vms = sorted(vms, key=lambda x: x.get('metadata', {}).get('name', '').lower())
        for i, vm in enumerate(sorted_vms, 1):
            name = vm.get('metadata', {}).get('name', 'N/A')
            ns = vm.get('metadata', {}).get('namespace', 'N/A')
            is_running = name in running_names
            status = "🟢 Running" if is_running else "🔴 Stopped"
            print(f"  {i}. {status} {name} ({ns})")
        
        choice = self.input_prompt("VM number to delete")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            selected = sorted_vms[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        vm_name = selected.get('metadata', {}).get('name')
        vm_ns = selected.get('metadata', {}).get('namespace')
        is_running = vm_name in running_names
        
        if is_running:
            print(colored("⚠️  VM is running! Stop it first.", Colors.YELLOW))
            return
        
        confirm = self.input_prompt(f"DELETE '{vm_name}'? (yes to confirm)")
        if confirm.lower() == 'yes':
            try:
                print(f"🗑️  Deleting {vm_name}...")
                self.harvester.delete_vm(vm_name, vm_ns)
                print(colored(f"✅ Deleted: {vm_name}", Colors.GREEN))
            except Exception as e:
                print(colored(f"❌ Error: {e}", Colors.RED))
        else:
            print("Cancelled")
    
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
        if not choice:
            return
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
        
        # Choose import method
        print(colored("\n📤 Import Method:", Colors.BOLD))
        print("  1. HTTP - Start HTTP server, Harvester downloads from it")
        print("     (Requires network access from Harvester to this machine)")
        print("  2. Upload - Push file directly to Harvester")
        print("     (Uses virtctl or CDI upload proxy)")
        
        method = self.input_prompt("Method (1/2) [1]")
        if not method or method == "1":
            self._import_via_http(image_name, selected_file, namespace)
        elif method == "2":
            self._import_via_upload(image_name, selected_file, namespace)
        else:
            print(colored("Invalid choice", Colors.RED))
    
    def _import_via_http(self, image_name: str, selected_file: dict, namespace: str):
        """Import image via HTTP server method."""
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
            self.actions.stop_http_server()
            return
        
        self.input_prompt("Press Enter when image download is complete to stop HTTP server")
        self.actions.stop_http_server()
        print(colored("✅ HTTP server stopped", Colors.GREEN))
    
    def _import_via_upload(self, image_name: str, selected_file: dict, namespace: str):
        """Import image via direct upload to Harvester."""
        print(colored("\n📤 Upload Method", Colors.BOLD))
        print("\nThis method requires 'virtctl' to be installed on this machine.")
        print("Install: https://kubevirt.io/user-guide/operations/virtctl/")
        
        # Check if virtctl is available
        import shutil
        virtctl_path = shutil.which('virtctl')
        
        if not virtctl_path:
            print(colored("\n⚠️  'virtctl' not found in PATH", Colors.YELLOW))
            print("\nAlternative: Manual upload via Harvester UI")
            print(f"  1. Go to Harvester UI → Images → Create")
            print(f"  2. Select 'Upload' as source type")
            print(f"  3. Upload file: {selected_file['path']}")
            print(f"  4. Name: {image_name}")
            print(f"  5. Namespace: {namespace}")
            return
        
        print(colored(f"✅ virtctl found: {virtctl_path}", Colors.GREEN))
        
        # Get kubeconfig path
        kubeconfig = self.input_prompt("Kubeconfig path [~/.kube/harvester.yaml]")
        if not kubeconfig:
            kubeconfig = os.path.expanduser("~/.kube/harvester.yaml")
        
        # Build virtctl command
        file_size = selected_file['size']
        print(f"\n📤 Uploading {selected_file['name']} ({format_size(file_size)}) to Harvester...")
        print(colored("   This may take a while for large images...", Colors.YELLOW))
        
        # Create image first, then upload
        import subprocess
        
        # Use virtctl to upload
        cmd = [
            'virtctl', 'image-upload',
            f'--image-path={selected_file["path"]}',
            f'--storage-class=harvester-longhorn',
            f'--size={file_size}',
            f'--uploadproxy-url=https://{self.config["harvester"]["api_url"].replace("https://", "").split(":")[0]}:31001',
            f'--namespace={namespace}',
            f'--kubeconfig={kubeconfig}',
            '--insecure',
            '--force-bind',
            f'pvc/{image_name}'
        ]
        
        print(f"Command: {' '.join(cmd)}")
        confirm = self.input_prompt("\nRun upload? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print(colored(f"✅ Upload complete!", Colors.GREEN))
                print(result.stdout)
            else:
                print(colored(f"❌ Upload failed", Colors.RED))
                print(result.stderr)
                print("\nAlternative: Use HTTP method or upload via Harvester UI")
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
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
        """Create a VM in Harvester from Nutanix specs - using imported Harvester images."""
        if not self.harvester and not self.connect_harvester():
            return
        
        # Get Nutanix VM specs if selected
        vm_info = None
        source_mac = None
        source_ip = None
        source_disks = []
        if self._selected_vm and self.nutanix:
            print(f"\n📋 Getting specs from Nutanix VM: {self._selected_vm}")
            vm = self.nutanix.get_vm_by_name(self._selected_vm)
            if vm:
                vm_info = NutanixClient.parse_vm_info(vm)
                print(colored(f"   vCPU: {vm_info['vcpu']}, RAM: {format_size(vm_info['memory_mb'] * 1024 * 1024)}, Boot: {vm_info['boot_type']}", Colors.GREEN))
                
                # Display disk info
                if vm_info['disks']:
                    print(colored(f"\n💾 Source Disks ({len(vm_info['disks'])}):", Colors.BOLD))
                    for i, disk in enumerate(vm_info['disks']):
                        size_gb = disk['size_bytes'] // (1024**3)
                        adapter = disk.get('adapter', 'N/A')
                        index = disk.get('index', i)
                        print(f"   Disk {i}: {adapter}.{index} - {size_gb} GB")
                        source_disks.append({
                            'index': i,
                            'size_gb': size_gb,
                            'adapter': adapter
                        })
                
                # Display network info
                if vm_info['nics']:
                    print(colored("\n🌐 Source Network Configuration:", Colors.BOLD))
                    for i, nic in enumerate(vm_info['nics']):
                        source_mac = nic.get('mac')
                        source_ip = nic.get('ip')
                        subnet = nic.get('subnet', 'N/A')
                        print(f"   NIC {i}: {subnet}")
                        print(f"      MAC: {colored(source_mac, Colors.YELLOW)}")
                        print(f"      IP:  {colored(source_ip or 'DHCP/Unknown', Colors.YELLOW)}")
                    print(colored("\n   ⚠️  Save this info! You may need to reconfigure network.", Colors.YELLOW))
        
        # VM Name
        default_name = self._selected_vm.split(' - ')[0] if self._selected_vm and ' - ' in self._selected_vm else (self._selected_vm or "")
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
        active_images = [img for img in images if img.get('status', {}).get('progress', 0) == 100]
        
        if not active_images:
            print(colored("❌ No active images available. Import images first (Menu 3 → Option 6).", Colors.RED))
            return
        
        # Determine number of disks
        num_disks = len(source_disks) if source_disks else 1
        if not source_disks:
            num_disks_input = self.input_prompt("Number of disks [1]")
            num_disks = int(num_disks_input) if num_disks_input else 1
        
        print(f"\n💾 Configuring {num_disks} disk(s)...")
        
        # Select images for each disk
        selected_images = []
        disk_sizes = []
        
        for disk_idx in range(num_disks):
            print(f"\n--- Disk {disk_idx} ---")
            print("Available images:")
            for i, img in enumerate(active_images, 1):
                name = img.get('metadata', {}).get('name', 'N/A')
                ns = img.get('metadata', {}).get('namespace', 'N/A')
                size = img.get('status', {}).get('size', 0)
                print(f"  {i}. {name} ({ns}) - {format_size(size)}")
            
            choice = self.input_prompt(f"Image number for disk {disk_idx}")
            if not choice:
                print(colored("Cancelled", Colors.YELLOW))
                return
            try:
                idx = int(choice) - 1
                selected_image = active_images[idx]
                image_name = selected_image.get('metadata', {}).get('name')
                image_ns = selected_image.get('metadata', {}).get('namespace')
                selected_images.append({'name': image_name, 'namespace': image_ns})
            except:
                print(colored("Invalid choice", Colors.RED))
                return
            
            # Disk size
            default_size = source_disks[disk_idx]['size_gb'] if disk_idx < len(source_disks) else 50
            size_input = self.input_prompt(f"Disk {disk_idx} size in GB [{default_size}]")
            disk_size = int(size_input) if size_input else default_size
            disk_sizes.append(disk_size)
        
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
        
        # MAC address option
        use_source_mac = False
        custom_mac = None
        if source_mac:
            print(f"\n   Source MAC: {colored(source_mac, Colors.YELLOW)}")
            keep_mac = self.input_prompt("Keep source MAC address? (y/n) [y]")
            if keep_mac.lower() != 'n':
                use_source_mac = True
                custom_mac = source_mac
                print(colored(f"   ✅ Will use MAC: {source_mac}", Colors.GREEN))
        
        if not use_source_mac:
            manual_mac = self.input_prompt("Enter custom MAC (or Enter for auto)")
            if manual_mac:
                custom_mac = manual_mac
        
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
        
        cpu = self.input_prompt(f"CPU cores [{default_cpu}]")
        cpu = int(cpu) if cpu else default_cpu
        
        ram = self.input_prompt(f"RAM in GB [{default_ram}]")
        ram = int(ram) if ram else default_ram
        
        boot = self.input_prompt(f"Boot type (BIOS/UEFI) [{default_boot}]")
        boot = boot.upper() if boot else default_boot
        if boot not in ('BIOS', 'UEFI'):
            boot = default_boot
        
        if boot == "UEFI":
            print(colored("   ⚠️  UEFI boot selected - make sure source VM was UEFI!", Colors.YELLOW))
        
        # Disk bus selection
        print(colored("\n💾 Disk Bus Selection:", Colors.BOLD))
        print("   - sata   : Most compatible, works without extra drivers (recommended for migration)")
        print("   - virtio : Best performance, requires virtio drivers installed in guest")
        print("   - scsi   : Uses virtio-scsi, also requires virtio drivers")
        
        default_bus = "sata"
        disk_bus = self.input_prompt(f"Disk bus (sata/virtio/scsi) [{default_bus}]")
        disk_bus = disk_bus.lower() if disk_bus else default_bus
        if disk_bus not in ('sata', 'virtio', 'scsi'):
            disk_bus = default_bus
        
        if disk_bus == "sata":
            print(colored("   ℹ️  Using SATA for compatibility.", Colors.CYAN))
        
        # Summary
        print(colored(f"\n📋 VM Configuration:", Colors.BOLD))
        print(f"   Name: {vm_name}")
        print(f"   Namespace: {namespace}")
        print(f"   Disks: {num_disks}")
        for i, (img, size) in enumerate(zip(selected_images, disk_sizes)):
            print(f"      Disk {i}: {img['name']} ({img['namespace']}) - {size} GB")
        print(f"   Disk bus: {disk_bus}")
        print(f"   Network: {network_name}")
        if custom_mac:
            print(f"   MAC: {custom_mac}")
        else:
            print(f"   MAC: auto-generated")
        if source_ip:
            print(f"   Source IP (for reference): {source_ip}")
        print(f"   Storage: {storage_class}")
        print(f"   CPU: {cpu} cores")
        print(f"   RAM: {ram} GB")
        print(f"   Boot: {boot}")
        
        confirm = self.input_prompt("\nCreate VM? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        # Build disks and volumes arrays
        disks_spec = []
        volumes_spec = []
        data_volume_templates = []
        
        for i, (img, size) in enumerate(zip(selected_images, disk_sizes)):
            disk_name = f"disk-{i}"
            
            # Disk spec
            disk_spec = {
                "name": disk_name,
                "disk": {
                    "bus": disk_bus
                }
            }
            if i == 0:
                disk_spec["bootOrder"] = 1
            disks_spec.append(disk_spec)
            
            # Volume spec
            volumes_spec.append({
                "name": disk_name,
                "dataVolume": {
                    "name": f"{vm_name}-{disk_name}"
                }
            })
            
            # DataVolumeTemplate with image reference
            data_volume_templates.append({
                "metadata": {
                    "name": f"{vm_name}-{disk_name}",
                    "annotations": {
                        "harvesterhci.io/imageId": f"{img['namespace']}/{img['name']}"
                    }
                },
                "spec": {
                    "pvc": {
                        "accessModes": ["ReadWriteMany"],
                        "resources": {
                            "requests": {
                                "storage": f"{size}Gi"
                            }
                        },
                        "storageClassName": storage_class
                    },
                    "source": {
                        "blank": {}
                    }
                }
            })
        
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
                                    "disks": disks_spec,
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
                            "volumes": volumes_spec
                        }
                    },
                    "dataVolumeTemplates": data_volume_templates
                }
            }
            
            # Add UEFI firmware if needed
            if boot == "UEFI":
                manifest['spec']['template']['spec']['domain']['firmware'] = {
                    "bootloader": {
                        "efi": {
                            "secureBoot": False,
                            "persistent": False
                        }
                    }
                }
                manifest['spec']['template']['spec']['domain']['machine'] = {
                    "type": "q35"
                }
            
            # Add custom MAC address if specified
            if custom_mac:
                manifest['spec']['template']['spec']['domain']['devices']['interfaces'][0]['macAddress'] = custom_mac
            
            result = self.harvester.create_vm(manifest)
            print(colored(f"✅ VM created: {vm_name} in {namespace}", Colors.GREEN))
            print("   Start it from Harvester UI or wait for disk provisioning")
            
            # Info about dissociate feature
            print(colored("\n💡 TIP: After VM is running, use 'Dissociate VM from image' (Menu Harvester → Option 5)", Colors.CYAN))
            print(colored("   to clone volumes and remove image dependency for easier cleanup.", Colors.CYAN))
            
            # Remind about Nutanix image cleanup
            print(colored("\n💡 Don't forget to delete the Nutanix export images (Menu Nutanix → Delete image)", Colors.YELLOW))
            
            # Remind about virtio drivers if using SATA
            if disk_bus == "sata":
                print(colored("\n📝 POST-MIGRATION STEPS:", Colors.BOLD))
                print(colored("   1. Boot VM and verify it works", Colors.CYAN))
                print(colored("   2. For Windows: Install virtio drivers from https://fedorapeople.org/groups/virt/virtio-win/", Colors.CYAN))
                print(colored("   3. Shutdown VM, edit config: change disk bus from 'sata' to 'virtio'", Colors.CYAN))
                print(colored("   4. Start VM - now with better disk performance!", Colors.CYAN))
            
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
                ("4", "Power ON VM"),
                ("5", "Power OFF VM"),
                ("6", "List images"),
                ("7", "Delete image (cleanup)"),
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
                self.power_on_nutanix_vm()
                self.pause()
            elif choice == "5":
                self.power_off_nutanix_vm()
                self.pause()
            elif choice == "6":
                self.list_nutanix_images()
                self.pause()
            elif choice == "7":
                self.delete_nutanix_image()
                self.pause()
            elif choice == "0":
                break
    
    def menu_harvester(self):
        while True:
            self.print_header()
            self.print_menu("HARVESTER", [
                ("1", "List VMs"),
                ("2", "Start VM"),
                ("3", "Stop VM"),
                ("4", "Delete VM"),
                ("5", "Dissociate VM from image"),
                ("6", "List images"),
                ("7", "Delete image"),
                ("8", "List volumes"),
                ("9", "Delete volume"),
                ("10", "List networks"),
                ("11", "List storage classes"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.list_harvester_vms()
                self.pause()
            elif choice == "2":
                self.power_on_harvester_vm()
                self.pause()
            elif choice == "3":
                self.power_off_harvester_vm()
                self.pause()
            elif choice == "4":
                self.delete_harvester_vm()
                self.pause()
            elif choice == "5":
                self.dissociate_vm_from_image()
                self.pause()
            elif choice == "6":
                self.list_harvester_images()
                self.pause()
            elif choice == "7":
                self.delete_harvester_image()
                self.pause()
            elif choice == "8":
                self.list_harvester_volumes()
                self.pause()
            elif choice == "9":
                self.delete_harvester_volume()
                self.pause()
            elif choice == "10":
                self.list_harvester_networks()
                self.pause()
            elif choice == "11":
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
