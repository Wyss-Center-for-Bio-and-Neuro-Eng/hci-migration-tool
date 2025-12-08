"""
Nutanix Prism API Client
"""

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
        """
        self.base_url = f"https://{config['prism_ip']}:9440/api/nutanix/v3"
        self.auth = (config['username'], config['password'])
        self.verify_ssl = config.get('verify_ssl', False)
        self.prism_ip = config['prism_ip']
    
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
        first_check = True
        
        while time.time() - start < timeout:
            try:
                image = self.get_image(image_uuid)
                status = image.get('status', {})
                resources = status.get('resources', {})
                
                # Debug on first check
                if first_check:
                    first_check = False
                    print(f"\n      [DEBUG] API response status.state = '{status.get('state')}'")
                    print(f"      [DEBUG] status.resources.size_bytes = {resources.get('size_bytes', 'N/A')}")
                
                # PRIMARY CHECK: If size_bytes > 0, image is ready
                # This is the most reliable indicator for Prism Element
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
                if first_check:
                    print(f"\n      [DEBUG] Exception: {e}")
                time.sleep(5)
        
        return False  # Timeout

    def download_image(self, image_uuid: str, dest_path: str, 
                       progress_callback=None) -> bool:
        """
        Download image to file.
        
        Args:
            image_uuid: UUID of the image
            dest_path: Destination file path
            progress_callback: Optional callback(downloaded, total) for progress
        
        Returns:
            True if successful
        """
        url = self.get_image_download_url(image_uuid)
        
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
