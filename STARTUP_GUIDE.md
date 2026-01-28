# Startup & Testing Guide

Complete guide for running the Distributed Energy Trading System on single or multiple devices.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Single Device Testing](#single-device-testing)
3. [Multi-Device Testing](#multi-device-testing)
4. [Edge Case Testing Scenarios](#edge-case-testing-scenarios)
5. [Troubleshooting](#troubleshooting)
6. [Quick Reference](#quick-reference)

---

## Prerequisites

### Software Requirements
- Python 3.8 or higher
- No external dependencies (standard library only)

### Verify Python Installation
```powershell
python --version
# Should show: Python 3.8.x or higher
```

### Download/Clone the Project
```powershell
cd c:\DSproject2
dir
# Should show: node.py, config.py, multicast.py, etc.
```

---

## Single Device Testing

### Quick Start (3 Terminals)

Open **3 separate PowerShell/Terminal windows** and run:

**Terminal 1:**
```powershell
cd c:\DSproject2
python node.py 1
```

**Terminal 2:**
```powershell
cd c:\DSproject2
python node.py 2
```

**Terminal 3:**
```powershell
cd c:\DSproject2
python node.py 3
```

### What Happens:

```
Timeline:
─────────────────────────────────────────────────────
0s    Node 1 starts, sends JOIN, waits 2s
2s    Node 1: No response → starts election → becomes coordinator
      
0s    Node 2 starts, sends JOIN
      Node 1 receives JOIN, sends JOIN_RESPONSE
2s    Node 2: Learns Node 1 is coordinator
      Node 2: Starts election (has higher ID)
      Node 2: Sends ELECTION to higher nodes (none)
      Node 2: Becomes coordinator

0s    Node 3 starts, sends JOIN
2s    Node 3: Starts election (has highest ID)
      Node 3: No higher nodes → becomes COORDINATOR
      
FINAL: Node 3 is coordinator, Nodes 1 & 2 are followers
```

### Verify Everything Works:

```
Node 1> status
  Role: FOLLOWER
  Coordinator: Node 3
  Balance: 100 credits
  Active Nodes: [1, 2, 3]

Node 3> status
  Role: COORDINATOR
  Coordinator: Node 3
  Balance: 100 credits
```

### Test a Trade:

```
Node 1> sell 2 30
# Node 1: SELL 30 credits to Node 2 [100 → 70]
# Node 2: BUY 30 credits from Node 1 [100 → 130]

Node 1> balance
Balance: 70 credits

Node 2> balance
Balance: 130 credits
```

---

## Multi-Device Testing

### Network Requirements

All devices must be on the **same network**:
- ✅ Same WiFi network
- ✅ Same mobile hotspot
- ✅ Same ethernet switch
- ❌ Different networks (won't work)

### Option 1: Fully Automatic (Recommended)

IP addresses are auto-detected. Just run:

**Laptop A:**
```bash
python node.py 1
```

**Laptop B:**
```bash
python node.py 2
```

**Laptop C:**
```bash
python node.py 3
```

The system will:
1. Auto-detect each laptop's IP (e.g., 192.168.1.10)
2. Learn peer IPs from multicast messages
3. Set up unicast communication automatically

### Option 2: Manual Host (If Auto-Detection Fails)

If auto-detection picks wrong IP (Docker, VPN, etc.):

```bash
# First, find your IP:
ipconfig    # Windows
ifconfig    # Mac/Linux

# Then specify it:
python node.py 1 --host 192.168.1.10
python node.py 2 --host 192.168.1.11
python node.py 3 --host 192.168.1.12
```

### Option 3: Manual Peers (If Multicast Blocked)

If multicast doesn't work on your network:

```bash
python node.py 1 --host 192.168.1.10 --peers 2:192.168.1.11,3:192.168.1.12
python node.py 2 --host 192.168.1.11 --peers 1:192.168.1.10,3:192.168.1.12
python node.py 3 --host 192.168.1.12 --peers 1:192.168.1.10,2:192.168.1.11
```

### Pre-Demo Checklist

- [ ] All devices on same WiFi/hotspot
- [ ] Firewall disabled OR UDP ports 5007, 6001-6003 allowed
- [ ] Node IDs assigned (Teammate A=1, B=2, C=3)
- [ ] Test ping between devices: `ping 192.168.1.xx`

---

## Edge Case Testing Scenarios

### Scenario 1: Leader Crash & Re-Election

**Purpose:** Test heartbeat failure detection and Bully algorithm

**Steps:**
```
1. Start all 3 nodes
2. Verify Node 3 is coordinator: status
3. Kill Node 3: Ctrl+C (NOT 'quit' - simulate crash)
4. Wait 10-20 seconds
5. Check Node 1 or 2: status
```

**Expected:**
```
+0s   Node 3 killed
+10s  "Node 3 SUSPECTED (missed heartbeat timeout)"
+20s  "Node 3 FAILED (sustained heartbeat absence)"
      "LEADER FAILED - triggering election"
+21s  Node 2 becomes new coordinator (highest alive)
```

**Verify:**
```
Node 1> status
  Role: FOLLOWER
  Coordinator: Node 2  ← Changed from 3!
```

---

### Scenario 2: Late Joiner (Dynamic Discovery)

**Purpose:** Test JOIN_RESPONSE and vector clock sync

**Steps:**
```
1. Start only Node 1 and Node 2
2. Do some trades between them
3. Start Node 3 later
4. Check Node 3's status
```

**Expected:**
```
# Node 3 receives JOIN_RESPONSE with:
# - Current vector clock state
# - List of known nodes
# - Coordinator ID

Node 3> status
  Vector Clock: [1:X, 2:Y, 3:0]  ← Synced from coordinator!
  Coordinator: Node 2
```

---

### Scenario 3: Graceful Leave vs Crash

**Purpose:** Understand difference between quit and Ctrl+C

**Graceful Leave (quit command):**
```
Node 2> quit
# Sends LEAVE message
# Other nodes immediately remove Node 2
# If coordinator, triggers election immediately
```

**Crash (Ctrl+C):**
```
# No LEAVE message sent
# Other nodes must detect via heartbeat timeout
# Takes 10-20 seconds to detect
```

---

### Scenario 4: Simultaneous Startup (Split-Brain Prevention)

**Purpose:** Test that only one coordinator emerges

**Steps:**
```
1. Start all 3 nodes at EXACTLY the same time
2. Wait for election to complete
3. Verify only ONE coordinator exists
```

**Expected:**
```
# Multiple nodes may start elections
# But Bully algorithm guarantees:
# - Higher ID always wins
# - Node 3 becomes sole coordinator

All nodes> status
  Coordinator: Node 3  ← Same on all nodes!
```

---

### Scenario 5: Trade During Election

**Purpose:** Test message buffering during leadership transition

**Steps:**
```
1. Start all 3 nodes, Node 3 is coordinator
2. Kill Node 3 (Ctrl+C)
3. IMMEDIATELY try to trade: Node 1> sell 2 20
4. Observe what happens
```

**Expected:**
```
# Trade may be buffered or delayed
# After new coordinator (Node 2) elected, trade proceeds
# No credits are lost
```

---

### Scenario 6: Duplicate Node ID (Safety Check)

**Purpose:** Verify port collision prevents duplicate IDs

**Steps:**
```
Terminal 1: python node.py 1
Terminal 2: python node.py 1  ← Same ID!
```

**Expected:**
```
# Terminal 2 fails with:
OSError: [Errno 10048] Address already in use
# Port 6001 is already taken by first Node 1
```

---

### Scenario 7: Network Partition Simulation

**Purpose:** Test behavior when nodes can't communicate

**Steps:**
```
1. Start all 3 nodes
2. On Node 1's machine, block traffic:
   # Windows: netsh advfirewall firewall add rule...
   # Or just disconnect WiFi briefly
3. Observe other nodes detect Node 1's "failure"
4. Reconnect Node 1
```

**Expected:**
```
# Nodes 2 & 3: "Node 1 SUSPECTED" → "Node 1 FAILED"
# Node 1: May elect itself coordinator (partition)
# After reconnection: System should converge
```

---

### Scenario 8: High-Frequency Trading

**Purpose:** Test vector clock ordering under load

**Steps:**
```
1. Start 3 nodes
2. Rapidly execute trades:
   Node 1> sell 2 10
   Node 1> sell 3 10
   Node 2> sell 1 5
   Node 2> sell 3 5
3. Check final balances
```

**Expected:**
```
# All trades should complete
# Total credits in system = 300 (100 × 3)
# Sum of all balances should equal 300

Node 1> balance  # e.g., 85
Node 2> balance  # e.g., 105
Node 3> balance  # e.g., 110
                 # Total: 300 ✓
```

---

### Scenario 9: Insufficient Credits Trade

**Purpose:** Test trade rejection

**Steps:**
```
Node 1> sell 2 150  ← More than balance (100)
```

**Expected:**
```
Cannot sell 150 energy credits - insufficient balance
# Trade is rejected locally, not sent
```

---

### Scenario 10: Multi-Device Dynamic Discovery

**Purpose:** Verify IP auto-detection across devices

**Steps:**
```
# Laptop A (don't know other IPs yet)
python node.py 1

# Laptop B
python node.py 2

# Laptop C
python node.py 3
```

**Expected Logs:**
```
# Laptop A:
Auto-detected local IP: 192.168.1.10
Discovered peer Node 2 at 192.168.1.11
Discovered peer Node 3 at 192.168.1.12

# All nodes should see each other without --peers!
```

---

## Troubleshooting

### Problem: "Address already in use"
```
OSError: [Errno 10048] Address already in use
```
**Solution:** Another node with same ID is running. Kill it or use different ID.

### Problem: Nodes don't see each other (Multi-Device)
```
# Symptoms:
# - No "Discovered peer" messages
# - Each node elects itself coordinator
```
**Solutions:**
1. Check same WiFi network
2. Disable firewall: `netsh advfirewall set allprofiles state off`
3. Use mobile hotspot instead of university WiFi
4. Use `--peers` option for manual configuration

### Problem: Multicast not working
```
# Symptoms:
# - Heartbeats not received
# - JOIN messages ignored
```
**Solution:** Use `--peers` option:
```bash
python node.py 1 --host YOUR_IP --peers 2:PEER2_IP,3:PEER3_IP
```

### Problem: Wrong IP auto-detected
```
# Log shows Docker/VPN IP instead of WiFi
Auto-detected local IP: 172.17.0.1  ← Wrong!
```
**Solution:** Specify manually:
```bash
python node.py 1 --host 192.168.1.10
```

### Problem: Election never completes
```
# Nodes keep starting elections
```
**Solutions:**
1. Check all nodes have unique IDs
2. Increase `HEARTBEAT_TIMEOUT` in config.py
3. Check network connectivity

### Problem: Trades fail silently
```
# Trade initiated but no confirmation
```
**Solutions:**
1. Check both nodes are running
2. Verify coordinator is alive
3. Check `history` command for pending trades

---

## Quick Reference

### Commands
| Command | Description |
|---------|-------------|
| `status` | Show role, coordinator, balance, clock |
| `balance` | Show current credits |
| `sell N A` | Sell A credits to Node N |
| `buy N A` | Buy A credits from Node N |
| `nodes` | List all known nodes |
| `history` | Transaction history |
| `election` | Force new election |
| `quit` | Graceful shutdown |
| `help` | Show all commands |

### CLI Arguments
| Argument | Description | Example |
|----------|-------------|---------|
| `node_id` | Unique node ID (required) | `1` |
| `--host` | Bind IP (auto-detected) | `--host 192.168.1.10` |
| `--peers` | Peer IP mappings | `--peers 2:192.168.1.11,3:192.168.1.12` |
| `--nodes` | Pre-known node IDs | `--nodes 2,3` |
| `--debug` | Verbose logging | `--debug` |
| `--quiet` | Minimal logging | `--quiet` |

### Ports
| Port | Purpose |
|------|---------|
| 5007 | Multicast (all nodes) |
| 6000+N | Unicast for Node N |

### Timing (Configurable in config.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `HEARTBEAT_INTERVAL` | 2s | Send heartbeat every 2s |
| `HEARTBEAT_TIMEOUT` | 10s | Suspect node after 10s |
| `ELECTION_TIMEOUT` | 5s | Wait for OK response |

---

## Demo Day Checklist

### Before Demo:
- [ ] All 3 laptops on same network
- [ ] Firewalls disabled
- [ ] Node IDs assigned (1, 2, 3)
- [ ] Tested `ping` between devices

### Demo Script:
```
1. Start all nodes: python node.py N
2. Show status on each node
3. Execute a trade: sell 2 30
4. Kill coordinator (Ctrl+C)
5. Show re-election happens
6. Show new coordinator
7. Execute another trade
8. Graceful shutdown: quit
```

### If Things Go Wrong:
1. **Nodes don't connect:** Use mobile hotspot
2. **Multicast fails:** Add `--peers` argument
3. **Wrong IP:** Add `--host` argument
4. **Port conflict:** Check for duplicate node IDs
