#!/usr/bin/env python3
"""
Enterprise-grade ZMQ Proxy with Enhanced Monitoring and Reliability
"""

import zmq
import threading
import logging
import signal
import json
import time
import psutil
from typing import Optional
from dataclasses import dataclass
import argparse
import sys
from prometheus_client import start_http_server, Counter, Gauge, Histogram, Enum

# --------------------------
# Configuration Management
# --------------------------

@dataclass
class ProxyConfig:
    frontend_port: int = 5545          # PUB port for subscribers (fixed)
    backend_pull_port: int = 5546      # PULL port for backend workers (fixed)
    monitoring_port: int = 8000
    max_workers: int = 1
    receive_timeout_ms: int = 1000
    high_water_mark: int = 1000
    log_level: str = "INFO"
    config_file: Optional[str] = None
    linger_ms: int = 1000
    enable_compression: bool = False
    max_message_size: int = 10 * 1024 * 1024  # 10MB
    
    def validate(self):
        """Validate configuration parameters"""
        if not (1024 <= self.frontend_port <= 65535):
            raise ValueError(f"Invalid frontend port: {self.frontend_port}")
        if not (1024 <= self.backend_pull_port <= 65535):
            raise ValueError(f"Invalid backend pull port: {self.backend_pull_port}")
        if not (1024 <= self.monitoring_port <= 65535):
            raise ValueError(f"Invalid monitoring port: {self.monitoring_port}")
        if self.max_workers < 1:
            raise ValueError("At least one worker required")
        if self.max_message_size <= 0:
            raise ValueError("Max message size must be positive")

# --------------------------
# Metrics Collection
# --------------------------

class ProxyMetrics:
    """Enhanced metrics collection for the proxy"""
    
    def __init__(self):
        # Message metrics
        self.messages_received = Counter(
            'zmq_proxy_messages_received_total', 
            'Total messages received',
            ['source', 'message_type']
        )
        self.messages_forwarded = Counter(
            'zmq_proxy_messages_forwarded_total',
            'Total messages forwarded',
            ['destination', 'status']
        )
        self.message_errors = Counter(
            'zmq_proxy_message_errors_total',
            'Total message processing errors',
            ['error_type', 'phase']
        )
        
        # Throughput metrics
        self.message_rate = Gauge(
            'zmq_proxy_message_rate_per_second',
            'Current message processing rate (messages/sec)',
            ['direction']
        )
        self.throughput = Gauge(
            'zmq_proxy_throughput_bytes',
            'Current data throughput in bytes',
            ['direction']
        )
        
        # System metrics
        self.queue_size = Gauge(
            'zmq_proxy_queue_size',
            'Current message queue size'
        )
        self.active_connections = Gauge(
            'zmq_proxy_active_connections',
            'Current active connections'
        )
        
        # Timing metrics
        self.processing_time = Histogram(
            'zmq_proxy_processing_time_seconds',
            'Time taken to process messages',
            buckets=[0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0]
        )
        
        # Operational metrics
        self.proxy_state = Enum(
            'zmq_proxy_state',
            'Current proxy state',
            states=['starting', 'running', 'stopping', 'stopped', 'error']
        )
        self.uptime = Gauge(
            'zmq_proxy_uptime_seconds',
            'Proxy uptime in seconds'
        )
        self.health_score = Gauge(
            'zmq_proxy_health_score',
            'Composite health score (0-100)'
        )
        
        # Resource metrics
        self.memory_usage = Gauge(
            'zmq_proxy_memory_usage_bytes',
            'Memory usage in bytes'
        )
        self.cpu_usage = Gauge(
            'zmq_proxy_cpu_usage_percent',
            'CPU usage percentage'
        )
        
        # Throughput calculation variables
        self._last_update_time = time.time()
        self._last_received_count = 0
        self._last_forwarded_count = 0
        
    def update_throughput_metrics(self):
        """Calculate and update message rates and throughput"""
        now = time.time()
        time_elapsed = now - self._last_update_time
        
        if time_elapsed > 0:
            received_count = self._get_metric_count(self.messages_received)
            forwarded_count = self._get_metric_count(self.messages_forwarded)
            
            # Update message rates
            received_rate = (received_count - self._last_received_count) / time_elapsed
            forwarded_rate = (forwarded_count - self._last_forwarded_count) / time_elapsed
            
            self.message_rate.labels(direction='in').set(received_rate)
            self.message_rate.labels(direction='out').set(forwarded_rate)
        
        # Update timestamps and counters
        self._last_update_time = now
        self._last_received_count = received_count
        self._last_forwarded_count = forwarded_count
    
    def _get_metric_count(self, metric):
        """Helper method to safely get metric count"""
        try:
            # For Prometheus >= 0.14
            return next(m for m in metric.collect() if m.name == metric._name).samples[0].value
        except:
            # Fallback for older versions
            return metric._metrics.get(('', ()), 0)

# --------------------------
# ZMQ Proxy Implementation
# --------------------------

class ZMQProxy:
    """Enhanced ZMQ Proxy with monitoring and reliability features"""
    
    def __init__(self, config: ProxyConfig):
        self.config = config
        self._setup_logging()
        self._running = False
        self._setup_complete = False
        self._context = None
        self._frontend = None
        self._backend_pull = None
        self._queue_size = 0
        self._start_time = time.time()
        self.metrics = ProxyMetrics()
        
    def _setup_logging(self):
        """Configure logging based on config"""
        logging.basicConfig(
            level=self.config.log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler('zmq_proxy.log')
            ]
        )
        self.logger = logging.getLogger('ZMQProxy')
        
    def _setup_metrics(self):
        """Initialize metrics server"""
        try:
            start_http_server(self.config.monitoring_port)
            self.logger.info(f"Metrics server started on port {self.config.monitoring_port}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to start metrics server: {e}")
            return False
            
    def _setup_zmq(self):
        """Initialize ZMQ sockets and context with enhanced settings"""
        try:
            self._context = zmq.Context()
            self._context.set(zmq.MAX_SOCKETS, 65536)
            
            # Frontend (PUB socket for subscribers)
            self._frontend = self._context.socket(zmq.PUB)
            self._frontend.set(zmq.SNDHWM, self.config.high_water_mark)
            self._frontend.set(zmq.LINGER, self.config.linger_ms)
            self._frontend.set(zmq.MAXMSGSIZE, self.config.max_message_size)
            self._frontend.bind(f"tcp://*:{self.config.frontend_port}")
            
            # Backend (PULL socket for workers)
            self._backend_pull = self._context.socket(zmq.PULL)
            self._backend_pull.set(zmq.RCVHWM, self.config.high_water_mark)
            self._backend_pull.set(zmq.LINGER, self.config.linger_ms)
            self._backend_pull.set(zmq.MAXMSGSIZE, self.config.max_message_size)
            self._backend_pull.bind(f"tcp://*:{self.config.backend_pull_port}")
            
            self.logger.info(f"Frontend PUB bound to port {self.config.frontend_port}")
            self.logger.info(f"Backend PULL bound to port {self.config.backend_pull_port}")
            return True
            
        except zmq.ZMQError as e:
            self.logger.error(f"ZMQ initialization failed: {e}")
            self.metrics.proxy_state.state('error')
            return False
            
    def health_status(self):
        """Return current health status"""
        status = {
            "status": "running" if self._running else "stopped",
            "uptime": time.time() - self._start_time,
            "setup_complete": self._setup_complete,
            "frontend_connected": bool(self._frontend.get(zmq.EVENTS)) if self._frontend else False,
            "backend_connected": bool(self._backend_pull.get(zmq.EVENTS)) if self._backend_pull else False,
            "queue_size": self._queue_size
        }
        
        # Calculate health score (0-100)
        health = 100
        if not status["frontend_connected"]:
            health -= 30
        if not status["backend_connected"]:
            health -= 30
        if self._queue_size > 1000:  # Example threshold
            health -= 20
            
        self.metrics.health_score.set(health)
        return status
    
    def _process_message(self, message):
        """Process and forward a message"""
        start_time = time.time()
        try:
            # Calculate message size
            msg_size = sum(len(part) for part in message) if isinstance(message, list) else len(message)
            
            # Forward message to frontend
            self._frontend.send_multipart(message if isinstance(message, list) else [message])
            
            # Update metrics
            processing_time = time.time() - start_time
            self.metrics.processing_time.observe(processing_time)
            self.metrics.messages_forwarded.labels(destination='frontend', status='success').inc()
            self.metrics.throughput.labels(direction='out').inc(msg_size)
            
            # Log message details with timing
            self.logger.info(
                f"Forwarded message|"
                f"Size: {msg_size} bytes|"
                f"Processing Time: {processing_time:.6f}s|"
                f"Queue Size: {self._queue_size}"
            )
            return True
            
        except Exception as e:
            self.logger.error(f"Error processing message: {e}")
            self.metrics.message_errors.labels(error_type=str(type(e).__name__), phase='processing').inc()
            return False
    
    def _proxy_loop(self):
        """Main proxy loop with enhanced monitoring"""
        poller = zmq.Poller()
        poller.register(self._backend_pull, zmq.POLLIN)
        
        self.metrics.proxy_state.state('running')
        self.logger.info("Proxy main loop started")
        
        while self._running:
            try:
                # Update metrics
                now = time.time()
                self.metrics.uptime.set(now - self._start_time)
                self.health_status()
                
                # Update resource usage
                self.metrics.memory_usage.set(psutil.Process().memory_info().rss)
                self.metrics.cpu_usage.set(psutil.cpu_percent())
                
                # Update throughput metrics every second
                if now - self.metrics._last_update_time >= 1.0:
                    self.metrics.update_throughput_metrics()
                
                # Process messages
                socks = dict(poller.poll(self.config.receive_timeout_ms))
                
                if self._backend_pull in socks:
                    # Receive message
                    message = self._backend_pull.recv_multipart()
                    msg_size = sum(len(part) for part in message)
                    
                    # Update metrics
                    self._queue_size += 1
                    self.metrics.queue_size.set(self._queue_size)
                    self.metrics.messages_received.labels(source='backend', message_type='market_data').inc()
                    self.metrics.throughput.labels(direction='in').inc(msg_size)
                    
                    # Process message
                    if self._process_message(message):
                        self._queue_size -= 1
                        self.metrics.queue_size.set(self._queue_size)
                        
            except zmq.ZMQError as e:
                self.logger.error(f"ZMQ error in proxy loop: {e}")
                self.metrics.message_errors.labels(error_type='zmq', phase='polling').inc()
            except Exception as e:
                self.logger.error(f"Unexpected error in proxy loop: {e}")
                self.metrics.message_errors.labels(error_type='unexpected', phase='processing').inc()
                time.sleep(1)
                
        self.logger.info("Proxy main loop stopped")
    
    def run(self):
        """Start the proxy service with comprehensive lifecycle management"""
        try:
            self.config.validate()
            
            if not self._setup_zmq():
                raise RuntimeError("ZMQ setup failed")
                
            if not self._setup_metrics():
                raise RuntimeError("Metrics setup failed")
                
            self._setup_complete = True
            self._running = True
            
            # Start the main proxy loop
            self._proxy_loop()
            
        except KeyboardInterrupt:
            self.logger.info("Shutdown signal received")
        except Exception as e:
            self.logger.critical(f"Fatal error: {e}", exc_info=True)
            self.metrics.proxy_state.state('error')
            raise
        finally:
            self.shutdown()
            
    def shutdown(self):
        """Enhanced graceful shutdown procedure"""
        self.logger.info("Initiating shutdown sequence")
        self.metrics.proxy_state.state('stopping')
        self._running = False
        
        try:
            if self._frontend:
                self._frontend.close()
            if self._backend_pull:
                self._backend_pull.close()
            if self._context:
                self._context.term()
                
            self.metrics.proxy_state.state('stopped')
            self.logger.info("ZMQ proxy shutdown complete")
        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")
            self.metrics.proxy_state.state('error')

# --------------------------
# Main Entry Point
# --------------------------

def main():
    """Main entry point with signal handling"""
    parser = argparse.ArgumentParser(description="Enterprise ZMQ Proxy with Pull Backend")
    parser.add_argument('--config', type=str, help="Path to config file")
    args = parser.parse_args()
    
    config = ProxyConfig()
    if args.config:
        try:
            with open(args.config) as f:
                config_data = json.load(f)
                for field in config_data:
                    if hasattr(config, field):
                        setattr(config, field, config_data[field])
            config.validate()
        except Exception as e:
            logging.error(f"Config error: {e}")
            sys.exit(1)
    
    proxy = ZMQProxy(config)
    
    # Signal handling
    def handle_signal(signum, frame):
        logging.info(f"Received signal {signum}, shutting down...")
        proxy.shutdown()
        sys.exit(0)
        
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    try:
        proxy.run()
    except Exception as e:
        logging.critical(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()