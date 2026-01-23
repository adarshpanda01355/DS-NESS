# Cross-Domain Architectural Audit
## Chat Application vs. Energy Trading System

**Date:** January 20, 2026  
**Purpose:** Compare the distributed mechanisms from the Decentralized Chat Room project to our Energy Trading implementation and identify gaps, risks, and incorrect implementations.

---

## Table of Contents

1. [Coordination: Leader Election](#1-coordination-leader-election-bully-algorithm)
2. [Failure Detection: Heartbeat Configuration](#2-failure-detection-heartbeat-configuration)
3. [Causal Ordering: Vector Clock Usage](#3-causal-ordering-vector-clock-usage)
4. [Edge Cases: Message Loss & Network Partitions](#4-edge-cases-message-loss--network-partitions)
5. [Reliability: Error Handling](#5-reliability-error-handling)
6. [Critical Evaluation: Gaps and Errors](#6-critical-evaluation-gaps-risks-and-incorrect-implementations)
7. [Summary Scorecard](#7-summary-scorecard)
8. [Recommended Priority Fixes](#8-recommended-priority-fixes)

---

## 1. COORDINATION: Leader Election (Bully Algorithm)

### Chat Project Approach:
- Uses **Bully Algorithm** with UUID-based priority (random UUIDs)
- **Dynamic discovery**: New nodes multicast `WANT_TO_JOIN` → Leader responds with `WANT_TO_JOIN_RESPONSE` containing group view, vector clock, and leader UUID
- **Sequence number reset**: New nodes reset their sequence number to 0 upon joining
- **Hold-back queue replication**: Leader multicasts its hold-back queue to new nodes
- Election/OK messages via **unicast**; COORDINATOR via **multicast**
- 10-second timeout before a lone node declares itself leader

### Our Energy Trading Approach:
- Uses **Bully Algorithm** with integer node_id as priority (deterministic)
- **"Lazy" join**: Nodes broadcast `JOIN` message; if we're coordinator, we re-announce COORDINATOR
- **No WANT_TO_JOIN_RESPONSE pattern**: We don't send group view, vector clock state, or ledger state to new nodes
- Election/OK via **unicast**; COORDINATOR via **multicast** ✅
- Initial election triggered after `HEARTBEAT_INTERVAL` (2s) if no coordinator known

### Comparison:

| Aspect | Chat Project | Energy Trading | Match? |
|--------|--------------|----------------|--------|
| Algorithm | Bully | Bully | ✅ |
| Priority scheme | Random UUID | Integer node_id | ⚠️ Different but valid |
| Join protocol | WANT_TO_JOIN → Response | Simple JOIN broadcast | ❌ Missing |
| State transfer on join | Vector clock + hold-back queue | **None** | ❌ **CRITICAL GAP** |
| Sequence number reset | Yes | **No** | ❌ **CRITICAL GAP** |
| Discovery timeout | 10 seconds | 2 seconds | ⚠️ Ours is faster |

### 🚨 CRITICAL GAP: No State Transfer on Join

The Chat report explicitly addresses **Consideration 1** (sequence number reset) and **Consideration 2** (hold-back queue replication) because without them:
- **New nodes will buffer messages forever** if they missed earlier messages
- The vector clock `can_deliver()` check will fail because the new node expects `vc[sender]+1` but receives `vc[sender]+N`

**Our system has this exact flaw.** If Node 3 joins after Nodes 1 and 2 have exchanged trades, Node 3's vector clock starts at `{3: 0}` and will **never** be able to deliver messages that have `vc[1] > 0` or `vc[2] > 0`.

### 💡 SUGGESTED FIX: Simplified State Transfer on Join

**Approach chosen: Lightweight coordinator-based sync (different from Chat)**

**Reasoning:** The Chat application's full hold-back queue replication is complex and involves multicasting potentially large queues. For a university project, we can use a simpler approach:

**Implementation:**
1. When a new node sends `JOIN`, the coordinator responds with a `JOIN_RESPONSE` message containing:
   - Current vector clock state
   - Current coordinator ID
   - List of active nodes
   
2. The new node initializes its vector clock from the coordinator's clock (not starting from zero)

3. Skip hold-back queue replication - for a university demo, we can assume nodes join before trading starts, or accept that late-joiners may miss some historical context

**Why this is better for our project:**
- Much simpler to implement (single message vs. queue replication)
- Sufficient for demonstration purposes
- Avoids complex synchronization edge cases
- Chat's approach is overkill for a demo with 3-5 nodes

**Code changes needed:**
- Add `MSG_JOIN_RESPONSE` message type in `message.py`
- Modify `_handle_join()` in `node.py` to send response if coordinator
- Add `_handle_join_response()` to initialize vector clock from received state

---

## 2. FAILURE DETECTION: Heartbeat Configuration

### Chat Project Configuration:
- `HEARTBEAT_INT = 2 seconds` (send interval)
- `HEARTBEAT_TIMEOUT = 5 seconds` (failure detection)
- Ratio: **2.5x** (timeout / interval)
- **No two-phase detection** mentioned in report (direct failure)

### Our Energy Trading Configuration:
- `HEARTBEAT_INTERVAL = 2 seconds` ✅
- `HEARTBEAT_TIMEOUT = 10 seconds` (changed from 6.0)
- Ratio: **5x**
- **Two-phase detection**: SUSPECTED → FAILED (requires 2 missed timeouts = 20s total!)

### Comparison:

| Metric | Chat | Energy Trading | Analysis |
|--------|------|----------------|----------|
| Send interval | 2s | 2s | ✅ Identical |
| Timeout | 5s | 10s | ⚠️ We're 2x slower |
| Detection time | 5s | **10-20s** (two-phase) | ❌ Much slower |
| False positive protection | Low | High | Trade-off |

### Analysis:

Our system is **more stable** (fewer false positives) but **slower to react** to failures. For a **trading system**, this is concerning:
- A crashed coordinator takes **10-20 seconds** to be replaced
- During this time, trades **cannot be processed** if they require coordinator involvement
- The Chat project's 5s timeout is more aggressive but suitable for a chat app

### ⚠️ RISK
With 10s timeout + two-phase detection, a coordinator crash could block trading for up to 20 seconds.

### 💡 SUGGESTED FIX: Reduce Timeout, Keep Two-Phase

**Approach chosen: Compromise between Chat's aggressive timeout and our stability**

**Recommended configuration:**
```python
HEARTBEAT_INTERVAL = 2.0   # Keep as-is
HEARTBEAT_TIMEOUT = 5.0    # Reduce from 10.0 to match Chat
```

**Reasoning:**
- Two-phase detection (SUSPECTED → FAILED) is actually a **good feature** - it reduces false positives from temporary network hiccups
- With 5s timeout + two-phase, actual detection time is ~10s (still reasonable)
- This balances stability with responsiveness
- For a university demo on localhost, network issues are rare anyway

**Why not go lower:**
- On WiFi networks (like in demos), packet loss can cause false suspicions
- 5s gives enough time for 2+ heartbeats to arrive
- Going lower than 5s on unreliable networks causes "flapping" (node appears to fail and recover repeatedly)

**Code change:** Single line in `config.py`

---

## 3. CAUSAL ORDERING: Vector Clock Usage

### Chat Project Approach:
- Vector clocks attached to **chat messages only**
- Increment on **send** (before multicasting message)
- Update/merge on **receive** (before delivering)
- **Does NOT remove entries** when nodes leave (to avoid hold-back queue issues)
- **Buffers messages during election** to avoid sync issues

### Our Energy Trading Approach:
- Vector clocks attached to **trade messages, heartbeats, elections, and all message types**
- Increment on **send** ✅
- Update/merge on **receive** ✅
- **DOES remove entries** when nodes leave
- **No election buffering** - messages flow during elections

### Comparison:

| Aspect | Chat | Energy Trading | Match? |
|--------|------|----------------|--------|
| Increment logic | Before send | Before send | ✅ |
| Merge logic | On receive | On receive (also increments!) | ⚠️ |
| Attached to heartbeats | ❌ No | ✅ Yes | ❌ **Incorrect** |
| Entry removal on leave | ❌ Never | ✅ Yes | ⚠️ Different |
| Election message buffering | ✅ Yes | ❌ No | ❌ **Gap** |

### 🚨 INCORRECT IMPLEMENTATION: Vector Clocks on Heartbeats

In `heartbeat.py` line 259-266:
```python
clock = self.vector_clock.increment()
heartbeat_msg = create_heartbeat(
    sender_id=self.node_id,
    vector_clock=clock,
    credits=self._energy_credits
)
```

**This is a fundamental design flaw.** The Chat report explicitly states heartbeats are sent via "UDP multicast unreliable" **without** causal ordering. Our system:
1. **Increments the vector clock every 2 seconds** for heartbeats
2. This **pollutes the causal ordering** - heartbeats don't represent causal events
3. A node that misses heartbeats will have a **stale vector clock** and buffer ALL subsequent trade messages forever

### 💡 SUGGESTED FIX: Remove Vector Clock from Heartbeats

**Approach chosen: Same as Chat - exclude heartbeats from causal ordering**

**Reasoning:**
- Heartbeats are **liveness probes**, not business events
- They don't need ordering - we only care about "is the node alive?"
- Including them pollutes the clock and breaks causal delivery

**Implementation:**
```python
# In heartbeat.py, change:
def _send_heartbeats(self):
    # DON'T increment vector clock
    heartbeat_msg = create_heartbeat(
        sender_id=self.node_id,
        vector_clock=None,  # No clock for heartbeats
        credits=self._energy_credits
    )
```

**Also modify `_on_multicast_message` in `node.py`:**
```python
# Skip vector clock update for heartbeats
if message.message_type == MSG_HEARTBEAT:
    self._handle_heartbeat(message)
    return  # Don't update vector clock
```

**Why this is correct:**
- Chat application explicitly separates "unreliable" messages (heartbeats) from "causally ordered" messages (chat)
- Vector clocks should only track **application-level events** (trades in our case)
- This is the textbook-correct approach

---

### 🚨 RISK: Vector Clock Entry Removal

The Chat report explicitly warns against entry removal in Section 2.5:
> "To simplify implementation, the application only adds entries to the vector clock when new nodes join but does not remove entries when nodes leave. This approach reduces complexity and ensures that: Messages in the hold-back queue that depend on a removed entry remain valid."

Our `vector_clock.py` line 156-167 removes entries, which can cause:
- Messages in the hold-back queue to become **undeliverable**
- Race conditions if removal happens while `can_deliver()` is checking

### 💡 SUGGESTED FIX: Stop Removing Vector Clock Entries

**Approach chosen: Same as Chat - never remove entries**

**Reasoning:**
- With max 10 nodes (per our config), memory usage is negligible
- Removal creates race conditions with the hold-back queue
- The complexity of "safe removal" isn't worth it for a university project

**Implementation:**
Simply comment out or remove the body of `remove_node()` in `vector_clock.py`:
```python
def remove_node(self, node_id):
    """
    Remove a departed node from the vector clock.
    
    NOTE: We intentionally do NOT remove entries to avoid
    invalidating messages in the hold-back queue. The Chat
    application report recommends this approach.
    """
    # Intentionally empty - keep all entries
    pass
```

**Why not implement "safe removal":**
- Would require locking the hold-back queue during removal
- Would need to check if any buffered message depends on the node
- Too complex for demo purposes
- Chat project (which was thoroughly tested) chose not to do it

---

## 4. EDGE CASES: Message Loss & Network Partitions

### Chat Project Edge Case Handling:

| Edge Case | Chat Solution | Our Solution | Gap? |
|-----------|---------------|--------------|------|
| **Message Loss** | Resend up to 15 times with 1.5s delay | 3 retries with 1s delay | ⚠️ Less robust |
| **Multicast reliability** | Send minimum 8 times | Send **once** | ❌ **CRITICAL** |
| **Network Partition** | Leader detects unknown heartbeat → TRY_JOIN_AGAIN | **Not handled** | ❌ **CRITICAL** |
| **Two leaders** | UUID comparison, lower steps down | **Not handled** | ❌ **CRITICAL** |
| **Join during election** | Buffer messages until election complete | **Not handled** | ❌ **GAP** |
| **Duplicate messages** | Track (ID + sender UUID), only deliver once | Track trade_id in `_completed_trades` | ✅ Partial |

### 🚨 CRITICAL GAP: No Multicast Reliability

The Chat report states (Section 4.6):
> "Multicast messages are sent at least 8 times, since we don't always know everyone who is supposed to receive it."

Our `multicast.py` sends **exactly once**. This means:
- If a UDP packet is lost (common on wireless), the message is **gone forever**
- Trade confirmations, coordinator announcements, and join notifications can all be lost
- For an **energy trading system**, a lost `TRADE_CONFIRM` means **money/credits lost**

### 💡 SUGGESTED FIX: Add Simple Multicast Retry

**Approach chosen: Simpler than Chat - 3 retries for critical messages only**

**Reasoning:**
- Chat's 8x for ALL multicast is overkill for localhost testing
- We can be selective: only retry for critical messages
- Reduces network spam while still providing reliability

**Implementation - add to `multicast.py`:**
```python
def send_reliable(self, message, retries=3, delay=0.1):
    """
    Send a message multiple times for reliability.
    Used for critical messages like COORDINATOR and TRADE_CONFIRM.
    """
    for i in range(retries):
        self.send(message)
        if i < retries - 1:
            time.sleep(delay)
    return True
```

**Then in `node.py`, use `send_reliable()` for:**
- `MSG_COORDINATOR` announcements
- `MSG_TRADE_CONFIRM` messages
- `MSG_JOIN` announcements

**Why 3 instead of 8:**
- On localhost, packet loss is near-zero
- Even on WiFi, 3 attempts with small delays handles most issues
- 8 is designed for truly unreliable networks (public WiFi, high latency)
- Less network traffic = cleaner demo logs

---

### 🚨 CRITICAL GAP: No Network Partition Handling

The Chat report describes (Section 4.4) a sophisticated partition handling mechanism:
1. Leader receives heartbeat from unknown node
2. Sends `TRY_JOIN_AGAIN` with its UUID
3. UUID comparison determines which leader steps down

Our system has **no equivalent**. If network partitions and heals:
- **Two nodes could both believe they are coordinator**
- Trades could be processed by both, leading to **double-updates**
- The system will **never converge** to a single leader

### 💡 SUGGESTED FIX: Minimal Partition Detection (Simplified)

**Approach chosen: Detection only, manual resolution (simpler than Chat)**

**Reasoning:**
- Full Chat-style automatic resolution is complex (UUID comparison, step-down logic)
- For a university demo, we just need to **detect** the problem and log it
- Demonstrating awareness of the issue is often sufficient for academic projects

**Implementation - add to `_handle_heartbeat` in `node.py`:**
```python
def _handle_heartbeat(self, message):
    sender_id = message.sender_id
    
    # Check for unknown node (potential partition recovery)
    with self._nodes_lock:
        if sender_id not in self._known_nodes:
            if self.election.is_coordinator():
                logger.warning(
                    f"Node {self.node_id}: Detected heartbeat from unknown node {sender_id}. "
                    f"Possible network partition recovery. Manual intervention may be needed."
                )
                # For demo: just add them to known nodes and let normal election handle it
                self._known_nodes.add(sender_id)
                self.heartbeat.add_node(sender_id)
                self.election.add_node(sender_id)
    
    self.heartbeat.record_heartbeat(sender_id)
    # ... rest of handler
```

**Why not full automatic resolution:**
- Requires bidirectional UUID comparison protocol
- Need to handle the "step down" case safely (ongoing trades?)
- For a 3-node demo, partitions are unlikely
- Showing you understand the problem is more important than solving it perfectly

**For extra credit (if time permits):**
Add the full `TRY_JOIN_AGAIN` protocol as described in the Chat report.

---

## 5. RELIABILITY: Error Handling

### Chat Project Error Handling:
- Socket timeouts for non-blocking operations
- ACK-based reliable unicast (resend until ACK received)
- 15 retries for critical messages
- Separate send/receive sockets to avoid platform issues

### Our Energy Trading Error Handling:

In `multicast.py` line 258-263:
```python
except OSError as e:
    if self._running:  # Only log if not intentionally stopped
        logger.error(f"Node {self.node_id}: Socket error in receive loop: {e}")
    break
```

And similar in `unicast.py`.

### Comparison:

| Aspect | Chat | Energy Trading | Gap? |
|--------|------|----------------|------|
| Socket timeout | ✅ Yes | ✅ Yes (1.0s) | ✅ |
| ACK mechanism | Full implementation | Partial (`send_and_wait_ack` exists but unused) | ⚠️ |
| Retry count | 15 unicast, 8 multicast | 3 unicast, 0 multicast | ❌ |
| WinError 10054 handling | Not mentioned | `OSError` catch + break | ⚠️ |
| Socket separation | ✅ Separate send/recv | ✅ Multicast has separate, unicast doesn't | ⚠️ |

### ⚠️ WinError 10054 Handling

Our `OSError` handling catches the error but **breaks out of the receive loop entirely**. This means:
- If a WinError 10054 occurs (connection reset by peer), the node **stops receiving all messages**
- The node becomes a zombie - it can send but cannot receive
- For unicast, this is especially problematic during elections

### 💡 SUGGESTED FIX: Recover from Socket Errors

**Approach chosen: Log and continue instead of breaking**

**Reasoning:**
- WinError 10054 on UDP is usually transient
- Breaking kills the node silently - very bad for debugging
- Better to log, skip the bad packet, and continue

**Implementation - modify `_receive_loop` in both `multicast.py` and `unicast.py`:**
```python
def _receive_loop(self):
    while self._running:
        try:
            data, addr = self._socket.recvfrom(BUFFER_SIZE)
            if data and self._on_receive:
                try:
                    self._on_receive(data, addr)
                except Exception as e:
                    logger.error(f"Node {self.node_id}: Callback error: {e}")
                    
        except socket.timeout:
            continue
        except OSError as e:
            # Log but DON'T break - try to recover
            if self._running:
                logger.warning(f"Node {self.node_id}: Socket error (continuing): {e}")
            # Small delay before retry to avoid spinning
            time.sleep(0.1)
            continue  # <-- Changed from 'break' to 'continue'
```

**Why continue instead of break:**
- UDP is connectionless - there's no "connection" to be "reset"
- The error usually means one bad packet, not socket death
- If the socket truly dies, subsequent recvfrom calls will also fail
- Continuing gives the system a chance to recover

---

## 6. CRITICAL EVALUATION: Gaps, Risks, and Incorrect Implementations

### ❌ Missing Requirements (MUST FIX)

#### 1. No State Transfer on Join
- **Problem**: New nodes don't receive vector clock state or pending messages
- **Impact**: New nodes will **never be able to deliver trade messages** if they join mid-session
- **Chat Solution**: `WANT_TO_JOIN_RESPONSE` with group view + vector clock + hold-back queue
- **Our Fix**: Simplified `JOIN_RESPONSE` with vector clock only (see Section 1)

#### 2. No Multicast Reliability
- **Problem**: Multicast messages sent exactly once
- **Impact**: Lost UDP packets = lost trade confirmations = **financial loss**
- **Chat Solution**: Send minimum 8 times for multicast
- **Our Fix**: `send_reliable()` with 3 retries for critical messages (see Section 4)

#### 3. No Network Partition Handling
- **Problem**: Two groups can elect separate leaders after partition heals
- **Impact**: **Dual-leader split-brain**, inconsistent ledger state, double-spending
- **Chat Solution**: Leader detects unknown heartbeats → UUID comparison → lower steps down
- **Our Fix**: Detection + logging only, let normal election resolve (see Section 4)

---

### ⚠️ Logical Risks (HIGH PRIORITY)

#### 1. Coordinator Crash During Balance Update

**Scenario**: 
1. Node 1 initiates trade with Node 2
2. Node 1 executes `SELL` (balance decreases)
3. Node 1 sends `TRADE_CONFIRM` to Node 2
4. **Network loses the TRADE_CONFIRM packet**
5. Node 2 never executes `BUY`

**Result**: Node 1 loses credits, Node 2 never gains them. **Credits disappear.**

**Our system has limited protection** - the `_completed_trades` set prevents double-execution but doesn't handle lost confirmations.

### 💡 SUGGESTED FIX: Use Reliable Unicast for Trade Messages

**Implementation:**
Change trade confirm to use `send_with_retry()`:
```python
# In _handle_trade_response, change:
self.unicast.send(confirm, sender_id)
# To:
self.unicast.send_with_retry(confirm, sender_id, retries=5)
```

---

#### 2. Two Nodes Trade at Exact Same Millisecond

**Scenario**:
1. Node 1 and Node 2 both initiate trades simultaneously
2. Both increment vector clocks to same value
3. Both send trade requests

**Result**: Vector clocks are **concurrent** (neither happened-before the other). Our `can_deliver()` will return `False` for both, buffering them forever.

### 💡 SUGGESTED FIX: This is Actually Fine

**Reasoning:**
- Concurrent events are **expected** in distributed systems
- The vector clock correctly identifies them as concurrent
- The issue is that our `can_deliver()` is too strict

**The real fix** is understanding that trade requests don't need strict causal ordering:
- Only the REQUEST needs to reach both parties
- The RESPONSE and CONFIRM create the actual ordering
- Concurrent requests should both be processed

**For demo purposes:** Just avoid initiating trades simultaneously. This is a theoretical edge case that rarely occurs in practice.

---

#### 3. Heartbeat Vector Clock Pollution

**Scenario**:
1. Node 1 sends heartbeat (vc[1] = 100)
2. Node 2 is temporarily disconnected, misses heartbeats
3. Node 1 sends trade request (vc[1] = 150)
4. Node 2 receives trade, but local vc[1] = 5
5. `can_deliver()` returns `False` (expects vc[1] = 6)

**Result**: Node 2 **buffers the trade forever**. The trade never executes.

### 💡 SUGGESTED FIX: Already Covered

See Section 3 - remove vector clock from heartbeats. This completely eliminates this risk.

---

### ❌ Incorrect Implementations (DESIGN FLAWS)

#### 1. Vector Clocks on Heartbeats
**Location**: `heartbeat.py` line 259
**Status**: 🔴 MUST FIX
**Fix**: Remove `vector_clock.increment()` from heartbeat sending (see Section 3)

#### 2. Vector Clock Entry Removal on Node Leave
**Location**: `vector_clock.py` line 156-167
**Status**: 🟠 SHOULD FIX
**Fix**: Comment out removal logic (see Section 3)

#### 3. Socket Error Breaks Receive Loop
**Location**: `multicast.py` line 258, `unicast.py` line 367
**Status**: 🟡 NICE TO FIX
**Fix**: Change `break` to `continue` (see Section 5)

---

## 7. Summary Scorecard

| Category | Chat Features | Our Implementation | Score |
|----------|---------------|-------------------|-------|
| Bully Election | ✅ | ✅ | 9/10 |
| Dynamic Join Protocol | ✅ Full handshake | ❌ Simple broadcast | 3/10 |
| Vector Clocks | ✅ Correct scope | ❌ Polluted by heartbeats | 4/10 |
| Causal Delivery | ✅ With election buffering | ⚠️ No buffering during election | 6/10 |
| Message Reliability | ✅ 15 retries + 8x multicast | ❌ 3 retries + 1x multicast | 2/10 |
| Network Partition | ✅ UUID-based resolution | ❌ Not implemented | 0/10 |
| Failure Detection | ✅ 5s timeout | ⚠️ 10-20s (too slow for trading) | 5/10 |
| Error Handling | ✅ Robust | ⚠️ Basic | 5/10 |

**Overall Assessment:** Our system implements the core algorithms correctly but lacks critical reliability and edge-case handling that the Chat project includes. For a financial/trading system, these gaps are concerning but fixable.

---

## 8. Recommended Priority Fixes

### 🔴 CRITICAL (Must fix before submission)

| # | Issue | Fix | Effort |
|---|-------|-----|--------|
| 1 | Vector clock increment on heartbeats | Remove `increment()` from heartbeat send | 5 min |
| 2 | No multicast reliability | Add `send_reliable()` with 3 retries | 15 min |
| 3 | Heartbeat timeout too slow | Change `HEARTBEAT_TIMEOUT` from 10.0 to 5.0 | 1 min |

### 🟠 HIGH (Should fix if time permits)

| # | Issue | Fix | Effort |
|---|-------|-----|--------|
| 4 | No state transfer on join | Add `JOIN_RESPONSE` with vector clock | 30 min |
| 5 | Vector clock entry removal | Comment out `remove_node()` body | 2 min |
| 6 | Trade confirm not reliable | Use `send_with_retry()` for confirms | 5 min |

### 🟡 MEDIUM (Nice to have)

| # | Issue | Fix | Effort |
|---|-------|-----|--------|
| 7 | Socket error breaks loop | Change `break` to `continue` | 5 min |
| 8 | No partition detection | Add warning log for unknown heartbeats | 15 min |
| 9 | No election message buffering | Buffer trades during `election_in_progress` | 45 min |

---

## Appendix: Quick Fix Checklist

For a minimal viable fix before submission, implement these changes:

### config.py
```python
HEARTBEAT_TIMEOUT = 5.0  # Changed from 10.0
```

### heartbeat.py (around line 259)
```python
# REMOVE this line:
# clock = self.vector_clock.increment()

# CHANGE to:
heartbeat_msg = create_heartbeat(
    sender_id=self.node_id,
    vector_clock=None,  # Don't include vector clock
    credits=self._energy_credits
)
```

### node.py (in _on_multicast_message)
```python
# ADD at the start of the method, after parsing:
if message.message_type == MSG_HEARTBEAT:
    self._handle_heartbeat(message)
    return  # Skip vector clock update for heartbeats
```

### vector_clock.py (remove_node method)
```python
def remove_node(self, node_id):
    # Intentionally empty - never remove entries
    # This prevents hold-back queue issues
    pass
```

### multicast.py (add new method)
```python
def send_reliable(self, message, retries=3, delay=0.1):
    """Send message multiple times for reliability."""
    for i in range(retries):
        self.send(message)
        if i < retries - 1:
            time.sleep(delay)
```

These minimal changes address the most critical issues in under 30 minutes of work.

---

*End of Architectural Audit Document*
