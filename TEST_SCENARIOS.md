# Test Scenarios

This document outlines the test scenarios verified for the DS-NESS distributed energy trading system.

**Test Environment:** 3 nodes running on separate machines connected via a local router (192.168.0.100).
---

## 1. Dynamic Peer Discovery

| Test | Result |
|------|--------|
| Nodes discover each other automatically via multicast | ✓ |
| No pre-configured peer IP addresses required | ✓ |
| Peer IPs learned from UDP packet source addresses | ✓ |
| New node joining mid-session is discovered by all existing nodes | ✓ |

---

## 2. Core Functionality

| Feature | Verified |
|---------|----------|
| Node startup and initialization | ✓ |
| Multicast JOIN broadcast on startup | ✓ |
| Heartbeat sending every 2 seconds | ✓ |
| Leader election on startup (first node becomes leader) | ✓ |
| Coordinator announcement via multicast | ✓ |
| Sell trade between any two nodes | ✓ |
| Buy trade between any two nodes | ✓ |
| Correct balance update after trade (both parties) | ✓ |
| Transaction history correctly reflects all trades | ✓ |
| `status`, `nodes`, `balance`, `history` commands | ✓ |
| Graceful shutdown with LEAVE message | ✓ |

**Leader Election Stability:**

When nodes join sequentially (Node 1, then Node 2, then Node 3), the first node to start becomes the leader and **remains leader**. A higher-ID node joining later does NOT trigger a new election. This ensures system stability.

Elections only occur when:
- No coordinator exists at startup
- Current leader fails or leaves

---

## 3. Simultaneous Trading

| Test | Result |
|------|--------|
| Node 1 sells to Node 2 while Node 2 sells to Node 1 | ✓ |
| Both trades complete without deadlock | ✓ |
| Final balances reflect both transactions | ✓ |
| Vector clocks correctly order concurrent trades | ✓ |

---

## 4. Trade During Node Departure

| Scenario | Behavior |
|----------|----------|
| Node A initiates trade with Node B | Trade request sent |
| Node B quits before responding | Trade times out (no ACK) |
| Node A sees no trade in history (trade not completed) | ✓ |
| Node B rejoins network | Receives state sync from coordinator |
| Trade history reflects only completed trades | ✓ |

---

## 5. State Persistence on Rejoin

| Scenario | Behavior |
|----------|----------|
| **Follower node** leaves and rejoins | Balance and history restored from coordinator's registry ✓ |
| **Leader node** leaves and rejoins | Balance reset to default (100 credits), history cleared |

**Why leader state is not preserved:**

When the leader leaves, a new leader is elected. The new leader has no record of the old leader's state (the old leader was the authority). When the old leader rejoins as a follower, it receives default initial state.

Fixing this would require:
- Persistent storage (database/file) on each node
- Distributed consensus for state recovery
- Complex conflict resolution for divergent histories

This is out of scope for this demonstration project.

---

## 6. Fault Tolerance

| Test | Result |
|------|--------|
| Leader crash triggers new election within ~10-12 seconds | ✓ |
| Follower crash detected and removed from tracking | ✓ |
| Network partition (node temporarily unreachable) - node rejoins cleanly | ✓ |
| Duplicate message detection prevents double-execution | ✓ |
| ACK-based retry for critical unicast messages | ✓ |

**Cascading Leader Failure Test:**

| Step | State |
|------|-------|
| Initial: Node 1 (leader), Node 2, Node 3 | System stable |
| Node 1 (leader) is killed | Election triggered |
| Node 3 becomes leader (highest remaining ID) | ✓ |
| Node 3 (new leader) is killed | Election triggered again |
| Node 2 becomes leader (only remaining node) | ✓ |

System correctly handles consecutive leader failures and elects the next highest available node.

---

## 7. Causal Ordering

| Test | Result |
|------|--------|
| Messages buffered until causal dependencies satisfied | ✓ |
| Vector clocks correctly merged on receive | ✓ |
| Trade confirmations delivered in correct order | ✓ |
| Heartbeats excluded from causal ordering (no clock pollution) | ✓ |

---

## 8. Edge Cases Handled

| Scenario | Handling |
|----------|----------|
| Trade with self | Rejected |
| Trade with unknown node | Rejected (node not in known peers) |
| Insufficient balance for sell | Rejected with error message |
| Negative trade amount | Rejected |
| Node ID collision | Not tested (assumed unique IDs) |

---
