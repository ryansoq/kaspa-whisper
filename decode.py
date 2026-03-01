#!/usr/bin/env python3
"""
🌊 Whisper Covenant v3 — Decode (讀取悄悄話 / Read a Whisper)

解密訊息並花費 CLTV covenant UTXO，自動退款給發送人。
Decrypts the message and spends the CLTV covenant UTXO, auto-refunding the sender.

Usage:
  python3 decode.py --tx <tx_id> --key <recipient_privkey> [--info covenant_info.json] [--remote]

Flow:
  1. Load covenant info (from file, API, or block explorer)
  2. Decrypt message (ECIES) or read plaintext
  3. Spend covenant UTXO → refund to sender (IF branch: OP_TRUE + Bob's sig)
  4. Sign locally with recipient's private key

Private key NEVER leaves local!

Sig script 結構 / Structure (IF branch — Bob reads):
  <bob_signature> <OP_TRUE (0x51)> <push redeem_script>
  OP_TRUE selects the IF branch where Bob can claim via OP_CHECKSIG.

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
REST_API_URL = "https://api-tn12.kaspa.org"
WALLET_PATH = os.path.expanduser("~/.secrets/testnet-wallet.json")

def push_data(data: bytes) -> bytes:
    """Push arbitrary data onto the script stack with the correct opcode prefix."""
    n = len(data)
    if n <= 75:
        return bytes([n]) + data
    elif n <= 255:
        return bytes([0x4c, n]) + data
    else:
        return bytes([0x4d]) + n.to_bytes(2, "little") + data


def covenant_info_from_payload(payload_json, tx_id):
    """
    Reconstruct covenant_info from on-chain payload's `a` field.
    從鏈上 payload 的 `a` 欄位重建 covenant_info。
    """
    a = payload_json["a"]
    script_hex = a["script"]
    script_bytes = bytes.fromhex(script_hex)

    # Extract b_pubkey from covenant script
    # For v3 CLTV script, the IF branch ends with: push_data(b_pubkey_32) + 0xac + 0x67 (OP_ELSE)
    # For v2 script, it ends with: push_data(b_pubkey_32) + 0xac
    # We search for the pattern: 0x20 (push 32 bytes) + 32 bytes + 0xac
    b_pubkey_hex = ""
    for i in range(len(script_bytes) - 34):
        if script_bytes[i] == 0x20 and script_bytes[i + 33] == 0xac:
            b_pubkey_hex = script_bytes[i + 1:i + 33].hex()
            break

    # Reconstruct p2sh_address from script
    p2sh_address = ""
    p2sh_spk = None
    try:
        covenant_script = script_bytes
        p2sh_spk = kaspa.pay_to_script_hash_script(covenant_script)
        p2sh_addr = kaspa.address_from_script_public_key(p2sh_spk, NETWORK_TYPE)
        p2sh_address = p2sh_addr.to_string()
    except Exception:
        pass

    # Reconstruct a_spk from a_address
    a_spk = ""
    try:
        a_addr_obj = kaspa.Address(a["from"])
        a_spk = kaspa.pay_to_address_script(a_addr_obj).script
    except Exception:
        a_spk = a.get("spk", "")

    return {
        "tx_id": tx_id,
        "covenant_script_hex": script_hex,
        "p2sh_address": p2sh_address,
        "p2sh_spk": p2sh_spk.script if p2sh_spk else "",
        "a_address": a["from"],
        "a_spk": a_spk,
        "b_pubkey": b_pubkey_hex,
        "deposit_sompi": a["deposit"],
        "timeout_daa": a.get("timeout_daa"),
        "d": payload_json["d"],
        "type": payload_json["t"],
        "output_index": 0,
    }


async def main():
    parser = argparse.ArgumentParser(description="Whisper Covenant v3 — Decode & Refund locally")
    parser.add_argument("--tx", required=True, help="Whisper TX ID")
    parser.add_argument("--key", "-k", default=None, help="Recipient private key (hex). Falls back to ~/.secrets/testnet-wallet.json")
    parser.add_argument("--info", default=None, help="Path to covenant_info.json (default: auto from API)")
    parser.add_argument("--payload", default=None, help="Raw TX payload JSON (offline decode, no API needed)")
    parser.add_argument("--no-refund", action="store_true", help="Only decrypt, don't spend covenant")
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

    # ── Load covenant info ──
    # Priority: --payload > --info > local file > API > block explorer
    info = None

    if args.payload:
        try:
            payload_json = json.loads(args.payload)
            info = covenant_info_from_payload(payload_json, args.tx)
            print(f"📦 Reconstructed covenant info from payload `a` field")
        except Exception as e:
            print(f"❌ Failed to parse --payload: {e}")
            sys.exit(1)
    elif args.info:
        with open(args.info) as f:
            info = json.load(f)
    else:
        # Try local file first
        info_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covenant_info.json")
        if os.path.exists(info_path):
            with open(info_path) as f:
                local_info = json.load(f)
            if local_info.get("tx_id") == args.tx:
                info = local_info

        # Fallback: fetch from API
        if not info:
            import urllib.request
            api_url = f"{args.api_url}/api/whisper/{args.tx}"
            print(f"📡 Fetching covenant info from API...")
            try:
                req = urllib.request.Request(api_url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        info = json.loads(resp.read().decode("utf-8"))
                        print(f"   ✅ Got covenant info from API")
                    else:
                        print(f"   ❌ API returned {resp.status}")
            except Exception as e:
                print(f"   ❌ API fetch failed: {e}")

        # Fallback: try block explorer
        if not info:
            import urllib.request
            explorer_url = f"{REST_API_URL}/transactions/{args.tx}"
            print(f"🔍 Trying block explorer...")
            try:
                req = urllib.request.Request(explorer_url, headers={"User-Agent": "whisper/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        tx_data = json.loads(resp.read().decode("utf-8"))
                        payload_hex = tx_data.get("payload", "")
                        if payload_hex:
                            payload_bytes = bytes.fromhex(payload_hex)
                            payload_json = json.loads(payload_bytes.decode("utf-8"))
                            if payload_json.get("a", {}).get("script"):
                                info = covenant_info_from_payload(payload_json, args.tx)
                                print(f"   ✅ Reconstructed from block explorer payload")
                            else:
                                print(f"   ⚠️ Payload found but no `a` field (old format?)")
            except Exception as e:
                print(f"   ❌ Block explorer failed: {e}")

    if not info or info.get("tx_id") != args.tx:
        print(f"❌ No covenant info found for TX {args.tx}")
        print(f"   Options:")
        print(f"     --payload '<JSON>'  (from on-chain TX payload)")
        print(f"     --info <file>       (covenant_info.json)")
        print(f"     API: {args.api_url}")
        sys.exit(1)

    # ── Verify recipient ──
    b_privkey = kaspa.PrivateKey(privkey_hex)
    b_pubkey = b_privkey.to_public_key()
    b_xonly_hex = b_pubkey.to_x_only_public_key().to_string()

    if b_xonly_hex != info["b_pubkey"]:
        print(f"❌ Wrong key! This whisper is for a different recipient.")
        print(f"   Your pubkey:     {b_xonly_hex}")
        print(f"   Expected pubkey: {info['b_pubkey']}")
        sys.exit(1)

    # ── Decrypt message ──
    msg_type = info.get("type", info.get("t", "message"))
    raw_data = info.get("d", "")

    if msg_type == "whisper":
        from ecies import decrypt as ecies_decrypt
        # Kaspa uses x-only pubkeys (no parity). Encoder uses 02 prefix,
        # but if our key's actual parity is 03, we must negate the secret key.
        # Try normal first, then negated.
        ciphertext = bytes.fromhex(raw_data)
        privkey_bytes = bytes.fromhex(privkey_hex)
        try:
            plaintext = ecies_decrypt(privkey_hex, ciphertext)
        except Exception:
            # Negate the private key (mod curve order) to match opposite parity
            from coincurve import PrivateKey as _CPrivateKey
            _sk = _CPrivateKey(privkey_bytes)
            _n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
            _neg = (_n - int.from_bytes(privkey_bytes, 'big')).to_bytes(32, 'big')
            plaintext = ecies_decrypt(_neg.hex(), ciphertext)
        message = plaintext.decode("utf-8")
    else:
        message = raw_data

    print(f"🌊 Whisper Covenant v3 — Decode")
    print(f"   TX: {args.tx}")
    print(f"   From: {info['a_address']}")
    print(f"   Type: {msg_type}")
    if info.get("timeout_daa"):
        print(f"   Timeout DAA: {info['timeout_daa']}")
    print(f"   💬 Message: {message}")
    print()

    if args.no_refund:
        print("   (--no-refund: skipping covenant spend)")
        return

    # ── Spend covenant UTXO → refund to sender (IF branch) ──
    client = None
    use_remote = args.remote

    if not use_remote:
        try:
            client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
            await client.connect()
            p2sh_addr = info["p2sh_address"]
            result = await client.get_utxos_by_addresses({"addresses": [p2sh_addr]})
            entries = result.get("entries", [])
        except Exception as e:
            print(f"⚠️  Local kaspad not available ({e}), falling back to REST API...")
            use_remote = True
            client = None

    if use_remote:
        import urllib.request
        p2sh_addr = info["p2sh_address"]
        utxo_url = f"{REST_API_URL}/addresses/{p2sh_addr}/utxos"
        print(f"🌐 Fetching covenant UTXO from REST API...")
        try:
            req = urllib.request.Request(utxo_url, headers={"User-Agent": "whisper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                utxo_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"❌ REST API UTXO fetch failed: {e}")
            sys.exit(1)
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
                "address": p2sh_addr,
                "utxoEntry": {
                    "amount": int(u["utxoEntry"]["amount"]),
                    "scriptPublicKey": spk,
                    "blockDaaScore": int(u["utxoEntry"]["blockDaaScore"]),
                    "isCoinbase": u["utxoEntry"]["isCoinbase"],
                },
            }
            entries.append(entry)
    else:
        p2sh_addr = info["p2sh_address"]

    if not entries:
        print("❌ No covenant UTXO found (already spent or not confirmed)")
        if client:
            await client.disconnect()
        sys.exit(1)

    # Find specific UTXO
    covenant_entry = None
    for e in entries:
        if e["outpoint"]["transactionId"] == args.tx:
            covenant_entry = e
            break
    if not covenant_entry:
        covenant_entry = entries[0]
        print(f"   ⚠️ Exact UTXO not found, using first available")

    utxo_amount = covenant_entry["utxoEntry"]["amount"]
    deposit = info["deposit_sompi"]
    a_addr_str = info["a_address"]
    fee = 3000
    refund_amount = utxo_amount - fee

    if refund_amount < deposit:
        print(f"❌ UTXO too small ({utxo_amount}) for refund ({deposit}) + fee ({fee})")
        if client:
            await client.disconnect()
        sys.exit(1)

    # Build spend TX
    tx = kaspa.create_transaction(
        [covenant_entry],
        [kaspa.PaymentOutput(kaspa.Address(a_addr_str), refund_amount)],
        0, b"",
    )

    # Sign with Bob's key
    sig = kaspa.create_input_signature(tx, 0, b_privkey, kaspa.SighashType.All)
    sig_bytes = bytes.fromhex(sig) if isinstance(sig, str) else sig

    # Build P2SH sig script: <sig> <OP_TRUE (0x51)> <push redeem_script>
    # OP_TRUE selects the IF branch (Bob reads / claims)
    covenant_script = bytes.fromhex(info["covenant_script_hex"])
    sig_script = sig_bytes + bytes([0x51]) + push_data(covenant_script)

    tx.inputs[0].signature_script = sig_script
    tx.inputs[0].sig_op_count = 1

    print(f"📝 Refund TX")
    print(f"   Refund: {refund_amount/1e8:.4f} tKAS → {a_addr_str}")
    print(f"   Fee: {fee/1e8:.5f} tKAS")

    if client and not use_remote:
        try:
            r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
            refund_tx_id = r.get("transactionId", tx.id)
            print(f"\n✅ Refund TX submitted! ID: {refund_tx_id}")
            print(f"   Sender {a_addr_str} gets {refund_amount/1e8:.4f} tKAS back")
        except Exception as e:
            print(f"\n❌ Refund failed: {e}")
        await client.disconnect()
    else:
        # Submit via Whisper API
        import urllib.request
        broadcast_url = f"{args.api_url}/api/broadcast"
        tx_dict = tx.serialize_to_dict()
        broadcast_body = {"signed_tx_dict": tx_dict}
        print(f"📡 Broadcasting refund TX via Whisper API...")
        try:
            req = urllib.request.Request(
                broadcast_url,
                data=json.dumps(broadcast_body).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "whisper/1.0",
                          "X-Whisper-Key": os.environ.get("WHISPER_API_KEY", "whisper-testnet-poc-key")},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result_data = json.loads(resp.read().decode("utf-8"))
                refund_tx_id = result_data.get("tx_id", tx.id)
                print(f"\n✅ Refund TX broadcast via Whisper API! ID: {refund_tx_id}")
                print(f"   Sender {a_addr_str} gets {refund_amount/1e8:.4f} tKAS back")
        except Exception as e:
            err_body = ""
            if hasattr(e, 'read'):
                err_body = e.read().decode("utf-8", errors="replace")
            print(f"\n❌ Whisper API broadcast failed: {e}")
            if err_body:
                print(f"   Detail: {err_body}")


if __name__ == "__main__":
    asyncio.run(main())
