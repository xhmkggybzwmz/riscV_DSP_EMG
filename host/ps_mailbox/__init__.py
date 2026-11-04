"""PS-side reader of the E4a RV32I -> PS result mailbox (docs/soc/e4a_mailbox.md).

protocol     layout and rules of mailbox v1 (single source of the generated constants)
registers    MMIO backends (pynq.MMIO, /dev/mem) with window bounds / alignment checks
pynq_reader  MailboxReader (consumer), board_guard, to_json
overlay_device  PynqMailboxDevice: Overlay(download=False) HWH metadata + checks, pynq.MMIO windows
golden       E2 golden decisions for checking records on the board
fake_mmio    software mailbox and producer model for tests on the PC
cli          command line reader (JSON Lines)
board_e4b    E4a-B board test controller (KV260, root)
"""
