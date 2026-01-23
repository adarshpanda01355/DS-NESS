"""
multicast.py - UDP multicast communication for group messaging.

Responsibility:
    - Set up multicast socket for group communication
    - Send messages to all nodes in the multicast group
    - Receive messages from the multicast group
    - Handle multicast group membership (join/leave)

UDP Multicast Overview:
    Multicast allows one-to-many communication. A single send reaches all
    nodes that have joined the multicast group. This is efficient for:
    - Heartbeat broadcasts
    - Coordinator announcements
    - Join/leave notifications
    
    Multicast addresses are in the range 224.0.0.0 - 239.255.255.255.
    We use a locally-scoped address (224.1.1.1) for testing on one machine.

Socket Options Used:
    - SO_REUSEADDR: Allow multiple processes to bind to the same port
    - IP_ADD_MEMBERSHIP: Join the multicast group to receive messages
    - IP_MULTICAST_TTL: Time-to-live for multicast packets (1 = local network)
    - IP_MULTICAST_LOOP: Allow receiving own multicast messages
"""

import socket
import struct
import threading
import logging

from config import MULTICAST_GROUP, MULTICAST_PORT, BUFFER_SIZE

# Configure logging for multicast events
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('multicast')


class MulticastHandler:
    """
    Handles UDP multicast send and receive operations.
    
    Creates two sockets:
    - Receive socket: Bound to multicast port, joined to group
    - Send socket: Used to send to multicast group
    
    This separation avoids issues with some platforms where a single
    socket cannot both send and receive multicast reliably.
    """
    
    def __init__(self, group=None, port=None, node_id=None):
        """
        Initialize multicast sockets and join the multicast group.
        
        Args:
            group: Multicast group IP address (default: from config)
            port: Multicast port number (default: from config)
            node_id: ID of this node (for logging)
        """
        self.group = group or MULTICAST_GROUP
        self.port = port or MULTICAST_PORT
        self.node_id = node_id
        
        # Flag to control receive loop
        self._running = False
        self._receive_thread = None
        
        # Callback function for received messages
        self._on_receive = None
        
        # Create and configure sockets
        self._setup_receive_socket()
        self._setup_send_socket()
        
        logger.info(f"Node {self.node_id}: Multicast handler initialized "
                    f"(group={self.group}, port={self.port})")
    
    def _setup_receive_socket(self):
        """
        Set up the socket for receiving multicast messages.
        
        Steps:
        1. Create UDP socket
        2. Enable address reuse (multiple nodes on same machine)
        3. Bind to multicast port on all interfaces
        4. Join the multicast group
        """
        # Create UDP socket (SOCK_DGRAM = UDP)
        self._recv_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # Allow multiple sockets to bind to the same port
        # Essential for running multiple nodes on the same machine
        self._recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # On Windows, also need SO_REUSEADDR for the port
        # On Linux, you might need SO_REUSEPORT instead
        try:
            self._recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass  # SO_REUSEPORT not available on Windows
        
        # Bind to the multicast port on all interfaces
        # Empty string '' means INADDR_ANY (all interfaces)
        self._recv_socket.bind(('', self.port))
        
        # Join the multicast group
        # This tells the OS to deliver multicast packets for this group to us
        # struct format: 4 bytes for group IP + 4 bytes for interface IP
        mreq = struct.pack('4sL', 
                          socket.inet_aton(self.group),  # Multicast group
                          socket.INADDR_ANY)             # Interface (any)
        self._recv_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        # Set socket timeout for non-blocking receive with periodic checks
        self._recv_socket.settimeout(1.0)
        
        logger.debug(f"Node {self.node_id}: Receive socket bound to port {self.port}, "
                     f"joined group {self.group}")
    
    def _setup_send_socket(self):
        """
        Set up the socket for sending multicast messages.
        
        Steps:
        1. Create UDP socket
        2. Set multicast TTL (time-to-live)
        3. Enable/disable loopback (receiving own messages)
        """
        # Create UDP socket for sending
        self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # Set Time-To-Live for multicast packets
        # TTL=1 means packets stay on the local network segment
        # Higher values allow crossing routers (not needed for local testing)
        ttl = struct.pack('b', 1)
        self._send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        
        # Enable multicast loopback
        # When enabled, this node will receive its own multicast messages
        # Useful for testing, can be disabled in production
        self._send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        
        logger.debug(f"Node {self.node_id}: Send socket configured with TTL=1")
    
    def send(self, message):
        """
        Send a message to the multicast group.
        
        All nodes that have joined the group will receive this message,
        including this node (if loopback is enabled).
        
        Args:
            message: Either a Message object (with to_bytes method),
                     bytes, or string to send
                     
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
            
            # Send to multicast group
            bytes_sent = self._send_socket.sendto(data, (self.group, self.port))
            
            logger.debug(f"Node {self.node_id}: MULTICAST SEND -> {bytes_sent} bytes "
                        f"to {self.group}:{self.port}")
            return True
            
        except Exception as e:
            logger.error(f"Node {self.node_id}: Multicast send error: {e}")
            return False
    
    def send_reliable(self, message, retries=3, delay=0.1):
        """
        Send a message multiple times for reliability.
        
        Used for critical messages like COORDINATOR announcements and JOIN
        where packet loss could cause system inconsistency.
        
        NOTE: Do NOT use for heartbeats - they should remain as single sends
        to avoid network congestion. See ARCHITECTURAL_AUDIT.md Section 4.
        
        Args:
            message: Message to send
            retries: Number of times to send (default: 3)
            delay: Seconds between sends (default: 0.1)
            
        Returns:
            bool: True if at least one send succeeded
        """
        import time
        success = False
        for i in range(retries):
            if self.send(message):
                success = True
            if i < retries - 1:
                time.sleep(delay)
        
        if success:
            logger.debug(f"Node {self.node_id}: Reliable multicast sent ({retries} copies)")
        return success
    
    def receive(self):
        """
        Receive a single message from the multicast group (blocking).
        
        Blocks until a message is received or socket times out.
        
        Returns:
            tuple: (data_bytes, sender_address) or (None, None) on timeout/error
        """
        try:
            # Receive data and sender address
            data, addr = self._recv_socket.recvfrom(BUFFER_SIZE)
            
            logger.debug(f"Node {self.node_id}: MULTICAST RECV <- {len(data)} bytes "
                        f"from {addr[0]}:{addr[1]}")
            return data, addr
            
        except socket.timeout:
            # Timeout is expected in non-blocking loop
            return None, None
        except Exception as e:
            logger.error(f"Node {self.node_id}: Multicast receive error: {e}")
            return None, None
    
    def start_receiving(self, callback):
        """
        Start the non-blocking receive loop in a background thread.
        
        The callback function is called for each received message.
        
        Args:
            callback: Function to call with (data, addr) for each message
        """
        if self._running:
            logger.warning(f"Node {self.node_id}: Receive loop already running")
            return
        
        self._on_receive = callback
        self._running = True
        
        # Start receive loop in background thread
        self._receive_thread = threading.Thread(
            target=self._receive_loop,
            name=f"Multicast-Recv-{self.node_id}",
            daemon=True  # Thread dies when main program exits
        )
        self._receive_thread.start()
        
        logger.info(f"Node {self.node_id}: Multicast receive loop started")
    
    def _receive_loop(self):
        """
        Background thread: continuously receive and process messages.
        
        Uses socket timeout to periodically check if we should stop.
        Calls the registered callback for each received message.
        """
        logger.debug(f"Node {self.node_id}: Entering multicast receive loop")
        
        while self._running:
            try:
                # Try to receive (will timeout after 1 second if no data)
                data, addr = self._recv_socket.recvfrom(BUFFER_SIZE)
                
                if data and self._on_receive:
                    logger.debug(f"Node {self.node_id}: MULTICAST RECV <- {len(data)} bytes "
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
        
        logger.debug(f"Node {self.node_id}: Exiting multicast receive loop")
    
    def stop_receiving(self):
        """
        Stop the background receive loop.
        """
        self._running = False
        
        if self._receive_thread and self._receive_thread.is_alive():
            self._receive_thread.join(timeout=2.0)
            
        logger.info(f"Node {self.node_id}: Multicast receive loop stopped")
    
    def close(self):
        """
        Leave multicast group and close sockets.
        
        Should be called when the node is shutting down.
        """
        # Stop receive loop first
        self.stop_receiving()
        
        # Leave the multicast group
        try:
            mreq = struct.pack('4sL',
                              socket.inet_aton(self.group),
                              socket.INADDR_ANY)
            self._recv_socket.setsockopt(socket.IPPROTO_IP, 
                                         socket.IP_DROP_MEMBERSHIP, mreq)
            logger.debug(f"Node {self.node_id}: Left multicast group {self.group}")
        except Exception as e:
            logger.debug(f"Node {self.node_id}: Error leaving multicast group: {e}")
        
        # Close sockets
        try:
            self._recv_socket.close()
            self._send_socket.close()
            logger.info(f"Node {self.node_id}: Multicast sockets closed")
        except Exception as e:
            logger.error(f"Node {self.node_id}: Error closing sockets: {e}")
