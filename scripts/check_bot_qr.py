"""The bot log drain must lift a pairing QR out of the stream, not log it.

Before this, a QR line went into the 40-line ring buffer and the dashboard's
own "last 12 lines" window dropped it — the code was emitted and never shown,
which is why pairing looked broken.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bot_qr.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.bots import manager  # noqa: E402

fails = []

def check(label, ok, detail=""):
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        fails.append(label)

class FakeStream:
    """A subprocess stdout stand-in, line by line."""
    def __init__(self, lines):
        self._lines = list(lines)
    async def readline(self):
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\n").encode("utf-8")

PAIRING = "2@AbCdEf1234567890+/xyz==,pairing-payload-that-is-fairly-long"

async def main():
    print("A QR line is captured, not left in the log")
    manager._qr.pop("whatsapp", None)
    manager._logs["whatsapp"].clear()
    stream = FakeStream([
        "[WhatsApp] Connected to Addled backend",
        "[WhatsApp] Scan this QR code with WhatsApp (Linked Devices).",
        f"{manager.QR_PREFIX}{PAIRING}",
        "[WhatsApp] some later line",
    ])
    await manager._drain("whatsapp", stream)

    check("the QR payload is kept", manager._qr.get("whatsapp") == PAIRING,
          repr(manager._qr.get("whatsapp"))[:80])
    log = list(manager._logs["whatsapp"])
    check("the QR line is NOT in the log",
          not any(PAIRING in line for line in log), str(log)[:160])
    check("ordinary lines are still logged",
          any("Connected to Addled backend" in x for x in log), str(log)[:160])
    check("the line after the QR is still logged",
          any("some later line" in x for x in log), str(log)[:160])

    print("\nA rotated code replaces the previous one")
    stream2 = FakeStream([f"{manager.QR_PREFIX}SECOND-CODE"])
    await manager._drain("whatsapp", stream2)
    check("the newest QR wins", manager._qr.get("whatsapp") == "SECOND-CODE",
          repr(manager._qr.get("whatsapp")))

    print("\nAn empty payload does not wipe a good code")
    manager._qr["whatsapp"] = "GOOD"
    await manager._drain("whatsapp", FakeStream([f"{manager.QR_PREFIX}   "]))
    check("a blank QR line is ignored", manager._qr.get("whatsapp") == "GOOD",
          repr(manager._qr.get("whatsapp")))

    print("\nstatus() reports the field the dashboard reads")
    info = manager.status()["platforms"]["whatsapp"]
    check("status carries 'qr'", "qr" in info, str(sorted(info)))
    check("status carries 'running'", "running" in info)
    check("status carries 'log'", "log" in info)

    print("\nA QR does not survive a stop")
    manager._qr["whatsapp"] = "STALE"
    await manager.stop("whatsapp")
    check("stopping clears the code", manager._qr.get("whatsapp") is None,
          repr(manager._qr.get("whatsapp")))

    print("\nThe bot script no longer relies on the removed option")
    js = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bots", "whatsapp-bot.js"),
        encoding="utf-8").read()
    check("printQRInTerminal is gone from the socket options",
          "printQRInTerminal: true" not in js,
          "Baileys 6.x ignores it and prints a deprecation warning instead")
    check("the code still reads qr from connection.update",
          "const { connection, lastDisconnect, qr } = update" in js)
    check("and still emits the marker the backend looks for",
          "QR_DATA:" in js and manager.QR_PREFIX == "QR_DATA:")

    print("\nThe dashboard renders it")
    page = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "dashboard", "src", "app", "bots",
        "page.tsx"), encoding="utf-8").read()
    check("QRCodeSVG is imported", "from 'qrcode.react'" in page)
    check("and rendered from bot.qr", "<QRCodeSVG value={bot.qr}" in page,
          "the payload would arrive and never be drawn")
    check("the type carries qr", "qr: string;" in page)
    check("a stale code is not offered as scannable",
          "qr" in page and "Waiting to be scanned" in page)

    print()
    if fails:
        print(f"{len(fails)} FAILED")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("All bot-QR checks passed.")

asyncio.run(main())
