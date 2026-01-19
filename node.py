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
    MSG_JOIN, MSG_LEAVE, MSG_TRADE_REQUEST, MSG_TRADE_RESPONSE,
    MSG_TRADE_CONFIRM, MSG_LEDGER_SYNC,
    create_join, create_leave, create_trade_request, create_trade_response,
    create_trade_confirm
)
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
    
    def __init__(self, node_id, known_nodes=None):
        """
        Initialize the node with all components.
        
        Args:
            node_id: Unique identifier for this node (also used as priority)
            known_nodes: Optional list of other node IDs in the system
        """
        self.node_id = node_id
        self.priority = node_id  # Priority = node_id for Bully algorithm
        
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
        
        self.unicast = UnicastHandler(node_id=self.node_id)
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
        
        # 4. Announce join
        self._announce_join()
        
        # 5. Dynamic discovery - wait a bit then check if election needed
        time.sleep(HEARTBEAT_INTERVAL)
        if self.election.get_coordinator() is None:
            logger.info(f"Node {self.node_id}: No coordinator known, starting election")
            self.election.start_election()
        
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
        
        # 4. Close sockets
        self.multicast.close()
        self.unicast.close()
        
        logger.info(f"Node {self.node_id}: Node stopped")
    
    def _announce_join(self):
        """
        Broadcast JOIN message to announce this node's presence.
        """
        clock = self.vector_clock.increment()
        join_msg = create_join(self.node_id, clock)
        self.multicast.send(join_msg)
        logger.info(f"Node {self.node_id}: JOIN announced")
    
    def _announce_leave(self):
        """
        Broadcast LEAVE message before shutting down.
        """
        clock = self.vector_clock.increment()
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
            addr: Sender address (ip, port)
        """
        try:
            message = Message.from_bytes(data)
            
            # Ignore our own messages (we receive them due to loopback)
            if message.sender_id == self.node_id:
                return
            
            logger.debug(f"Node {self.node_id}: Multicast received: {message}")
            
            # Check causal delivery for application messages
            if message.message_type in [MSG_TRADE_REQUEST, MSG_TRADE_CONFIRM, MSG_LEDGER_SYNC]:
                if not self.vector_clock.can_deliver(message.sender_id, message.vector_clock):
                    logger.debug(f"Node {self.node_id}: Buffering message for causal delivery")
                    self._message_buffer.add(message, addr)
                    return
            
            # Update vector clock
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
            
            logger.debug(f"Node {self.node_id}: Unicast received: {message}")
            
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
        
        # If we're coordinator, announce ourselves
        if self.election.is_coordinator():
            clock = self.vector_clock.increment()
            from message import create_coordinator
            coord_msg = create_coordinator(self.node_id, self.priority, clock)
            self.multicast.send(coord_msg)
    
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
        elif trade_type == "buy":
            # Sender wants to buy from us - we're selling
            if self.ledger.can_sell(amount):
                accepted = True
                self.ledger.add_pending_trade(trade_id, "sell", amount, sender_id)
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
            
            # Execute our side of the trade
            self.ledger.execute_pending_trade(trade_id, self.vector_clock.get_clock())
            
            # Send confirmation
            clock = self.vector_clock.increment()
            confirm = create_trade_confirm(self.node_id, clock, trade_id, success=True)
            self.unicast.send(confirm, sender_id)
            
            # Update heartbeat with new balance
            self.heartbeat.set_energy_credits(self.ledger.get_balance())
        else:
            logger.info(f"Node {self.node_id}: Energy trade {trade_id} REJECTED by Node {sender_id}: {reason}")
            self.ledger.remove_pending_trade(trade_id)
    
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
            
            # Execute our side of the trade
            self.ledger.execute_pending_trade(trade_id, self.vector_clock.get_clock())
            
            # Update heartbeat with new balance
            self.heartbeat.set_energy_credits(self.ledger.get_balance())
        else:
            logger.warning(f"Node {self.node_id}: Energy trade {trade_id} FAILED")
            self.ledger.remove_pending_trade(trade_id)
    
    def _handle_ledger_sync(self, message):
        """
        Handle LEDGER_SYNC message from coordinator.
        
        Sync our ledger state with coordinator's view.
        """
        # Only accept sync from coordinator
        if message.sender_id != self.election.get_coordinator():
            logger.debug(f"Node {self.node_id}: Ignoring LEDGER_SYNC from non-coordinator")
            return
        
        # For now, just log the sync - full implementation would reconcile state
        logger.debug(f"Node {self.node_id}: Received LEDGER_SYNC from coordinator")
    
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
        # Validate
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
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    # Set debug logging if requested
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Parse known nodes
    known_nodes = set()
    if args.nodes:
        known_nodes = {int(nid.strip()) for nid in args.nodes.split(",") if nid.strip()}
    
    # Create and start node
    node = Node(args.node_id, known_nodes)
    
    try:
        node.start()
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        node.stop()


if __name__ == "__main__":
    main()
