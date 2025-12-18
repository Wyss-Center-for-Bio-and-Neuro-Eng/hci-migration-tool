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
    
    def _request(self, method: str, endpoint: str, data: dict = None, content_type: str = None, silent: bool = False) -> dict:
        """Execute API request."""
        url = f"{self.base_url}{endpoint}"
        
        headers = {}
        if method == "PATCH":
            # Use provided content_type or default to merge-patch
            headers['Content-Type'] = content_type or 'application/merge-patch+json'
        
        # For JSON Patch, send data directly (not as json=)
        if content_type == "application/json-patch+json":
            import json as json_module
            response = requests.request(
                method=method,
                url=url,
                data=json_module.dumps(data),
                headers=headers,
                cert=self.cert,
                verify=self.verify if self.verify else False
            )
        else:
            response = requests.request(
                method=method,
                url=url,
                json=data,
                headers=headers if headers else None,
                cert=self.cert,
                verify=self.verify if self.verify else False
            )
        
        if not response.ok and not silent:
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
    
    def stop_vm(self, name: str, namespace: str = None) -> dict:
        """Stop VM by setting runStrategy to Halted."""
        ns = namespace or self.namespace
        # Patch runStrategy to Halted
        patch = {"spec": {"runStrategy": "Halted"}}
        return self._request(
            "PATCH", 
            f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
            patch
        )
    
    def start_vm(self, name: str, namespace: str = None) -> dict:
        """Start VM by setting runStrategy to RerunOnFailure."""
        ns = namespace or self.namespace
        # Patch runStrategy to RerunOnFailure (this starts the VM)
        patch = {"spec": {"runStrategy": "RerunOnFailure"}}
        return self._request(
            "PATCH", 
            f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
            patch
        )
    
    def get_vmi(self, name: str, namespace: str = None, silent: bool = False) -> dict:
        """Get VirtualMachineInstance (running VM) by name."""
        ns = namespace or self.namespace
        url = f"{self.base_url}/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}"
        
        response = requests.get(
            url,
            cert=self.cert,
            verify=self.verify if self.verify else False
        )
        
        if not response.ok:
            if not silent:
                try:
                    error_detail = response.json()
                    error_msg = error_detail.get('message', response.text)
                    print(f"API Error: {error_msg}")
                except:
                    print(f"API Error: {response.text}")
            response.raise_for_status()
        
        return response.json() if response.text else {}
    
    def get_vm_ip(self, name: str, namespace: str = None) -> List[dict]:
        """
        Get VM IP addresses from VMI status.
        Tries multiple sources: Harvester v1 API and KubeVirt API.
        Returns list of interfaces with IP info.
        """
        ns = namespace or self.namespace
        result = []
        
        # Try Harvester v1 API first (used by UI)
        try:
            url = f"{self.base_url}/v1/kubevirt.io.virtualmachineinstances/{ns}/{name}"
            response = requests.get(
                url,
                cert=self.cert,
                verify=self.verify if self.verify else False
            )
            if response.ok:
                vmi = response.json()
                interfaces = vmi.get('status', {}).get('interfaces', [])
                for iface in interfaces:
                    ip = iface.get('ipAddress', '')
                    # Clean up IP if it has CIDR notation
                    if '/' in ip:
                        ip = ip.split('/')[0]
                    result.append({
                        'name': iface.get('name', ''),
                        'mac': iface.get('mac', ''),
                        'ip': ip,
                        'ips': iface.get('ipAddresses', []),
                        'infoSource': iface.get('infoSource', '')
                    })
                if result:
                    return result
        except:
            pass
        
        # Fallback to KubeVirt API
        try:
            vmi = self.get_vmi(name, ns, silent=True)
            interfaces = vmi.get('status', {}).get('interfaces', [])
            
            for iface in interfaces:
                ip = iface.get('ipAddress', '')
                if '/' in ip:
                    ip = ip.split('/')[0]
                result.append({
                    'name': iface.get('name', ''),
                    'mac': iface.get('mac', ''),
                    'ip': ip,
                    'ips': iface.get('ipAddresses', []),
                    'infoSource': iface.get('infoSource', '')
                })
            return result
        except Exception as e:
            return []
    
    def wait_for_vm_ip(self, name: str, namespace: str = None, timeout: int = 300) -> str:
        """
        Wait for VM to get an IP address from QEMU guest agent.
        Returns the first non-empty IP found, or empty string on timeout.
        """
        import time
        ns = namespace or self.namespace
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            interfaces = self.get_vm_ip(name, ns)
            for iface in interfaces:
                if iface.get('ip'):
                    return iface['ip']
            time.sleep(5)
        
        return ""
    
    
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
                     namespace: str = None, storage_class: str = None) -> dict:
        """Create image from URL.
        
        Args:
            name: Image name
            url: URL to download image from
            display_name: Display name for the image
            namespace: Target namespace
            storage_class: StorageClass to use as TEMPLATE for the image.
                          Harvester will create longhorn-<image-name> that inherits
                          parameters (replicas, node selectors, etc.) from this class.
        """
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
        
        # Add storageClass template if specified
        if storage_class:
            image_manifest["spec"]["storageClassParameters"] = {
                "storageClassName": storage_class
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
    
    def get_pvc_actual_size(self, pvc_name: str, namespace: str = None) -> int:
        """Get the actual data size of a PVC from Longhorn.
        Returns actualSize in bytes, or 0 if not found/not ready."""
        ns = namespace or self.namespace
        try:
            # Get the PVC to find the bound PV name
            pvc = self.get_pvc(pvc_name, ns)
            pv_name = pvc.get('spec', {}).get('volumeName', '')
            if not pv_name:
                return 0
            
            # Get the Longhorn volume (name matches PV name)
            lh_volume = self._request("GET", f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/{pv_name}")
            actual_size = lh_volume.get('status', {}).get('actualSize', 0)
            return int(actual_size) if actual_size else 0
        except Exception:
            return 0
    
    def create_pvc_from_image(self, pvc_name: str, image_name: str, image_namespace: str,
                              size_gi: int, namespace: str = None) -> dict:
        """Create a PVC from a Harvester image using the image's auto-created storageClass."""
        ns = namespace or self.namespace
        
        # The storageClass auto-created by Harvester for the image
        storage_class = f"longhorn-{image_name}"
        
        pvc_manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc_name,
                "namespace": ns,
                "annotations": {
                    "harvesterhci.io/imageId": f"{image_namespace}/{image_name}"
                }
            },
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "volumeMode": "Block",
                "storageClassName": storage_class,
                "resources": {
                    "requests": {
                        "storage": f"{size_gi}Gi"
                    }
                }
            }
        }
        
        return self._request("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", pvc_manifest)
    
    def clone_pvc_to_storage_class(self, source_name: str, clone_name: str, 
                                    target_storage_class: str, size_gi: int,
                                    namespace: str = None) -> dict:
        """Clone a PVC to a new PVC with a specific storage class."""
        ns = namespace or self.namespace
        
        clone_manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": clone_name,
                "namespace": ns
            },
            "spec": {
                "storageClassName": target_storage_class,
                "dataSource": {
                    "name": source_name,
                    "kind": "PersistentVolumeClaim"
                },
                "accessModes": ["ReadWriteMany"],
                "volumeMode": "Block",
                "resources": {
                    "requests": {
                        "storage": f"{size_gi}Gi"
                    }
                }
            }
        }
        
        return self._request("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", clone_manifest)
    
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
    
    # =========================================================================
    # CDI DataVolume Methods - For creating independent volumes (no backing image)
    # =========================================================================
    
    def create_datavolume(self, name: str, http_url: str, size_gi: int, 
                          storage_class: str, namespace: str = None) -> dict:
        """
        Create a DataVolume with HTTP source - creates a 100% independent PVC.
        
        CDI will:
        - Download the image from http_url
        - Convert qcow2 to raw format automatically
        - Populate the PVC with the data
        
        The resulting PVC has NO dependency on any Harvester image or backing image.
        
        Args:
            name: DataVolume/PVC name
            http_url: URL to download the qcow2/raw image from
            size_gi: Size in GiB (must be >= image virtual size)
            storage_class: StorageClass to use (e.g. harvester-longhorn-dual-node)
            namespace: Target namespace
            
        Returns:
            Created DataVolume object
        """
        ns = namespace or self.namespace
        
        dv_manifest = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": name,
                "namespace": ns,
                "annotations": {
                    # Force immediate binding (don't wait for consumer)
                    "cdi.kubevirt.io/storage.bind.immediate.requested": "true"
                }
            },
            "spec": {
                "source": {
                    "http": {
                        "url": http_url
                    }
                },
                "storage": {
                    "storageClassName": storage_class,
                    "accessModes": ["ReadWriteMany"],
                    "volumeMode": "Block",
                    "resources": {
                        "requests": {
                            "storage": f"{size_gi}Gi"
                        }
                    }
                }
            }
        }
        
        return self._request("POST", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes", dv_manifest)
    
    def get_datavolume(self, name: str, namespace: str = None, silent: bool = False) -> dict:
        """Get a DataVolume by name."""
        ns = namespace or self.namespace
        return self._request("GET", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{name}", silent=silent)
    
    def list_datavolumes(self, namespace: str = None) -> List[dict]:
        """List DataVolumes in a namespace."""
        ns = namespace or self.namespace
        result = self._request("GET", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes")
        return result.get('items', [])
    
    def delete_datavolume(self, name: str, namespace: str = None) -> dict:
        """Delete a DataVolume (also deletes the associated PVC)."""
        ns = namespace or self.namespace
        return self._request("DELETE", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{name}")
    
    def get_datavolume_status(self, name: str, namespace: str = None) -> dict:
        """
        Get DataVolume status information.
        
        Returns dict with:
            - phase: Pending, ImportScheduled, ImportInProgress, Succeeded, Failed, etc.
            - progress: e.g. "45.5%" during import
            - conditions: list of conditions
        """
        ns = namespace or self.namespace
        try:
            dv = self.get_datavolume(name, ns)
            status = dv.get('status', {})
            return {
                'phase': status.get('phase', 'Unknown'),
                'progress': status.get('progress', 'N/A'),
                'conditions': status.get('conditions', [])
            }
        except Exception as e:
            return {
                'phase': 'Error',
                'progress': 'N/A',
                'error': str(e)
            }
    
    def wait_datavolume_ready(self, name: str, namespace: str = None, 
                               timeout: int = 1800, progress_callback=None) -> bool:
        """
        Wait for DataVolume to reach Succeeded state.
        
        Args:
            name: DataVolume name
            namespace: Namespace
            timeout: Max wait time in seconds (default 30 min)
            progress_callback: Optional function(phase, progress) called on updates
            
        Returns:
            True if Succeeded, False if Failed or timeout
        """
        import time
        ns = namespace or self.namespace
        start_time = time.time()
        last_progress = ""
        
        while time.time() - start_time < timeout:
            status = self.get_datavolume_status(name, ns)
            phase = status.get('phase', 'Unknown')
            progress = status.get('progress', '')
            
            # Call progress callback if provided
            if progress_callback and progress != last_progress:
                progress_callback(phase, progress)
                last_progress = progress
            
            if phase == 'Succeeded':
                return True
            elif phase in ('Failed', 'Error'):
                return False
            
            time.sleep(5)
        
        return False  # Timeout
    
    # =========================================================================
    # Sparse Import Methods - For efficient import of large sparse disks
    # =========================================================================
    
    def create_empty_block_pvc(self, name: str, size_gi: int, storage_class: str,
                                namespace: str = None) -> dict:
        """
        Create an empty PVC in Block mode for sparse import.
        
        Args:
            name: PVC name
            size_gi: Size in GiB
            storage_class: StorageClass (e.g. harvester-longhorn-dual-node)
            namespace: Target namespace
            
        Returns:
            Created PVC object
        """
        ns = namespace or self.namespace
        
        pvc_manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": name,
                "namespace": ns
            },
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": storage_class,
                "volumeMode": "Block",
                "resources": {
                    "requests": {
                        "storage": f"{size_gi}Gi"
                    }
                }
            }
        }
        
        return self._request("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", pvc_manifest)
    
    def create_importer_pod(self, pod_name: str, pvc_name: str, 
                            nfs_server: str, nfs_path: str, qcow2_file: str,
                            namespace: str = None) -> dict:
        """
        Create a pod that imports a QCOW2 file to a PVC using sparse-aware segment copy.
        
        Uses qemu-img map to identify only data segments, then copies them directly
        without reading through zero/sparse regions. This is critical for very sparse
        disks (e.g., 1TB virtual with only 2.5MB data).
        
        The pod will:
        1. Mount NFS staging (read-write for temp file)
        2. Mount PVC as block device
        3. Use qemu-img map to identify data segments in QCOW2
        4. Convert QCOW2 to sparse RAW file
        5. Copy ONLY the data segments to block device (skip zeros entirely)
        6. Cleanup temp file
        
        Args:
            pod_name: Name for the importer pod
            pvc_name: Target PVC name (must exist)
            nfs_server: NFS server IP
            nfs_path: NFS export path (e.g. /mnt/data)
            qcow2_file: Path to QCOW2 file relative to NFS root
            namespace: Target namespace
            
        Returns:
            Created Pod object
        """
        ns = namespace or self.namespace
        
        # Direct QCOW2 to block device conversion using qemu-img
        # qemu-img convert writes directly to block device - no intermediate file needed!
        # Longhorn handles thin provisioning, so zeros don't consume storage space
        convert_cmd = f"""
set -e
apt-get update > /dev/null 2>&1
apt-get install -y qemu-utils jq > /dev/null 2>&1

QCOW2_FILE="/staging/{qcow2_file}"
BLOCK_DEV="/dev/target"

echo "=== Step 1/2: Analyzing disk ==="
VIRT_SIZE=$(qemu-img info --output=json "$QCOW2_FILE" | jq -r '."virtual-size"')
ACTUAL_SIZE=$(qemu-img info --output=json "$QCOW2_FILE" | jq -r '."actual-size"')
VIRT_SIZE_GB=$((VIRT_SIZE / 1024 / 1024 / 1024))
ACTUAL_SIZE_MB=$((ACTUAL_SIZE / 1024 / 1024))
echo "   Virtual size: $VIRT_SIZE_GB GB"
echo "   QCOW2 actual data: $ACTUAL_SIZE_MB MB"

echo ""
echo "=== Step 2/2: Converting QCOW2 to block device ==="
echo "   Direct conversion (no intermediate file)..."

START_TIME=$(date +%s)

# Run qemu-img in background
qemu-img convert -f qcow2 -O raw "$QCOW2_FILE" "$BLOCK_DEV" &
PID=$!

# Wait a moment for qemu-img to open files
sleep 2

# Find the file descriptor for the QCOW2 file (look for the .qcow2 in /proc/PID/fd)
QCOW2_FD=""
for fd in /proc/$PID/fd/*; do
    if [ -L "$fd" ]; then
        target=$(readlink "$fd" 2>/dev/null || true)
        if echo "$target" | grep -q ".qcow2"; then
            QCOW2_FD=$(basename "$fd")
            break
        fi
    fi
done

if [ -z "$QCOW2_FD" ]; then
    echo "   Warning: Could not find QCOW2 file descriptor, progress will not be shown"
    QCOW2_FD="0"  # fallback, won't show real progress
fi

# Monitor progress by checking /proc/PID/fdinfo for read position on QCOW2 file
while kill -0 $PID 2>/dev/null; do
    sleep 3
    if [ -f /proc/$PID/fdinfo/$QCOW2_FD ]; then
        POS=$(grep -E '^pos:' /proc/$PID/fdinfo/$QCOW2_FD 2>/dev/null | awk '{{print $2}}')
        if [ -n "$POS" ] && [ "$ACTUAL_SIZE" -gt 0 ] && [ "$POS" -gt 0 ]; then
            POS_MB=$((POS / 1024 / 1024))
            PCT=$((POS * 100 / ACTUAL_SIZE))
            # Cap at 100%
            if [ $PCT -gt 100 ]; then PCT=100; fi
            ELAPSED=$(($(date +%s) - START_TIME))
            if [ $ELAPSED -gt 0 ]; then
                SPEED=$((POS_MB / ELAPSED))
                if [ $SPEED -gt 0 ]; then
                    REMAINING=$(( (ACTUAL_SIZE_MB - POS_MB) / SPEED ))
                else
                    REMAINING=0
                fi
                echo "   Progress: $POS_MB / $ACTUAL_SIZE_MB MB ($PCT%) - $SPEED MB/s - ETA: ${{REMAINING}}s"
            else
                echo "   Progress: $POS_MB / $ACTUAL_SIZE_MB MB ($PCT%)"
            fi
        fi
    fi
done

wait $PID
EXIT_CODE=$?

sync

ELAPSED=$(($(date +%s) - START_TIME))
if [ $ELAPSED -gt 0 ]; then
    SPEED=$((ACTUAL_SIZE_MB / ELAPSED))
else
    SPEED=0
fi

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "========================================="
    echo "=== IMPORT COMPLETED SUCCESSFULLY ==="
    echo "   Data processed: $ACTUAL_SIZE_MB MB"
    echo "   Duration: $ELAPSED seconds"
    echo "   Speed: $SPEED MB/s"
    echo "   Virtual size: $VIRT_SIZE_GB GB"
    echo "========================================="
else
    echo "========================================="
    echo "=== IMPORT FAILED (exit code: $EXIT_CODE) ==="
    echo "========================================="
    exit $EXIT_CODE
fi
"""
        
        pod_manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": ns
            },
            "spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": "importer",
                    "image": "ubuntu:22.04",
                    "command": ["/bin/bash", "-c", convert_cmd],
                    "securityContext": {
                        "privileged": True
                    },
                    "volumeMounts": [{
                        "name": "staging-nfs",
                        "mountPath": "/staging",
                        "readOnly": False  # Need write for temp file
                    }],
                    "volumeDevices": [{
                        "name": "target-disk",
                        "devicePath": "/dev/target"
                    }]
                }],
                "volumes": [
                    {
                        "name": "staging-nfs",
                        "nfs": {
                            "server": nfs_server,
                            "path": nfs_path
                        }
                    },
                    {
                        "name": "target-disk",
                        "persistentVolumeClaim": {
                            "claimName": pvc_name
                        }
                    }
                ]
            }
        }
        
        return self._request("POST", f"/api/v1/namespaces/{ns}/pods", pod_manifest)
    
    def get_pod(self, name: str, namespace: str = None) -> dict:
        """Get a Pod by name."""
        ns = namespace or self.namespace
        return self._request("GET", f"/api/v1/namespaces/{ns}/pods/{name}")
    
    def get_pod_logs(self, name: str, namespace: str = None, tail_lines: int = 100) -> str:
        """Get pod logs."""
        ns = namespace or self.namespace
        url = f"{self.base_url}/api/v1/namespaces/{ns}/pods/{name}/log?tailLines={tail_lines}"
        response = requests.get(url, cert=self.cert, verify=self.verify if self.verify else False)
        if response.ok:
            return response.text
        return ""
    
    def delete_pod(self, name: str, namespace: str = None) -> dict:
        """Delete a Pod."""
        ns = namespace or self.namespace
        return self._request("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}")
    
    def wait_pod_completed(self, name: str, namespace: str = None, 
                           timeout: int = 7200, progress_callback=None) -> bool:
        """
        Wait for a Pod to complete (Succeeded or Failed).
        
        Args:
            name: Pod name
            namespace: Namespace
            timeout: Max wait time in seconds (default 2 hours)
            progress_callback: Optional function(phase, logs_tail) called on updates
            
        Returns:
            True if Succeeded, False if Failed or timeout
        """
        import time
        ns = namespace or self.namespace
        start_time = time.time()
        seen_lines = set()
        
        while time.time() - start_time < timeout:
            try:
                pod = self.get_pod(name, ns)
                phase = pod.get('status', {}).get('phase', 'Unknown')
                
                # Get container status for more detail
                container_statuses = pod.get('status', {}).get('containerStatuses', [])
                container_state = ""
                if container_statuses:
                    state = container_statuses[0].get('state', {})
                    if 'running' in state:
                        container_state = "Running"
                    elif 'waiting' in state:
                        container_state = f"Waiting: {state['waiting'].get('reason', '')}"
                    elif 'terminated' in state:
                        container_state = f"Terminated: {state['terminated'].get('reason', '')}"
                
                # Get logs and show only new lines
                logs = self.get_pod_logs(name, ns, tail_lines=500)
                if logs:
                    new_lines = []
                    for line in logs.split('\n'):
                        if line and line not in seen_lines:
                            seen_lines.add(line)
                            new_lines.append(line)
                    if new_lines and progress_callback:
                        progress_callback(phase, container_state, '\n'.join(new_lines))
                
                if phase == 'Succeeded':
                    return True
                elif phase == 'Failed':
                    return False
                
            except Exception as e:
                if progress_callback:
                    progress_callback('Error', str(e), '')
            
            time.sleep(2)  # Check every 2 seconds
        
        return False  # Timeout
    
    def import_disk_sparse(self, pvc_name: str, size_gi: int, storage_class: str,
                           nfs_server: str, nfs_path: str, qcow2_file: str,
                           namespace: str = None, progress_callback=None,
                           timeout: int = 7200) -> bool:
        """
        High-level method to import a QCOW2 disk using sparse conversion.
        
        This method:
        1. Creates an empty PVC
        2. Creates an importer pod
        3. Waits for conversion to complete
        4. Cleans up the pod
        
        Args:
            pvc_name: Name for the new PVC
            size_gi: Size in GiB (virtual size of disk)
            storage_class: StorageClass to use
            nfs_server: NFS server IP hosting the QCOW2
            nfs_path: NFS export path
            qcow2_file: Path to QCOW2 relative to NFS root
            namespace: Target namespace
            progress_callback: Optional function(stage, detail, logs)
            timeout: Max time for conversion in seconds
            
        Returns:
            True if successful, False otherwise
        """
        ns = namespace or self.namespace
        pod_name = f"importer-{pvc_name}"
        
        try:
            # Step 1: Create empty PVC
            if progress_callback:
                progress_callback("Creating", f"Creating empty PVC {pvc_name} ({size_gi} GiB)", "")
            
            self.create_empty_block_pvc(pvc_name, size_gi, storage_class, ns)
            
            # Wait for PVC to be bound
            import time
            for _ in range(60):  # 5 min timeout
                pvc = self.get_pvc(pvc_name, ns)
                if pvc.get('status', {}).get('phase') == 'Bound':
                    break
                time.sleep(5)
            else:
                if progress_callback:
                    progress_callback("Error", "PVC did not bind within timeout", "")
                return False
            
            # Step 2: Create importer pod
            if progress_callback:
                progress_callback("Importing", f"Creating importer pod {pod_name}", "")
            
            self.create_importer_pod(pod_name, pvc_name, nfs_server, nfs_path, qcow2_file, ns)
            
            # Step 3: Wait for pod to complete
            success = self.wait_pod_completed(pod_name, ns, timeout, progress_callback)
            
            # Step 4: Get final logs
            if progress_callback:
                logs = self.get_pod_logs(pod_name, ns, tail_lines=20)
                if success:
                    progress_callback("Completed", f"PVC {pvc_name} ready", logs)
                else:
                    progress_callback("Failed", "Import failed", logs)
            
            # Step 5: Cleanup pod
            try:
                self.delete_pod(pod_name, ns)
            except:
                pass  # Ignore cleanup errors
            
            return success
            
        except Exception as e:
            if progress_callback:
                progress_callback("Error", str(e), "")
            # Try to cleanup
            try:
                self.delete_pod(pod_name, ns)
            except:
                pass
            return False
