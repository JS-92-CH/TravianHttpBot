# proxy_util.py

import random
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from config import log

def parse_proxy_file(file_path="proxies.txt"):
    """Parses the proxy file and returns a list of proxy dictionaries."""
    proxies = []
    try:
        with open(file_path, "r") as f:
            for line in f:
                parts = line.strip().split(':')
                if len(parts) == 4:
                    proxies.append({
                        "ip": parts[0],
                        "port": parts[1],
                        "username": parts[2],
                        "password": parts[3]
                    })
    except FileNotFoundError:
        print(f"Proxy file not found at {file_path}")
    return proxies

def check_proxy_speed(proxy):
    """Checks the speed of a single proxy and returns its latency."""
    proxy_url = f"http://{proxy['username']}:{proxy['password']}@{proxy['ip']}:{proxy['port']}"
    try:
        start_time = time.time()
        # Using a reliable site for testing
        response = requests.get("http://httpbin.org/get", proxies={"http": proxy_url, "https": proxy_url}, timeout=5)
        response.raise_for_status()  # This will raise an error for bad status codes
        end_time = time.time()
        latency = end_time - start_time
        return (proxy, latency)
    except requests.RequestException:
        # This will catch timeouts, connection errors, etc.
        return (proxy, float('inf'))

def get_fastest_proxies(num_to_check=10, num_to_return=3):
    """
    Pings a random selection of proxies and returns the top fastest proxies.
    """
    proxies = parse_proxy_file()
    if not proxies:
        return []

    # Ensure we don't try to sample more proxies than available
    if len(proxies) < num_to_check:
        proxies_to_check = proxies
    else:
        proxies_to_check = random.sample(proxies, num_to_check)
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(check_proxy_speed, proxies_to_check))

    # Filter out failed proxies and sort by speed
    successful_proxies = [res for res in results if res and res[1] != float('inf')]
    successful_proxies.sort(key=lambda x: x[1])

    # Return the proxy dictionaries themselves, not the tuples with latency
    return [p[0] for p in successful_proxies[:num_to_return]]

def test_proxy(proxy):
    """
    Tests if a connection can be successfully made through a given proxy.
    Returns True if the proxy is working, False otherwise.
    """
    if not proxy or not proxy.get('ip'):
        return True # No proxy is configured, so no test is needed.

    log.info(f"Testing proxy connection for: {proxy['ip']}:{proxy['port']}")
    proxy_url = f"http://{proxy.get('username', '')}:{proxy.get('password', '')}@{proxy['ip']}:{proxy['port']}"
    try:
        # We use a reliable, lightweight endpoint for this check.
        response = requests.get("http://httpbin.org/get", proxies={"http": proxy_url, "https": proxy_url}, timeout=10)
        # raise_for_status() will throw an exception for any HTTP error codes (4xx or 5xx)
        response.raise_for_status()
        log.info(f"Proxy {proxy['ip']}:{proxy['port']} is working correctly.")
        return True
    except requests.RequestException as e:
        log.error(f"Proxy test failed for {proxy['ip']}:{proxy['port']}. Reason: {e}")
        return False