#!/usr/bin/env python3
"""
Nutanix to Harvester VM Migration Tool
======================================
Interactive menu to migrate VMs from Nutanix to Harvester
"""

import os
import sys
import re
import time
import yaml
import argparse
import json
import requests
from datetime import datetime

# Add lib to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import Colors, colored, format_size, format_timestamp
from lib import NutanixClient, HarvesterClient, MigrationActions
from lib.vault import Vault, VaultError, get_kerberos_auth, kinit
from lib.windows import (
    WinRMClient, WindowsPreCheck, WindowsPostConfig, VMConfig, ListeningService,
    download_virtio_tools, check_winrm_available, WINRM_AVAILABLE
)


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def detect_boot_type_from_disk(disk_path: str) -> str:
    """
    Detect boot type (UEFI or BIOS) by analyzing disk partition table.
    GPT = UEFI, MBR = BIOS
    
    Args:
        disk_path: Path to QCOW2 or RAW disk image
        
    Returns:
        'UEFI' or 'BIOS'
    """
    import subprocess
    
    if not os.path.exists(disk_path):
        print(colored(f"   ⚠️  Disk not found: {disk_path}", Colors.YELLOW))
        return 'BIOS'  # Default fallback
    
    try:
        # For QCOW2, we need to use qemu-nbd or qemu-img to inspect
        if disk_path.endswith('.qcow2'):
            # Use qemu-img map to check if we can read it
            result = subprocess.run(
                ['qemu-img', 'info', '--output=json', disk_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                print(colored(f"   ⚠️  Cannot read QCOW2: {result.stderr}", Colors.YELLOW))
                return 'BIOS'
            
            # Try to mount via nbd and check partition type
            # First, find an available nbd device
            nbd_device = None
            for i in range(16):
                dev = f'/dev/nbd{i}'
                if os.path.exists(dev):
                    # Check if it's in use
                    check = subprocess.run(['lsblk', dev], capture_output=True, text=True)
                    if 'disk' not in check.stdout or check.returncode != 0:
                        nbd_device = dev
                        break
            
            if not nbd_device:
                # Try to load nbd module and use nbd0
                subprocess.run(['modprobe', 'nbd', 'max_part=16'], capture_output=True)
                nbd_device = '/dev/nbd0'
            
            # Connect qcow2 to nbd
            disconnect_cmd = ['qemu-nbd', '-d', nbd_device]
            subprocess.run(disconnect_cmd, capture_output=True)  # Disconnect if already connected
            
            connect_cmd = ['qemu-nbd', '-c', nbd_device, disk_path]
            result = subprocess.run(connect_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                print(colored(f"   ⚠️  Cannot connect NBD: {result.stderr}", Colors.YELLOW))
                return 'BIOS'
            
            try:
                # Give it a moment to settle
                time.sleep(1)
                
                # Check partition table type with fdisk
                fdisk_result = subprocess.run(
                    ['fdisk', '-l', nbd_device],
                    capture_output=True, text=True, timeout=30
                )
                
                if 'Disklabel type: gpt' in fdisk_result.stdout.lower() or 'disklabel type: gpt' in fdisk_result.stdout.lower():
                    return 'UEFI'
                elif 'Disklabel type: dos' in fdisk_result.stdout.lower() or 'disklabel type: dos' in fdisk_result.stdout.lower():
                    return 'BIOS'
                elif 'gpt' in fdisk_result.stdout.lower():
                    return 'UEFI'
                else:
                    return 'BIOS'
            finally:
                # Always disconnect
                subprocess.run(disconnect_cmd, capture_output=True)
        
        else:
            # For RAW images, we can use fdisk directly
            result = subprocess.run(
                ['fdisk', '-l', disk_path],
                capture_output=True, text=True, timeout=30
            )
            
            if 'gpt' in result.stdout.lower():
                return 'UEFI'
            else:
                return 'BIOS'
                
    except subprocess.TimeoutExpired:
        print(colored("   ⚠️  Timeout detecting boot type", Colors.YELLOW))
        return 'BIOS'
    except Exception as e:
        print(colored(f"   ⚠️  Error detecting boot type: {e}", Colors.YELLOW))
        return 'BIOS'


class MigrationTool:
    """Migration tool with interactive menu."""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        self.nutanix = None
        self.harvester = None
        self.actions = None
        self._selected_vm = None
        
        # Initialize vault for credentials
        windows_config = self.config.get('windows', {})
        vault_backend = windows_config.get('vault_backend', 'prompt')
        vault_path = windows_config.get('vault_path', 'migration/windows')
        try:
            self.vault = Vault(backend=vault_backend, vault_path=vault_path)
        except VaultError:
            # Fallback to prompt if vault not configured
            self.vault = Vault(backend='prompt', vault_path=vault_path)
    
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
    
    # === Migration Tracker Methods ===
    
    MIGRATION_STEPS = ['precheck', 'export', 'import_disks', 'create_vm', 'postmig']
    STEP_LABELS = {
        'precheck': 'Pre-check (collect config)',
        'export': 'Export/Download disks',
        'import_disks': 'Import disks to Harvester',
        'create_vm': 'Create VM in Harvester',
        'postmig': 'Post-migration config'
    }
    
    def get_tracker_path(self) -> str:
        """Get path to migration tracker JSON file."""
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        return os.path.join(staging_dir, 'migrations', 'migration-tracker.json')
    
    def load_tracker(self) -> dict:
        """Load migration tracker from JSON file."""
        tracker_path = self.get_tracker_path()
        if os.path.exists(tracker_path):
            try:
                with open(tracker_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(colored(f"⚠️  Error loading tracker: {e}", Colors.YELLOW))
        return {"migrations": {}}
    
    def save_tracker(self, tracker: dict):
        """Save migration tracker to JSON file."""
        tracker_path = self.get_tracker_path()
        os.makedirs(os.path.dirname(tracker_path), exist_ok=True)
        with open(tracker_path, 'w') as f:
            json.dump(tracker, f, indent=2)
    
    def add_vm_to_tracker(self, vm_name: str, os_type: str = "windows") -> bool:
        """Add a VM to the migration tracker."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key in tracker['migrations']:
            print(colored(f"⚠️  VM '{vm_name}' already in tracker", Colors.YELLOW))
            return False
        
        tracker['migrations'][vm_key] = {
            "display_name": vm_name,
            "added_at": datetime.utcnow().isoformat() + "Z",
            "os_type": os_type,
            "source": "nutanix",
            "steps": {step: {"done": False, "date": None} for step in self.MIGRATION_STEPS}
        }
        
        self.save_tracker(tracker)
        return True
    
    def remove_vm_from_tracker(self, vm_name: str) -> bool:
        """Remove a VM from the migration tracker."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            print(colored(f"❌ VM '{vm_name}' not in tracker", Colors.RED))
            return False
        
        del tracker['migrations'][vm_key]
        self.save_tracker(tracker)
        return True
    
    def update_step(self, vm_name: str, step_name: str) -> bool:
        """Mark a step as completed for a VM."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            # Auto-add VM if not in tracker
            self.add_vm_to_tracker(vm_name)
            tracker = self.load_tracker()
        
        if step_name not in self.MIGRATION_STEPS:
            print(colored(f"❌ Invalid step: {step_name}", Colors.RED))
            return False
        
        tracker['migrations'][vm_key]['steps'][step_name] = {
            "done": True,
            "date": datetime.utcnow().isoformat() + "Z"
        }
        
        self.save_tracker(tracker)
        return True
    
    def reset_step(self, vm_name: str, step_name: str) -> bool:
        """Reset a step for a VM."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            print(colored(f"❌ VM '{vm_name}' not in tracker", Colors.RED))
            return False
        
        if step_name == 'all':
            for step in self.MIGRATION_STEPS:
                tracker['migrations'][vm_key]['steps'][step] = {"done": False, "date": None}
        else:
            if step_name not in self.MIGRATION_STEPS:
                print(colored(f"❌ Invalid step: {step_name}", Colors.RED))
                return False
            tracker['migrations'][vm_key]['steps'][step_name] = {"done": False, "date": None}
        
        self.save_tracker(tracker)
        return True
    
    def is_step_done(self, vm_name: str, step_name: str) -> bool:
        """Check if a step is already completed."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            return False
        
        return tracker['migrations'][vm_key]['steps'].get(step_name, {}).get('done', False)
    
    def get_step_date(self, vm_name: str, step_name: str) -> str:
        """Get the completion date of a step."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            return None
        
        return tracker['migrations'][vm_key]['steps'].get(step_name, {}).get('date')
    
    def get_next_step(self, vm_name: str) -> str:
        """Get the next step to be done for a VM."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            return self.MIGRATION_STEPS[0]
        
        steps = tracker['migrations'][vm_key]['steps']
        for step in self.MIGRATION_STEPS:
            if not steps.get(step, {}).get('done', False):
                return step
        
        return "completed"
    
    def get_vm_progress(self, vm_name: str) -> str:
        """Get progress bar for a VM like [✓✓✓○○○]."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            return "[○○○○○○]"
        
        steps = tracker['migrations'][vm_key]['steps']
        progress = ""
        for step in self.MIGRATION_STEPS:
            if steps.get(step, {}).get('done', False):
                progress += "✓"
            else:
                progress += "○"
        
        return f"[{progress}]"
    
    def get_vm_progress_count(self, vm_name: str) -> tuple:
        """Get progress count for a VM (done, total)."""
        tracker = self.load_tracker()
        vm_key = vm_name.lower()
        
        if vm_key not in tracker['migrations']:
            return (0, len(self.MIGRATION_STEPS))
        
        steps = tracker['migrations'][vm_key]['steps']
        done = sum(1 for step in self.MIGRATION_STEPS if steps.get(step, {}).get('done', False))
        return (done, len(self.MIGRATION_STEPS))
    
    def check_step_and_confirm(self, vm_name: str, step_name: str) -> bool:
        """Check if step is done and ask for confirmation to re-run."""
        if self.is_step_done(vm_name, step_name):
            date = self.get_step_date(vm_name, step_name)
            date_str = date[:10] if date else "unknown"
            print(colored(f"\n⚠️  Step '{self.STEP_LABELS.get(step_name, step_name)}' already completed ({date_str})", Colors.YELLOW))
            confirm = self.input_prompt("   Re-run this step? (y/n) [n]") or "n"
            if confirm.lower() != 'y':
                return False
        return True

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
        """Select a VM from the migration tracker for the workflow."""
        print(colored("\n📋 Select VM for Migration", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        tracker = self.load_tracker()
        migrations = tracker.get('migrations', {})
        
        if not migrations:
            print(colored("   No VMs in migration tracker.", Colors.YELLOW))
            print(colored("   Use 'Migration Tracker → Add VM' first.", Colors.YELLOW))
            return
        
        # Sort VMs and display
        vm_list = sorted(migrations.keys())
        
        print("\n   VMs in migration tracker:")
        for i, vm_key in enumerate(vm_list, 1):
            vm_data = migrations[vm_key]
            display_name = vm_data.get('display_name', vm_key)
            os_type = vm_data.get('os_type', '?')
            progress = self.get_vm_progress(vm_key)
            next_step = self.get_next_step(vm_key)
            next_label = self.STEP_LABELS.get(next_step, next_step)[:20] if next_step != "completed" else "✅ Done"
            
            # Mark current selection
            current = colored(" ← selected", Colors.YELLOW) if self._selected_vm and self._selected_vm.lower() == vm_key else ""
            
            print(f"   {i:3}. {display_name:<20} {progress} → {next_label}{current}")
        
        print(f"\n   0. Cancel")
        
        choice = self.input_prompt("\nSelect VM number")
        if not choice or choice == "0":
            return
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(vm_list):
                vm_key = vm_list[idx]
                display_name = migrations[vm_key].get('display_name', vm_key)
                self._selected_vm = display_name
                next_step = self.get_next_step(vm_key)
                next_label = self.STEP_LABELS.get(next_step, next_step) if next_step != "completed" else "All done!"
                print(colored(f"\n✅ VM '{display_name}' selected", Colors.GREEN))
                print(colored(f"   Next step: {next_label}", Colors.CYAN))
            else:
                print(colored("❌ Invalid selection", Colors.RED))
        except ValueError:
            print(colored("❌ Invalid input", Colors.RED))
    
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
        
        print(colored(f"\n{'='*80}", Colors.BLUE))
        print(colored("HARVESTER NETWORKS", Colors.BOLD))
        print(colored(f"{'='*80}", Colors.BLUE))
        print(f"{'Network Name':<30} {'Namespace':<20} {'Type':<12} {'VLAN':<8}")
        print(f"{'-'*80}")
        
        # Group by namespace
        by_namespace = {}
        for net in networks:
            ns = net.get('metadata', {}).get('namespace', 'N/A')
            if ns not in by_namespace:
                by_namespace[ns] = []
            by_namespace[ns].append(net)
        
        for ns in sorted(by_namespace.keys()):
            for net in sorted(by_namespace[ns], key=lambda x: x.get('metadata', {}).get('name', '')):
                name = net.get('metadata', {}).get('name', 'N/A')
                
                # Parse config to get network type and VLAN
                net_type = "unknown"
                vlan_id = "-"
                
                try:
                    config_str = net.get('spec', {}).get('config', '{}')
                    config = json.loads(config_str)
                    
                    # Determine type
                    if config.get('type') == 'bridge':
                        net_type = "bridge"
                        if 'vlan' in config:
                            vlan_id = str(config.get('vlan', '-'))
                            net_type = "vlan"
                    elif 'ipam' in config:
                        net_type = config.get('type', 'ipam')
                    else:
                        net_type = config.get('type', 'unknown')
                except:
                    pass
                
                print(f"{name:<30} {ns:<20} {net_type:<12} {vlan_id:<8}")
        
        print(colored(f"{'='*80}", Colors.BLUE))
        print(f"Total: {len(networks)} network(s) in {len(by_namespace)} namespace(s)")
    
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
        
        # Separate scratch volumes
        scratch_pvcs = [p for p in pvcs if 'scratch' in p.get('metadata', {}).get('name', '').lower() 
                        or p.get('metadata', {}).get('name', '').startswith('prime-')]
        regular_pvcs = [p for p in pvcs if p not in scratch_pvcs]
        
        print(f"\n{'='*100}")
        print(f"{'Volume Name':<50} {'Namespace':<18} {'Size':<10} {'Status':<10} {'Type'}")
        print(f"{'='*100}")
        
        for pvc in sorted(regular_pvcs, key=lambda x: x.get('metadata', {}).get('name', '').lower()):
            name = pvc.get('metadata', {}).get('name', 'N/A')[:49]
            ns = pvc.get('metadata', {}).get('namespace', 'N/A')[:17]
            size = pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
            status = pvc.get('status', {}).get('phase', 'N/A')
            
            # Check if it has image dependency
            annotations = pvc.get('metadata', {}).get('annotations', {})
            if 'harvesterhci.io/imageId' in annotations:
                vol_type = colored("image-backed", Colors.YELLOW)
            else:
                vol_type = colored("independent", Colors.GREEN)
            
            print(f"{name:<50} {ns:<18} {size:<10} {status:<10} {vol_type}")
        
        if scratch_pvcs:
            print(f"\n{colored('Scratch/Temporary volumes (CDI):', Colors.YELLOW)}")
            for pvc in scratch_pvcs:
                name = pvc.get('metadata', {}).get('name', 'N/A')[:49]
                ns = pvc.get('metadata', {}).get('namespace', 'N/A')[:17]
                print(f"  {name} ({ns})")
        
        print(f"{'='*100}")
        print(f"Total: {len(regular_pvcs)} volumes" + (f", {len(scratch_pvcs)} scratch" if scratch_pvcs else ""))
    
    def delete_harvester_volume(self):
        """Delete a Harvester volume (PVC) or DataVolume."""
        if not self.harvester and not self.connect_harvester():
            return
        
        pvcs = self.harvester.list_all_pvcs()
        
        if not pvcs:
            print(colored("❌ No volumes found", Colors.YELLOW))
            return
        
        # Separate scratch volumes and regular volumes
        scratch_pvcs = [p for p in pvcs if 'scratch' in p.get('metadata', {}).get('name', '').lower() 
                        or p.get('metadata', {}).get('name', '').startswith('prime-')]
        regular_pvcs = [p for p in pvcs if p not in scratch_pvcs]
        
        print(colored("\n📦 Volume Management", Colors.BOLD))
        print(f"   Regular volumes: {len(regular_pvcs)}")
        print(f"   Scratch/temp volumes (prime-*): {len(scratch_pvcs)}")
        
        if scratch_pvcs:
            print(colored(f"\n⚠️  Found {len(scratch_pvcs)} scratch volumes (CDI temporary)", Colors.YELLOW))
            clean_scratch = self.input_prompt("Clean up scratch volumes? (y/n) [n]")
            if clean_scratch and clean_scratch.lower() == 'y':
                for pvc in scratch_pvcs:
                    name = pvc.get('metadata', {}).get('name')
                    ns = pvc.get('metadata', {}).get('namespace')
                    try:
                        self.harvester.delete_pvc(name, ns)
                        print(colored(f"   ✅ Deleted scratch: {name}", Colors.GREEN))
                    except Exception as e:
                        print(colored(f"   ⚠️  Could not delete {name}: {e}", Colors.YELLOW))
                return
        
        print("\nAvailable volumes (Enter to cancel):")
        sorted_pvcs = sorted(regular_pvcs, key=lambda x: x.get('metadata', {}).get('name', '').lower())
        for i, pvc in enumerate(sorted_pvcs, 1):
            name = pvc.get('metadata', {}).get('name', 'N/A')
            ns = pvc.get('metadata', {}).get('namespace', 'N/A')
            size = pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
            phase = pvc.get('status', {}).get('phase', '')
            status = colored("Bound", Colors.GREEN) if phase == 'Bound' else colored(phase, Colors.YELLOW)
            print(f"  {i}. {name} ({ns}) - {size} - {status}")
        
        choice = self.input_prompt("Volume number to delete (or 'all-scratch' to clean scratch)")
        if not choice:
            return
        
        if choice.lower() == 'all-scratch':
            for pvc in scratch_pvcs:
                name = pvc.get('metadata', {}).get('name')
                ns = pvc.get('metadata', {}).get('namespace')
                try:
                    self.harvester.delete_pvc(name, ns)
                    print(colored(f"   ✅ Deleted: {name}", Colors.GREEN))
                except:
                    pass
            return
        
        try:
            idx = int(choice) - 1
            selected = sorted_pvcs[idx]
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        vol_name = selected.get('metadata', {}).get('name')
        vol_ns = selected.get('metadata', {}).get('namespace')
        
        # Check if there's also a DataVolume with this name
        try:
            dv = self.harvester.get_datavolume(vol_name, vol_ns, silent=True)
            has_dv = True
        except:
            has_dv = False
        
        if has_dv:
            print(colored(f"   Note: This volume has an associated DataVolume", Colors.CYAN))
            delete_dv = self.input_prompt(f"Delete DataVolume '{vol_name}' (will also delete PVC)? (yes to confirm)")
            if delete_dv.lower() == 'yes':
                try:
                    self.harvester.delete_datavolume(vol_name, vol_ns)
                    print(colored(f"✅ Deleted DataVolume: {vol_name}", Colors.GREEN))
                except Exception as e:
                    print(colored(f"❌ Error deleting DataVolume: {e}", Colors.RED))
            return
        
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
    
    def _convert_single_file(self, raw_path: str):
        """Convert a single RAW file to QCOW2."""
        self.init_actions()
        
        if not os.path.exists(raw_path):
            print(colored(f"   ❌ File not found: {raw_path}", Colors.RED))
            return False
        
        filename = os.path.basename(raw_path)
        size = os.path.getsize(raw_path)
        print(f"\n🔄 Converting: {filename} ({format_size(size)})")
        
        def progress(pct):
            print(f"\r   Progress: {pct:.1f}%", end='', flush=True)
        
        result = self.actions.convert_raw_to_qcow2(raw_path, compress=True, progress_callback=progress)
        print()  # New line after progress
        
        if result['success']:
            print(colored(f"   ✅ Done: {format_size(result['size_before'])} → {format_size(result['size_after'])} ({result['reduction_pct']:.1f}% reduction)", Colors.GREEN))
            
            # Auto-delete RAW file
            if self.actions.delete_file(raw_path):
                print(colored("   ✅ RAW file deleted", Colors.GREEN))
            return True
        else:
            print(colored(f"   ❌ Error: {result['error']}", Colors.RED))
            return False
    
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
                
                # Auto-delete RAW file
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
        """Export VM disks from Nutanix to staging."""
        if not self._selected_vm:
            print(colored("❌ No VM selected. Use 'Select VM' first.", Colors.RED))
            return
        
        # Check if step already done
        if not self.check_step_and_confirm(self._selected_vm, 'export'):
            return
        
        if not self.nutanix:
            self.connect_nutanix()
            if not self.nutanix:
                return
        
        print(colored(f"\n📤 Export VM: {self._selected_vm}", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        # Ask for transfer method
        print(colored("\nTransfer method:", Colors.BOLD))
        print("  1. NFS direct (FAST ~1000+ MB/s) - requires NFS whitelist")
        print("  2. API download (slower ~100 MB/s) - always works")
        method = self.input_prompt("Choice [1]")
        
        if method == "2":
            self._export_vm_api()
        else:
            self._export_vm_nfs()
    
    def _export_vm_nfs(self):
        """Export VM disks via NFS (fast method)."""
        print(colored(f"\n🚀 NFS Export: {self._selected_vm}", Colors.BOLD))
        
        # Get VM details for power state check
        vm = self.nutanix.get_vm_by_name(self._selected_vm)
        if not vm:
            print(colored(f"❌ VM not found: {self._selected_vm}", Colors.RED))
            return
        
        # Check power state
        power_state = vm.get('status', {}).get('resources', {}).get('power_state', 'UNKNOWN')
        if power_state != 'OFF':
            print(colored(f"⚠️  VM is {power_state}. It's recommended to power off before export.", Colors.YELLOW))
            proceed = self.input_prompt("Continue anyway? (y/n)")
            if proceed.lower() != 'y':
                return
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        vm_name_clean = self._selected_vm.lower().replace(' ', '-').replace('/', '-')
        
        # Create VM-specific migrations folder
        vm_migration_dir = os.path.join(staging_dir, 'migrations', vm_name_clean)
        os.makedirs(vm_migration_dir, exist_ok=True)
        print(colored(f"   📁 Migration folder: {vm_migration_dir}", Colors.CYAN))
        
        # Get vdisks via API v2
        print(colored("\n📡 Getting vdisk info via API v2...", Colors.CYAN))
        try:
            vdisks = self.nutanix.get_vm_vdisks_v2(self._selected_vm)
        except Exception as e:
            print(colored(f"❌ Failed to get vdisks: {e}", Colors.RED))
            return
        
        if not vdisks:
            print(colored("❌ No vdisks found", Colors.RED))
            return
        
        print(colored(f"\n💾 Found {len(vdisks)} disk(s):", Colors.BOLD))
        for i, vdisk in enumerate(vdisks):
            size_gb = vdisk['size_bytes'] // (1024**3)
            print(f"   Disk {i} ({vdisk.get('disk_address', 'N/A')}): {size_gb} GB")
            print(f"      UUID: {vdisk['uuid']}")
            print(f"      Container: {vdisk['container']}")
        
        # Mount NFS
        container = vdisks[0].get('container', 'container01')
        print(colored(f"\n📁 Mounting NFS container: {container}", Colors.CYAN))
        try:
            mount_path = self.nutanix.mount_nfs_container(container)
            print(colored(f"   ✅ Mounted at {mount_path}", Colors.GREEN))
        except Exception as e:
            print(colored(f"❌ Failed to mount NFS: {e}", Colors.RED))
            print(colored("   Make sure the server IP is in the container's filesystem whitelist", Colors.YELLOW))
            return
        
        # Copy disks
        print(colored(f"\n🚀 Starting NFS copy to {vm_migration_dir}", Colors.CYAN))
        print(colored("   This should be MUCH faster than API download!\n", Colors.CYAN))
        
        downloaded_files = []
        
        for i, vdisk in enumerate(vdisks):
            dest_file = os.path.join(vm_migration_dir, f"{vm_name_clean}-disk{i}.raw")
            size_gb = vdisk['size_bytes'] // (1024**3)
            
            print(colored(f"   📀 Disk {i} ({size_gb} GB):", Colors.BOLD))
            
            if os.path.exists(dest_file):
                existing_size = os.path.getsize(dest_file)
                if existing_size == vdisk['size_bytes']:
                    print(f"      File exists with correct size")
                    skip = self.input_prompt("      Skip? (y/n) [y]")
                    if skip.lower() != 'n':
                        downloaded_files.append(dest_file)
                        continue
                else:
                    overwrite = self.input_prompt(f"      File exists ({existing_size // (1024**3)} GB). Overwrite? (y/n)")
                    if overwrite.lower() != 'y':
                        downloaded_files.append(dest_file)
                        continue
            
            start_time = time.time()
            last_print = start_time
            
            def copy_progress(copied, total, speed):
                nonlocal last_print
                now = time.time()
                if now - last_print >= 2.0:  # Update every 2 seconds
                    pct = (copied / total * 100) if total > 0 else 0
                    copied_gb = copied / (1024**3)
                    total_gb = total / (1024**3)
                    print(f"\r      Progress: {pct:.1f}% ({copied_gb:.1f}/{total_gb:.1f} GB) - {speed:.0f} MB/s   ", end='', flush=True)
                    last_print = now
            
            try:
                self.nutanix.copy_vdisk_nfs(
                    vdisk['uuid'],
                    container,
                    dest_file,
                    progress_callback=copy_progress
                )
                elapsed = time.time() - start_time
                avg_speed = (vdisk['size_bytes'] / elapsed) / (1024**2)
                print(f"\n      ✅ Done in {elapsed:.0f}s (avg {avg_speed:.0f} MB/s)")
                downloaded_files.append(dest_file)
            except Exception as e:
                print(f"\n      ❌ Failed: {e}")
        
        # Summary
        print(colored(f"\n✅ Export complete!", Colors.GREEN))
        print(f"   Files in staging: {len(downloaded_files)}")
        for f in downloaded_files:
            size = os.path.getsize(f) if os.path.exists(f) else 0
            print(f"      {f} ({size // (1024**3)} GB)")
        
        # Auto-convert to QCOW2
        if downloaded_files:
            print(colored("\n🔄 Converting to QCOW2...", Colors.CYAN))
            for raw_file in downloaded_files:
                self._convert_single_file(raw_file)
        
        # Update tracker
        self.update_step(self._selected_vm, 'export')
        print(colored(f"   ✅ Step 'export' marked complete in tracker", Colors.GREEN))
    
    def _export_vm_api(self):
        """Export VM disks via API (original method)."""
        print(colored(f"\n📤 API Export: {self._selected_vm}", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        # Get VM details
        vm = self.nutanix.get_vm_by_name(self._selected_vm)
        if not vm:
            print(colored(f"❌ VM not found: {self._selected_vm}", Colors.RED))
            return
        
        vm_info = NutanixClient.parse_vm_info(vm)
        vm_uuid = vm.get('metadata', {}).get('uuid')
        
        # Check power state
        power_state = vm.get('status', {}).get('resources', {}).get('power_state', 'UNKNOWN')
        if power_state != 'OFF':
            print(colored(f"⚠️  VM is {power_state}. It's recommended to power off before export.", Colors.YELLOW))
            proceed = self.input_prompt("Continue anyway? (y/n)")
            if proceed.lower() != 'y':
                return
        
        # Get disk list
        disks = vm_info.get('disks', [])
        if not disks:
            print(colored("❌ No disks found on VM", Colors.RED))
            return
        
        print(colored(f"\n💾 Found {len(disks)} disk(s):", Colors.BOLD))
        for i, disk in enumerate(disks):
            size_gb = disk['size_bytes'] // (1024**3)
            print(f"   Disk {i}: {disk.get('adapter', 'N/A')}.{disk.get('index', i)} - {size_gb} GB")
            print(f"      UUID: {disk.get('uuid', 'N/A')}")
        
        # Select disks to export
        export_all = self.input_prompt(f"\nExport all {len(disks)} disk(s)? (y/n) [y]")
        if export_all.lower() == 'n':
            disk_nums = self.input_prompt("Enter disk numbers to export (comma-separated, e.g., 0,1)")
            try:
                selected_indices = [int(x.strip()) for x in disk_nums.split(',')]
                disks_to_export = [disks[i] for i in selected_indices]
            except:
                print(colored("Invalid input", Colors.RED))
                return
        else:
            disks_to_export = disks
        
        # Staging directory
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        vm_name_clean = self._selected_vm.lower().replace(' ', '-').replace('/', '-')
        
        # Create VM-specific migrations folder
        vm_migration_dir = os.path.join(staging_dir, 'migrations', vm_name_clean)
        os.makedirs(vm_migration_dir, exist_ok=True)
        
        print(colored(f"\n🚀 Starting export to {vm_migration_dir}", Colors.CYAN))
        print(colored("   This may take a while depending on disk size...\n", Colors.CYAN))
        
        created_images = []
        
        for i, disk in enumerate(disks_to_export):
            disk_uuid = disk.get('uuid')
            if not disk_uuid:
                print(colored(f"   ⚠️  Disk {i} has no UUID, skipping", Colors.YELLOW))
                continue
            
            image_name = f"{vm_name_clean}-disk{i}-export"
            disk_idx = disk.get('index', i)
            size_gb = disk['size_bytes'] // (1024**3)
            
            print(colored(f"   📀 Disk {i} ({size_gb} GB):", Colors.BOLD))
            
            # Check if image already exists
            existing = self.nutanix.get_image_by_name(image_name)
            if existing:
                print(f"      Image '{image_name}' already exists")
                reuse = self.input_prompt("      Use existing image? (y/n) [y]")
                if reuse.lower() != 'n':
                    image_uuid = existing.get('metadata', {}).get('uuid')
                    created_images.append({
                        'name': image_name,
                        'uuid': image_uuid,
                        'disk_index': disk_idx,
                        'size_gb': size_gb
                    })
                    continue
                else:
                    # Delete and recreate
                    print("      Deleting existing image...")
                    self.nutanix.delete_image(existing.get('metadata', {}).get('uuid'))
                    import time
                    time.sleep(5)
            
            # Create image from disk
            print(f"      Creating image from disk...")
            try:
                result = self.nutanix.create_image_from_disk(
                    image_name=image_name,
                    vmdisk_uuid=disk_uuid,
                    description=f"Migration export of {self._selected_vm} disk {disk_idx}"
                )
                image_uuid = result.get('metadata', {}).get('uuid')
                print(f"      Image UUID: {image_uuid}")
            except Exception as e:
                print(colored(f"      ❌ Failed to create image: {e}", Colors.RED))
                continue
            
            # Wait for image to be ready
            print(f"      Waiting for image to be ready...")
            
            def progress_cb(state, pct):
                print(f"\r      State: {state} ({pct}%)   ", end='', flush=True)
            
            ready = self.nutanix.wait_for_image_ready(
                image_uuid, 
                timeout=7200,  # 2 hours max
                progress_callback=progress_cb
            )
            print()  # New line after progress
            
            if not ready:
                print(colored(f"      ❌ Image creation failed or timed out", Colors.RED))
                continue
            
            print(colored(f"      ✅ Image ready", Colors.GREEN))
            created_images.append({
                'name': image_name,
                'uuid': image_uuid,
                'disk_index': disk_idx,
                'size_gb': size_gb
            })
        
        if not created_images:
            print(colored("\n❌ No images created", Colors.RED))
            return
        
        # Download images
        print(colored(f"\n📥 Downloading {len(created_images)} image(s)...", Colors.BOLD))
        
        downloaded_files = []
        
        for img in created_images:
            dest_file = os.path.join(vm_migration_dir, f"{vm_name_clean}-disk{img['disk_index']}.raw")
            print(f"\n   Downloading {img['name']} → {dest_file}")
            print(f"   Size: ~{img['size_gb']} GB")
            
            if os.path.exists(dest_file):
                overwrite = self.input_prompt(f"   File exists. Overwrite? (y/n)")
                if overwrite.lower() != 'y':
                    downloaded_files.append(dest_file)
                    continue
            
            last_print_pct = -10  # Track last printed percentage
            last_print_gb = -1   # Track last printed GB
            
            def download_progress(downloaded, total):
                nonlocal last_print_pct, last_print_gb
                dl_gb = downloaded / (1024**3)
                
                if total > 0:
                    pct = (downloaded / total) * 100
                    total_gb = total / (1024**3)
                    # Print every 5% or every 1GB
                    if pct - last_print_pct >= 5 or int(dl_gb) > last_print_gb:
                        print(f"   Progress: {pct:.1f}% ({dl_gb:.1f} / {total_gb:.1f} GB)")
                        last_print_pct = pct
                        last_print_gb = int(dl_gb)
                else:
                    # No total size - print every 1GB
                    if int(dl_gb) > last_print_gb:
                        print(f"   Downloaded: {dl_gb:.1f} GB")
                        last_print_gb = int(dl_gb)
            
            try:
                print(f"   Starting download...")
                self.nutanix.download_image(
                    img['uuid'],
                    dest_file,
                    progress_callback=download_progress
                )
                print()  # New line
                print(colored(f"   ✅ Downloaded: {dest_file}", Colors.GREEN))
                downloaded_files.append(dest_file)
            except Exception as e:
                print()
                print(colored(f"   ❌ Download failed: {e}", Colors.RED))
        
        # Summary
        print(colored(f"\n✅ Export complete!", Colors.GREEN))
        print(f"   Files in staging: {len(downloaded_files)}")
        for f in downloaded_files:
            size = os.path.getsize(f) if os.path.exists(f) else 0
            print(f"      {f} ({size // (1024**3)} GB)")
        
        # Auto-convert to QCOW2
        if downloaded_files:
            print(colored("\n🔄 Converting to QCOW2...", Colors.CYAN))
            for raw_file in downloaded_files:
                self._convert_single_file(raw_file)
        
        # Cleanup reminder
        print(colored("\n💡 TIP: After successful migration, delete the Nutanix export images:", Colors.YELLOW))
        for img in created_images:
            print(f"      - {img['name']} ({img['uuid']})")
        
        # Update tracker
        self.update_step(self._selected_vm, 'export')
        print(colored(f"   ✅ Step 'export' marked complete in tracker", Colors.GREEN))
    
    def download_vm_disks(self):
        """Download VM disks from Nutanix - wrapper that uses selected VM."""
        # This is essentially export_vm but explicitly named for the workflow
        self.export_vm()
    
    def create_pvcs_for_vm(self):
        """Create PVCs in Harvester for the selected VM's disks (legacy - use import_disks_to_pvcs instead)."""
        print(colored("\n📦 Create PVCs for VM Disks", Colors.BOLD))
        print(colored("=" * 60, Colors.BLUE))
        
        if not self.harvester and not self.connect_harvester():
            return
        
        vm_name = self._selected_vm.lower().replace(' ', '-')
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        vm_dir = os.path.join(staging_dir, 'migrations', vm_name)
        
        # Find QCOW2 files
        qcow2_files = []
        if os.path.exists(vm_dir):
            for f in sorted(os.listdir(vm_dir)):
                if f.endswith('.qcow2'):
                    qcow2_files.append(os.path.join(vm_dir, f))
        
        if not qcow2_files:
            print(colored(f"❌ No QCOW2 files found in {vm_dir}", Colors.RED))
            print(colored("   Run 'Download VM disks' first (option 3)", Colors.YELLOW))
            return
        
        print(f"\n📁 Found {len(qcow2_files)} disk(s) for {self._selected_vm}:")
        
        import subprocess
        import math
        
        disk_info = []
        for i, qcow2_path in enumerate(qcow2_files):
            try:
                result = subprocess.run(
                    ['qemu-img', 'info', '--output=json', qcow2_path],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    virtual_size = info.get('virtual-size', 0)
                    size_gi = math.ceil(virtual_size / (1024**3))
                    disk_info.append({
                        'path': qcow2_path,
                        'name': f"{vm_name}-disk{i}",
                        'size_gi': size_gi
                    })
                    print(f"   [{i}] {os.path.basename(qcow2_path)} → PVC: {vm_name}-disk{i} ({size_gi} GiB)")
            except Exception as e:
                print(colored(f"   [{i}] Error reading {qcow2_path}: {e}", Colors.RED))
        
        if not disk_info:
            return
        
        # Get namespace - numbered list selection
        namespaces = self.harvester.list_namespaces()
        excluded_ns = ['kube-system', 'kube-public', 'kube-node-lease', 
                       'cattle-system', 'cattle-fleet-system', 'cattle-impersonation-system',
                       'harvester-system', 'longhorn-system', 'fleet-local']
        ns_names = sorted([ns.get('metadata', {}).get('name', '') for ns in namespaces 
                   if ns.get('metadata', {}).get('name', '') not in excluded_ns
                   and not ns.get('metadata', {}).get('name', '').startswith('kube-')
                   and not ns.get('metadata', {}).get('name', '').startswith('cattle-')])
        
        print(colored("\n   Available namespaces:", Colors.BOLD))
        default_ns_idx = 0
        for i, ns in enumerate(ns_names, 1):
            if ns == "default":
                default_ns_idx = i
                print(f"     {i}. {ns}                    ← default")
            else:
                print(f"     {i}. {ns}")
        
        ns_choice = self.input_prompt(f"   Select namespace [{default_ns_idx}]") or str(default_ns_idx)
        try:
            ns_idx = int(ns_choice) - 1
            if 0 <= ns_idx < len(ns_names):
                namespace = ns_names[ns_idx]
            else:
                namespace = "default"
        except ValueError:
            namespace = "default"
        print(colored(f"   → Using: {namespace}", Colors.CYAN))
        
        # Get storage classes - numbered list
        scs = self.harvester.list_storage_classes()
        sc_names = sorted([sc.get('metadata', {}).get('name', '') for sc in scs])
        
        print(colored("\n   Available storage classes:", Colors.BOLD))
        default_sc_idx = 0
        for i, sc in enumerate(sc_names, 1):
            if sc == "harvester-longhorn-dual-node":
                default_sc_idx = i
                print(f"     {i}. {sc}    ← default")
            else:
                print(f"     {i}. {sc}")
        
        if default_sc_idx == 0 and sc_names:
            default_sc_idx = 1
        
        # Storage class selection per disk
        print()
        for disk in disk_info:
            sc_choice = self.input_prompt(f"   Storage class for {disk['name']} ({disk['size_gi']} GiB) [{default_sc_idx}]") or str(default_sc_idx)
            try:
                sc_idx = int(sc_choice) - 1
                if 0 <= sc_idx < len(sc_names):
                    disk['storage_class'] = sc_names[sc_idx]
                else:
                    disk['storage_class'] = sc_names[default_sc_idx - 1] if default_sc_idx > 0 else sc_names[0]
            except ValueError:
                disk['storage_class'] = sc_names[default_sc_idx - 1] if default_sc_idx > 0 else sc_names[0]
        
        # Summary before creation
        print(colored("\n   Summary:", Colors.BOLD))
        for disk in disk_info:
            print(f"     {disk['name']} ({disk['size_gi']} GiB) → {disk['storage_class']}")
        
        # Create PVCs
        confirm = self.input_prompt(f"\n   Create {len(disk_info)} PVC(s)? (y/n) [y]") or "y"
        if confirm.lower() != 'y':
            return
        
        for disk in disk_info:
            print(colored(f"\n   Creating PVC: {disk['name']} ({disk['size_gi']} GiB) on {disk['storage_class']}...", Colors.CYAN))
            try:
                self.harvester.create_empty_block_pvc(
                    name=disk['name'],
                    size_gi=disk['size_gi'],
                    storage_class=disk['storage_class'],
                    namespace=namespace
                )
                print(colored(f"   ✅ PVC created: {disk['name']}", Colors.GREEN))
            except Exception as e:
                if "already exists" in str(e):
                    print(colored(f"   ⚠️  PVC already exists: {disk['name']}", Colors.YELLOW))
                else:
                    print(colored(f"   ❌ Error: {e}", Colors.RED))
        
        print(colored(f"\n✅ PVCs created.", Colors.GREEN))
    
    def import_disks_to_pvcs(self):
        """Import disk data from staging to Harvester PVCs."""
        print(colored("\n📥 Import Disks to Harvester", Colors.BOLD))
        print(colored("=" * 60, Colors.BLUE))
        
        if not self.harvester and not self.connect_harvester():
            return
        
        vm_name = self._selected_vm.lower().replace(' ', '-')
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        vm_dir = os.path.join(staging_dir, 'migrations', vm_name)
        
        # Find QCOW2 files
        qcow2_files = []
        if os.path.exists(vm_dir):
            for f in sorted(os.listdir(vm_dir)):
                if f.endswith('.qcow2'):
                    qcow2_files.append(f)
        
        if not qcow2_files:
            print(colored(f"❌ No QCOW2 files found in {vm_dir}", Colors.RED))
            return
        
        # Get disk sizes
        import subprocess
        import math
        disk_info = []
        print(f"\n📁 Found {len(qcow2_files)} disk(s) for {self._selected_vm}:")
        for i, f in enumerate(qcow2_files):
            qcow2_path = os.path.join(vm_dir, f)
            size_gi = 0
            try:
                result = subprocess.run(
                    ['qemu-img', 'info', '--output=json', qcow2_path],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    virtual_size = info.get('virtual-size', 0)
                    size_gi = math.ceil(virtual_size / (1024**3))
            except:
                pass
            disk_info.append({'index': i, 'file': f, 'size_gi': size_gi})
            print(f"   [{i}] {f} ({size_gi} GiB)")
        
        # Ask which disk(s) to import
        print(f"\n   Options: 'all' or disk number (0-{len(qcow2_files)-1})")
        choice = self.input_prompt("   Import which disk(s)? [all]") or "all"
        
        import_all = choice.lower() == 'all'
        
        if import_all:
            disks_to_import = disk_info
        else:
            try:
                disk_idx = int(choice)
                if disk_idx < 0 or disk_idx >= len(disk_info):
                    print(colored("❌ Invalid disk number", Colors.RED))
                    return
                disks_to_import = [disk_info[disk_idx]]
            except:
                print(colored("❌ Invalid choice", Colors.RED))
                return
        
        # Check if step already done (only if importing all)
        if import_all and self.is_step_done(self._selected_vm, 'import_disks'):
            date = self.get_step_date(self._selected_vm, 'import_disks')
            date_str = date[:10] if date else "unknown"
            print(colored(f"\n⚠️  Step 'Import disks to Harvester' already completed ({date_str})", Colors.YELLOW))
            confirm = self.input_prompt("   Re-run this step? (y/n) [n]") or "n"
            if confirm.lower() != 'y':
                return
        
        # Get namespace - numbered list selection
        namespaces = self.harvester.list_namespaces()
        excluded_ns = ['kube-system', 'kube-public', 'kube-node-lease', 
                       'cattle-system', 'cattle-fleet-system', 'cattle-impersonation-system',
                       'harvester-system', 'longhorn-system', 'fleet-local']
        ns_names = sorted([ns.get('metadata', {}).get('name', '') for ns in namespaces 
                   if ns.get('metadata', {}).get('name', '') not in excluded_ns
                   and not ns.get('metadata', {}).get('name', '').startswith('kube-')
                   and not ns.get('metadata', {}).get('name', '').startswith('cattle-')])
        
        print(colored("\n   Available namespaces:", Colors.BOLD))
        default_ns_idx = 0
        for i, ns in enumerate(ns_names, 1):
            if ns == "default":
                default_ns_idx = i
                print(f"     {i}. {ns}                    ← default")
            else:
                print(f"     {i}. {ns}")
        
        ns_choice = self.input_prompt(f"   Select namespace [{default_ns_idx}]") or str(default_ns_idx)
        try:
            ns_idx = int(ns_choice) - 1
            if 0 <= ns_idx < len(ns_names):
                namespace = ns_names[ns_idx]
            else:
                namespace = "default"
        except ValueError:
            namespace = "default"
        print(colored(f"   → Using: {namespace}", Colors.CYAN))
        
        # Get storage classes - numbered list
        scs = self.harvester.list_storage_classes()
        sc_names = sorted([sc.get('metadata', {}).get('name', '') for sc in scs])
        
        print(colored("\n   Available storage classes:", Colors.BOLD))
        default_sc_idx = 0
        for i, sc in enumerate(sc_names, 1):
            if sc == "harvester-longhorn-dual-node":
                default_sc_idx = i
                print(f"     {i}. {sc}    ← default")
            else:
                print(f"     {i}. {sc}")
        
        if default_sc_idx == 0 and sc_names:
            default_sc_idx = 1
        
        # Storage class selection per disk
        print()
        for disk in disks_to_import:
            sc_choice = self.input_prompt(f"   Storage class for disk{disk['index']} ({disk['size_gi']} GiB) [{default_sc_idx}]") or str(default_sc_idx)
            try:
                sc_idx = int(sc_choice) - 1
                if 0 <= sc_idx < len(sc_names):
                    disk['storage_class'] = sc_names[sc_idx]
                else:
                    disk['storage_class'] = sc_names[default_sc_idx - 1] if default_sc_idx > 0 else sc_names[0]
            except ValueError:
                disk['storage_class'] = sc_names[default_sc_idx - 1] if default_sc_idx > 0 else sc_names[0]
        
        # Summary
        print(colored("\n   Summary:", Colors.BOLD))
        print(f"     Namespace: {namespace}")
        for disk in disks_to_import:
            print(f"     disk{disk['index']} ({disk['size_gi']} GiB) → {disk['storage_class']}")
        
        confirm = self.input_prompt(f"\n   Start import of {len(disks_to_import)} disk(s)? (y/n) [y]") or "y"
        if confirm.lower() != 'y':
            return
        
        # Import each disk
        success_count = 0
        for disk in disks_to_import:
            print(colored(f"\n{'='*60}", Colors.BLUE))
            print(colored(f"   Importing disk{disk['index']}: {disk['file']}", Colors.BOLD))
            print(colored(f"{'='*60}", Colors.BLUE))
            
            self.import_vm_disk(
                vm_name=vm_name,
                disk_idx=disk['index'],
                namespace=namespace,
                storage_class=disk['storage_class']
            )
            success_count += 1
        
        # Update tracker only if ALL disks imported
        if import_all and success_count == len(disk_info):
            self.update_step(self._selected_vm, 'import_disks')
            print(colored(f"\n   ✅ Step 'import_disks' marked complete in tracker", Colors.GREEN))
        elif not import_all:
            print(colored(f"\n   ℹ️  Single disk imported. Run 'all' to mark step complete.", Colors.YELLOW))

    def import_to_harvester(self):
        """Create independent volume in Harvester using CDI DataVolume."""
        print(colored("\n📦 Create Volume in Harvester (DataVolume)", Colors.BOLD))
        print(colored("=" * 60, Colors.BLUE))
        print(colored("Creates a PVC with NO backing image dependency.", Colors.GREEN))
        print(colored("The volume is 100% independent.", Colors.GREEN))
        print(colored("=" * 60, Colors.BLUE))
        
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
        
        # Volume name
        file_basename = os.path.basename(selected_file['name']).replace('.qcow2', '')
        vol_name = self.input_prompt(f"Volume name [{file_basename}]") or file_basename
        vol_name = vol_name.lower().replace('_', '-')
        
        # Namespace
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
        
        # Storage Class
        print(colored("\n💾 Storage Class:", Colors.BOLD))
        
        all_scs = self.harvester.list_storage_classes()
        valid_scs = []
        default_sc_idx = 0
        
        for sc in all_scs:
            sc_name = sc.get('metadata', {}).get('name', '')
            if sc_name.startswith('longhorn-image-') or (sc_name.startswith('longhorn-') and '-disk' in sc_name):
                continue
            if 'vmstate' in sc_name:
                continue
            valid_scs.append(sc)
            if 'dual-node' in sc_name:
                default_sc_idx = len(valid_scs)
            annotations = sc.get('metadata', {}).get('annotations', {})
            if annotations.get('storageclass.kubernetes.io/is-default-class') == 'true' and default_sc_idx == 0:
                default_sc_idx = len(valid_scs)
        
        print("   Available storage classes:")
        for i, sc in enumerate(valid_scs, 1):
            sc_name = sc.get('metadata', {}).get('name', 'N/A')
            params = sc.get('parameters', {})
            replicas = params.get('numberOfReplicas', '?')
            marker = " ← recommended" if i == default_sc_idx else ""
            print(f"     {i}. {sc_name} ({replicas} replicas){marker}")
        
        default_choice = str(default_sc_idx) if default_sc_idx else "1"
        sc_choice = self.input_prompt(f"Storage class [{default_choice}]") or default_choice
        
        try:
            sc_idx = int(sc_choice) - 1
            selected_sc = valid_scs[sc_idx].get('metadata', {}).get('name')
        except:
            selected_sc = valid_scs[0].get('metadata', {}).get('name')
        
        print(colored(f"   ✓ Using: {selected_sc}", Colors.GREEN))
        
        # Volume size - get virtual size
        import subprocess
        import math
        try:
            result = subprocess.run(
                ['qemu-img', 'info', '--output=json', selected_file['path']],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                virtual_size = info.get('virtual-size', 0)
                virtual_size_gb = virtual_size / (1024**3)
                min_size_gi = math.ceil(virtual_size_gb)
                print(f"\n📏 Image virtual size: {virtual_size_gb:.2f} GB")
                print(f"   Volume size: {min_size_gi} GiB")
            else:
                min_size_gi = int(selected_file['size'] / (1024**3)) + 5
        except:
            min_size_gi = int(selected_file['size'] / (1024**3)) + 5
        
        default_size = min_size_gi
        size_input = self.input_prompt(f"Volume size in GiB [{default_size}]")
        size_gi = int(size_input) if size_input else default_size
        
        # Check NFS config for sparse import
        transfer_config = self.config.get('transfer', {})
        nfs_server = transfer_config.get('nfs_server')
        nfs_path = transfer_config.get('nfs_path')
        
        if not nfs_server or not nfs_path:
            print(colored("❌ NFS not configured. Add nfs_server and nfs_path to config.yaml", Colors.RED))
            return
        
        # Summary
        print(colored("\n📋 Summary:", Colors.BOLD))
        print(f"   Source: {selected_file['name']}")
        print(f"   Volume: {vol_name}")
        print(f"   Namespace: {namespace}")
        print(f"   Storage class: {selected_sc}")
        print(f"   Size: {size_gi} GiB")
        print(f"   Method: Sparse Import (NFS)")
        
        confirm = self.input_prompt("\nCreate volume? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        # Sparse import via NFS
        self._import_sparse(vol_name, size_gi, selected_sc, namespace, 
                           nfs_server, nfs_path, selected_file['name'])
    
    def _import_sparse(self, vol_name, size_gi, storage_class, namespace,
                       nfs_server, nfs_path, qcow2_file):
        """Import disk using sparse conversion via NFS mount."""
        print(colored("\n🚀 Starting Sparse Import...", Colors.CYAN))
        print(f"   NFS: {nfs_server}:{nfs_path}")
        print(f"   File: {qcow2_file}")
        print(colored("\n--- Pod Logs ---", Colors.BOLD))
        
        def progress_callback(stage, detail, logs):
            if logs:
                # Print new log lines
                for line in logs.strip().split('\n'):
                    if line.strip():
                        print(f"   {line}")
        
        import time
        start_time = time.time()
        
        try:
            success = self.harvester.import_disk_sparse(
                pvc_name=vol_name,
                size_gi=size_gi,
                storage_class=storage_class,
                nfs_server=nfs_server,
                nfs_path=nfs_path,
                qcow2_file=qcow2_file,
                namespace=namespace,
                progress_callback=progress_callback,
                timeout=7200
            )
            
            elapsed = int(time.time() - start_time)
            
            if success:
                print(colored(f"\n\n✅ Volume created: {namespace}/{vol_name}", Colors.GREEN))
                print(colored(f"   Completed in {elapsed} seconds", Colors.GREEN))
                print(colored("   Ready for VM creation!", Colors.GREEN))
            else:
                print(colored(f"\n\n❌ Import failed after {elapsed}s", Colors.RED))
                print(colored("   Check pod logs for details", Colors.RED))
                
        except Exception as e:
            print(colored(f"\n\n❌ Error: {e}", Colors.RED))
    
    def import_vm_disk(self, vm_name: str, disk_idx: int = None, 
                       namespace: str = "harvester-public",
                       storage_class: str = "harvester-longhorn-dual-node"):
        """
        Import VM disk(s) from staging to Harvester.
        
        CLI command: migrate.py import <vmname> --disk <N>
        
        Args:
            vm_name: VM name (e.g., wlchgvaopefs1)
            disk_idx: Specific disk index (0, 1, ...) or None for all
            namespace: Target Harvester namespace
            storage_class: Storage class to use
        """
        print(colored("\n📦 Import VM Disk to Harvester", Colors.BOLD))
        print(colored("=" * 60, Colors.BLUE))
        
        self.init_actions()
        
        if not self.harvester and not self.connect_harvester():
            return
        
        # Get transfer config
        transfer_config = self.config.get('transfer', {})
        nfs_server = transfer_config.get('nfs_server')
        nfs_path = transfer_config.get('nfs_path')
        
        if not nfs_server or not nfs_path:
            print(colored("❌ NFS not configured in config.yaml", Colors.RED))
            return
        
        # Find QCOW2 files for this VM
        qcow2_files = self.actions.list_qcow2_files()
        vm_files = [f for f in qcow2_files if f['name'].startswith(f"migrations/{vm_name}/")]
        
        if not vm_files:
            print(colored(f"❌ No QCOW2 files found for VM: {vm_name}", Colors.RED))
            print(f"   Looking in: {nfs_path}/migrations/{vm_name}/")
            return
        
        # Sort by disk number
        vm_files.sort(key=lambda f: f['name'])
        
        print(f"\n📁 Found {len(vm_files)} disk(s) for {vm_name}:")
        for i, f in enumerate(vm_files):
            size_info = format_size(f['size'])
            print(f"   [{i}] {os.path.basename(f['name'])} ({size_info})")
        
        # Filter to specific disk if requested
        if disk_idx is not None:
            if disk_idx < 0 or disk_idx >= len(vm_files):
                print(colored(f"❌ Invalid disk index: {disk_idx} (valid: 0-{len(vm_files)-1})", Colors.RED))
                return
            vm_files = [vm_files[disk_idx]]
            print(colored(f"\n→ Importing disk {disk_idx} only", Colors.CYAN))
        else:
            print(colored(f"\n→ Importing all {len(vm_files)} disk(s)", Colors.CYAN))
        
        # Import each disk
        import subprocess
        import math
        
        for i, qcow2_info in enumerate(vm_files):
            qcow2_file = qcow2_info['name']
            qcow2_path = qcow2_info['path']
            
            # Derive volume name from file
            base_name = os.path.basename(qcow2_file).replace('.qcow2', '')
            vol_name = base_name.lower().replace('_', '-')
            
            print(colored(f"\n{'='*60}", Colors.BLUE))
            print(colored(f"📀 Disk {i+1}/{len(vm_files)}: {base_name}", Colors.BOLD))
            print(colored(f"{'='*60}", Colors.BLUE))
            
            # Get virtual size
            try:
                result = subprocess.run(
                    ['qemu-img', 'info', '--output=json', qcow2_path],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    virtual_size = info.get('virtual-size', 0)
                    actual_size = info.get('actual-size', qcow2_info['size'])
                    virtual_size_gb = virtual_size / (1024**3)
                    actual_size_gb = actual_size / (1024**3)
                    size_gi = math.ceil(virtual_size_gb)
                    print(f"   Virtual size: {virtual_size_gb:.2f} GB")
                    print(f"   Actual data:  {actual_size_gb:.2f} GB")
                    print(f"   PVC size:     {size_gi} GiB")
                else:
                    size_gi = int(qcow2_info['size'] / (1024**3)) + 5
            except Exception as e:
                print(colored(f"   ⚠️  Could not get size: {e}", Colors.YELLOW))
                size_gi = int(qcow2_info['size'] / (1024**3)) + 5
            
            print(f"   Volume name:  {vol_name}")
            print(f"   Namespace:    {namespace}")
            print(f"   Storage:      {storage_class}")
            
            # Run the import
            self._import_sparse(vol_name, size_gi, storage_class, namespace,
                               nfs_server, nfs_path, qcow2_file)
    
    def _import_datavolume(self, vol_name, size_gi, storage_class, namespace,
                           selected_file, transfer_config):
        """Import disk using CDI DataVolume (HTTP)."""
        # Start HTTP server
        print(colored("\n🚀 Starting HTTP server...", Colors.CYAN))
        
        http_server_ip = transfer_config.get('http_server_ip', None)
        
        http_url = self.actions.start_http_server(8080, bind_ip=http_server_ip)
        print(colored(f"✅ Server running at {http_url}", Colors.GREEN))
        
        if "127.0" in http_url:
            print(colored("⚠️  Warning: URL contains localhost!", Colors.YELLOW))
            print(colored("   Add http_server_ip to config.yaml", Colors.YELLOW))
            self.actions.stop_http_server()
            return
        
        # Build file URL
        file_url = f"{http_url}/{selected_file['name']}"
        print(f"   File URL: {file_url}")
        
        # Create DataVolume
        print(colored("\n📦 Creating DataVolume...", Colors.CYAN))
        print("   CDI will download and convert qcow2 → raw")
        
        try:
            result = self.harvester.create_datavolume(
                name=vol_name,
                http_url=file_url,
                size_gi=size_gi,
                storage_class=storage_class,
                namespace=namespace
            )
            print(colored(f"✅ DataVolume created: {vol_name}", Colors.GREEN))
        except Exception as e:
            print(colored(f"❌ Failed to create DataVolume: {e}", Colors.RED))
            self.actions.stop_http_server()
            return
        
        # Wait for completion
        print(colored("\n⏳ Waiting for CDI to download and process...", Colors.CYAN))
        print("   Press Ctrl+C to stop waiting (import continues in background)")
        print("   Note: 'prime-*' scratch volumes are normal and will be cleaned up")
        
        import time
        start_time = time.time()
        last_phase = ""
        
        try:
            while True:
                status = self.harvester.get_datavolume_status(vol_name, namespace)
                phase = status.get('phase', 'Unknown')
                progress = status.get('progress', '')
                elapsed = int(time.time() - start_time)
                
                # Show phase changes
                if phase != last_phase:
                    print(f"\n   Phase: {phase}")
                    last_phase = phase
                
                # Progress display
                progress_str = f" {progress}" if progress and progress != 'N/A' else ""
                print(f"\r   [{elapsed}s]{progress_str}     ", end='', flush=True)
                
                if phase == 'Succeeded':
                    print(colored(f"\n\n✅ Volume created: {namespace}/{vol_name}", Colors.GREEN))
                    print(colored("   Ready for VM creation!", Colors.GREEN))
                    break
                elif phase == 'Failed':
                    print(colored(f"\n\n❌ Import failed!", Colors.RED))
                    # Get error details from conditions
                    conditions = status.get('conditions', [])
                    for cond in conditions:
                        if cond.get('type') == 'Running' and cond.get('status') == 'False':
                            print(f"   Reason: {cond.get('reason', 'Unknown')}")
                            msg = cond.get('message', '')
                            if msg:
                                print(f"   Message: {msg[:200]}")
                    break
                elif 'Error' in phase:
                    print(colored(f"\n\n❌ Error: {phase}", Colors.RED))
                    break
                
                # Long timeout for large files
                if elapsed > 3600:  # 1 hour
                    print(colored(f"\n\n⚠️  Timeout after 1h - check manually:", Colors.YELLOW))
                    print(f"   kubectl get dv {vol_name} -n {namespace}")
                    print(f"   kubectl describe dv {vol_name} -n {namespace}")
                    break
                
                time.sleep(5)
                
        except KeyboardInterrupt:
            print(colored(f"\n\n⚠️  Import continues in background", Colors.YELLOW))
        
        finally:
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
        """Create a VM in Harvester using existing PVCs (created via DataVolume)."""
        if not self.harvester and not self.connect_harvester():
            return
        
        # Check if step already done
        if self._selected_vm and not self.check_step_and_confirm(self._selected_vm, 'create_vm'):
            return
        
        print(colored("\n🖥️  Create VM in Harvester", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        # Get namespace first
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
        
        # List available PVCs
        all_pvcs = self.harvester.list_pvcs(namespace)
        if not all_pvcs:
            print(colored(f"❌ No PVCs found in {namespace}. Import volumes first (option 6).", Colors.RED))
            return
        
        # Detect VM names from PVCs (e.g., "vm-name-disk0" → "vm-name")
        detected_vms = {}
        for pvc in all_pvcs:
            pvc_name = pvc.get('metadata', {}).get('name', '')
            if '-disk' in pvc_name:
                vm_base = pvc_name.rsplit('-disk', 1)[0]
                if vm_base not in detected_vms:
                    detected_vms[vm_base] = []
                detected_vms[vm_base].append(pvc)
        
        # Look for saved VM configs
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        migrations_dir = os.path.join(staging_dir, 'migrations')
        
        # Initialize variables
        loaded_config = None
        source_nics = []
        vm_info = {}
        
        # Show detected VMs from PVCs
        if detected_vms:
            print(colored("\n🔍 Detected VMs from PVCs:", Colors.BOLD))
            detected_list = list(detected_vms.keys())
            
            # Check if current selection matches a detected VM
            current_match = None
            for i, vm_base in enumerate(detected_list):
                if self._selected_vm and vm_base.lower() == self._selected_vm.lower():
                    current_match = i
                    break
            
            for i, vm_base in enumerate(detected_list, 1):
                disk_count = len(detected_vms[vm_base])
                config_path = os.path.join(migrations_dir, vm_base.lower(), 'vm-config.json')
                has_config = os.path.exists(config_path)
                status = colored("✓ config", Colors.GREEN) if has_config else colored("○ manual", Colors.YELLOW)
                selected = " ← current" if current_match == i - 1 else ""
                print(f"   {i}. {vm_base} ({disk_count} disk(s)) {status}{selected}")
            print(f"   0. Enter manually")
            
            default_choice = str(current_match + 1) if current_match is not None else "1"
            choice = self.input_prompt(f"\nSelect VM [{default_choice}]") or default_choice
            try:
                idx = int(choice) - 1
                if idx >= 0 and idx < len(detected_list):
                    self._selected_vm = detected_list[idx]
            except:
                pass
        
        # Load vm-config.json if available
        if self._selected_vm:
            config_path = os.path.join(migrations_dir, self._selected_vm.lower(), 'vm-config.json')
            if os.path.exists(config_path):
                try:
                    with open(config_path) as f:
                        loaded_config = json.load(f)
                    print(colored(f"\n✅ Loaded config: {config_path}", Colors.GREEN))
                    
                    nutanix_info = loaded_config.get('nutanix', {})
                    vm_info = {
                        'vcpu': nutanix_info.get('cpu_cores', loaded_config.get('cpu_cores', 4)),
                        'memory_mb': nutanix_info.get('memory_mb', loaded_config.get('memory_mb', 8192)),
                        'boot_type': nutanix_info.get('boot_type', loaded_config.get('boot_type', 'UEFI')),
                    }
                    print(f"   CPU: {vm_info['vcpu']}, RAM: {vm_info['memory_mb']//1024} GB, Boot: {vm_info['boot_type']}")
                    
                    # Load NICs info - they are in network.interfaces
                    network_info = loaded_config.get('network', {})
                    source_nics = network_info.get('interfaces', [])
                    if source_nics:
                        print(colored(f"   NICs ({len(source_nics)}):", Colors.CYAN))
                        for i, nic in enumerate(source_nics):
                            nic_name = nic.get('name', 'unknown')
                            mac = nic.get('mac', 'N/A')
                            ip = nic.get('ip') if not nic.get('dhcp') else 'DHCP'
                            print(f"      NIC-{i}: {nic_name} - MAC: {mac}, IP: {ip}")
                except Exception as e:
                    print(colored(f"   ⚠️  Error loading config: {e}", Colors.YELLOW))
        
        # VM Name
        default_name = self._selected_vm or ""
        vm_name = self.input_prompt(f"VM name [{default_name}]") or default_name
        if not vm_name:
            print(colored("❌ VM name required", Colors.RED))
            return
        vm_name = vm_name.lower().replace('_', '-')
        
        # Select PVCs for disks
        print(colored("\n💾 Select PVCs for VM disks:", Colors.BOLD))
        
        # Auto-select if we have detected disks for this VM
        selected_pvcs = []
        # Check case-insensitive match
        detected_key = None
        for key in detected_vms:
            if key.lower() == vm_name.lower():
                detected_key = key
                break
            if self._selected_vm and key.lower() == self._selected_vm.lower():
                detected_key = key
                break
        
        if detected_key:
            auto_pvcs = sorted(detected_vms[detected_key], key=lambda p: p.get('metadata', {}).get('name', ''))
            print(f"   Auto-detected {len(auto_pvcs)} disk(s) for {detected_key}:")
            for i, pvc in enumerate(auto_pvcs):
                pvc_name = pvc.get('metadata', {}).get('name', '')
                size = pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
                print(f"      Disk {i}: {pvc_name} ({size})")
                selected_pvcs.append({'name': pvc_name, 'size': size})
            
            use_auto = self.input_prompt("\nUse these PVCs? (y/n) [y]") or "y"
            if use_auto.lower() != 'y':
                selected_pvcs = []
        
        # Manual selection if needed
        if not selected_pvcs:
            print("\nAvailable PVCs:")
            for i, pvc in enumerate(all_pvcs, 1):
                pvc_name = pvc.get('metadata', {}).get('name', '')
                size = pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
                print(f"  {i}. {pvc_name} ({size})")
            
            pvc_choices = self.input_prompt("PVC numbers (comma-separated, first=boot)")
            if not pvc_choices:
                return
            
            try:
                indices = [int(x.strip()) - 1 for x in pvc_choices.split(',')]
                for idx in indices:
                    pvc = all_pvcs[idx]
                    selected_pvcs.append({
                        'name': pvc.get('metadata', {}).get('name', ''),
                        'size': pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', 'N/A')
                    })
            except:
                print(colored("Invalid selection", Colors.RED))
                return
        
        # CPU and RAM
        default_cpu = str(vm_info.get('vcpu', 4))
        default_ram = str(vm_info.get('memory_mb', 8192) // 1024)
        
        cpu = int(self.input_prompt(f"CPU cores [{default_cpu}]") or default_cpu)
        ram = int(self.input_prompt(f"RAM in GB [{default_ram}]") or default_ram)
        
        # Boot type
        default_boot = "2" if vm_info.get('boot_type') == 'UEFI' else "1"
        print(colored("\n🔧 Boot Type:", Colors.BOLD))
        print("   1. BIOS (legacy)")
        print("   2. UEFI (modern, for Windows 10+)")
        
        boot_choice = self.input_prompt(f"Boot type [{default_boot}]") or default_boot
        boot_type = "UEFI" if boot_choice == "2" else "BIOS"
        
        # Disk bus
        print(colored("\n💾 Disk Bus:", Colors.BOLD))
        print("   1. sata   - Most compatible (recommended for migration)")
        print("   2. virtio - Best performance (requires VirtIO drivers)")
        
        bus_choice = self.input_prompt("Disk bus [1]") or "1"
        disk_bus = "virtio" if bus_choice == "2" else "sata"
        
        # Network - Map source NICs to Harvester networks
        print(colored("\n🌐 Network Configuration:", Colors.BOLD))
        
        # Get all networks from all namespaces
        all_networks = self.harvester.list_all_networks()
        
        if not all_networks:
            print(colored("   No networks found!", Colors.RED))
            return
        
        # Parse networks with namespace and VLAN info
        network_list = []
        for net in all_networks:
            net_name = net.get('metadata', {}).get('name', 'N/A')
            net_ns = net.get('metadata', {}).get('namespace', 'N/A')
            
            # Parse VLAN from config
            vlan_id = "-"
            try:
                config_str = net.get('spec', {}).get('config', '{}')
                config = json.loads(config_str)
                if 'vlan' in config:
                    vlan_id = str(config.get('vlan', '-'))
            except:
                pass
            
            network_list.append({
                'name': net_name,
                'namespace': net_ns,
                'vlan': vlan_id,
                'full_name': f"{net_ns}/{net_name}"
            })
        
        # Sort by namespace then name
        network_list.sort(key=lambda x: (x['namespace'], x['name']))
        
        print("   Available Harvester networks:")
        for i, net in enumerate(network_list, 1):
            vlan_info = f" (VLAN {net['vlan']})" if net['vlan'] != "-" else ""
            print(f"      {i}. {net['namespace']}/{net['name']}{vlan_info}")
        
        # Determine number of NICs from source config or ask
        if source_nics:
            nic_count = len(source_nics)
            print(colored(f"\n   Mapping {nic_count} NIC(s) from source VM:", Colors.CYAN))
        else:
            nic_count_input = self.input_prompt("\n   Number of NICs [1]") or "1"
            try:
                nic_count = int(nic_count_input)
            except:
                nic_count = 1
        
        # Configure each NIC
        selected_networks = []
        for nic_idx in range(nic_count):
            # Show source NIC info if available
            if source_nics and nic_idx < len(source_nics):
                src_nic = source_nics[nic_idx]
                src_name = src_nic.get('name', 'unknown')
                src_mac = src_nic.get('mac', 'N/A')
                src_ip = src_nic.get('ip') if not src_nic.get('dhcp') else 'DHCP'
                print(f"\n   NIC-{nic_idx} source: {src_name}")
                print(f"      MAC: {src_mac}, IP: {src_ip}")
                
                prompt = f"      Select Harvester network [1]"
                default_idx = 1
            else:
                prompt = f"   NIC-{nic_idx} network [1]"
                default_idx = 1
            
            net_choice = self.input_prompt(prompt) or str(default_idx)
            try:
                net_idx = int(net_choice) - 1
                selected_net = network_list[net_idx]
            except:
                selected_net = network_list[0]
            
            selected_networks.append(selected_net)
            print(colored(f"      → NIC-{nic_idx}: {selected_net['full_name']}", Colors.GREEN))
        
        # Summary
        print(colored("\n📋 Summary:", Colors.BOLD))
        print(f"   VM: {vm_name}")
        print(f"   Namespace: {namespace}")
        print(f"   CPU: {cpu}, RAM: {ram} GB, Boot: {boot_type}, Bus: {disk_bus}")
        print(f"   Network ({len(selected_networks)} NIC(s)):")
        for i, net in enumerate(selected_networks):
            print(f"      NIC-{i}: {net['full_name']}")
        print(f"   Disks ({len(selected_pvcs)}):")
        for i, pvc in enumerate(selected_pvcs):
            boot = " (boot)" if i == 0 else ""
            print(f"      {pvc['name']}{boot}")
        
        confirm = self.input_prompt("\nCreate VM? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        # Build VM manifest with PVC references (NO images)
        print(colored("\n🚀 Creating VM...", Colors.CYAN))
        
        disks_spec = []
        volumes_spec = []
        volume_claim_templates = []
        
        for i, pvc in enumerate(selected_pvcs):
            disk_name = f"disk-{i}"
            disk_spec = {"name": disk_name, "disk": {"bus": disk_bus}}
            if i == 0:
                disk_spec["bootOrder"] = 1
            disks_spec.append(disk_spec)
            
            volumes_spec.append({
                "name": disk_name,
                "persistentVolumeClaim": {"claimName": pvc['name']}
            })
            
            # Build volume claim template for UI annotation
            volume_claim_templates.append({
                "metadata": {"name": pvc['name'], "annotations": {"harvesterhci.io/imageId": ""}},
                "spec": {
                    "accessModes": ["ReadWriteMany"],
                    "resources": {"requests": {"storage": pvc['size']}},
                    "volumeMode": "Block"
                }
            })
        
        net_model = "e1000" if disk_bus == "sata" else "virtio"
        
        # Build interfaces and networks specs for multi-NIC
        interfaces_spec = []
        networks_spec = []
        network_ips_annotation = {}
        for i, net in enumerate(selected_networks):
            nic_name = f"nic-{i}"
            interfaces_spec.append({
                "name": nic_name,
                "model": net_model,
                "bridge": {}
            })
            networks_spec.append({
                "name": nic_name,
                "multus": {"networkName": net['full_name']}
            })
            network_ips_annotation[nic_name] = ""
        
        manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": vm_name,
                "namespace": namespace,
                "labels": {"harvesterhci.io/creator": "harvesterhci"},
                "annotations": {
                    "harvesterhci.io/vmRunStrategy": "RerunOnFailure",
                    "harvesterhci.io/volumeClaimTemplates": json.dumps(volume_claim_templates),
                    "harvesterhci.io/networkIps": json.dumps(network_ips_annotation)
                }
            },
            "spec": {
                "runStrategy": "RerunOnFailure",
                "template": {
                    "metadata": {
                        "annotations": {
                            "harvesterhci.io/sshNames": "[]"
                        },
                        "labels": {
                            "harvesterhci.io/creator": "harvester",
                            "harvesterhci.io/vmName": vm_name
                        }
                    },
                    "spec": {
                        "hostname": vm_name,
                        "evictionStrategy": "LiveMigrateIfPossible",
                        "domain": {
                            "cpu": {
                                "cores": cpu,
                                "sockets": 1,
                                "maxSockets": 1,
                                "threads": 1
                            },
                            "memory": {"guest": f"{ram}Gi"},
                            "resources": {
                                "limits": {"cpu": str(cpu), "memory": f"{ram}Gi"},
                                "requests": {"cpu": "250m", "memory": f"{ram * 682}Mi"}
                            },
                            "devices": {
                                "disks": disks_spec,
                                "interfaces": interfaces_spec,
                                "inputs": [{"bus": "usb", "name": "tablet", "type": "tablet"}]
                            },
                            "features": {"acpi": {"enabled": True}},
                            "machine": {"type": "q35"}
                        },
                        "networks": networks_spec,
                        "volumes": volumes_spec,
                        "terminationGracePeriodSeconds": 120
                    }
                }
            }
        }
        
        if boot_type == "UEFI":
            manifest['spec']['template']['spec']['domain']['firmware'] = {
                "bootloader": {"efi": {"secureBoot": False, "persistent": False}}
            }
        
        try:
            self.harvester.create_vm(manifest)
            print(colored(f"\n✅ VM created: {vm_name}", Colors.GREEN))
            print(colored("   This VM has NO image dependencies!", Colors.GREEN))
            
            # Update tracker
            if self._selected_vm:
                self.update_step(self._selected_vm, 'create_vm')
                print(colored(f"   ✅ Step 'create_vm' marked complete in tracker", Colors.GREEN))
            
            start = self.input_prompt("\nStart VM? (y/n) [y]") or "y"
            if start.lower() == 'y':
                self.harvester.start_vm(vm_name, namespace)
                print(colored(f"✅ VM starting", Colors.GREEN))
                
                # Show next steps
                if loaded_config and source_nics:
                    print(colored("\n📋 Network settings to restore:", Colors.BOLD))
                    for i, nic in enumerate(source_nics):
                        ip = nic.get('ip') if not nic.get('dhcp') else 'DHCP'
                        print(f"      NIC-{i}: {ip}")
                    print(colored("\n💡 Next step: Menu Migration → Post-migration Windows (option 7)", Colors.YELLOW))
                else:
                    print(colored("\n💡 Next step: Configure VM network if needed", Colors.YELLOW))
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    # === Menus ===
    
    def menu_tracker(self):
        """Migration Tracker menu."""
        while True:
            self.print_header()
            self.print_menu("MIGRATION TRACKER", [
                ("1", "List all migrations"),
                ("2", "Add VM to migration list"),
                ("3", "Remove VM from migration list"),
                ("4", "Reset VM step(s)"),
                ("5", "View VM details"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.tracker_list_all()
                self.pause()
            elif choice == "2":
                self.tracker_add_vm()
                self.pause()
            elif choice == "3":
                self.tracker_remove_vm()
                self.pause()
            elif choice == "4":
                self.tracker_reset_steps()
                self.pause()
            elif choice == "5":
                self.tracker_view_vm()
                self.pause()
            elif choice == "0":
                break
    
    def tracker_list_all(self):
        """List all VMs in the migration tracker."""
        print(colored("\n📋 Migration Status Overview", Colors.BOLD))
        print(colored("=" * 80, Colors.BLUE))
        
        tracker = self.load_tracker()
        migrations = tracker.get('migrations', {})
        
        if not migrations:
            print(colored("   No VMs in migration tracker.", Colors.YELLOW))
            print(colored("   Use 'Add VM to migration list' to start.", Colors.YELLOW))
            return
        
        # Header
        print(f"{'VM Name':<25} {'OS':<10} {'Progress':<12} {'Next Step':<20} {'Added':<12}")
        print(colored("=" * 80, Colors.BLUE))
        
        for vm_key, vm_data in sorted(migrations.items()):
            display_name = vm_data.get('display_name', vm_key)
            os_type = vm_data.get('os_type', 'unknown')
            progress = self.get_vm_progress(vm_key)
            done, total = self.get_vm_progress_count(vm_key)
            next_step = self.get_next_step(vm_key)
            next_label = self.STEP_LABELS.get(next_step, next_step)[:18]
            added = vm_data.get('added_at', '')[:10]
            
            # Color based on progress
            if next_step == "completed":
                progress_colored = colored(progress, Colors.GREEN)
                next_label = colored("✅ Completed", Colors.GREEN)
            elif done == 0:
                progress_colored = colored(progress, Colors.YELLOW)
            else:
                progress_colored = colored(progress, Colors.CYAN)
            
            print(f"{display_name:<25} {os_type:<10} {progress_colored} {next_label:<20} {added:<12}")
        
        print(colored("=" * 80, Colors.BLUE))
        print(f"Total: {len(migrations)} VM(s) in migration pipeline")
    
    def tracker_add_vm(self):
        """Add a VM from Nutanix to the migration tracker."""
        print(colored("\n➕ Add VM to Migration List", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        if not self.nutanix and not self.connect_nutanix():
            return
        
        # Get existing migrations to filter them out
        tracker = self.load_tracker()
        existing = set(tracker.get('migrations', {}).keys())
        
        # List Nutanix VMs
        vms = self.nutanix.list_vms()
        if not vms:
            print(colored("❌ No VMs found in Nutanix", Colors.RED))
            return
        
        # Filter out already tracked VMs and sort
        available_vms = []
        for vm in vms:
            name = vm.get('spec', {}).get('name', '')
            if name.lower() not in existing:
                available_vms.append(vm)
        
        available_vms.sort(key=lambda x: x.get('spec', {}).get('name', '').lower())
        
        if not available_vms:
            print(colored("   All VMs are already in the tracker!", Colors.YELLOW))
            return
        
        print(f"\n   Available VMs ({len(available_vms)}):")
        for i, vm in enumerate(available_vms, 1):
            name = vm.get('spec', {}).get('name', 'N/A')
            power = vm.get('status', {}).get('resources', {}).get('power_state', 'N/A')
            power_icon = "🟢" if power == "ON" else "🔴"
            print(f"   {i:3}. {power_icon} {name}")
        
        print(f"\n   0. Cancel")
        
        choice = self.input_prompt("\nSelect VM to add")
        if not choice or choice == "0":
            return
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available_vms):
                vm_name = available_vms[idx].get('spec', {}).get('name')
                
                # Ask for OS type
                print(colored(f"\n   VM: {vm_name}", Colors.CYAN))
                print("   OS Type:")
                print("   1. Windows")
                print("   2. Linux")
                os_choice = self.input_prompt("   Select OS type [1]") or "1"
                os_type = "linux" if os_choice == "2" else "windows"
                
                if self.add_vm_to_tracker(vm_name, os_type):
                    print(colored(f"\n✅ VM '{vm_name}' added to migration tracker ({os_type})", Colors.GREEN))
                    self._selected_vm = vm_name
                    print(colored(f"   Selected for migration workflow", Colors.GREEN))
            else:
                print(colored("❌ Invalid selection", Colors.RED))
        except ValueError:
            print(colored("❌ Invalid input", Colors.RED))
    
    def tracker_remove_vm(self):
        """Remove a VM from the migration tracker."""
        print(colored("\n➖ Remove VM from Migration List", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        tracker = self.load_tracker()
        migrations = tracker.get('migrations', {})
        
        if not migrations:
            print(colored("   No VMs in tracker.", Colors.YELLOW))
            return
        
        vm_list = list(migrations.keys())
        print("\n   VMs in tracker:")
        for i, vm_key in enumerate(sorted(vm_list), 1):
            vm_data = migrations[vm_key]
            display_name = vm_data.get('display_name', vm_key)
            progress = self.get_vm_progress(vm_key)
            print(f"   {i}. {display_name} {progress}")
        
        print(f"   0. Cancel")
        
        choice = self.input_prompt("\nSelect VM to remove")
        if not choice or choice == "0":
            return
        
        try:
            idx = int(choice) - 1
            sorted_keys = sorted(vm_list)
            if 0 <= idx < len(sorted_keys):
                vm_key = sorted_keys[idx]
                display_name = migrations[vm_key].get('display_name', vm_key)
                
                confirm = self.input_prompt(f"   Remove '{display_name}'? (y/n) [n]") or "n"
                if confirm.lower() == 'y':
                    # Check for staging files to cleanup
                    staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
                    vm_staging_dir = os.path.join(staging_dir, 'migrations', vm_key)
                    
                    files_to_delete = []
                    total_size = 0
                    
                    if os.path.exists(vm_staging_dir):
                        for f in os.listdir(vm_staging_dir):
                            f_path = os.path.join(vm_staging_dir, f)
                            if os.path.isfile(f_path):
                                f_size = os.path.getsize(f_path)
                                files_to_delete.append({'name': f, 'path': f_path, 'size': f_size})
                                total_size += f_size
                    
                    # Ask to cleanup staging files
                    if files_to_delete:
                        print(colored(f"\n   🗑️  Staging files found:", Colors.YELLOW))
                        print(f"      {vm_staging_dir}/")
                        for f in files_to_delete:
                            size_str = format_size(f['size'])
                            print(f"      - {f['name']} ({size_str})")
                        
                        print(f"\n      Total: {format_size(total_size)}")
                        
                        cleanup = self.input_prompt("   Delete staging files? (y/n) [y]") or "y"
                        if cleanup.lower() == 'y':
                            import shutil
                            try:
                                shutil.rmtree(vm_staging_dir)
                                print(colored(f"   ✅ Staging files deleted ({format_size(total_size)} freed)", Colors.GREEN))
                            except Exception as e:
                                print(colored(f"   ⚠️  Could not delete staging files: {e}", Colors.YELLOW))
                    
                    # Remove from tracker
                    if self.remove_vm_from_tracker(vm_key):
                        print(colored(f"✅ VM '{display_name}' removed from tracker", Colors.GREEN))
                        if self._selected_vm and self._selected_vm.lower() == vm_key:
                            self._selected_vm = None
            else:
                print(colored("❌ Invalid selection", Colors.RED))
        except ValueError:
            print(colored("❌ Invalid input", Colors.RED))
    
    def tracker_reset_steps(self):
        """Reset step(s) for a VM in the tracker."""
        print(colored("\n🔄 Reset VM Step(s)", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        tracker = self.load_tracker()
        migrations = tracker.get('migrations', {})
        
        if not migrations:
            print(colored("   No VMs in tracker.", Colors.YELLOW))
            return
        
        # Select VM
        vm_list = sorted(migrations.keys())
        print("\n   Select VM:")
        for i, vm_key in enumerate(vm_list, 1):
            vm_data = migrations[vm_key]
            display_name = vm_data.get('display_name', vm_key)
            progress = self.get_vm_progress(vm_key)
            print(f"   {i}. {display_name} {progress}")
        
        print(f"   0. Cancel")
        
        choice = self.input_prompt("\nSelect VM")
        if not choice or choice == "0":
            return
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(vm_list):
                vm_key = vm_list[idx]
                vm_data = migrations[vm_key]
                display_name = vm_data.get('display_name', vm_key)
                steps = vm_data.get('steps', {})
                
                # Show steps for this VM
                print(colored(f"\n   Steps for {display_name}:", Colors.BOLD))
                for i, step in enumerate(self.MIGRATION_STEPS, 1):
                    step_data = steps.get(step, {})
                    done = step_data.get('done', False)
                    date = step_data.get('date', '')[:10] if step_data.get('date') else '-'
                    status = colored("✓", Colors.GREEN) if done else colored("○", Colors.YELLOW)
                    label = self.STEP_LABELS.get(step, step)
                    print(f"   {i}. {status} {label:<30} {date}")
                
                print(f"   A. Reset ALL steps")
                print(f"   0. Cancel")
                
                step_choice = self.input_prompt("\n   Select step to reset")
                if not step_choice or step_choice == "0":
                    return
                
                if step_choice.lower() == 'a':
                    confirm = self.input_prompt(f"   Reset ALL steps for {display_name}? (y/n) [n]") or "n"
                    if confirm.lower() == 'y':
                        self.reset_step(vm_key, 'all')
                        print(colored(f"✅ All steps reset for {display_name}", Colors.GREEN))
                else:
                    step_idx = int(step_choice) - 1
                    if 0 <= step_idx < len(self.MIGRATION_STEPS):
                        step_name = self.MIGRATION_STEPS[step_idx]
                        self.reset_step(vm_key, step_name)
                        print(colored(f"✅ Step '{self.STEP_LABELS.get(step_name)}' reset for {display_name}", Colors.GREEN))
                    else:
                        print(colored("❌ Invalid selection", Colors.RED))
            else:
                print(colored("❌ Invalid selection", Colors.RED))
        except ValueError:
            print(colored("❌ Invalid input", Colors.RED))
    
    def tracker_view_vm(self):
        """View detailed status of a VM in the tracker."""
        print(colored("\n🔍 View VM Details", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        tracker = self.load_tracker()
        migrations = tracker.get('migrations', {})
        
        if not migrations:
            print(colored("   No VMs in tracker.", Colors.YELLOW))
            return
        
        vm_list = sorted(migrations.keys())
        print("\n   Select VM:")
        for i, vm_key in enumerate(vm_list, 1):
            display_name = migrations[vm_key].get('display_name', vm_key)
            print(f"   {i}. {display_name}")
        
        print(f"   0. Cancel")
        
        choice = self.input_prompt("\nSelect VM")
        if not choice or choice == "0":
            return
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(vm_list):
                vm_key = vm_list[idx]
                vm_data = migrations[vm_key]
                
                display_name = vm_data.get('display_name', vm_key)
                os_type = vm_data.get('os_type', 'unknown')
                source = vm_data.get('source', 'unknown')
                added_at = vm_data.get('added_at', 'unknown')
                steps = vm_data.get('steps', {})
                
                print(colored(f"\n{'=' * 50}", Colors.BLUE))
                print(colored(f"   VM: {display_name}", Colors.BOLD))
                print(colored(f"{'=' * 50}", Colors.BLUE))
                print(f"   OS Type:  {os_type}")
                print(f"   Source:   {source}")
                print(f"   Added:    {added_at}")
                print(f"   Progress: {self.get_vm_progress(vm_key)}")
                
                print(colored(f"\n   Steps:", Colors.BOLD))
                for step in self.MIGRATION_STEPS:
                    step_data = steps.get(step, {})
                    done = step_data.get('done', False)
                    date = step_data.get('date', '')
                    date_str = date[:19].replace('T', ' ') if date else '-'
                    status = colored("✓ Done", Colors.GREEN) if done else colored("○ Pending", Colors.YELLOW)
                    label = self.STEP_LABELS.get(step, step)
                    print(f"   {label:<30} {status:<15} {date_str}")
                
                next_step = self.get_next_step(vm_key)
                if next_step != "completed":
                    print(colored(f"\n   → Next: {self.STEP_LABELS.get(next_step, next_step)}", Colors.CYAN))
                else:
                    print(colored(f"\n   ✅ Migration completed!", Colors.GREEN))
            else:
                print(colored("❌ Invalid selection", Colors.RED))
        except ValueError:
            print(colored("❌ Invalid input", Colors.RED))

    def menu_nutanix(self):
        while True:
            self.print_header()
            self.print_menu("NUTANIX", [
                ("1", "List VMs"),
                ("2", "VM details"),
                ("3", "Add VM to migration → Tracker"),
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
                self.tracker_add_vm()
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
                ("12", "Switch VM disk bus (SATA → VirtIO)"),
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
            elif choice == "12":
                self.switch_vm_disk_bus()
                self.pause()
            elif choice == "0":
                break
    
    def switch_vm_disk_bus(self):
        """Switch VM disk bus from SATA to VirtIO."""
        print(colored("\n🔄 Switch VM Disk Bus (SATA → VirtIO)", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        if not self.harvester and not self.connect_harvester():
            return
        
        # List VMs
        vms = self.harvester.list_vms()
        if not vms:
            print(colored("❌ No VMs found in Harvester", Colors.RED))
            return
        
        print("\nHarvester VMs:")
        for i, vm in enumerate(vms, 1):
            name = vm.get('metadata', {}).get('name', 'N/A')
            ns = vm.get('metadata', {}).get('namespace', 'N/A')
            status = vm.get('status', {})
            running = status.get('ready', False)
            state = "🟢 Running" if running else "🔴 Stopped"
            
            # Get current disk bus
            disks = vm.get('spec', {}).get('template', {}).get('spec', {}).get('domain', {}).get('devices', {}).get('disks', [])
            bus_types = set()
            for disk in disks:
                bus = disk.get('disk', {}).get('bus', 'unknown')
                bus_types.add(bus)
            bus_str = '/'.join(bus_types) if bus_types else 'unknown'
            
            print(f"  {i}. {name} ({ns}) - {state} - Bus: {bus_str}")
        
        choice = self.input_prompt("\nSelect VM number")
        if not choice:
            return
        
        try:
            idx = int(choice) - 1
            selected_vm = vms[idx]
            vm_name = selected_vm.get('metadata', {}).get('name')
            namespace = selected_vm.get('metadata', {}).get('namespace')
        except:
            print(colored("Invalid choice", Colors.RED))
            return
        
        # Check current bus types
        spec = selected_vm.get('spec', {}).get('template', {}).get('spec', {})
        disks = spec.get('domain', {}).get('devices', {}).get('disks', [])
        
        print(f"\n📋 Current disk configuration for {vm_name}:")
        needs_change = False
        for i, disk in enumerate(disks):
            name = disk.get('name', f'disk-{i}')
            bus = disk.get('disk', {}).get('bus', 'unknown')
            print(f"   {name}: {bus}")
            if bus in ('sata', 'ide'):
                needs_change = True
        
        if not needs_change:
            print(colored("\n✅ All disks already using VirtIO!", Colors.GREEN))
            return
        
        # Check if VM is running
        vm_status = selected_vm.get('status', {})
        if vm_status.get('ready', False):
            print(colored(f"\n⚠️  VM {vm_name} is running", Colors.YELLOW))
            print(colored("   VM must be stopped to change disk bus", Colors.YELLOW))
            stop = self.input_prompt("Stop VM now? (y/n)")
            if stop.lower() == 'y':
                print("   Stopping VM...")
                try:
                    self.harvester.stop_vm(vm_name, namespace)
                    print(colored("   ✅ Stop command sent. Waiting...", Colors.GREEN))
                    
                    # Wait for VM to stop
                    import time
                    max_wait = 120
                    elapsed = 0
                    while elapsed < max_wait:
                        time.sleep(5)
                        elapsed += 5
                        vm_data = self.harvester.get_vm(vm_name, namespace)
                        if not vm_data.get('status', {}).get('ready', False):
                            print(colored("   ✅ VM stopped", Colors.GREEN))
                            break
                        print(f"   Waiting... ({elapsed}s)")
                    else:
                        print(colored("   ⚠️  VM did not stop in time", Colors.YELLOW))
                        return
                except Exception as e:
                    print(colored(f"   ❌ Error: {e}", Colors.RED))
                    return
            else:
                print("   Cancelled")
                return
        
        # Confirm change
        print(colored("\n🔧 Ready to switch disk bus to VirtIO", Colors.BOLD))
        print(colored("   ⚠️  Make sure Red Hat VirtIO drivers are installed in the guest!", Colors.YELLOW))
        print(colored("   If drivers are not installed, the VM will fail to boot!", Colors.RED))
        
        confirm = self.input_prompt("\nProceed with disk bus change? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        # Patch the VM to change disk bus
        print(colored("\n   Updating VM configuration...", Colors.CYAN))
        
        try:
            # Get fresh VM data
            vm_data = self.harvester.get_vm(vm_name, namespace)
            
            # Modify disk bus in the spec
            new_disks = []
            template_spec = vm_data.get('spec', {}).get('template', {}).get('spec', {})
            for disk in template_spec.get('domain', {}).get('devices', {}).get('disks', []):
                new_disk = disk.copy()
                if 'disk' in new_disk:
                    new_disk['disk'] = new_disk['disk'].copy()
                    old_bus = new_disk['disk'].get('bus', 'sata')
                    if old_bus in ('sata', 'ide', 'scsi'):
                        new_disk['disk']['bus'] = 'virtio'
                        print(f"   {new_disk.get('name')}: {old_bus} → virtio")
                new_disks.append(new_disk)
            
            # Build patch
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "domain": {
                                "devices": {
                                    "disks": new_disks
                                }
                            }
                        }
                    }
                }
            }
            
            # Apply patch via Harvester API
            result = self.harvester._request(
                "PATCH",
                f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{vm_name}",
                json=patch,
                headers={"Content-Type": "application/merge-patch+json"}
            )
            
            print(colored("   ✅ VM configuration updated!", Colors.GREEN))
            
            # Offer to start VM
            start = self.input_prompt("\nStart VM now? (y/n)")
            if start.lower() == 'y':
                print("   Starting VM...")
                self.harvester.start_vm(vm_name, namespace)
                print(colored("   ✅ Start command sent", Colors.GREEN))
                print(colored("\n💡 Monitor VM boot via Harvester console", Colors.YELLOW))
            
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
            import traceback
            traceback.print_exc()
    
    def menu_migration(self):
        while True:
            self.print_header()
            self.print_menu("MIGRATION", [
                ("1", "Select source VM"),
                ("2", "Export VM config (pre-check)"),
                ("3", "Download VM disks (QCOW2)"),
                ("4", "Import disks to Harvester"),
                ("5", "Create VM in Harvester"),
                ("6", "Post-migration Windows"),
                ("7", "Post-migration Linux"),
                ("─", "─" * 30),
                ("8", "Check staging"),
                ("9", "List staging disks"),
                ("10", "Disk image details"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.select_vm()
                self.pause()
            elif choice == "2":
                if not self._selected_vm:
                    print(colored("❌ No VM selected. Use option 1 first.", Colors.RED))
                else:
                    self.windows_precheck()
                self.pause()
            elif choice == "3":
                if not self._selected_vm:
                    print(colored("❌ No VM selected. Use option 1 first.", Colors.RED))
                else:
                    self.export_vm()
                self.pause()
            elif choice == "4":
                if not self._selected_vm:
                    print(colored("❌ No VM selected. Use option 1 first.", Colors.RED))
                else:
                    self.import_disks_to_pvcs()
                self.pause()
            elif choice == "5":
                self.create_harvester_vm()
                self.pause()
            elif choice == "6":
                self.postmig_autoconfigure()
                self.pause()
            elif choice == "7":
                print(colored("\n🚧 Post-migration Linux - Coming soon", Colors.YELLOW))
                self.pause()
            elif choice == "8":
                self.check_staging()
                self.pause()
            elif choice == "9":
                self.list_staging_disks()
                self.pause()
            elif choice == "10":
                self.show_disk_info()
                self.pause()
            elif choice == "0":
                break
    
    # === Windows Tools Menu ===
    
    def menu_windows(self):
        """Windows tools menu."""
        while True:
            self.print_header()
            self.print_menu("WINDOWS TOOLS", [
                ("1", "Check WinRM/Prerequisites"),
                ("2", "Pre-migration check (collect config)"),
                ("3", "View VM config"),
                ("4", "Download virtio/qemu-ga tools"),
                ("5", "Stop services (pre-migration)"),
                ("6", "Start services (post-migration)"),
                ("7", "Generate post-migration script"),
                ("8", "Post-migration auto-configure"),
                ("9", "Vault management"),
                ("10", "Install Red Hat VirtIO drivers"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.check_winrm_prereqs()
                self.pause()
            elif choice == "2":
                self.windows_precheck()
                self.pause()
            elif choice == "3":
                self.view_vm_config()
                self.pause()
            elif choice == "4":
                self.download_tools()
                self.pause()
            elif choice == "5":
                self.stop_windows_services()
                self.pause()
            elif choice == "6":
                self.start_windows_services()
                self.pause()
            elif choice == "7":
                self.generate_postmig_script()
                self.pause()
            elif choice == "8":
                self.postmig_autoconfigure()
                self.pause()
            elif choice == "9":
                self.menu_vault()
            elif choice == "10":
                self.install_virtio_drivers()
                self.pause()
            elif choice == "0":
                break
    
    def install_virtio_drivers(self):
        """Install Red Hat VirtIO drivers on a running Windows VM."""
        print(colored("\n📦 Install Red Hat VirtIO Drivers", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        print(colored("   This will install the proper VirtIO drivers for KVM/Harvester.", Colors.CYAN))
        print(colored("   Required BEFORE switching VM disk bus from SATA to VirtIO!", Colors.YELLOW))
        
        if not WINRM_AVAILABLE:
            print(colored("❌ pywinrm not installed. Run: pip install pywinrm[kerberos]", Colors.RED))
            return
        
        # Check tools exist
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        tools_dir = os.path.join(staging_dir, 'tools')
        virtio_iso = os.path.join(tools_dir, 'virtio-win.iso')
        
        if not os.path.exists(virtio_iso):
            print(colored(f"❌ VirtIO ISO not found: {virtio_iso}", Colors.RED))
            print(colored("   Run 'Download virtio/qemu-ga tools' first (option 4)", Colors.YELLOW))
            return
        
        # Get target host
        windows_config = self.config.get('windows', {})
        domain = windows_config.get('domain', 'AD.WYSSCENTER.CH').lower()
        use_kerberos = windows_config.get('use_kerberos', True)
        
        print(colored("\n   Target Windows VM (must be running)", Colors.CYAN))
        host = self.input_prompt("Windows hostname (FQDN)")
        if not host:
            return
        
        # Add domain suffix if needed
        if '.' not in host:
            host = f"{host}.{domain}"
            print(colored(f"   → Using FQDN: {host}", Colors.CYAN))
        
        # Determine authentication
        username = None
        password = None
        transport = "ntlm"
        
        if use_kerberos and get_kerberos_auth():
            print(colored("   Using Kerberos authentication", Colors.GREEN))
            transport = "kerberos"
        else:
            print("   Using NTLM authentication")
            try:
                username, password = self.vault.get_credential("local-admin")
                print(f"   Using: {username}")
            except:
                username = self.input_prompt("   Username [Administrator]") or "Administrator"
                import getpass
                password = getpass.getpass("   Password: ")
        
        # Connect
        print(colored("\n🔌 Connecting...", Colors.CYAN))
        
        try:
            client = WinRMClient(
                host=host,
                username=username,
                password=password,
                transport=transport
            )
            
            if not client.test_connection():
                print(colored("❌ WinRM connection failed", Colors.RED))
                return
            
            print(colored("   ✅ Connected!", Colors.GREEN))
        except Exception as e:
            print(colored(f"❌ Connection error: {e}", Colors.RED))
            return
        
        # Start HTTP server for file transfer
        print(colored("\n🚀 Starting file transfer server...", Colors.CYAN))
        
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            target_ip = socket.gethostbyname(host.split('.')[0])
            s.connect((target_ip, 5985))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception as e:
            print(colored(f"   ⚠️  Could not auto-detect local IP: {e}", Colors.YELLOW))
            local_ip = self.input_prompt("   Enter this machine's IP (reachable from Windows)")
            if not local_ip:
                return
        
        http_port = 8888
        http_url = f"http://{local_ip}:{http_port}"
        
        import threading
        import http.server
        import socketserver
        
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tools_dir, **kwargs)
            def log_message(self, format, *args):
                pass
        
        httpd = socketserver.TCPServer(("", http_port), QuietHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        print(colored(f"   ✅ Server running at {http_url}", Colors.GREEN))
        
        try:
            # Download ISO to Windows
            print(colored("\n📥 Downloading VirtIO ISO to Windows...", Colors.CYAN))
            
            ps_download = f'''
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$isoPath = "$env:TEMP\\virtio-win.iso"
Invoke-WebRequest -Uri "{http_url}/virtio-win.iso" -OutFile $isoPath -UseBasicParsing
if (Test-Path $isoPath) {{ "DOWNLOADED" }} else {{ "FAILED" }}
'''
            stdout, stderr, rc = client.run_powershell(ps_download, timeout=600)
            
            if "DOWNLOADED" not in stdout:
                print(colored(f"   ❌ Download failed: {stderr}", Colors.RED))
                return
            
            print(colored("   ✅ Downloaded", Colors.GREEN))
            
            # Mount and install
            print(colored("\n📦 Installing VirtIO drivers...", Colors.CYAN))
            
            ps_install = '''
$ErrorActionPreference = "Continue"
$iso = "$env:TEMP\\virtio-win.iso"
$logFile = "C:\\temp\\virtio-install.log"

if (-not (Test-Path "C:\\temp")) {
    New-Item -ItemType Directory -Path "C:\\temp" -Force | Out-Null
}

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts - $msg" | Tee-Object -FilePath $logFile -Append
}

Log "=========================================="
Log "VirtIO Driver Installation Started"
Log "=========================================="

# Mount ISO
Log "Mounting ISO..."
$mount = Mount-DiskImage -ImagePath $iso -PassThru
Start-Sleep 2
$driveLetter = ($mount | Get-Volume).DriveLetter + ":"
Log "Mounted on $driveLetter"

# Find installer
$installers = @(
    "$driveLetter\\virtio-win-guest-tools.exe",
    "$driveLetter\\virtio-win-gt-x64.exe"
)

$installerPath = $null
foreach ($path in $installers) {
    if (Test-Path $path) {
        $installerPath = $path
        Log "Found installer: $path"
        break
    }
}

if (-not $installerPath) {
    Log "ERROR: No installer found"
    Dismount-DiskImage -ImagePath $iso
    Write-Host "INSTALLER_NOT_FOUND"
    exit 1
}

# Run silent install
Log "Running silent installation..."
$proc = Start-Process $installerPath -ArgumentList "/S" -PassThru -Wait
Log "Installer exit code: $($proc.ExitCode)"

# Wait for installation to complete
Start-Sleep 5

# Verify installation
$virtioPath = "$env:ProgramFiles\\Virtio-Win"
$redhatPath = "$env:ProgramFiles\\Red Hat"

if ((Test-Path $virtioPath) -or (Test-Path $redhatPath)) {
    Log "SUCCESS: VirtIO drivers installed"
    Write-Host "INSTALL_SUCCESS"
} else {
    Log "WARNING: Installation may have failed"
    Write-Host "INSTALL_UNKNOWN"
}

# Cleanup
Log "Cleaning up..."
Dismount-DiskImage -ImagePath $iso -ErrorAction SilentlyContinue
Remove-Item $iso -Force -ErrorAction SilentlyContinue

Log "=========================================="
Log "Installation Complete"
Log "=========================================="
'''
            stdout, stderr, rc = client.run_powershell(ps_install, timeout=300)
            
            if "INSTALL_SUCCESS" in stdout:
                print(colored("   ✅ VirtIO drivers installed successfully!", Colors.GREEN))
                print(colored("      Log: C:\\temp\\virtio-install.log", Colors.CYAN))
            elif "INSTALLER_NOT_FOUND" in stdout:
                print(colored("   ❌ VirtIO installer not found in ISO", Colors.RED))
            else:
                print(colored("   ⚠️  Installation status unknown", Colors.YELLOW))
                print(colored("      Check log: C:\\temp\\virtio-install.log", Colors.CYAN))
            
            # Recommend reboot
            print(colored("\n   ⚠️  A reboot is recommended to activate drivers", Colors.YELLOW))
            reboot = self.input_prompt("   Reboot now? (y/n) [n]") or "n"
            if reboot.lower() == 'y':
                print("   🔄 Rebooting...")
                client.run_powershell("Restart-Computer -Force")
                print(colored("   Reboot initiated. Wait for VM to come back.", Colors.GREEN))
            
            print(colored("\n✅ VirtIO drivers installation complete!", Colors.GREEN))
            print(colored("\n💡 Next steps:", Colors.YELLOW))
            print("   1. Reboot the VM if not done")
            print("   2. Menu Harvester → Switch VM disk bus (option 12)")
            print("   3. Start VM and verify boot")
            
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
        finally:
            httpd.shutdown()
    
    def check_winrm_prereqs(self):
        """Check WinRM prerequisites."""
        print(colored("\n🔍 Checking Windows Remote Management Prerequisites", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        # Check pywinrm
        available, msg = check_winrm_available()
        if available:
            print(colored(f"   ✅ {msg}", Colors.GREEN))
        else:
            print(colored(f"   ❌ {msg}", Colors.RED))
            return
        
        # Check Kerberos
        print("\n   Checking Kerberos...")
        if get_kerberos_auth():
            print(colored("   ✅ Valid Kerberos ticket found", Colors.GREEN))
        else:
            print(colored("   ⚠️  No valid Kerberos ticket", Colors.YELLOW))
            print("      Run: kinit your_user@AD.WYSSCENTER.CH")
        
        # Check vault
        print("\n   Checking Vault...")
        try:
            creds = self.vault.list_credentials()
            if creds:
                print(colored(f"   ✅ Vault configured with {len(creds)} credential(s)", Colors.GREEN))
                for c in creds:
                    print(f"      - {c}")
            else:
                print(colored("   ⚠️  Vault empty or not configured", Colors.YELLOW))
        except VaultError as e:
            print(colored(f"   ⚠️  Vault not configured: {e}", Colors.YELLOW))
        
        # Check tools directory
        print("\n   Checking tools...")
        tools_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data') + '/tools'
        if os.path.exists(tools_dir):
            tools = os.listdir(tools_dir)
            if tools:
                print(colored(f"   ✅ Tools directory: {tools_dir}", Colors.GREEN))
                for t in tools:
                    size = os.path.getsize(os.path.join(tools_dir, t)) / (1024*1024)
                    print(f"      - {t} ({size:.1f} MB)")
            else:
                print(colored(f"   ⚠️  Tools directory empty: {tools_dir}", Colors.YELLOW))
        else:
            print(colored(f"   ⚠️  Tools directory not found: {tools_dir}", Colors.YELLOW))
            print("      Run 'Download virtio/qemu-ga tools' to populate")
    
    def _connect_windows(self, prompt_host: bool = True) -> tuple:
        """
        Helper to establish WinRM connection.
        
        Returns:
            Tuple of (client, config, vm_dir) or (None, None, None) on failure
        """
        if not WINRM_AVAILABLE:
            print(colored("❌ pywinrm not installed. Run: pip install pywinrm[kerberos]", Colors.RED))
            return None, None, None
        
        windows_config = self.config.get('windows', {})
        domain = windows_config.get('domain', 'AD.WYSSCENTER.CH').lower()
        use_kerberos = windows_config.get('use_kerberos', True)
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        
        # List available VM configs
        migrations_dir = os.path.join(staging_dir, 'migrations')
        if os.path.exists(migrations_dir):
            configs = []
            for d in os.listdir(migrations_dir):
                config_path = os.path.join(migrations_dir, d, 'vm-config.json')
                if os.path.exists(config_path):
                    configs.append((d, config_path))
            
            if configs:
                print("\n   Available VM configurations:")
                for i, (name, path) in enumerate(configs, 1):
                    print(f"      {i}. {name}")
                
                choice = self.input_prompt("Select VM number (or Enter for manual)")
                if choice:
                    try:
                        idx = int(choice) - 1
                        selected_name, config_path = configs[idx]
                        config = VMConfig.load(config_path)
                        host = f"{config.hostname.lower()}.{domain}"
                        print(f"   → Using: {host}")
                    except (ValueError, IndexError):
                        print(colored("Invalid choice", Colors.RED))
                        return None, None, None
                else:
                    config = None
                    host = self.input_prompt("Windows hostname (FQDN)")
                    if not host:
                        return None, None, None
            else:
                config = None
                host = self.input_prompt("Windows hostname (FQDN)")
                if not host:
                    return None, None, None
        else:
            config = None
            host = self.input_prompt("Windows hostname (FQDN)")
            if not host:
                return None, None, None
        
        # Add domain suffix if needed
        if '.' not in host:
            host = f"{host}.{domain}"
        
        # Determine auth method
        username = None
        password = None
        transport = "kerberos"
        
        if use_kerberos and get_kerberos_auth():
            print(colored("   Using Kerberos authentication", Colors.GREEN))
            transport = "kerberos"
        else:
            print("   Using NTLM authentication")
            transport = "ntlm"
            try:
                username, password = self.vault.get_credential("local-admin")
            except:
                username = self.input_prompt("Username [Administrator]") or "Administrator"
                import getpass
                password = getpass.getpass("Password: ")
        
        # Connect
        print(f"\n   Connecting to {host}...")
        try:
            client = WinRMClient(
                host=host,
                username=username,
                password=password,
                transport=transport
            )
            
            if not client.test_connection():
                print(colored("❌ Connection failed", Colors.RED))
                return None, None, None
            
            print(colored("   ✅ Connected!", Colors.GREEN))
            
            # Get vm_dir from hostname
            hostname = host.split('.')[0].lower()
            vm_dir = os.path.join(staging_dir, 'migrations', hostname)
            
            # Load config if not already loaded
            if not config:
                config_path = os.path.join(vm_dir, 'vm-config.json')
                if os.path.exists(config_path):
                    config = VMConfig.load(config_path)
            
            return client, config, vm_dir
            
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
            return None, None, None
    
    def stop_windows_services(self):
        """Stop listening services before migration."""
        print(colored("\n🛑 Stop Services (Pre-Migration)", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        client, config, vm_dir = self._connect_windows()
        if not client:
            return
        
        if not config or not config.listening_services:
            print(colored("❌ No VM config or no listening services found.", Colors.YELLOW))
            print("   Run pre-migration check first to collect service information.")
            return
        
        # Get unique service names
        service_names = list(set(s.name for s in config.listening_services))
        
        # Categorize services
        dc_service_names = ['NTDS', 'DNS', 'Netlogon', 'Kdc', 'DHCPServer', 'IsmServ', 'DFSR', 'NtFrs', 'W32Time']
        
        dc_services = []
        app_services = []
        
        for name in service_names:
            svc = next((s for s in config.listening_services if s.name == name), None)
            if name in dc_service_names:
                dc_services.append((name, svc.display_name if svc else name))
            else:
                app_services.append((name, svc.display_name if svc else name))
        
        # Display services by category
        if dc_services:
            print(colored("\n   ⚠️  DOMAIN CONTROLLER SERVICES (critical):", Colors.YELLOW))
            for name, display_name in dc_services:
                print(colored(f"      • {display_name} ({name})", Colors.YELLOW))
            print(colored("\n   ⚠️  WARNING: This is a Domain Controller!", Colors.RED))
            print(colored("   Stopping these services will affect AD authentication and DNS.", Colors.RED))
        
        if app_services:
            print(colored("\n   📦 APPLICATION SERVICES:", Colors.CYAN))
            for name, display_name in app_services:
                print(f"      • {display_name} ({name})")
        
        print(f"\n   Total: {len(service_names)} service(s) to stop")
        
        # Confirmation
        if dc_services:
            confirm = self.input_prompt("\n   ⚠️  Type 'STOP DC' to confirm stopping Domain Controller services")
            if confirm != 'STOP DC':
                print("   Cancelled - DC services require explicit confirmation")
                return
        else:
            confirm = self.input_prompt("\n   Stop these services? (y/n)")
            if confirm.lower() != 'y':
                print("   Cancelled")
                return
        
        # Stop services in order: applications first, then DC services
        all_stopped = []
        all_failed = []
        
        if app_services:
            print("\n   🛑 Stopping application services...")
            app_names = [name for name, _ in app_services]
            results = client.stop_services(app_names)
            for name, success in results.items():
                if success:
                    print(colored(f"      ✅ Stopped: {name}", Colors.GREEN))
                    all_stopped.append(name)
                else:
                    print(colored(f"      ❌ Failed: {name}", Colors.RED))
                    all_failed.append(name)
        
        if dc_services:
            print(colored("\n   🛑 Stopping Domain Controller services...", Colors.YELLOW))
            # Stop DC services in specific order for clean shutdown
            dc_stop_order = ['DHCPServer', 'DNS', 'Netlogon', 'Kdc', 'DFSR', 'NtFrs', 'IsmServ', 'NTDS', 'W32Time']
            dc_names = [name for name, _ in dc_services]
            # Sort by stop order
            dc_names_sorted = sorted(dc_names, key=lambda x: dc_stop_order.index(x) if x in dc_stop_order else 999)
            
            results = client.stop_services(dc_names_sorted)
            for name, success in results.items():
                if success:
                    print(colored(f"      ✅ Stopped: {name}", Colors.GREEN))
                    all_stopped.append(name)
                else:
                    print(colored(f"      ❌ Failed: {name}", Colors.RED))
                    all_failed.append(name)
        
        # Save stopped services list for later restart
        if all_stopped:
            stopped_file = os.path.join(vm_dir, 'stopped-services.json')
            os.makedirs(vm_dir, exist_ok=True)
            with open(stopped_file, 'w') as f:
                json.dump({
                    'stopped_services': all_stopped,
                    'dc_services': [name for name, _ in dc_services],
                    'app_services': [name for name, _ in app_services],
                    'stopped_at': datetime.utcnow().isoformat()
                }, f, indent=2)
            print(colored(f"\n   💾 Saved stopped services list: {stopped_file}", Colors.GREEN))
        
        if all_failed:
            print(colored(f"\n   ⚠️  {len(all_failed)} service(s) failed to stop", Colors.YELLOW))
        else:
            print(colored(f"\n   ✅ All {len(all_stopped)} services stopped successfully!", Colors.GREEN))
            print(colored("   VM is ready for shutdown and migration.", Colors.CYAN))
    
    def start_windows_services(self):
        """Start services after migration."""
        print(colored("\n▶️  Start Services (Post-Migration)", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        client, config, vm_dir = self._connect_windows()
        if not client:
            return
        
        # Load stopped services list
        stopped_file = os.path.join(vm_dir, 'stopped-services.json')
        dc_services = []
        app_services = []
        
        if not os.path.exists(stopped_file):
            print(colored("❌ No stopped services file found.", Colors.YELLOW))
            print("   Either services were not stopped, or file was deleted.")
            
            # Offer to use config's listening_services
            if config and config.listening_services:
                use_config = self.input_prompt("   Use services from VM config? (y/n)")
                if use_config.lower() == 'y':
                    service_names = list(set(s.name for s in config.listening_services))
                    # Categorize
                    dc_service_names = ['NTDS', 'DNS', 'Netlogon', 'Kdc', 'DHCPServer', 'IsmServ', 'DFSR', 'NtFrs', 'W32Time']
                    dc_services = [n for n in service_names if n in dc_service_names]
                    app_services = [n for n in service_names if n not in dc_service_names]
                else:
                    return
            else:
                return
        else:
            with open(stopped_file, 'r') as f:
                data = json.load(f)
            dc_services = data.get('dc_services', [])
            app_services = data.get('app_services', [])
            stopped_at = data.get('stopped_at', 'unknown')
            print(f"   📋 Services stopped at: {stopped_at}")
        
        total_services = len(dc_services) + len(app_services)
        if total_services == 0:
            print(colored("   No services to start.", Colors.YELLOW))
            return
        
        # Display services by category
        if dc_services:
            print(colored("\n   ⚠️  DOMAIN CONTROLLER SERVICES:", Colors.YELLOW))
            for name in dc_services:
                print(colored(f"      • {name}", Colors.YELLOW))
        
        if app_services:
            print(colored("\n   📦 APPLICATION SERVICES:", Colors.CYAN))
            for name in app_services:
                print(f"      • {name}")
        
        print(f"\n   Total: {total_services} service(s) to start")
        
        confirm = self.input_prompt("\n   Start these services? (y/n)")
        if confirm.lower() != 'y':
            print("   Cancelled")
            return
        
        all_started = []
        all_failed = []
        
        # Start DC services FIRST (reverse order of shutdown)
        if dc_services:
            print(colored("\n   ▶️  Starting Domain Controller services...", Colors.YELLOW))
            # Start DC services in specific order for clean startup
            dc_start_order = ['W32Time', 'NTDS', 'IsmServ', 'NtFrs', 'DFSR', 'Kdc', 'Netlogon', 'DNS', 'DHCPServer']
            dc_services_sorted = sorted(dc_services, key=lambda x: dc_start_order.index(x) if x in dc_start_order else 999)
            
            results = client.start_services(dc_services_sorted)
            for name, success in results.items():
                if success:
                    print(colored(f"      ✅ Started: {name}", Colors.GREEN))
                    all_started.append(name)
                else:
                    print(colored(f"      ❌ Failed: {name}", Colors.RED))
                    all_failed.append(name)
        
        # Then start application services
        if app_services:
            print("\n   ▶️  Starting application services...")
            results = client.start_services(app_services)
            for name, success in results.items():
                if success:
                    print(colored(f"      ✅ Started: {name}", Colors.GREEN))
                    all_started.append(name)
                else:
                    print(colored(f"      ❌ Failed: {name}", Colors.RED))
                    all_failed.append(name)
        
        if all_failed:
            print(colored(f"\n   ⚠️  {len(all_failed)} service(s) failed to start", Colors.YELLOW))
            print("   Check Windows Event Log for details.")
        else:
            print(colored(f"\n   ✅ All {len(all_started)} services started successfully!", Colors.GREEN))
            
            # Clean up stopped services file
            if os.path.exists(stopped_file):
                os.remove(stopped_file)
                print(colored("   🗑️  Cleaned up stopped-services.json", Colors.CYAN))
    
    def windows_precheck(self):
        """Run pre-migration check on Windows VM."""
        print(colored("\n🔍 Windows Pre-Migration Check", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        if not WINRM_AVAILABLE:
            print(colored("❌ pywinrm not installed. Run: pip install pywinrm[kerberos]", Colors.RED))
            return
        
        # Require a selected VM
        if not self._selected_vm:
            print(colored("❌ No VM selected.", Colors.RED))
            print(colored("   Use 'Migration Tracker → Add VM' then 'Migration → Select VM' first.", Colors.YELLOW))
            return
        
        # Check if step already done
        if not self.check_step_and_confirm(self._selected_vm, 'precheck'):
            return
        
        # Build default FQDN from selected VM
        windows_config = self.config.get('windows', {})
        domain = windows_config.get('domain', 'AD.WYSSCENTER.CH').lower()
        use_kerberos = windows_config.get('use_kerberos', True)
        
        default_fqdn = f"{self._selected_vm}.{domain}"
        
        print(f"   Selected VM: {self._selected_vm}")
        print(colored("   ℹ️  Using Kerberos authentication (FQDN required)", Colors.CYAN))
        
        host = self.input_prompt(f"Windows hostname (FQDN) [{default_fqdn}]") or default_fqdn
        
        # Check if IP address was provided
        import re
        is_ip = bool(re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', host))
        
        if is_ip and use_kerberos:
            print(colored("\n   ⚠️  IP address detected but Kerberos requires hostname (FQDN)", Colors.YELLOW))
            print(colored("   Kerberos uses Service Principal Names based on DNS names, not IPs.", Colors.YELLOW))
            host = self.input_prompt(f"Enter FQDN (e.g., servername.{domain})")
            if not host:
                return
            is_ip = bool(re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', host))
            if is_ip:
                print(colored("❌ FQDN required for Kerberos authentication", Colors.RED))
                return
        
        # Add domain suffix if not present (for short hostnames)
        if not is_ip and '.' not in host:
            host = f"{host}.{domain}"
            print(colored(f"   → Using FQDN: {host}", Colors.CYAN))
        
        # Determine authentication method
        username = None
        password = None
        transport = "kerberos"
        
        if use_kerberos and get_kerberos_auth():
            print(colored("   Using Kerberos authentication", Colors.GREEN))
            transport = "kerberos"
        else:
            print("   Using NTLM authentication")
            transport = "ntlm"
            try:
                username, password = self.vault.get_credential("local-admin")
                print(f"   Username: {username}")
            except VaultError:
                username = self.input_prompt("Username [Administrator]") or "Administrator"
                import getpass
                password = getpass.getpass("Password: ")
        
        # Connect
        print(f"\n   Connecting to {host}...")
        try:
            client = WinRMClient(
                host=host,
                username=username,
                password=password,
                transport=transport
            )
            
            if not client.test_connection():
                print(colored("❌ Connection failed", Colors.RED))
                if transport == "kerberos":
                    print(colored("   💡 Tips:", Colors.YELLOW))
                    print(colored("      - Verify FQDN is correct and resolvable", Colors.YELLOW))
                    print(colored("      - Check Kerberos ticket: klist", Colors.YELLOW))
                    print(colored("      - Renew ticket if expired: kinit user@AD.WYSSCENTER.CH", Colors.YELLOW))
                return
            
            print(colored("   ✅ Connected!", Colors.GREEN))
            
            # Run pre-check
            print("\n   Running pre-migration checks...")
            checker = WindowsPreCheck(client)
            config = checker.run_full_check()
            
            # Display results
            print(colored("\n📋 SYSTEM INFORMATION", Colors.BOLD))
            print(f"   Hostname: {config.hostname}")
            print(f"   OS: {config.os_name}")
            print(f"   Version: {config.os_version}")
            print(f"   Architecture: {config.architecture}")
            print(f"   Domain: {config.domain}")
            print(f"   Domain Joined: {config.domain_joined}")
            
            print(colored("\n🌐 NETWORK CONFIGURATION", Colors.BOLD))
            for nic in config.network_interfaces:
                print(f"   Interface: {nic.name}")
                print(f"      MAC: {nic.mac}")
                print(f"      DHCP: {nic.dhcp}")
                if not nic.dhcp:
                    print(f"      IP: {nic.ip}/{nic.prefix}")
                    print(f"      Gateway: {nic.gateway}")
                    print(f"      DNS: {', '.join(nic.dns or [])}")
            
            print(colored("\n💾 STORAGE", Colors.BOLD))
            for disk in config.disks:
                print(f"   Disk {disk.number}: {disk.size_gb} GB")
                for part in disk.partitions:
                    letter = part.get('Letter')
                    label = part.get('Label') or ''
                    size = part.get('SizeGB', 0)
                    # Skip tiny system partitions without drive letter
                    if not letter and size < 1:
                        continue
                    # Format display
                    if letter:
                        if label:
                            print(f"      {letter}: {label} ({size} GB)")
                        else:
                            print(f"      {letter}: ({size} GB)")
                    else:
                        # Partition without letter (recovery, reserved, etc.)
                        part_type = label if label else "System"
                        print(f"      [{part_type}] ({size} GB)")
            
            print(colored("\n⚙️  SERVICES", Colors.BOLD))
            print(f"   WinRM: {'✅' if config.winrm_enabled else '❌'}")
            print(f"   RDP: {'✅' if config.rdp_enabled else '❌'}")
            
            # Display Nutanix tools (for post-migration cleanup planning)
            # Only list MAIN programs visible in Programs and Features, not sub-components
            print(colored("\n🔧 NUTANIX TOOLS INSTALLED (to remove post-migration)", Colors.BOLD))
            
            # These are the ONLY main programs that appear in Programs and Features
            # Everything else (Infrastructure Components, VSS Modules, Guest Agent, etc.)
            # are sub-components that get removed automatically
            main_nutanix_programs = [
                "Nutanix Guest Tools",
                "Nutanix VirtIO", 
                "Nutanix VM Mobility"
            ]
            
            # Sub-components to EXCLUDE (not visible in Programs and Features)
            exclude_patterns = [
                "Infrastructure Components",
                "VSS Modules",
                "Guest Agent",
                "Checks & Prereqs",
                "Self Service Restore"
            ]
            
            # Check for Nutanix software via registry - only main programs
            ps_nutanix_check = '''
Get-ItemProperty HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*,
                 HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like "*Nutanix*" } |
    Select-Object DisplayName, DisplayVersion |
    ForEach-Object { "$($_.DisplayName)|$($_.DisplayVersion)" }
'''
            nutanix_tools = []
            stdout, _, _ = client.run_powershell(ps_nutanix_check)
            if stdout.strip():
                for line in stdout.strip().split('\n'):
                    if '|' in line:
                        name, version = line.split('|', 1)
                        name = name.strip()
                        version = version.strip()
                        
                        # Exclude sub-components
                        is_subcomponent = False
                        for pattern in exclude_patterns:
                            if pattern in name:
                                is_subcomponent = True
                                break
                        
                        if is_subcomponent:
                            continue
                        
                        # Only include if it's a main program
                        is_main_program = False
                        for main_prog in main_nutanix_programs:
                            if name.startswith(main_prog):
                                is_main_program = True
                                break
                        
                        if is_main_program:
                            tool_str = f"{name} {version}".strip() if version else name
                            if tool_str not in nutanix_tools:
                                nutanix_tools.append(tool_str)
            
            if nutanix_tools:
                for tool in nutanix_tools:
                    print(f"   • {tool}")
                print(colored(f"\n   ℹ️  {len(nutanix_tools)} Nutanix program(s) to remove after migration", Colors.CYAN))
            else:
                print("   No Nutanix programs detected")
            
            # QEMU Guest Agent status - the only thing we need to install
            print(colored("\n📡 QEMU GUEST AGENT (required for Harvester)", Colors.BOLD))
            agents = config.agents
            if agents.qemu_guest_agent:
                print(f"   Status: ✅ Installed")
                print(f"   Running: {'✅' if agents.qemu_guest_agent_running else '⚠️  NOT RUNNING'}")
                print(f"   Auto-start: {'✅' if agents.qemu_guest_agent_autostart else '⚠️  NOT AUTO'}")
            else:
                print(f"   Status: ❌ NOT INSTALLED")
            
            # Display listening services
            if config.listening_services:
                print(colored("\n🔌 SERVICES TO STOP BEFORE MIGRATION", Colors.BOLD))
                
                # Group by service name to avoid duplicates
                unique_services = {}
                for svc in config.listening_services:
                    if svc.name not in unique_services:
                        unique_services[svc.name] = {
                            'display_name': svc.display_name,
                            'ports': []
                        }
                    unique_services[svc.name]['ports'].append(f"{svc.protocol}/{svc.local_port}")
                
                # Categorize services
                dc_services = ['NTDS', 'DNS', 'Netlogon', 'Kdc', 'DHCPServer', 'IsmServ', 'DFSR', 'NtFrs', 'W32Time']
                
                dc_found = {}
                other_found = {}
                
                for name, info in unique_services.items():
                    if name in dc_services:
                        dc_found[name] = info
                    else:
                        other_found[name] = info
                
                # Display DC services first (critical)
                if dc_found:
                    print(colored("\n   ⚠️  DOMAIN CONTROLLER SERVICES (critical):", Colors.YELLOW))
                    for name, info in dc_found.items():
                        ports_str = ', '.join(info['ports'])
                        print(colored(f"   • {info['display_name']}", Colors.YELLOW))
                        print(f"     Service: {name} | Ports: {ports_str}")
                
                # Display other services
                if other_found:
                    print(colored("\n   📦 APPLICATION SERVICES:", Colors.CYAN))
                    for name, info in other_found.items():
                        ports_str = ', '.join(info['ports'])
                        print(f"   • {info['display_name']}")
                        print(f"     Service: {name} | Ports: {ports_str}")
                
                print(colored(f"\n   ℹ️  {len(unique_services)} service(s) to stop before migration", Colors.CYAN))
                if dc_found:
                    print(colored("   ⚠️  This is a Domain Controller - stopping AD services is REQUIRED!", Colors.YELLOW))
                print(colored("   Services will be restarted after network reconfiguration on Harvester", Colors.CYAN))
            else:
                print(colored("\n🔌 LISTENING SERVICES", Colors.BOLD))
                print("   No application services listening (only WinRM/RDP)")
            
            # Migration readiness - QEMU Guest Agent will be installed post-migration
            # VirtIO drivers will also be installed post-migration (Nutanix VirtIO is incompatible)
            print(colored("\n📊 PRE-MIGRATION READINESS", Colors.BOLD))
            
            qemu_ga_installed = config.agents.qemu_guest_agent
            
            print(colored("   ✅ VM is ready for migration!", Colors.GREEN))
            print(colored("   ℹ️  Post-migration steps (automatic):", Colors.CYAN))
            print(colored("      1. Uninstall Nutanix tools", Colors.CYAN))
            print(colored("      2. Install Red Hat VirtIO drivers", Colors.CYAN))
            if not qemu_ga_installed:
                print(colored("      3. Install QEMU Guest Agent", Colors.CYAN))
            print(colored("      4. Configure network", Colors.CYAN))
            
            # Mark as ready for migration (no QEMU GA required pre-migration)
            config.migration_ready = True
            
            # Save config
            staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
            vm_dir = os.path.join(staging_dir, 'migrations', config.hostname.lower())
            os.makedirs(vm_dir, exist_ok=True)
            config_path = os.path.join(vm_dir, 'vm-config.json')
            config.save(config_path)
            
            # Add Nutanix VM info (boot_type, cpu, ram) to the saved config
            # Auto-connect to Nutanix if not already connected
            if not self.nutanix:
                print(colored("\n   🔗 Connecting to Nutanix to get VM boot type...", Colors.CYAN))
                try:
                    nutanix_config = self.config.get('nutanix', {})
                    if nutanix_config.get('prism_central'):
                        self.nutanix = NutanixClient(
                            host=nutanix_config['prism_central'],
                            username=nutanix_config.get('username'),
                            password=nutanix_config.get('password'),
                            verify_ssl=nutanix_config.get('verify_ssl', False)
                        )
                        print(colored("   ✅ Connected to Nutanix", Colors.GREEN))
                except Exception as e:
                    print(colored(f"   ⚠️  Could not connect to Nutanix: {e}", Colors.YELLOW))
            
            if self.nutanix:
                try:
                    nutanix_vm = self.nutanix.get_vm_by_name(config.hostname)
                    if nutanix_vm:
                        vm_info = NutanixClient.parse_vm_info(nutanix_vm)
                        
                        # Reload and enhance the config with Nutanix data
                        with open(config_path, 'r') as f:
                            saved_config = json.load(f)
                        
                        saved_config['nutanix'] = {
                            'boot_type': vm_info.get('boot_type', 'BIOS'),
                            'cpu_cores': vm_info.get('vcpu', 2),
                            'memory_mb': vm_info.get('memory_mb', 4096),
                            'num_sockets': vm_info.get('num_sockets', 1),
                            'num_vcpus_per_socket': vm_info.get('num_vcpus_per_socket', 2)
                        }
                        
                        with open(config_path, 'w') as f:
                            json.dump(saved_config, f, indent=2)
                        
                        print(colored(f"   ✅ Added Nutanix info: Boot={vm_info.get('boot_type')}, CPU={vm_info.get('vcpu')}, RAM={vm_info.get('memory_mb')//1024}GB", Colors.GREEN))
                    else:
                        print(colored(f"   ⚠️  VM '{config.hostname}' not found in Nutanix", Colors.YELLOW))
                except Exception as e:
                    print(colored(f"   ⚠️  Could not get Nutanix VM info: {e}", Colors.YELLOW))
            else:
                print(colored("   ⚠️  Nutanix not configured - boot_type will need to be set manually", Colors.YELLOW))
            
            print(colored(f"\n   💾 Configuration saved: {config_path}", Colors.GREEN))
            
            # Update tracker
            if self._selected_vm:
                self.update_step(self._selected_vm, 'precheck')
                print(colored(f"   ✅ Step 'precheck' marked complete in tracker", Colors.GREEN))
            
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    def _install_qemu_guest_agent(self, client, host):
        """Install only QEMU Guest Agent on Windows VM via WinRM."""
        self.init_actions()
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        tools_dir = os.path.join(staging_dir, 'tools')
        qemu_ga_msi = os.path.join(tools_dir, 'qemu-ga-x86_64.msi')
        
        if not os.path.exists(qemu_ga_msi):
            print(colored(f"   ❌ QEMU GA MSI not found: {qemu_ga_msi}", Colors.RED))
            print(colored("      Run 'Download virtio/qemu-ga tools' first (Menu Windows → 4)", Colors.YELLOW))
            return False
        
        # Start HTTP server to serve files
        print(colored("\n   🚀 Starting HTTP server for file transfer...", Colors.CYAN))
        
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                target_ip = socket.gethostbyname(host)
            except:
                target_ip = socket.gethostbyname(host.split('.')[0])
            s.connect((target_ip, 5985))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception as e:
            print(colored(f"   ⚠️  Could not auto-detect local IP: {e}", Colors.YELLOW))
            local_ip = self.input_prompt("   Enter this machine's IP (reachable from Windows)")
            if not local_ip:
                return False
        
        http_port = 8888
        http_url = f"http://{local_ip}:{http_port}"
        
        import threading
        import http.server
        import socketserver
        
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tools_dir, **kwargs)
            def log_message(self, format, *args):
                pass
        
        httpd = socketserver.TCPServer(("", http_port), QuietHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        print(colored(f"   ✅ HTTP server running at {http_url}", Colors.GREEN))
        
        try:
            print(colored("\n   📦 Installing QEMU Guest Agent...", Colors.CYAN))
            
            ps_script = f'''
$ErrorActionPreference = "Stop"
$msiUrl = "{http_url}/qemu-ga-x86_64.msi"
$msiPath = "$env:TEMP\\qemu-ga-x86_64.msi"

# Download MSI
Write-Host "Downloading QEMU Guest Agent..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing

# Install silently
Write-Host "Installing QEMU Guest Agent..."
$process = Start-Process msiexec.exe -ArgumentList "/i `"$msiPath`" /qn /norestart" -Wait -PassThru -NoNewWindow
if ($process.ExitCode -eq 0 -or $process.ExitCode -eq 3010) {{
    Write-Host "QEMU Guest Agent installed successfully"
    # Start the service
    Start-Service -Name "QEMU-GA" -ErrorAction SilentlyContinue
    # Set to auto-start
    Set-Service -Name "QEMU-GA" -StartupType Automatic -ErrorAction SilentlyContinue
    Write-Host "INSTALL_SUCCESS"
}} else {{
    Write-Host "Installation failed with exit code: $($process.ExitCode)"
    Write-Host "INSTALL_FAILED"
}}

# Cleanup
Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
'''
            stdout, stderr, rc = client.run_powershell(ps_script, timeout=120)
            
            if "INSTALL_SUCCESS" in stdout:
                print(colored("   ✅ QEMU Guest Agent installed successfully", Colors.GREEN))
                return True
            else:
                print(colored(f"   ❌ Installation failed (exit code: {rc})", Colors.RED))
                if stderr.strip():
                    print(f"      {stderr.strip()}")
                return False
                
        except Exception as e:
            print(colored(f"   ❌ Error: {e}", Colors.RED))
            return False
        finally:
            httpd.shutdown()
    
    def _install_windows_prerequisites(self, client, config, host, username, password, transport):
        """Install missing prerequisites on Windows VM via WinRM."""
        self.init_actions()
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        tools_dir = os.path.join(staging_dir, 'tools')
        
        # Check what needs to be installed
        install_qemu_ga = not config.agents.qemu_guest_agent
        install_virtio = not config.agents.virtio_fedora or not config.agents.virtio_serial
        
        # Verify tools exist
        qemu_ga_msi = os.path.join(tools_dir, 'qemu-ga-x86_64.msi')
        virtio_iso = os.path.join(tools_dir, 'virtio-win.iso')
        
        if install_qemu_ga and not os.path.exists(qemu_ga_msi):
            print(colored(f"   ❌ QEMU GA MSI not found: {qemu_ga_msi}", Colors.RED))
            print(colored("      Run 'Download virtio/qemu-ga tools' first", Colors.YELLOW))
            return
        
        if install_virtio and not os.path.exists(virtio_iso):
            print(colored(f"   ❌ VirtIO ISO not found: {virtio_iso}", Colors.RED))
            print(colored("      Run 'Download virtio/qemu-ga tools' first", Colors.YELLOW))
            return
        
        # Start HTTP server to serve files
        print(colored("\n   🚀 Starting HTTP server for file transfer...", Colors.CYAN))
        
        # Get local IP that the Windows server can reach
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                target_ip = socket.gethostbyname(host)
            except:
                target_ip = socket.gethostbyname(host.split('.')[0])
            s.connect((target_ip, 5985))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception as e:
            print(colored(f"   ⚠️  Could not auto-detect local IP: {e}", Colors.YELLOW))
            local_ip = self.input_prompt("   Enter this machine's IP (reachable from Windows)")
            if not local_ip:
                print(colored("   ❌ Cancelled", Colors.RED))
                return
        
        http_port = 8888
        http_url = f"http://{local_ip}:{http_port}"
        
        print(f"   Local IP: {local_ip}")
        
        # Start HTTP server in tools directory
        import threading
        import http.server
        import socketserver
        
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tools_dir, **kwargs)
            def log_message(self, format, *args):
                pass  # Suppress logging
        
        httpd = socketserver.TCPServer(("", http_port), QuietHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        print(colored(f"   ✅ HTTP server running at {http_url}", Colors.GREEN))
        
        try:
            # Install VirtIO drivers FIRST (before QEMU GA, as GA needs serial driver)
            if install_virtio:
                print(colored("\n   📦 Installing VirtIO drivers...", Colors.CYAN))
                
                # Step 1: Download ISO
                print(colored("      Downloading ISO...", Colors.CYAN))
                ps_download = f'''
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri "{http_url}/virtio-win.iso" -OutFile "$env:TEMP\\virtio-win.iso" -UseBasicParsing
Write-Host "OK"
'''
                stdout, stderr, rc = client.run_powershell(ps_download, timeout=600)
                if rc != 0:
                    print(colored(f"   ❌ Download failed: {stderr}", Colors.RED))
                else:
                    print(colored("      ✅ Downloaded", Colors.GREEN))
                    
                    # Step 2: Mount and start installer
                    print(colored("      Mounting and installing...", Colors.CYAN))
                    ps_install = '''
$iso = "$env:TEMP\\virtio-win.iso"
$m = Mount-DiskImage -ImagePath $iso -PassThru
Start-Sleep 2
$d = ($m | Get-Volume).DriveLetter + ":"
Write-Host "Mounted on $d"

$exe = "$d\\virtio-win-guest-tools.exe"
if (-not (Test-Path $exe)) { $exe = "$d\\virtio-win-gt-x64.exe" }

if (Test-Path $exe) {
    Write-Host "Found: $exe"
    Start-Process $exe -ArgumentList "/S"
    Start-Sleep 2
    $proc = Get-Process -Name "virtio-win*" -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "STARTED:$($proc.Id)"
    } else {
        Write-Host "STARTED:0"
    }
} else {
    Write-Host "NOTFOUND"
}
'''
                    stdout, stderr, rc = client.run_powershell(ps_install)
                    if stdout:
                        for line in stdout.strip().split('\n'):
                            print(f"      {line}")
                    
                    if "NOTFOUND" in stdout:
                        print(colored("   ❌ VirtIO installer not found in ISO", Colors.RED))
                    elif "STARTED" in stdout:
                        # Wait for process to finish
                        print(colored("      Waiting for installer to complete...", Colors.CYAN))
                        max_wait = 120
                        elapsed = 0
                        
                        while elapsed < max_wait:
                            time.sleep(5)
                            elapsed += 5
                            
                            # Check if process still running
                            check = 'if (Get-Process -Name "virtio-win*" -ErrorAction SilentlyContinue) { "RUNNING" } else { "DONE" }'
                            stdout2, _, _ = client.run_powershell(check)
                            
                            if "DONE" in stdout2:
                                break
                            print(f"      Still installing... ({elapsed}s)")
                        
                        # Verify installation
                        verify = 'if ((Test-Path "$env:ProgramFiles\\Red Hat") -or (Test-Path "$env:ProgramFiles\\Virtio-Win\\Vioscsi")) { "SUCCESS" } else { "FAILED" }'
                        stdout3, _, _ = client.run_powershell(verify)
                        
                        if "SUCCESS" in stdout3:
                            print(colored("   ✅ VirtIO drivers installed", Colors.GREEN))
                        else:
                            print(colored("   ⚠️  Installation may have failed, check manually", Colors.YELLOW))
                        
                        # Cleanup
                        cleanup = 'Dismount-DiskImage -ImagePath "$env:TEMP\\virtio-win.iso" -ErrorAction SilentlyContinue; Remove-Item "$env:TEMP\\virtio-win.iso" -Force -ErrorAction SilentlyContinue'
                        client.run_powershell(cleanup)
            
            # Install QEMU Guest Agent
            if install_qemu_ga:
                print(colored("\n   📦 Installing QEMU Guest Agent...", Colors.CYAN))
                
                ps_script = f'''
$ErrorActionPreference = "Stop"
$msiUrl = "{http_url}/qemu-ga-x86_64.msi"
$msiPath = "$env:TEMP\\qemu-ga-x86_64.msi"

# Download MSI
Write-Host "Downloading QEMU Guest Agent..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing

# Install silently
Write-Host "Installing QEMU Guest Agent..."
$process = Start-Process msiexec.exe -ArgumentList "/i `"$msiPath`" /qn /norestart" -Wait -PassThru -NoNewWindow
if ($process.ExitCode -eq 0 -or $process.ExitCode -eq 3010) {{
    Write-Host "QEMU Guest Agent installed successfully"
    # Start the service
    Start-Service -Name "QEMU-GA" -ErrorAction SilentlyContinue
    # Set to auto-start
    Set-Service -Name "QEMU-GA" -StartupType Automatic -ErrorAction SilentlyContinue
}} else {{
    Write-Host "Installation failed with exit code: $($process.ExitCode)"
}}

# Cleanup
Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
'''
                stdout, stderr, rc = client.run_powershell(ps_script)
                if rc == 0:
                    print(colored("   ✅ QEMU Guest Agent installed", Colors.GREEN))
                    if stdout.strip():
                        for line in stdout.strip().split('\n'):
                            print(f"      {line}")
                else:
                    print(colored(f"   ❌ Installation failed (exit code: {rc})", Colors.RED))
                    if stderr.strip():
                        print(f"      {stderr.strip()}")
            
            # Recommend reboot
            print(colored("\n   ⚠️  A reboot is recommended to activate all drivers", Colors.YELLOW))
            reboot = self.input_prompt("   Reboot now? (y/n)")
            if reboot.lower() == 'y':
                print("   🔄 Rebooting VM...")
                client.run_powershell("Restart-Computer -Force")
                
                # Wait for VM to come back
                print(colored("\n   ⏳ Waiting for VM to restart...", Colors.CYAN))
                time.sleep(10)  # Initial wait for shutdown
                
                max_attempts = 12  # 12 * 30s = 6 minutes max
                reconnected = False
                
                for attempt in range(1, max_attempts + 1):
                    print(f"   🔍 Checking connection... (attempt {attempt}/{max_attempts})")
                    try:
                        new_client = WinRMClient(
                            host=host,
                            username=username,
                            password=password,
                            transport=transport
                        )
                        if new_client.test_connection():
                            print(colored("   ✅ VM is back online!", Colors.GREEN))
                            client = new_client
                            reconnected = True
                            break
                        else:
                            print(f"      WinRM not ready yet...")
                    except Exception as e:
                        print(f"      Connection error: {str(e)[:50]}...")
                    
                    if attempt < max_attempts:
                        print(f"      Waiting 30 seconds...")
                        time.sleep(30)
                
                if reconnected:
                    # Re-run verification
                    print(colored("\n   📋 Re-checking prerequisites after reboot...", Colors.CYAN))
                    time.sleep(5)  # Short delay for services to stabilize
                    
                    checker = WindowsPreCheck(client)
                    new_config = checker.run_full_check()
                    
                    # Show updated status
                    print(colored("\n   🔧 UPDATED DRIVERS & AGENTS STATUS", Colors.BOLD))
                    agents = new_config.agents
                    print(f"      VirtIO Network:  {'✅' if agents.virtio_net else '❌'}")
                    print(f"      VirtIO Storage:  {'✅' if agents.virtio_storage else '❌'}")
                    print(f"      VirtIO Serial:   {'✅' if agents.virtio_serial else '❌'}")
                    print(f"      VirtIO Balloon:  {'✅' if agents.virtio_balloon else '⚪ (optional)'}")
                    print(f"      QEMU Guest Agent: {'✅' if agents.qemu_guest_agent else '❌'}")
                    
                    if new_config.migration_ready:
                        print(colored("\n   🎉 VM is now READY for migration!", Colors.GREEN))
                    else:
                        print(colored("\n   ⚠️  Still missing prerequisites:", Colors.YELLOW))
                        for prereq in new_config.missing_prerequisites:
                            print(f"      - {prereq}")
                    
                    # Update saved config
                    staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
                    vm_dir = os.path.join(staging_dir, 'migrations', new_config.hostname.lower())
                    os.makedirs(vm_dir, exist_ok=True)
                    config_path = os.path.join(vm_dir, 'vm-config.json')
                    new_config.save(config_path)
                    print(colored(f"\n   💾 Configuration updated: {config_path}", Colors.GREEN))
                else:
                    print(colored("   ❌ Could not reconnect to VM after reboot", Colors.RED))
                    print(colored("      Re-run pre-migration check manually", Colors.YELLOW))
        
        finally:
            # Stop HTTP server
            httpd.shutdown()
            print(colored("\n   ✅ HTTP server stopped", Colors.GREEN))
    
    def view_vm_config(self):
        """View saved VM configuration."""
        print(colored("\n📋 View VM Configuration", Colors.BOLD))
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        migrations_dir = os.path.join(staging_dir, 'migrations')
        
        if not os.path.exists(migrations_dir):
            print(colored("❌ No migrations directory found", Colors.YELLOW))
            return
        
        # List available configs
        configs = []
        for vm_dir in os.listdir(migrations_dir):
            config_path = os.path.join(migrations_dir, vm_dir, 'vm-config.json')
            if os.path.exists(config_path):
                configs.append((vm_dir, config_path))
        
        if not configs:
            print(colored("❌ No VM configurations found", Colors.YELLOW))
            return
        
        print("\nAvailable configurations:")
        for i, (name, path) in enumerate(configs, 1):
            mtime = os.path.getmtime(path)
            from datetime import datetime
            mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            print(f"  {i}. {name} (saved: {mtime_str})")
        
        choice = self.input_prompt("Config number to view")
        if not choice:
            return
        
        try:
            idx = int(choice) - 1
            name, path = configs[idx]
            
            with open(path, 'r') as f:
                data = json.load(f)
            
            print(colored(f"\n--- {name} ---", Colors.BOLD))
            print(json.dumps(data, indent=2))
            
        except (ValueError, IndexError):
            print(colored("Invalid choice", Colors.RED))
    
    def download_tools(self):
        """Download virtio-win and QEMU guest agent tools."""
        print(colored("\n⬇️  Download VirtIO and QEMU Guest Agent Tools", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        tools_dir = os.path.join(staging_dir, 'tools')
        
        print(f"\n   Destination: {tools_dir}")
        print("\n   Files to download:")
        print("   - virtio-win.iso (~500 MB)")
        print("   - virtio-win-gt-x64.msi (~15 MB)")
        print("   - qemu-ga-x86_64.msi (~2 MB)")
        
        confirm = self.input_prompt("\nStart download? (y/n)")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
        
        print("\n")
        downloaded = download_virtio_tools(tools_dir, verbose=True)
        
        print(colored(f"\n✅ Downloaded {len(downloaded)} file(s) to {tools_dir}", Colors.GREEN))
    
    def generate_postmig_script(self):
        """Generate post-migration PowerShell script."""
        # TODO: Implement Nutanix tools uninstallation after migration to Harvester
        # Tools to uninstall:
        # - Nutanix Guest Tools
        # - Nutanix VirtIO
        # - Nutanix VM Mobility
        # Note: Only uninstall AFTER VM is successfully running on Harvester with Fedora VirtIO drivers
        
        print(colored("\n📜 Generate Post-Migration Script", Colors.BOLD))
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        migrations_dir = os.path.join(staging_dir, 'migrations')
        
        if not os.path.exists(migrations_dir):
            print(colored("❌ No migrations directory found", Colors.YELLOW))
            return
        
        # List available configs
        configs = []
        for vm_dir in os.listdir(migrations_dir):
            config_path = os.path.join(migrations_dir, vm_dir, 'vm-config.json')
            if os.path.exists(config_path):
                configs.append((vm_dir, config_path))
        
        if not configs:
            print(colored("❌ No VM configurations found. Run pre-migration check first.", Colors.YELLOW))
            return
        
        print("\nAvailable configurations:")
        for i, (name, path) in enumerate(configs, 1):
            print(f"  {i}. {name}")
        
        choice = self.input_prompt("Config number")
        if not choice:
            return
        
        try:
            idx = int(choice) - 1
            name, config_path = configs[idx]
            
            # Load config
            vm_config = VMConfig.load(config_path)
            
            # Generate script
            post_config = WindowsPostConfig(None)  # No client needed for script generation
            script = post_config.generate_reconfig_script(vm_config)
            
            # Save script
            script_path = os.path.join(os.path.dirname(config_path), 'reconfigure-network.ps1')
            with open(script_path, 'w') as f:
                f.write(script)
            
            print(colored(f"\n✅ Script generated: {script_path}", Colors.GREEN))
            print("\n--- Script Preview ---")
            print(script[:1000])
            if len(script) > 1000:
                print("...")
            
        except (ValueError, IndexError):
            print(colored("Invalid choice", Colors.RED))
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    def _install_virtio_drivers_postmig(self, client, host):
        """Install Red Hat VirtIO drivers during post-migration (reuses existing WinRM connection)."""
        self.init_actions()
        
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        tools_dir = os.path.join(staging_dir, 'tools')
        virtio_iso = os.path.join(tools_dir, 'virtio-win.iso')
        
        if not os.path.exists(virtio_iso):
            print(colored(f"   ❌ VirtIO ISO not found: {virtio_iso}", Colors.RED))
            print(colored("      Run 'Download virtio/qemu-ga tools' first", Colors.YELLOW))
            return False
        
        # Start HTTP server
        print(colored("\n   🚀 Starting file server...", Colors.CYAN))
        
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                target_ip = socket.gethostbyname(host)
            except:
                target_ip = socket.gethostbyname(host.split('.')[0])
            s.connect((target_ip, 5985))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception as e:
            print(colored(f"   ⚠️  Could not auto-detect local IP: {e}", Colors.YELLOW))
            local_ip = self.input_prompt("   Enter this machine's IP")
            if not local_ip:
                return False
        
        http_port = 8889  # Use different port to avoid conflict
        http_url = f"http://{local_ip}:{http_port}"
        
        import threading
        import http.server
        import socketserver
        
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tools_dir, **kwargs)
            def log_message(self, format, *args):
                pass
        
        httpd = socketserver.TCPServer(("", http_port), QuietHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        try:
            print(colored("   📥 Downloading VirtIO ISO...", Colors.CYAN))
            
            ps_download = f'''
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$isoPath = "$env:TEMP\\virtio-win.iso"
Invoke-WebRequest -Uri "{http_url}/virtio-win.iso" -OutFile $isoPath -UseBasicParsing
if (Test-Path $isoPath) {{ "DOWNLOADED" }} else {{ "FAILED" }}
'''
            stdout, stderr, rc = client.run_powershell(ps_download, timeout=600)
            
            if "DOWNLOADED" not in stdout:
                print(colored(f"   ❌ Download failed", Colors.RED))
                return False
            
            print(colored("   ✅ Downloaded", Colors.GREEN))
            print(colored("   📦 Installing VirtIO drivers...", Colors.CYAN))
            
            ps_install = '''
$iso = "$env:TEMP\\virtio-win.iso"
$logFile = "C:\\temp\\virtio-install.log"

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts - $msg" | Tee-Object -FilePath $logFile -Append
}

Log "VirtIO installation started"

$mount = Mount-DiskImage -ImagePath $iso -PassThru
Start-Sleep 2
$driveLetter = ($mount | Get-Volume).DriveLetter + ":"
Log "Mounted on $driveLetter"

$installers = @(
    "$driveLetter\\virtio-win-guest-tools.exe",
    "$driveLetter\\virtio-win-gt-x64.exe"
)

$installerPath = $null
foreach ($path in $installers) {
    if (Test-Path $path) {
        $installerPath = $path
        Log "Found: $path"
        break
    }
}

if (-not $installerPath) {
    Log "No installer found"
    Dismount-DiskImage -ImagePath $iso
    Write-Host "FAILED"
    exit 1
}

$proc = Start-Process $installerPath -ArgumentList "/S" -PassThru -Wait
Log "Exit code: $($proc.ExitCode)"

Start-Sleep 5

$virtioPath = "$env:ProgramFiles\\Virtio-Win"
$redhatPath = "$env:ProgramFiles\\Red Hat"

if ((Test-Path $virtioPath) -or (Test-Path $redhatPath)) {
    Log "SUCCESS"
    Write-Host "SUCCESS"
} else {
    Log "May have failed"
    Write-Host "UNKNOWN"
}

Dismount-DiskImage -ImagePath $iso -ErrorAction SilentlyContinue
Remove-Item $iso -Force -ErrorAction SilentlyContinue
'''
            stdout, stderr, rc = client.run_powershell(ps_install, timeout=300)
            
            if "SUCCESS" in stdout:
                print(colored("   ✅ VirtIO drivers installed!", Colors.GREEN))
                return True
            else:
                print(colored("   ⚠️  Installation status unknown", Colors.YELLOW))
                print(colored("      Check log: C:\\temp\\virtio-install.log", Colors.CYAN))
                return False
                
        except Exception as e:
            print(colored(f"   ❌ Error: {e}", Colors.RED))
            return False
        finally:
            httpd.shutdown()
    
    def postmig_autoconfigure(self, vm_name=None, namespace=None):
        """Auto-configure Windows VM after migration using ping FQDN."""
        print(colored("\n🔧 Post-Migration Auto-Configure", Colors.BOLD))
        print(colored("-" * 50, Colors.BLUE))
        
        # Check if step already done (use selected_vm or passed vm_name)
        check_vm = vm_name or self._selected_vm
        if check_vm and not self.check_step_and_confirm(check_vm, 'postmig'):
            return
        
        if not self.harvester and not self.connect_harvester():
            return
        
        if not WINRM_AVAILABLE:
            print(colored("❌ pywinrm not installed", Colors.RED))
            return
        
        # Use selected VM if available
        if not vm_name and self._selected_vm:
            vm_name = self._selected_vm.lower().replace(' ', '-')
            # Try to find the VM in Harvester to get namespace
            vms = self.harvester.list_all_vms()
            for vm in vms:
                if vm.get('metadata', {}).get('name', '').lower() == vm_name:
                    namespace = vm.get('metadata', {}).get('namespace', 'default')
                    # Check if VM is running
                    vm_status = vm.get('status', {})
                    if not vm_status.get('ready', False):
                        print(colored(f"\n⚠️  VM {vm_name} is not running", Colors.YELLOW))
                        start = self.input_prompt("Start it now? (y/n)")
                        if start.lower() == 'y':
                            print("   Starting VM...")
                            try:
                                self.harvester.start_vm(vm_name, namespace)
                                print(colored("   ✅ Start command sent. Waiting 30s for boot...", Colors.GREEN))
                                import time
                                time.sleep(30)
                            except Exception as e:
                                print(colored(f"   ❌ Error: {e}", Colors.RED))
                                return
                        else:
                            return
                    break
            else:
                print(colored(f"⚠️  VM {vm_name} not found in Harvester, continuing anyway...", Colors.YELLOW))
                namespace = namespace or "harvester-public"
            
            print(colored(f"   VM: {vm_name} ({namespace})", Colors.CYAN))
        
        # If still no vm_name, list VMs and ask user to select
        if not vm_name:
            # List ALL VMs in Harvester (across all namespaces)
            vms = self.harvester.list_all_vms()
            if not vms:
                print(colored("❌ No VMs found in Harvester", Colors.RED))
                return
            
            print("\nHarvester VMs:")
            for i, vm in enumerate(vms, 1):
                name = vm.get('metadata', {}).get('name', 'N/A')
                ns = vm.get('metadata', {}).get('namespace', 'N/A')
                status = vm.get('status', {})
                running = status.get('ready', False)
                state = "🟢 Running" if running else "🔴 Stopped"
                print(f"  {i}. {name} ({ns}) - {state}")
            
            choice = self.input_prompt("\nSelect VM number")
            if not choice:
                return
            
            try:
                idx = int(choice) - 1
                selected_vm = vms[idx]
                vm_name = selected_vm.get('metadata', {}).get('name')
                namespace = selected_vm.get('metadata', {}).get('namespace')
            except:
                print(colored("Invalid choice", Colors.RED))
                return
            
            # Check if VM is running
            vm_status = selected_vm.get('status', {})
            if not vm_status.get('ready', False):
                print(colored(f"\n⚠️  VM {vm_name} is not running", Colors.YELLOW))
                start = self.input_prompt("Start it now? (y/n)")
                if start.lower() == 'y':
                    print("   Starting VM...")
                    try:
                        self.harvester.start_vm(vm_name, namespace)
                        print(colored("   ✅ Start command sent. Waiting 30s for boot...", Colors.GREEN))
                        import time
                        time.sleep(30)
                    except Exception as e:
                        print(colored(f"   ❌ Error: {e}", Colors.RED))
                        return
                else:
                    return
        elif not namespace:
            print(colored(f"   VM: {vm_name} ({namespace})", Colors.CYAN))
        
        # Build FQDN and ping
        windows_config = self.config.get('windows', {})
        domain = windows_config.get('domain', 'AD.WYSSCENTER.CH').lower()
        vm_fqdn = f"{vm_name}.{domain}"
        
        print(colored(f"\n🔍 Pinging VM: {vm_fqdn}...", Colors.CYAN))
        
        import time
        import subprocess
        
        max_wait = 180  # 3 minutes max
        start_time = time.time()
        vm_reachable = False
        
        while time.time() - start_time < max_wait:
            elapsed = int(time.time() - start_time)
            
            try:
                result = subprocess.run(
                    ['ping', '-c', '1', '-W', '2', vm_fqdn],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    vm_reachable = True
                    print(colored(f"\n   ✅ VM responds to ping! ({elapsed}s)", Colors.GREEN))
                    break
                else:
                    if elapsed % 15 == 0:
                        print(f"   ⏳ Waiting for VM to respond... ({elapsed}s)")
            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                if elapsed % 30 == 0:
                    print(f"   ⏳ Ping error: {e}")
            
            time.sleep(5)
        
        if not vm_reachable:
            print(colored(f"\n   ⚠️  VM not responding to ping after {max_wait}s", Colors.YELLOW))
            retry = self.input_prompt("   Continue anyway? (y/n) [n]") or "n"
            if retry.lower() != 'y':
                return
        
        # Load vm-config.json
        staging_dir = self.config.get('transfer', {}).get('staging_mount', '/mnt/data')
        config_path = os.path.join(staging_dir, 'migrations', vm_name.lower(), 'vm-config.json')
        
        if not os.path.exists(config_path):
            migrations_dir = os.path.join(staging_dir, 'migrations')
            if os.path.exists(migrations_dir):
                configs = [d for d in os.listdir(migrations_dir) 
                          if os.path.exists(os.path.join(migrations_dir, d, 'vm-config.json'))]
                if configs:
                    print(f"\n   Config not found for '{vm_name}'. Available:")
                    for i, cfg in enumerate(configs, 1):
                        print(f"     {i}. {cfg}")
                    choice = self.input_prompt("   Select config number")
                    try:
                        idx = int(choice) - 1
                        config_path = os.path.join(migrations_dir, configs[idx], 'vm-config.json')
                    except:
                        return
        
        if not os.path.exists(config_path):
            print(colored(f"❌ Config not found. Run pre-migration check first.", Colors.RED))
            return
        
        try:
            with open(config_path) as f:
                vm_config = json.load(f)
            print(colored(f"   📋 Loaded: {config_path}", Colors.GREEN))
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
            return
        
        # Show config to apply
        print(colored("\n📋 Network Configuration to Apply:", Colors.BOLD))
        interfaces = vm_config.get('network', {}).get('interfaces', [])
        static_interfaces = []
        
        for iface in interfaces:
            dhcp = iface.get('dhcp', True)
            name = iface.get('name', 'Unknown')
            if not dhcp:
                static_interfaces.append(iface)
                print(f"   {name}: {iface.get('ip')}/{iface.get('prefix')}")
                print(f"      Gateway: {iface.get('gateway')}")
                print(f"      DNS: {', '.join(iface.get('dns', []))}")
            else:
                print(f"   {name}: DHCP (no change)")
        
        if not static_interfaces:
            print(colored("\n✅ All interfaces use DHCP - no reconfiguration needed!", Colors.GREEN))
            return
        
        confirm = self.input_prompt("\nApply configuration? (y/n)")
        if confirm.lower() != 'y':
            return
        
        # Connect via WinRM using FQDN
        use_kerberos = windows_config.get('use_kerberos', True)
        
        print(colored(f"\n🔌 Connecting to {vm_fqdn}...", Colors.CYAN))
        
        username = None
        password = None
        transport = "ntlm"
        
        if use_kerberos and get_kerberos_auth():
            print(colored("   Using Kerberos authentication", Colors.GREEN))
            transport = "kerberos"
        else:
            print("   Using NTLM authentication")
            try:
                username, password = self.vault.get_credential("local-admin")
                print(f"   Using: {username}")
            except:
                username = self.input_prompt("   Username [Administrator]") or "Administrator"
                import getpass
                password = getpass.getpass("   Password: ")
        
        # Wait for WinRM to be ready
        print("   Waiting 10s for WinRM service...")
        time.sleep(10)
        
        try:
            client = WinRMClient(
                host=vm_fqdn,
                username=username,
                password=password,
                transport=transport
            )
            
            if not client.test_connection():
                print(colored("❌ WinRM connection failed", Colors.RED))
                print(colored("   Tip: Ensure WinRM is enabled and firewall allows it", Colors.YELLOW))
                return
            
            print(colored("   ✅ Connected!", Colors.GREEN))
            
            # Clean up ghost NICs (from Nutanix VirtIO)
            print(colored("\n   🧹 Cleaning up ghost network adapters...", Colors.CYAN))
            ghost_cleanup_script = '''
$ErrorActionPreference = "SilentlyContinue"

# Method 1: Remove ghost devices via devcon/pnputil
$ghostNics = Get-PnpDevice -Class Net | Where-Object { $_.Status -eq "Unknown" -or $_.Status -eq "Error" }
$removedCount = 0

foreach ($nic in $ghostNics) {
    $name = $nic.FriendlyName
    $instanceId = $nic.InstanceId
    Write-Host "   Removing ghost NIC: $name"
    
    # Try pnputil first (Windows 10/Server 2016+)
    $result = pnputil /remove-device $instanceId 2>&1
    if ($LASTEXITCODE -eq 0) {
        $removedCount++
    }
}

# Method 2: Clean up orphaned IP configurations from registry
# This fixes the "IP already assigned to another adapter" error
$netConfigs = Get-ChildItem "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces" -ErrorAction SilentlyContinue
foreach ($config in $netConfigs) {
    $guid = $config.PSChildName
    # Check if this GUID corresponds to an existing adapter
    $adapter = Get-NetAdapter | Where-Object { $_.InterfaceGuid -eq "{$guid}" }
    if (-not $adapter) {
        # Orphaned config - check if it has a static IP
        $ipAddress = (Get-ItemProperty $config.PSPath -ErrorAction SilentlyContinue).IPAddress
        if ($ipAddress -and $ipAddress -ne "0.0.0.0") {
            Write-Host "   Cleaning orphaned IP config: $ipAddress"
            # Remove the static IP configuration
            Remove-ItemProperty -Path $config.PSPath -Name "IPAddress" -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $config.PSPath -Name "SubnetMask" -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $config.PSPath -Name "DefaultGateway" -ErrorAction SilentlyContinue
            $removedCount++
        }
    }
}

Write-Host "GHOST_CLEANUP_RESULT:$removedCount"
'''
            try:
                result = client.run_powershell(ghost_cleanup_script)
                output = result[0]  # stdout is first element of tuple
                if 'GHOST_CLEANUP_RESULT:' in output:
                    count = output.split('GHOST_CLEANUP_RESULT:')[1].strip().split()[0]
                    if int(count) > 0:
                        print(colored(f"   ✅ Cleaned {count} ghost adapter(s)/config(s)", Colors.GREEN))
                    else:
                        print(colored("   ✅ No ghost adapters found", Colors.GREEN))
                else:
                    print(colored("   ✅ Ghost cleanup completed", Colors.GREEN))
            except Exception as e:
                print(colored(f"   ⚠️  Ghost cleanup warning: {e}", Colors.YELLOW))
                # Continue anyway - this is not critical
            
            # Check and apply each static interface config
            for iface in static_interfaces:
                iface_name = iface.get('name', 'Ethernet')
                ip = iface.get('ip')
                prefix = iface.get('prefix', 24)
                gateway = iface.get('gateway', '')
                dns_list = iface.get('dns', [])
                
                # First, check if network config is already correct
                print(colored(f"\n   🔍 Checking {iface_name} configuration...", Colors.CYAN))
                
                check_script = f'''
$targetIP = "{ip}"
$targetPrefix = {prefix}
$targetGateway = "{gateway}"
$targetDNS = @({','.join([f'"{d}"' for d in dns_list])})

# Find active adapter
$adapter = Get-NetAdapter | Where-Object {{ $_.Status -eq "Up" }} | Select-Object -First 1
if (-not $adapter) {{
    Write-Host "CONFIG_CHECK:NO_ADAPTER"
    exit
}}

$ifName = $adapter.Name

# Get current config
$currentIP = Get-NetIPAddress -InterfaceAlias $ifName -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
$currentRoute = Get-NetRoute -InterfaceAlias $ifName -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue | Select-Object -First 1
$currentDNS = (Get-DnsClientServerAddress -InterfaceAlias $ifName -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses

$ipMatch = $currentIP -and ($currentIP.IPAddress -eq $targetIP) -and ($currentIP.PrefixLength -eq $targetPrefix)
$gwMatch = (-not $targetGateway) -or ($currentRoute -and ($currentRoute.NextHop -eq $targetGateway))
$dnsMatch = ($targetDNS.Count -eq 0) -or (($currentDNS -join ",") -eq ($targetDNS -join ","))

if ($ipMatch -and $gwMatch -and $dnsMatch) {{
    Write-Host "CONFIG_CHECK:OK"
    Write-Host "CURRENT_IP:$($currentIP.IPAddress)/$($currentIP.PrefixLength)"
    Write-Host "CURRENT_GW:$($currentRoute.NextHop)"
    Write-Host "CURRENT_DNS:$($currentDNS -join ',')"
}} else {{
    Write-Host "CONFIG_CHECK:MISMATCH"
    Write-Host "CURRENT_IP:$($currentIP.IPAddress)/$($currentIP.PrefixLength)"
    Write-Host "EXPECTED_IP:$targetIP/$targetPrefix"
    Write-Host "CURRENT_GW:$($currentRoute.NextHop)"
    Write-Host "EXPECTED_GW:$targetGateway"
    Write-Host "CURRENT_DNS:$($currentDNS -join ',')"
    Write-Host "EXPECTED_DNS:$($targetDNS -join ',')"
}}
'''
                try:
                    check_result = client.run_powershell(check_script)
                    check_output = check_result[0]  # stdout is first element of tuple
                    
                    if 'CONFIG_CHECK:OK' in check_output:
                        print(colored(f"   ✅ Network already configured correctly ({ip}/{prefix})", Colors.GREEN))
                        continue  # Skip to next interface
                    elif 'CONFIG_CHECK:NO_ADAPTER' in check_output:
                        print(colored(f"   ⚠️  No active network adapter found", Colors.YELLOW))
                    else:
                        # Show mismatch details
                        print(colored(f"   ℹ️  Network config needs update", Colors.YELLOW))
                        for line in check_output.split('\n'):
                            if line.startswith('CURRENT_') or line.startswith('EXPECTED_'):
                                print(f"      {line}")
                except Exception as e:
                    print(colored(f"   ⚠️  Could not check config: {e}", Colors.YELLOW))
                
                print(colored(f"\n   🔧 Configuring {iface_name}...", Colors.CYAN))
                
                # PowerShell with logging
                ps_script = f'''
$ErrorActionPreference = "Continue"
$logFile = "C:\\temp\\network-reconfig.log"

if (-not (Test-Path "C:\\temp")) {{
    New-Item -ItemType Directory -Path "C:\\temp" -Force | Out-Null
}}

function Log {{
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts - $msg" | Tee-Object -FilePath $logFile -Append
}}

Log "=========================================="
Log "Network reconfiguration started"
Log "=========================================="

$ifName = "{iface_name}"
$ip = "{ip}"
$prefix = {prefix}
$gateway = "{gateway}"
$dns = @({','.join([f'"{d}"' for d in dns_list])})

Log "Target: $ifName -> $ip/$prefix via $gateway"

try {{
    $adapter = Get-NetAdapter -Name $ifName -ErrorAction SilentlyContinue
    if (-not $adapter) {{
        $adapter = Get-NetAdapter | Where-Object {{ $_.Name -like "*Ethernet*" -and $_.Status -eq "Up" }} | Select-Object -First 1
        if (-not $adapter) {{
            $adapter = Get-NetAdapter | Where-Object {{ $_.Status -eq "Up" }} | Select-Object -First 1
        }}
        if ($adapter) {{
            $ifName = $adapter.Name
            Log "Using adapter: $ifName"
        }} else {{
            throw "No active adapter found"
        }}
    }}

    Get-NetIPAddress -InterfaceAlias $ifName -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {{
        Log "Removing: $($_.IPAddress)"
        Remove-NetIPAddress -InterfaceAlias $ifName -IPAddress $_.IPAddress -Confirm:$false -ErrorAction SilentlyContinue
    }}
    Remove-NetRoute -InterfaceAlias $ifName -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue

    New-NetIPAddress -InterfaceAlias $ifName -IPAddress $ip -PrefixLength $prefix -DefaultGateway $gateway -ErrorAction Stop
    Log "IP configured: $ip/$prefix"

    Set-DnsClientServerAddress -InterfaceAlias $ifName -ServerAddresses $dns -ErrorAction Stop
    Log "DNS configured: $($dns -join ', ')"

    Log "SUCCESS"
    Write-Host "SUCCESS"
}} catch {{
    Log "ERROR: $($_.Exception.Message)"
    throw
}}
'''
                try:
                    stdout, stderr, rc = client.run_powershell(ps_script)
                    
                    if "SUCCESS" in stdout:
                        print(colored(f"   ✅ {iface_name} configured: {ip}/{prefix}", Colors.GREEN))
                    else:
                        print(colored(f"   ⚠️  Partial success (rc={rc})", Colors.YELLOW))
                        print(colored(f"      Check log: C:\\temp\\network-reconfig.log", Colors.CYAN))
                except Exception as e:
                    if "Connection reset" in str(e) or "WinRM" in str(e):
                        print(colored(f"   ✅ {iface_name} likely configured (connection reset)", Colors.GREEN))
                        print(colored("      This is normal when changing IP", Colors.CYAN))
                    else:
                        print(colored(f"   ⚠️  Error: {e}", Colors.YELLOW))
                        print(colored(f"      Check log: C:\\temp\\network-reconfig.log", Colors.CYAN))
            
            original_ip = static_interfaces[0].get('ip') if static_interfaces else 'N/A'
            
            # Reboot after network configuration
            print(colored("\n🔄 Post-Configuration Reboot", Colors.BOLD))
            print("   A reboot is recommended to ensure network changes are fully applied.")
            
            reboot = self.input_prompt("\n   Reboot VM now? (y/n) [y]") or "y"
            if reboot.lower() == 'y':
                print(colored("\n   🔄 Rebooting VM...", Colors.CYAN))
                try:
                    # Send reboot command
                    client.run_powershell("Restart-Computer -Force", timeout=10)
                except:
                    pass  # Connection will drop during reboot
                
                print("   ⏳ Waiting for VM to reboot...")
                time.sleep(30)  # Initial wait for shutdown
                
                # Wait for VM to come back online via ping
                print(f"   ⏳ Pinging {original_ip} to verify VM is back online...")
                
                max_wait = 180  # 3 minutes max
                start_time = time.time()
                vm_back = False
                
                while time.time() - start_time < max_wait:
                    elapsed = int(time.time() - start_time)
                    
                    try:
                        result = subprocess.run(
                            ['ping', '-c', '1', '-W', '2', original_ip],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        
                        if result.returncode == 0:
                            vm_back = True
                            print(colored(f"\n   ✅ VM is back online! IP: {original_ip} ({elapsed}s)", Colors.GREEN))
                            break
                        else:
                            if elapsed % 15 == 0:
                                print(f"   ⏳ Waiting for VM... ({elapsed}s)")
                    except:
                        pass
                    
                    time.sleep(5)
                
                if not vm_back:
                    print(colored(f"\n   ⚠️  VM not responding on {original_ip} after {max_wait}s", Colors.YELLOW))
                    print(colored("      Check console via Harvester UI", Colors.YELLOW))
                else:
                    # Wait for WinRM to be ready
                    print("   ⏳ Waiting for WinRM service (15s)...")
                    time.sleep(15)
                    
                    # Try to reconnect and verify
                    print(colored(f"   🔌 Verifying connection to {original_ip}...", Colors.CYAN))
                    try:
                        verify_client = WinRMClient(
                            host=original_ip,
                            username=username,
                            password=password,
                            transport=transport
                        )
                        if verify_client.test_connection():
                            print(colored("   ✅ WinRM connection verified!", Colors.GREEN))
                            
                            # Quick verification
                            stdout, _, _ = verify_client.run_powershell(
                                "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '169.*' -and $_.IPAddress -ne '127.0.0.1'}).IPAddress"
                            )
                            current_ip = stdout.strip().split('\n')[0] if stdout else 'unknown'
                            print(colored(f"   ✅ Current IP: {current_ip}", Colors.GREEN))
                        else:
                            print(colored("   ⚠️  WinRM not ready yet, but VM is pingable", Colors.YELLOW))
                    except Exception as e:
                        print(colored(f"   ⚠️  Verification failed: {e}", Colors.YELLOW))
                        print(colored("      VM is pingable, should be operational", Colors.YELLOW))
            
            # Offer to uninstall Nutanix tools
            print(colored("\n🧹 Nutanix Tools Cleanup", Colors.BOLD))
            print("   The following Nutanix tools should be removed after migration:")
            print("   - Nutanix Guest Tools")
            print("   - Nutanix VirtIO")
            print("   - Nutanix VM Mobility")
            
            cleanup = self.input_prompt("\n   Remove Nutanix tools? (y/n) [y]") or "y"
            if cleanup.lower() == 'y':
                print(colored("\n   🗑️  Uninstalling Nutanix tools...", Colors.CYAN))
                
                ps_uninstall = '''
$ErrorActionPreference = "Continue"
$logFile = "C:\\temp\\nutanix-cleanup.log"

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts - $msg" | Tee-Object -FilePath $logFile -Append
}

Log "=========================================="
Log "Nutanix tools cleanup started"
Log "=========================================="

# Find and uninstall Nutanix products
$nutanixApps = Get-WmiObject -Class Win32_Product | Where-Object { $_.Name -like "*Nutanix*" }

if ($nutanixApps) {
    foreach ($app in $nutanixApps) {
        Log "Uninstalling: $($app.Name)"
        try {
            $result = $app.Uninstall()
            if ($result.ReturnValue -eq 0) {
                Log "SUCCESS: $($app.Name) uninstalled"
            } else {
                Log "WARNING: $($app.Name) returned code $($result.ReturnValue)"
            }
        } catch {
            Log "ERROR: $($_.Exception.Message)"
        }
    }
} else {
    Log "No Nutanix applications found via WMI"
}

# Also try uninstall strings from registry
$uninstallKeys = @(
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
    "HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*"
)

foreach ($key in $uninstallKeys) {
    $apps = Get-ItemProperty $key -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "*Nutanix*" }
    foreach ($app in $apps) {
        if ($app.UninstallString) {
            Log "Found: $($app.DisplayName)"
            $uninstall = $app.UninstallString
            # Handle different uninstall string formats
            if ($uninstall -match "msiexec") {
                $uninstall = $uninstall -replace "/I", "/X"
                $uninstall = "$uninstall /qn /norestart"
            }
            Log "Running: $uninstall"
            try {
                Start-Process cmd.exe -ArgumentList "/c $uninstall" -Wait -NoNewWindow
                Log "Completed uninstall command"
            } catch {
                Log "ERROR: $($_.Exception.Message)"
            }
        }
    }
}

# Stop and disable Nutanix services
$services = Get-Service | Where-Object { $_.Name -like "*Nutanix*" -or $_.DisplayName -like "*Nutanix*" }
foreach ($svc in $services) {
    Log "Stopping service: $($svc.Name)"
    Stop-Service -Name $svc.Name -Force -ErrorAction SilentlyContinue
    Set-Service -Name $svc.Name -StartupType Disabled -ErrorAction SilentlyContinue
}

# Clean up Nutanix folders
$folders = @(
    "$env:ProgramFiles\\Nutanix",
    "${env:ProgramFiles(x86)}\\Nutanix",
    "$env:ProgramData\\Nutanix"
)
foreach ($folder in $folders) {
    if (Test-Path $folder) {
        Log "Removing folder: $folder"
        Remove-Item -Path $folder -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Log "=========================================="
Log "Nutanix cleanup completed"
Log "=========================================="
Write-Host "CLEANUP_DONE"
'''
                try:
                    stdout, stderr, rc = client.run_powershell(ps_uninstall, timeout=300)
                    if "CLEANUP_DONE" in stdout:
                        print(colored("   ✅ Nutanix tools cleanup completed", Colors.GREEN))
                        print(colored("      Log: C:\\temp\\nutanix-cleanup.log", Colors.CYAN))
                    else:
                        print(colored("   ⚠️  Cleanup may be incomplete", Colors.YELLOW))
                        print(colored("      Check log: C:\\temp\\nutanix-cleanup.log", Colors.CYAN))
                except Exception as e:
                    print(colored(f"   ⚠️  Cleanup error: {e}", Colors.YELLOW))
                    print(colored("      You may need to uninstall manually", Colors.YELLOW))
            
            # Offer to install Red Hat VirtIO drivers
            print(colored("\n📦 Red Hat VirtIO Drivers", Colors.BOLD))
            print("   Required for switching from SATA to VirtIO disk bus (better performance)")
            
            install_virtio = self.input_prompt("\n   Install Red Hat VirtIO drivers now? (y/n) [y]") or "y"
            virtio_installed = False
            if install_virtio.lower() == 'y':
                virtio_installed = self._install_virtio_drivers_postmig(client, vm_fqdn)
            
            # Summary and next steps
            print(colored("\n" + "="*50, Colors.GREEN))
            print(colored("✅ Post-migration configuration complete!", Colors.GREEN))
            print(colored("="*50, Colors.GREEN))
            print(f"\n   VM: {vm_name}")
            print(f"   FQDN: {vm_fqdn}")
            print(f"   Static IP: {original_ip}")
            
            if virtio_installed:
                # Offer to switch disk bus
                print(colored("\n🔄 Disk Bus Optimization", Colors.BOLD))
                print("   VirtIO drivers are installed. You can now switch from SATA to VirtIO for better performance.")
                print(colored("   ⚠️  This requires stopping the VM, changing config, and restarting.", Colors.YELLOW))
                
                switch_bus = self.input_prompt("\n   Switch to VirtIO disk bus now? (y/n) [y]") or "y"
                if switch_bus.lower() == 'y':
                    print(colored("\n   🔄 Switching disk bus to VirtIO...", Colors.CYAN))
                    
                    # Stop VM
                    print("   Stopping VM...")
                    try:
                        self.harvester.stop_vm(vm_name, namespace)
                        
                        # Wait for VM to stop
                        max_wait = 120
                        elapsed = 0
                        while elapsed < max_wait:
                            time.sleep(5)
                            elapsed += 5
                            vm_data = self.harvester.get_vm(vm_name, namespace)
                            if not vm_data.get('status', {}).get('ready', False):
                                print(colored("   ✅ VM stopped", Colors.GREEN))
                                break
                            print(f"   Waiting... ({elapsed}s)")
                        
                        # Get current VM config
                        vm_data = self.harvester.get_vm(vm_name, namespace)
                        template_spec = vm_data.get('spec', {}).get('template', {}).get('spec', {})
                        
                        # Update disk bus
                        new_disks = []
                        for disk in template_spec.get('domain', {}).get('devices', {}).get('disks', []):
                            new_disk = disk.copy()
                            if 'disk' in new_disk:
                                new_disk['disk'] = new_disk['disk'].copy()
                                old_bus = new_disk['disk'].get('bus', 'sata')
                                if old_bus in ('sata', 'ide', 'scsi'):
                                    new_disk['disk']['bus'] = 'virtio'
                                    print(f"   {new_disk.get('name')}: {old_bus} → virtio")
                            new_disks.append(new_disk)
                        
                        # Apply patch
                        patch = {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "domain": {
                                            "devices": {
                                                "disks": new_disks
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        
                        self.harvester._request(
                            "PATCH",
                            f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{vm_name}",
                            json=patch,
                            headers={"Content-Type": "application/merge-patch+json"}
                        )
                        
                        print(colored("   ✅ Disk bus switched to VirtIO!", Colors.GREEN))
                        
                        # Start VM
                        print("   Starting VM with VirtIO...")
                        self.harvester.start_vm(vm_name, namespace)
                        print(colored("   ✅ VM starting with VirtIO disk bus", Colors.GREEN))
                        
                        print(colored("\n🎉 Migration complete with VirtIO optimization!", Colors.GREEN))
                        
                    except Exception as e:
                        print(colored(f"   ❌ Error switching disk bus: {e}", Colors.RED))
                        print(colored("   You can do this manually: Menu Harvester → Switch VM disk bus", Colors.YELLOW))
                else:
                    print(colored("\n💡 To optimize later:", Colors.YELLOW))
                    print("   Menu Harvester → Switch VM disk bus (option 12)")
            else:
                print(colored("\n💡 Next steps for VirtIO optimization:", Colors.YELLOW))
                print("   1. Install Red Hat VirtIO drivers (Menu Windows → Install VirtIO)")
                print("   2. Switch VM to VirtIO bus (Menu Harvester → Switch VM disk bus)")
                print("   3. Reboot and verify performance")
            
            # Update tracker
            track_vm = vm_name or self._selected_vm
            if track_vm:
                self.update_step(track_vm, 'postmig')
                print(colored(f"\n   ✅ Step 'postmig' marked complete in tracker", Colors.GREEN))
                print(colored(f"\n   🎉 Migration of {track_vm} is COMPLETE!", Colors.GREEN + Colors.BOLD))
            
        except Exception as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    def menu_vault(self):
        """Vault management submenu."""
        while True:
            self.print_header()
            self.print_menu("VAULT MANAGEMENT", [
                ("1", "List credentials"),
                ("2", "Add credential"),
                ("3", "Test credential"),
                ("4", "Check Kerberos ticket"),
                ("5", "Get Kerberos ticket (kinit)"),
                ("0", "Back")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self._vault_list()
                self.pause()
            elif choice == "2":
                self._vault_add()
                self.pause()
            elif choice == "3":
                self._vault_test()
                self.pause()
            elif choice == "4":
                self._kerberos_check()
                self.pause()
            elif choice == "5":
                self._kerberos_kinit()
                self.pause()
            elif choice == "0":
                break
    
    def _vault_list(self):
        """List vault credentials."""
        print(colored("\n🔐 Vault Credentials", Colors.BOLD))
        try:
            creds = self.vault.list_credentials()
            if creds:
                for c in creds:
                    print(f"   - {c}")
            else:
                print("   No credentials stored")
        except VaultError as e:
            print(colored(f"   Error: {e}", Colors.RED))
    
    def _vault_add(self):
        """Add credential to vault."""
        print(colored("\n🔐 Add Credential", Colors.BOLD))
        
        name = self.input_prompt("Credential name (e.g., local-admin)")
        if not name:
            return
        
        username = self.input_prompt("Username")
        if not username:
            return
        
        import getpass
        password = getpass.getpass("Password: ")
        if not password:
            return
        
        try:
            self.vault.set_credential(name, username, password)
            print(colored(f"✅ Credential '{name}' saved", Colors.GREEN))
        except VaultError as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    def _vault_test(self):
        """Test credential retrieval."""
        print(colored("\n🔐 Test Credential", Colors.BOLD))
        
        name = self.input_prompt("Credential name to test")
        if not name:
            return
        
        try:
            username, password = self.vault.get_credential(name)
            print(colored(f"✅ Retrieved: {username} / {'*' * len(password)}", Colors.GREEN))
        except VaultError as e:
            print(colored(f"❌ Error: {e}", Colors.RED))
    
    def _kerberos_check(self):
        """Check Kerberos ticket status."""
        print(colored("\n🎫 Kerberos Ticket Status", Colors.BOLD))
        
        import subprocess
        result = subprocess.run(["klist"], capture_output=True, text=True)
        
        if result.returncode == 0:
            print(colored("✅ Valid Kerberos tickets:", Colors.GREEN))
            print(result.stdout)
        else:
            print(colored("❌ No valid Kerberos tickets", Colors.YELLOW))
            print("   Run: kinit user@AD.WYSSCENTER.CH")
    
    def _kerberos_kinit(self):
        """Get Kerberos ticket."""
        print(colored("\n🎫 Get Kerberos Ticket", Colors.BOLD))
        
        principal = self.input_prompt("Principal (e.g., adm_user@AD.WYSSCENTER.CH)")
        if not principal:
            return
        
        import getpass
        password = getpass.getpass("Password: ")
        
        if kinit(principal, password):
            print(colored("✅ Kerberos ticket obtained", Colors.GREEN))
            import subprocess
            subprocess.run(["klist"])
        else:
            print(colored("❌ Failed to get Kerberos ticket", Colors.RED))
    
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
                ("1", "Migration Tracker"),
                ("2", "Migration"),
                ("3", "Nutanix"),
                ("4", "Harvester"),
                ("5", "Windows Tools"),
                ("6", "Configuration"),
                ("q", "Quit")
            ])
            
            choice = self.input_prompt()
            
            if choice == "1":
                self.menu_tracker()
            elif choice == "2":
                self.menu_migration()
            elif choice == "3":
                self.menu_nutanix()
            elif choice == "4":
                self.menu_harvester()
            elif choice == "5":
                self.menu_windows()
            elif choice == "6":
                self.menu_config()
            elif choice.lower() == "q":
                print(colored("\nGoodbye! 👋", Colors.CYAN))
                break


def main():
    parser = argparse.ArgumentParser(description="Nutanix to Harvester Migration Tool")
    parser.add_argument("-c", "--config", default="config.yaml", help="Configuration file")
    parser.add_argument("command", nargs="?", help="Direct command (optional)")
    parser.add_argument("args", nargs="*", help="Arguments")
    parser.add_argument("--disk", type=int, help="Disk index for import command")
    parser.add_argument("--namespace", "-n", default="harvester-public", help="Target namespace")
    parser.add_argument("--storage-class", "-s", default="harvester-longhorn-dual-node", help="Storage class")
    
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
        elif args.command == "import":
            # Import command: python3 migrate.py import vmname --disk N
            if not args.args:
                print(colored("Usage: migrate.py import <vmname> --disk <N>", Colors.RED))
                print(colored("   or: migrate.py import <vmname> (imports all disks)", Colors.RED))
                sys.exit(1)
            vm_name = args.args[0]
            disk_idx = args.disk
            tool.import_vm_disk(vm_name, disk_idx, args.namespace, args.storage_class)
        else:
            print(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
