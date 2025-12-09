"""
Windows VM management module.
Handles pre-migration checks, WinRM operations, and post-migration configuration.
"""

import os
import json
import subprocess
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, asdict

# WinRM import with fallback
try:
    import winrm
    from winrm.protocol import Protocol
    WINRM_AVAILABLE = True
except ImportError:
    WINRM_AVAILABLE = False


@dataclass
class NetworkConfig:
    """Network interface configuration."""
    name: str
    mac: str
    dhcp: bool
    ip: Optional[str] = None
    prefix: Optional[int] = None
    gateway: Optional[str] = None
    dns: Optional[List[str]] = None
    dns_suffix: Optional[str] = None


@dataclass
class DiskInfo:
    """Disk information."""
    number: int
    size_gb: int
    partitions: List[Dict[str, Any]]


@dataclass
class AgentStatus:
    """Guest agent and driver status for migration."""
    # Nutanix-specific
    ngt_installed: bool = False
    ngt_version: Optional[str] = None
    virtio_nutanix: bool = False
    # Fedora/Red Hat VirtIO drivers
    virtio_fedora: bool = False
    virtio_net: bool = False
    virtio_storage: bool = False
    virtio_serial: bool = False
    virtio_balloon: bool = False
    vioserial_device_present: bool = False
    # QEMU Guest Agent
    qemu_guest_agent: bool = False
    qemu_guest_agent_running: bool = False
    qemu_guest_agent_autostart: bool = False


@dataclass
class ListeningService:
    """Service listening on a network port."""
    name: str
    display_name: str
    state: str
    pid: int
    local_port: int
    protocol: str  # TCP or UDP


@dataclass
class VMConfig:
    """Complete VM configuration for migration."""
    collected_at: str
    source_platform: str
    hostname: str
    os_name: str
    os_version: str
    architecture: str
    domain: Optional[str]
    domain_joined: bool
    network_interfaces: List[NetworkConfig]
    disks: List[DiskInfo]
    agents: AgentStatus
    winrm_enabled: bool
    rdp_enabled: bool
    migration_ready: bool
    missing_prerequisites: List[str]
    warnings: List[str] = None
    listening_services: List[ListeningService] = None
    
    def __post_init__(self):
        if self.listening_services is None:
            self.listening_services = []
        if self.warnings is None:
            self.warnings = []
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "collected_at": self.collected_at,
            "source_platform": self.source_platform,
            "system": {
                "hostname": self.hostname,
                "os_name": self.os_name,
                "os_version": self.os_version,
                "architecture": self.architecture,
                "domain": self.domain,
                "domain_joined": self.domain_joined
            },
            "network": {
                "interfaces": [asdict(nic) for nic in self.network_interfaces]
            },
            "storage": {
                "disks": [asdict(d) for d in self.disks]
            },
            "agents": asdict(self.agents),
            "services": {
                "winrm_enabled": self.winrm_enabled,
                "rdp_enabled": self.rdp_enabled
            },
            "listening_services": [asdict(s) for s in self.listening_services],
            "migration_ready": self.migration_ready,
            "missing_prerequisites": self.missing_prerequisites
        }
    
    def save(self, path: str):
        """Save configuration to JSON file."""
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, path: str) -> 'VMConfig':
        """Load configuration from JSON file."""
        with open(path, 'r') as f:
            data = json.load(f)
        
        network_interfaces = [
            NetworkConfig(**nic) for nic in data.get('network', {}).get('interfaces', [])
        ]
        
        disks = [
            DiskInfo(**d) for d in data.get('storage', {}).get('disks', [])
        ]
        
        agents = AgentStatus(**data.get('agents', {}))
        
        listening_services = [
            ListeningService(**s) for s in data.get('listening_services', [])
        ]
        
        system = data.get('system', {})
        services = data.get('services', {})
        
        return cls(
            collected_at=data.get('collected_at', ''),
            source_platform=data.get('source_platform', 'unknown'),
            hostname=system.get('hostname', ''),
            os_name=system.get('os_name', ''),
            os_version=system.get('os_version', ''),
            architecture=system.get('architecture', ''),
            domain=system.get('domain'),
            domain_joined=system.get('domain_joined', False),
            network_interfaces=network_interfaces,
            disks=disks,
            agents=agents,
            winrm_enabled=services.get('winrm_enabled', False),
            rdp_enabled=services.get('rdp_enabled', False),
            migration_ready=data.get('migration_ready', False),
            missing_prerequisites=data.get('missing_prerequisites', []),
            listening_services=listening_services
        )


class WinRMClient:
    """WinRM client for Windows remote management."""
    
    def __init__(self, host: str, username: str = None, password: str = None,
                 transport: str = "kerberos", port: int = 5985, ssl: bool = False):
        """
        Initialize WinRM client.
        
        Args:
            host: Target hostname or IP
            username: Username (optional for Kerberos)
            password: Password (optional for Kerberos)
            transport: "kerberos", "ntlm", or "basic"
            port: WinRM port (5985 HTTP, 5986 HTTPS)
            ssl: Use SSL/TLS
        """
        if not WINRM_AVAILABLE:
            raise ImportError("pywinrm not installed. Run: pip install pywinrm[kerberos]")
        
        self.host = host
        self.transport = transport
        self.port = port
        self.ssl = ssl
        
        scheme = "https" if ssl else "http"
        endpoint = f"{scheme}://{host}:{port}/wsman"
        
        # Build session based on transport
        if transport == "kerberos":
            self.session = winrm.Session(
                endpoint,
                auth=(username or '', password or ''),
                transport='kerberos',
                server_cert_validation='ignore'
            )
        elif transport == "ntlm":
            self.session = winrm.Session(
                endpoint,
                auth=(username, password),
                transport='ntlm',
                server_cert_validation='ignore'
            )
        else:  # basic
            self.session = winrm.Session(
                endpoint,
                auth=(username, password),
                transport='basic',
                server_cert_validation='ignore'
            )
    
    def run_powershell(self, script: str, timeout: int = 60) -> Tuple[str, str, int]:
        """
        Execute PowerShell script.
        
        Args:
            script: PowerShell script to execute
            timeout: Timeout in seconds
            
        Returns:
            Tuple of (stdout, stderr, return_code)
        """
        try:
            result = self.session.run_ps(script)
            return (
                result.std_out.decode('utf-8', errors='replace'),
                result.std_err.decode('utf-8', errors='replace'),
                result.status_code
            )
        except Exception as e:
            return ('', str(e), -1)
    
    def run_cmd(self, command: str) -> Tuple[str, str, int]:
        """
        Execute CMD command.
        
        Args:
            command: Command to execute
            
        Returns:
            Tuple of (stdout, stderr, return_code)
        """
        try:
            result = self.session.run_cmd(command)
            return (
                result.std_out.decode('utf-8', errors='replace'),
                result.std_err.decode('utf-8', errors='replace'),
                result.status_code
            )
        except Exception as e:
            return ('', str(e), -1)
    
    def test_connection(self) -> bool:
        """Test WinRM connection."""
        stdout, stderr, rc = self.run_powershell("$env:COMPUTERNAME")
        return rc == 0
    
    def get_listening_services(self) -> List[Dict]:
        """
        Get Windows services that are listening on network ports.
        Only excludes services essential for WinRM connectivity.
        
        Returns:
            List of service dictionaries with name, display_name, state, pid, port, protocol
        """
        script = '''
# Get TCP connections in Listen state
$tcpListeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | 
    Select-Object OwningProcess, LocalPort

# Get UDP listeners
$udpListeners = Get-NetUDPEndpoint -ErrorAction SilentlyContinue | 
    Select-Object OwningProcess, LocalPort

# Combine and get unique PIDs
$allPids = @()
$portMap = @{}

foreach ($l in $tcpListeners) {
    $allPids += $l.OwningProcess
    $key = "$($l.OwningProcess)"
    if (-not $portMap.ContainsKey($key)) { $portMap[$key] = @() }
    $portMap[$key] += @{Port = $l.LocalPort; Protocol = "TCP"}
}

foreach ($l in $udpListeners) {
    $allPids += $l.OwningProcess
    $key = "$($l.OwningProcess)"
    if (-not $portMap.ContainsKey($key)) { $portMap[$key] = @() }
    $portMap[$key] += @{Port = $l.LocalPort; Protocol = "UDP"}
}

$allPids = $allPids | Select-Object -Unique

# Get services for these PIDs
$services = Get-CimInstance Win32_Service | Where-Object { 
    $_.ProcessId -in $allPids -and $_.State -eq 'Running'
}

# ONLY exclude services essential for WinRM connectivity
# All other services (AD, DNS, DHCP, etc.) CAN and SHOULD be stopped before DC migration
$excludeServices = @(
    'WinRM',           # WinRM - Required for remote connection
    'TermService',     # RDP - Backup access method
    'RpcSs',           # RPC - Required for WinRM
    'RpcEptMapper',    # RPC Endpoint Mapper - Required for WinRM
    'DcomLaunch',      # DCOM - Required for WinRM
    'EventLog',        # Event Log - System logging
    'PlugPlay',        # Plug and Play - Hardware detection
    'Power',           # Power Management
    'BFE',             # Base Filtering Engine - Firewall
    'MpsSvc'           # Windows Firewall
)

$result = @()
foreach ($svc in $services) {
    if ($svc.Name -notin $excludeServices) {
        $pid = $svc.ProcessId
        $ports = $portMap["$pid"]
        foreach ($p in $ports) {
            $result += @{
                Name = $svc.Name
                DisplayName = $svc.DisplayName
                State = $svc.State
                PID = $pid
                LocalPort = $p.Port
                Protocol = $p.Protocol
            }
        }
    }
}

$result | ConvertTo-Json -Depth 3
'''
        stdout, stderr, rc = self.run_powershell(script)
        if rc == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
                # Ensure it's always a list
                if isinstance(data, dict):
                    data = [data]
                return data if data else []
            except json.JSONDecodeError:
                return []
        return []
    
    def stop_services(self, service_names: List[str]) -> Dict[str, bool]:
        """
        Stop specified Windows services.
        
        Args:
            service_names: List of service names to stop
            
        Returns:
            Dict mapping service name to success status
        """
        results = {}
        for name in service_names:
            script = f'''
try {{
    Stop-Service -Name "{name}" -Force -ErrorAction Stop
    "SUCCESS"
}} catch {{
    "FAILED: $($_.Exception.Message)"
}}
'''
            stdout, stderr, rc = self.run_powershell(script)
            results[name] = stdout.strip() == "SUCCESS"
        return results
    
    def start_services(self, service_names: List[str]) -> Dict[str, bool]:
        """
        Start specified Windows services.
        
        Args:
            service_names: List of service names to start
            
        Returns:
            Dict mapping service name to success status
        """
        results = {}
        for name in service_names:
            script = f'''
try {{
    Start-Service -Name "{name}" -ErrorAction Stop
    "SUCCESS"
}} catch {{
    "FAILED: $($_.Exception.Message)"
}}
'''
            stdout, stderr, rc = self.run_powershell(script)
            results[name] = stdout.strip() == "SUCCESS"
        return results
    
    def get_service_status(self, service_names: List[str]) -> Dict[str, str]:
        """
        Get status of specified services.
        
        Args:
            service_names: List of service names
            
        Returns:
            Dict mapping service name to status (Running, Stopped, etc.)
        """
        names_str = "','".join(service_names)
        script = f'''
$services = Get-Service -Name @('{names_str}') -ErrorAction SilentlyContinue
$result = @{{}}
foreach ($s in $services) {{
    $result[$s.Name] = $s.Status.ToString()
}}
$result | ConvertTo-Json
'''
        stdout, stderr, rc = self.run_powershell(script)
        if rc == 0 and stdout.strip():
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {}
        return {}


class WindowsPreCheck:
    """Pre-migration checks for Windows VMs."""
    
    # PowerShell scripts for data collection
    PS_SYSTEM_INFO = '''
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
@{
    Hostname = $env:COMPUTERNAME
    OSName = $os.Caption
    OSVersion = $os.Version
    Architecture = $os.OSArchitecture
    Domain = $cs.Domain
    DomainJoined = $cs.PartOfDomain
} | ConvertTo-Json
'''

    PS_NETWORK_INFO = '''
$adapters = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' }
$results = @()
foreach ($adapter in $adapters) {
    $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue
    $ipAddr = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
    $dns = Get-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    
    $isDhcp = (Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4).Dhcp -eq 'Enabled'
    
    $results += @{
        Name = $adapter.Name
        MAC = $adapter.MacAddress -replace '-', ':'
        DHCP = $isDhcp
        IP = if ($ipAddr) { $ipAddr.IPAddress } else { $null }
        Prefix = if ($ipAddr) { $ipAddr.PrefixLength } else { $null }
        Gateway = if ($ipConfig.IPv4DefaultGateway) { $ipConfig.IPv4DefaultGateway.NextHop } else { $null }
        DNS = if ($dns) { $dns.ServerAddresses } else { @() }
        DNSSuffix = (Get-DnsClient -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue).ConnectionSpecificSuffix
    }
}
$results | ConvertTo-Json -Depth 3
'''

    PS_DISK_INFO = '''
$disks = Get-Disk | Where-Object { $_.BusType -ne 'USB' }
$results = @()
foreach ($disk in $disks) {
    $partitions = Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue | ForEach-Object {
        $vol = $_ | Get-Volume -ErrorAction SilentlyContinue
        @{
            Letter = if ($_.DriveLetter) { [string]$_.DriveLetter } else { $null }
            Label = if ($vol) { $vol.FileSystemLabel } else { $null }
            SizeGB = [math]::Round($_.Size / 1GB, 2)
        }
    }
    $results += @{
        Number = $disk.Number
        SizeGB = [math]::Round($disk.Size / 1GB, 0)
        Partitions = $partitions
    }
}
$results | ConvertTo-Json -Depth 3
'''

    PS_AGENT_STATUS = r'''
# Check Nutanix Guest Tools
$ngt = Get-Service -Name "Nutanix Guest Agent" -ErrorAction SilentlyContinue
$ngtVersion = $null
if ($ngt) {
    $ngtPath = (Get-ItemProperty "HKLM:\SOFTWARE\Nutanix\GuestTools" -ErrorAction SilentlyContinue).Version
    $ngtVersion = $ngtPath
}

# Check Nutanix VirtIO drivers
$virtioNutanix = (Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.Manufacturer -like "*Nutanix*" }).Count -gt 0

# Get all PnP signed drivers for VirtIO checks
$allDrivers = Get-WmiObject Win32_PnPSignedDriver

# Check Fedora/Red Hat VirtIO drivers (any)
$virtioFedora = ($allDrivers | Where-Object { 
    $_.Manufacturer -like "*Red Hat*" -or $_.DeviceName -like "*VirtIO*" 
}).Count -gt 0

# Check specific VirtIO drivers needed for Harvester/KubeVirt
# VirtIO Network adapter
$virtioNet = ($allDrivers | Where-Object { 
    $_.DeviceName -like "*VirtIO*Net*" -or 
    ($_.Manufacturer -like "*Red Hat*" -and $_.DeviceClass -eq "Net")
}).Count -gt 0

# VirtIO SCSI/Block storage
$virtioStorage = ($allDrivers | Where-Object { 
    $_.DeviceName -like "*VirtIO*SCSI*" -or 
    $_.DeviceName -like "*VirtIO*Stor*" -or
    ($_.Manufacturer -like "*Red Hat*" -and $_.DeviceClass -eq "SCSIAdapter")
}).Count -gt 0

# VirtIO Serial (required for QEMU Guest Agent communication)
$virtioSerial = ($allDrivers | Where-Object { 
    $_.DeviceName -like "*VirtIO*Serial*" -or
    $_.DeviceName -like "*VirtIO Serial Driver*"
}).Count -gt 0

# VirtIO Balloon (memory management - optional but recommended)
$virtioBalloon = ($allDrivers | Where-Object { 
    $_.DeviceName -like "*VirtIO*Balloon*"
}).Count -gt 0

# Check QEMU Guest Agent service
$qemuGA = Get-Service -Name "QEMU-GA" -ErrorAction SilentlyContinue
if (-not $qemuGA) {
    $qemuGA = Get-Service -Name "QEMU Guest Agent" -ErrorAction SilentlyContinue
}

$qemuGAInstalled = $null -ne $qemuGA
$qemuGARunning = $false
$qemuGAAutoStart = $false

if ($qemuGA) {
    $qemuGARunning = $qemuGA.Status -eq 'Running'
    $qemuGAAutoStart = $qemuGA.StartType -eq 'Automatic'
}

# Check if vioserial device exists (even without driver)
$vioserialDevice = (Get-WmiObject Win32_PnPEntity | Where-Object { 
    $_.Name -like "*VirtIO*Serial*" -or $_.DeviceID -like "*VEN_1AF4&DEV_1003*"
}).Count -gt 0

@{
    NGTInstalled = $null -ne $ngt
    NGTVersion = $ngtVersion
    VirtIONutanix = $virtioNutanix
    VirtIOFedora = $virtioFedora
    VirtIONet = $virtioNet
    VirtIOStorage = $virtioStorage
    VirtIOSerial = $virtioSerial
    VirtioBalloon = $virtioBalloon
    VioSerialDevicePresent = $vioserialDevice
    QEMUGuestAgent = $qemuGAInstalled
    QEMUGuestAgentRunning = $qemuGARunning
    QEMUGuestAgentAutoStart = $qemuGAAutoStart
} | ConvertTo-Json
'''

    PS_SERVICE_STATUS = r'''
# Check WinRM
$winrm = Get-Service -Name WinRM -ErrorAction SilentlyContinue

# Check RDP
$rdp = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server" -ErrorAction SilentlyContinue).fDenyTSConnections -eq 0

@{
    WinRMEnabled = ($winrm -and $winrm.Status -eq 'Running')
    RDPEnabled = $rdp
} | ConvertTo-Json
'''

    def __init__(self, client: WinRMClient):
        """
        Initialize pre-check with WinRM client.
        
        Args:
            client: Connected WinRM client
        """
        self.client = client
    
    def collect_system_info(self) -> Dict[str, Any]:
        """Collect system information."""
        stdout, stderr, rc = self.client.run_powershell(self.PS_SYSTEM_INFO)
        if rc == 0 and stdout.strip():
            return json.loads(stdout)
        return {}
    
    def collect_network_info(self) -> List[Dict[str, Any]]:
        """Collect network configuration."""
        stdout, stderr, rc = self.client.run_powershell(self.PS_NETWORK_INFO)
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            # Ensure it's always a list
            if isinstance(data, dict):
                return [data]
            return data
        return []
    
    def collect_disk_info(self) -> List[Dict[str, Any]]:
        """Collect disk information."""
        stdout, stderr, rc = self.client.run_powershell(self.PS_DISK_INFO)
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, dict):
                return [data]
            return data
        return []
    
    def collect_agent_status(self) -> Dict[str, Any]:
        """Check installed agents."""
        stdout, stderr, rc = self.client.run_powershell(self.PS_AGENT_STATUS)
        if rc == 0 and stdout.strip():
            return json.loads(stdout)
        return {}
    
    def collect_service_status(self) -> Dict[str, Any]:
        """Check service status."""
        stdout, stderr, rc = self.client.run_powershell(self.PS_SERVICE_STATUS)
        if rc == 0 and stdout.strip():
            return json.loads(stdout)
        return {}
    
    def run_full_check(self) -> VMConfig:
        """
        Run complete pre-migration check.
        
        Returns:
            VMConfig with all collected information
        """
        print("   📋 Collecting system info...")
        system = self.collect_system_info()
        
        print("   🌐 Collecting network config...")
        network = self.collect_network_info()
        
        print("   💾 Collecting disk info...")
        disks = self.collect_disk_info()
        
        print("   🔧 Checking agents...")
        agents = self.collect_agent_status()
        
        print("   ⚙️  Checking services...")
        services = self.collect_service_status()
        
        print("   🔌 Collecting listening services...")
        listening_svc_data = self.client.get_listening_services()
        
        # Build network interfaces
        network_interfaces = []
        for nic in network:
            network_interfaces.append(NetworkConfig(
                name=nic.get('Name', ''),
                mac=nic.get('MAC', ''),
                dhcp=nic.get('DHCP', True),
                ip=nic.get('IP'),
                prefix=nic.get('Prefix'),
                gateway=nic.get('Gateway'),
                dns=nic.get('DNS', []),
                dns_suffix=nic.get('DNSSuffix')
            ))
        
        # Build disk info
        disk_list = []
        for d in disks:
            disk_list.append(DiskInfo(
                number=d.get('Number', 0),
                size_gb=d.get('SizeGB', 0),
                partitions=d.get('Partitions', [])
            ))
        
        # Build agent status
        agent_status = AgentStatus(
            ngt_installed=agents.get('NGTInstalled', False),
            ngt_version=agents.get('NGTVersion'),
            virtio_nutanix=agents.get('VirtIONutanix', False),
            virtio_fedora=agents.get('VirtIOFedora', False),
            virtio_net=agents.get('VirtIONet', False),
            virtio_storage=agents.get('VirtIOStorage', False),
            virtio_serial=agents.get('VirtIOSerial', False),
            virtio_balloon=agents.get('VirtioBalloon', False),
            vioserial_device_present=agents.get('VioSerialDevicePresent', False),
            qemu_guest_agent=agents.get('QEMUGuestAgent', False),
            qemu_guest_agent_running=agents.get('QEMUGuestAgentRunning', False),
            qemu_guest_agent_autostart=agents.get('QEMUGuestAgentAutoStart', False)
        )
        
        # Build listening services list
        listening_services = []
        for svc in listening_svc_data:
            listening_services.append(ListeningService(
                name=svc.get('Name', ''),
                display_name=svc.get('DisplayName', ''),
                state=svc.get('State', ''),
                pid=svc.get('PID', 0),
                local_port=svc.get('LocalPort', 0),
                protocol=svc.get('Protocol', 'TCP')
            ))
        
        # Determine missing prerequisites for Harvester/KubeVirt migration
        missing = []
        warnings = []
        
        # Critical: Need VirtIO drivers (either Fedora/Red Hat or Nutanix)
        if not agent_status.virtio_fedora and not agent_status.virtio_nutanix:
            missing.append("virtio_drivers")
        
        # Critical: VirtIO Storage driver (required for disk access after migration)
        if not agent_status.virtio_storage:
            missing.append("virtio_storage")
        
        # Critical: VirtIO Network driver (required for network after migration)
        if not agent_status.virtio_net:
            missing.append("virtio_net")
        
        # Important: VirtIO Serial driver (required for Guest Agent ↔ KubeVirt communication)
        if not agent_status.virtio_serial:
            missing.append("virtio_serial")
        
        # Important: QEMU Guest Agent (required for IP detection in Harvester UI)
        if not agent_status.qemu_guest_agent:
            missing.append("qemu_guest_agent")
        
        # Warnings (not blocking but recommended)
        if agent_status.qemu_guest_agent:
            if not agent_status.qemu_guest_agent_running:
                warnings.append("qemu_ga_not_running")
            if not agent_status.qemu_guest_agent_autostart:
                warnings.append("qemu_ga_not_autostart")
        
        if not agent_status.virtio_balloon:
            warnings.append("virtio_balloon_missing")
        
        migration_ready = len(missing) == 0
        
        return VMConfig(
            collected_at=datetime.utcnow().isoformat() + "Z",
            source_platform="nutanix",
            hostname=system.get('Hostname', ''),
            os_name=system.get('OSName', ''),
            os_version=system.get('OSVersion', ''),
            architecture=system.get('Architecture', ''),
            domain=system.get('Domain'),
            domain_joined=system.get('DomainJoined', False),
            network_interfaces=network_interfaces,
            disks=disk_list,
            agents=agent_status,
            winrm_enabled=services.get('WinRMEnabled', False),
            rdp_enabled=services.get('RDPEnabled', False),
            migration_ready=migration_ready,
            missing_prerequisites=missing,
            warnings=warnings,
            listening_services=listening_services
        )


class WindowsPostConfig:
    """Post-migration configuration for Windows VMs."""
    
    PS_SET_STATIC_IP = '''
param(
    [string]$InterfaceName,
    [string]$IPAddress,
    [int]$PrefixLength,
    [string]$Gateway,
    [string[]]$DNSServers
)

# Remove existing IP configuration
$adapter = Get-NetAdapter -Name $InterfaceName -ErrorAction Stop
Remove-NetIPAddress -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue

# Set static IP
New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $IPAddress -PrefixLength $PrefixLength -DefaultGateway $Gateway -ErrorAction Stop

# Set DNS
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses $DNSServers -ErrorAction Stop

Write-Output "Static IP configured successfully"
'''

    PS_UNINSTALL_NGT = r'''
$ngtUninstall = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*" | 
    Where-Object { $_.DisplayName -like "*Nutanix*Guest*" }

if ($ngtUninstall) {
    $uninstallString = $ngtUninstall.UninstallString
    if ($uninstallString -match "msiexec") {
        $productCode = $uninstallString -replace '.*({[^}]+}).*', '$1'
        Start-Process msiexec.exe -ArgumentList "/x $productCode /qn" -Wait -NoNewWindow
        Write-Output "NGT uninstalled"
    } else {
        Start-Process $uninstallString -ArgumentList "/S" -Wait -NoNewWindow
        Write-Output "NGT uninstalled"
    }
} else {
    Write-Output "NGT not found"
}
'''

    def __init__(self, client: WinRMClient):
        """Initialize with WinRM client."""
        self.client = client
    
    def apply_network_config(self, config: NetworkConfig) -> bool:
        """
        Apply network configuration.
        
        Args:
            config: Network configuration to apply
            
        Returns:
            True if successful
        """
        if config.dhcp:
            # Enable DHCP
            script = f'''
$adapter = Get-NetAdapter -Name "{config.name}" -ErrorAction Stop
Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -Dhcp Enabled
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ResetServerAddresses
Write-Output "DHCP enabled"
'''
        else:
            # Set static IP
            dns_array = '@(' + ','.join([f'"{d}"' for d in (config.dns or [])]) + ')'
            script = f'''
$adapter = Get-NetAdapter -Name "{config.name}" -ErrorAction Stop

# Remove existing configuration
Remove-NetIPAddress -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue

# Set static IP
New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress "{config.ip}" -PrefixLength {config.prefix} -DefaultGateway "{config.gateway}" -ErrorAction Stop

# Set DNS
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses {dns_array} -ErrorAction Stop

Write-Output "Static IP configured"
'''
        
        stdout, stderr, rc = self.client.run_powershell(script)
        return rc == 0
    
    def uninstall_ngt(self) -> bool:
        """Uninstall Nutanix Guest Tools."""
        stdout, stderr, rc = self.client.run_powershell(self.PS_UNINSTALL_NGT)
        return rc == 0
    
    def generate_reconfig_script(self, config: VMConfig) -> str:
        """
        Generate a PowerShell script for manual reconfiguration.
        
        Args:
            config: VM configuration
            
        Returns:
            PowerShell script content
        """
        script_lines = [
            "# Auto-generated network reconfiguration script",
            f"# Generated: {datetime.utcnow().isoformat()}",
            f"# Source VM: {config.hostname}",
            "",
            "# Run this script as Administrator after migration",
            "",
        ]
        
        for nic in config.network_interfaces:
            if nic.dhcp:
                continue  # Skip DHCP interfaces
            
            dns_array = '@(' + ','.join([f'"{d}"' for d in (nic.dns or [])]) + ')'
            
            script_lines.extend([
                f"# Configure interface: {nic.name}",
                f"$adapter = Get-NetAdapter | Where-Object {{ $_.MacAddress -replace '-',':' -eq '{nic.mac}' }}",
                "if ($adapter) {",
                "    # Remove existing configuration",
                "    Remove-NetIPAddress -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue",
                "    Remove-NetRoute -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue",
                "",
                "    # Set static IP",
                f'    New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress "{nic.ip}" -PrefixLength {nic.prefix} -DefaultGateway "{nic.gateway}"',
                "",
                "    # Set DNS",
                f"    Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses {dns_array}",
                "",
                f'    Write-Host "Configured {nic.name} with IP {nic.ip}" -ForegroundColor Green',
                "} else {",
                f'    Write-Host "Adapter with MAC {nic.mac} not found" -ForegroundColor Red',
                "}",
                "",
            ])
        
        script_lines.extend([
            "# Verify configuration",
            "Get-NetIPConfiguration | Format-List",
            "",
            "Write-Host 'Configuration complete!' -ForegroundColor Green",
        ])
        
        return '\n'.join(script_lines)


def download_virtio_tools(dest_dir: str, verbose: bool = True) -> Dict[str, str]:
    """
    Download latest virtio-win tools from Fedora.
    
    Args:
        dest_dir: Destination directory
        verbose: Print progress
        
    Returns:
        Dict with paths to downloaded files
    """
    import urllib.request
    import ssl
    
    os.makedirs(dest_dir, exist_ok=True)
    
    # URLs for latest stable versions
    URLS = {
        "virtio-win.iso": "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso",
        "virtio-win-gt-x64.msi": "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win-gt-x64.msi",
        "qemu-ga-x86_64.msi": "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-qemu-ga/qemu-ga-x86_64.msi",
    }
    
    downloaded = {}
    
    # Create SSL context that doesn't verify (for corporate proxies)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    for filename, url in URLS.items():
        dest_path = os.path.join(dest_dir, filename)
        
        if os.path.exists(dest_path):
            if verbose:
                print(f"   ✓ {filename} already exists")
            downloaded[filename] = dest_path
            continue
        
        if verbose:
            print(f"   ⬇️  Downloading {filename}...")
        
        try:
            # Use wget or curl if available (better for large files)
            if subprocess.run(["which", "wget"], capture_output=True).returncode == 0:
                result = subprocess.run(
                    ["wget", "-q", "--no-check-certificate", "-O", dest_path, url],
                    capture_output=True,
                    timeout=600  # 10 min timeout for large ISO
                )
                if result.returncode == 0:
                    downloaded[filename] = dest_path
                    if verbose:
                        size_mb = os.path.getsize(dest_path) / (1024 * 1024)
                        print(f"   ✅ {filename} ({size_mb:.1f} MB)")
                else:
                    if verbose:
                        print(f"   ❌ Failed to download {filename}")
            else:
                # Fallback to urllib
                urllib.request.urlretrieve(url, dest_path)
                downloaded[filename] = dest_path
                if verbose:
                    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
                    print(f"   ✅ {filename} ({size_mb:.1f} MB)")
                    
        except Exception as e:
            if verbose:
                print(f"   ❌ Error downloading {filename}: {e}")
    
    return downloaded


def check_winrm_available() -> Tuple[bool, str]:
    """
    Check if pywinrm is available and properly configured.
    
    Returns:
        Tuple of (available, message)
    """
    if not WINRM_AVAILABLE:
        return (False, "pywinrm not installed. Run: pip install pywinrm[kerberos]")
    
    # Check for Kerberos support
    try:
        from winrm.transport import Transport
        return (True, "pywinrm available with Kerberos support")
    except ImportError:
        return (True, "pywinrm available (Kerberos may not be configured)")
