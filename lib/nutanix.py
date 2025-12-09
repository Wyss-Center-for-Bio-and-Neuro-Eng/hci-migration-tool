"""
Nutanix Prism API Client
"""

import os
import subprocess
import shutil
import time
import requests
import urllib3
from typing import Optional, List, Dict, Any

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NutanixClient:
    """Nutanix Prism API client."""
    
    def __init__(self, config: dict):
        """
        Initialize Nutanix client.
        
        Args:
            config: Dictionary with prism_ip, username, password, verify_ssl
            Optional: cvm_ip, cvm_user, nfs_mount_path
        """
        self.base_url = f"https://{config['prism_ip']}:9440/api/nutanix/v3"
        self.auth = (config['username'], config['password'])
        self.verify_ssl = config.get('verify_ssl', False)
        self.prism_ip = config['prism_ip']
        
        # NFS/SSH config for fast transfer
        self.cvm_ip = config.get('cvm_ip', config['prism_ip'])  # Default to prism_ip
        self.cvm_user = config.get('cvm_user', 'nutanix')
        self.nfs_mount_path = config.get('nfs_mount_path', '/mnt/nutanix')
    
    def _request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Execute API request."""
        url = f"{self.base_url}/{endpoint}"
        response = requests.request(
            method=method,
            url=url,
            auth=self.auth,
            json=data,
            verify=self.verify_ssl
        )
        response.raise_for_status()
        return response.json()
    
    # === VM Operations ===
    
    def list_vms(self, limit: int = 500) -> List[dict]:
        """List all VMs."""
        payload = {"kind": "vm", "length": limit}
        result = self._request("POST", "vms/list", payload)
        return result.get('entities', [])
    
    def get_vm(self, vm_uuid: str) -> dict:
        """Get VM details by UUID."""
        return self._request("GET", f"vms/{vm_uuid}")
    
    def get_vm_by_name(self, vm_name: str) -> Optional[dict]:
        """Get VM by name."""
        payload = {"kind": "vm", "filter": f"vm_name=={vm_name}", "length": 1}
        result = self._request("POST", "vms/list", payload)
        entities = result.get('entities', [])
        return entities[0] if entities else None
    
    def power_off_vm(self, vm_uuid: str) -> dict:
        """Power off a VM (ACPI shutdown)."""
        vm = self.get_vm(vm_uuid)
        spec_version = vm.get('metadata', {}).get('spec_version', 0)
        
        payload = {
            "metadata": {
                "kind": "vm",
                "uuid": vm_uuid,
                "spec_version": spec_version
            },
            "spec": vm['spec']
        }
        payload['spec']['resources']['power_state'] = 'OFF'
        
        return self._request("PUT", f"vms/{vm_uuid}", payload)
    
    def power_on_vm(self, vm_uuid: str) -> dict:
        """Power on a VM."""
        vm = self.get_vm(vm_uuid)
        spec_version = vm.get('metadata', {}).get('spec_version', 0)
        
        payload = {
            "metadata": {
                "kind": "vm",
                "uuid": vm_uuid,
                "spec_version": spec_version
            },
            "spec": vm['spec']
        }
        payload['spec']['resources']['power_state'] = 'ON'
        
        return self._request("PUT", f"vms/{vm_uuid}", payload)
    
    # === Image Operations ===
    
    def list_images(self, limit: int = 500) -> List[dict]:
        """List all images."""
        payload = {"kind": "image", "length": limit}
        result = self._request("POST", "images/list", payload)
        return result.get('entities', [])
    
    def get_image(self, image_uuid: str) -> dict:
        """Get image details."""
        return self._request("GET", f"images/{image_uuid}")
    
    def get_image_by_name(self, image_name: str) -> Optional[dict]:
        """Get image by name."""
        payload = {"kind": "image", "filter": f"name=={image_name}", "length": 1}
        result = self._request("POST", "images/list", payload)
        entities = result.get('entities', [])
        return entities[0] if entities else None
    
    def get_image_download_url(self, image_uuid: str) -> str:
        """Return image download URL."""
        return f"https://{self.prism_ip}:9440/api/nutanix/v3/images/{image_uuid}/file"
    
    def delete_image(self, image_uuid: str) -> dict:
        """Delete an image."""
        return self._request("DELETE", f"images/{image_uuid}")
    
    def create_image_from_disk(self, image_name: str, vmdisk_uuid: str, 
                                description: str = "") -> dict:
        """
        Create an image from a VM disk.
        
        Args:
            image_name: Name for the new image
            vmdisk_uuid: UUID of the VM disk to clone
            description: Optional description
        
        Returns:
            Created image entity
        """
        payload = {
            "spec": {
                "name": image_name,
                "description": description or f"Migration export of {image_name}",
                "resources": {
                    "image_type": "DISK_IMAGE",
                    "data_source_reference": {
                        "kind": "vm_disk",
                        "uuid": vmdisk_uuid
                    }
                }
            },
            "metadata": {
                "kind": "image"
            }
        }
        return self._request("POST", "images", payload)
    
    def wait_for_image_ready(self, image_uuid: str, timeout: int = 3600, 
                              progress_callback=None) -> bool:
        """
        Wait for image to be ready (COMPLETE state).
        
        Args:
            image_uuid: UUID of the image
            timeout: Max wait time in seconds (default 1 hour)
            progress_callback: Optional callback(state, progress_pct)
        
        Returns:
            True if image is ready, False if timeout or error
        """
        import time
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                image = self.get_image(image_uuid)
                status = image.get('status', {})
                resources = status.get('resources', {})
                
                # PRIMARY CHECK: If size_bytes > 0, image is ready
                size_bytes = resources.get('size_bytes', 0)
                if size_bytes and size_bytes > 0:
                    if progress_callback:
                        progress_callback('COMPLETE', 100)
                    return True
                
                # SECONDARY CHECK: state field
                state = status.get('state', '') or ''
                state = state.upper()
                
                # Progress estimation
                progress = 0
                if 'retrieval_uri_list' in resources:
                    uri_list = resources.get('retrieval_uri_list', [{}])
                    if uri_list:
                        progress = uri_list[0].get('progress_percentage', 0)
                
                if progress_callback:
                    progress_callback(state or 'PENDING', progress)
                
                # Check state-based completion
                if state in ('COMPLETE', 'SUCCEEDED', 'AVAILABLE', 'ACTIVE'):
                    return True
                elif state in ('ERROR', 'FAILED', 'FAILURE'):
                    return False
                
                time.sleep(5)
                
            except Exception as e:
                time.sleep(5)
        
        return False  # Timeout

    def download_image(self, image_uuid: str, dest_path: str, 
                       progress_callback=None, use_aria2=False) -> bool:
        """
        Download image to file.
        
        Args:
            image_uuid: UUID of the image
            dest_path: Destination file path
            progress_callback: Optional callback(downloaded, total) for progress
            use_aria2: Use aria2c (disabled by default - Nutanix doesn't support Range requests)
        
        Returns:
            True if successful
        """
        url = self.get_image_download_url(image_uuid)
        
        # aria2c disabled by default - Nutanix API doesn't support HTTP Range requests
        # so multi-connection doesn't work (CN:1 instead of CN:16), making it slower
        if use_aria2:
            try:
                return self._download_with_aria2(url, dest_path, progress_callback)
            except Exception as e:
                print(f"      aria2c failed ({e}), falling back to Python...")
        
        # Fallback to Python requests
        return self._download_with_requests(url, dest_path, progress_callback)
    
    def _download_with_aria2(self, url: str, dest_path: str, progress_callback=None) -> bool:
        """Download using aria2c for maximum speed."""
        import subprocess
        import os
        import time
        import shutil
        
        # Check if aria2c is available
        if not shutil.which('aria2c'):
            raise Exception("aria2c not installed")
        
        dest_dir = os.path.dirname(dest_path) or '.'
        dest_file = os.path.basename(dest_path)
        
        # Build aria2c command
        cmd = [
            'aria2c',
            '-x16', '-s16',
            '-k1M',
            '--file-allocation=none',
            '--check-certificate=false',
            '--http-user=' + self.auth[0],
            '--http-passwd=' + self.auth[1],
            '-d', dest_dir,
            '-o', dest_file,
            url
        ]
        
        print(f"      Using aria2c (16 parallel connections)...")
        print(f"      URL: {url[:80]}...")
        start_time = time.time()
        
        # Run aria2c directly - output goes to terminal
        returncode = subprocess.call(cmd)
        
        if returncode != 0:
            raise Exception(f"aria2c exited with code {returncode}")
        
        # Verify file exists
        if not os.path.exists(dest_path):
            raise Exception(f"File not created: {dest_path}")
        
        elapsed = time.time() - start_time
        size_gb = os.path.getsize(dest_path) / (1024**3)
        speed = size_gb / elapsed * 1024 if elapsed > 0 else 0
        print(f"\n      Average speed: {speed:.0f} MB/s ({size_gb:.1f} GB in {elapsed:.0f}s)")
        
        return True
    
    def _download_with_requests(self, url: str, dest_path: str, progress_callback=None) -> bool:
        """Download using Python requests (fallback)."""
        response = requests.get(
            url,
            auth=self.auth,
            verify=self.verify_ssl,
            stream=True
        )
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(dest_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192 * 1024):  # 8MB chunks
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)
        
        return True
    
    # === Cluster Operations ===
    
    def get_cluster(self) -> dict:
        """Get cluster information."""
        payload = {"kind": "cluster", "length": 1}
        result = self._request("POST", "clusters/list", payload)
        entities = result.get('entities', [])
        return entities[0] if entities else {}
    
    # === Helper Methods ===
    
    @staticmethod
    def parse_vm_info(vm: dict) -> dict:
        """Parse VM entity to simplified info dict."""
        spec = vm.get('spec', {})
        status = vm.get('status', {})
        resources = spec.get('resources', {})
        metadata = vm.get('metadata', {})
        
        # Calculate vCPU
        num_sockets = resources.get('num_sockets', 1)
        num_vcpus = resources.get('num_vcpus_per_socket', 1)
        
        # Calculate disk info
        disks = resources.get('disk_list', [])
        disk_list = []
        for disk in disks:
            device_props = disk.get('device_properties', {})
            if device_props.get('device_type') == 'DISK':
                disk_list.append({
                    'uuid': disk.get('uuid'),
                    'size_bytes': disk.get('disk_size_bytes', 0) or disk.get('disk_size_mib', 0) * 1024 * 1024,
                    'adapter': device_props.get('disk_address', {}).get('adapter_type'),
                    'index': device_props.get('disk_address', {}).get('device_index'),
                })
        
        # Parse NICs
        nics = resources.get('nic_list', [])
        nic_list = []
        for nic in nics:
            ip_list = nic.get('ip_endpoint_list', [])
            nic_list.append({
                'mac': nic.get('mac_address'),
                'subnet': nic.get('subnet_reference', {}).get('name'),
                'ip': ip_list[0].get('ip') if ip_list else None,
            })
        
        # Boot type
        boot = resources.get('boot_config', {})
        boot_type = "UEFI" if boot.get('boot_type') == 'UEFI' else "BIOS"
        
        return {
            'uuid': metadata.get('uuid'),
            'name': spec.get('name'),
            'power_state': status.get('resources', {}).get('power_state'),
            'vcpu': num_sockets * num_vcpus,
            'num_sockets': num_sockets,
            'num_vcpus_per_socket': num_vcpus,
            'memory_mb': resources.get('memory_size_mib', 0),
            'boot_type': boot_type,
            'disks': disk_list,
            'nics': nic_list,
        }
    
    # === NFS Fast Transfer Methods ===
    
    def get_vm_vdisks_v2(self, vm_name: str) -> List[Dict]:
        """
        Get VM vdisk info via API v2 (returns vmdisk_uuid for NFS access).
        
        Args:
            vm_name: VM name
            
        Returns:
            List of dicts with uuid, nfs_path, container, size
        """
        # Use v2 API to get all virtual disks
        url = f"https://{self.prism_ip}:9440/PrismGateway/services/rest/v2.0/virtual_disks/"
        
        response = requests.get(
            url,
            auth=self.auth,
            verify=self.verify_ssl
        )
        response.raise_for_status()
        
        all_disks = response.json().get('entities', [])
        
        # Filter by attached VM name
        vm_disks = []
        for disk in all_disks:
            if disk.get('attached_vmname') == vm_name:
                nfs_path = disk.get('nutanix_nfsfile_path', '')
                # Extract container from path like "/container01/.acropolis/vmdisk/uuid"
                parts = nfs_path.split('/')
                container = parts[1] if len(parts) > 1 else ''
                
                vm_disks.append({
                    'uuid': disk.get('uuid'),  # This is the NFS filename
                    'nfs_path': nfs_path,
                    'container': container,
                    'size_bytes': disk.get('disk_capacity_in_bytes', 0),
                    'device_uuid': disk.get('device_uuid'),
                    'disk_address': disk.get('disk_address', '')
                })
        
        # Sort by disk_address (scsi.0, scsi.1, etc.)
        vm_disks.sort(key=lambda x: x.get('disk_address', ''))
        
        return vm_disks
    
    def mount_nfs_container(self, container_name: str, mount_path: str = None) -> str:
        """
        Mount Nutanix container via NFS.
        
        Args:
            container_name: Name of the container (e.g., 'container01')
            mount_path: Optional mount point (default: self.nfs_mount_path)
            
        Returns:
            Mount path
        """
        mount_path = mount_path or self.nfs_mount_path
        
        # Check if already mounted
        if os.path.ismount(mount_path):
            # Verify it's the right container
            test_path = os.path.join(mount_path, '.acropolis', 'vmdisk')
            if os.path.exists(test_path):
                return mount_path
            # Wrong mount, unmount first
            subprocess.run(['umount', mount_path], capture_output=True)
        
        # Create mount point
        os.makedirs(mount_path, exist_ok=True)
        
        # Mount NFS
        nfs_source = f"{self.prism_ip}:/{container_name}"
        cmd = ['mount', '-t', 'nfs', nfs_source, mount_path]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to mount NFS: {result.stderr}")
        
        return mount_path
    
    def unmount_nfs(self, mount_path: str = None):
        """Unmount NFS container."""
        mount_path = mount_path or self.nfs_mount_path
        if os.path.ismount(mount_path):
            subprocess.run(['umount', mount_path], capture_output=True)
    
    def copy_vdisk_nfs(self, vmdisk_uuid: str, container_name: str, 
                       dest_path: str, progress_callback=None) -> bool:
        """
        Copy vdisk via NFS (much faster than API).
        
        Args:
            vmdisk_uuid: The vmdisk UUID (from acli, not API)
            container_name: Container name
            dest_path: Destination file path
            progress_callback: Optional callback(copied_bytes, total_bytes, speed_mbps)
            
        Returns:
            True if successful
        """
        # Ensure NFS is mounted
        mount_path = self.mount_nfs_container(container_name)
        
        # Source path
        src_path = os.path.join(mount_path, '.acropolis', 'vmdisk', vmdisk_uuid)
        
        if not os.path.exists(src_path):
            raise Exception(f"vdisk not found: {src_path}")
        
        total_size = os.path.getsize(src_path)
        
        # Copy with progress
        start_time = time.time()
        copied = 0
        chunk_size = 64 * 1024 * 1024  # 64MB chunks for speed
        last_update = start_time
        last_copied = 0
        
        with open(src_path, 'rb') as src, open(dest_path, 'wb') as dst:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                
                # Progress update every second
                now = time.time()
                if progress_callback and (now - last_update) >= 1.0:
                    speed = (copied - last_copied) / (now - last_update) / (1024 * 1024)
                    progress_callback(copied, total_size, speed)
                    last_update = now
                    last_copied = copied
        
        # Final progress
        elapsed = time.time() - start_time
        if progress_callback and elapsed > 0:
            avg_speed = copied / elapsed / (1024 * 1024)
            progress_callback(copied, total_size, avg_speed)
        
        return True
    
    def export_vm_disks_nfs(self, vm_name: str, dest_dir: str, 
                            progress_callback=None) -> List[Dict]:
        """
        Export all VM disks via NFS (fast method).
        
        Args:
            vm_name: VM name
            dest_dir: Destination directory
            progress_callback: Optional callback(disk_index, filename, copied, total, speed)
            
        Returns:
            List of exported disk info
        """
        # Get vdisks via SSH
        vdisks = self.get_vm_vdisks_ssh(vm_name)
        
        if not vdisks:
            raise Exception(f"No vdisks found for VM {vm_name}")
        
        exported = []
        
        for idx, vdisk in enumerate(vdisks):
            filename = f"{vm_name}-disk{idx}.raw"
            dest_path = os.path.join(dest_dir, filename)
            
            def disk_progress(copied, total, speed):
                if progress_callback:
                    progress_callback(idx, filename, copied, total, speed)
            
            self.copy_vdisk_nfs(
                vdisk['uuid'],
                vdisk['container'],
                dest_path,
                progress_callback=disk_progress
            )
            
            exported.append({
                'index': idx,
                'filename': filename,
                'path': dest_path,
                'size_bytes': vdisk['size_bytes'],
                'vmdisk_uuid': vdisk['uuid']
            })
        
        return exported
