#!/usr/bin/env python3
"""
Test VMI API to debug IP retrieval
"""
import requests
import yaml
import json
import sys
import warnings
warnings.filterwarnings('ignore')

# Load config
with open('config.yaml') as f:
    config = yaml.safe_load(f)

harvester = config.get('harvester', {})
base_url = harvester.get('api_url', 'https://10.16.16.130')
cert_file = harvester.get('cert_file')
key_file = harvester.get('key_file')
namespace = harvester.get('default_namespace', 'harvester-public')

# VM to test
vm_name = sys.argv[1] if len(sys.argv) > 1 else 'wlchgvaopefs1'

print(f"Testing VMI API for: {vm_name} in {namespace}")
print(f"Base URL: {base_url}")
print()

# Setup cert
cert = (cert_file, key_file) if cert_file and key_file else None

# Test 1: Harvester v1 API (what UI uses)
print("=" * 60)
print("Test 1: Harvester v1 API")
print("=" * 60)
url1 = f"{base_url}/v1/kubevirt.io.virtualmachineinstances/{namespace}/{vm_name}"
print(f"URL: {url1}")

try:
    r = requests.get(url1, cert=cert, verify=False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        print(f"Phase: {data.get('status', {}).get('phase', 'N/A')}")
        interfaces = data.get('status', {}).get('interfaces', [])
        print(f"Interfaces: {len(interfaces)}")
        for i, iface in enumerate(interfaces):
            print(f"  Interface {i}:")
            print(f"    name: {iface.get('name', 'N/A')}")
            print(f"    mac: {iface.get('mac', 'N/A')}")
            print(f"    ipAddress: {iface.get('ipAddress', 'N/A')}")
            print(f"    ipAddresses: {iface.get('ipAddresses', [])}")
            print(f"    infoSource: {iface.get('infoSource', 'N/A')}")
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 2: KubeVirt API
print("=" * 60)
print("Test 2: KubeVirt API")
print("=" * 60)
url2 = f"{base_url}/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{vm_name}"
print(f"URL: {url2}")

try:
    r = requests.get(url2, cert=cert, verify=False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        print(f"Phase: {data.get('status', {}).get('phase', 'N/A')}")
        interfaces = data.get('status', {}).get('interfaces', [])
        print(f"Interfaces: {len(interfaces)}")
        for i, iface in enumerate(interfaces):
            print(f"  Interface {i}:")
            print(f"    name: {iface.get('name', 'N/A')}")
            print(f"    mac: {iface.get('mac', 'N/A')}")
            print(f"    ipAddress: {iface.get('ipAddress', 'N/A')}")
            print(f"    ipAddresses: {iface.get('ipAddresses', [])}")
            print(f"    infoSource: {iface.get('infoSource', 'N/A')}")
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 3: Check VM annotations
print("=" * 60)
print("Test 3: VM network.harvesterhci.io/ips annotation")
print("=" * 60)
url3 = f"{base_url}/v1/kubevirt.io.virtualmachines/{namespace}/{vm_name}"
print(f"URL: {url3}")

try:
    r = requests.get(url3, cert=cert, verify=False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        annotations = data.get('metadata', {}).get('annotations', {})
        ips_annotation = annotations.get('network.harvesterhci.io/ips', '[]')
        print(f"network.harvesterhci.io/ips: {ips_annotation}")
        
        # Check status
        status = data.get('status', {})
        print(f"printableStatus: {status.get('printableStatus', 'N/A')}")
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")
