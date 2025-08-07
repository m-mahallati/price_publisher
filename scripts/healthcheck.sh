#!/bin/bash

# Health check for ZMQ Proxy container

# Configuration
METRICS_URL="http://localhost:8000"
FRONTEND_PORT=5545
BACKEND_PORT=5546
TIMEOUT=5

# Check metrics endpoint
curl -sSf --max-time $TIMEOUT "$METRICS_URL/metrics" > /dev/null 2>&1 || {
    echo "Metrics endpoint not responding"
    exit 1
}

# Check ZMQ ports
check_port() {
    local port=$1
    nc -z -w $TIMEOUT localhost $port || {
        echo "Port $port not responding"
        exit 1
    }
}

check_port $FRONTEND_PORT
check_port $BACKEND_PORT

# Simple ZMQ health check
python3 -c "
import zmq, sys
ctx = zmq.Context()
try:
    # Test frontend connection
    s = ctx.socket(zmq.SUB)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 1000)
    s.connect('tcp://localhost:$FRONTEND_PORT')
    s.close()
    
    # Test backend connection
    s = ctx.socket(zmq.PUSH)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.SNDTIMEO, 1000)
    s.connect('tcp://localhost:$BACKEND_PORT') 
    s.close()
    
    sys.exit(0)
except Exception as e:
    print(f'ZMQ healthcheck failed: {str(e)}')
    sys.exit(1)
" || exit 1

exit 0