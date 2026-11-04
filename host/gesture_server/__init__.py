"""E4b-A gesture server: one mailbox consumer, WebSocket fan-out to browsers
(docs/system/e4b_visualization.md).

protocol     WebSocket message schema v1 (hello / result / status), validation
source       HardwareSource (PYNQ Overlay(download=False) + pynq.MMIO + MailboxReader),
             SyntheticSource (synthetic_demo, via the mailbox model), ReplaySource (test_fixture)
broadcaster  thread -> asyncio handoff, per-client bounded queues, drop counters
server       GestureServer: HTTP (web UI, /status.json) + WebSocket /ws, lifetime / shutdown
cli          command line (python -m gesture_server.cli, run from host/)
"""
