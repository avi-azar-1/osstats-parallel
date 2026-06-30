#!/usr/bin/env python3
"""
Discover Redis instances across servers and generate config.ini for osstats.

Usage:
    python prepare.py <servers_file> [-o config.ini]

Assumes:
    - Passwordless root SSH to all listed servers
    - All redis-server processes are running
    - All Redis instances have requirepass set in their config
"""

import argparse
import subprocess
import re
import sys
from concurrent.futures import ThreadPoolExecutor


def expand_server_line(line):
    """Expand a server line into one or more hostnames.

    Examples:
        "redis01" -> ["redis01"]
        "redis01-03" -> ["redis01", "redis02", "redis03"]
        "server1" -> ["server1"]
        "server3-5" -> ["server3", "server4", "server5"]
        "node008-012" -> ["node008", "node009", "node010", "node011", "node012"]
    """
    match = re.match(r"^([a-zA-Z]+)(\d+)-(\d+)$", line)
    if match:
        prefix = match.group(1)
        start_str = match.group(2)
        end_str = match.group(3)
        width = len(start_str)
        start = int(start_str)
        end = int(end_str)
        return [f"{prefix}{str(n).zfill(width)}" for n in range(start, end + 1)]

    return [line]


def ssh_exec(host, command):
    """Execute command on remote host via SSH as root."""
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "root@" + host, command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout, result.stderr, result.returncode


def extract_port(ps_line):
    """Extract port from redis-server process line."""
    match = re.search(r"[\*0-9.:]+:(\d+)", ps_line)
    if match:
        return int(match.group(1))
    match = re.search(r"--port\s+(\d+)", ps_line)
    if match:
        return int(match.group(1))
    return 6379


def extract_config_path(ps_line):
    """Extract config file path from redis-server process line."""
    match = re.search(r"redis-server\s+(/\S+\.conf)", ps_line)
    if match:
        return match.group(1)
    return None


def get_password(host, config_path, port):
    """Read requirepass from redis config file on remote host."""
    paths_to_try = []
    if config_path:
        paths_to_try.append(config_path)
    paths_to_try.extend(
        [
            f"/etc/redis/{port}.conf",
            f"/etc/{port}.conf",
        ]
    )

    for path in paths_to_try:
        stdout, _, rc = ssh_exec(host, f"grep -E '^requirepass' {path} 2>/dev/null")
        if rc == 0 and stdout.strip():
            match = re.match(r"requirepass\s+(.+)", stdout.strip())
            if match:
                return match.group(1).strip().strip('"').strip("'")
    return None


def get_cluster_id(host, port, password):
    """Get a unique cluster fingerprint, or None if instance is standalone."""
    auth = f"-a '{password}' --no-auth-warning" if password else ""
    cmd = f"redis-cli -h 127.0.0.1 -p {port} {auth} info cluster 2>/dev/null"
    stdout, _, rc = ssh_exec(host, cmd)
    if rc != 0 or "cluster_enabled:1" not in stdout:
        return None

    cmd = f"redis-cli -h 127.0.0.1 -p {port} {auth} cluster nodes 2>/dev/null"
    stdout, _, rc = ssh_exec(host, cmd)
    if rc != 0 or not stdout.strip():
        return None

    node_ids = sorted(line.split()[0] for line in stdout.strip().splitlines() if line)
    return "|".join(node_ids)


def discover_redis_instances(host):
    """Discover all running Redis instances on a host.
    Returns list of (port, password) tuples."""
    instances = []

    stdout, _, _ = ssh_exec(host, "ps -eo pid,args | grep '[r]edis-server'")

    for line in stdout.strip().splitlines():
        port = extract_port(line)
        config_path = extract_config_path(line)
        password = get_password(host, config_path, port)
        if port:
            instances.append((port, password))

    return instances


def generate_config(instances):
    """Generate config.ini content from discovered instances."""
    lines = []
    for idx, (host, port, password) in enumerate(instances, 1):
        lines.append(f"[redis-{idx}]")
        lines.append(f"host        = {host}")
        lines.append(f"port        = {port}")
        lines.append(f"password    = {password or ''}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Discover Redis instances and generate config.ini for osstats"
    )
    parser.add_argument(
        "servers_file", help="File with server hostnames/IPs, one per line"
    )
    parser.add_argument(
        "-o", "--output", default="config.ini", help="Output config file path"
    )
    args = parser.parse_args()

    with open(args.servers_file) as f:
        raw_lines = [
            line.strip() for line in f if line.strip() and not line.startswith("#")
        ]

    servers = list(dict.fromkeys(s for raw in raw_lines for s in expand_server_line(raw)))

    all_instances = []

    with ThreadPoolExecutor() as pool:
        results = pool.map(discover_redis_instances, servers)
    for server, instances in zip(servers, results):
        for port, password in instances:
            all_instances.append((server, port, password))

    def check_cluster(instance):
        host, port, password = instance
        return instance, get_cluster_id(host, port, password)

    seen_clusters = set()
    deduplicated = []

    with ThreadPoolExecutor() as pool:
        cluster_results = list(pool.map(check_cluster, all_instances))

    for instance, cluster_id in cluster_results:
        if cluster_id is None:
            deduplicated.append(instance)
        elif cluster_id not in seen_clusters:
            seen_clusters.add(cluster_id)
            deduplicated.append(instance)

    config_content = generate_config(deduplicated)
    with open(args.output, "w") as f:
        f.write(config_content)

    print(
        f"Generated {args.output} with {len(deduplicated)} Redis instances "
        f"({len(all_instances) - len(deduplicated)} cluster duplicates removed) "
        f"from {len(servers)} servers"
    )


if __name__ == "__main__":
    main()
