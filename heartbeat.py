"""
heartbeat.py - Heartbeat-based fault detection mechanism.

Responsibility:
    - Periodically send heartbeat messages to indicate liveness
    - Monitor heartbeats from other nodes
    - Detect node failures based on missing heartbeats
    - Trigger leader election when coordinator fails

Failure Detection Model:
    This implements an "eventually perfect" failure detector:
    - Failure is treated as SUSPICION, not certainty
    - A node is suspected after missing HEARTBEAT_TIMEOUT seconds
    - Suspected nodes may recover (false positives are possible)
    - Uses timeout-based detection (not ping-based)

Heartbeat Flow:
    Non-Leader Nodes:
        1. Periodically send HEARTBEAT to leader via multicast
        2. Leader responds with ACK (also via multicast for simplicity)
        3. If no ACK from leader within timeout, suspect leader failure
        4. Trigger leader election if leader is suspected dead
    
    Leader Node:
        1. Receive heartbeats from all nodes
        2. Track last heartbeat time for each node
        3. Detect nodes that haven't sent heartbeat within timeout
        4. Mark suspected nodes and notify the system
"""

import threading
import time
import logging
from collections import defaultdict

from config import HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT
from message import Message, MSG_HEARTBEAT, create_heartbeat

# Configure logging for heartbeat events
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('heartbeat')


class HeartbeatManager:
    """
    Manages heartbeat sending and failure detection.
    
    Runs two background threads:
    1. Heartbeat sender: Periodically sends heartbeat messages
    2. Failure checker: Monitors for nodes that missed heartbeats
    
    Failure detection is probabilistic:
    - Missing heartbeats -> node is SUSPECTED
    - Receiving heartbeat from suspected node -> suspicion cleared
    - Only after sustained absence is a node considered FAILED
    """
    
    def __init__(self, node_id, multicast_handler, vector_clock,
                 on_node_failure=None, on_leader_failure=None, get_coordinator=None):
        """
        Initialize the heartbeat manager.
        
        Args:
            node_id: This node's unique identifier
            multicast_handler: MulticastHandler for sending/receiving heartbeats
            vector_clock: VectorClock instance for message ordering
            on_node_failure: Callback when a node is detected as failed
                            Signature: on_node_failure(failed_node_id)
            on_leader_failure: Callback when leader failure is detected
                              Signature: on_leader_failure()
            get_coordinator: Function that returns current coordinator ID
                            Signature: get_coordinator() -> int
        """
        self.node_id = node_id
        self.multicast_handler = multicast_handler
        self.vector_clock = vector_clock
        
        # Callbacks for failure events
        self._on_node_failure = on_node_failure
        self._on_leader_failure = on_leader_failure
        self._get_coordinator = get_coordinator
        
        # Track last heartbeat time for each node
        # Maps node_id -> timestamp of last heartbeat received
        self._last_heartbeat = defaultdict(float)
        self._heartbeat_lock = threading.Lock()
        
        # Set of nodes currently suspected of failure
        # Suspected != Failed (may recover)
        self._suspected_nodes = set()
        
        # Set of known active nodes in the system
        self._known_nodes = set()
        self._known_nodes.add(node_id)  # Include self
        
        # Track last time we received ACK from leader (for non-leaders)
        self._last_leader_ack = time.time()
        
        # Control flags for background threads
        self._running = False
        self._sender_thread = None
        self._checker_thread = None
        
        # Optional: energy credits to include in heartbeat
        self._energy_credits = None
        
        logger.info(f"Node {self.node_id}: HeartbeatManager initialized "
                   f"(interval={HEARTBEAT_INTERVAL}s, timeout={HEARTBEAT_TIMEOUT}s)")
    
    def set_energy_credits(self, credits):
        """
        Set energy credits to include in heartbeat messages.
        
        Args:
            credits: Current energy credit balance
        """
        self._energy_credits = credits
    
    def add_node(self, node_id):
        """
        Add a node to the set of known nodes.
        
        Called when a new node joins the system.
        
        Args:
            node_id: ID of the joining node
        """
        with self._heartbeat_lock:
            self._known_nodes.add(node_id)
            # Initialize last heartbeat to now (give them grace period)
            self._last_heartbeat[node_id] = time.time()
            # Clear any suspicion
            self._suspected_nodes.discard(node_id)
            
        logger.info(f"Node {self.node_id}: Added node {node_id} to heartbeat tracking")
    
    def remove_node(self, node_id):
        """
        Remove a node from tracking (graceful departure).
        
        Called when a node announces it's leaving.
        
        Args:
            node_id: ID of the departing node
        """
        with self._heartbeat_lock:
            self._known_nodes.discard(node_id)
            self._last_heartbeat.pop(node_id, None)
            self._suspected_nodes.discard(node_id)
            
        logger.info(f"Node {self.node_id}: Removed node {node_id} from heartbeat tracking")
    
    def start(self):
        """
        Start the heartbeat sender and failure checker threads.
        """
        if self._running:
            logger.warning(f"Node {self.node_id}: Heartbeat manager already running")
            return
        
        self._running = True
        
        # Start heartbeat sender thread
        self._sender_thread = threading.Thread(
            target=self._send_heartbeats,
            name=f"Heartbeat-Sender-{self.node_id}",
            daemon=True
        )
        self._sender_thread.start()
        
        # Start failure checker thread
        self._checker_thread = threading.Thread(
            target=self._check_failures,
            name=f"Heartbeat-Checker-{self.node_id}",
            daemon=True
        )
        self._checker_thread.start()
        
        logger.info(f"Node {self.node_id}: Heartbeat manager started")
    
    def stop(self):
        """
        Stop the heartbeat threads.
        """
        self._running = False
        
        # Wait for threads to finish
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=2.0)
        if self._checker_thread and self._checker_thread.is_alive():
            self._checker_thread.join(timeout=2.0)
        
        logger.info(f"Node {self.node_id}: Heartbeat manager stopped")
    
    def record_heartbeat(self, node_id):
        """
        Record that a heartbeat was received from a node.
        
        Called by the message handler when a HEARTBEAT message arrives.
        Updates the last-seen timestamp and clears any suspicion.
        
        Args:
            node_id: ID of the node that sent the heartbeat
        """
        current_time = time.time()
        
        with self._heartbeat_lock:
            # Update last heartbeat time
            self._last_heartbeat[node_id] = current_time
            
            # Add to known nodes if new
            self._known_nodes.add(node_id)
            
            # Clear suspicion if previously suspected
            if node_id in self._suspected_nodes:
                self._suspected_nodes.discard(node_id)
                logger.info(f"Node {self.node_id}: Node {node_id} recovered "
                           f"(suspicion cleared)")
        
        logger.debug(f"Node {self.node_id}: Recorded heartbeat from Node {node_id}")
    
    def record_leader_ack(self):
        """
        Record that we received an acknowledgment from the leader.
        
        Called when the leader responds to our heartbeat.
        Resets the leader failure timeout.
        """
        self._last_leader_ack = time.time()
        logger.debug(f"Node {self.node_id}: Recorded leader ACK")
    
    def _send_heartbeats(self):
        """
        Background thread: periodically send heartbeat messages.
        
        Heartbeats are sent via multicast so all nodes (including leader)
        can track this node's liveness.
        
        Heartbeat message includes:
        - sender_id: This node's ID
        - vector_clock: Current logical time
        - payload: Optional energy credits
        """
        logger.debug(f"Node {self.node_id}: Heartbeat sender thread started")
        
        while self._running:
            try:
                # NOTE: We intentionally do NOT increment vector clock for heartbeats.
                # Heartbeats are liveness probes, not application events.
                # Including them in causal ordering pollutes the clock and causes
                # nodes that miss heartbeats to buffer all subsequent trade messages.
                # See ARCHITECTURAL_AUDIT.md Section 3 for details.
                
                # Create heartbeat message (no vector clock)
                heartbeat_msg = create_heartbeat(
                    sender_id=self.node_id,
                    vector_clock=None,  # Heartbeats don't participate in causal ordering
                    credits=self._energy_credits
                )
                
                # Send via multicast
                self.multicast_handler.send(heartbeat_msg)
                
                logger.debug(f"Node {self.node_id}: HEARTBEAT SENT (credits={self._energy_credits})")
                
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error sending heartbeat: {e}")
            
            # Wait for next heartbeat interval
            # Use small sleep increments to allow faster shutdown
            sleep_time = 0
            while sleep_time < HEARTBEAT_INTERVAL and self._running:
                time.sleep(0.5)
                sleep_time += 0.5
        
        logger.debug(f"Node {self.node_id}: Heartbeat sender thread stopped")
    
    def _check_failures(self):
        """
        Background thread: check for nodes that missed heartbeats.
        
        Failure Detection Algorithm:
        1. For each known node, check time since last heartbeat
        2. If time > HEARTBEAT_TIMEOUT, mark as SUSPECTED
        3. If already suspected and still no heartbeat, mark as FAILED
        4. If failed node is the leader, trigger election
        
        Note: This implements "suspicion" before "failure" to reduce
        false positives from temporary network issues.
        """
        logger.debug(f"Node {self.node_id}: Failure checker thread started")
        
        # Wait a bit before starting checks (allow initial heartbeats)
        time.sleep(HEARTBEAT_INTERVAL)
        
        while self._running:
            try:
                current_time = time.time()
                
                # Get current coordinator
                coordinator_id = None
                if self._get_coordinator:
                    coordinator_id = self._get_coordinator()
                
                # Check each known node
                nodes_to_check = []
                with self._heartbeat_lock:
                    nodes_to_check = list(self._known_nodes)
                
                for node_id in nodes_to_check:
                    # Don't check ourselves
                    if node_id == self.node_id:
                        continue
                    
                    with self._heartbeat_lock:
                        last_seen = self._last_heartbeat.get(node_id, 0)
                    
                    time_since_heartbeat = current_time - last_seen
                    
                    # Check if node missed heartbeat timeout
                    if time_since_heartbeat > HEARTBEAT_TIMEOUT:
                        self._handle_suspected_failure(node_id, coordinator_id)
                
                # For non-leaders: check if leader is alive
                if coordinator_id is not None and coordinator_id != self.node_id:
                    time_since_leader = current_time - self._last_leader_ack
                    if time_since_leader > HEARTBEAT_TIMEOUT:
                        self._handle_leader_failure(coordinator_id)
                
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error in failure check: {e}")
            
            # Check every half the heartbeat interval
            time.sleep(HEARTBEAT_INTERVAL / 2)
        
        logger.debug(f"Node {self.node_id}: Failure checker thread stopped")
    
    def _handle_suspected_failure(self, node_id, coordinator_id):
        """
        Handle a node that is suspected of failure.
        
        Two-phase detection:
        1. First timeout -> SUSPECTED (logged, may recover)
        2. Second timeout -> FAILED (callback triggered)
        
        Args:
            node_id: ID of the suspected node
            coordinator_id: Current coordinator ID (for leader failure check)
        """
        with self._heartbeat_lock:
            was_suspected = node_id in self._suspected_nodes
            
            if not was_suspected:
                # First timeout: mark as suspected
                self._suspected_nodes.add(node_id)
                logger.warning(f"Node {self.node_id}: Node {node_id} SUSPECTED "
                              f"(missed heartbeat timeout)")
            else:
                # Second timeout: confirm failure
                logger.error(f"Node {self.node_id}: Node {node_id} FAILED "
                            f"(sustained heartbeat absence)")
                
                # Remove from tracking
                self._known_nodes.discard(node_id)
                self._last_heartbeat.pop(node_id, None)
                self._suspected_nodes.discard(node_id)
                
                # Trigger failure callback
                if self._on_node_failure:
                    try:
                        self._on_node_failure(node_id)
                    except Exception as e:
                        logger.error(f"Node {self.node_id}: Error in failure callback: {e}")
                
                # If failed node was the leader, trigger election
                if node_id == coordinator_id:
                    logger.warning(f"Node {self.node_id}: LEADER FAILED - triggering election")
                    if self._on_leader_failure:
                        try:
                            self._on_leader_failure()
                        except Exception as e:
                            logger.error(f"Node {self.node_id}: Error in leader failure callback: {e}")
    
    def _handle_leader_failure(self, coordinator_id):
        """
        Handle suspected leader failure (from non-leader perspective).
        
        If we haven't received acknowledgment from leader within timeout,
        we suspect the leader has failed and trigger an election.
        
        Args:
            coordinator_id: ID of the current (possibly failed) coordinator
        """
        with self._heartbeat_lock:
            if coordinator_id in self._suspected_nodes:
                # Already suspected, now confirm and trigger election
                logger.error(f"Node {self.node_id}: LEADER {coordinator_id} CONFIRMED FAILED")
                
                self._suspected_nodes.discard(coordinator_id)
                
                if self._on_leader_failure:
                    try:
                        self._on_leader_failure()
                    except Exception as e:
                        logger.error(f"Node {self.node_id}: Error in leader failure callback: {e}")
            else:
                # First suspicion
                self._suspected_nodes.add(coordinator_id)
                logger.warning(f"Node {self.node_id}: LEADER {coordinator_id} SUSPECTED "
                              f"(no ACK received)")
    
    def get_active_nodes(self):
        """
        Get the set of currently active (non-suspected) nodes.
        
        Returns:
            set: Node IDs of active nodes
        """
        with self._heartbeat_lock:
            return self._known_nodes - self._suspected_nodes
    
    def get_suspected_nodes(self):
        """
        Get the set of currently suspected nodes.
        
        Returns:
            set: Node IDs of suspected nodes
        """
        with self._heartbeat_lock:
            return set(self._suspected_nodes)
    
    def is_node_alive(self, node_id):
        """
        Check if a node is considered alive (not suspected).
        
        Args:
            node_id: ID of the node to check
            
        Returns:
            bool: True if node is alive, False if suspected/unknown
        """
        with self._heartbeat_lock:
            return node_id in self._known_nodes and node_id not in self._suspected_nodes
