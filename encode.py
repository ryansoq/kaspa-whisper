#!/usr/bin/env python3
"""
🌊 Whisper Covenant v3 — Encode (發送悄悄話 / Send a Whisper)

將訊息加密後鎖入 Kaspa CLTV covenant，收件人讀取後自動退款，超時可回收。
Encrypts a message into a Kaspa CLTV covenant. Recipient reads → auto-refund. Timeout → reclaim.

Usage:
  python3 encode.py --to <recipient_address> --message "Hello!" --key <sender_privkey> [--plain] [--remote]

Options:
  --to              Recipient Kaspa address (testnet)
  --message, -m     Message text
  --key, -k         Sender private key (hex). Falls back to ~/.secrets/testnet-wallet.json
  --plain           Send plaintext (type=message); default is ECIES encrypted (type=whisper)
  --remote          Use REST API instead of local kaspad (no node needed!)
  --timeout-offset  CLTV timeout offset from current DAA score (default: 1000 ≈ 100s)

Private key NEVER leaves local!

CLTV Covenant Script 結構 / Structure:
  OP_IF
    <a_spk> <0> OP_TX_OUTPUT_SPK OP_EQUAL OP_VERIFY   // 強制輸出到 A 地址
    <deposit> <0> OP_TX_OUTPUT_AMOUNT OP_GTE OP_VERIFY  // 強制輸出金額 >= deposit
    <b_pubkey> OP_CHECKSIG                              // B (收件人) 簽名
  OP_ELSE
    <timeout_daa> OP_CHECKLOCKTIMEVERIFY                // 超時後...
    <a_pubkey> OP_CHECKSIG                              // A (發送人) 可回收
  OP_ENDIF

⚠️ Kaspa 的 OP_CHECKLOCKTIMEVERIFY (0xb0) 會 pop stack！不像 Bitcoin 需要 OP_DROP。
"""

import argparse
import asyncio
import json
import os
import sys

import kaspa

# ─── Config ───────────────────────────────────────────────────────
WRPC_URL = "ws://localhost:17210"
NETWORK_ID = "testnet-12"
NETWORK_TYPE = "testnet"
DEPOSIT_SOMPI = 20_000_000      # 0.2 tKAS
FEE_SOMPI = 10_000
FEE_BUFFER_SOMPI = 5_000
REST_API_URL = "https://api-tn12.kaspa.org"
WALLET_PATH = os.path.expanduser("~/.secrets/testnet-wallet.json")

# ─── Script helpers ───────────────────────────────────────────────

def push_data(data: bytes) -> bytes:
    """Push arbitrary data onto the script stack with the correct opcode prefix."""
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
    """Push an integer onto the script stack using minimal encoding."""
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


def build_covenant_script_with_timeout(
    a_spk_bytes: bytes, a_pubkey: bytes, b_pubkey: bytes, deposit: int, timeout_daa: int
) -> bytes:
    """
    Build a CLTV covenant script with timeout.
    建構帶有超時機制的 CLTV covenant 腳本。

    IF branch (Bob reads):
      - Covenant check: output[0] must pay >= deposit to Alice's address
      - Bob signs with OP_CHECKSIG
    ELSE branch (Alice reclaims after timeout):
      - OP_CHECKLOCKTIMEVERIFY ensures DAA score >= timeout_daa
      - Alice signs with OP_CHECKSIG

    Args:
        a_spk_bytes: Alice's scriptPublicKey with version prefix (2 bytes BE + script)
        a_pubkey: Alice's x-only public key (32 bytes)
        b_pubkey: Bob's x-only public key (32 bytes)
        deposit: Minimum deposit amount in sompi
        timeout_daa: DAA score after which Alice can reclaim

    ⚠️ Kaspa's OP_CHECKLOCKTIMEVERIFY (0xb0) pops the stack value!
       Unlike Bitcoin where CLTV is a NOP-style opcode, no OP_DROP needed.
    """
    OP_IF = 0x63
    OP_ELSE = 0x67
    OP_ENDIF = 0x68
    OP_CLTV = 0xB0
    OP_TX_OUTPUT_SPK = 0xC3
    OP_TX_OUTPUT_AMOUNT = 0xC2
    OP_EQUAL = 0x87
    OP_VERIFY = 0x69
    OP_GTE = 0xA2
    OP_CHECKSIG = 0xAC

    s = b""
    s += bytes([OP_IF])
    # IF: B reads — covenant check + B signs
    s += push_data(a_spk_bytes)
    s += push_int(0)
    s += bytes([OP_TX_OUTPUT_SPK, OP_EQUAL, OP_VERIFY])
    s += push_int(0)
    s += bytes([OP_TX_OUTPUT_AMOUNT])
    s += push_int(deposit)
    s += bytes([OP_GTE, OP_VERIFY])
    s += push_data(b_pubkey)
    s += bytes([OP_CHECKSIG])
    # ELSE: A reclaims after timeout
    s += bytes([OP_ELSE])
    s += push_int(timeout_daa)
    s += bytes([OP_CLTV])
    s += push_data(a_pubkey)
    s += bytes([OP_CHECKSIG])
    s += bytes([OP_ENDIF])
    return s


async def main():
    parser = argparse.ArgumentParser(description="Whisper Covenant v3 — Encode & Sign locally")
    parser.add_argument("--to", required=True, help="Recipient address")
    parser.add_argument("--message", "-m", required=True, help="Message text")
    parser.add_argument("--key", "-k", default=None, help="Sender private key (hex). Falls back to ~/.secrets/testnet-wallet.json")
    parser.add_argument("--plain", action="store_true", help="Send plaintext (type=message)")
    parser.add_argument("--timeout-offset", type=int, default=1000, help="CLTV timeout offset from current DAA (default: 1000 ≈ 100s)")
    parser.add_argument("--local-only", action="store_true", help="Skip uploading covenant_info to API")
    parser.add_argument("--api-url", default="http://whisper.openclaw-alpha.com", help="Whisper API URL")
    parser.add_argument("--remote", action="store_true", help="Use REST API instead of local kaspad (no node needed!)")
    args = parser.parse_args()

    # ── Load private key ──
    if args.key:
        privkey_hex = args.key
    else:
        with open(WALLET_PATH) as f:
            wallet = json.load(f)
        privkey_hex = wallet["private_key"]

    # ── Derive sender info ──
    a_privkey = kaspa.PrivateKey(privkey_hex)
    a_pubkey = a_privkey.to_public_key()
    a_xonly = a_pubkey.to_x_only_public_key()
    a_addr = a_xonly.to_address(NETWORK_TYPE)
    a_addr_str = a_addr.to_string()
    a_spk = kaspa.pay_to_address_script(a_addr)
    # OP_TX_OUTPUT_SPK returns: version(2 bytes BE) + script_bytes
    a_spk_bytes = b'\x00\x00' + bytes.fromhex(a_spk.script)
    a_pubkey_bytes = bytes.fromhex(a_xonly.to_string())

    # ── Derive recipient pubkey ──
    to_addr_obj = kaspa.Address(args.to)
    to_spk = kaspa.pay_to_address_script(to_addr_obj)
    to_script = bytes.fromhex(to_spk.script)
    if len(to_script) != 34 or to_script[0] != 0x20 or to_script[33] != 0xac:
        print("❌ Cannot extract pubkey from recipient address")
        sys.exit(1)
    b_pubkey_bytes = to_script[1:33]
    b_xonly_hex = b_pubkey_bytes.hex()

    # ── Encrypt or plaintext ──
    msg_type = "message" if args.plain else "whisper"
    if args.plain:
        data_str = args.message
    else:
        from ecies import encrypt as ecies_encrypt
        # ECIES needs 33-byte compressed pubkey; x-only has no parity info,
        # so we try 02 first (even y). Decoder will try both prefixes.
        compressed_hex = "02" + b_xonly_hex
        ciphertext = ecies_encrypt(compressed_hex, args.message.encode("utf-8"))
        data_str = ciphertext.hex()

    # ── Get current DAA score (needed for CLTV timeout) ──
    client = None
    use_remote = args.remote

    if not use_remote:
        try:
            client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
            await client.connect()
            result = await client.get_utxos_by_addresses({"addresses": [a_addr_str]})
            entries = result.get("entries", [])
            dag_info = await client.get_block_dag_info()
            current_daa = int(dag_info["virtualDaaScore"])
        except Exception as e:
            print(f"⚠️  Local kaspad not available ({e}), falling back to REST API...")
            use_remote = True
            client = None

    if use_remote:
        import urllib.request
        # Fetch UTXOs from REST API
        utxo_url = f"{REST_API_URL}/addresses/{a_addr_str}/utxos"
        print(f"🌐 Fetching UTXOs from REST API...")
        try:
            req = urllib.request.Request(utxo_url, headers={"User-Agent": "whisper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                utxo_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"❌ REST API UTXO fetch failed: {e}")
            sys.exit(1)

        # Convert REST API format to kaspa SDK format
        entries = []
        for u in utxo_data:
            spk = u["utxoEntry"]["scriptPublicKey"]
            if isinstance(spk, dict):
                spk = "0000" + spk.get("scriptPublicKey", "")
            entry = {
                "outpoint": {
                    "transactionId": u["outpoint"]["transactionId"],
                    "index": u["outpoint"]["index"],
                },
                "address": a_addr_str,
                "utxoEntry": {
                    "amount": int(u["utxoEntry"]["amount"]),
                    "scriptPublicKey": spk,
                    "blockDaaScore": int(u["utxoEntry"]["blockDaaScore"]),
                    "isCoinbase": u["utxoEntry"]["isCoinbase"],
                },
            }
            entries.append(entry)

        # Fetch DAA score
        daa_url = f"{REST_API_URL}/info/virtual-chain-blue-score"
        req = urllib.request.Request(daa_url, headers={"User-Agent": "whisper/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            daa_data = json.loads(resp.read().decode("utf-8"))
        current_daa = int(daa_data["blueScore"])

    # ── Calculate CLTV timeout ──
    timeout_daa = current_daa + args.timeout_offset

    # ── Build covenant with CLTV timeout ──
    covenant_script = build_covenant_script_with_timeout(
        a_spk_bytes, a_pubkey_bytes, b_pubkey_bytes, DEPOSIT_SOMPI, timeout_daa
    )

    # ── Build payload (includes covenant metadata in `a` for on-chain self-containment) ──
    payload_obj = {
        "v": 3,
        "t": msg_type,
        "d": data_str,
        "a": {
            "from": a_addr_str,
            "script": covenant_script.hex(),
            "spk": a_spk.script,
            "deposit": DEPOSIT_SOMPI,
            "timeout_daa": timeout_daa,
        }
    }
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    p2sh_spk = kaspa.pay_to_script_hash_script(covenant_script)
    p2sh_addr = kaspa.address_from_script_public_key(p2sh_spk, NETWORK_TYPE)
    p2sh_addr_str = p2sh_addr.to_string()

    print(f"🌊 Whisper Covenant v3 — Encode")
    print(f"   From: {a_addr_str}")
    print(f"   To:   {args.to}")
    print(f"   Type: {msg_type}")
    print(f"   P2SH: {p2sh_addr_str}")
    print(f"   Current DAA: {current_daa}")
    print(f"   Timeout DAA: {timeout_daa} (current + {args.timeout_offset})")
    print()

    # ── Select UTXO ──
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
        print(f"❌ No suitable UTXO (need {needed} sompi = {needed/1e8:.4f} tKAS)")
        print(f"   💧 Need tKAS? Message @Nami_Kaspa_Bot on Telegram for testnet faucet!")
        if client:
            await client.disconnect()
        sys.exit(1)

    selected = mature[0]
    input_amount = selected["utxoEntry"]["amount"]
    change = input_amount - lock_amount - FEE_SOMPI

    # ── Build & sign TX ──
    tx = kaspa.create_transaction(
        [selected],
        [
            kaspa.PaymentOutput(kaspa.Address(p2sh_addr_str), lock_amount),
            kaspa.PaymentOutput(kaspa.Address(a_addr_str), change),
        ],
        0, payload,
    )
    kaspa.sign_transaction(tx, [a_privkey], False)

    tx_id = tx.id
    print(f"✅ TX signed locally! ID: {tx_id}")
    print(f"   Lock: {lock_amount/1e8:.4f} tKAS → P2SH")
    print(f"   Change: {change/1e8:.4f} tKAS")

    # ── Build covenant_info for decode/reclaim ──
    covenant_info = {
        "tx_id": tx_id,
        "covenant_script_hex": covenant_script.hex(),
        "p2sh_address": p2sh_addr_str,
        "p2sh_spk": p2sh_spk.script,
        "a_address": a_addr_str,
        "a_spk": a_spk.script,
        "a_pubkey": a_pubkey_bytes.hex(),
        "b_pubkey": b_xonly_hex,
        "deposit_sompi": DEPOSIT_SOMPI,
        "timeout_daa": timeout_daa,
        "message": args.message,
        "d": data_str,
        "type": msg_type,
        "output_index": 0,
    }

    # ── Submit TX ──
    if client and not use_remote:
        try:
            r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
            submitted_id = r.get("transactionId", tx_id)
            print(f"📡 TX submitted to kaspad! ID: {submitted_id}")
        except Exception as e:
            print(f"❌ Submit failed: {e}")
            await client.disconnect()
            sys.exit(1)
    else:
        # Submit via Whisper API (which connects to kaspad wRPC)
        import urllib.request
        broadcast_url = f"{args.api_url}/api/broadcast"
        tx_dict = tx.serialize_to_dict()
        broadcast_body = {"signed_tx_dict": tx_dict, "covenant_info": covenant_info}
        print(f"📡 Broadcasting TX via Whisper API ({args.api_url})...")
        try:
            req = urllib.request.Request(
                broadcast_url,
                data=json.dumps(broadcast_body).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "whisper/1.0",
                          "X-Whisper-Key": os.environ.get("WHISPER_API_KEY", "whisper-testnet-poc-key")},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                print(f"📡 TX broadcast via Whisper API! ID: {result.get('tx_id', tx_id)}")
                if result.get("covenant_info_saved"):
                    print(f"☁️  Covenant info uploaded to API")
        except Exception as e:
            err_body = ""
            if hasattr(e, 'read'):
                err_body = e.read().decode("utf-8", errors="replace")
            print(f"❌ Whisper API broadcast failed: {e}")
            if err_body:
                print(f"   Detail: {err_body}")
            sys.exit(1)

    # Save covenant_info locally
    info_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covenant_info.json")
    with open(info_path, "w") as f:
        json.dump(covenant_info, f, indent=2)
    print(f"💾 Covenant info → {info_path}")

    # ── Upload covenant_info to API (default, skip if already uploaded via remote broadcast) ──
    if not args.local_only and not use_remote:
        import aiohttp
        api_key = os.environ.get("WHISPER_API_KEY", "whisper-testnet-poc-key")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{args.api_url}/api/broadcast",
                    json={"covenant_info": covenant_info},
                    headers={"X-Whisper-Key": api_key}
                ) as resp:
                    if resp.status == 200:
                        print(f"☁️  Covenant info uploaded to API")
                    else:
                        print(f"⚠️  API upload failed: {await resp.text()}")
        except Exception as e:
            print(f"⚠️  API upload failed (offline?): {e}")
    else:
        print(f"   (--local-only: skipped API upload)")

    if client:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
