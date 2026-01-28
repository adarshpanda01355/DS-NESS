"""
ledger.py - Energy credit simulation and trade ledger.

Responsibility:
    - Track energy credits for this node
    - Record trade transactions (buy/sell energy)
    - Validate trade requests (sufficient credits)
    - Maintain transaction history
    - Coordinator aggregates and broadcasts ledger state

Simulation Notes:
    This is a SIMULATED energy trading system for demonstrating
    distributed systems concepts. There is:
    - No real financial correctness guarantees
    - No transaction rollback mechanism
    - No persistent storage
    
    Operations are applied after CAUSAL DELIVERY is confirmed
    (using vector clocks) to ensure consistent ordering across nodes.

Operations:
    BUY <amount>: Receive energy credits (increase balance)
    SELL <amount>: Send energy credits (decrease balance)
    
    A trade between two nodes:
    - Seller executes SELL (balance decreases)
    - Buyer executes BUY (balance increases)
"""

import threading
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum

from config import INITIAL_ENERGY_CREDITS, MIN_ENERGY_CREDITS

# Configure logging for ledger events
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('ledger')


class TransactionType(Enum):
    """Types of ledger transactions."""
    BUY = "BUY"      # Receive credits (balance increases)
    SELL = "SELL"    # Send credits (balance decreases)


@dataclass
class Transaction:
    """
    Represents a single ledger transaction.
    
    Attributes:
        tx_type: BUY or SELL
        amount: Number of credits involved
        counterparty_id: The other node in the trade
        trade_id: Unique identifier for the trade
        timestamp: When the transaction was executed
        vector_clock: Vector clock state at execution time
        balance_after: Balance after this transaction
    """
    tx_type: TransactionType
    amount: int
    counterparty_id: int
    trade_id: str
    timestamp: float = field(default_factory=time.time)
    vector_clock: Optional[Dict] = None
    balance_after: int = 0
    
    def __repr__(self):
        return (f"Transaction({self.tx_type.value} {self.amount} credits "
                f"{'from' if self.tx_type == TransactionType.BUY else 'to'} "
                f"Node {self.counterparty_id}, balance={self.balance_after})")


class EnergyLedger:
    """
    Manages energy credits and trade transactions for a node.
    
    Thread-safe implementation using locks.
    Maintains transaction history for auditing/debugging.
    
    Balance Rules:
    - Cannot go below MIN_ENERGY_CREDITS (default: 0)
    - BUY always succeeds (receiving credits)
    - SELL requires sufficient balance
    """
    
    def __init__(self, node_id, initial_credits=None):
        """
        Initialize ledger with starting energy credits.
        
        Args:
            node_id: This node's unique identifier
            initial_credits: Starting balance (default: from config)
        """
        self.node_id = node_id
        
        # Current energy credit balance
        self._balance = initial_credits if initial_credits is not None else INITIAL_ENERGY_CREDITS
        self._balance_lock = threading.Lock()
        
        # Transaction history (most recent last)
        self._transactions: List[Transaction] = []
        self._tx_lock = threading.Lock()
        
        # Pending trades waiting for confirmation
        # Maps trade_id -> trade details
        self._pending_trades: Dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        
        # Set of completed trade IDs (to prevent duplicates)
        self._completed_trades: set = set()
        
        logger.info(f"Node {self.node_id}: Ledger initialized with {self._balance} credits")
    
    def get_balance(self):
        """
        Return current energy credit balance.
        
        Returns:
            int: Current balance
        """
        with self._balance_lock:
            return self._balance
    
    def can_sell(self, amount):
        """
        Check if node has enough credits to sell.
        
        Args:
            amount: Number of credits to sell
            
        Returns:
            bool: True if balance >= amount + MIN_ENERGY_CREDITS
        """
        with self._balance_lock:
            # Ensure we don't go below minimum
            return self._balance - amount >= MIN_ENERGY_CREDITS
    
    def execute_sell(self, amount, buyer_id, trade_id, vector_clock=None):
        """
        Execute a SELL operation - deduct credits for a sale.
        
        Called after causal delivery is confirmed.
        
        Args:
            amount: Number of credits to sell
            buyer_id: ID of the node buying the credits
            trade_id: Unique identifier for this trade
            vector_clock: Vector clock state at execution time
            
        Returns:
            bool: True if successful, False if insufficient balance
        """
        # Check for duplicate execution
        with self._tx_lock:
            if trade_id in self._completed_trades:
                logger.warning(f"Node {self.node_id}: Duplicate SELL ignored "
                              f"(trade_id={trade_id})")
                return True  # Already executed
        
        with self._balance_lock:
            # Validate balance
            if self._balance - amount < MIN_ENERGY_CREDITS:
                logger.error(f"Node {self.node_id}: SELL FAILED - insufficient balance "
                            f"(have={self._balance}, need={amount})")
                return False
            
            # Execute the sale
            old_balance = self._balance
            self._balance -= amount
            new_balance = self._balance
        
        # Record transaction
        tx = Transaction(
            tx_type=TransactionType.SELL,
            amount=amount,
            counterparty_id=buyer_id,
            trade_id=trade_id,
            vector_clock=vector_clock,
            balance_after=new_balance
        )
        
        with self._tx_lock:
            self._transactions.append(tx)
            self._completed_trades.add(trade_id)
        
        logger.info(f"Node {self.node_id}: SELL {amount} credits to Node {buyer_id} "
                   f"[{old_balance} -> {new_balance}] (trade_id={trade_id})")
        
        return True
    
    def execute_buy(self, amount, seller_id, trade_id, vector_clock=None):
        """
        Execute a BUY operation - add credits for a purchase.
        
        Called after causal delivery is confirmed.
        BUY always succeeds (receiving credits cannot fail).
        
        Args:
            amount: Number of credits to buy
            seller_id: ID of the node selling the credits
            trade_id: Unique identifier for this trade
            vector_clock: Vector clock state at execution time
            
        Returns:
            bool: Always True (buy cannot fail)
        """
        # Check for duplicate execution
        with self._tx_lock:
            if trade_id in self._completed_trades:
                logger.warning(f"Node {self.node_id}: Duplicate BUY ignored "
                              f"(trade_id={trade_id})")
                return True  # Already executed
        
        with self._balance_lock:
            # Execute the purchase
            old_balance = self._balance
            self._balance += amount
            new_balance = self._balance
        
        # Record transaction
        tx = Transaction(
            tx_type=TransactionType.BUY,
            amount=amount,
            counterparty_id=seller_id,
            trade_id=trade_id,
            vector_clock=vector_clock,
            balance_after=new_balance
        )
        
        with self._tx_lock:
            self._transactions.append(tx)
            self._completed_trades.add(trade_id)
        
        logger.info(f"Node {self.node_id}: BUY {amount} credits from Node {seller_id} "
                   f"[{old_balance} -> {new_balance}] (trade_id={trade_id})")
        
        return True
    
    def add_pending_trade(self, trade_id, trade_type, amount, counterparty_id):
        """
        Add a trade to the pending list (waiting for confirmation).
        
        Args:
            trade_id: Unique identifier for the trade
            trade_type: "buy" or "sell"
            amount: Number of credits
            counterparty_id: The other node in the trade
        """
        with self._pending_lock:
            self._pending_trades[trade_id] = {
                "trade_type": trade_type,
                "amount": amount,
                "counterparty_id": counterparty_id,
                "timestamp": time.time()
            }
        
        logger.debug(f"Node {self.node_id}: Added pending trade {trade_id} "
                    f"({trade_type} {amount} with Node {counterparty_id})")
    
    def get_pending_trade(self, trade_id):
        """
        Get details of a pending trade.
        
        Args:
            trade_id: Trade identifier
            
        Returns:
            dict: Trade details, or None if not found
        """
        with self._pending_lock:
            return self._pending_trades.get(trade_id)

    def has_trade(self, trade_id):
        """
        Check if a trade ID is known (either pending or completed).

        Args:
            trade_id: Trade identifier

        Returns:
            bool: True if trade is pending or already completed
        """
        with self._pending_lock:
            if trade_id in self._pending_trades:
                return True
        with self._tx_lock:
            return trade_id in self._completed_trades
    
    def remove_pending_trade(self, trade_id):
        """
        Remove a trade from the pending list.
        
        Args:
            trade_id: Trade identifier
        """
        with self._pending_lock:
            self._pending_trades.pop(trade_id, None)
        
        logger.debug(f"Node {self.node_id}: Removed pending trade {trade_id}")
    
    def execute_pending_trade(self, trade_id, vector_clock=None):
        """
        Execute a pending trade (after causal delivery confirmed).
        
        Args:
            trade_id: Trade identifier
            vector_clock: Vector clock at execution time
            
        Returns:
            bool: True if executed, False if not found or failed
        """
        trade = self.get_pending_trade(trade_id)
        if not trade:
            logger.warning(f"Node {self.node_id}: Pending trade {trade_id} not found")
            return False
        
        success = False
        if trade["trade_type"] == "sell":
            success = self.execute_sell(
                amount=trade["amount"],
                buyer_id=trade["counterparty_id"],
                trade_id=trade_id,
                vector_clock=vector_clock
            )
        elif trade["trade_type"] == "buy":
            success = self.execute_buy(
                amount=trade["amount"],
                seller_id=trade["counterparty_id"],
                trade_id=trade_id,
                vector_clock=vector_clock
            )
        
        if success:
            self.remove_pending_trade(trade_id)
        
        return success
    
    def get_transaction_history(self):
        """
        Return list of all completed transactions.
        
        Returns:
            list: List of Transaction objects (oldest first)
        """
        with self._tx_lock:
            return list(self._transactions)
    
    def get_recent_transactions(self, count=10):
        """
        Return the most recent transactions.
        
        Args:
            count: Number of transactions to return
            
        Returns:
            list: List of Transaction objects (most recent first)
        """
        with self._tx_lock:
            return list(reversed(self._transactions[-count:]))
    
    def get_state(self):
        """
        Return current ledger state for synchronization.
        Includes balance, all transactions, and completed trades.
        """
        with self._balance_lock:
            balance = self._balance
        with self._tx_lock:
            transactions = [
                {
                    "tx_type": tx.tx_type.value,
                    "amount": tx.amount,
                    "counterparty_id": tx.counterparty_id,
                    "trade_id": tx.trade_id,
                    "timestamp": tx.timestamp,
                    "vector_clock": tx.vector_clock,
                    "balance_after": tx.balance_after
                }
                for tx in self._transactions
            ]
            completed_trades = list(self._completed_trades)
        return {
            "node_id": self.node_id,
            "balance": balance,
            "transactions": transactions,
            "completed_trades": completed_trades
        }
    
    def sync_from_state(self, state):
        """
        Synchronize ledger from coordinator's state.
        Updates balance, transaction history, and completed trades.
        """
        if state.get("node_id") != self.node_id:
            return
        with self._balance_lock:
            old_balance = self._balance
            self._balance = state.get("balance", self._balance)
        with self._tx_lock:
            self._transactions.clear()
            for tx in state.get("transactions", []):
                from enum import Enum
                class _FakeEnum(Enum):
                    BUY = "BUY"
                    SELL = "SELL"
                tx_type = _FakeEnum(tx["tx_type"]) if isinstance(tx["tx_type"], str) else tx["tx_type"]
                self._transactions.append(
                    Transaction(
                        tx_type=TransactionType.BUY if tx_type == _FakeEnum.BUY else TransactionType.SELL,
                        amount=tx["amount"],
                        counterparty_id=tx["counterparty_id"],
                        trade_id=tx["trade_id"],
                        timestamp=tx["timestamp"],
                        vector_clock=tx.get("vector_clock"),
                        balance_after=tx.get("balance_after", 0)
                    )
                )
            self._completed_trades = set(state.get("completed_trades", []))
        logger.info(f"Node {self.node_id}: Synced ledger state (balance: {old_balance} -> {self._balance}, tx count: {len(self._transactions)})")
    
    def print_status(self):
        """
        Print current energy ledger status to console.
        """
        balance = self.get_balance()
        tx_count = len(self._transactions)
        pending_count = len(self._pending_trades)
        
        print(f"\n{'='*40}")
        print(f"  ENERGY LEDGER - Node {self.node_id}")
        print(f"{'='*40}")
        print(f"  Energy Balance: {balance} credits")
        print(f"  Completed Trades: {tx_count}")
        print(f"  Pending Trades: {pending_count}")
        
        # Show recent transactions
        recent = self.get_recent_transactions(5)
        if recent:
            print(f"\n  Recent Energy Transactions:")
            for tx in recent:
                direction = "from" if tx.tx_type == TransactionType.BUY else "to"
                print(f"    {tx.tx_type.value} {tx.amount} credits {direction} Node {tx.counterparty_id}")
    
        print(f"{'='*40}\n")
