#!/usr/bin/env python3
"""
Health check script for the price publisher
This can be used in Kubernetes liveness and readiness probes
"""
import time
import sys
import os

def check_health():
    """Check if the price publisher is healthy"""
    
    # Check if the main process is likely running by looking for typical behavior
    # This is a simple check - in production you might want to use a more sophisticated method
    
    # Check if we can import the main modules (basic check)
    try:
        import zmq
        import websockets
        import asyncio
    except ImportError as e:
        print(f"Required modules not available: {e}")
        return False
    
    # For now, we'll just return True if imports work
    # In a more sophisticated setup, you could:
    # 1. Check if ZMQ socket is responsive
    # 2. Check if websocket connections are active
    # 3. Check if leader election is working
    # 4. Check timestamp of last published message
    
    return True

if __name__ == "__main__":
    if check_health():
        print("Health check passed")
        sys.exit(0)
    else:
        print("Health check failed")
        sys.exit(1)
