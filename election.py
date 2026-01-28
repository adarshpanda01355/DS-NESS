"""
election.py - Bully algorithm for leader election.

Responsibility:
    - Implement the Bully election algorithm
    - Start election when coordinator failure is detected
    - Handle ELECTION and OK messages
    - Announce new coordinator via COORDINATOR message
    - Node with highest ID becomes the leader

Bully Algorithm Overview:
    The Bully algorithm elects the node with the highest priority (ID) as leader.
    It is called "Bully" because the highest-priority node "bullies" others into
    accepting it as the leader.

    PHASE 1 - Election Initiation:
        When a node detects the coordinator has failed:
        1. Send ELECTION message to all nodes with HIGHER priority
        2. Wait for OK responses (timeout = ELECTION_TIMEOUT)
    
    PHASE 2 - Response Handling:
        If OK received from any higher-priority node:
            - Stop election, wait for COORDINATOR announcement
            - A higher node will take over the election
        
        If NO OK received (timeout):
            - This node has the highest priority among alive nodes
            - Declare self as coordinator
    
    PHASE 3 - Coordinator Announcement:
        The new coordinator broadcasts COORDINATOR message to ALL nodes.
        All nodes update their coordinator reference.

Message Types Used:
    - ELECTION: "I am starting an election" (sent to higher-ID nodes)
    - OK: "I am alive and have higher priority" (response to ELECTION)
    - COORDINATOR: "I am the new leader" (broadcast to all)
"""

import threading
import time
import logging

from config import ELECTION_TIMEOUT
from message import (
    Message, MSG_ELECTION, MSG_OK, MSG_COORDINATOR,
    create_election, create_ok, create_coordinator
)

# Configure logging for election events
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('election')


class ElectionManager:
    """
    Manages leader election using the Bully algorithm.
    
    Key properties:
    - node_id: This node's unique identifier (also its priority)
    - coordinator_id: Current leader's ID (None if unknown)
    - election_in_progress: True if an election is currently running
    
    Higher node_id = Higher priority = More likely to become leader
    """
    
    def __init__(self, node_id, unicast_handler, multicast_handler, vector_clock,
                 known_nodes=None, on_coordinator_change=None):
        """
        Initialize the election manager.
        
        Args:
            node_id: This node's unique identifier (used as priority)
            unicast_handler: UnicastHandler for direct ELECTION/OK messages
            multicast_handler: MulticastHandler for COORDINATOR broadcast
            vector_clock: VectorClock instance for message ordering
            known_nodes: Set of known node IDs in the system
            on_coordinator_change: Callback when coordinator changes
                                  Signature: on_coordinator_change(new_coordinator_id)
        """
        self.node_id = node_id
        self.priority = node_id  # Priority equals node ID in Bully algorithm
        
        self.unicast_handler = unicast_handler
        self.multicast_handler = multicast_handler
        self.vector_clock = vector_clock
        
        # Current coordinator (leader) - None if unknown
        self._coordinator_id = None
        self._coordinator_lock = threading.Lock()
        
        # Set of known nodes in the system
        self._known_nodes = set(known_nodes) if known_nodes else set()
        self._known_nodes.add(node_id)  # Include self
        self._nodes_lock = threading.Lock()
        
        # Election state
        self._election_in_progress = False
        self._election_lock = threading.Lock()
        
        # Track if we received an OK during election
        self._received_ok = False
        self._ok_event = threading.Event()
        
        # Callback for coordinator changes
        self._on_coordinator_change = on_coordinator_change
        
        logger.info(f"Node {self.node_id}: ElectionManager initialized "
                   f"(priority={self.priority}, timeout={ELECTION_TIMEOUT}s)")
    
    def add_node(self, node_id):
        """
        Add a node to the known nodes set.
        
        Args:
            node_id: ID of the node to add
        """
        with self._nodes_lock:
            self._known_nodes.add(node_id)
        logger.debug(f"Node {self.node_id}: Added node {node_id} to election tracking")
    
    def remove_node(self, node_id):
        """
        Remove a node from the known nodes set.
        
        Args:
            node_id: ID of the node to remove
        """
        with self._nodes_lock:
            self._known_nodes.discard(node_id)
        logger.debug(f"Node {self.node_id}: Removed node {node_id} from election tracking")
    
    def start_election(self):
        """
        Initiate a new election process (PHASE 1).
        
        Called when:
        - This node detects the coordinator has failed
        - This node starts up and doesn't know the coordinator
        - This node receives an ELECTION from a lower-priority node
        
        Algorithm:
        1. Mark election as in progress
        2. Send ELECTION to all higher-priority nodes
        3. Wait for OK responses (with timeout)
        4. If no OK received, declare self as coordinator
        """
        # Check if election already in progress
        with self._election_lock:
            if self._election_in_progress:
                logger.debug(f"Node {self.node_id}: Election already in progress, skipping")
                return
            self._election_in_progress = True
        
        logger.info(f"Node {self.node_id}: ========== STARTING ELECTION ==========")
        
        # Reset OK tracking
        self._received_ok = False
        self._ok_event.clear()
        
        # Get list of higher-priority nodes
        higher_nodes = self._get_higher_priority_nodes()
        
        if not higher_nodes:
            # No higher-priority nodes exist - we are the highest!
            logger.info(f"Node {self.node_id}: No higher-priority nodes, declaring self as coordinator")
            self._declare_victory()
            return
        
        # PHASE 1: Send ELECTION message to all higher-priority nodes
        logger.info(f"Node {self.node_id}: Sending ELECTION to higher nodes: {higher_nodes}")
        
        # Increment vector clock for send
        clock = self.vector_clock.increment()
        
        # Create ELECTION message
        election_msg = create_election(
            sender_id=self.node_id,
            sender_priority=self.priority,
            vector_clock=clock
        )
        
        # Send to each higher-priority node via unicast
        for target_id in higher_nodes:
            self.unicast_handler.send(election_msg, target_id)
            logger.debug(f"Node {self.node_id}: ELECTION sent to Node {target_id}")
        
        # PHASE 2: Wait for OK responses
        # Start waiting in a separate thread to not block
        election_thread = threading.Thread(
            target=self._wait_for_ok_responses,
            name=f"Election-Wait-{self.node_id}",
            daemon=True
        )
        election_thread.start()
    
    def _wait_for_ok_responses(self):
        """
        Wait for OK responses from higher-priority nodes (PHASE 2).
        
        If we receive any OK:
            - A higher node is alive and will run the election
            - We step back and wait for COORDINATOR announcement
        
        If timeout with no OK:
            - No higher node responded
            - We declare ourselves the coordinator
        """
        logger.debug(f"Node {self.node_id}: Waiting for OK responses "
                    f"(timeout={ELECTION_TIMEOUT}s)")
        
        # Wait for OK with timeout
        received = self._ok_event.wait(timeout=ELECTION_TIMEOUT)
        
        with self._election_lock:
            if self._received_ok:
                # Received OK from a higher-priority node
                # That node will complete the election
                logger.info(f"Node {self.node_id}: Received OK from higher node, "
                           f"waiting for COORDINATOR announcement")
                self._election_in_progress = False
                return
            
            # No OK received - we are the highest alive node
            logger.info(f"Node {self.node_id}: No OK received (timeout), "
                       f"declaring self as coordinator")
            self._declare_victory()
    
    def _declare_victory(self):
        """
        Declare this node as the new coordinator (PHASE 3).
        
        Broadcasts COORDINATOR message to all nodes via multicast.
        All nodes will update their coordinator reference.
        """
        logger.info(f"Node {self.node_id}: ========== DECLARING VICTORY ==========")
        
        # Update local coordinator reference
        with self._coordinator_lock:
            old_coordinator = self._coordinator_id
            self._coordinator_id = self.node_id
        
        # End election
        with self._election_lock:
            self._election_in_progress = False
        
        # Increment vector clock for send
        clock = self.vector_clock.increment()
        
        # Create COORDINATOR announcement
        coordinator_msg = create_coordinator(
            sender_id=self.node_id,
            sender_priority=self.priority,
            vector_clock=clock
        )
        
        # Broadcast to all nodes via multicast (reliable to ensure all nodes receive it)
        self.multicast_handler.send_reliable(coordinator_msg)
        
        logger.info(f"Node {self.node_id}: COORDINATOR message broadcast (reliable) - "
                   f"I am the new leader!")
        
        # Notify callback
        if self._on_coordinator_change and old_coordinator != self.node_id:
            try:
                self._on_coordinator_change(self.node_id)
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error in coordinator change callback: {e}")
    
    def handle_election_message(self, sender_id, sender_priority):
        """
        Handle incoming ELECTION message from another node.
        
        When we receive an ELECTION message:
        1. If sender has lower priority: Send OK, then start our own election
        2. If sender has higher priority: This shouldn't happen (they wouldn't send to us)
        
        Args:
            sender_id: ID of the node that sent the ELECTION
            sender_priority: Priority of the sending node
        """
        logger.info(f"Node {self.node_id}: Received ELECTION from Node {sender_id} "
                   f"(priority={sender_priority})")
        
        # Only respond if we have higher priority
        if self.priority > sender_priority:
            # Send OK to tell them we're taking over
            logger.info(f"Node {self.node_id}: Sending OK to Node {sender_id} "
                       f"(we have higher priority)")
            
            # Increment vector clock
            clock = self.vector_clock.increment()
            
            # Create and send OK message
            ok_msg = create_ok(
                sender_id=self.node_id,
                sender_priority=self.priority,
                vector_clock=clock
            )
            self.unicast_handler.send(ok_msg, sender_id)
            
            # Start our own election (we might be the highest)
            # Use a small delay to avoid message collision
            threading.Timer(0.1, self.start_election).start()
        else:
            # Sender has higher or equal priority - shouldn't receive ELECTION from them
            logger.warning(f"Node {self.node_id}: Unexpected ELECTION from higher/equal "
                          f"priority Node {sender_id}")
    
    def handle_ok_message(self, sender_id, sender_priority):
        """
        Handle incoming OK message (election response).
        
        Receiving an OK means a higher-priority node is alive and will
        take over the election. We should stop and wait.
        
        Args:
            sender_id: ID of the node that sent the OK
            sender_priority: Priority of the sending node
        """
        logger.info(f"Node {self.node_id}: Received OK from Node {sender_id} "
                   f"(priority={sender_priority})")
        
        # Mark that we received an OK
        self._received_ok = True
        self._ok_event.set()
        
        logger.debug(f"Node {self.node_id}: Higher-priority node {sender_id} will "
                    f"take over election")
    
    def handle_coordinator_message(self, coordinator_id, coordinator_priority):
        """
        Handle incoming COORDINATOR announcement.
        
        A node is announcing itself as the new leader.
        Update our coordinator reference.
        
        Args:
            coordinator_id: ID of the new coordinator
            coordinator_priority: Priority of the new coordinator
        """
        logger.info(f"Node {self.node_id}: Received COORDINATOR from Node {coordinator_id} "
                   f"(priority={coordinator_priority})")
        
        with self._coordinator_lock:
            old_coordinator = self._coordinator_id
            self._coordinator_id = coordinator_id
        
        # End any ongoing election
        with self._election_lock:
            self._election_in_progress = False
        
        # Clear OK event (election is over)
        self._received_ok = False
        self._ok_event.clear()
        
        logger.info(f"Node {self.node_id}: Accepted Node {coordinator_id} as coordinator")
        
        # Notify callback if coordinator changed
        if self._on_coordinator_change and old_coordinator != coordinator_id:
            try:
                self._on_coordinator_change(coordinator_id)
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error in coordinator change callback: {e}")
    
    def _get_higher_priority_nodes(self):
        """
        Get list of nodes with higher priority than this node.
        
        Returns:
            list: Node IDs with priority > self.priority
        """
        with self._nodes_lock:
            return [nid for nid in self._known_nodes if nid > self.priority]
    
    def _get_lower_priority_nodes(self):
        """
        Get list of nodes with lower priority than this node.
        
        Returns:
            list: Node IDs with priority < self.priority
        """
        with self._nodes_lock:
            return [nid for nid in self._known_nodes if nid < self.priority]
    
    def get_coordinator(self):
        """
        Return the current coordinator node ID.
        
        Returns:
            int: Coordinator's node ID, or None if unknown
        """
        with self._coordinator_lock:
            return self._coordinator_id
    
    def is_coordinator(self):
        """
        Check if this node is the current coordinator.
        
        Returns:
            bool: True if this node is the coordinator
        """
        with self._coordinator_lock:
            return self._coordinator_id == self.node_id
    
    def is_election_in_progress(self):
        """
        Check if an election is currently in progress.
        
        Returns:
            bool: True if election is ongoing
        """
        with self._election_lock:
            return self._election_in_progress
    
    def set_coordinator(self, coordinator_id):
        """
        Manually set the coordinator (used during initialization).
        
        Args:
            coordinator_id: ID of the coordinator
        """
        with self._coordinator_lock:
            self._coordinator_id = coordinator_id
        logger.info(f"Node {self.node_id}: Coordinator set to Node {coordinator_id}")
