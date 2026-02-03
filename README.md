# DS-NESS: Distributed Energy Trading System

A peer-to-peer distributed system simulating energy credit trading between nodes. Built using Python's standard library to demonstrate core distributed systems concepts including dynamic peer discovery, reliable ordered multicast, leader election, and fault tolerance.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture](#architecture)
3. [Node Lifecycle Flow](#node-lifecycle-flow)
4. [Trading Flow](#trading-flow)
5. [Distributed Systems Concepts](#distributed-systems-concepts)
6. [Configuration Parameters](#configuration-parameters)
7. [Running the System](#running-the-system)
8. [Commands](#commands)

---

## System Overview

DS-NESS is a simulation where multiple nodes form a decentralized network to trade energy credits. Each node:
- Starts with 100 energy credits
- Can buy/sell credits with other nodes
- Participates in leader election
- Detects and handles node failures

**Key Properties:**
- No central server — fully decentralized
- Dynamic membership — nodes can join/leave at any time
- Fault tolerant — system continues operating despite failures

---

## Architecture

```
Node
├── MulticastHandler   → Group broadcast (JOIN, HEARTBEAT, COORDINATOR)
├── UnicastHandler     → Direct messaging (TRADE, ELECTION, ACK)
├── VectorClock        → Causal ordering of messages
├── HeartbeatManager   → Failure detection via periodic heartbeats
├── ElectionManager    → Bully algorithm for leader election
├── EnergyLedger       → Credit balance and transaction history
└── MessageBuffer      → Hold-back queue for out-of-order messages
```

---

## Node Lifecycle Flow

### 1. Node Startup

```
[Node starts]
     │
     ▼
┌─────────────────────────────────┐
│ Initialize all components       │
│ (VectorClock, Handlers, Ledger) │
└─────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────┐
│ Start multicast & unicast       │
│ listeners on respective ports   │
└─────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────┐
│ Start heartbeat sender          │
│ and failure detector threads    │
└─────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────┐
│ Broadcast JOIN message          │
│ (multicast, sent 3× for         │
│  reliability)                   │
└─────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────┐
│ Wait HEARTBEAT_INTERVAL (2s)    │
│ for peer discovery              │
└─────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────┐
│ If no coordinator known →            │
│   Start Bully election               │
│ If coordinator exists →              │
│   Receive LEDGER_SYNC from leader    │
└──────────────────────────────────────┘
     │
     ▼
   [Node Ready - Enter command loop]
```

### 2. Dynamic Peer Discovery

When a node receives a multicast message:
1. Extract sender's IP from UDP packet source address
2. Register mapping: `node_id → IP address`
3. Add node to `known_nodes` set
4. Add node to heartbeat and election tracking

This eliminates the need for pre-configured peer lists — nodes discover each other automatically.

### 3. Leader Election (Bully Algorithm)

Triggered when:
- A node starts and no coordinator is known
- The coordinator's heartbeat times out (leader failure)
- The coordinator sends a LEAVE message (graceful departure)

```
[Election Triggered]
     │
     ▼
┌─────────────────────────────────────┐
│ Send ELECTION to all higher-ID      │
│ nodes via unicast                   │
└─────────────────────────────────────┘
     │
     ├── If OK received from any higher node:
     │        Wait for COORDINATOR announcement
     │
     └── If NO OK within ELECTION_TIMEOUT (5s):
              │
              ▼
         ┌────────────────────────────────┐
         │ Declare self as coordinator    │
         │ Broadcast COORDINATOR message  │
         │ (multicast, sent 3× reliably)  │
         └────────────────────────────────┘
```

### 4. Node Failure Detection

```
[Every HEARTBEAT_INTERVAL = 2s]
     │
     ▼
┌────────────────────────────────┐
│ Each node broadcasts HEARTBEAT │
│ with current credits balance   │
└────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────┐
│ Failure checker runs every second       │
│ If no heartbeat from node in 10s →      │
│   Mark node as FAILED                   │
│   If failed node was coordinator →      │
│     Trigger new election                │
└─────────────────────────────────────────┘
```

### 5. Graceful Shutdown

```
[User types 'exit']
     │
     ▼
┌─────────────────────────────────┐
│ Broadcast LEAVE message         │
│ (multicast, for peer cleanup)   │
└─────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────┐
│ Stop heartbeat threads          │
│ Stop message listeners          │
│ Close sockets                   │
└─────────────────────────────────┘
```

---

## Trading Flow

### Initiating a Trade

```
[User: sell 2 10]  (sell 10 credits to Node 2)
     │
     ▼
┌─────────────────────────────────────────┐
│ Validate: sufficient balance (≥10)?     │
│ Generate unique trade_id                │
└─────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────┐
│ Increment vector clock                  │
│ Send TRADE_REQUEST to Node 2 (unicast)  │
│ with ACK retry (5 attempts, 1.5s each)  │
└─────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────┐
│ Node 2 receives, validates, sends       │
│ TRADE_RESPONSE (accepted: true/false)   │
└─────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────┐
│ If accepted:                            │
│   - Seller deducts credits              │
│   - Buyer adds credits                  │
│   - Both log transaction                │
│   - TRADE_CONFIRM exchanged             │
└─────────────────────────────────────────┘
```

---

## Distributed Systems Concepts

### 1. Dynamic Peer Discovery

**Implementation:** When any multicast/unicast message arrives, the source IP is extracted from the UDP packet and mapped to the sender's node ID. This allows nodes to discover each other without static configuration.

**Files:** `multicast.py`, `unicast.py`, `node.py`

### 2. Reliable Ordered Multicast

**Implementation:**
- **Reliability:** Critical multicast messages (JOIN, COORDINATOR) are sent 3 times with 100ms delay between sends to handle packet loss
- **Ordering:** Vector clocks attached to every message enable causal ordering. Messages failing the delivery condition are buffered until dependencies arrive
- **Causal Delivery Condition:** A message from sender S with clock VC_msg can be delivered iff:
  - `VC_msg[S] == VC_local[S] + 1` (next expected from sender)
  - `VC_msg[K] <= VC_local[K]` for all K ≠ S (no missing dependencies)

**Files:** `vector_clock.py`, `multicast.py` (`send_reliable`)

### 3. Leader Election (Bully Algorithm)

**Implementation:** The node with the highest ID becomes coordinator.
- ELECTION messages sent to higher-ID nodes only (unicast)
- OK response suppresses the election — higher node takes over
- Timeout (5s) with no OK → declare self coordinator
- COORDINATOR announcement broadcast reliably to all nodes

**Files:** `election.py`

### 4. Fault Tolerance

**Implementation:**
- **Heartbeat-based failure detection:** Every node sends periodic HEARTBEAT via multicast. If no heartbeat received within timeout, node is marked failed
- **Leader failure handling:** When coordinator fails, any node detecting it triggers a new election
- **Message retry with ACK:** Critical unicast messages use send-with-ACK pattern (5 retries, 1.5s timeout each)
- **Anti-entropy gossip:** Periodic state exchange (every 8s) ensures eventual consistency even if some messages are lost
- **Duplicate detection:** Recent message IDs are cached to prevent duplicate processing

**Files:** `heartbeat.py`, `unicast.py`, `node.py`

### 5. Coordinator Responsibilities

The elected coordinator:
- Maintains global ledger registry of all nodes' balances
- Sends LEDGER_SYNC to new/rejoining nodes
- Broadcasts periodic state synchronization

---

## Configuration Parameters

All values in `config.py` — configurable via environment variables.

| Parameter | Value | Justification |
|-----------|-------|---------------|
| `HEARTBEAT_INTERVAL` | 2.0s | Frequent enough for timely failure detection, low enough to minimize network spam |
| `HEARTBEAT_TIMEOUT` | 10.0s | 5× heartbeat interval — tolerates temporary network delays and prevents false positives (split-brain) |
| `ELECTION_TIMEOUT` | 5.0s | Long enough for UDP round-trip + processing across network; short enough for quick leader recovery |
| `MESSAGE_RETRY_COUNT` | 5 | Balances reliability (multiple attempts) with avoiding indefinite blocking on unreachable nodes |
| `MESSAGE_RETRY_DELAY` | 1.5s | Gives network time to recover between retries without excessive waiting |
| `MULTICAST_RELIABLE_RETRIES` | 3 | Compensates for UDP unreliability on critical announcements; 3× provides high delivery probability |
| `GOSSIP_INTERVAL` | 8.0s | Infrequent enough to not flood the network, frequent enough for eventual consistency |
| `BUFFER_SIZE` | 4096 bytes | Sufficient for JSON-serialized messages; standard UDP safe payload size |
| `INITIAL_ENERGY_CREDITS` | 100 | Reasonable starting balance for simulation/demonstration |
| `MULTICAST_GROUP` | 224.1.1.1 | Locally-scoped multicast address — stays within local network segment |
| `MULTICAST_PORT` | 5007 | Arbitrary high port; avoids conflicts with well-known services |
| `UNICAST_PORT_BASE` | 6000 | Node N listens on port 6000+N; simple scheme avoids port collisions |

---

## Running the System

### Requirements
- Python 3.8+
- No external dependencies (standard library only)

### Start Nodes (Multi-Machine Setup)

Run one node per machine. No configuration needed — the system auto-detects the local IP address.

```bash
# Machine 1
python node.py 1

# Machine 2
python node.py 2

# Machine 3
python node.py 3
```

**What happens automatically:**
1. Each node detects its own IP via `get_local_ip()` (UDP socket trick)
2. Nodes discover each other through multicast JOIN messages
3. Peer IPs are learned from incoming UDP packet source addresses
4. Node 3 (highest ID) is elected as coordinator

> **Note:** All machines must be on the same local network (same subnet) for multicast to work.

---

## Commands

Once a node is running, use these commands:

| Command | Description |
|---------|-------------|
| `status` | Show node status, balance, coordinator, and active nodes |
| `balance` | Display current energy credits |
| `sell N A` | Sell A credits to Node N |
| `buy N A` | Buy A credits from Node N |
| `nodes` | List all known nodes with their status |
| `history` | Show transaction history |
| `exit` | Gracefully leave the network |
| `help` | Show command list |

---

## File Structure

| File | Purpose |
|------|---------|
| `node.py` | Main orchestrator — lifecycle, command loop, message routing |
| `multicast.py` | UDP multicast send/receive for group communication |
| `unicast.py` | UDP unicast with ACK-based reliability |
| `message.py` | Message types and JSON serialization |
| `vector_clock.py` | Causal ordering via vector clocks |
| `heartbeat.py` | Failure detection via periodic heartbeats |
| `election.py` | Bully algorithm implementation |
| `ledger.py` | Energy credit tracking and transactions |
| `config.py` | All configurable parameters |

---
