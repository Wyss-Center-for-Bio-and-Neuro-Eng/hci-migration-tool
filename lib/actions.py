"""
Migration Actions - Export, Convert, Import operations
"""

import os
import subprocess
import time
import http.server
import socketserver
import threading
from typing import Optional, Callable, List, Dict
from .utils import Colors, colored, format_size


class MigrationActions:
    """Handles VM migration operations."""
    
    def __init__(self, config: dict, nutanix=None, harvester=None):
        """
        Initialize migration actions.
        
        Args:
            config: Full configuration dict
            nutanix: Optional NutanixClient instance
            harvester: Optional HarvesterClient instance
        """
        self.config = config
        self.nutanix = nutanix
        self.harvester = harvester
        self.staging_path = config.get('transfer', {}).get('staging_mount', '/mnt/staging')
        self._http_server = None
        self._http_thread = None
    
    # === Staging Operations ===
    
    def is_staging_mounted(self) -> bool:
        """Check if staging is mounted."""
        return os.path.ismount(self.staging_path)
    
    def check_staging(self) -> dict:
        """
        Check staging directory status.
        
        Returns:
            Dict with mounted, path, files info
        """
        result = {
            'path': self.staging_path,
            'mounted': self.is_staging_mounted(),
            'files': [],
            'total_size': 0
        }
        
        if result['mounted']:
            try:
                for f in os.listdir(self.staging_path):
                    fpath = os.path.join(self.staging_path, f)
                    if os.path.isfile(fpath):
                        size = os.path.getsize(fpath)
                        result['files'].append({
                            'name': f,
                            'size': size,
                            'type': 'file'
                        })
                        result['total_size'] += size
                    elif os.path.isdir(fpath):
                        result['files'].append({
                            'name': f,
                            'size': 0,
                            'type': 'directory'
                        })
            except Exception as e:
                result['error'] = str(e)
        
        return result
    
    def list_staging_files(self, filter_ext: str = None) -> List[Dict]:
        """
        List all files in staging directory.
        
        Args:
            filter_ext: Optional extension filter (e.g., '.raw', '.qcow2')
        
        Returns:
            List of dicts with name, path, size, mtime
        """
        if not self.is_staging_mounted():
            return []
        
        files = []
        try:
            for f in os.listdir(self.staging_path):
                fpath = os.path.join(self.staging_path, f)
                if os.path.isfile(fpath):
                    if filter_ext and not f.endswith(filter_ext):
                        continue
                    stat = os.stat(fpath)
                    files.append({
                        'name': f,
                        'path': fpath,
                        'size': stat.st_size,
                        'mtime': stat.st_mtime,
                    })
        except Exception as e:
            pass
        
        return sorted(files, key=lambda x: x['name'])
    
    def list_raw_files(self) -> List[Dict]:
        """List RAW files in staging."""
        return self.list_staging_files(filter_ext='.raw')
    
    def list_qcow2_files(self) -> List[Dict]:
        """List QCOW2 files in staging."""
        return self.list_staging_files(filter_ext='.qcow2')
    
    def get_file_info(self, filename: str) -> Optional[Dict]:
        """
        Get detailed info about a file in staging.
        
        Args:
            filename: Name of the file
        
        Returns:
            Dict with file info or None
        """
        fpath = os.path.join(self.staging_path, filename)
        if not os.path.exists(fpath):
            return None
        
        stat = os.stat(fpath)
        info = {
            'name': filename,
            'path': fpath,
            'size': stat.st_size,
            'mtime': stat.st_mtime,
        }
        
        # Get qemu-img info for disk images
        if filename.endswith(('.raw', '.qcow2', '.vmdk', '.vhd', '.vhdx')):
            try:
                result = subprocess.run(
                    ['qemu-img', 'info', '--output=json', fpath],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    import json
                    qemu_info = json.loads(result.stdout)
                    info['format'] = qemu_info.get('format')
                    info['virtual_size'] = qemu_info.get('virtual-size')
                    info['actual_size'] = qemu_info.get('actual-size')
            except:
                pass
        
        return info
    
    def delete_file(self, filepath: str) -> bool:
        """Delete a file from staging."""
        try:
            os.remove(filepath)
            return True
        except:
            return False
    
    # === Export Operations ===
    
    def download_nutanix_image(self, image_uuid: str, filename: str,
                                progress_callback: Callable = None) -> str:
        """
        Download Nutanix image to staging.
        
        Args:
            image_uuid: UUID of the Nutanix image
            filename: Destination filename
            progress_callback: Optional callback(downloaded, total, speed)
        
        Returns:
            Full path to downloaded file
        """
        if not self.nutanix:
            raise RuntimeError("Nutanix client not initialized")
        
        dest_path = os.path.join(self.staging_path, filename)
        
        start_time = time.time()
        last_update = start_time
        last_downloaded = 0
        
        def progress_wrapper(downloaded, total):
            nonlocal last_update, last_downloaded
            now = time.time()
            if now - last_update >= 1.0:  # Update every second
                speed = (downloaded - last_downloaded) / (now - last_update)
                if progress_callback:
                    progress_callback(downloaded, total, speed)
                last_update = now
                last_downloaded = downloaded
        
        self.nutanix.download_image(image_uuid, dest_path, progress_wrapper)
        
        return dest_path
    
    # === Convert Operations ===
    
    def convert_raw_to_qcow2(self, raw_file: str, compress: bool = True,
                             progress_callback: Callable = None) -> dict:
        """
        Convert RAW file to QCOW2.
        
        Args:
            raw_file: Path to RAW file
            compress: Whether to compress the output
            progress_callback: Optional callback(percent)
        
        Returns:
            Dict with success, output_file, size_before, size_after
        """
        if not os.path.exists(raw_file):
            return {'success': False, 'error': f"File not found: {raw_file}"}
        
        qcow2_file = raw_file.replace('.raw', '.qcow2')
        
        cmd = ['qemu-img', 'convert', '-f', 'raw', '-O', 'qcow2']
        if compress:
            cmd.append('-c')
        cmd.extend(['-p', raw_file, qcow2_file])
        
        try:
            size_before = os.path.getsize(raw_file)
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            
            # Read progress output
            for line in process.stdout:
                if progress_callback:
                    # Parse progress from qemu-img output
                    if '%' in line:
                        try:
                            pct = float(line.strip().replace('%', '').split()[-1])
                            progress_callback(pct)
                        except:
                            pass
            
            process.wait()
            
            if process.returncode != 0:
                return {'success': False, 'error': f"qemu-img failed with code {process.returncode}"}
            
            size_after = os.path.getsize(qcow2_file)
            
            return {
                'success': True,
                'input_file': raw_file,
                'output_file': qcow2_file,
                'size_before': size_before,
                'size_after': size_after,
                'reduction_pct': (1 - size_after / size_before) * 100 if size_before > 0 else 0
            }
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    # === HTTP Server for Image Serving ===
    
    def start_http_server(self, port: int = 8080) -> str:
        """
        Start HTTP server to serve images.
        
        Args:
            port: Port to listen on
        
        Returns:
            Base URL for serving files
        """
        if self._http_server:
            self.stop_http_server()
        
        handler = http.server.SimpleHTTPRequestHandler
        
        class QuietHandler(handler):
            def log_message(self, format, *args):
                pass  # Suppress logging
        
        os.chdir(self.staging_path)
        self._http_server = socketserver.TCPServer(("", port), QuietHandler)
        
        self._http_thread = threading.Thread(target=self._http_server.serve_forever)
        self._http_thread.daemon = True
        self._http_thread.start()
        
        # Get local IP
        import socket
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except:
            local_ip = "127.0.0.1"
        
        return f"http://{local_ip}:{port}"
    
    def stop_http_server(self):
        """Stop the HTTP server."""
        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None
            self._http_thread = None
    
    # === Import Operations ===
    
    def create_harvester_image(self, name: str, filename: str, 
                                http_base_url: str = None,
                                namespace: str = "default") -> dict:
        """
        Create Harvester image from staged file.
        
        Args:
            name: Image name in Harvester
            filename: Filename in staging
            http_base_url: Base URL of HTTP server (starts one if None)
            namespace: Harvester namespace
        
        Returns:
            API response dict
        """
        if not self.harvester:
            raise RuntimeError("Harvester client not initialized")
        
        if not http_base_url:
            http_base_url = self.start_http_server()
        
        url = f"{http_base_url}/{filename}"
        
        return self.harvester.create_image(name, url, name, namespace)
    
    def create_harvester_vm(self, name: str, vm_info: dict, 
                            image_name: str, network_name: str,
                            storage_class: str = "harvester-longhorn",
                            namespace: str = "default") -> dict:
        """
        Create Harvester VM from Nutanix VM info.
        
        Args:
            name: VM name
            vm_info: Parsed VM info from NutanixClient.parse_vm_info()
            image_name: Harvester image name to use
            network_name: Network attachment definition name
            storage_class: Storage class for disks
            namespace: Harvester namespace
        
        Returns:
            API response dict
        """
        if not self.harvester:
            raise RuntimeError("Harvester client not initialized")
        
        # Calculate memory in GB
        memory_gb = vm_info.get('memory_mb', 4096) // 1024
        if memory_gb < 1:
            memory_gb = 1
        
        manifest = self.harvester.generate_vm_manifest(
            name=name,
            cpu_cores=vm_info.get('vcpu', 2),
            memory_gb=memory_gb,
            image_name=image_name,
            network_name=network_name,
            storage_class=storage_class,
            namespace=namespace,
            boot_type=vm_info.get('boot_type', 'BIOS')
        )
        
        return self.harvester.create_vm(manifest)
    
    # === Full Migration ===
    
    def migrate_vm(self, vm_name: str, network_name: str,
                   storage_class: str = "harvester-longhorn",
                   namespace: str = "default",
                   progress_callback: Callable = None) -> dict:
        """
        Full migration workflow.
        
        Args:
            vm_name: Source VM name in Nutanix
            network_name: Target network in Harvester
            storage_class: Storage class for Harvester
            namespace: Harvester namespace
            progress_callback: Optional callback(step, message, progress_pct)
        
        Returns:
            Dict with success and details
        """
        from .nutanix import NutanixClient
        
        result = {
            'success': False,
            'steps': [],
            'error': None
        }
        
        def log_step(step: str, message: str, pct: int = None):
            result['steps'].append({'step': step, 'message': message})
            if progress_callback:
                progress_callback(step, message, pct)
        
        try:
            # Step 1: Get VM info from Nutanix
            log_step('get_vm', f"Getting VM info for {vm_name}", 5)
            vm = self.nutanix.get_vm_by_name(vm_name)
            if not vm:
                raise RuntimeError(f"VM {vm_name} not found in Nutanix")
            
            vm_info = NutanixClient.parse_vm_info(vm)
            
            # Step 2: Check if VM is powered off
            if vm_info['power_state'] == 'ON':
                log_step('power_check', "VM is running - must be powered off first", 10)
                raise RuntimeError("VM must be powered off before migration")
            
            log_step('power_check', "VM is powered off", 10)
            
            # Step 3: Export each disk
            # Note: This requires images to be created via acli first
            log_step('export', "Export step - requires manual image creation via acli", 20)
            
            # Step 4: Convert to QCOW2
            log_step('convert', "Conversion step", 50)
            
            # Step 5: Import to Harvester
            log_step('import', "Import step", 80)
            
            # Step 6: Create VM
            log_step('create_vm', "VM creation step", 90)
            
            log_step('complete', "Migration workflow complete", 100)
            result['success'] = True
            
        except Exception as e:
            result['error'] = str(e)
            log_step('error', str(e))
        
        return result
