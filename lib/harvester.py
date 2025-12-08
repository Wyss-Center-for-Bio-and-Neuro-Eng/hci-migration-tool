"""
Harvester/KubeVirt API Client
"""

import requests
import urllib3
import base64
import tempfile
import os
from typing import Optional, List, Dict, Any

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HarvesterClient:
    """Harvester/KubeVirt API client using Kubernetes REST API."""
    
    def __init__(self, config: dict):
        """
        Initialize Harvester client.
        
        Args:
            config: Dictionary with api_url, namespace, and certificate data
        """
        self.base_url = config['api_url']
        self.namespace = config.get('namespace', 'default')
        self.verify_ssl = config.get('verify_ssl', False)
        self._temp_files = []
        self.cert = None
        self.verify = False
        self._setup_certs(config)
    
    def _setup_certs(self, config: dict):
        """Configure certificates from config."""
        # CA Certificate
        ca_data = config.get('certificate_authority_data')
        if ca_data:
            ca_file = tempfile.NamedTemporaryFile(delete=False, suffix='.crt')
            ca_file.write(base64.b64decode(ca_data))
            ca_file.close()
            self._temp_files.append(ca_file.name)
            self.verify = ca_file.name
        
        # Client Certificate and Key
        cert_data = config.get('client_certificate_data')
        key_data = config.get('client_key_data')
        
        if cert_data and key_data:
            cert_file = tempfile.NamedTemporaryFile(delete=False, suffix='.crt')
            cert_file.write(base64.b64decode(cert_data))
            cert_file.close()
            self._temp_files.append(cert_file.name)
            
            key_file = tempfile.NamedTemporaryFile(delete=False, suffix='.key')
            key_file.write(base64.b64decode(key_data))
            key_file.close()
            self._temp_files.append(key_file.name)
            
            self.cert = (cert_file.name, key_file.name)
    
    def __del__(self):
        """Cleanup temporary certificate files."""
        for f in self._temp_files:
            try:
                os.unlink(f)
            except:
                pass
    
    def _request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Execute API request."""
        url = f"{self.base_url}{endpoint}"
        
        headers = {}
        if method == "PATCH":
            headers['Content-Type'] = 'application/merge-patch+json'
        
        response = requests.request(
            method=method,
            url=url,
            json=data,
            headers=headers if headers else None,
            cert=self.cert,
            verify=self.verify if self.verify else False
        )
        
        if not response.ok:
            # Try to get detailed error message
            try:
                error_detail = response.json()
                error_msg = error_detail.get('message', response.text)
                print(f"API Error: {error_msg}")
            except:
                print(f"API Error: {response.text}")
        
        response.raise_for_status()
        return response.json() if response.text else {}
    
    # === Node Operations ===
    
    def get_nodes(self) -> List[dict]:
        """List cluster nodes."""
        result = self._request("GET", "/api/v1/nodes")
        return result.get('items', [])
    
    # === VM Operations ===
    
    def list_vms(self, namespace: str = None) -> List[dict]:
        """List VMs in a namespace."""
        ns = namespace or self.namespace
        result = self._request("GET", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines")
        return result.get('items', [])
    
    def list_all_vms(self) -> List[dict]:
        """List all VMs across all namespaces."""
        result = self._request("GET", "/apis/kubevirt.io/v1/virtualmachines")
        return result.get('items', [])
    
    def get_vm(self, name: str, namespace: str = None) -> dict:
        """Get VM by name."""
        ns = namespace or self.namespace
        return self._request("GET", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}")
    
    def create_vm(self, manifest: dict) -> dict:
        """Create VM from manifest."""
        ns = manifest.get('metadata', {}).get('namespace', self.namespace)
        return self._request("POST", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines", manifest)
    
    def delete_vm(self, name: str, namespace: str = None) -> dict:
        """Delete VM."""
        ns = namespace or self.namespace
        return self._request("DELETE", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}")
    
    def start_vm(self, name: str, namespace: str = None) -> dict:
        """Start VM using KubeVirt subresources API."""
        ns = namespace or self.namespace
        # Use subresources.kubevirt.io API for start/stop/restart
        url = f"{self.base_url}/apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/start"
        
        response = requests.put(
            url,
            json={},  # Empty body for start
            cert=self.cert,
            verify=self.verify if self.verify else False
        )
        response.raise_for_status()
        return response.json() if response.text else {}
    
    def stop_vm(self, name: str, namespace: str = None) -> dict:
        """Stop VM using KubeVirt subresources API."""
        ns = namespace or self.namespace
        # Use subresources.kubevirt.io API for start/stop/restart
        url = f"{self.base_url}/apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/stop"
        
        response = requests.put(
            url,
            json={},  # Empty body for stop
            cert=self.cert,
            verify=self.verify if self.verify else False
        )
        response.raise_for_status()
        return response.json() if response.text else {}
    
    def restart_vm(self, name: str, namespace: str = None) -> dict:
        """Restart VM using KubeVirt subresources API."""
        ns = namespace or self.namespace
        url = f"{self.base_url}/apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/restart"
        
        response = requests.put(
            url,
            json={},
            cert=self.cert,
            verify=self.verify if self.verify else False
        )
        response.raise_for_status()
        return response.json() if response.text else {}
    
    # === VMI Operations (Running Instances) ===
    
    def list_all_vmis(self) -> List[dict]:
        """List all VirtualMachineInstances (running VMs)."""
        result = self._request("GET", "/apis/kubevirt.io/v1/virtualmachineinstances")
        return result.get('items', [])
    
    def list_vmis(self, namespace: str = None) -> List[dict]:
        """List VMIs in a namespace."""
        ns = namespace or self.namespace
        result = self._request("GET", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances")
        return result.get('items', [])
    
    def get_vmi(self, name: str, namespace: str = None) -> Optional[dict]:
        """Get VMI by name (returns None if not running)."""
        ns = namespace or self.namespace
        try:
            return self._request("GET", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}")
        except:
            return None
    
    # === Image Operations ===
    
    def list_images(self, namespace: str = None) -> List[dict]:
        """List images in a namespace."""
        ns = namespace or self.namespace
        result = self._request("GET", f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages")
        return result.get('items', [])
    
    def list_all_images(self) -> List[dict]:
        """List all images across all namespaces."""
        result = self._request("GET", "/apis/harvesterhci.io/v1beta1/virtualmachineimages")
        return result.get('items', [])
    
    def get_image(self, name: str, namespace: str = None) -> dict:
        """Get image by name."""
        ns = namespace or self.namespace
        return self._request("GET", f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages/{name}")
    
    def create_image(self, name: str, url: str, display_name: str = None, 
                     namespace: str = None) -> dict:
        """Create image from URL."""
        ns = namespace or self.namespace
        image_manifest = {
            "apiVersion": "harvesterhci.io/v1beta1",
            "kind": "VirtualMachineImage",
            "metadata": {
                "name": name,
                "namespace": ns
            },
            "spec": {
                "displayName": display_name or name,
                "sourceType": "download",
                "url": url
            }
        }
        return self._request("POST", f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages", image_manifest)
    
    def delete_image(self, name: str, namespace: str = None) -> dict:
        """Delete image."""
        ns = namespace or self.namespace
        return self._request("DELETE", f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages/{name}")
    
    # === Network Operations ===
    
    def list_networks(self, namespace: str = None) -> List[dict]:
        """List network attachment definitions in a namespace."""
        ns = namespace or self.namespace
        result = self._request("GET", f"/apis/k8s.cni.cncf.io/v1/namespaces/{ns}/network-attachment-definitions")
        return result.get('items', [])
    
    def list_all_networks(self) -> List[dict]:
        """List all networks across all namespaces."""
        result = self._request("GET", "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions")
        return result.get('items', [])
    
    # === Storage Operations ===
    
    def list_storage_classes(self) -> List[dict]:
        """List storage classes."""
        result = self._request("GET", "/apis/storage.k8s.io/v1/storageclasses")
        return result.get('items', [])
    
    def list_pvcs(self, namespace: str = None) -> List[dict]:
        """List persistent volume claims."""
        ns = namespace or self.namespace
        result = self._request("GET", f"/api/v1/namespaces/{ns}/persistentvolumeclaims")
        return result.get('items', [])
    
    def list_all_pvcs(self) -> List[dict]:
        """List all persistent volume claims across namespaces."""
        result = self._request("GET", "/api/v1/persistentvolumeclaims")
        return result.get('items', [])
    
    def delete_pvc(self, name: str, namespace: str = None) -> dict:
        """Delete a PersistentVolumeClaim (volume)."""
        ns = namespace or self.namespace
        return self._request("DELETE", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    
    def clone_pvc(self, source_name: str, clone_name: str, namespace: str = None, storage_class: str = None) -> dict:
        """Clone a PVC using CSI volume cloning."""
        ns = namespace or self.namespace
        
        # Get source PVC to copy settings
        source_pvc = self._request("GET", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{source_name}")
        source_size = source_pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', '10Gi')
        source_sc = storage_class or source_pvc.get('spec', {}).get('storageClassName', 'harvester-longhorn')
        access_modes = source_pvc.get('spec', {}).get('accessModes', ['ReadWriteMany'])
        volume_mode = source_pvc.get('spec', {}).get('volumeMode', 'Block')
        
        # Create clone PVC with dataSource pointing to original
        clone_manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": clone_name,
                "namespace": ns
            },
            "spec": {
                "storageClassName": source_sc,
                "dataSource": {
                    "name": source_name,
                    "kind": "PersistentVolumeClaim"
                },
                "accessModes": access_modes,
                "volumeMode": volume_mode,
                "resources": {
                    "requests": {
                        "storage": source_size
                    }
                }
            }
        }
        
        return self._request("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", clone_manifest)
    
    def get_pvc(self, name: str, namespace: str = None) -> dict:
        """Get a specific PVC."""
        ns = namespace or self.namespace
        return self._request("GET", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    
    def update_vm_volume(self, vm_name: str, old_volume_name: str, new_volume_name: str, namespace: str = None) -> dict:
        """Update VM to use a different volume."""
        ns = namespace or self.namespace
        
        # Get current VM
        vm = self.get_vm(vm_name, ns)
        
        # Update dataVolumeTemplates and volumes to point to new volume
        spec = vm.get('spec', {})
        template_spec = spec.get('template', {}).get('spec', {})
        
        # Update volumes section
        volumes = template_spec.get('volumes', [])
        for vol in volumes:
            if vol.get('dataVolume', {}).get('name') == old_volume_name:
                vol['dataVolume']['name'] = new_volume_name
            elif vol.get('persistentVolumeClaim', {}).get('claimName') == old_volume_name:
                vol['persistentVolumeClaim']['claimName'] = new_volume_name
        
        # Remove dataVolumeTemplates (since we're using existing PVC now)
        if 'dataVolumeTemplates' in spec:
            spec['dataVolumeTemplates'] = [
                dvt for dvt in spec['dataVolumeTemplates'] 
                if dvt.get('metadata', {}).get('name') != old_volume_name
            ]
        
        # Change volume reference to persistentVolumeClaim instead of dataVolume
        for vol in volumes:
            if vol.get('dataVolume', {}).get('name') == new_volume_name:
                vol['persistentVolumeClaim'] = {'claimName': new_volume_name}
                del vol['dataVolume']
        
        # Update the VM
        return self._request("PUT", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{vm_name}", vm)
    
    # === Helper Methods ===
    
    def get_vm_status(self, name: str, namespace: str = None) -> str:
        """Get actual VM status (Running, Stopped, Starting, etc.)."""
        ns = namespace or self.namespace
        try:
            vm = self.get_vm(name, ns)
            status = vm.get('status', {})
            printable = status.get('printableStatus', '')
            if printable:
                return printable
            
            # Check if VMI exists
            vmi = self.get_vmi(name, ns)
            if vmi:
                return "Running"
            return "Stopped"
        except:
            return "Unknown"
    
    @staticmethod
    def parse_vm_info(vm: dict) -> dict:
        """Parse VM to simplified info dict."""
        metadata = vm.get('metadata', {})
        spec = vm.get('spec', {})
        status = vm.get('status', {})
        
        template = spec.get('template', {}).get('spec', {})
        domain = template.get('domain', {})
        
        return {
            'name': metadata.get('name'),
            'namespace': metadata.get('namespace'),
            'running': spec.get('running', False),
            'status': status.get('printableStatus', 'Unknown'),
            'cpu_cores': domain.get('cpu', {}).get('cores'),
            'memory': domain.get('memory', {}).get('guest'),
        }
    
    def generate_vm_manifest(self, name: str, cpu_cores: int, memory_gb: int,
                             image_name: str, network_name: str,
                             storage_class: str = "harvester-longhorn",
                             namespace: str = None,
                             boot_type: str = "BIOS") -> dict:
        """
        Generate a VM manifest for Harvester.
        
        Args:
            name: VM name
            cpu_cores: Number of CPU cores
            memory_gb: Memory in GB
            image_name: Name of the VM image to use
            network_name: Network attachment definition name
            storage_class: Storage class for the root disk
            namespace: Kubernetes namespace
            boot_type: BIOS or UEFI
        
        Returns:
            VM manifest dict
        """
        ns = namespace or self.namespace
        
        # Firmware config
        firmware = {}
        if boot_type == "UEFI":
            firmware = {
                "bootloader": {
                    "efi": {
                        "secureBoot": False
                    }
                }
            }
        
        manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": name,
                "namespace": ns
            },
            "spec": {
                "running": False,
                "template": {
                    "metadata": {
                        "labels": {
                            "kubevirt.io/vm": name
                        }
                    },
                    "spec": {
                        "domain": {
                            "cpu": {
                                "cores": cpu_cores
                            },
                            "memory": {
                                "guest": f"{memory_gb}Gi"
                            },
                            "devices": {
                                "disks": [
                                    {
                                        "name": "rootdisk",
                                        "disk": {
                                            "bus": "virtio"
                                        }
                                    }
                                ],
                                "interfaces": [
                                    {
                                        "name": "default",
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
                                "name": "default",
                                "multus": {
                                    "networkName": network_name
                                }
                            }
                        ],
                        "volumes": [
                            {
                                "name": "rootdisk",
                                "dataVolume": {
                                    "name": f"{name}-rootdisk"
                                }
                            }
                        ]
                    }
                },
                "dataVolumeTemplates": [
                    {
                        "metadata": {
                            "name": f"{name}-rootdisk"
                        },
                        "spec": {
                            "source": {
                                "http": {
                                    "url": ""  # Will be filled with image URL
                                }
                            },
                            "pvc": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {
                                    "requests": {
                                        "storage": "50Gi"  # Adjust as needed
                                    }
                                },
                                "storageClassName": storage_class
                            }
                        }
                    }
                ]
            }
        }
        
        if firmware:
            manifest['spec']['template']['spec']['domain']['firmware'] = firmware
        
        return manifest
