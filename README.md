# Distributed Energy Trading System

A distributed systems project demonstrating peer-to-peer energy credit trading using UDP multicast/unicast communication, vector clocks for causal ordering, the Bully algorithm for leader election, and heartbeat-based fault detection.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Technical Design Decisions](#technical-design-decisions)
4. [Project Structure](#project-structure)
5. [Getting Started](#getting-started)
6. [User Commands](#user-commands)
7. [Testing Scenarios](#testing-scenarios)
8. [Configuration](#configuration)
9. [Technical Challenges & Solutions](#technical-challenges--solutions)
10. [Lessons Learned](#lessons-learned)


## Overview

This system simulates a decentralized energy trading network where multiple nodes can:
- **Trade energy credits** with each other (buy/sell operations)
- **Elect a coordinator** using the Bully algorithm
- **Detect node failures** through heartbeat monitoring
- **Maintain causal ordering** of trade messages using vector clocks
- **Dynamically join/leave** the network

## Architecture

┌─────────────────────────────────────────────────────────────────────────────┐
│                              Node (node.py)                                 │
│                         Main Orchestrator Layer                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────┐ │
│  │ MulticastHandler│  │ UnicastHandler  │  │      VectorClock            │ │
│  │  (group comm)   │  │ (direct msgs)   │  │   (causal ordering)         │ │
│  │                 │  │                 │  │                             │ │
│  │ • JOIN/LEAVE    │  │ • ELECTION/OK   │  │ • Increment on send         │ │
│  │ • HEARTBEAT     │  │ • TRADE_REQUEST │  │ • Merge on receive          │ │
│  │ • COORDINATOR   │  │ • TRADE_CONFIRM │  │ • can_deliver() check       │ │
│  │ • send_reliable │  │ • JOIN_RESPONSE │  │ • Hold-back queue support   │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────────┘ │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────┐ │
│  │ HeartbeatManager│  │ ElectionManager │  │      EnergyLedger           │ │
│  │ (fault detect)  │  │ (Bully algo)    │  │   (credit tracking)         │ │
│  │                 │  │                 │  │                             │ │
│  │ • 2s interval   │  │ • Highest wins  │  │ • Balance management        │ │
│  │ • 10s timeout   │  │ • OK response   │  │ • Transaction history       │ │
│  │ • Two-phase:    │  │ • COORDINATOR   │  │ • Duplicate detection       │ │
│  │   SUSPECTED →   │  │   broadcast     │  │ • Pending trade tracking    │ │
│  │   FAILED        │  │                 │  │                             │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────────┘ │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                       MessageBuffer (Hold-back Queue)                │   │
│  │            Stores messages awaiting causal delivery                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Communication Layers

| Layer | Protocol | Purpose | Reliability |
|-------|----------|---------|-------------|
| **Multicast** | UDP 224.1.1.1:5007 | Group broadcasts (heartbeats, join, coordinator) | Reliable (3x retry) for critical messages |
| **Unicast** | UDP <host>:600X | Direct messaging (elections, trades) | Reliable (3x retry) for trades |

---

## Technical Design Decisions

### 1. Leader Election: Bully Algorithm with Integer Priority

**Why Bully Algorithm?**
- Simple to implement and understand
- Guarantees eventual leader selection
- Handles node failures during election gracefully

**Why Integer Node IDs (not UUIDs)?**
- Deterministic priority ordering (Node 3 > Node 2 > Node 1)
- Easier debugging and demonstration
- Sufficient for a bounded set of nodes (≤10)
- Trade-off: Less suitable for dynamic cloud environments where UUIDs prevent collision

**Implementation Details:**
- ELECTION messages sent via **unicast** to higher-ID nodes only (reduces message complexity)
- COORDINATOR announcements sent via **reliable multicast** (ensures all nodes receive)
- 5-second timeout for OK responses before declaring victory

### 2. Failure Detection: Two-Phase Heartbeat System

**Configuration:**
```python
HEARTBEAT_INTERVAL = 2.0 seconds  # Send frequency
HEARTBEAT_TIMEOUT = 10.0 seconds  # Detection threshold
```

**Why Two-Phase Detection (SUSPECTED → FAILED)?**
- **Reduces false positives**: Temporary network hiccups don't trigger unnecessary elections
- **More stable**: A single missed heartbeat doesn't crash the system
- **Trade-off**: Slower detection (10-20s) vs. Chat app's 5s

**Why 10s Timeout?**
- Our initial testing showed "split-brain" issues with faster timeouts
- Two nodes would both declare themselves coordinator
- 10s provides stability at the cost of slower failure response
- Acceptable for a demonstration system

### 3. Causal Ordering: Decoupled Vector Clocks

**Critical Design Decision: Heartbeats DO NOT participate in vector clock ordering**

**Why?**
- Heartbeats are **liveness probes**, not application events
- Including them in causal ordering caused clock values to climb into the hundreds
- Nodes that missed heartbeats would buffer ALL subsequent trade messages forever
- This was a critical bug discovered during testing (see [Technical Challenges](#technical-challenges--solutions))

**Implementation:**
```python
# Heartbeats: No vector clock
heartbeat_msg = create_heartbeat(sender_id, vector_clock=None, credits=100)

# Trades: Include vector clock
trade_msg = create_trade_request(sender_id, vector_clock=clock.increment(), ...)
```

**Vector Clock Entry Removal: DISABLED**

We intentionally **do not remove** vector clock entries when nodes leave:
- Prevents race conditions with the hold-back queue
- Messages buffered before a node left remain deliverable
- Memory overhead is negligible (max 10 nodes)

### 4. Reliable Communication: Selective Retry Strategy

**Why not retry everything?**
- Heartbeats: Single send (avoid network congestion)
- JOIN/COORDINATOR: 3x multicast (critical for membership)
- TRADE_CONFIRM: 5x unicast (financial integrity)

**Implementation:**
```python
# multicast.py
def send_reliable(self, message, retries=3, delay=0.1):
    for i in range(retries):
        self.send(message)
        if i < retries - 1:
            time.sleep(delay)
```

### 5. State Transfer: JOIN_RESPONSE Protocol

**Why is this needed?**

When Node 3 joins after Nodes 1 and 2 have traded, Node 3's vector clock starts at `{3: 0}`. Without state transfer:
- Node 3 receives a trade with `vc[1] = 50`
- Node 3's `can_deliver()` expects `vc[1] = 1` (next in sequence)
- Message is buffered **forever**

**Our Solution:**
1. New node broadcasts JOIN (reliable multicast)
2. Coordinator sends JOIN_RESPONSE via reliable unicast containing:
   - Current vector clock state
   - Coordinator ID
   - List of known nodes
3. New node initializes its clock from coordinator's state

**Trade-off vs. Chat Application:**
- Chat: Full hold-back queue replication (complex)
- Ours: Clock state only (simpler, sufficient for demo)

---

## Project Structure

```
DSproject2/
├── node.py           # Main orchestrator - coordinates all components
├── config.py         # Configuration constants (timeouts, ports, etc.)
├── message.py        # Message types and JSON serialization
├── vector_clock.py   # Vector clock implementation for causal ordering
├── multicast.py      # UDP multicast handler with reliable send
├── unicast.py        # UDP unicast handler with retry support
├── heartbeat.py      # Heartbeat-based fault detection
├── election.py       # Bully algorithm for leader election
├── ledger.py         # Energy credit tracking and transactions
├── README.md         # This documentation
├── STARTUP_GUIDE.md  # Complete startup and testing guide
├── ARCHITECTURAL_AUDIT.md  # Detailed comparison with Chat application
└── TEST_SCENARIOS.md # Manual testing procedures
```

---

## Getting Started

### Prerequisites

- Python 3.8 or higher
- No external dependencies (standard library only)
- Windows/Linux/macOS

### Quick Start: 3 Nodes

Open **three terminal windows**:

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

> **Note:** Nodes discover each other dynamically via multicast. The `--nodes` argument is optional (e.g., `python node.py 1 --nodes 2,3`) for pre-seeding known nodes.

### Multi-Device Deployment (3 Different Machines)

For testing across different devices on the same network:

**Option A: Fully Automatic (Recommended)**

IP addresses are auto-detected! Just run:

**Device 1:**
```bash
python node.py 1
```

**Device 2:**
```bash
python node.py 2
```

**Device 3:**
```bash
python node.py 3
```

**How it works:** 
1. `get_local_ip()` auto-detects your WiFi/network IP (e.g., 192.168.1.10)
2. When nodes send multicast messages, other nodes learn IPs from UDP packets
3. No manual configuration needed!

**Option B: Manual Host (If Auto-Detection Fails)**

If auto-detection picks the wrong interface (e.g., Docker), specify manually:

```bash
python node.py 1 --host 192.168.1.10
python node.py 2 --host 192.168.1.11
python node.py 3 --host 192.168.1.12
```

**Option C: With Pre-configured Peers (Multicast Issues)**

If multicast doesn't work on your network, specify peers manually:

```bash
python node.py 1 --host 192.168.1.10 --peers 2:192.168.1.11,3:192.168.1.12
python node.py 2 --host 192.168.1.11 --peers 1:192.168.1.10,3:192.168.1.12
python node.py 3 --host 192.168.1.12 --peers 1:192.168.1.10,2:192.168.1.11
```

**Requirements for multi-device:**
- All devices must be on the same network (same subnet for multicast)
- Firewall must allow UDP on ports 5007 (multicast) and 6001-6003 (unicast)
- Multicast may not work across WiFi on some routers (use Option C if issues)

### Demo Day Procedure (3 Devices)

**Before the demo:**
1. Connect all 3 devices to the **same WiFi network**
2. Assign node IDs: "Teammate A = Node 1, Teammate B = Node 2, Teammate C = Node 3"
3. Disable Windows Firewall OR allow UDP ports 5007, 6001, 6002, 6003

**Start the nodes (in any order) - just run:**
```bash
# Teammate A's laptop
python node.py 1

# Teammate B's laptop  
python node.py 2

# Teammate C's laptop
python node.py 3
```

**What you'll see:**
- First node starts, waits, then elects itself as coordinator
- Other nodes join, discover each other via multicast
- Node 3 becomes coordinator (highest ID wins Bully election)

### Expected Startup Sequence

1. **Node Registration**: Each node announces JOIN via multicast
2. **Dynamic Discovery**: Nodes learn each other's IPs from multicast packets
3. **State Sync**: Coordinator sends JOIN_RESPONSE to new nodes
4. **Election**: Bully algorithm elects Node 3 (highest ID)
5. **Heartbeats**: Periodic liveness messages every 2 seconds
6. **Ready**: Interactive command prompt appears

---

## User Commands

| Command | Description | Example |
|---------|-------------|---------|
| `help` | Show available commands | `help` |
| `status` | Show node role, coordinator, balance, clock | `status` |
| `balance` | Show current energy credits | `balance` |
| `sell N A` | Sell A credits to Node N | `sell 2 30` |
| `buy N A` | Buy A credits from Node N | `buy 1 20` |
| `nodes` | List known nodes and their status | `nodes` |
| `history` | Show transaction history | `history` |
| `election` | Force start a new election | `election` |
| `quit` | Gracefully shutdown (announces LEAVE) | `quit` |

---

## Testing Scenarios

### Scenario 1: Basic Trading
```
Node 1> sell 2 30
# Node 1: SELL 30 credits to Node 2 [100 -> 70]
# Node 2: BUY 30 credits from Node 1 [100 -> 130]
```

### Scenario 2: Leader Failure & Re-election
```
# Kill Node 3 (Ctrl+C)
# Wait 10-20 seconds for detection
Node 1: Node 3 SUSPECTED (missed heartbeat timeout)
Node 1: Node 3 FAILED (sustained heartbeat absence)
Node 1: LEADER FAILED - triggering election
Node 2: No higher-priority nodes, declaring self as coordinator
```

### Scenario 3: Late Joiner
```
# Start Nodes 1 and 2, perform some trades
# Then start Node 3
Node 3: Received JOIN_RESPONSE from coordinator Node 2
Node 3: Vector clock synchronized from coordinator
Node 3: State synchronized - coordinator=2, known_nodes=[1, 2, 3]
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DS_MULTICAST_GROUP` | `224.1.1.1` | Multicast group address |
| `DS_MULTICAST_PORT` | `5007` | Shared multicast port |
| `DS_UNICAST_PORT_BASE` | `6000` | Base for unicast ports (node N uses 6000+N) |
| `DS_HEARTBEAT_INTERVAL` | `2.0` | Seconds between heartbeats |
| `DS_HEARTBEAT_TIMEOUT` | `10.0` | Seconds before failure detection |
| `DS_ELECTION_TIMEOUT` | `5.0` | Seconds to wait for OK response |
| `DS_INITIAL_CREDITS` | `100` | Starting energy balance |

### Port Allocation

| Port | Purpose |
|------|---------|
| 5007 | Multicast (all nodes listen here) |
| 6000+N | Unicast for Node N (e.g., Node 1 → 6001, Node 2 → 6002) |

**Example for 3 nodes:**
- Node 1: Listens on multicast 5007 + unicast 6001
- Node 2: Listens on multicast 5007 + unicast 6002  
- Node 3: Listens on multicast 5007 + unicast 6003

---

## Technical Challenges & Solutions

### Challenge 1: Vector Clock Pollution from Heartbeats

**Problem:**
Heartbeats were incrementing the vector clock every 2 seconds. After a few minutes, clock values reached hundreds. If a node temporarily disconnected and missed heartbeats, it could never deliver subsequent trade messages because `can_deliver()` expected sequential clock values.

**Symptom:**
```
Node 2: Buffering message for causal delivery
Node 2: Buffering message for causal delivery
# Messages buffered forever, trades never complete
```

**Root Cause Analysis:**
The original implementation treated heartbeats as "causal events" when they are actually just liveness probes. The Chat application reference explicitly states that heartbeats should be "UDP multicast unreliable" without causal ordering.

**Solution:**
1. Modified `heartbeat.py` to send heartbeats with `vector_clock=None`
2. Modified `node.py` to skip vector clock update when receiving heartbeats
3. Heartbeats now follow a separate "liveness channel" from trades

**Why This Fix:**
- Decouples liveness detection from causal ordering
- Vector clocks only increment for business events (trades)
- Matches the theoretical model where heartbeats are not application-level events

---

### Challenge 2: Split-Brain During Elections

**Problem:**
With fast heartbeat timeouts (5s), two nodes would sometimes both declare themselves coordinator simultaneously.

**Symptom:**
```
Node 1: I am now the COORDINATOR
Node 2: I am now the COORDINATOR
# Two coordinators, inconsistent state
```

**Root Cause Analysis:**
- Network timing jitter caused both nodes to detect "leader failure" at nearly the same time
- Both started elections and found no higher-priority node responding
- Race condition in COORDINATOR announcement reception

**Solution:**
1. Increased `HEARTBEAT_TIMEOUT` from 5.0 to 10.0 seconds
2. This gives more time for legitimate heartbeats to arrive
3. Reduces false positives from temporary network issues

**Trade-off:**
- Slower failure detection (10-20s instead of 5s)
- More stable coordinator election
- Acceptable for demonstration purposes

---

### Challenge 3: Late Joiner Cannot Deliver Messages

**Problem:**
When Node 3 joins after Nodes 1 and 2 have exchanged trades, Node 3's vector clock starts at `{3: 0}`. All received messages are buffered because `can_deliver()` fails.

**Symptom:**
```
Node 3> status
Buffered Messages: 47
# Trades from Node 1 and 2 are stuck
```

**Root Cause Analysis:**
The original system had no state transfer mechanism. New nodes were "born" with empty clocks and couldn't understand the causal history of existing nodes.

**Solution:**
Implemented `JOIN_RESPONSE` protocol:
1. Coordinator sends its current vector clock state to new node
2. New node initializes its clock from this state
3. New node can now properly evaluate `can_deliver()` for incoming messages

**Why Not Full Hold-Back Queue Replication (like Chat app)?**
- Too complex for a university demo
- Clock state transfer is sufficient if nodes join before heavy trading
- Simpler implementation, fewer edge cases

---

### Challenge 4: Vector Clock Entry Removal Race Condition

**Problem:**
When a node left, its entry was removed from the vector clock. If messages were still in the hold-back queue referencing that node, `can_deliver()` would fail with missing keys.

**Symptom:**
```
KeyError: '2'
# Node 2 left, but messages in queue reference vc[2]
```

**Root Cause Analysis:**
The Chat application report explicitly warns: "Messages in the hold-back queue that depend on a removed entry remain valid." Our original implementation removed entries, breaking this invariant.

**Solution:**
Modified `remove_node()` in `vector_clock.py` to do nothing:
```python
def remove_node(self, node_id):
    # Intentionally empty - never remove entries
    pass
```

**Why This Fix:**
- Prevents race conditions with buffered messages
- Memory overhead is negligible (max 10 nodes × 4 bytes = 40 bytes)
- Follows distributed systems best practice for vector clocks

---

### Challenge 5: Lost Trade Confirmations

**Problem:**
UDP packets can be lost. If a `TRADE_CONFIRM` message was lost, the initiating node would deduct credits but the receiving node would never credit them.

**Symptom:**
```
Node 1: SELL 30 credits [100 -> 70]
# Node 2 never receives confirmation
# 30 credits "disappeared" from the system
```

**Root Cause Analysis:**
Original implementation used single `send()` for all messages. UDP provides no delivery guarantee.

**Solution:**
Implemented selective reliability:
- `send_reliable()` for multicast (3 retries)
- `send_with_retry()` for unicast trade confirmations (5 retries)

**Why Selective (not all messages)?**
- Heartbeats: Single send to avoid network congestion
- Elections: Timeout handles missed messages
- Trades: Must be reliable (financial integrity)

---

### Challenge 6: WinError 10054 Breaking Receive Loop

**Problem:**
On Windows, `WinError 10054` (connection reset) would cause the receive loop to break entirely. The node would become a "zombie" - able to send but not receive.

**Symptom:**
```
Node 1: Socket error in receive loop: [WinError 10054]
# Node stops receiving, appears dead to others
```

**Root Cause Analysis:**
Original code used `break` on socket errors, exiting the receive loop. For UDP (connectionless), this is overly aggressive.

**Solution:**
Changed `break` to `continue` with logging:
```python
except OSError as e:
    logger.warning(f"Socket error (continuing): {e}")
    time.sleep(0.1)
    continue  # Don't break, try to recover
```

**Why This Fix:**
- UDP has no "connection" to reset
- Error usually indicates one bad packet, not socket death
- Continuing allows recovery from transient issues

---

## Lessons Learned

### 1. Separate Liveness from Causality
Heartbeats and application messages serve fundamentally different purposes. Mixing them in the same vector clock system caused cascading failures.

### 2. Two-Phase Failure Detection Adds Stability
The SUSPECTED → FAILED pattern prevents overreaction to temporary network issues. Worth the slower detection time.

### 3. State Transfer is Critical for Dynamic Membership
Nodes cannot participate in causal ordering without knowing the system's history. Even a simplified state transfer (clock sync) is better than none.

### 4. Reliability Must Match Criticality
Not all messages need the same reliability. Heartbeats can be lossy; trade confirmations cannot.

---

## Demo Q&A Reference

Quick answers for common supervisor questions about distributed systems concepts:

### Leader Election (Bully Algorithm)

| Question | Answer |
|----------|--------|
| Why Bully over Ring? | Simpler, handles node failures during election, guarantees highest-priority wins |
| Why integer IDs not UUIDs? | Deterministic priority ordering, easier debugging, sufficient for ≤10 nodes |
| What prevents split-brain? | 10s timeout + OK response mechanism. Higher-ID node "bullies" lower by sending OK |
| ELECTION unicast vs COORDINATOR multicast? | ELECTION only goes to higher-ID nodes (efficient). COORDINATOR must reach ALL nodes |
| Election complexity? | O(n²) messages worst case (all nodes start election), O(n) best case |

### Vector Clocks & Causal Ordering

| Question | Answer |
|----------|--------|
| Why vector clocks not Lamport? | Vector clocks detect concurrent events; Lamport only gives total order |
| What does can_deliver() check? | 1) Message is next from sender (`vc_msg[sender] == vc_local[sender]+1`), 2) All dependencies met (`vc_msg[k] <= vc_local[k]` for others) |
| Why NOT remove clock entries on leave? | Buffered messages may reference departed node. Removing breaks causal delivery |
| Why heartbeats excluded from VC? | Heartbeats are liveness probes, not business events. Including them pollutes clock |
| What if message fails can_deliver()? | Buffered in hold-back queue, retried periodically until dependencies arrive |

### Failure Detection

| Question | Answer |
|----------|--------|
| Why 10s timeout not 5s? | Prevents split-brain from network jitter causing simultaneous elections |
| Why two-phase (SUSPECTED→FAILED)? | Reduces false positives from temporary network issues |
| Detection time? | 10-20 seconds (one timeout to suspect, another to confirm) |
| Trade-off? | Stability over speed. Acceptable for demo, may need tuning for production |

### State Transfer (JOIN_RESPONSE)

| Question | Answer |
|----------|--------|
| Why needed? | New node starts with empty clock `{N:0}`, can't deliver messages with `vc[other]>0` |
| What's transferred? | Vector clock state, coordinator ID, list of known nodes |
| Why not full hold-back queue? | Too complex for demo. Clock sync sufficient if nodes join before heavy trading |
| Alternative? | Full queue replication (like Chat app), but adds complexity |

### Reliability

| Question | Answer |
|----------|--------|
| Why UDP not TCP? | Simpler, no connection state, suitable for multicast, acceptable for demo |
| Why selective retries? | Different criticality: heartbeats (1x), JOIN (3x), TRADE_CONFIRM (5x) |
| What if TRADE_CONFIRM lost? | 5 retries with delays. If all fail, inconsistent state possible (demo limitation) |
| How to fix properly? | Two-phase commit or saga pattern (beyond project scope) |

### Edge Cases Handled

| Scenario | How We Handle It |
|----------|------------------|
| Leader crashes | Two-phase detection → election triggered → new leader elected |
| Two nodes elect simultaneously | Higher-ID sends OK to lower, takes over election |
| Late joiner | JOIN_RESPONSE provides clock state to sync |
| WinError 10054 (Windows) | `continue` instead of `break` in receive loop |
| Duplicate trade execution | `_completed_trades` set prevents re-execution |
| Message out of order | Hold-back queue buffers until dependencies arrive |
| Duplicate node ID | Second node fails on port bind (built-in safety) |

### Node ID Uniqueness & Multi-Device

| Question | Answer |
|----------|--------|
| How are node IDs unique? | Manual assignment via command line. Team coordinates beforehand |
| What if two nodes use same ID? | Second node fails to start (port already in use) - built-in safety |
| Why not auto-generate UUIDs? | Integer IDs give deterministic priority for Bully algorithm |
| Production alternative? | Use UUIDs + registration service |
| Multi-device supported? | Yes, use `--host` argument. Peer IPs are discovered automatically |
| How does dynamic discovery work? | When receiving multicast, we extract sender IP from UDP packet and register it |
| What about firewalls? | Must allow UDP on port 5007 (multicast) and 6001+ (unicast) |
| What if multicast fails? | Use `--peers` to manually specify node IP mappings |


---

## References

- **Bully Algorithm**: Garcia-Molina, H. (1982). Elections in a Distributed Computing System
- **Vector Clocks**: Fidge, C. (1988). Timestamps in Message-Passing Systems
- **UDP Multicast**: RFC 1112 - Host Extensions for IP Multicasting

