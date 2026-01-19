# Manual Test Scenarios

These scenarios demonstrate the distributed systems concepts in action.
Run them manually and observe the expected behavior.

---

## Scenario 1: Leader Crash and Re-Election

**Purpose:** Demonstrate heartbeat-based failure detection and Bully algorithm re-election.

**Setup:** Start 3 nodes in 3 terminals.

### Steps:

```
STEP 1: Start all nodes (in order, with ~2 second gaps)
────────────────────────────────────────────────────────
Terminal 1:  python node.py 1 --nodes 2,3
Terminal 2:  python node.py 2 --nodes 1,3
Terminal 3:  python node.py 3 --nodes 1,2

STEP 2: Wait for initial election to complete (~5 seconds)
────────────────────────────────────────────────────────
All nodes should show: "Accepted Node 3 as coordinator"

STEP 3: Verify Node 3 is coordinator
────────────────────────────────────────────────────────
On any node, type: status

Expected output on Node 1 or 2:
  Role: FOLLOWER
  Coordinator: Node 3

Expected output on Node 3:
  Role: COORDINATOR
  Coordinator: Node 3

STEP 4: Kill the leader (Node 3)
────────────────────────────────────────────────────────
In Terminal 3, press: Ctrl+C
(Do NOT use 'quit' - we want to simulate a crash, not graceful leave)

STEP 5: Observe failure detection on Node 1 and Node 2 (~6-12 seconds)
────────────────────────────────────────────────────────
Wait for heartbeat timeout (HEARTBEAT_TIMEOUT = 6 seconds)
```

### Expected Behavior:

```
Timeline after killing Node 3:
────────────────────────────────────────────────────────

+0s   Node 3 killed (Ctrl+C)

+6s   Node 1 logs: "Node 3 SUSPECTED (missed heartbeat timeout)"
      Node 2 logs: "Node 3 SUSPECTED (missed heartbeat timeout)"
      
      (Nodes enter suspicion phase - not yet confirmed failed)

+12s  Node 1 logs: "Node 3 FAILED (sustained heartbeat absence)"
      Node 2 logs: "Node 3 FAILED (sustained heartbeat absence)"
      
      Node 1 logs: "LEADER FAILED - triggering election"
      Node 2 logs: "LEADER FAILED - triggering election"

+12s  Election starts:
      Node 1 logs: "Sending ELECTION to higher nodes: [2]"
      Node 2 logs: "Sending OK to Node 1 (we have higher priority)"
      Node 2 logs: "No higher-priority nodes, declaring self as coordinator"
      
+13s  Node 2 logs: "COORDINATOR message broadcast - I am the new leader!"
      Node 1 logs: "Accepted Node 2 as coordinator"

FINAL STATE:
────────────────────────────────────────────────────────
Node 1: FOLLOWER, Coordinator = Node 2
Node 2: COORDINATOR
Node 3: (dead)
```

### Verification:

```
On Node 1, type: status

Expected:
  Role: FOLLOWER
  Coordinator: Node 2
  Active Nodes: [1, 2]

On Node 2, type: status

Expected:
  Role: COORDINATOR
  Coordinator: Node 2
  Active Nodes: [1, 2]
```

### Key Observations:

1. **Two-phase failure detection**: SUSPECTED → FAILED (reduces false positives)
2. **Bully algorithm**: Node 1 sends ELECTION to Node 2, Node 2 responds with OK
3. **Highest ID wins**: Node 2 becomes coordinator (highest alive node)
4. **Coordinator broadcast**: New leader announces via multicast

---

## Scenario 2: Node Join After Election

**Purpose:** Demonstrate dynamic node discovery and re-election when a higher-priority node joins.

**Setup:** Start with 2 nodes, then add a third.

### Steps:

```
STEP 1: Start only Node 1 and Node 2
────────────────────────────────────────────────────────
Terminal 1:  python node.py 1 --nodes 2,3
Terminal 2:  python node.py 2 --nodes 1,3

(Note: They list node 3 but it's not running yet)

STEP 2: Wait for election (~5 seconds)
────────────────────────────────────────────────────────
Node 2 should become coordinator (highest ID among alive nodes)

STEP 3: Verify Node 2 is coordinator
────────────────────────────────────────────────────────
On Node 1, type: status

Expected:
  Role: FOLLOWER
  Coordinator: Node 2

On Node 2, type: status

Expected:
  Role: COORDINATOR
  Coordinator: Node 2

STEP 4: Start Node 3 (higher priority node joins)
────────────────────────────────────────────────────────
Terminal 3:  python node.py 3 --nodes 1,2

STEP 5: Observe re-election (~3 seconds)
────────────────────────────────────────────────────────
```

### Expected Behavior:

```
When Node 3 starts:
────────────────────────────────────────────────────────

+0s   Node 3 broadcasts: JOIN
      Node 1 logs: "Node 3 has joined"
      Node 2 logs: "Node 3 has joined"
      
      Node 2 (as coordinator) responds with: COORDINATOR announcement
      Node 3 logs: "Received COORDINATOR from Node 2"

+2s   Node 3: "No coordinator known, starting election"
      (Node 3 triggers election because it has higher priority)
      
      Node 3 logs: "No higher-priority nodes, declaring self as coordinator"
      Node 3 logs: "COORDINATOR message broadcast - I am the new leader!"

+2s   Node 1 logs: "Accepted Node 3 as coordinator"
      Node 2 logs: "Accepted Node 3 as coordinator"

FINAL STATE:
────────────────────────────────────────────────────────
Node 1: FOLLOWER, Coordinator = Node 3
Node 2: FOLLOWER, Coordinator = Node 3
Node 3: COORDINATOR
```

### Verification:

```
On all nodes, type: nodes

Expected on each:
  Node 1 (active)
  Node 2 (active)
  Node 3 (active, coordinator)
```

### Key Observations:

1. **JOIN broadcast**: New node announces presence to group
2. **Coordinator responds**: Existing leader sends COORDINATOR to inform newcomer
3. **Higher node takes over**: Node 3 starts election, wins because it's highest
4. **Seamless transition**: All nodes update their coordinator reference

---

## Scenario 3: Causal Message Ordering (Vector Clocks)

**Purpose:** Demonstrate that vector clocks ensure causal ordering of trade messages.

**Setup:** 3 nodes with debug logging enabled.

### Steps:

```
STEP 1: Start all nodes with debug logging
────────────────────────────────────────────────────────
Terminal 1:  python node.py 1 --nodes 2,3 --debug
Terminal 2:  python node.py 2 --nodes 1,3 --debug
Terminal 3:  python node.py 3 --nodes 1,2 --debug

STEP 2: Wait for election to complete
────────────────────────────────────────────────────────
Node 3 becomes coordinator

STEP 3: Check initial vector clocks
────────────────────────────────────────────────────────
On each node, type: status

Expected (approximately):
  Node 1: Vector Clock: [1:3, 2:2, 3:4]
  Node 2: Vector Clock: [1:2, 2:4, 3:3]
  Node 3: Vector Clock: [1:2, 2:2, 3:5]
  
(Actual values depend on message timing)

STEP 4: Execute a trade from Node 1 to Node 2
────────────────────────────────────────────────────────
On Node 1, type: sell 2 25

STEP 5: Observe vector clock updates in debug logs
────────────────────────────────────────────────────────
```

### Expected Behavior:

```
Trade Message Flow:
────────────────────────────────────────────────────────

Node 1 (seller):
  - Increments own clock: [1:X+1, ...]
  - Sends TRADE_REQUEST to Node 2
  - Log: "Proposed trade to Node 2: sell 25 credits"

Node 2 (buyer):
  - Receives TRADE_REQUEST with Node 1's vector clock
  - Merges clocks: max(local[i], received[i]) for all i
  - Increments own clock after merge
  - Sends TRADE_RESPONSE (accepted)
  - Log: "Trade request from Node 1: sell 25 credits"
  - Log: "Trade ACCEPTED"

Node 1:
  - Receives TRADE_RESPONSE
  - Merges and increments clock
  - Executes trade: balance 100 -> 75
  - Sends TRADE_CONFIRM
  - Log: "SELL 25 credits to Node 2 [100 -> 75]"

Node 2:
  - Receives TRADE_CONFIRM
  - Merges and increments clock
  - Executes trade: balance 100 -> 125
  - Log: "BUY 25 credits from Node 1 [100 -> 125]"
```

### Verification:

```
After trade, on Node 1 type: status
Expected:
  Balance: 75 credits
  Vector Clock: [1:X, 2:Y, 3:Z]  (X increased significantly)

On Node 2 type: status
Expected:
  Balance: 125 credits
  Vector Clock: [1:X', 2:Y', 3:Z']  (reflects merged history)
```

### Key Observations:

1. **Clock increment on send**: Each outgoing message increments sender's clock
2. **Clock merge on receive**: Receiver takes max of local and received clocks
3. **Causal ordering**: If message A happened-before message B, VC(A) < VC(B)
4. **Trade consistency**: Both parties see consistent final balances

---

## Scenario 4: Rapid Trades with Causal Buffering

**Purpose:** Demonstrate that out-of-order messages are buffered until causally deliverable.

**Setup:** 3 nodes, rapid trading.

### Steps:

```
STEP 1: Start all nodes with debug logging
────────────────────────────────────────────────────────
Terminal 1:  python node.py 1 --nodes 2,3 --debug
Terminal 2:  python node.py 2 --nodes 1,3 --debug
Terminal 3:  python node.py 3 --nodes 1,2 --debug

STEP 2: Wait for election

STEP 3: Execute multiple rapid trades
────────────────────────────────────────────────────────
On Node 1 (quickly, one after another):
  sell 2 10
  sell 3 15

On Node 2 (quickly):
  sell 3 20

STEP 4: Observe debug logs for buffering
────────────────────────────────────────────────────────
Look for: "Buffering message for causal delivery"
Look for: "Delivering buffered message from Node X"
```

### Expected Behavior:

```
Possible buffering scenario:
────────────────────────────────────────────────────────

If Node 3 receives TRADE_REQUEST from Node 2 before
it has processed the earlier message from Node 1:

Node 3 logs:
  "Buffering message for causal delivery"
  (Message saved, not yet processed)

Later, after processing Node 1's message:
  "Delivering buffered message from Node 2"
  (Causal dependencies now satisfied)

This ensures:
  - Messages are processed in causal order
  - No trade depends on state that hasn't been seen yet
```

### Verification:

```
On each node, type: status

Check "Buffered Messages" count:
  - Usually 0 (messages delivered promptly)
  - May briefly show 1-2 during rapid trading

On each node, type: history

Verify all trades completed successfully with correct amounts.
```

### Key Observations:

1. **Causal delivery check**: `can_deliver()` verifies dependencies are met
2. **Buffering**: Messages wait in `MessageBuffer` until deliverable
3. **Periodic flush**: Buffer is checked every 0.5 seconds
4. **Eventual delivery**: All messages eventually delivered in correct order

---

## Scenario 5: Graceful Leave vs Crash

**Purpose:** Compare graceful shutdown (LEAVE message) vs crash (no message).

**Setup:** 3 nodes.

### Steps:

```
STEP 1: Start all nodes
────────────────────────────────────────────────────────
Terminal 1:  python node.py 1 --nodes 2,3
Terminal 2:  python node.py 2 --nodes 1,3
Terminal 3:  python node.py 3 --nodes 1,2

STEP 2: Graceful leave from Node 1
────────────────────────────────────────────────────────
On Node 1, type: quit

STEP 3: Observe immediate removal on other nodes
────────────────────────────────────────────────────────
```

### Expected Behavior (Graceful Leave):

```
Node 1:
  - Broadcasts LEAVE message
  - Logs: "LEAVE announced"
  - Shuts down cleanly

Node 2 and Node 3 (immediately):
  - Log: "Node 1 is leaving"
  - Remove Node 1 from tracking
  - No suspicion phase, no waiting
  
On Node 2, type: nodes
  Node 2 (self, active)
  Node 3 (active, coordinator)
  
(Node 1 is immediately gone - no suspicion period)
```

### Compare to Crash (from Scenario 1):

```
Crash (Ctrl+C without quit):
  - No LEAVE message sent
  - Other nodes must wait for HEARTBEAT_TIMEOUT (6s)
  - Then wait for second timeout to confirm (another 6s)
  - Total: ~12 seconds before node is removed

Graceful Leave (quit command):
  - LEAVE message broadcast
  - Other nodes remove immediately
  - No waiting, no suspicion phase
  - Total: immediate removal
```

### Key Observations:

1. **LEAVE broadcast**: Graceful shutdown notifies other nodes
2. **Immediate removal**: No timeout waiting for graceful leaves
3. **Crash detection**: Requires heartbeat timeout + confirmation
4. **Trade-off**: Graceful is faster, crash detection is more robust

---

## Scenario 6: Trade Rejection (Insufficient Credits)

**Purpose:** Demonstrate trade validation and rejection.

**Setup:** 3 nodes.

### Steps:

```
STEP 1: Start all nodes
────────────────────────────────────────────────────────
Terminal 1:  python node.py 1 --nodes 2,3
Terminal 2:  python node.py 2 --nodes 1,3
Terminal 3:  python node.py 3 --nodes 1,2

STEP 2: Check initial balance
────────────────────────────────────────────────────────
On Node 1, type: balance
Expected: Balance: 100 credits

STEP 3: Attempt to sell more than available
────────────────────────────────────────────────────────
On Node 1, type: sell 2 150

STEP 4: Observe rejection
────────────────────────────────────────────────────────
```

### Expected Behavior:

```
On Node 1:
────────────────────────────────────────────────────────
"Cannot sell 150 credits - insufficient balance"

(Trade is rejected locally before even sending)

On Node 2:
────────────────────────────────────────────────────────
(Nothing - trade request was never sent)
```

### Now test rejection by counterparty:

```
STEP 5: Drain Node 2's credits first
────────────────────────────────────────────────────────
On Node 2, type: sell 1 90
(Node 2 now has 10 credits)

STEP 6: Try to buy more than Node 2 has
────────────────────────────────────────────────────────
On Node 1, type: buy 2 50

STEP 7: Observe rejection from Node 2
────────────────────────────────────────────────────────
```

### Expected Behavior:

```
On Node 1:
  "Proposed trade to Node 2: buy 50 credits"
  
On Node 2:
  "Trade request from Node 1: buy 50 credits"
  "Trade REJECTED (Insufficient credits)"
  
On Node 1 (after response):
  "Trade REJECTED by Node 2: Insufficient credits"
```

### Key Observations:

1. **Local validation**: Seller checks balance before sending request
2. **Remote validation**: Counterparty validates before accepting
3. **Rejection reason**: TRADE_RESPONSE includes reason for rejection
4. **No partial execution**: Failed trades don't modify any balances

---

## Summary of Test Scenarios

| Scenario | Concepts Demonstrated |
|----------|----------------------|
| **1. Leader Crash and Re-Election** | Heartbeat timeout, suspicion → failure, Bully algorithm |
| **2. Node Join After Election** | Dynamic discovery, JOIN broadcast, re-election with higher node |
| **3. Causal Message Ordering** | Vector clock increment/merge, happened-before relationship |
| **4. Rapid Trades with Buffering** | Causal delivery check, message buffering, eventual delivery |
| **5. Graceful Leave vs Crash** | LEAVE message, immediate vs timeout-based removal |
| **6. Trade Rejection** | Local validation, remote validation, rejection reasons |
