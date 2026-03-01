#!/usr/bin/env python3
"""
🌊 Whisper Covenant REST API Server
Port 18803 — serves static landing page + REST API

Endpoints:
  GET  /              — landing page (index.html)
  GET  /api/status    — health check
  POST /api/send      — send whisper message (lock deposit in covenant)
  POST /api/read      — read whisper message (spend covenant, refund sender)
  GET  /api/inbox     — list pending messages for an address
  GET  /api/whisper/{tx_id} — get single covenant info
"""

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from aiohttp import web
import aiohttp_cors

# ─── Config ───────────────────────────────────────────────────────
PORT = 18803
WRPC_URL = "ws://localhost:17210"
NETWORK_ID = "testnet-12"
NETWORK_TYPE = "testnet"
DEPOSIT_SOMPI = 20_000_000      # 0.2 tKAS
FEE_SOMPI = 10_000
FEE_BUFFER_SOMPI = 5_000
NATIVE_SUBNETWORK = "00" * 20

WALLET_PATH = os.path.expanduser("~/.secrets/testnet-wallet.json")
STATIC_DIR = Path(__file__).parent
WHISPERS_DIR = STATIC_DIR / "whispers"
WHISPERS_DIR.mkdir(exist_ok=True)
API_KEY = os.environ.get("WHISPER_API_KEY", "whisper-testnet-poc-key")

# ─── Script helpers (from covenant_send.py) ───────────────────────

def push_data(data: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return bytes([0x00])
    if n <= 75:
        return bytes([n]) + data
    elif n <= 255:
        return bytes([0x4c, n]) + data
    else:
        return bytes([0x4d]) + n.to_bytes(2, "little") + data

def push_int(val: int) -> bytes:
    if val == 0:
        return bytes([0x00])
    if 1 <= val <= 16:
        return bytes([0x50 + val])
    neg = val < 0
    abs_val = abs(val)
    result = []
    while abs_val > 0:
        result.append(abs_val & 0xFF)
        abs_val >>= 8
    if result[-1] & 0x80:
        result.append(0x80 if neg else 0x00)
    elif neg:
        result[-1] |= 0x80
    return push_data(bytes(result))

def build_covenant_script(a_spk_bytes: bytes, b_pubkey: bytes, deposit: int) -> bytes:
    s = b""
    s += push_data(a_spk_bytes)
    s += push_int(0)
    s += bytes([0xC3, 0x87, 0x69])  # OP_TX_OUTPUT_SPK, OP_EQUAL, OP_VERIFY
    s += push_int(0)
    s += bytes([0xC2])              # OP_TX_OUTPUT_AMOUNT
    s += push_int(deposit)
    s += bytes([0xA2, 0x69])        # OP_GTE, OP_VERIFY
    s += push_data(b_pubkey)
    s += bytes([0xAC])              # OP_CHECKSIG
    return s

# ─── Load default wallet ──────────────────────────────────────────

def load_default_wallet():
    with open(WALLET_PATH) as f:
        return json.load(f)

# ─── Auth middleware ──────────────────────────────────────────────

@web.middleware
async def auth_middleware(request, handler):
    # Skip auth for static files, status, inbox, and whisper lookups
    if request.path == "/" or request.path == "/api/status" or not request.path.startswith("/api/"):
        return await handler(request)
    if request.method == "GET" and (request.path == "/api/inbox" or request.path.startswith("/api/whisper/")):
        return await handler(request)
    
    key = request.headers.get("X-Whisper-Key", "")
    if key != API_KEY:
        return web.json_response({"error": "Invalid or missing X-Whisper-Key"}, status=401)
    return await handler(request)

# ─── API Handlers ─────────────────────────────────────────────────

async def handle_status(request):
    return web.json_response({
        "status": "ok",
        "network": NETWORK_ID,
        "version": "0.1",
        "endpoints": ["/api/status", "/api/send", "/api/read", "/api/inbox", "/api/broadcast", "/api/whisper/{tx_id}"],
    })

async def handle_send(request):
    """Send a whisper message by locking deposit into covenant P2SH."""
    import kaspa

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    to_address = body.get("to")
    message = body.get("message", "")
    sender_key_hex = body.get("sender_key")

    if not to_address:
        return web.json_response({"error": "Missing 'to' address"}, status=400)
    if not message:
        return web.json_response({"error": "Missing 'message'"}, status=400)

    # Use provided key or default wallet
    if not sender_key_hex:
        wallet = load_default_wallet()
        sender_key_hex = wallet["private_key"]

    try:
        # Derive sender info
        a_privkey = kaspa.PrivateKey(sender_key_hex)
        a_pubkey = a_privkey.to_public_key()
        a_xonly = a_pubkey.to_x_only_public_key()
        a_addr = a_xonly.to_address(NETWORK_TYPE)
        a_addr_str = a_addr.to_string()
        a_spk = kaspa.pay_to_address_script(a_addr)
        a_spk_bytes = b'\x00\x00' + bytes.fromhex(a_spk.script)

        # Derive receiver pubkey from address
        to_addr_obj = kaspa.Address(to_address)
        to_spk = kaspa.pay_to_address_script(to_addr_obj)
        to_script = bytes.fromhex(to_spk.script)
        # P2PK script: 0x20 <32-byte-pubkey> 0xac
        if len(to_script) == 34 and to_script[0] == 0x20 and to_script[33] == 0xac:
            b_pubkey_bytes = to_script[1:33]
        else:
            return web.json_response({"error": "Cannot extract pubkey from receiver address"}, status=400)

        # Build covenant
        covenant_script = build_covenant_script(a_spk_bytes, b_pubkey_bytes, DEPOSIT_SOMPI)
        p2sh_spk = kaspa.pay_to_script_hash_script(covenant_script)
        p2sh_addr = kaspa.address_from_script_public_key(p2sh_spk, NETWORK_TYPE)
        p2sh_addr_str = p2sh_addr.to_string()

        # Connect to kaspad
        client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
        await client.connect()

        try:
            # Get UTXOs
            result = await client.get_utxos_by_addresses({"addresses": [a_addr_str]})
            entries = result.get("entries", [])

            # Filter mature UTXOs
            dag_info = await client.get_block_dag_info()
            current_daa = dag_info["virtualDaaScore"]

            lock_amount = DEPOSIT_SOMPI + FEE_BUFFER_SOMPI
            needed = lock_amount + FEE_SOMPI + 10000

            mature = []
            for e in entries:
                utxo = e["utxoEntry"]
                if utxo["isCoinbase"] and (current_daa - utxo["blockDaaScore"]) < 500:
                    continue
                if utxo["amount"] >= needed:
                    mature.append(e)

            if not mature:
                return web.json_response({"error": "No suitable UTXO found", "needed": needed}, status=400)

            selected = mature[0]
            input_amount = selected["utxoEntry"]["amount"]
            change = input_amount - lock_amount - FEE_SOMPI
            payload = json.dumps({
                "v": 1,
                "t": "message",
                "d": message,
                "a": {
                    "from": a_addr_str,
                    "script": covenant_script.hex(),
                    "spk": a_spk.script,
                    "deposit": DEPOSIT_SOMPI,
                }
            }, ensure_ascii=False).encode("utf-8")

            tx = kaspa.create_transaction(
                [selected],
                [
                    kaspa.PaymentOutput(kaspa.Address(p2sh_addr_str), lock_amount),
                    kaspa.PaymentOutput(kaspa.Address(a_addr_str), change),
                ],
                0, payload,
            )
            kaspa.sign_transaction(tx, [a_privkey], False)

            r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
            tx_id = r.get("transactionId", tx.id)

            # Save covenant info
            covenant_info = {
                "tx_id": tx_id,
                "covenant_script_hex": covenant_script.hex(),
                "p2sh_address": p2sh_addr_str,
                "p2sh_spk": p2sh_spk.script,
                "a_address": a_addr_str,
                "a_spk": a_spk.script,
                "b_pubkey": b_pubkey_bytes.hex(),
                "deposit_sompi": DEPOSIT_SOMPI,
                "message": message,
                "output_index": 0,
            }
            info_path = STATIC_DIR / "covenant_info.json"
            with open(info_path, "w") as f:
                json.dump(covenant_info, f, indent=2)

            return web.json_response({
                "tx_id": tx_id,
                "covenant_address": p2sh_addr_str,
                "deposit": DEPOSIT_SOMPI,
                "status": "sent",
                "sender": a_addr_str,
                "receiver": to_address,
            })
        finally:
            await client.disconnect()

    except Exception as e:
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


async def handle_read(request):
    """Read a whisper message by spending the covenant UTXO."""
    import kaspa

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    tx_id = body.get("tx_id")
    reader_key_hex = body.get("reader_key")

    if not tx_id:
        return web.json_response({"error": "Missing 'tx_id'"}, status=400)

    if not reader_key_hex:
        return web.json_response({"error": "Missing 'reader_key' — only the designated receiver can read a Whisper"}, status=400)

    try:
        # Load covenant info
        info_path = STATIC_DIR / "covenant_info.json"
        if not info_path.exists():
            return web.json_response({"error": "No covenant info found"}, status=404)

        with open(info_path) as f:
            info = json.load(f)

        if info["tx_id"] != tx_id:
            return web.json_response({"error": f"TX ID mismatch. Expected: {info['tx_id']}"}, status=400)

        b_privkey = kaspa.PrivateKey(reader_key_hex)
        b_pubkey_compressed = b_privkey.to_public_key().to_string()
        # SDK gives 33-byte compressed pubkey (02/03 prefix), covenant stores 32-byte x-only
        b_pubkey_xonly = b_pubkey_compressed[2:]  # strip prefix
        expected_pubkey = info.get('b_pubkey', '')
        print(f"[READ] reader pubkey (x-only): {b_pubkey_xonly}")
        print(f"[READ] expected pubkey: {expected_pubkey}")
        print(f"[READ] match: {b_pubkey_xonly == expected_pubkey}")
        
        if b_pubkey_xonly != expected_pubkey:
            return web.json_response({
                "error": f"Wrong reader_key — pubkey mismatch. This Whisper is for a different recipient.",
                "your_pubkey": b_pubkey_xonly,
                "expected_pubkey": expected_pubkey,
            }, status=403)

        # Connect
        client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
        await client.connect()

        try:
            p2sh_addr = info["p2sh_address"]
            result = await client.get_utxos_by_addresses({"addresses": [p2sh_addr]})
            entries = result.get("entries", [])

            if not entries:
                return web.json_response({"error": "No covenant UTXO found (not confirmed yet?)"}, status=404)

            # Find specific UTXO
            covenant_entry = None
            for e in entries:
                if e["outpoint"]["transactionId"] == tx_id:
                    covenant_entry = e
                    break
            if not covenant_entry:
                covenant_entry = entries[0]

            utxo_outpoint = covenant_entry["outpoint"]
            utxo_amount = covenant_entry["utxoEntry"]["amount"]
            deposit = info["deposit_sompi"]
            a_addr_str = info["a_address"]
            fee = 3000
            refund_amount = utxo_amount - fee

            if refund_amount < deposit:
                return web.json_response({"error": "UTXO too small for refund + fee"}, status=400)

            # Build spend TX (use original UTXO entry - Kaspa P2SH sighash uses original SPK)
            covenant_script = bytes.fromhex(info["covenant_script_hex"])
            
            tx = kaspa.create_transaction(
                [covenant_entry],
                [kaspa.PaymentOutput(kaspa.Address(a_addr_str), refund_amount)],
                0, b"",
            )

            sig = kaspa.create_input_signature(tx, 0, b_privkey, kaspa.SighashType.All)
            sig_bytes = bytes.fromhex(sig) if isinstance(sig, str) else sig
            sig_script_hex = kaspa.pay_to_script_hash_signature_script(covenant_script, sig_bytes)
            sig_script = bytes.fromhex(sig_script_hex) if isinstance(sig_script_hex, str) else sig_script_hex

            inp = kaspa.TransactionInput(
                previous_outpoint=kaspa.TransactionOutpoint(
                    transaction_id=kaspa.Hash(utxo_outpoint["transactionId"]),
                    index=utxo_outpoint["index"],
                ),
                signature_script=sig_script,
                sequence=0,
                sig_op_count=1,
            )

            out = kaspa.TransactionOutput(
                value=refund_amount,
                script_public_key=kaspa.ScriptPublicKey(0, info["a_spk"]),
            )

            tx_signed = kaspa.Transaction(
                version=0,
                inputs=[inp],
                outputs=[out],
                lock_time=0,
                subnetwork_id=NATIVE_SUBNETWORK,
                gas=0,
                payload=b"",
                mass=0,
            )

            r = await client.submit_transaction({"transaction": tx_signed, "allow_orphan": False})

            return web.json_response({
                "message": info["message"],
                "sender": a_addr_str,
                "refund_tx": tx_signed.id,
                "refund_amount": refund_amount,
                "status": "read",
            })
        finally:
            await client.disconnect()

    except Exception as e:
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


async def handle_broadcast(request):
    """Broadcast a pre-signed TX and/or save covenant info."""
    import kaspa

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    result = {}

    # Save covenant info if provided
    covenant_info = body.get("covenant_info")
    if covenant_info:
        # Save to whispers/{tx_id}.json
        tx_id = covenant_info.get("tx_id", "unknown")
        whisper_path = WHISPERS_DIR / f"{tx_id}.json"
        with open(whisper_path, "w") as f:
            json.dump(covenant_info, f, indent=2)
        # Also save to legacy covenant_info.json for backward compat
        info_path = STATIC_DIR / "covenant_info.json"
        with open(info_path, "w") as f:
            json.dump(covenant_info, f, indent=2)
        result["covenant_info_saved"] = True
        result["tx_id"] = tx_id

    # Broadcast signed TX if provided
    # Accepts either:
    #   "signed_tx_dict": dict from tx.serialize_to_dict() (recommended)
    #   "signed_tx": hex-encoded serialized TX (legacy)
    signed_tx_dict = body.get("signed_tx_dict")
    signed_tx_hex = body.get("signed_tx")
    if signed_tx_dict or signed_tx_hex:
        try:
            client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
            await client.connect()
            try:
                if signed_tx_dict:
                    # Reconstruct Transaction from dict
                    d = signed_tx_dict
                    inputs = []
                    for inp in d["inputs"]:
                        outpoint = kaspa.TransactionOutpoint(kaspa.Hash(inp["transactionId"]), inp["index"])
                        ti = kaspa.TransactionInput(
                            outpoint,
                            bytes.fromhex(inp.get("signatureScript", "")),
                            inp.get("sequence", 0),
                            inp.get("sigOpCount", 1),
                        )
                        inputs.append(ti)
                    outputs = []
                    for out in d["outputs"]:
                        spk_hex = out["scriptPublicKey"]
                        version = int(spk_hex[:4], 16)
                        script = spk_hex[4:]
                        spk = kaspa.ScriptPublicKey(version, script)
                        outputs.append(kaspa.TransactionOutput(out["value"], spk))
                    tx = kaspa.Transaction(
                        d.get("version", 0), inputs, outputs, d.get("lockTime", 0),
                        d.get("subnetworkId", "0" * 40), d.get("gas", 0),
                        bytes.fromhex(d.get("payload", "")), d.get("mass", 0),
                    )
                else:
                    tx = kaspa.Transaction.deserialize(bytes.fromhex(signed_tx_hex))
                r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
                result["tx_id"] = r.get("transactionId", "")
                result["status"] = "broadcast"
            finally:
                await client.disconnect()
        except Exception as e:
            return web.json_response({"error": f"Broadcast failed: {e}"}, status=500)

    if not result:
        return web.json_response({"error": "Provide 'signed_tx' and/or 'covenant_info'"}, status=400)

    return web.json_response(result)


async def handle_whisper_get(request):
    """Get single covenant info by TX ID."""
    tx_id = request.match_info.get("tx_id", "")
    if not tx_id:
        return web.json_response({"error": "Missing tx_id"}, status=400)

    # Try whispers/ dir first, then legacy file
    whisper_path = WHISPERS_DIR / f"{tx_id}.json"
    if whisper_path.exists():
        with open(whisper_path) as f:
            info = json.load(f)
        return web.json_response(info)

    # Fallback: legacy covenant_info.json
    info_path = STATIC_DIR / "covenant_info.json"
    if info_path.exists():
        with open(info_path) as f:
            info = json.load(f)
        if info.get("tx_id") == tx_id:
            return web.json_response(info)

    return web.json_response({"error": f"No covenant info found for TX {tx_id}"}, status=404)


async def handle_inbox(request):
    """List pending whisper messages for an address."""
    import kaspa

    address = request.query.get("address")
    if not address:
        return web.json_response({"error": "Missing 'address' query param"}, status=400)

    try:
        # Extract pubkey from address
        try:
            addr_obj = kaspa.Address(address)
            spk = kaspa.pay_to_address_script(addr_obj)
            script_bytes = bytes.fromhex(spk.script)
            if len(script_bytes) == 34:
                addr_pubkey = script_bytes[1:33].hex()
            else:
                addr_pubkey = None
        except Exception:
            addr_pubkey = None

        if not addr_pubkey:
            return web.json_response({"error": "Cannot extract pubkey from address"}, status=400)

        messages = []

        # Scan whispers/ directory
        whisper_files = list(WHISPERS_DIR.glob("*.json"))
        
        # Also check legacy file
        legacy_path = STATIC_DIR / "covenant_info.json"
        legacy_tx_ids = set()
        
        for wf in whisper_files:
            try:
                with open(wf) as f:
                    info = json.load(f)
                legacy_tx_ids.add(info.get("tx_id"))
                if info.get("b_pubkey") == addr_pubkey:
                    messages.append({
                        "tx_id": info["tx_id"],
                        "sender": info.get("a_address", ""),
                        "deposit": info.get("deposit_sompi", 0),
                        "covenant_address": info.get("p2sh_address", ""),
                    })
            except Exception:
                continue

        # Check legacy file if not already covered
        if legacy_path.exists():
            try:
                with open(legacy_path) as f:
                    info = json.load(f)
                if info.get("tx_id") not in legacy_tx_ids and info.get("b_pubkey") == addr_pubkey:
                    messages.append({
                        "tx_id": info["tx_id"],
                        "sender": info.get("a_address", ""),
                        "deposit": info.get("deposit_sompi", 0),
                        "covenant_address": info.get("p2sh_address", ""),
                    })
            except Exception:
                pass

        return web.json_response({"address": address, "messages": messages})

    except Exception as e:
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


# ─── App Setup ────────────────────────────────────────────────────

def create_app():
    app = web.Application(middlewares=[auth_middleware])

    # API routes
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/send", handle_send)
    app.router.add_post("/api/read", handle_read)
    app.router.add_post("/api/broadcast", handle_broadcast)
    app.router.add_get("/api/inbox", handle_inbox)
    app.router.add_get("/api/whisper/{tx_id}", handle_whisper_get)

    # Static files (index.html at root)
    app.router.add_get("/", lambda r: web.FileResponse(STATIC_DIR / "index.html"))
    app.router.add_static("/static", STATIC_DIR, show_index=False)

    # CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*",
        )
    })
    for route in list(app.router.routes()):
        try:
            cors.add(route)
        except ValueError:
            pass

    return app


if __name__ == "__main__":
    print(f"🌊 Whisper Covenant API starting on port {PORT}")
    print(f"   API Key: {API_KEY[:8]}...")
    print(f"   Network: {NETWORK_ID}")
    print(f"   wRPC:    {WRPC_URL}")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda msg: print(f"   {msg}"))
