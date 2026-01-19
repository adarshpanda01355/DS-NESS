# Distributed Energy Trading System

A distributed systems project demonstrating UDP multicast, vector clocks, 
leader election (Bully algorithm), and heartbeat-based fault detection.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Node (node.py)                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ MulticastHandler │  │ UnicastHandler │  │    VectorClock     │  │
│  │  (group comm) │  │(direct msgs)  │  │ (causal ordering)   │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │HeartbeatMgr  │  │ElectionMgr   │  │    EnergyLedger      │  │
│  │(fault detect)│  │(Bully algo)  │  │  (credit tracking)   │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **UDP Multicast**: Group communication for heartbeats and announcements
- **UDP Unicast**: Direct messaging for elections and trades
- **Vector Clocks**: Causal ordering of messages
- **Bully Algorithm**: Leader election (highest node ID wins)
- **Heartbeat Detection**: Fault detection with suspicion-based failure
- **Energy Trading**: Simulated buy/sell of energy credits

## Project Structure

```
DSproject2/
├── node.py           # Main entry point - orchestrates all components
├── config.py         # Configuration (ports, timeouts, etc.)
├── message.py        # Message types and serialization
├── vector_clock.py   # Vector clock implementation
├── multicast.py      # UDP multicast handler
├── unicast.py        # UDP unicast handler
├── heartbeat.py      # Heartbeat-based fault detection
├── election.py       # Bully algorithm for leader election
├── ledger.py         # Energy credit ledger
└── README.md         # This file
```

## Single-Machine Testing

### Prerequisites

- Python 3.8 or higher
- No external dependencies required (uses standard library only)

### Quick Start: 3 Nodes in 3 Terminals

Open **three separate terminal windows** and run one command in each:

**Terminal 1 - Node 1:**
```powershell
cd c:\DSproject2
python node.py 1 --nodes 2,3
```

**Terminal 2 - Node 2:**
```powershell
cd c:\DSproject2
python node.py 2 --nodes 1,3
```

**Terminal 3 - Node 3:**
```powershell
cd c:\DSproject2
python node.py 3 --nodes 1,2
```

### What You'll See

1. **Node Joins**: Each node announces itself to the group
   ```
   Node 1: JOIN announced
   Node 2: Node 1 has joined
   ```

2. **Leader Election**: Bully algorithm elects Node 3 (highest ID)
   ```
   Node 1: Sending ELECTION to higher nodes: [2, 3]
   Node 3: No higher-priority nodes, declaring self as coordinator
   Node 1: Accepted Node 3 as coordinator
   ```

3. **Heartbeats**: Periodic liveness messages
   ```
   Node 1: HEARTBEAT SENT (credits=100)
   Node 2: Recorded heartbeat from Node 1
   ```

4. **Trading**: Interactive buy/sell commands
   ```
   Node 1> sell 2 20
   Node 1: Trade ACCEPTED by Node 2
   Node 1: SELL 20 credits to Node 2 [100 -> 80]
   ```

### User Commands

Once a node is running, you can enter these commands:

| Command | Description |
|---------|-------------|
| `status` | Show node status (role, coordinator, balance) |
| `balance` | Show current energy credits |
| `sell N A` | Sell A credits to node N |
| `buy N A` | Buy A credits from node N |
| `nodes` | List all known nodes and their status |
| `history` | Show transaction history |
| `election` | Force start a new election |
| `quit` | Gracefully shutdown the node |

### Testing Scenarios

#### 1. Basic Trading
```
# On Node 1:
Node 1> sell 2 30

# On Node 2 (you'll see):
Node 2: Trade request from Node 1: sell 30 credits
Node 2: Trade ACCEPTED
Node 2: BUY 30 credits from Node 1 [100 -> 130]
```

#### 2. Leader Failure & Re-election
```
# Kill Node 3 (the coordinator) with Ctrl+C
# Watch Nodes 1 and 2 detect the failure and elect Node 2 as new leader:

Node 1: Node 3 SUSPECTED (missed heartbeat timeout)
Node 1: LEADER 3 CONFIRMED FAILED
Node 1: Leader failure detected, starting election
Node 2: No higher-priority nodes, declaring self as coordinator
```

#### 3. Node Rejoin
```
# Restart Node 3:
python node.py 3 --nodes 1,2

# Node 3 will trigger election and become coordinator again (highest ID)
```

### Debug Mode

For verbose logging (shows all message sends/receives):

```powershell
python node.py 1 --nodes 2,3 --debug
```

### Environment Variables

You can customize configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DS_MULTICAST_GROUP` | `224.1.1.1` | Multicast group IP |
| `DS_MULTICAST_PORT` | `5007` | Multicast port |
| `DS_UNICAST_PORT_BASE` | `6000` | Base port for unicast |
| `DS_HEARTBEAT_INTERVAL` | `2.0` | Heartbeat frequency (seconds) |
| `DS_HEARTBEAT_TIMEOUT` | `6.0` | Failure detection timeout |
| `DS_ELECTION_TIMEOUT` | `5.0` | Election response timeout |
| `DS_INITIAL_CREDITS` | `100` | Starting energy credits |
| `DS_LOG_LEVEL` | `INFO` | Log verbosity |

Example:
```powershell
$env:DS_HEARTBEAT_INTERVAL = "1.0"
$env:DS_LOG_LEVEL = "DEBUG"
python node.py 1 --nodes 2,3
```

### Port Allocation

Each node uses a unique unicast port:
- Node 1: `6001` (UNICAST_PORT_BASE + 1)
- Node 2: `6002` (UNICAST_PORT_BASE + 2)
- Node 3: `6003` (UNICAST_PORT_BASE + 3)

All nodes share multicast port `5007`.

### Troubleshooting

**"Address already in use" error:**
- A previous node instance may still be running
- Wait a few seconds or kill the process
- Check: `netstat -ano | findstr :6001`

**Nodes not seeing each other:**
- Ensure Windows Firewall allows UDP on ports 5007, 6001-6010
- Check that multicast is enabled on your network adapter

**No coordinator elected:**
- Ensure all nodes know about each other via `--nodes`
- Check that nodes are receiving multicast messages

## Distributed Systems Concepts

### Vector Clocks
Each message carries a vector clock for causal ordering:
```python
# On send: increment local clock
clock = vector_clock.increment()
message.vector_clock = clock

# On receive: merge clocks
vector_clock.update(message.vector_clock)
```

### Bully Algorithm
Leader election process:
1. Node detects coordinator failure
2. Sends ELECTION to all higher-ID nodes
3. If no OK received → declares itself coordinator
4. If OK received → waits for COORDINATOR announcement

### Heartbeat Failure Detection
Two-phase detection to reduce false positives:
1. First timeout → node is **SUSPECTED**
2. Second timeout → node is **FAILED**

## Reference

Based on distributed systems concepts from:
- [chat_app_DS](https://github.com/ayushmittalde/chat_app_DS)

Adapted for energy trading simulation with equivalent complexity.
