"""
message.py - Message types and serialization for distributed communication.

Responsibility:
    - Define message types (TRADE, HEARTBEAT, ELECTION, COORDINATOR, etc.)
    - Serialize messages to JSON for network transmission
    - Deserialize incoming JSON messages
    - Include vector clock data in messages

UDP Compatibility:
    - All messages are serialized to JSON strings (UTF-8 encoded bytes)
    - Messages must fit within UDP packet size limit (see config.BUFFER_SIZE)
    - No connection state required - each message is self-contained
"""

import json
import time


# ==============================================================================
# Message Type Constants
# ==============================================================================
# Using string constants instead of Enum for simpler JSON serialization

# Heartbeat message - sent periodically to indicate node is alive
MSG_HEARTBEAT = "HEARTBEAT"

# Election messages (Bully Algorithm)
# ELECTION: sent to higher-ID nodes to start election
MSG_ELECTION = "ELECTION"
# OK: response to ELECTION message, indicates a higher node is alive
MSG_OK = "OK"
# COORDINATOR: announcement of new leader to all nodes
MSG_COORDINATOR = "COORDINATOR"

# Node membership messages
# JOIN: broadcast when a node joins the network
MSG_JOIN = "JOIN"
# JOIN_RESPONSE: coordinator sends state to joining node
MSG_JOIN_RESPONSE = "JOIN_RESPONSE"
# LEAVE: broadcast when a node gracefully leaves
MSG_LEAVE = "LEAVE"

# Energy trading messages
# TRADE_REQUEST: proposal to buy/sell energy credits
MSG_TRADE_REQUEST = "TRADE_REQUEST"
# TRADE_RESPONSE: accept/reject response to trade request
MSG_TRADE_RESPONSE = "TRADE_RESPONSE"
# TRADE_CONFIRM: final confirmation that trade was executed
MSG_TRADE_CONFIRM = "TRADE_CONFIRM"

# Ledger synchronization messages
# LEDGER_SYNC: coordinator broadcasts current ledger state
MSG_LEDGER_SYNC = "LEDGER_SYNC"
# State request (used by new coordinator to poll peers for their ledger state)
MSG_STATE_REQUEST = "STATE_REQUEST"
# Acknowledgement message for reliable unicast
MSG_ACK = "ACK"
# Anti-entropy gossip
MSG_GOSSIP = "GOSSIP"


# ==============================================================================
# Message Class
# ==============================================================================

class Message:
    """
    Represents a message exchanged between nodes in the distributed system.
    
    All messages contain:
        - message_type: Type of message (one of MSG_* constants)
        - sender_id: Unique identifier of the sending node (integer)
        - sender_priority: Priority for elections, typically same as sender_id
        - vector_clock: Dictionary mapping node_id -> logical timestamp
        - payload: Message-specific data (dictionary)
        - timestamp: Unix timestamp when message was created
    
    Messages are serialized to JSON for UDP transmission.
    """
    
    def __init__(self, message_type, sender_id, sender_priority=None, 
                 vector_clock=None, payload=None):
        """
        Initialize a new message.
        
        Args:
            message_type: One of the MSG_* constants defining message purpose
            sender_id: Integer ID of the node sending this message
            sender_priority: Priority for leader election (defaults to sender_id)
            vector_clock: Dict of {node_id: clock_value} for causal ordering
            payload: Dict containing message-specific data
        """
        # Type of message - determines how it will be processed
        self.message_type = message_type
        
        # Unique identifier of the sending node
        self.sender_id = sender_id
        
        # Priority used in Bully algorithm (higher priority wins election)
        # Defaults to sender_id if not specified
        self.sender_priority = sender_priority if sender_priority is not None else sender_id
        
        # Vector clock for establishing causal order of messages
        # Format: {"0": 5, "1": 3, "2": 7} where keys are node IDs
        self.vector_clock = vector_clock if vector_clock is not None else {}
        
        # Message-specific payload data
        # Examples:
        #   TRADE_REQUEST: {"amount": 10, "target_id": 2, "trade_type": "sell"}
        #   TRADE_RESPONSE: {"accepted": True, "trade_id": "uuid"}
        #   HEARTBEAT: {"credits": 100}
        self.payload = payload if payload is not None else {}
        
        # Unix timestamp for debugging and timeout calculations
        self.timestamp = time.time()
    
    def serialize(self):
        """
        Convert message to JSON string for UDP transmission.
        
        Returns:
            str: JSON-encoded string representation of the message
            
        Note:
            The returned string should be encoded to bytes using UTF-8
            before sending over UDP socket: message.serialize().encode('utf-8')
        """
        # Build dictionary with all message fields
        message_dict = {
            "message_type": self.message_type,
            "sender_id": self.sender_id,
            "sender_priority": self.sender_priority,
            "vector_clock": self.vector_clock,
            "payload": self.payload,
            "timestamp": self.timestamp
        }
        
        # Convert to JSON string
        # Using separators to minimize message size for UDP
        return json.dumps(message_dict, separators=(',', ':'))
    
    def to_bytes(self):
        """
        Convert message to bytes for direct UDP transmission.
        
        Returns:
            bytes: UTF-8 encoded JSON representation
        """
        return self.serialize().encode('utf-8')
    
    @staticmethod
    def deserialize(data):
        """
        Parse received data into a Message object.
        
        Args:
            data: Either a JSON string or bytes received from UDP socket
            
        Returns:
            Message: Reconstructed Message object
            
        Raises:
            json.JSONDecodeError: If data is not valid JSON
            KeyError: If required fields are missing
        """
        # Handle both bytes and string input
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        
        # Parse JSON string to dictionary
        message_dict = json.loads(data)
        
        # Reconstruct Message object from dictionary
        msg = Message(
            message_type=message_dict["message_type"],
            sender_id=message_dict["sender_id"],
            sender_priority=message_dict.get("sender_priority", message_dict["sender_id"]),
            vector_clock=message_dict.get("vector_clock", {}),
            payload=message_dict.get("payload", {})
        )
        
        # Restore original timestamp if present
        if "timestamp" in message_dict:
            msg.timestamp = message_dict["timestamp"]
        
        return msg
    
    @staticmethod
    def from_bytes(data):
        """
        Convenience method to create Message from bytes.
        
        Args:
            data: Bytes received from UDP socket
            
        Returns:
            Message: Reconstructed Message object
        """
        return Message.deserialize(data)
    
    def __repr__(self):
        """String representation for debugging."""
        return (f"Message(type={self.message_type}, sender={self.sender_id}, "
                f"priority={self.sender_priority}, payload={self.payload})")


# ==============================================================================
# Helper Functions for Creating Common Messages
# ==============================================================================

def create_heartbeat(sender_id, vector_clock, credits=None):
    """
    Create a heartbeat message.
    
    Args:
        sender_id: ID of the sending node
        vector_clock: Current vector clock state
        credits: Optional current energy credit balance
        
    Returns:
        Message: Heartbeat message ready for transmission
    """
    payload = {}
    if credits is not None:
        payload["credits"] = credits
    return Message(MSG_HEARTBEAT, sender_id, vector_clock=vector_clock, payload=payload)


def create_election(sender_id, sender_priority, vector_clock):
    """
    Create an election message for Bully algorithm.
    
    Args:
        sender_id: ID of the node initiating election
        sender_priority: Priority of the initiating node
        vector_clock: Current vector clock state
        
    Returns:
        Message: Election message to send to higher-ID nodes
    """
    return Message(MSG_ELECTION, sender_id, sender_priority=sender_priority,
                   vector_clock=vector_clock)


def create_ok(sender_id, sender_priority, vector_clock):
    """
    Create an OK response to an election message.
    
    Args:
        sender_id: ID of the responding node
        sender_priority: Priority of the responding node
        vector_clock: Current vector clock state
        
    Returns:
        Message: OK message indicating this node will take over election
    """
    return Message(MSG_OK, sender_id, sender_priority=sender_priority,
                   vector_clock=vector_clock)


def create_coordinator(sender_id, sender_priority, vector_clock):
    """
    Create a coordinator announcement message.
    
    Args:
        sender_id: ID of the new coordinator
        sender_priority: Priority of the new coordinator
        vector_clock: Current vector clock state
        
    Returns:
        Message: Coordinator announcement to broadcast to all nodes
    """
    # Include a message id so receivers can deduplicate reliable multicast copies
    payload = {"msg_id": f"coord-{sender_id}-{time.time()}"}
    return Message(MSG_COORDINATOR, sender_id, sender_priority=sender_priority,
                   vector_clock=vector_clock, payload=payload)


def create_trade_request(sender_id, vector_clock, target_id, amount, trade_type):
    """
    Create a trade request message.
    
    Args:
        sender_id: ID of the node proposing the trade
        vector_clock: Current vector clock state
        target_id: ID of the node to trade with
        amount: Number of energy credits to trade
        trade_type: "buy" or "sell" from sender's perspective
        
    Returns:
        Message: Trade request message
    """
    payload = {
        "target_id": target_id,
        "amount": amount,
        "trade_type": trade_type,  # "buy" or "sell"
        "trade_id": f"{sender_id}-{target_id}-{time.time()}"  # Unique trade ID
    }
    return Message(MSG_TRADE_REQUEST, sender_id, vector_clock=vector_clock,
                   payload=payload)


def create_trade_response(sender_id, vector_clock, trade_id, accepted, reason=None):
    """
    Create a trade response message.
    
    Args:
        sender_id: ID of the responding node
        vector_clock: Current vector clock state
        trade_id: ID of the trade being responded to
        accepted: Boolean indicating if trade is accepted
        reason: Optional reason for rejection
        
    Returns:
        Message: Trade response message
    """
    payload = {
        "trade_id": trade_id,
        "accepted": accepted
    }
    if reason:
        payload["reason"] = reason
    return Message(MSG_TRADE_RESPONSE, sender_id, vector_clock=vector_clock,
                   payload=payload)


def create_trade_confirm(sender_id, vector_clock, trade_id, success, buyer_id=None, seller_id=None, amount=None):
    """
    Create a trade confirmation message.
    
    Args:
        sender_id: ID of the confirming node
        vector_clock: Current vector clock state
        trade_id: ID of the confirmed trade
        success: Boolean indicating if trade completed successfully
        buyer_id: ID of the buying node (optional)
        seller_id: ID of the selling node (optional)
        amount: Amount of credits traded (optional)
        
    Returns:
        Message: Trade confirmation message
    """
    payload = {
        "trade_id": trade_id,
        "success": success
    }
    if buyer_id is not None:
        payload["buyer_id"] = buyer_id
    if seller_id is not None:
        payload["seller_id"] = seller_id
    if amount is not None:
        payload["amount"] = amount
    return Message(MSG_TRADE_CONFIRM, sender_id, vector_clock=vector_clock,
                   payload=payload)


def create_join(sender_id, vector_clock):
    """
    Create a join announcement message.
    
    Args:
        sender_id: ID of the joining node
        vector_clock: Current vector clock state
        
    Returns:
        Message: Join announcement to broadcast
    """
    # Include a message id so duplicates of the JOIN can be ignored
    payload = {"msg_id": f"join-{sender_id}-{time.time()}"}
    return Message(MSG_JOIN, sender_id, vector_clock=vector_clock, payload=payload)


def create_join_response(sender_id, vector_clock, coordinator_id, known_nodes):
    """
    Create a join response message (sent by coordinator to joining node).
    
    Contains state information the new node needs to properly participate
    in the distributed system, including vector clock state for causal ordering.
    
    Args:
        sender_id: ID of the coordinator sending the response
        vector_clock: Current vector clock state (new node will adopt this)
        coordinator_id: ID of the current coordinator
        known_nodes: List of all known node IDs in the system
        
    Returns:
        Message: Join response message for unicast to new node
    """
    payload = {
        "coordinator_id": coordinator_id,
        "known_nodes": list(known_nodes),
        "clock_state": vector_clock,  # New node will initialize from this
        # Optionally add ledger_state if provided (for join/rejoin sync)
    }
    # Attach a message id so coordinator can detect ACKs
    payload["msg_id"] = f"joinresp-{sender_id}-{time.time()}"
    return Message(MSG_JOIN_RESPONSE, sender_id, vector_clock=vector_clock,
                   payload=payload)


def create_ledger_sync(sender_id, vector_clock, ledger_state):
    """
    Create a LEDGER_SYNC message with the current ledger state.
    Args:
        sender_id: ID of the coordinator sending the sync
        vector_clock: Current vector clock state
        ledger_state: Dict representing the ledger state
    Returns:
        Message: LEDGER_SYNC message
    """
    # Attach message id so recipients can ACK this specific sync
    payload = {"ledger_state": ledger_state, "msg_id": f"ledger-{sender_id}-{time.time()}"}
    return Message(MSG_LEDGER_SYNC, sender_id, vector_clock=vector_clock, payload=payload)


def create_leave(sender_id, vector_clock):
    """
    Create a leave announcement message.
    
    Args:
        sender_id: ID of the leaving node
        vector_clock: Current vector clock state
        
    Returns:
        Message: Leave announcement to broadcast
    """
    return Message(MSG_LEAVE, sender_id, vector_clock=vector_clock)


def create_state_request(sender_id, vector_clock):
    """
    Create a state request message asking a peer to send its ledger state.

    Args:
        sender_id: ID of the requester (typically new coordinator)
        vector_clock: Current vector clock state

    Returns:
        Message: STATE_REQUEST message
    """
    payload = {"request": "ledger_state"}
    return Message(MSG_STATE_REQUEST, sender_id, vector_clock=vector_clock, payload=payload)


def create_ack(sender_id, vector_clock, msg_id):
    """
    Create an ACK message acknowledging receipt of a message with `msg_id`.

    Args:
        sender_id: ID of the acknowledging node
        vector_clock: Current vector clock state
        msg_id: The message id being acknowledged

    Returns:
        Message: ACK message
    """
    payload = {"msg_id": msg_id}
    return Message(MSG_ACK, sender_id, vector_clock=vector_clock, payload=payload)


def create_gossip(sender_id, vector_clock, ledger_state):
    """
    Create a GOSSIP message carrying this node's ledger state for anti-entropy.
    """
    payload = {"ledger_state": ledger_state}
    return Message(MSG_GOSSIP, sender_id, vector_clock=vector_clock, payload=payload)
