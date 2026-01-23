"""
vector_clock.py - Vector clock implementation for message ordering.

Responsibility:
    - Maintain logical timestamps for each node
    - Increment local clock on send events
    - Merge clocks on receive events
    - Compare vector clocks to determine causal ordering
    - Support dynamic group membership (nodes can join/leave)

Vector Clock Theory:
    A vector clock is an array of N logical clocks, one per node.
    Each node maintains its own vector clock and includes it in messages.
    
    Rules:
    1. On SEND: Increment own clock, attach vector clock to message
    2. On RECEIVE: Merge received clock with local clock (element-wise max),
       then increment own clock
    
    Causal Ordering:
    Event A "happened before" event B (A -> B) if:
    - VC(A)[i] <= VC(B)[i] for all nodes i, AND
    - VC(A)[j] < VC(B)[j] for at least one node j
"""

import copy
import threading


class VectorClock:
    """
    Vector clock for tracking causality in a distributed system.
    
    Supports dynamic membership - nodes can be added or removed at runtime.
    Thread-safe implementation using locks for concurrent access.
    
    The clock is stored as a dictionary mapping node_id (string) to 
    logical timestamp (integer). Using strings as keys ensures JSON
    serialization compatibility.
    """
    
    def __init__(self, node_id):
        """
        Initialize vector clock for a node.
        
        Args:
            node_id: Unique identifier for this node (will be converted to string)
            
        Note:
            Clock starts at 0 for this node. Other nodes are added dynamically
            as messages are received from them.
        """
        # Convert node_id to string for consistent dictionary keys
        self.node_id = str(node_id)
        
        # The vector clock: maps node_id -> logical timestamp
        # Initialize with only this node's entry
        self._clock = {self.node_id: 0}
        
        # Lock for thread-safe access
        self._lock = threading.Lock()
    
    def increment(self):
        """
        Increment this node's logical clock.
        
        Called BEFORE sending a message. This ensures the message
        carries a clock value that reflects the send event.
        
        Returns:
            dict: Copy of the current clock state (for attaching to message)
        """
        with self._lock:
            self._clock[self.node_id] = self._clock.get(self.node_id, 0) + 1
            # Return a copy to attach to outgoing message
            return copy.deepcopy(self._clock)
    
    def update(self, received_clock):
        """
        Merge received vector clock with local clock.
        
        Called AFTER receiving a message. For each node in either clock,
        take the maximum value. This captures all causal dependencies.
        
        Merge rule: VC_local[i] = max(VC_local[i], VC_received[i]) for all i
        
        Args:
            received_clock: Dictionary of {node_id: timestamp} from received message
        """
        if received_clock is None:
            return
        
        with self._lock:
            # Convert all keys to strings for consistency
            received = {str(k): v for k, v in received_clock.items()}
            
            # Merge: take element-wise maximum
            all_nodes = set(self._clock.keys()) | set(received.keys())
            for node in all_nodes:
                local_val = self._clock.get(node, 0)
                received_val = received.get(node, 0)
                self._clock[node] = max(local_val, received_val)
            
            # Increment own clock after receive (optional but common)
            # This ensures receive events are also captured
            self._clock[self.node_id] = self._clock.get(self.node_id, 0) + 1
    
    def get_clock(self):
        """
        Return current clock state as a dictionary.
        
        Returns:
            dict: Copy of {node_id: timestamp} mapping
        """
        with self._lock:
            return copy.deepcopy(self._clock)
    
    def get_local_time(self):
        """
        Return this node's logical timestamp.
        
        Returns:
            int: Current logical time for this node
        """
        with self._lock:
            return self._clock.get(self.node_id, 0)
    
    def add_node(self, node_id):
        """
        Add a new node to the vector clock (for dynamic membership).
        
        Called when a new node joins the system.
        
        Args:
            node_id: ID of the joining node
        """
        with self._lock:
            node_key = str(node_id)
            if node_key not in self._clock:
                self._clock[node_key] = 0
    
    def remove_node(self, node_id):
        """
        Remove a departed node from the vector clock.
        
        Called when a node leaves the system (gracefully or due to failure).
        Removing departed nodes prevents unbounded clock growth.
        
        Args:
            node_id: ID of the departing node
            
        NOTE: We intentionally do NOT remove entries from the vector clock.
        The Chat Application report (Section 2.5) recommends this approach:
        "Messages in the hold-back queue that depend on a removed entry remain valid."
        Removing entries can cause buffered messages to become undeliverable.
        See ARCHITECTURAL_AUDIT.md Section 3 for details.
        """
        # Intentionally empty - never remove vector clock entries
        # This prevents race conditions with the hold-back queue
        pass
    
    def can_deliver(self, sender_id, message_clock):
        """
        Check if a message satisfies causal delivery conditions.
        
        Causal Delivery Condition:
        A message M from sender S can be delivered to this node if:
        
        1. VC_msg[S] == VC_local[S] + 1
           (This is the NEXT expected message from S - no gaps)
        
        2. For all nodes K where K != S:
           VC_msg[K] <= VC_local[K]
           (We have already seen all messages that causally precede M)
        
        In other words:
        - The message must be the next in sequence from its sender
        - We must have already received all messages that happened before it
        
        Args:
            sender_id: ID of the node that sent the message
            message_clock: Vector clock attached to the message
            
        Returns:
            bool: True if message can be delivered, False if it must be buffered
            
        Example:
            If local clock is {A:2, B:3, C:1} and we receive from B with 
            clock {A:2, B:4, C:1}, we can deliver because:
            - VC_msg[B]=4 == VC_local[B]+1=3+1=4 ✓
            - VC_msg[A]=2 <= VC_local[A]=2 ✓
            - VC_msg[C]=1 <= VC_local[C]=1 ✓
            
            But if message clock was {A:3, B:4, C:1}, we must buffer because:
            - VC_msg[A]=3 > VC_local[A]=2 ✗ (missing a message from A)
        """
        if message_clock is None:
            # No clock attached - deliver immediately (legacy/simple message)
            return True
        
        sender_key = str(sender_id)
        
        with self._lock:
            # Convert message clock keys to strings
            msg_clock = {str(k): v for k, v in message_clock.items()}
            
            # Condition 1: Message is next in sequence from sender
            local_sender_time = self._clock.get(sender_key, 0)
            msg_sender_time = msg_clock.get(sender_key, 0)
            
            if msg_sender_time != local_sender_time + 1:
                # Not the next expected message from this sender
                return False
            
            # Condition 2: All other entries are <= our local clock
            for node_id, msg_time in msg_clock.items():
                if node_id == sender_key:
                    continue  # Already checked above
                
                local_time = self._clock.get(node_id, 0)
                if msg_time > local_time:
                    # Message depends on something we haven't seen yet
                    return False
            
            return True
    
    def compare(self, other_clock):
        """
        Compare this vector clock with another to determine ordering.
        
        Returns:
            "BEFORE": This clock happened before the other (this -> other)
            "AFTER": This clock happened after the other (other -> this)  
            "CONCURRENT": Events are concurrent (neither happened before)
            "EQUAL": Clocks are identical
            
        Comparison rules:
        - A < B if A[i] <= B[i] for all i, and A[j] < B[j] for some j
        - A > B if B < A
        - A || B (concurrent) if neither A < B nor B < A
        
        Args:
            other_clock: Dictionary representing another vector clock
            
        Returns:
            str: One of "BEFORE", "AFTER", "CONCURRENT", "EQUAL"
        """
        if other_clock is None:
            return "AFTER"
        
        with self._lock:
            # Convert other clock keys to strings
            other = {str(k): v for k, v in other_clock.items()}
            
            # Get all node IDs from both clocks
            all_nodes = set(self._clock.keys()) | set(other.keys())
            
            less_than = False  # True if any self[i] < other[i]
            greater_than = False  # True if any self[i] > other[i]
            
            for node in all_nodes:
                self_val = self._clock.get(node, 0)
                other_val = other.get(node, 0)
                
                if self_val < other_val:
                    less_than = True
                elif self_val > other_val:
                    greater_than = True
            
            if less_than and not greater_than:
                return "BEFORE"
            elif greater_than and not less_than:
                return "AFTER"
            elif not less_than and not greater_than:
                return "EQUAL"
            else:
                return "CONCURRENT"
    
    def __repr__(self):
        """String representation for debugging."""
        with self._lock:
            return f"VectorClock(node={self.node_id}, clock={self._clock})"
    
    def __str__(self):
        """Human-readable string of clock state."""
        with self._lock:
            entries = [f"{k}:{v}" for k, v in sorted(self._clock.items())]
            return f"[{', '.join(entries)}]"
