import os
import asyncio
import logging
import zlib
import gzip
import io
import zmq.asyncio
import websockets
import uuid
import signal
import time
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import os

# Leader election imports
try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    KUBERNETES_AVAILABLE = True
except ImportError:
    KUBERNETES_AVAILABLE = False
    logging.warning("Kubernetes client not available - leader election disabled")

# Use ujson for speed if available, otherwise fall back to Python's json.
try:
    import ujson as json
except ImportError:
    import json

# -----------------------
# Logging configuration
# -----------------------
log_dir = os.getenv('LOG_DIR', '/var/log/crypto/')
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, 'price_publisher.log')

# Create handlers
console_handler = logging.StreamHandler()
file_handler = RotatingFileHandler(
    log_file_path,
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)

# Create formatters and add it to handlers
log_format = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_handler.setFormatter(log_format)
file_handler.setFormatter(log_format)

# Get the root logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Add handlers to the logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

#logging.disable(logging.CRITICAL)

"""Load configuration from environment variables only"""
load_dotenv()  # load .env file

# -----------------------
# Leader Election Class
# -----------------------
class LeaderElection:
    def __init__(self, 
                 lease_name: str,
                 namespace: str,
                 identity: str,
                 lease_duration: int = 15,
                 renew_deadline: int = 10,
                 retry_period: int = 2):
        self.lease_name = lease_name
        self.namespace = namespace
        self.identity = identity
        self.lease_duration = lease_duration
        self.renew_deadline = renew_deadline
        self.retry_period = retry_period
        self.is_leader = False
        self.shutdown_event = asyncio.Event()
        self.lease_resource_version = None  # Track lease version for optimistic concurrency
        
        if not KUBERNETES_AVAILABLE:
            logging.warning("Kubernetes not available - running without leader election")
            self.is_leader = True  # Default to leader if no K8s
            return
            
        # Initialize Kubernetes client
        try:
            config.load_incluster_config()  # For running inside cluster
        except:
            try:
                config.load_kube_config()  # For local testing
            except:
                logging.error("Could not load Kubernetes config")
                self.is_leader = True  # Fallback to leader
                return
                
        self.coordination_client = client.CoordinationV1Api()
        
    async def start_leader_election(self):
        """Start the leader election process"""
        if not KUBERNETES_AVAILABLE:
            return
            
        logging.info(f"Starting leader election for {self.identity}")
        
        while not self.shutdown_event.is_set():
            try:
                await self._try_acquire_or_renew_lease()
                
                # Dynamic retry period: faster when not leader, slower when leader
                if self.is_leader:
                    # When we are leader, renew every retry_period seconds
                    sleep_time = self.retry_period
                else:
                    # When we are not leader, check more frequently for faster takeover
                    sleep_time = min(self.retry_period, 1)  # Check every 1 second when not leader
                
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=sleep_time)
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Normal timeout, continue the loop
                    
            except Exception as e:
                logging.error(f"Leader election error: {e}")
                self.is_leader = False
                await asyncio.sleep(self.retry_period)
        
        # Graceful shutdown: release the lease if we are the leader
        if self.is_leader:
            await self._release_lease()
            
    async def shutdown(self):
        """Signal the leader election to shutdown gracefully"""
        logging.info("Shutting down leader election...")
        self.shutdown_event.set()
    
    async def _try_acquire_or_renew_lease(self):
        """Try to acquire or renew the lease"""
        try:
            # Try to get existing lease
            lease = self.coordination_client.read_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace
            )
            
            # Store resource version for optimistic concurrency control
            self.lease_resource_version = lease.metadata.resource_version
            
            # Check if we are the current leader
            if lease.spec.holder_identity == self.identity:
                # We are the leader, renew the lease
                await self._renew_lease(lease)
            else:
                # Check if lease is expired
                if self._is_lease_expired(lease):
                    # Try to acquire the lease
                    logging.info(f"Attempting to acquire expired lease from {lease.spec.holder_identity}")
                    await self._acquire_lease(lease)
                else:
                    # Someone else is the leader and lease is still valid
                    if self.is_leader:
                        logging.info(f"Lost leadership to {lease.spec.holder_identity}")
                        self.is_leader = False
                        
        except ApiException as e:
            if e.status == 404:
                # Lease doesn't exist, create it
                logging.info("Lease not found, creating new lease")
                await self._create_lease()
            else:
                raise e
    
    async def _create_lease(self):
        """Create a new lease and become the leader"""
        lease_body = client.V1Lease(
            metadata=client.V1ObjectMeta(name=self.lease_name),
            spec=client.V1LeaseSpec(
                holder_identity=self.identity,
                lease_duration_seconds=self.lease_duration,
                acquire_time=self._current_time(),
                renew_time=self._current_time()
            )
        )
        
        try:
            self.coordination_client.create_namespaced_lease(
                namespace=self.namespace,
                body=lease_body
            )
            self.is_leader = True
            logging.info(f"Acquired leadership with new lease")
        except ApiException as e:
            if e.status == 409:  # Conflict - someone else created it
                logging.info("Lease was created by another instance")
            else:
                raise e
    
    async def _acquire_lease(self, lease):
        """Try to acquire an existing lease"""
        lease.spec.holder_identity = self.identity
        lease.spec.acquire_time = self._current_time()
        lease.spec.renew_time = self._current_time()
        
        try:
            self.coordination_client.replace_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace,
                body=lease
            )
            self.is_leader = True
            logging.info(f"Acquired leadership from previous holder")
        except ApiException as e:
            if e.status == 409:  # Conflict - someone else updated it
                logging.debug("Lease was updated by another instance during acquisition")
            else:
                raise e
    
    async def _release_lease(self):
        """Release the lease when shutting down gracefully"""
        if not self.is_leader:
            return
            
        try:
            # Get current lease
            lease = self.coordination_client.read_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace
            )
            
            # Only release if we are still the holder
            if lease.spec.holder_identity == self.identity:
                # Set lease as expired by setting renew_time to past
                from datetime import datetime, timezone, timedelta
                past_time = datetime.now(timezone.utc) - timedelta(seconds=self.lease_duration + 1)
                lease.spec.renew_time = past_time
                
                self.coordination_client.replace_namespaced_lease(
                    name=self.lease_name,
                    namespace=self.namespace,
                    body=lease
                )
                
                logging.info("Released leadership lease for graceful shutdown")
                self.is_leader = False
                
        except Exception as e:
            logging.warning(f"Could not release lease during shutdown: {e}")
            # Not critical - the lease will expire naturally
    
    async def _renew_lease(self, lease):
        """Renew the lease we currently hold"""
        lease.spec.renew_time = self._current_time()
        
        try:
            self.coordination_client.replace_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace,
                body=lease
            )
            if not self.is_leader:
                logging.info("Renewed leadership")
                self.is_leader = True
        except ApiException as e:
            if e.status == 409:  # Conflict - we lost the lease
                logging.info("Lost leadership during renewal")
                self.is_leader = False
            else:
                raise e
    
    def _is_lease_expired(self, lease):
        """Check if a lease is expired"""
        if not lease.spec.renew_time:
            return True
            
        renew_time = lease.spec.renew_time.timestamp()
        current_time = time.time()
        
        return (current_time - renew_time) > self.lease_duration
    
    def _current_time(self):
        """Get current time in the format expected by Kubernetes"""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

# -----------------------
# Global Publisher Queue and Health Status
# -----------------------
publisher_queue = asyncio.Queue()
health_status = {
    "healthy": True,
    "last_message_time": None,
    "leader_status": False
}

# -----------------------
# ZMQ PUSH Endpoint
# -----------------------
ZMQ_PUSH_ENDPOINT = os.getenv('ZMQ_PUSH_ENDPOINT', "tcp://127.0.0.1:6001")  # Connect to proxy's PULL socket

# -----------------------
# Exchange Name
# -----------------------
EXCHANGE_NAME = os.getenv('EXCHANGE_NAME', "bingx")

# -----------------------
# Exchange Endpoints & Subscription Messages
# -----------------------

# Bingx configuration
URL_BINGX = os.getenv('URL_BINGX', "wss://open-api-swap.bingx.com/swap-market")
URL_BINGX_SPOT = os.getenv('URL_BINGX_SPOT', "wss://open-api-ws.bingx.com/market")
#URL_BINGX = os.getenv('URL_BINGX', "wss://open-api.bingx.com/")
BINGX_PAIRS = os.getenv('TRADING_PAIRS', "ETH-USDT").split(',')
BINGX_SPOT_PAIRS = os.getenv('BINGX_SPOT_PAIRS', '').split(',') if os.getenv('BINGX_SPOT_PAIRS') else []
CHANNELS_BINGX = [
    {
        "id": str(uuid.uuid4()),
        "reqType": "sub",
        "dataType": f"{pair}@lastPrice"  # Dynamic pair subscription
    } for pair in BINGX_PAIRS
]

# Bingx Spot subscription messages - corrected format
CHANNELS_BINGX_SPOT = [
    {
        "id": str(uuid.uuid4()),
        "reqType": "sub",
        "dataType": f"{pair.strip()}@lastPrice"
    } for pair in BINGX_SPOT_PAIRS if pair.strip()
]

# Binance configuration
BINANCE_PAIRS = os.getenv('TRADING_PAIRS', "dogeusdt@trade")
URL_BINANCE = f"wss://stream.binance.com:9443/stream?streams={BINANCE_PAIRS}"

# OKX configuration
URL_OKX = os.getenv('URL_OKX', "wss://ws.okx.com:8443/ws/v5/public")
OKX_SUBSCRIPTION_MESSAGE = {
    "op": "subscribe",
    "args": [{"channel": "tickers", "instId": "DOGE-USDT"}]
}

# CEX configuration
URL_CEX = os.getenv('URL_CEX', "wss://ws.cex.io/ws/")
CEX_SUBSCRIPTION_MESSAGE = {"e": "subscribe", "rooms": ["ticker"]}

# -----------------------
# ZMQ Publisher Loop (Async)
# -----------------------
async def publisher_loop(leader_election=None):
    context = zmq.asyncio.Context.instance()
    pusher = context.socket(zmq.PUSH)
    try:
        pusher.connect(ZMQ_PUSH_ENDPOINT)  # Connect to proxy's PULL socket
        logging.info(f"Pusher connected to {ZMQ_PUSH_ENDPOINT}")
        while True:
            msg = await publisher_queue.get()
            
            # Update health status
            health_status["last_message_time"] = time.time()
            health_status["leader_status"] = leader_election.is_leader if leader_election else True
            
            # Only push if we are the leader (or if leader election is disabled)
            if leader_election is None or leader_election.is_leader:
                await pusher.send_string(msg)
                logging.info(f"{msg}")  # Changed from debug to info for testing
            else:
                # We are not the leader, just consume the message but don't push
                logging.debug(f"Follower - not pushing: {msg}")
                
    except asyncio.CancelledError:
        logging.info("pusher loop shutting down...")
    finally:
        pusher.close()
        context.term()
        logging.info("pusher resources cleaned up")

# -----------------------
# Bingx Client (Async)
# -----------------------
async def bingx_client():
    while True:
        try:
            async with websockets.connect(URL_BINGX, ping_interval=30, ping_timeout=10) as ws:
                logging.info("Bingx websocket connected")
                # Send subscription messages
                for channel in CHANNELS_BINGX:
                    sub_str = json.dumps(channel)
                    await ws.send(sub_str)
                    logging.info(f"Bingx subscribed to: {sub_str}")
                async for message in ws:
                    try:
                        # Bingx sends gzip-compressed messages
                        utf8_data = zlib.decompress(message, zlib.MAX_WBITS | 16).decode('utf-8')
                    except Exception as decomp_err:
                        logging.error(f"Bingx decompress error: {decomp_err}")
                        continue
                    # Handle Ping/Pong
                    if utf8_data == "Ping":
                        await ws.send("Pong")
                        continue
                    try:
                        data = json.loads(utf8_data)
                        data_payload = data.get('data')
                        if not data_payload or not isinstance(data_payload, dict):
                            logging.warning(f"Bingx received message without valid 'data': {data}")
                            continue
                        symbol = data_payload.get('s')
                        price = data_payload.get('c')
                        event_time = data_payload.get('E')
                        if symbol and price and event_time:
                            output = f"BINGX {symbol} {price} {event_time}"
                            await publisher_queue.put(output)
                            #logging.info(output)
                        else:
                            logging.warning(f"Bingx incomplete data: {data_payload}")
                    except json.JSONDecodeError as json_err:
                        logging.error(f"Bingx JSON decode error: {json_err}")
        except asyncio.CancelledError:
            logging.info("Bingx client received cancellation signal")
            raise
        except Exception as e:
            logging.error(f"Bingx connection error: {e}")
            await asyncio.sleep(1)

# -----------------------
# Bingx Spot Client (Async)
# -----------------------
async def bingx_spot_client():
    while True:
        try:
            async with websockets.connect(URL_BINGX_SPOT, ping_interval=30, ping_timeout=10) as ws:
                logging.info("Bingx spot websocket connected")
                # Send subscription messages
                for channel in CHANNELS_BINGX_SPOT:
                    sub_str = json.dumps(channel)
                    await ws.send(sub_str)
                    logging.info(f"Bingx spot subscribed to: {sub_str}")
                async for message in ws:
                    try:
                        # Handle both binary (gzipped) and text messages
                        if isinstance(message, bytes):
                            # Decompress gzipped message
                            try:
                                compressed_data = gzip.GzipFile(fileobj=io.BytesIO(message), mode='rb')
                                decompressed_data = compressed_data.read()
                                message_text = decompressed_data.decode('utf-8')
                            except (gzip.BadGzipFile, UnicodeDecodeError):
                                # If not gzipped, try to decode as utf-8 directly
                                message_text = message.decode('utf-8')
                        else:
                            message_text = message
                        
                        # Handle ping/pong for BingX spot
                        if "ping" in message_text.lower():
                            await ws.send("pong")
                            continue
                            
                        data = json.loads(message_text)
                        
                        # Handle subscription confirmation messages
                        if data.get('code') == 0 and 'data' not in data:
                            logging.debug(f"Bingx spot subscription confirmed: {data}")
                            continue
                        
                        # Check if it's a price update message
                        if data.get('code') == 0 and 'data' in data:
                            data_payload = data.get('data')
                            if not data_payload or not isinstance(data_payload, dict):
                                logging.warning(f"Bingx spot received message without valid 'data': {data}")
                                continue
                            
                            symbol = data_payload.get('s')
                            price = data_payload.get('c')  # Close price (last price)
                            event_type = data_payload.get('e')
                            
                            # Only process valid price updates
                            if symbol and price and (event_type == 'lastPriceUpdate' or not event_type):
                                # Use current timestamp since spot API might not provide event time
                                event_time = data_payload.get('E') or int(time.time() * 1000)
                                output = f"BINGX {symbol} {price} {event_time}"
                                await publisher_queue.put(output)
                                logging.debug(f"Bingx spot price: {output}")
                            else:
                                logging.debug(f"Bingx spot incomplete or non-price data: {data_payload}")
                        else:
                            logging.debug(f"Bingx spot non-success message: {data}")
                            
                    except json.JSONDecodeError as json_err:
                        logging.error(f"Bingx spot JSON decode error: {json_err}, message: {message_text[:200] if 'message_text' in locals() else message}")
                    except Exception as msg_err:
                        logging.error(f"Bingx spot message error: {msg_err}")
        except asyncio.CancelledError:
            logging.info("Bingx spot client received cancellation signal")
            raise
        except Exception as e:
            logging.error(f"Bingx spot connection error: {e}")
            await asyncio.sleep(1)

# -----------------------
# Binance Client with Duplicate Filtering (Async)
# -----------------------
async def binance_client():
    last_prices = {}
    while True:
        try:
            async with websockets.connect(URL_BINANCE) as ws:
                logging.info("Binance websocket connected")
                async for message in ws:
                    try:
                        data = json.loads(message)
                        if 'data' in data:
                            trade_data = data['data']
                            symbol = trade_data.get('s')
                            price = trade_data.get('p')
                            event_time = trade_data.get('E')
                            if symbol and price and event_time:
                                if symbol in last_prices and last_prices[symbol] == price:
                                    continue  # Skip duplicate
                                last_prices[symbol] = price
                                output = f"BINANCE {symbol} {price} {event_time}"
                                await publisher_queue.put(output)
                                #logging.info(output)
                    except Exception as msg_err:
                        logging.error(f"Binance message error: {msg_err}")
        except asyncio.CancelledError:
            logging.info("Binance client received cancellation signal")
            raise
        except Exception as conn_err:
            logging.error(f"Binance connection error: {conn_err}")
            await asyncio.sleep(1)

# -----------------------
# OKX Client (Async)
# -----------------------
async def okx_client():
    while True:
        try:
            async with websockets.connect(URL_OKX, ping_interval=30, ping_timeout=10) as ws:
                logging.info("OKX websocket connected")
                sub_str = json.dumps(OKX_SUBSCRIPTION_MESSAGE)
                await ws.send(sub_str)
                logging.info(f"OKX subscribed to: {sub_str}")
                async for message in ws:
                    try:
                        data = json.loads(message)
                        # Handle ping messages
                        if "ping" in data:
                            pong_msg = json.dumps({"pong": data["ping"]})
                            await ws.send(pong_msg)
                            logging.debug(f"OKX Pong: {pong_msg}")
                            continue
                        if "event" in data:
                            logging.info(f"OKX event: {data}")
                            continue
                        if "data" in data:
                            for item in data["data"]:
                                symbol = item.get("instId")
                                price = item.get("last")
                                timestamp = item.get("ts")
                                if symbol and price and timestamp:
                                    output = f"OKX {symbol} {price} {timestamp}"
                                    await publisher_queue.put(output)
                                    logging.info(output)
                                else:
                                    logging.info(f"OKX incomplete data: {item}")
                        else:
                            logging.debug(f"OKX unrecognized message: {data}")
                    except Exception as msg_err:
                        logging.error(f"OKX message error: {msg_err}")
        except asyncio.CancelledError:
            logging.info("OKX client received cancellation signal")
            raise
        except Exception as conn_err:
            logging.error(f"OKX connection error: {conn_err}")
            await asyncio.sleep(1)

# -----------------------
# CEX Client (Async)
# -----------------------
async def cex_client():
    while True:
        try:
            async with websockets.connect(URL_CEX) as ws:
                logging.info("CEX websocket connected")
                sub_str = json.dumps(CEX_SUBSCRIPTION_MESSAGE)
                await ws.send(sub_str)
                logging.info(f"CEX subscribed with: {sub_str}")
                async for message in ws:
                    try:
                        data = json.loads(message)
                        # Handle ping if sent by CEX
                        if data.get("e") == "ping":
                            pong_msg = json.dumps({"e": "pong", "data": data.get("data", "")})
                            await ws.send(pong_msg)
                            logging.debug(f"CEX Pong: {pong_msg}")
                            continue
                        if data.get("room") == "ticker" and "data" in data:
                            ticker_data = data["data"]
                            if ticker_data.get("pair") == "BTCUSD":
                                price = ticker_data.get("last")
                                timestamp = ticker_data.get("timestamp")
                                if price and timestamp:
                                    output = f"CEX BTC {price} {timestamp}"
                                    await publisher_queue.put(output)
                                    logging.info(output)
                                else:
                                    logging.info(f"CEX incomplete data: {ticker_data}")
                    except Exception as msg_err:
                        logging.error(f"CEX message error: {msg_err}")
        except asyncio.CancelledError:
            logging.info("CEX client received cancellation signal")
            raise
        except Exception as conn_err:
            logging.error(f"CEX connection error: {conn_err}")
            await asyncio.sleep(1)

# -----------------------
# Shutdown Handler
# -----------------------
async def shutdown(signal, loop, tasks, leader_election=None):
    """Cancel all running tasks"""
    logging.info(f"Received exit signal {signal.name}...")
    
    # Gracefully shutdown leader election first if it exists
    if leader_election:
        await leader_election.shutdown()
    
    # Cancel all tasks
    for task in tasks:
        task.cancel()
    
    # Wait for tasks to complete their cancellation
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

# -----------------------
# Main Application
# -----------------------
async def main():
    # Check if leader election is enabled
    leader_election_enabled = os.getenv('LEADER_ELECTION_ENABLED', 'false').lower() == 'true'
    leader_election = None
    
    if leader_election_enabled and KUBERNETES_AVAILABLE:
        # Initialize leader election
        lease_name = os.getenv('LEADER_ELECTION_LEASE_NAME', 'bingx-publisher-leader')
        namespace = os.getenv('LEADER_ELECTION_NAMESPACE', 'martingale')
        identity = os.getenv('HOSTNAME', str(uuid.uuid4()))  # Use pod name as identity
        lease_duration = int(os.getenv('LEADER_ELECTION_LEASE_DURATION', '15'))
        renew_deadline = int(os.getenv('LEADER_ELECTION_RENEW_DEADLINE', '10'))
        retry_period = int(os.getenv('LEADER_ELECTION_RETRY_PERIOD', '2'))
        
        leader_election = LeaderElection(
            lease_name=lease_name,
            namespace=namespace,
            identity=identity,
            lease_duration=lease_duration,
            renew_deadline=renew_deadline,
            retry_period=retry_period
        )
        
        logging.info(f"Leader election enabled for pod: {identity}")
    else:
        logging.info("Leader election disabled - running as single instance")
    
    # Create tasks based on exchange
    tasks = []
    
    # Add leader election task if enabled
    if leader_election:
        tasks.append(asyncio.create_task(leader_election.start_leader_election(), name="leader_election"))
    
    if EXCHANGE_NAME == "bingx":
        tasks.extend([
            asyncio.create_task(publisher_loop(leader_election), name="publisher"),
            asyncio.create_task(bingx_client(), name="bingx_swap_client"),
        ])
        
        # Add spot client if spot pairs are defined
        if BINGX_SPOT_PAIRS:
            tasks.append(asyncio.create_task(bingx_spot_client(), name="bingx_spot_client"))
            logging.info(f"Bingx spot client enabled for pairs: {', '.join(BINGX_SPOT_PAIRS)}")
        else:
            logging.info("No spot pairs defined - spot client disabled")
    elif EXCHANGE_NAME == "binance":
        tasks.extend([
            asyncio.create_task(publisher_loop(leader_election), name="publisher"),
            asyncio.create_task(binance_client(), name="binance_client"),
        ])
    elif EXCHANGE_NAME == "okx":
        tasks.extend([
            asyncio.create_task(publisher_loop(leader_election), name="publisher"),
            asyncio.create_task(okx_client(), name="okx_client"),
        ])
    elif EXCHANGE_NAME == "cex":
        tasks.extend([
            asyncio.create_task(publisher_loop(leader_election), name="publisher"),
            asyncio.create_task(cex_client(), name="cex_client"),
        ])
    else:
        raise ValueError(f"Unknown exchange: {EXCHANGE_NAME}")
    
    return tasks, leader_election

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    
    try:
        # Create tasks and get leader_election object
        tasks, leader_election = loop.run_until_complete(main())
        
        # Setup signal handlers - CORRECTED VERSION
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(
                s,
                lambda s=s: asyncio.create_task(shutdown(s, loop, tasks, leader_election))
            )
        
        loop.run_forever()
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
    finally:
        loop.close()
        logging.info("Successfully shutdown the service.")