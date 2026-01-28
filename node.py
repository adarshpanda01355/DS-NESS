"""
node.py - Main entry point for a distributed energy trading node.

Responsibility:
    - Initialize all node components (multicast, unicast, heartbeat, election, ledger)
    - Coordinate message routing between components
    - Handle user commands (propose trades, check balance, etc.)
    - Manage node lifecycle (startup, shutdown, rejoin)
    - Main event loop for processing incoming messages
    - Maintain message buffers for causal ordering

Architecture:
    This file ORCHESTRATES the system - it does not contain low-level logic.
    Each component (multicast, unicast, election, etc.) handles its own concerns.
    
    Node
    ├── MulticastHandler  - Group communication
    ├── UnicastHandler    - Direct messaging
    ├── VectorClock       - Causal ordering
    ├── HeartbeatManager  - Fault detection
    ├── ElectionManager   - Leader election
    ├── EnergyLedger      - Credit tracking
    └── MessageBuffer     - Causal delivery queue

Usage:
    python node.py <node_id> [--nodes N1,N2,N3]
    
    Examples:
        python node.py 1
        python node.py 2 --nodes 1,3
        python node.py 3 --nodes 1,2
"""

import sys
import os
import argparse
import threading
import time
import logging
from collections import deque

from config import (
    MULTICAST_GROUP, MULTICAST_PORT, UNICAST_PORT_BASE,
    INITIAL_ENERGY_CREDITS, HEARTBEAT_INTERVAL, LOG_LEVEL
)
from multicast import MulticastHandler
from unicast import UnicastHandler
from message import (
    Message, MSG_HEARTBEAT, MSG_ELECTION, MSG_OK, MSG_COORDINATOR,
    MSG_JOIN, MSG_JOIN_RESPONSE, MSG_LEAVE, MSG_TRADE_REQUEST, MSG_TRADE_RESPONSE,
    MSG_TRADE_CONFIRM, MSG_LEDGER_SYNC, MSG_STATE_REQUEST, MSG_ACK, MSG_GOSSIP,
    create_join, create_join_response, create_leave, create_trade_request, create_trade_response,
    create_trade_confirm, create_ledger_sync, create_state_request, create_ack
)
import random
from config import GOSSIP_INTERVAL
from vector_clock import VectorClock
from heartbeat import HeartbeatManager
from election import ElectionManager
from ledger import EnergyLedger

# Configure logging with clear format for testing
# Format: TIME [LEVEL] component: Node X: message
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)-5s] %(name)-10s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('node')


class MessageBuffer:
    """
    Buffer for messages awaiting causal delivery.
    
    Messages that arrive out of order (failing causal delivery check)
    are stored here until their dependencies arrive.
    
    Periodically, the buffer is checked to see if any messages
    can now be delivered.
    """
    
    def __init__(self):
        """Initialize empty message buffer."""
        self._buffer = deque()
        self._lock = threading.Lock()
    
    def add(self, message, addr):
        """
        Add a message to the buffer.
        
        Args:
            message: Message object
            addr: Sender address tuple
        """
        with self._lock:
            self._buffer.append((message, addr))
    
    def get_deliverable(self, can_deliver_func):
        """
        Get all messages that can now be delivered.
        
        Args:
            can_deliver_func: Function to check if message can be delivered
                             Signature: can_deliver_func(sender_id, vector_clock) -> bool
        
        Returns:
            list: List of (message, addr) tuples that can be delivered
        """
        deliverable = []
        remaining = deque()
        
        with self._lock:
            while self._buffer:
                msg, addr = self._buffer.popleft()
                if can_deliver_func(msg.sender_id, msg.vector_clock):
                    deliverable.append((msg, addr))
                else:
                    remaining.append((msg, addr))
            
            self._buffer = remaining
        
        return deliverable
    
    def size(self):
        """Return number of buffered messages."""
        with self._lock:
            return len(self._buffer)


class Node:
    """
    Represents a node in the distributed energy trading system.
    
    This is the main orchestrator that:
    - Initializes and connects all components
    - Routes incoming messages to appropriate handlers
    - Provides user interface for commands
    - Manages the node lifecycle
    """
    
    def __init__(self, node_id, known_nodes=None, host=None, peer_addresses=None):
        """
        Initialize the node with all components.
        
        Args:
            node_id: Unique identifier for this node (also used as priority)
            known_nodes: Optional list of other node IDs in the system
            host: Optional IP address to bind to (for multi-device deployment)
            peer_addresses: Optional dict mapping node_id -> IP for multi-device
        """
        self.node_id = node_id
        self.priority = node_id  # Priority = node_id for Bully algorithm
        
        # Multi-device configuration
        self._host = host
        self._peer_addresses = peer_addresses or {}
        
        # Track known nodes in the system
        self._known_nodes = set(known_nodes) if known_nodes else set()
        self._known_nodes.add(node_id)  # Include self
        self._nodes_lock = threading.Lock()
        
        # Running state
        self._running = False
        
        # Initialize components
        self._init_components()
        
        # Message buffer for causal delivery
        self._message_buffer = MessageBuffer()
        
        # Buffer check thread
        self._buffer_thread = None

        # Global ledger registry: node_id -> ledger state
        # Only used by coordinator, but always present for code simplicity
        self._global_ledger_registry = {}
        # Initialize with own state
        self._global_ledger_registry[self.node_id] = self.ledger.get_state()

        # Recent message deduplication (msg_id -> timestamp)
        self._recent_msg_ids = {}
        self._recent_lock = threading.Lock()
        # Deduplication cleanup thread control
        self._dedup_thread = None
        
        logger.info(f"Node {self.node_id}: Initialized with priority {self.priority}")
        logger.info(f"Node {self.node_id}: Known nodes: {self._known_nodes}")
    
    def _init_components(self):
        """
        Initialize all node components.
        
        Order matters - some components depend on others.
        """
        # 1. Vector Clock - needed by most components for message ordering
        self.vector_clock = VectorClock(self.node_id)
        logger.debug(f"Node {self.node_id}: Vector clock initialized")
        
        # 2. Communication handlers
        self.multicast = MulticastHandler(
            group=MULTICAST_GROUP,
            port=MULTICAST_PORT,
            node_id=self.node_id
        )
        
        self.unicast = UnicastHandler(
            node_id=self.node_id,
            host=self._host,
            peer_addresses=self._peer_addresses
        )
        logger.debug(f"Node {self.node_id}: Communication handlers initialized")
        
        # 3. Energy ledger
        self.ledger = EnergyLedger(
            node_id=self.node_id,
            initial_credits=INITIAL_ENERGY_CREDITS
        )
        logger.debug(f"Node {self.node_id}: Ledger initialized")
        
        # 4. Election manager
        self.election = ElectionManager(
            node_id=self.node_id,
            unicast_handler=self.unicast,
            multicast_handler=self.multicast,
            vector_clock=self.vector_clock,
            known_nodes=self._known_nodes,
            on_coordinator_change=self._on_coordinator_change
        )
        logger.debug(f"Node {self.node_id}: Election manager initialized")
        
        # 5. Heartbeat manager - needs election for coordinator info
        self.heartbeat = HeartbeatManager(
            node_id=self.node_id,
            multicast_handler=self.multicast,
            vector_clock=self.vector_clock,
            on_node_failure=self._on_node_failure,
            on_leader_failure=self._on_leader_failure,
            get_coordinator=self.election.get_coordinator
        )
        logger.debug(f"Node {self.node_id}: Heartbeat manager initialized")
    
    def start(self):
        """
        Start the node and all its components.
        
        Startup sequence:
        1. Start message listeners (multicast, unicast)
        2. Start heartbeat manager
        3. Announce join to other nodes
        4. Perform dynamic discovery / trigger election
        5. Start command loop
        """
        logger.info(f"Node {self.node_id}: ========== STARTING NODE ==========")
        self._running = True
        # 1. Start message listeners
        self.multicast.start_receiving(self._on_multicast_message)
        self.unicast.start_receiving(self._on_unicast_message)
        logger.info(f"Node {self.node_id}: Message listeners started")
        # 2. Start heartbeat manager
        self.heartbeat.set_energy_credits(self.ledger.get_balance())
        self.heartbeat.start()
        # Add known nodes to heartbeat tracking
        for nid in self._known_nodes:
            if nid != self.node_id:
                self.heartbeat.add_node(nid)
        # 3. Start buffer check thread
        self._buffer_thread = threading.Thread(
            target=self._check_buffer_loop,
            name=f"Buffer-Check-{self.node_id}",
            daemon=True
        )
        self._buffer_thread.start()
        # 3b. Start gossip thread for anti-entropy
        self._gossip_thread = threading.Thread(
            target=self._gossip_loop,
            name=f"Gossip-{self.node_id}",
            daemon=True
        )
        self._gossip_thread.start()
        # Start dedup cleanup thread
        self._dedup_thread = threading.Thread(
            target=self._dedup_cleanup_loop,
            name=f"Dedup-Cleanup-{self.node_id}",
            daemon=True
        )
        self._dedup_thread.start()
        # 4. Announce join
        self._announce_join()
        # 5. Dynamic discovery - wait a bit then check if election needed
        time.sleep(HEARTBEAT_INTERVAL)
        if self.election.get_coordinator() is None:
            logger.info(f"Node {self.node_id}: No coordinator known, starting election")
            self.election.start_election()
        # If not coordinator, request state sync from coordinator
        coordinator_id = self.election.get_coordinator()
        if coordinator_id is not None and coordinator_id != self.node_id:
            from message import create_ledger_sync
            clock = self.vector_clock.increment()
            # Send a LEDGER_SYNC request to coordinator (could be a custom message, here we just send a request)
            # For simplicity, coordinator will send LEDGER_SYNC on join anyway
            logger.info(f"Node {self.node_id}: Requesting state sync from coordinator Node {coordinator_id}")
            # No explicit request message, coordinator will send LEDGER_SYNC on join
        # If coordinator, send LEDGER_SYNC to all nodes (except self)
        if self.election.is_coordinator():
            from message import create_ledger_sync
            clock = self.vector_clock.increment()
            # Send each node its own state from the registry
            with self._nodes_lock:
                for nid in self._known_nodes:
                    if nid != self.node_id:
                        state = self._global_ledger_registry.get(nid, self.ledger.get_state())
                        ledger_sync_msg = create_ledger_sync(self.node_id, clock, state)
                        sent = self.send_with_ack_retry(ledger_sync_msg, nid, attempts=5, timeout=1.5)
                        if not sent:
                            logger.warning(f"Node {self.node_id}: LEDGER_SYNC to Node {nid} not ACKed")
            logger.info(f"Node {self.node_id}: Sent per-node LEDGER_SYNC to all nodes after join")
        # 6. Start command loop (blocking)
        logger.info(f"Node {self.node_id}: Node is ready")
        self._command_loop()
    
    def stop(self):
        """
        Gracefully shutdown the node.
        
        Shutdown sequence:
        1. Announce leave
        2. Stop heartbeat
        3. Stop message listeners
        4. Close sockets
        """
        logger.info(f"Node {self.node_id}: ========== STOPPING NODE ==========")
        self._running = False
        
        # 1. Announce leave to other nodes
        self._announce_leave()
        time.sleep(0.5)  # Give time for message to send
        
        # 2. Stop heartbeat
        self.heartbeat.stop()
        
        # 3. Stop message listeners
        self.multicast.stop_receiving()
        self.unicast.stop_receiving()
        # Stop gossip thread
        try:
            if hasattr(self, '_gossip_thread') and self._gossip_thread.is_alive():
                # thread checks self._running and will exit
                self._gossip_thread.join(timeout=1.0)
        except Exception:
            pass
        
        # 4. Close sockets
        self.multicast.close()
        self.unicast.close()
        
        logger.info(f"Node {self.node_id}: Node stopped")
    
    def _announce_join(self):
        """
        Broadcast JOIN message to announce this node's presence.
        Uses reliable multicast (3 sends) to ensure delivery.
        """
        clock = self.vector_clock.increment()
        join_msg = create_join(self.node_id, clock)
        self.multicast.send_reliable(join_msg)  # Critical: use reliable send
        logger.info(f"Node {self.node_id}: JOIN announced (reliable)")
    
    def _announce_leave(self):
        """
        Broadcast LEAVE message before shutting down.
        """
        clock = self.vector_clock.increment()
        # Before leaving, send our latest ledger state to the coordinator
        try:
            coordinator_id = self.election.get_coordinator()
            from message import create_ledger_sync, create_leave
            ledger_state = self.ledger.get_state()
            if coordinator_id is not None and coordinator_id != self.node_id:
                state_msg = create_ledger_sync(self.node_id, clock, ledger_state)
                # send reliably so coordinator registry is up-to-date and wait for ACK
                sent = self.send_with_ack_retry(state_msg, coordinator_id, attempts=5, timeout=1.5)
                if sent:
                    logger.debug(f"Node {self.node_id}: Sent ledger state to coordinator {coordinator_id} before leave (ACK received)")
                else:
                    logger.warning(f"Node {self.node_id}: Sent ledger state to coordinator {coordinator_id} before leave (no ACK)")
        except Exception as e:
            logger.error(f"Node {self.node_id}: Error sending state to coordinator on leave: {e}")

        leave_msg = create_leave(self.node_id, clock)
        self.multicast.send(leave_msg)
        logger.info(f"Node {self.node_id}: LEAVE announced")
    
    # ==========================================================================
    # Message Receiving and Routing
    # ==========================================================================
    
    def _on_multicast_message(self, data, addr):
        """
        Handle incoming multicast message.
        
        Called by MulticastHandler when a message is received.
        Routes to appropriate handler based on message type.
        
        Args:
            data: Raw bytes received
            addr: Sender address (ip, port) - used for dynamic peer discovery
        """
        try:
            message = Message.from_bytes(data)
            
            # Ignore our own messages (we receive them due to loopback)
            if message.sender_id == self.node_id:
                return

            # Deduplicate messages that include an explicit msg_id
            try:
                mid = message.payload.get('msg_id')
                if mid and self._is_duplicate(mid):
                    logger.debug(f"Node {self.node_id}: Ignoring duplicate multicast msg_id={mid}")
                    return
            except Exception:
                pass
            
            # Dynamic peer discovery: learn sender's IP from multicast message
            # This enables unicast communication without pre-configuring --peers
            sender_ip = addr[0]
            self.unicast.register_peer(message.sender_id, sender_ip)
            
            logger.debug(f"Node {self.node_id}: Multicast received: {message}")
            
            # Heartbeats don't participate in causal ordering - handle immediately
            # without updating vector clock. See ARCHITECTURAL_AUDIT.md Section 3.
            if message.message_type == MSG_HEARTBEAT:
                self._handle_heartbeat(message)
                return
            
            # Check causal delivery for application messages
            if message.message_type in [MSG_TRADE_REQUEST, MSG_TRADE_CONFIRM]:
                if not self.vector_clock.can_deliver(message.sender_id, message.vector_clock):
                    logger.debug(f"Node {self.node_id}: Buffering message for causal delivery")
                    self._message_buffer.add(message, addr)
                    return
            
            # Update vector clock for non-heartbeat messages
            self.vector_clock.update(message.vector_clock)
            
            # Route to handler
            self._handle_message(message, addr)
            
        except Exception as e:
            logger.error(f"Node {self.node_id}: Error processing multicast: {e}")
    
    def _on_unicast_message(self, data, addr):
        """
        Handle incoming unicast message.
        
        Called by UnicastHandler when a message is received.
        Routes to appropriate handler based on message type.
        
        Args:
            data: Raw bytes received
            addr: Sender address (ip, port)
        """
        try:
            message = Message.from_bytes(data)
            
            # Dynamic peer discovery: learn sender's IP from unicast message
            sender_ip = addr[0]
            self.unicast.register_peer(message.sender_id, sender_ip)
            
            logger.debug(f"Node {self.node_id}: Unicast received: {message}")

            # Deduplicate messages that include an explicit msg_id
            try:
                mid = message.payload.get('msg_id')
                if mid and self._is_duplicate(mid):
                    logger.debug(f"Node {self.node_id}: Ignoring duplicate unicast msg_id={mid}")
                    return
            except Exception:
                pass
            
            # Update vector clock
            self.vector_clock.update(message.vector_clock)
            
            # Route to handler
            self._handle_message(message, addr)
            
        except Exception as e:
            logger.error(f"Node {self.node_id}: Error processing unicast: {e}")
    
    def _handle_message(self, message, addr):
        """
        Route message to appropriate handler based on type.
        
        Args:
            message: Parsed Message object
            addr: Sender address
        """
        msg_type = message.message_type
        
        # Heartbeat messages
        if msg_type == MSG_HEARTBEAT:
            self._handle_heartbeat(message)
        
        # Election messages
        elif msg_type == MSG_ELECTION:
            self._handle_election(message)
        elif msg_type == MSG_OK:
            self._handle_ok(message)
        elif msg_type == MSG_COORDINATOR:
            self._handle_coordinator(message)
        
        # Membership messages
        elif msg_type == MSG_JOIN:
            self._handle_join(message)
        elif msg_type == MSG_JOIN_RESPONSE:
            self._handle_join_response(message)
        elif msg_type == MSG_LEAVE:
            self._handle_leave(message)
        
        # Trade messages
        elif msg_type == MSG_TRADE_REQUEST:
            self._handle_trade_request(message)
        elif msg_type == MSG_TRADE_RESPONSE:
            self._handle_trade_response(message)
        elif msg_type == MSG_TRADE_CONFIRM:
            self._handle_trade_confirm(message)
        
        # Ledger sync
        elif msg_type == MSG_LEDGER_SYNC:
            self._handle_ledger_sync(message)
        elif msg_type == MSG_STATE_REQUEST:
            self._handle_state_request(message)
        elif msg_type == MSG_ACK:
            self._handle_ack(message)
        elif msg_type == MSG_GOSSIP:
            self._handle_gossip(message)
        
        else:
            logger.warning(f"Node {self.node_id}: Unknown message type: {msg_type}")
    
    # ==========================================================================
    # Message Handlers
    # ==========================================================================
    
    def _handle_heartbeat(self, message):
        """
        Handle HEARTBEAT message.
        
        Record the heartbeat and update leader ACK if from coordinator.
        """
        self.heartbeat.record_heartbeat(message.sender_id)
        
        # If from coordinator, record as leader ACK
        if message.sender_id == self.election.get_coordinator():
            self.heartbeat.record_leader_ack()
    
    def _handle_election(self, message):
        """Handle ELECTION message - delegate to ElectionManager."""
        self.election.handle_election_message(
            message.sender_id,
            message.sender_priority
        )
    
    def _handle_ok(self, message):
        """Handle OK message - delegate to ElectionManager."""
        self.election.handle_ok_message(
            message.sender_id,
            message.sender_priority
        )
    
    def _handle_coordinator(self, message):
        """Handle COORDINATOR message - delegate to ElectionManager."""
        self.election.handle_coordinator_message(
            message.sender_id,
            message.sender_priority
        )
    
    def _handle_join(self, message):
        """
        Handle JOIN message - a new node has joined.
        
        Add the node to tracking systems and respond if we're coordinator.
        If coordinator, send JOIN_RESPONSE with current vector clock state
        so the new node can properly participate in causal ordering.
        """
        new_node_id = message.sender_id
        logger.info(f"Node {self.node_id}: Node {new_node_id} has joined")
        
        # Add to known nodes
        with self._nodes_lock:
            self._known_nodes.add(new_node_id)
        
        # Add to component tracking
        self.vector_clock.add_node(new_node_id)
        self.heartbeat.add_node(new_node_id)
        self.election.add_node(new_node_id)
        
        # If we're coordinator, send state to new node and announce ourselves
        if self.election.is_coordinator():
            # Get current state to send to new node
            clock = self.vector_clock.increment()
            current_clock_state = self.vector_clock.get_clock()
            # Get the rejoining node's state from the registry, or default to initial state
            ledger_state = self._global_ledger_registry.get(new_node_id, {
                'balance': INITIAL_ENERGY_CREDITS,
                'transaction_history': [],
                'pending_trades': {},
                'node_id': new_node_id
            })
            with self._nodes_lock:
                known_nodes_list = list(self._known_nodes)
            # Create and send JOIN_RESPONSE with state (reliable unicast)
            from message import create_join_response, create_ledger_sync, create_coordinator
            join_response = create_join_response(
                sender_id=self.node_id,
                vector_clock=clock,
                coordinator_id=self.node_id,
                known_nodes=known_nodes_list
            )
            # Add ledger_state to payload
            join_response.payload["ledger_state"] = ledger_state
            sent = self.send_with_ack_retry(join_response, new_node_id, attempts=5, timeout=1.5)
            if sent:
                logger.info(f"Node {self.node_id}: Sent JOIN_RESPONSE to Node {new_node_id} with state (ACK received)")
            else:
                logger.warning(f"Node {self.node_id}: Sent JOIN_RESPONSE to Node {new_node_id} (no ACK)")
            # Also send LEDGER_SYNC for redundancy (reliable unicast) and wait for ACK
            ledger_sync_msg = create_ledger_sync(self.node_id, clock, ledger_state)
            sent2 = self.send_with_ack_retry(ledger_sync_msg, new_node_id, attempts=5, timeout=1.5)
            if not sent2:
                logger.warning(f"Node {self.node_id}: LEDGER_SYNC to Node {new_node_id} not ACKed")
            # Also announce ourselves as coordinator (reliable multicast)
            coord_msg = create_coordinator(self.node_id, self.priority, clock)
            self.multicast.send_reliable(coord_msg)  # Critical: use reliable send
    
    def _handle_leave(self, message):
        """
        Handle LEAVE message - a node is leaving gracefully.
        
        Remove the node from tracking systems.
        """
        leaving_node_id = message.sender_id
        logger.info(f"Node {self.node_id}: Node {leaving_node_id} is leaving")
        
        # Remove from known nodes
        with self._nodes_lock:
            self._known_nodes.discard(leaving_node_id)
        
        # Remove from component tracking
        self.vector_clock.remove_node(leaving_node_id)
        self.heartbeat.remove_node(leaving_node_id)
        self.election.remove_node(leaving_node_id)
        
        # If leaving node was coordinator, start election
        if leaving_node_id == self.election.get_coordinator():
            logger.info(f"Node {self.node_id}: Coordinator left, starting election")
            self.election.start_election()
    
    def _handle_join_response(self, message):
        """
        Handle JOIN_RESPONSE message from coordinator.
        
        This message contains critical state for the new node:
        - Vector clock state: Allows proper causal ordering of messages
        - Coordinator ID: Know who the leader is
        - Known nodes: List of all participants
        
        See ARCHITECTURAL_AUDIT.md Section 1 for why this is needed.
        """
        payload = message.payload
        coordinator_id = payload.get("coordinator_id")
        known_nodes = payload.get("known_nodes", [])
        clock_state = payload.get("clock_state", {})
        ledger_state = payload.get("ledger_state")
        logger.info(f"Node {self.node_id}: Received JOIN_RESPONSE from coordinator Node {coordinator_id}")
        # Update our vector clock from coordinator's state
        if clock_state:
            self.vector_clock.update(clock_state)
            logger.info(f"Node {self.node_id}: Vector clock synchronized from coordinator")
        # Update coordinator reference
        self.election.set_coordinator(coordinator_id)
        # Add all known nodes to our tracking
        for node_id in known_nodes:
            if node_id != self.node_id:
                with self._nodes_lock:
                    self._known_nodes.add(node_id)
                self.vector_clock.add_node(node_id)
                self.heartbeat.add_node(node_id)
                self.election.add_node(node_id)
        # Sync ledger state if provided
        if ledger_state:
            # Always apply the received state, regardless of node_id
            self.ledger.sync_from_state(ledger_state)
            # No local persistence in this configuration
            logger.info(f"Node {self.node_id}: Ledger state synchronized from coordinator")
            # ACK the join response / ledger sync to coordinator if msg_id present
            try:
                msg_id = payload.get("msg_id")
                if msg_id and coordinator_id is not None:
                    from message import create_ack
                    ack_clock = self.vector_clock.increment()
                    ack = create_ack(self.node_id, ack_clock, msg_id)
                    self.unicast.send(ack, coordinator_id)
                    logger.debug(f"Node {self.node_id}: Sent ACK for JOIN_RESPONSE msg_id={msg_id} to coordinator {coordinator_id}")
            except Exception:
                pass
        logger.info(f"Node {self.node_id}: State synchronized - "
                   f"coordinator={coordinator_id}, known_nodes={sorted(known_nodes)}")
    
    def _handle_trade_request(self, message):
        """
        Handle TRADE_REQUEST message.
        
        Another node wants to trade energy credits with us.
        """
        payload = message.payload
        trade_id = payload.get("trade_id")
        amount = payload.get("amount")
        trade_type = payload.get("trade_type")  # From sender's perspective
        sender_id = message.sender_id
        
        logger.info(f"Node {self.node_id}: Trade request from Node {sender_id}: "
                   f"{trade_type} {amount} credits (trade_id={trade_id})")

        # Deduplicate: ignore if we already know about this trade (pending or completed)
        try:
            if self.ledger.has_trade(trade_id):
                logger.debug(f"Node {self.node_id}: Ignoring duplicate trade request {trade_id} from Node {sender_id}")
                return
        except Exception:
            # In case ledger lacks the method for some reason, continue normally
            pass
        
        # If sender wants to SELL, we are the BUYER
        # If sender wants to BUY, we are the SELLER
        accepted = False
        reason = None
        
        if trade_type == "sell":
            # Sender is selling to us - we're buying
            # Always accept buys (we receive credits)
            accepted = True
            # Add to pending trades
            self.ledger.add_pending_trade(trade_id, "buy", amount, sender_id)
            self._update_ledger_registry()
        elif trade_type == "buy":
            # Sender wants to buy from us - we're selling
            if self.ledger.can_sell(amount):
                accepted = True
                self.ledger.add_pending_trade(trade_id, "sell", amount, sender_id)
                self._update_ledger_registry()
            else:
                reason = "Insufficient credits"
        
        # Send response
        clock = self.vector_clock.increment()
        response = create_trade_response(self.node_id, clock, trade_id, accepted, reason)
        self.unicast.send(response, sender_id)
        
        logger.info(f"Node {self.node_id}: Trade {'ACCEPTED' if accepted else 'REJECTED'} "
                   f"(trade_id={trade_id})")
    
    def _handle_trade_response(self, message):
        """
        Handle TRADE_RESPONSE message.
        
        Response to our energy trade request.
        """
        payload = message.payload
        trade_id = payload.get("trade_id")
        accepted = payload.get("accepted")
        reason = payload.get("reason")
        sender_id = message.sender_id
        
        if accepted:
            logger.info(f"Node {self.node_id}: Energy trade {trade_id} ACCEPTED by Node {sender_id}")
            
            # Get trade details for confirmation message
            trade = self.ledger.get_pending_trade(trade_id)
            if trade:
                if trade['trade_type'] == 'sell':
                    seller_id = self.node_id
                    buyer_id = trade['counterparty_id']
                else:  # buy
                    buyer_id = self.node_id
                    seller_id = trade['counterparty_id']
                amount = trade['amount']
            else:
                buyer_id = seller_id = amount = None
            
            # Execute our side of the trade
            self.ledger.execute_pending_trade(trade_id, self.vector_clock.get_clock())
            self._update_ledger_registry()
            
            # No local persistence in this configuration
            
            # Send confirmation (reliable to prevent lost credits)
            clock = self.vector_clock.increment()
            confirm = create_trade_confirm(self.node_id, clock, trade_id, success=True, buyer_id=buyer_id, seller_id=seller_id, amount=amount)
            self.unicast.send_with_retry(confirm, sender_id, retries=5)  # Critical: trade must be confirmed
            
            # Update heartbeat with new balance
            self.heartbeat.set_energy_credits(self.ledger.get_balance())
        else:
            logger.info(f"Node {self.node_id}: Energy trade {trade_id} REJECTED by Node {sender_id}: {reason}")
            self.ledger.remove_pending_trade(trade_id)
            self._update_ledger_registry()
    
    def _handle_trade_confirm(self, message):
        """
        Handle TRADE_CONFIRM message.
        
        Energy trade has been confirmed - execute our side.
        """
        payload = message.payload
        trade_id = payload.get("trade_id")
        success = payload.get("success")
        sender_id = message.sender_id
        
        if success:
            logger.info(f"Node {self.node_id}: Energy trade {trade_id} CONFIRMED by Node {sender_id}")
            
            if self.election.is_coordinator():
                # Broadcast the confirm to all nodes so they can execute
                self.multicast.send(message)
                
                # Update the global ledger registry with new balances
                buyer_id = payload.get("buyer_id")
                seller_id = payload.get("seller_id")
                amount = payload.get("amount")
                if buyer_id and seller_id and amount is not None:
                    if buyer_id in self._global_ledger_registry:
                        self._global_ledger_registry[buyer_id]['balance'] += amount
                    if seller_id in self._global_ledger_registry:
                        self._global_ledger_registry[seller_id]['balance'] -= amount
            
            # Execute our side of the trade
            self.ledger.execute_pending_trade(trade_id, self.vector_clock.get_clock())
            self._update_ledger_registry()
            
            # No local persistence in this configuration
            
            # Update heartbeat with new balance
            self.heartbeat.set_energy_credits(self.ledger.get_balance())
            
            # If not coordinator, send updated state to coordinator for registry
            if not self.election.is_coordinator():
                coordinator_id = self.election.get_coordinator()
                if coordinator_id is not None:
                    clock = self.vector_clock.increment()
                    state_msg = create_ledger_sync(self.node_id, clock, self.ledger.get_state())
                    sent = self.send_with_ack_retry(state_msg, coordinator_id, attempts=5, timeout=1.5)
                    if not sent:
                        logger.warning(f"Node {self.node_id}: LEDGER_SYNC to coordinator {coordinator_id} not ACKed")
        else:
            logger.warning(f"Node {self.node_id}: Energy trade {trade_id} FAILED")
            self.ledger.remove_pending_trade(trade_id)
            self._update_ledger_registry()
    
    def _handle_ledger_sync(self, message):
        """
        Handle LEDGER_SYNC message.
        
        If from coordinator, sync our state.
        If from follower, update registry (if we're coordinator).
        """
        sender_id = message.sender_id
        coordinator_id = self.election.get_coordinator()
        
        if sender_id == coordinator_id:
            # Sync from coordinator
            ledger_state = message.payload.get("ledger_state")
            if ledger_state:
                # Always apply the received state, regardless of node_id
                self.ledger.sync_from_state(ledger_state)
                logger.info(f"Node {self.node_id}: Ledger state synchronized from coordinator (LEDGER_SYNC)")
            else:
                logger.debug(f"Node {self.node_id}: Received LEDGER_SYNC from coordinator (no state)")
        elif self.election.is_coordinator():
            # Update registry with follower's state
            ledger_state = message.payload.get("ledger_state")
            if ledger_state:
                self._global_ledger_registry[sender_id] = ledger_state
                logger.debug(f"Node {self.node_id}: Updated registry for Node {sender_id}")
        else:
            logger.debug(f"Node {self.node_id}: Ignoring LEDGER_SYNC from Node {sender_id}")

        # If coordinator, update the registry with the latest state for this node
        if self.election.is_coordinator() and sender_id == coordinator_id:
            ledger_state = message.payload.get("ledger_state")
            if ledger_state:
                node_id = ledger_state.get("node_id")
                if node_id is not None:
                    self._global_ledger_registry[node_id] = ledger_state
        # Send ACK for LEDGER_SYNC messages that include a msg_id
        msg_id = message.payload.get("msg_id")
        if msg_id:
            try:
                from message import create_ack
                ack_clock = self.vector_clock.increment()
                ack = create_ack(self.node_id, ack_clock, msg_id)
                # use simple send for ACK (no need to wait)
                self.unicast.send(ack, sender_id)
                logger.debug(f"Node {self.node_id}: Sent ACK for msg_id={msg_id} to Node {sender_id}")
            except Exception as e:
                logger.error(f"Node {self.node_id}: Failed to send ACK for LEDGER_SYNC: {e}")

    def _handle_state_request(self, message):
        """
        Handle STATE_REQUEST message from a coordinator who wants our ledger state.

        Respond with a LEDGER_SYNC (our current ledger state) via reliable unicast.
        """
        requester_id = message.sender_id
        logger.info(f"Node {self.node_id}: Received STATE_REQUEST from Node {requester_id}")
        try:
            clock = self.vector_clock.increment()
            ledger_state = self.ledger.get_state()
            state_msg = create_ledger_sync(self.node_id, clock, ledger_state)
            # send reliably back to requester (new coordinator) and wait for ACK
            sent = self.send_with_ack_retry(state_msg, requester_id, attempts=5, timeout=1.5)
            if sent:
                logger.debug(f"Node {self.node_id}: Sent LEDGER_SYNC to Node {requester_id} in response to STATE_REQUEST and received ACK")
            else:
                logger.warning(f"Node {self.node_id}: Sent LEDGER_SYNC to Node {requester_id} but did not receive ACK")
        except Exception as e:
            logger.error(f"Node {self.node_id}: Failed to respond to STATE_REQUEST from {requester_id}: {e}")

    def _handle_gossip(self, message):
        """
        Handle incoming GOSSIP message: update local view of sender's ledger state.
        This provides anti-entropy to help rebuild registry after crashes/partitions.
        """
        sender_id = message.sender_id
        ledger_state = message.payload.get("ledger_state")
        if ledger_state:
            # Update local registry copy (used by coordinator and helpful for followers)
            try:
                self._global_ledger_registry[sender_id] = ledger_state
                logger.debug(f"Node {self.node_id}: GOSSIP updated registry entry for Node {sender_id}")
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error applying gossip from {sender_id}: {e}")

    def _handle_ack(self, message):
        """
        Handle ACK message by unblocking any waiting sender.
        """
        payload = message.payload
        msg_id = payload.get("msg_id")
        if not msg_id:
            logger.debug(f"Node {self.node_id}: Received ACK with no msg_id from Node {message.sender_id}")
            return
        # Notify Unicast handler that an ACK arrived
        try:
            self.unicast.acknowledge(msg_id)
            logger.debug(f"Node {self.node_id}: Processed ACK for msg_id={msg_id} from Node {message.sender_id}")
        except Exception as e:
            logger.error(f"Node {self.node_id}: Error processing ACK for {msg_id}: {e}")

    def _is_duplicate(self, msg_id, window_seconds=30):
        """
        Check and record a message id for deduplication.
        Returns True if the msg_id was seen recently.
        """
        now = time.time()
        with self._recent_lock:
            if msg_id in self._recent_msg_ids:
                return True
            self._recent_msg_ids[msg_id] = now
        return False

    def _dedup_cleanup_loop(self, purge_interval=5, ttl=30):
        """Background thread: purge old msg_ids periodically."""
        while self._running:
            try:
                cutoff = time.time() - ttl
                with self._recent_lock:
                    to_remove = [mid for mid, ts in self._recent_msg_ids.items() if ts < cutoff]
                    for mid in to_remove:
                        self._recent_msg_ids.pop(mid, None)
                time.sleep(purge_interval)
            except Exception:
                time.sleep(purge_interval)

    def send_with_ack_retry(self, message, target_node_id, attempts=3, timeout=1.5):
        """
        Send a message and wait for an ACK, retrying up to `attempts` times.

        Uses `message.payload['msg_id']` as the message identifier.
        """
        msg_id = None
        if isinstance(message, Message):
            msg_id = message.payload.get('msg_id')
        if not msg_id:
            # Fallback: generate a temporary msg_id
            msg_id = f"msg-{self.node_id}-{target_node_id}-{time.time()}"
            if isinstance(message, Message):
                message.payload['msg_id'] = msg_id

        for attempt in range(attempts):
            try:
                ok = self.unicast.send_and_wait_ack(message, target_node_id, timeout=timeout, message_id=msg_id)
                if ok:
                    return True
            except Exception as e:
                logger.error(f"Node {self.node_id}: send_with_ack_retry error to Node {target_node_id}: {e}")
            # small backoff
            time.sleep(0.2)

        return False

    def _update_ledger_registry(self):
        """
        Update the global ledger registry with this node's current state.
        Should be called after any trade or ledger change.
        Only does work if this node is the coordinator.
        """
        if hasattr(self, 'election') and self.election.is_coordinator():
            self._global_ledger_registry[self.node_id] = self.ledger.get_state()
    
    # ==========================================================================
    # Callbacks
    # ==========================================================================
    
    def _on_coordinator_change(self, new_coordinator_id):
        """
        Callback when coordinator changes.
        
        Args:
            new_coordinator_id: ID of the new coordinator
        """
        if new_coordinator_id == self.node_id:
            logger.info(f"Node {self.node_id}: I am now the COORDINATOR")
        else:
            logger.info(f"Node {self.node_id}: New coordinator is Node {new_coordinator_id}")
        # When coordinator changes, proactively send our current ledger state
        # to the new coordinator so it can rebuild its registry reliably.
        try:
            if new_coordinator_id is not None and new_coordinator_id != self.node_id:
                from message import create_ledger_sync
                clock = self.vector_clock.increment()
                ledger_state = self.ledger.get_state()
                msg = create_ledger_sync(self.node_id, clock, ledger_state)
                # send reliably to new coordinator so registry gets updated and wait for ACK
                sent = self.send_with_ack_retry(msg, new_coordinator_id, attempts=5, timeout=1.5)
                if sent:
                    logger.debug(f"Node {self.node_id}: Sent ledger state to new coordinator Node {new_coordinator_id} (ACK received)")
                else:
                    logger.warning(f"Node {self.node_id}: Sent ledger state to new coordinator Node {new_coordinator_id} (no ACK)")
        except Exception as e:
            logger.error(f"Node {self.node_id}: Failed to send state to new coordinator: {e}")

        # If *we* became the coordinator, actively poll peers for their ledger state
        if new_coordinator_id == self.node_id:
            try:
                self._bootstrap_registry()
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error during registry bootstrap: {e}")

    def _bootstrap_registry(self, timeout_per_request=0.5):
        """
        When this node becomes coordinator, poll all known peers for their ledger state
        so we can rebuild `_global_ledger_registry`.

        Sends a `STATE_REQUEST` to each known node (except self). Followers reply
        with a `LEDGER_SYNC` which is handled by `_handle_ledger_sync` to populate
        the registry.
        """
        logger.info(f"Node {self.node_id}: Bootstrapping ledger registry from peers")
        # Reset registry and add our own state
        self._global_ledger_registry = {}
        self._global_ledger_registry[self.node_id] = self.ledger.get_state()

        with self._nodes_lock:
            peers = [nid for nid in self._known_nodes if nid != self.node_id]

        # Send state requests to peers
        for nid in peers:
            try:
                clock = self.vector_clock.increment()
                req = create_state_request(self.node_id, clock)
                # best-effort reliable send; peers will respond with LEDGER_SYNC
                sent = self.unicast.send_with_retry(req, nid, retries=3)
                if not sent:
                    logger.warning(f"Node {self.node_id}: STATE_REQUEST to Node {nid} failed")
                else:
                    logger.debug(f"Node {self.node_id}: STATE_REQUEST sent to Node {nid}")
                # small pause to avoid flooding
                time.sleep(timeout_per_request)
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error requesting state from Node {nid}: {e}")

        logger.info(f"Node {self.node_id}: Registry bootstrap requests sent to peers")
    
    def _on_node_failure(self, failed_node_id):
        """
        Callback when a node is detected as failed.
        
        Args:
            failed_node_id: ID of the failed node
        """
        logger.warning(f"Node {self.node_id}: Node {failed_node_id} has FAILED")
        
        # Remove from tracking
        with self._nodes_lock:
            self._known_nodes.discard(failed_node_id)
        
        self.vector_clock.remove_node(failed_node_id)
        self.election.remove_node(failed_node_id)
    
    def _on_leader_failure(self):
        """
        Callback when leader failure is detected.
        
        Triggers a new election.
        """
        logger.warning(f"Node {self.node_id}: Leader failure detected, starting election")
        self.election.start_election()
    
    # ==========================================================================
    # Causal Delivery Buffer
    # ==========================================================================
    
    def _check_buffer_loop(self):
        """
        Background thread: periodically check buffer for deliverable messages.
        """
        while self._running:
            try:
                # Get messages that can now be delivered
                deliverable = self._message_buffer.get_deliverable(
                    self.vector_clock.can_deliver
                )
                
                for message, addr in deliverable:
                    logger.debug(f"Node {self.node_id}: Delivering buffered message from Node {message.sender_id}")
                    self.vector_clock.update(message.vector_clock)
                    self._handle_message(message, addr)
                
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error checking buffer: {e}")
            
            time.sleep(0.5)

    def _gossip_loop(self):
        """
        Periodically gossip this node's ledger state to a random peer.
        """
        while self._running:
            try:
                # Choose a random peer to gossip to
                with self._nodes_lock:
                    peers = [nid for nid in self._known_nodes if nid != self.node_id]

                if peers:
                    target = random.choice(peers)
                    clock = self.vector_clock.increment()
                    from message import create_gossip
                    state = self.ledger.get_state()
                    gossip_msg = create_gossip(self.node_id, clock, state)
                    # best-effort send (no ACK required)
                    self.unicast.send(gossip_msg, target)
                    logger.debug(f"Node {self.node_id}: GOSSIP sent to Node {target}")

                time.sleep(GOSSIP_INTERVAL)
            except Exception as e:
                logger.error(f"Node {self.node_id}: Error in gossip loop: {e}")
                time.sleep(GOSSIP_INTERVAL)
    
    # ==========================================================================
    # User Commands
    # ==========================================================================
    
    def propose_trade(self, target_node, amount, trade_type="sell"):
        """
        Initiate an energy trade request with another node.
        
        Args:
            target_node: ID of the node to trade with
            amount: Number of energy credits to trade
            trade_type: "sell" (we sell to them) or "buy" (we buy from them)
        """
        # Validate - cannot trade with self
        if target_node == self.node_id:
            print(f"Cannot trade with yourself")
            return
        
        # Validate - target node must exist in the system
        active_nodes = self.heartbeat.get_active_nodes()
        if target_node not in active_nodes:
            print(f"Node {target_node} is not available in the system. Active nodes: {sorted(active_nodes)}")
            return
        
        if trade_type == "sell" and not self.ledger.can_sell(amount):
            print(f"Cannot sell {amount} energy credits - insufficient balance")
            return
        
        # Create trade request
        clock = self.vector_clock.increment()
        trade_msg = create_trade_request(
            sender_id=self.node_id,
            vector_clock=clock,
            target_id=target_node,
            amount=amount,
            trade_type=trade_type
        )
        
        # Add to pending trades
        trade_id = trade_msg.payload["trade_id"]
        self.ledger.add_pending_trade(trade_id, trade_type, amount, target_node)
        
        # Send to target
        self.unicast.send(trade_msg, target_node)
        
        logger.info(f"Node {self.node_id}: Proposed energy trade to Node {target_node}: "
                   f"{trade_type} {amount} credits")
    
    def _command_loop(self):
        """
        Interactive command loop for user input.
        
        Commands:
            status - Show node status
            balance - Show current credits
            sell <node> <amount> - Sell credits to node
            buy <node> <amount> - Buy credits from node
            nodes - List known nodes
            history - Show transaction history
            election - Force start election
            quit - Shutdown node
        """
        print(f"\n{'='*50}")
        print(f"  ENERGY TRADING NODE {self.node_id}")
        print(f"  Type 'help' for commands")
        print(f"{'='*50}\n")
        
        while self._running:
            try:
                cmd = input(f"Node {self.node_id}> ").strip().lower()

                if not cmd:
                    continue

                parts = cmd.split()
                command = parts[0]

                if command == "help":
                    self._print_help()

                elif command == "status":
                    self._print_status()

                elif command == "balance":
                    print(f"Balance: {self.ledger.get_balance()} credits")

                elif command == "sell":
                    if len(parts) != 3:
                        print("Usage: sell <node_id> <amount>")
                    else:
                        target = int(parts[1])
                        amount = int(parts[2])
                        self.propose_trade(target, amount, "sell")

                elif command == "buy":
                    if len(parts) != 3:
                        print("Usage: buy <node_id> <amount>")
                    else:
                        target = int(parts[1])
                        amount = int(parts[2])
                        self.propose_trade(target, amount, "buy")

                elif command == "nodes":
                    self._print_nodes()

                elif command == "history":
                    self.ledger.print_status()

                elif command == "election":
                    print("Starting election...")
                    self.election.start_election()

                elif command in ["quit", "exit", "q"]:
                    print("Shutting down...")
                    self._running = False
                    break

                else:
                    print(f"Unknown command: {command}. Type 'help' for commands.")
                    
            except KeyboardInterrupt:
                print("\nUse 'quit' to exit gracefully")
            except EOFError:
                break
            except Exception as e:
                print(f"Error: {e}")
    
    def _print_help(self):
        """Print available commands."""
        print("""
Available Commands:
  help       - Show this help message
  status     - Show node status (coordinator, balance, etc.)
  balance    - Show current energy credits
  sell N A   - Sell A energy credits to node N
  buy N A    - Buy A energy credits from node N
  nodes      - List known nodes
  history    - Show energy transaction history
  election   - Force start a new leader election
  quit       - Shutdown node gracefully
        """)
    
    def _print_status(self):
        """Print current node status."""
        coordinator = self.election.get_coordinator()
        is_coordinator = self.election.is_coordinator()
        balance = self.ledger.get_balance()
        active_nodes = self.heartbeat.get_active_nodes()
        
        print(f"\n{'='*40}")
        print(f"  NODE STATUS - Node {self.node_id}")
        print(f"{'='*40}")
        print(f"  Role: {'COORDINATOR' if is_coordinator else 'FOLLOWER'}")
        print(f"  Coordinator: Node {coordinator}")
        print(f"  Balance: {balance} credits")
        print(f"  Active Nodes: {sorted(active_nodes)}")
        print(f"  Vector Clock: {self.vector_clock}")
        print(f"  Buffered Messages: {self._message_buffer.size()}")
        print(f"{'='*40}\n")
    
    def _print_nodes(self):
        """Print list of known nodes."""
        with self._nodes_lock:
            nodes = sorted(self._known_nodes)
        
        active = self.heartbeat.get_active_nodes()
        suspected = self.heartbeat.get_suspected_nodes()
        coordinator = self.election.get_coordinator()
        
        print(f"\nKnown Nodes:")
        for nid in nodes:
            status = []
            if nid == self.node_id:
                status.append("self")
            if nid == coordinator:
                status.append("coordinator")
            if nid in suspected:
                status.append("suspected")
            elif nid in active:
                status.append("active")
            
            status_str = f" ({', '.join(status)})" if status else ""
            print(f"  Node {nid}{status_str}")
        print()


def parse_args():
    """
    Parse command-line arguments.
    
    Returns:
        Namespace with parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Distributed Energy Trading Node"
    )
    
    parser.add_argument(
        "node_id",
        type=int,
        help="Unique node identifier (also used as priority)"
    )
    
    parser.add_argument(
        "--nodes",
        type=str,
        default="",
        help="Comma-separated list of other node IDs (e.g., 1,2,3)"
    )
    
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="IP address to bind this node to (for multi-device deployment)"
    )
    
    parser.add_argument(
        "--peers",
        type=str,
        default="",
        help="Node-to-IP mappings for multi-device (e.g., 1:192.168.1.10,2:192.168.1.11)"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress most log output (only show errors and user messages)"
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    # Set logging level based on flags
    if args.quiet:
        # Suppress console output but capture detailed logs to per-node files.
        # Keep root logger level at DEBUG so file handlers can capture debug logs,
        # but raise existing console/stream handlers to ERROR to silence terminal output.
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        for h in list(root.handlers):
            try:
                # Raise console/stream handlers to ERROR to quiet the terminal
                h.setLevel(logging.ERROR)
            except Exception:
                pass
        # Ensure logs directory exists
        log_dir = os.path.join(os.getcwd(), 'logs')
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass
        # Per-node log file path
        logfile = os.path.join(log_dir, f'node-{args.node_id}.log')
        # If the node previously had a log file, append a rejoin header
        try:
            rejoined = os.path.exists(logfile) and os.path.getsize(logfile) > 0
            with open(logfile, 'a', encoding='utf-8') as f:
                ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                if rejoined:
                    f.write(f"\n=== Node {args.node_id} REJOINED at {ts} ===\n")
                else:
                    f.write(f"\n=== Node {args.node_id} STARTED at {ts} ===\n")
        except Exception:
            pass
        # Add a file handler that logs DEBUG+ to the per-node file
        try:
            fh = logging.FileHandler(logfile, mode='a', encoding='utf-8')
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter('%(asctime)s [%(levelname)-5s] %(name)-10s: %(message)s', datefmt='%H:%M:%S')
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            pass
    elif args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Parse known nodes
    known_nodes = set()
    if args.nodes:
        known_nodes = {int(nid.strip()) for nid in args.nodes.split(",") if nid.strip()}
    
    # Parse peer addresses for multi-device deployment
    # Format: 1:192.168.1.10,2:192.168.1.11,3:192.168.1.12
    peer_addresses = {}
    if args.peers:
        for mapping in args.peers.split(","):
            if ":" in mapping:
                parts = mapping.strip().split(":")
                if len(parts) == 2:
                    node_id_str, ip = parts
                    peer_addresses[int(node_id_str)] = ip
    
    # Auto-detect host IP if not provided
    # This enables multi-device deployment without manual --host argument
    from config import get_local_ip
    host = args.host
    if host is None:
        host = get_local_ip()
        logger.info(f"Auto-detected local IP: {host}")
    
    # Create and start node
    node = Node(args.node_id, known_nodes, host=host, peer_addresses=peer_addresses)
    
    try:
        node.start()
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        node.stop()


if __name__ == "__main__":
    main()
