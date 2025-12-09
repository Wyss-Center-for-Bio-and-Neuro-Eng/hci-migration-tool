#!/usr/bin/env python3
"""
Test VMI API to debug IP retrieval - Uses raw API calls
"""
import yaml
import json
import sys
import requests
import base64
import tempfile
import os
import warnings
warnings.filterwarnings('ignore')

# Load config
with open('config.yaml') as f:
    config = yaml.safe_load(f)

harvester_config = config.get('harvester', {})
base_url = harvester_config.get('api_url')
namespace = harvester_config.get('default_namespace', 'harvester-public')

# VM to test
vm_name = sys.argv[1] if len(sys.argv) > 1 else 'wlchgvaopefs1'

print(f"Testing VMI API for: {vm_name} in {namespace}")
print(f"Base URL: {base_url}")
print()

# Setup certificates from base64 data
cert_data = harvester_config.get('client_certificate_data')
key_data = harvester_config.get('client_key_data')
ca_data = harvester_config.get('certificate_authority_data')

cert = None
verify = False

if cert_data and key_data:
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix='.crt')
    cert_file.write(base64.b64decode(cert_data))
    cert_file.close()
    
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix='.key')
    key_file.write(base64.b64decode(key_data))
    key_file.close()
    
    cert = (cert_file.name, key_file.name)
    print(f"✅ Using client certificate")

if ca_data:
    ca_file = tempfile.NamedTemporaryFile(delete=False, suffix='.crt')
    ca_file.write(base64.b64decode(ca_data))
    ca_file.close()
    verify = ca_file.name
    print(f"✅ Using CA certificate")

print()

# Test 1: KubeVirt API - Get VMI
print("=" * 60)
print("Test 1: KubeVirt API - /apis/kubevirt.io/v1/")
print("=" * 60)
url = f"{base_url}/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{vm_name}"
print(f"URL: {url}")

try:
    r = requests.get(url, cert=cert, verify=verify if verify else False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        print(f"Phase: {data.get('status', {}).get('phase', 'N/A')}")
        
        # Show raw interfaces
        interfaces = data.get('status', {}).get('interfaces', [])
        print(f"Interfaces count: {len(interfaces)}")
        
        if interfaces:
            print("\nRaw interfaces data:")
            print(json.dumps(interfaces, indent=2))
        else:
            print("\n⚠️  No interfaces in status!")
            print("\nFull status keys:", list(data.get('status', {}).keys()))
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 2: Get VM (not VMI) to check annotations
print("=" * 60)
print("Test 2: VM annotations - /apis/kubevirt.io/v1/virtualmachines")
print("=" * 60)
url = f"{base_url}/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{vm_name}"
print(f"URL: {url}")

try:
    r = requests.get(url, cert=cert, verify=verify if verify else False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        annotations = data.get('metadata', {}).get('annotations', {})
        
        # Show relevant annotations
        print("\nRelevant annotations:")
        for key in ['network.harvesterhci.io/ips', 'harvesterhci.io/mac-address']:
            print(f"  {key}: {annotations.get(key, 'N/A')}")
        
        print(f"\nStatus.printableStatus: {data.get('status', {}).get('printableStatus', 'N/A')}")
        print(f"Status.ready: {data.get('status', {}).get('ready', 'N/A')}")
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

print()

# Test 3: List all VMIs to see if ours exists
print("=" * 60)
print("Test 3: List all VMIs in namespace")
print("=" * 60)
url = f"{base_url}/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances"
print(f"URL: {url}")

try:
    r = requests.get(url, cert=cert, verify=verify if verify else False)
    print(f"Status: {r.status_code}")
    if r.ok:
        data = r.json()
        items = data.get('items', [])
        print(f"Found {len(items)} VMI(s):")
        for item in items:
            name = item.get('metadata', {}).get('name', 'N/A')
            phase = item.get('status', {}).get('phase', 'N/A')
            interfaces = item.get('status', {}).get('interfaces', [])
            ips = [iface.get('ipAddress', '') for iface in interfaces if iface.get('ipAddress')]
            print(f"  - {name}: {phase}, IPs: {ips}")
    else:
        print(f"Error: {r.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

# Cleanup temp files
if cert:
    os.unlink(cert[0])
    os.unlink(cert[1])
if verify and verify != False:
    os.unlink(verify)
