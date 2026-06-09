#!/usr/bin/env python3
"""Take a full-page screenshot of http://127.0.0.1:5001 via CDP."""

import json
import os
import base64
import time
import ssl
from websocket import create_connection

CDP_URL = "ws://127.0.0.1:18800/devtools/browser/bdbe0103-adf7-4bdb-8b58-b5790e811429"
TARGET_URL = "http://127.0.0.1:5001"
OUTPUT_PATH = "/Users/pmains/Code/openclaw/maricopa-agendas/logs/navbar-logo-test.png"

def send(ws, method, params=None):
    """Send a CDP command and return the result."""
    msg_id = 1
    # Re-use a simple incrementing ID scheme per session
    if not hasattr(send, 'counter'):
        send.counter = 0
    send.counter += 1
    msg_id = send.counter
    cmd = {"id": msg_id, "method": method}
    if params:
        cmd["params"] = params
    ws.send(json.dumps(cmd))
    
    # Read responses until we get our id
    while True:
        response = json.loads(ws.recv())
        if response.get("id") == msg_id:
            return response.get("result")
        elif "method" in response:
            # It's an event; ignore for now
            continue

def main():
    ws = create_connection(CDP_URL, timeout=30, suppress_origin=True)
    
    # 1. Create a new target (tab)
    result = send(ws, "Target.createTarget", {
        "url": "about:blank",
        "newWindow": False
    })
    target_id = result["targetId"]
    print(f"Created target: {target_id}")
    
    # 2. Get the page's websocket URL
    targets_result = send(ws, "Target.getTargets")
    page_ws_url = None
    for t in targets_result.get("targetInfos", []):
        if t["targetId"] == target_id and t["type"] == "page":
            page_ws_url = t["targetId"]
            break
    
    # 3. Attach to the target to get a session
    result = send(ws, "Target.attachToTarget", {
        "targetId": target_id,
        "flatten": True
    })
    session_id = result.get("sessionId")
    print(f"Session ID: {session_id}")
    
    # We'll use Target.sendMessageToTarget for session-scoped commands
    def send_session(method, params=None):
        send.counter += 1
        msg_id = send.counter
        cmd = {
            "id": msg_id,
            "method": "Target.sendMessageToTarget",
            "params": {
                "sessionId": session_id,
                "message": json.dumps({"id": msg_id, "method": method, "params": params or {}})
            }
        }
        ws.send(json.dumps(cmd))
        # Read responses until we get our id
        while True:
            response = json.loads(ws.recv())
            if response.get("id") == msg_id:
                inner = json.loads(response.get("result", {}).get("message", "{}"))
                if "result" in inner:
                    return inner["result"]
                elif "error" in inner:
                    raise Exception(f"CDP error: {inner['error']}")
                return inner
            elif "method" in response:
                continue
    
    # 4. Navigate to the target URL
    print(f"Navigating to {TARGET_URL}...")
    send.counter += 1
    nav_id = send.counter
    cmd = {
        "id": nav_id,
        "method": "Target.sendMessageToTarget",
        "params": {
            "sessionId": session_id,
            "message": json.dumps({"id": nav_id, "method": "Page.navigate", "params": {"url": TARGET_URL}})
        }
    }
    ws.send(json.dumps(cmd))
    
    # Wait for navigation response
    while True:
        response = json.loads(ws.recv())
        if response.get("id") == nav_id:
            inner = json.loads(response.get("result", {}).get("message", "{}"))
            print(f"Navigation result: {json.dumps(inner, indent=2)[:200]}")
            break
        elif "method" in response:
            continue
    
    # 5. Wait for page to fully load
    print("Waiting for page load...")
    time.sleep(3)
    
    # 6. Enable Page domain
    send.counter += 1
    enable_id = send.counter
    cmd = {
        "id": enable_id,
        "method": "Target.sendMessageToTarget",
        "params": {
            "sessionId": session_id,
            "message": json.dumps({"id": enable_id, "method": "Page.enable", "params": {}})
        }
    }
    ws.send(json.dumps(cmd))
    while True:
        response = json.loads(ws.recv())
        if response.get("id") == enable_id:
            break
    
    # 7. Get layout metrics for full page size
    send.counter += 1
    metrics_id = send.counter
    cmd = {
        "id": metrics_id,
        "method": "Target.sendMessageToTarget",
        "params": {
            "sessionId": session_id,
            "message": json.dumps({"id": metrics_id, "method": "Page.getLayoutMetrics", "params": {}})
        }
    }
    ws.send(json.dumps(cmd))
    metrics = None
    while True:
        response = json.loads(ws.recv())
        if response.get("id") == metrics_id:
            inner_str = response.get("result", {}).get("message", "{}")
            inner = json.loads(inner_str)
            if "result" in inner:
                metrics = inner["result"]
            elif "error" in inner:
                # Try page.getLayoutMetrics via direct attach
                print(f"Error getting metrics via session: {inner['error']}")
            break
    
    # If session-based didn't work, try using Page domain directly
    if not metrics:
        # Try a different approach - direct page attachment
        ws.close()
        
        # Get the page-specific websocket URL
        ws2 = create_connection(CDP_URL, timeout=30, suppress_origin=True)
        targets = send(ws2, "Target.getTargets")
        
        # Find our page
        page_info = None
        for t in targets.get("targetInfos", []):
            if t["targetId"] == target_id:
                page_info = t
                break
        
        # Connect directly to the page
        result = send(ws2, "Target.attachToTarget", {
            "targetId": target_id,
            "flatten": True
        })
        sess_id = result.get("sessionId")
        
        # Navigate via flat session
        def flat_send(method, params=None):
            send.counter += 1
            mid = send.counter
            ws2.send(json.dumps({
                "id": mid,
                "sessionId": sess_id,
                "method": method,
                "params": params or {}
            }))
            while True:
                resp = json.loads(ws2.recv())
                if resp.get("id") == mid:
                    return resp.get("result")
        
        result = flat_send("Page.navigate", {"url": TARGET_URL})
        print(f"Navigate result: {result}")
        time.sleep(3)
        
        result = flat_send("Page.enable")
        print(f"Page.enable: {result}")
        
        metrics = flat_send("Page.getLayoutMetrics")
        print(f"Layout metrics: {json.dumps(metrics, indent=2)[:300]}")
        
        if metrics:
            css_content_size = metrics.get("cssContentSize", {})
            width = int(css_content_size.get("width", 1280))
            height = int(css_content_size.get("height", 900))
            print(f"Full page size: {width}x{height}")
            
            # Set viewport to full page size
            flat_send("Emulation.setDeviceMetricsOverride", {
                "width": width,
                "height": height,
                "deviceScaleFactor": 2,
                "mobile": False
            })
            time.sleep(1)
            
            # Take screenshot
            result = flat_send("Page.captureScreenshot", {
                "format": "png",
                "clip": {
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": height,
                    "scale": 1
                },
                "fromSurface": True
            })
            
            if result and "data" in result:
                img_data = base64.b64decode(result["data"])
                os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
                with open(OUTPUT_PATH, "wb") as f:
                    f.write(img_data)
                print(f"Screenshot saved to {OUTPUT_PATH} ({len(img_data)} bytes)")
            else:
                print(f"No screenshot data in result: {result}")
        
        ws2.close()
        return
    
    ws.close()

if __name__ == "__main__":
    main()
