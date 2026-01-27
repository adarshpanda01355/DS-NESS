"""
config.py - Configuration constants for the distributed energy trading system.

Responsibility:
    - Define network settings (multicast group, ports, buffer sizes)
    - Define timing parameters (heartbeat intervals, election timeouts)
    - Define node configuration defaults

Single-Machine Testing:
    This configuration is optimized for running multiple nodes on localhost.
    Each node uses UNICAST_PORT_BASE + node_id for its unicast port.
    All nodes share the same multicast group and port.
"""

import os
import socket

# ==============================================================================
# Network Configuration
# ==============================================================================

def get_local_ip():
    """
    Auto-detect the local IP address for multi-device communication.
    
    Uses a trick: create a UDP socket and "connect" to an external IP.
    This doesn't actually send data, but the OS determines which network
    interface would be used, revealing our local IP on that interface.
    
    Returns:
        str: Local IP address (e.g., "192.168.1.10") or "127.0.0.1" as fallback
    """
    try:
        # Create a UDP socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to Google's DNS (doesn't send data, just routes)
        s.connect(('8.8.8.8', 80))
        # Get the local IP that would be used for this connection
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        # Fallback to localhost if detection fails
        return '127.0.0.1'


# Multicast group IP address (must be in range 224.0.0.0 - 239.255.255.255)
# Using a locally-scoped multicast address for single-machine testing
MULTICAST_GROUP = os.environ.get("DS_MULTICAST_GROUP", "224.1.1.1")

# Port for multicast communication (all nodes listen on this port)
MULTICAST_PORT = int(os.environ.get("DS_MULTICAST_PORT", "5007"))

# Base port for unicast communication
# Each node uses UNICAST_PORT_BASE + node_id for direct messaging
# Example: Node 1 -> 6001, Node 2 -> 6002, Node 3 -> 6003
UNICAST_PORT_BASE = int(os.environ.get("DS_UNICAST_PORT_BASE", "6000"))

# Maximum size of UDP packets in bytes
# 4096 bytes is sufficient for JSON-serialized messages
BUFFER_SIZE = 4096

# Host address for unicast - localhost for single-machine testing
# For multi-machine deployment, IP is auto-detected or use --host argument
LOCALHOST = "127.0.0.1"

# ==============================================================================
# Timing Configuration (in seconds)
# ==============================================================================

# How often each node sends heartbeat messages to indicate liveness
# Lower values = faster failure detection but more network traffic
HEARTBEAT_INTERVAL = float(os.environ.get("DS_HEARTBEAT_INTERVAL", "2.0"))

# Time to wait before considering a node as failed
# Should be > HEARTBEAT_INTERVAL to account for network delays
# Typically 2-3x the heartbeat interval
HEARTBEAT_TIMEOUT = float(os.environ.get("DS_HEARTBEAT_TIMEOUT", "6.0"))

# Maximum time to wait for OK responses during leader election
# If no OK received within this time, node declares itself coordinator
ELECTION_TIMEOUT = float(os.environ.get("DS_ELECTION_TIMEOUT", "5.0"))

# ==============================================================================
# Message Retry Configuration
# ==============================================================================

# Number of times to retry sending a message before giving up
# Used for critical messages like trade confirmations
MESSAGE_RETRY_COUNT = 3

# Delay between message retries in seconds
MESSAGE_RETRY_DELAY = 1.0

# ==============================================================================
# Buffer and Synchronization Configuration
# ==============================================================================

# Interval for flushing/synchronizing buffered state (e.g., ledger updates)
# Coordinator broadcasts aggregated state at this interval
BUFFER_FLUSH_INTERVAL = 5.0

# ==============================================================================
# Energy Trading Configuration
# ==============================================================================

# Starting energy credits for each node
INITIAL_ENERGY_CREDITS = int(os.environ.get("DS_INITIAL_CREDITS", "100"))

# Minimum energy credits required (cannot go below this)
MIN_ENERGY_CREDITS = 0

# Maximum nodes supported in the system (for vector clock sizing)
MAX_NODES = 10

# ==============================================================================
# Logging Configuration
# ==============================================================================

# Log level: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL = os.environ.get("DS_LOG_LEVEL", "INFO")
