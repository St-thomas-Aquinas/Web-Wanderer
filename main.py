#!/usr/bin/env python3
"""Test script to verify GitHub token works for Gist API"""

import os
import requests
import json

token = os.getenv("GITHUB_TOKEN", "").strip()
if not token:
    print("❌ Set GITHUB_TOKEN environment variable first")
    print("   export GITHUB_TOKEN=ghp_your_token_here")
    exit(1)

# ✅ Use 'token' prefix for classic PATs
headers = {
    "Authorization": f"token {token}",
    "Accept": "application/vnd.github.v3+json"
}

print("🔍 Testing GitHub Gist API...")
print(f"   Token: {token[:10]}...{token[-4:]} (len={len(token)})")

# Test 1: List gists
print("\n1️⃣  Testing: List gists")
resp = requests.get("https://api.github.com/gists", headers=headers, timeout=10)
print(f"   Status: {resp.status_code}")
if resp.status_code == 200:
    print("   ✅ Token works for reading gists!")
else:
    print(f"   ❌ Error: {resp.text}")
    exit(1)

# Test 2: Create a test gist
print("\n2️⃣  Testing: Create gist")
payload = {
    "description": "web-wanderer-test",
    "public": False,
    "files": {"test.txt": {"content": "Hello from Web Wanderer!"}}
}
resp = requests.post("https://api.github.com/gists", headers=headers, json=payload, timeout=10)
print(f"   Status: {resp.status_code}")

if resp.status_code in (200, 201):
    gist_url = resp.json()['html_url']
    print(f"   ✅ Token works for creating gists!")
    print(f"   📎 Test Gist: {gist_url}")
    
    # Cleanup: delete test gist
    gist_id = resp.json()['id']
    requests.delete(f"https://api.github.com/gists/{gist_id}", headers=headers)
    print("   🗑️  Test gist deleted")
else:
    print(f"   ❌ Error: {resp.text}")
    if resp.status_code == 403:
        print("\n💡 Common fixes:")
        print("   1. Token must have 'gist' scope (check at github.com/settings/tokens)")
        print("   2. Header must be 'token YOUR_TOKEN' (NOT 'Bearer')")
        print("   3. No spaces/quotes in environment variable")
    exit(1)

print("\n🎉 All tests passed! Your token is ready.")
