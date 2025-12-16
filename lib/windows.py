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
                 transport: str = "kerberos", port: int = 5985, ssl: bool = False,
                 operation_timeout: int = 60, read_timeout: int = 70):
        """
        Initialize WinRM client.
        
        Args:
            host: Target hostname or IP
            username: Username (optional for Kerberos)
            password: Password (optional for Kerberos)
            transport: "kerberos", "ntlm", or "basic"
            port: WinRM port (5985 HTTP, 5986 HTTPS)
            ssl: Use SSL/TLS
            operation_timeout: WinRM operation timeout in seconds
            read_timeout: HTTP read timeout in seconds
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
                server_cert_validation='ignore',
                operation_timeout_sec=operation_timeout,
                read_timeout_sec=read_timeout
            )
        elif transport == "ntlm":
            self.session = winrm.Session(
                endpoint,
                auth=(username, password),
                transport='ntlm',
                server_cert_validation='ignore',
                operation_timeout_sec=operation_timeout,
                read_timeout_sec=read_timeout
            )
        else:  # basic
            self.session = winrm.Session(
                endpoint,
                auth=(username, password),
                transport='basic',
                server_cert_validation='ignore',
                operation_timeout_sec=operation_timeout,
                read_timeout_sec=read_timeout
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
# Debug logging
$logFile = "C:\temp\virtio-debug.log"
New-Item -ItemType Directory -Path "C:\temp" -Force -ErrorAction SilentlyContinue | Out-Null

"=== VirtIO Detection Debug ===" | Out-File $logFile
"Date: $(Get-Date)" | Out-File $logFile -Append

# Fast method: check registry for installed programs
$uninstallPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
)
$installed = Get-ItemProperty $uninstallPaths -ErrorAction SilentlyContinue | Select-Object -ExpandProperty DisplayName -ErrorAction SilentlyContinue

$virtioPrograms = $installed | Where-Object { $_ -match "virtio" }
$qemuPrograms = $installed | Where-Object { $_ -match "qemu" }

"VirtIO programs: $($virtioPrograms -join ', ')" | Out-File $logFile -Append
"QEMU programs: $($qemuPrograms -join ', ')" | Out-File $logFile -Append

# VirtIO is installed if virtio-win-guest-tools or virtio-win-driver is in registry
$virtioGTInstalled = ($virtioPrograms | Where-Object { $_ -match "guest-tools|driver" }).Count -gt 0

# Also check Red Hat folder (contains actual driver files)
$redHatDir = Test-Path "$env:ProgramFiles\Red Hat"
$virtioWinDrivers = Test-Path "$env:ProgramFiles\Virtio-Win\Vioscsi"
"Red Hat folder: $redHatDir" | Out-File $logFile -Append
"Virtio-Win drivers: $virtioWinDrivers" | Out-File $logFile -Append

# VirtIO is installed if program in registry OR Red Hat folder OR Virtio-Win with drivers
$virtioInstalled = $virtioGTInstalled -or $redHatDir -or $virtioWinDrivers

# NGT
$ngt = Get-Service -Name "Nutanix Guest Agent" -ErrorAction SilentlyContinue

# QEMU GA - service check (most reliable)
$qemuGA = Get-Service -Name "QEMU-GA" -ErrorAction SilentlyContinue
if (-not $qemuGA) { $qemuGA = Get-Service -Name "QEMU Guest Agent" -ErrorAction SilentlyContinue }

$qemuGaInstalled = $null -ne $qemuGA

"" | Out-File $logFile -Append
"=== Final Results ===" | Out-File $logFile -Append
"VirtIO in registry: $virtioGTInstalled" | Out-File $logFile -Append
"Red Hat folder: $redHatDir" | Out-File $logFile -Append  
"VirtIO installed: $virtioInstalled" | Out-File $logFile -Append
"QEMU GA service: $qemuGaInstalled" | Out-File $logFile -Append

@{
    NGTInstalled = $null -ne $ngt
    VirtIONutanix = $false
    VirtIOFedora = $virtioInstalled
    VirtIONet = $virtioInstalled
    VirtIOStorage = $virtioInstalled
    VirtIOSerial = $virtioInstalled
    VirtioBalloon = $virtioInstalled
    QEMUGuestAgent = $qemuGaInstalled
    QEMUGuestAgentRunning = ($qemuGA -and $qemuGA.Status -eq 'Running')
    QEMUGuestAgentAutoStart = ($qemuGA -and $qemuGA.StartType -eq 'Automatic')
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
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {}
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
    
    # TODO: Implement uninstall_nutanix_tools() method
    # Should uninstall after successful migration to Harvester:
    # - Nutanix Guest Tools (msiexec /x or wmic product uninstall)
    # - Nutanix VirtIO
    # - Nutanix VM Mobility
    # Command example: wmic product where "name like 'Nutanix%'" call uninstall /nointeractive
    
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

    # Script to uninstall ALL Nutanix software (Guest Tools, VirtIO, VM Mobility)
    PS_UNINSTALL_ALL_NUTANIX = r'''
$ErrorActionPreference = "Continue"
$logFile = "C:\temp\nutanix-uninstall.log"

# Ensure log directory exists
if (-not (Test-Path "C:\temp")) {
    New-Item -ItemType Directory -Path "C:\temp" -Force | Out-Null
}

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts - $msg" | Tee-Object -FilePath $logFile -Append
}

Log "=========================================="
Log "Nutanix Software Uninstallation Started"
Log "=========================================="

# List of Nutanix software patterns to uninstall
$nutanixPatterns = @(
    "*Nutanix*Guest*",
    "*Nutanix*VirtIO*",
    "*Nutanix*VM*Mobility*",
    "*Nutanix*Frame*",
    "*Nutanix*Move*"
)

$uninstalledCount = 0
$failedCount = 0

# Search in both 32-bit and 64-bit registry locations
$registryPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
)

foreach ($regPath in $registryPaths) {
    foreach ($pattern in $nutanixPatterns) {
        $apps = Get-ItemProperty $regPath -ErrorAction SilentlyContinue | 
            Where-Object { $_.DisplayName -like $pattern }
        
        foreach ($app in $apps) {
            $displayName = $app.DisplayName
            $uninstallString = $app.UninstallString
            
            if ($uninstallString) {
                Log "Uninstalling: $displayName"
                Log "  Uninstall string: $uninstallString"
                
                try {
                    if ($uninstallString -match "msiexec") {
                        # MSI-based uninstall
                        $productCode = $uninstallString -replace '.*({[^}]+}).*', '$1'
                        if ($productCode -match '{.*}') {
                            Log "  Using MSI product code: $productCode"
                            $proc = Start-Process msiexec.exe -ArgumentList "/x $productCode /qn /norestart" -Wait -NoNewWindow -PassThru
                            if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 3010) {
                                Log "  SUCCESS: $displayName uninstalled (exit code: $($proc.ExitCode))"
                                $uninstalledCount++
                            } else {
                                Log "  WARNING: Exit code $($proc.ExitCode) for $displayName"
                                $failedCount++
                            }
                        }
                    } else {
                        # EXE-based uninstall
                        Log "  Using EXE uninstaller"
                        $proc = Start-Process $uninstallString -ArgumentList "/S /NORESTART" -Wait -NoNewWindow -PassThru -ErrorAction SilentlyContinue
                        if ($proc -and ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 3010)) {
                            Log "  SUCCESS: $displayName uninstalled"
                            $uninstalledCount++
                        } else {
                            # Try without arguments
                            $proc = Start-Process $uninstallString -Wait -NoNewWindow -PassThru -ErrorAction SilentlyContinue
                            if ($proc) {
                                Log "  Completed: $displayName (exit code: $($proc.ExitCode))"
                                $uninstalledCount++
                            }
                        }
                    }
                } catch {
                    Log "  ERROR: Failed to uninstall $displayName - $($_.Exception.Message)"
                    $failedCount++
                }
            }
        }
    }
}

# Also try WMI-based uninstall for any remaining Nutanix products
Log "Checking for remaining Nutanix products via WMI..."
try {
    $wmiProducts = Get-WmiObject -Class Win32_Product -ErrorAction SilentlyContinue | 
        Where-Object { $_.Name -like "*Nutanix*" }
    
    foreach ($product in $wmiProducts) {
        Log "Found via WMI: $($product.Name)"
        try {
            $result = $product.Uninstall()
            if ($result.ReturnValue -eq 0) {
                Log "  SUCCESS: $($product.Name) uninstalled via WMI"
                $uninstalledCount++
            } else {
                Log "  WARNING: WMI uninstall returned $($result.ReturnValue)"
            }
        } catch {
            Log "  ERROR: WMI uninstall failed - $($_.Exception.Message)"
        }
    }
} catch {
    Log "WMI query failed (may not have Nutanix products): $($_.Exception.Message)"
}

Log "=========================================="
Log "Uninstallation Summary"
Log "  Uninstalled: $uninstalledCount"
Log "  Failed: $failedCount"
Log "=========================================="

# Return result
if ($uninstalledCount -gt 0) {
    Write-Output "UNINSTALLED:$uninstalledCount"
} elseif ($failedCount -eq 0) {
    Write-Output "NONE_FOUND"
} else {
    Write-Output "FAILED:$failedCount"
}
'''

    # Script to install Red Hat VirtIO drivers from ISO
    PS_INSTALL_VIRTIO_REDHAT = r'''
$ErrorActionPreference = "Continue"
$logFile = "C:\temp\virtio-install.log"

# Ensure log directory exists
if (-not (Test-Path "C:\temp")) {
    New-Item -ItemType Directory -Path "C:\temp" -Force | Out-Null
}

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts - $msg" | Tee-Object -FilePath $logFile -Append
}

Log "=========================================="
Log "Red Hat VirtIO Drivers Installation"
Log "=========================================="

# Determine Windows version for driver folder
$osVersion = [System.Environment]::OSVersion.Version
$osBuild = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuild

Log "OS Version: $($osVersion.Major).$($osVersion.Minor) Build $osBuild"

# Map to virtio-win driver folder names
$driverFolder = switch -Regex ($osBuild) {
    "^(9200|9600)$"     { "2k12R2" }    # Server 2012 R2
    "^1(0240|4393)$"    { "2k16" }       # Server 2016
    "^1(7763|8362|8363)$" { "2k19" }     # Server 2019
    "^20348$"           { "2k22" }       # Server 2022
    "^2(2000|2621|2631|6100)$" { "2k25" } # Server 2025 / Windows 11
    default { 
        if ([int]$osBuild -ge 22000) { "2k22" }  # Fallback for newer
        else { "2k19" }  # Fallback for older
    }
}

Log "Using driver folder: $driverFolder"

# Download VirtIO ISO if not present
$isoUrl = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
$isoPath = "C:\temp\virtio-win.iso"

if (-not (Test-Path $isoPath)) {
    Log "Downloading VirtIO ISO..."
    try {
        # Try with BITS first (better for large files)
        Import-Module BitsTransfer -ErrorAction SilentlyContinue
        Start-BitsTransfer -Source $isoUrl -Destination $isoPath -ErrorAction Stop
        Log "Downloaded via BITS"
    } catch {
        # Fallback to WebClient
        Log "BITS failed, trying WebClient..."
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $wc = New-Object System.Net.WebClient
            $wc.DownloadFile($isoUrl, $isoPath)
            Log "Downloaded via WebClient"
        } catch {
            Log "ERROR: Failed to download ISO - $($_.Exception.Message)"
            Write-Output "DOWNLOAD_FAILED"
            exit 1
        }
    }
} else {
    Log "ISO already exists at $isoPath"
}

# Mount the ISO
Log "Mounting ISO..."
try {
    $mountResult = Mount-DiskImage -ImagePath $isoPath -PassThru -ErrorAction Stop
    $driveLetter = ($mountResult | Get-Volume).DriveLetter
    Log "Mounted at drive $driveLetter`:"
} catch {
    Log "ERROR: Failed to mount ISO - $($_.Exception.Message)"
    Write-Output "MOUNT_FAILED"
    exit 1
}

# Driver categories to install
$driverCategories = @(
    @{Name="NetKVM"; Desc="Network (virtio-net)"},
    @{Name="vioscsi"; Desc="SCSI Controller"},
    @{Name="viostor"; Desc="Block Storage"},
    @{Name="vioserial"; Desc="Serial Port (Guest Agent)"},
    @{Name="Balloon"; Desc="Memory Balloon"},
    @{Name="viogpudo"; Desc="GPU"},
    @{Name="viorng"; Desc="RNG"},
    @{Name="pvpanic"; Desc="PV Panic"},
    @{Name="fwcfg"; Desc="FW Config"},
    @{Name="vioinput"; Desc="Input"}
)

$installed = 0
$skipped = 0
$failed = 0

foreach ($driver in $driverCategories) {
    $driverPath = "${driveLetter}:\$($driver.Name)\$driverFolder\amd64"
    
    if (Test-Path $driverPath) {
        Log "Installing $($driver.Name) ($($driver.Desc))..."
        $infFiles = Get-ChildItem -Path $driverPath -Filter "*.inf" -ErrorAction SilentlyContinue
        
        foreach ($inf in $infFiles) {
            try {
                $result = pnputil.exe /add-driver $inf.FullName /install 2>&1
                if ($LASTEXITCODE -eq 0 -or $result -match "successfully") {
                    Log "  Installed: $($inf.Name)"
                    $installed++
                } elseif ($result -match "already exists") {
                    Log "  Skipped (exists): $($inf.Name)"
                    $skipped++
                } else {
                    Log "  Warning: $($inf.Name) - $result"
                    $failed++
                }
            } catch {
                Log "  Error installing $($inf.Name): $($_.Exception.Message)"
                $failed++
            }
        }
    } else {
        Log "Driver path not found: $driverPath"
        # Try without version folder
        $altPath = "${driveLetter}:\$($driver.Name)\amd64"
        if (Test-Path $altPath) {
            Log "  Trying alternate path: $altPath"
            $infFiles = Get-ChildItem -Path $altPath -Filter "*.inf" -ErrorAction SilentlyContinue
            foreach ($inf in $infFiles) {
                try {
                    pnputil.exe /add-driver $inf.FullName /install 2>&1 | Out-Null
                    Log "  Installed from alt path: $($inf.Name)"
                    $installed++
                } catch {
                    $failed++
                }
            }
        }
    }
}

# Install guest-tools MSI if present
$guestToolsMsi = "${driveLetter}:\virtio-win-gt-x64.msi"
if (Test-Path $guestToolsMsi) {
    Log "Installing virtio-win-guest-tools MSI..."
    try {
        $proc = Start-Process msiexec.exe -ArgumentList "/i `"$guestToolsMsi`" /qn /norestart" -Wait -NoNewWindow -PassThru
        if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 3010) {
            Log "Guest tools MSI installed successfully"
            $installed++
        } else {
            Log "Guest tools MSI exit code: $($proc.ExitCode)"
        }
    } catch {
        Log "Failed to install guest tools MSI: $($_.Exception.Message)"
    }
}

# Unmount ISO
Log "Unmounting ISO..."
try {
    Dismount-DiskImage -ImagePath $isoPath -ErrorAction SilentlyContinue
    Log "ISO unmounted"
} catch {
    Log "Warning: Could not unmount ISO"
}

# Restart QEMU Guest Agent service if present
$qemuga = Get-Service -Name "QEMU-GA" -ErrorAction SilentlyContinue
if ($qemuga) {
    Log "Restarting QEMU Guest Agent service..."
    Restart-Service -Name "QEMU-GA" -Force -ErrorAction SilentlyContinue
    Log "QEMU-GA service restarted"
}

Log "=========================================="
Log "Installation Summary"
Log "  Installed: $installed"
Log "  Skipped: $skipped"  
Log "  Failed: $failed"
Log "=========================================="

Write-Output "INSTALLED:$installed,SKIPPED:$skipped,FAILED:$failed"
'''

    def __init__(self, client: WinRMClient):
        """Initialize with WinRM client."""
        self.client = client
    
    def uninstall_all_nutanix(self) -> Tuple[bool, int, int]:
        """
        Uninstall ALL Nutanix software (Guest Tools, VirtIO, VM Mobility).
        
        Returns:
            Tuple of (success, uninstalled_count, failed_count)
        """
        stdout, stderr, rc = self.client.run_powershell(self.PS_UNINSTALL_ALL_NUTANIX)
        
        if "UNINSTALLED:" in stdout:
            count = int(stdout.split("UNINSTALLED:")[1].strip())
            return (True, count, 0)
        elif "NONE_FOUND" in stdout:
            return (True, 0, 0)
        elif "FAILED:" in stdout:
            count = int(stdout.split("FAILED:")[1].strip())
            return (False, 0, count)
        else:
            return (rc == 0, 0, 0)
    
    def install_virtio_redhat(self) -> Tuple[bool, int, int, int]:
        """
        Install Red Hat VirtIO drivers from ISO.
        
        Returns:
            Tuple of (success, installed_count, skipped_count, failed_count)
        """
        stdout, stderr, rc = self.client.run_powershell(self.PS_INSTALL_VIRTIO_REDHAT)
        
        if "INSTALLED:" in stdout:
            # Parse "INSTALLED:X,SKIPPED:Y,FAILED:Z"
            parts = stdout.strip().split(",")
            installed = int(parts[0].split(":")[1]) if len(parts) > 0 else 0
            skipped = int(parts[1].split(":")[1]) if len(parts) > 1 else 0
            failed = int(parts[2].split(":")[1]) if len(parts) > 2 else 0
            return (failed == 0, installed, skipped, failed)
        elif "DOWNLOAD_FAILED" in stdout or "MOUNT_FAILED" in stdout:
            return (False, 0, 0, 1)
        else:
            return (rc == 0, 0, 0, 0)
    
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
            "# Uninstall Nutanix Guest Tools",
            "Write-Host 'Removing Nutanix Guest Tools...' -ForegroundColor Cyan",
            '$ngtUninstall = Get-ItemProperty "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*" |',
            '    Where-Object { $_.DisplayName -like "*Nutanix*Guest*" }',
            "",
            "if ($ngtUninstall) {",
            "    $uninstallString = $ngtUninstall.UninstallString",
            '    if ($uninstallString -match "msiexec") {',
            "        $productCode = $uninstallString -replace '.*({[^}]+}).*', '$1'",
            '        Start-Process msiexec.exe -ArgumentList "/x $productCode /qn" -Wait -NoNewWindow',
            "        Write-Host 'NGT uninstalled' -ForegroundColor Green",
            "    } else {",
            '        Start-Process $uninstallString -ArgumentList "/S" -Wait -NoNewWindow',
            "        Write-Host 'NGT uninstalled' -ForegroundColor Green",
            "    }",
            "} else {",
            "    Write-Host 'NGT not found (already removed or not installed)' -ForegroundColor Yellow",
            "}",
            "",
            "Write-Host 'Post-migration configuration complete!' -ForegroundColor Green",
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
