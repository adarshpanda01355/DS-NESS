"""
unicast.py - Direct UDP messaging between specific nodes.

Responsibility:
    - Send messages directly to a specific node (point-to-point)
    - Listen for incoming unicast messages
    - Used for election messages and direct trade negotiations
    - Support acknowledgments for reliable delivery

UDP Unicast Overview:
    Unlike multicast (one-to-many), unicast is one-to-one communication.
    Messages are sent directly to a specific node's IP and port.
    
    Used for:
    - Election messages (ELECTION, OK) - sent to specific higher/lower nodes
    - Direct trade negotiations between two parties
    - Acknowledgments requiring point-to-point confirmation

Port Assignment:
    Each node listens on UNICAST_PORT_BASE + node_id
    Example: Node 1 -> port 6001, Node 2 -> port 6002, etc.
    This allows multiple nodes on the same machine for testing.
"""

import socket
import threading
import time
import logging
from collections import defaultdict

from config import UNICAST_PORT_BASE, BUFFER_SIZE, MESSAGE_RETRY_COUNT, MESSAGE_RETRY_DELAY

# Configure logging for unicast events
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('unicast')


class UnicastHandler:
    """
    Handles direct UDP communication between nodes.
    
    Each node has a unique unicast port based on its ID.
    Supports both blocking and non-blocking (callback-based) receive.
    Includes simple acknowledgment tracking for reliable delivery.
    """
    
    def __init__(self, node_id, port=None):
        """
        Initialize unicast socket on the node's designated port.
        
        Args:
            node_id: Unique identifier for this node
            port: Optional explicit port (default: UNICAST_PORT_BASE + node_id)
        """
        self.node_id = node_id
        
        # Calculate port for this node
        # Each node gets a unique port: base + node_id
        self.port = port if port is not None else (UNICAST_PORT_BASE + node_id)
        
        # Host address - localhost for single-machine testing
        self.host = '127.0.0.1'
        
        # Flag to control receive loop
        self._running = False
        self._receive_thread = None
        
        # Callback function for received messages
        self._on_receive = None
        
        # Acknowledgment tracking
        # Maps message_id -> Event object for waiting on ACKs
        self._pending_acks = {}
        self._ack_lock = threading.Lock()
        
        # Create and configure socket
        self._setup_socket()
        
        logger.info(f"Node {self.node_id}: Unicast handler initialized "
                    f"(listening on {self.host}:{self.port})")
    
    def _setup_socket(self):
        """
        Set up the UDP socket for unicast communication.
        
        Unlike multicast, we use a single socket for both send and receive.
        The socket is bound to this node's specific port.
        """
        # Create UDP socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # Allow address reuse (helpful during development/restart)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Bind to this node's port on localhost
        self._socket.bind((self.host, self.port))
        
        # Set timeout for non-blocking receive with periodic checks
        self._socket.settimeout(1.0)
        
        logger.debug(f"Node {self.node_id}: Unicast socket bound to {self.host}:{self.port}")
    
    def get_node_address(self, target_node_id):
        """
        Get the address (host, port) for a target node.
        
        Args:
            target_node_id: ID of the target node
            
        Returns:
            tuple: (host, port) for the target node
        """
        target_port = UNICAST_PORT_BASE + target_node_id
        return (self.host, target_port)
    
    def send(self, message, target_node_id):
        """
        Send a message directly to a specific node.
        
        Args:
            message: Either a Message object (with to_bytes method),
                     bytes, or string to send
            target_node_id: ID of the node to send to
            
        Returns:
            bool: True if send succeeded, False otherwise
        """
        try:
            # Convert message to bytes if needed
            if hasattr(message, 'to_bytes'):
                data = message.to_bytes()
            elif isinstance(message, str):
                data = message.encode('utf-8')
            else:
                data = message
            
            # Get target address
            target_addr = self.get_node_address(target_node_id)
            
            # Send to target node
            bytes_sent = self._socket.sendto(data, target_addr)
            
            logger.debug(f"Node {self.node_id}: UNICAST SEND -> {bytes_sent} bytes "
                        f"to Node {target_node_id} ({target_addr[0]}:{target_addr[1]})")
            return True
            
        except Exception as e:
            logger.error(f"Node {self.node_id}: Unicast send error to Node {target_node_id}: {e}")
            return False
    
    def send_to_address(self, message, target_host, target_port):
        """
        Send a message to a specific address (host, port).
        
        Used when the target address is known but not necessarily a node ID.
        
        Args:
            message: Message to send (Message object, bytes, or string)
            target_host: Target IP address
            target_port: Target port number
            
        Returns:
            bool: True if send succeeded, False otherwise
        """
        try:
            # Convert message to bytes if needed
            if hasattr(message, 'to_bytes'):
                data = message.to_bytes()
            elif isinstance(message, str):
                data = message.encode('utf-8')
            else:
                data = message
            
            # Send to address
            bytes_sent = self._socket.sendto(data, (target_host, target_port))
            
            logger.debug(f"Node {self.node_id}: UNICAST SEND -> {bytes_sent} bytes "
                        f"to {target_host}:{target_port}")
            return True
            
        except Exception as e:
            logger.error(f"Node {self.node_id}: Unicast send error to {target_host}:{target_port}: {e}")
            return False
    
    def send_with_retry(self, message, target_node_id, retries=None):
        """
        Send a message with automatic retry on failure.
        
        Attempts to send the message multiple times with delays between attempts.
        Useful for critical messages that must be delivered.
        
        Args:
            message: Message to send
            target_node_id: ID of the target node
            retries: Number of retry attempts (default: from config)
            
        Returns:
            bool: True if send succeeded on any attempt, False if all failed
        """
        if retries is None:
            retries = MESSAGE_RETRY_COUNT
        
        for attempt in range(retries + 1):
            if self.send(message, target_node_id):
                return True
            
            if attempt < retries:
                logger.debug(f"Node {self.node_id}: Retry {attempt + 1}/{retries} "
                            f"for message to Node {target_node_id}")
                time.sleep(MESSAGE_RETRY_DELAY)
        
        logger.warning(f"Node {self.node_id}: Failed to send to Node {target_node_id} "
                      f"after {retries + 1} attempts")
        return False
    
    def send_and_wait_ack(self, message, target_node_id, timeout=None, message_id=None):
        """
        Send a message and wait for acknowledgment.
        
        This implements a simple request-response pattern:
        1. Register that we're waiting for an ACK
        2. Send the message
        3. Wait for ACK (with timeout)
        4. Return whether ACK was received
        
        Args:
            message: Message to send
            target_node_id: ID of the target node
            timeout: Seconds to wait for ACK (default: MESSAGE_RETRY_DELAY * 2)
            message_id: Unique ID to match ACK (default: generated from message)
            
        Returns:
            bool: True if ACK received, False if timeout
        """
        if timeout is None:
            timeout = MESSAGE_RETRY_DELAY * 2
        
        # Generate message ID if not provided
        if message_id is None:
            if hasattr(message, 'payload') and 'trade_id' in message.payload:
                message_id = message.payload['trade_id']
            else:
                message_id = f"{self.node_id}-{target_node_id}-{time.time()}"
        
        # Create event for waiting on ACK
        ack_event = threading.Event()
        
        with self._ack_lock:
            self._pending_acks[message_id] = ack_event
        
        try:
            # Send the message
            if not self.send(message, target_node_id):
                return False
            
            # Wait for ACK
            received = ack_event.wait(timeout=timeout)
            
            if received:
                logger.debug(f"Node {self.node_id}: ACK received for message {message_id}")
            else:
                logger.debug(f"Node {self.node_id}: ACK timeout for message {message_id}")
            
            return received
            
        finally:
            # Clean up pending ACK
            with self._ack_lock:
                self._pending_acks.pop(message_id, None)
    
    def acknowledge(self, message_id):
        """
        Signal that an acknowledgment was received for a pending message.
        
        Called by the message handler when an ACK message is received.
        
        Args:
            message_id: ID of the message being acknowledged
        """
        with self._ack_lock:
            if message_id in self._pending_acks:
                self._pending_acks[message_id].set()
                logger.debug(f"Node {self.node_id}: Signaled ACK for message {message_id}")
    
    def receive(self):
        """
        Receive a single message (blocking with timeout).
        
        Blocks until a message is received or socket times out.
        
        Returns:
            tuple: (data_bytes, sender_address) or (None, None) on timeout/error
        """
        try:
            # Receive data and sender address
            data, addr = self._socket.recvfrom(BUFFER_SIZE)
            
            logger.debug(f"Node {self.node_id}: UNICAST RECV <- {len(data)} bytes "
                        f"from {addr[0]}:{addr[1]}")
            return data, addr
            
        except socket.timeout:
            # Timeout is expected in non-blocking loop
            return None, None
        except Exception as e:
            logger.error(f"Node {self.node_id}: Unicast receive error: {e}")
            return None, None
    
    def start_receiving(self, callback):
        """
        Start the non-blocking receive loop in a background thread.
        
        The callback function is called for each received message.
        
        Args:
            callback: Function to call with (data, addr) for each message
        """
        if self._running:
            logger.warning(f"Node {self.node_id}: Unicast receive loop already running")
            return
        
        self._on_receive = callback
        self._running = True
        
        # Start receive loop in background thread
        self._receive_thread = threading.Thread(
            target=self._receive_loop,
            name=f"Unicast-Recv-{self.node_id}",
            daemon=True  # Thread dies when main program exits
        )
        self._receive_thread.start()
        
        logger.info(f"Node {self.node_id}: Unicast receive loop started")
    
    def _receive_loop(self):
        """
        Background thread: continuously receive and process messages.
        
        Uses socket timeout to periodically check if we should stop.
        Calls the registered callback for each received message.
        """
        logger.debug(f"Node {self.node_id}: Entering unicast receive loop")
        
        while self._running:
            try:
                # Try to receive (will timeout after 1 second if no data)
                data, addr = self._socket.recvfrom(BUFFER_SIZE)
                
                if data and self._on_receive:
                    logger.debug(f"Node {self.node_id}: UNICAST RECV <- {len(data)} bytes "
                                f"from {addr[0]}:{addr[1]}")
                    
                    # Call the callback with received data
                    try:
                        self._on_receive(data, addr)
                    except Exception as e:
                        logger.error(f"Node {self.node_id}: Callback error: {e}")
                        
            except socket.timeout:
                # Expected - allows periodic check of _running flag
                continue
            except OSError as e:
                if self._running:  # Only log if not intentionally stopped
                    logger.error(f"Node {self.node_id}: Socket error in receive loop: {e}")
                break
        
        logger.debug(f"Node {self.node_id}: Exiting unicast receive loop")
    
    def stop_receiving(self):
        """
        Stop the background receive loop.
        """
        self._running = False
        
        if self._receive_thread and self._receive_thread.is_alive():
            self._receive_thread.join(timeout=2.0)
            
        logger.info(f"Node {self.node_id}: Unicast receive loop stopped")
    
    def close(self):
        """
        Close the unicast socket.
        
        Should be called when the node is shutting down.
        """
        # Stop receive loop first
        self.stop_receiving()
        
        # Close socket
        try:
            self._socket.close()
            logger.info(f"Node {self.node_id}: Unicast socket closed")
        except Exception as e:
            logger.error(f"Node {self.node_id}: Error closing unicast socket: {e}")
