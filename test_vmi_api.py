#!/usr/bin/env python3
"""
Test VMI API to debug IP retrieval
"""
import yaml
import json
import sys
import warnings
warnings.filterwarnings('ignore')

# Add lib to path
sys.path.insert(0, '.')
from lib.harvester import HarvesterClient

# Load config
with open('config.yaml') as f:
    config = yaml.safe_load(f)

harvester_config = config.get('harvester', {})
namespace = harvester_config.get('default_namespace', 'harvester-public')

# VM to test
vm_name = sys.argv[1] if len(sys.argv) > 1 else 'wlchgvaopefs1'

print(f"Testing VMI API for: {vm_name} in {namespace}")
print(f"Base URL: {harvester_config.get('api_url')}")
print()

# Create client (handles auth correctly)
client = HarvesterClient(harvester_config)

# Test 1: Get VMI via get_vmi method
print("=" * 60)
print("Test 1: get_vmi() method")
print("=" * 60)
try:
    vmi = client.get_vmi(vm_name, namespace, silent=False)
    print(f"Phase: {vmi.get('status', {}).get('phase', 'N/A')}")
    interfaces = vmi.get('status', {}).get('interfaces', [])
    print(f"Interfaces: {len(interfaces)}")
    for i, iface in enumerate(interfaces):
        print(f"  Interface {i}:")
        print(f"    name: {iface.get('name', 'N/A')}")
        print(f"    mac: {iface.get('mac', 'N/A')}")
        print(f"    ipAddress: {iface.get('ipAddress', 'N/A')}")
        print(f"    ipAddresses: {iface.get('ipAddresses', [])}")
        print(f"    infoSource: {iface.get('infoSource', 'N/A')}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 2: Get VM IP via get_vm_ip method
print("=" * 60)
print("Test 2: get_vm_ip() method")
print("=" * 60)
try:
    ips = client.get_vm_ip(vm_name, namespace)
    print(f"Result: {json.dumps(ips, indent=2)}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 3: Raw API call to Harvester v1 endpoint
print("=" * 60)
print("Test 3: Raw Harvester v1 API")
print("=" * 60)
import requests
url = f"{client.base_url}/v1/kubevirt.io.virtualmachineinstances/{namespace}/{vm_name}"
print(f"URL: {url}")
try:
    r = requests.get(url, cert=client.cert, verify=client.verify if client.verify else False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        print(f"Phase: {data.get('status', {}).get('phase', 'N/A')}")
        interfaces = data.get('status', {}).get('interfaces', [])
        print(f"Interfaces: {len(interfaces)}")
        for i, iface in enumerate(interfaces):
            print(f"  Interface {i}:")
            for key in ['name', 'mac', 'ipAddress', 'ipAddresses', 'infoSource']:
                print(f"    {key}: {iface.get(key, 'N/A')}")
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 4: Check VM annotations
print("=" * 60)
print("Test 4: VM annotations")
print("=" * 60)
try:
    vm = client.get_vm(vm_name, namespace)
    annotations = vm.get('metadata', {}).get('annotations', {})
    ips_annotation = annotations.get('network.harvesterhci.io/ips', '[]')
    print(f"network.harvesterhci.io/ips: {ips_annotation}")
    print(f"printableStatus: {vm.get('status', {}).get('printableStatus', 'N/A')}")
except Exception as e:
    print(f"Exception: {e}")
