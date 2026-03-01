#!/usr/bin/env python3
"""
🌊 Whisper Covenant v3 — Reclaim (超時回收 / Reclaim after timeout)

當收件人未在超時前讀取，發送人可回收鎖定的押金。
When the recipient doesn't read before timeout, sender can reclaim the locked deposit.

Usage:
  python3 covenant_reclaim.py [--key <sender_privkey>] [--remote]

Flow:
  1. Load covenant info from covenant_info.json
  2. Check that current DAA score > timeout_daa
  3. Build spend TX with lock_time = timeout_daa
  4. Sign with A's key, use OP_FALSE to select ELSE branch
  5. A gets deposit back

Sig script 結構 / Structure (ELSE branch — Alice reclaims):
  <alice_signature> <OP_FALSE (0x00)> <push redeem_script>
  OP_FALSE selects the ELSE branch where Alice can reclaim via CLTV + OP_CHECKSIG.

⚠️ Kaspa 的 OP_CHECKLOCKTIMEVERIFY (0xb0) 會 pop stack！不像 Bitcoin 需要 OP_DROP。
⚠️ TX 的 lock_time 必須設為 timeout_daa，否則 CLTV 驗證會失敗。
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


async def main():
    parser = argparse.ArgumentParser(description="Whisper Covenant v3 — Reclaim after timeout")
    parser.add_argument("--key", "-k", default=None, help="Sender private key (hex). Falls back to ~/.secrets/testnet-wallet.json")
    parser.add_argument("--info", default=None, help="Path to covenant_info.json")
    parser.add_argument("--remote", action="store_true", help="Use REST API instead of local kaspad (no node needed!)")
    parser.add_argument("--api-url", default="http://whisper.openclaw-alpha.com", help="Whisper API URL")
    args = parser.parse_args()

    # ── Load covenant info ──
    if args.info:
        info_path = args.info
    else:
        info_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covenant_info.json")

    if not os.path.exists(info_path):
        print("❌ covenant_info.json not found. Run encode.py first!")
        sys.exit(1)

    with open(info_path) as f:
        info = json.load(f)

    timeout_daa = info.get("timeout_daa")
    if not timeout_daa:
        print("❌ No timeout_daa in covenant_info.json. Was this sent with CLTV timeout?")
        sys.exit(1)

    print(f"🌊 Whisper Covenant v3 — Reclaim (timeout)")
    print(f"   TX ID: {info['tx_id']}")
    print(f"   Deposit: {info['deposit_sompi'] / 1e8:.2f} tKAS")
    print(f"   Timeout DAA: {timeout_daa}")
    print()

    # ── Load private key ──
    if args.key:
        privkey_hex = args.key
    else:
        with open(WALLET_PATH) as f:
            wallet = json.load(f)
        privkey_hex = wallet["private_key"]

    a_privkey = kaspa.PrivateKey(privkey_hex)
    a_pubkey = a_privkey.to_public_key()
    a_xonly = a_pubkey.to_x_only_public_key()
    a_addr = a_xonly.to_address(NETWORK_TYPE)
    a_addr_str = a_addr.to_string()

    # ── Get current DAA score and covenant UTXO ──
    client = None
    use_remote = args.remote

    if not use_remote:
        try:
            client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
            await client.connect()
            print("✅ Connected to kaspad")

            # Check current DAA
            dag_info = await client.get_block_dag_info()
            current_daa = int(dag_info["virtualDaaScore"])

            # Find the covenant UTXO
            p2sh_addr = info["p2sh_address"]
            result = await client.get_utxos_by_addresses({"addresses": [p2sh_addr]})
            entries = result.get("entries", [])
        except Exception as e:
            print(f"⚠️  Local kaspad not available ({e}), falling back to REST API...")
            use_remote = True
            client = None

    if use_remote:
        import urllib.request

        # Fetch current DAA score
        daa_url = f"{REST_API_URL}/info/virtual-chain-blue-score"
        print(f"🌐 Fetching DAA score from REST API...")
        try:
            req = urllib.request.Request(daa_url, headers={"User-Agent": "whisper/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                daa_data = json.loads(resp.read().decode("utf-8"))
            current_daa = int(daa_data["blueScore"])
        except Exception as e:
            print(f"❌ REST API DAA fetch failed: {e}")
            sys.exit(1)

        # Fetch covenant UTXO
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

    # ── Check timeout ──
    print(f"   Current DAA: {current_daa}")
    print(f"   Timeout DAA: {timeout_daa}")

    if current_daa < timeout_daa:
        remaining = timeout_daa - current_daa
        print(f"   ⏳ Not yet! Need to wait ~{remaining} more DAA scores (~{remaining // 10}s)")
        if client:
            await client.disconnect()
        sys.exit(1)

    print(f"   ✅ Timeout reached! (current {current_daa} >= timeout {timeout_daa})")
    print()

    # ── Find covenant UTXO ──
    if not entries:
        print("❌ No covenant UTXO found! Already spent?")
        if client:
            await client.disconnect()
        sys.exit(1)

    covenant_entry = None
    for e in entries:
        if e["outpoint"]["transactionId"] == info["tx_id"]:
            covenant_entry = e
            break

    if not covenant_entry:
        covenant_entry = entries[0]
        print(f"   ⚠️ Exact UTXO not found, using first available")

    utxo_amount = covenant_entry["utxoEntry"]["amount"]
    print(f"   Found UTXO: {covenant_entry['outpoint']['transactionId']}:{covenant_entry['outpoint']['index']}")
    print(f"   Amount: {utxo_amount / 1e8:.4f} tKAS")

    # ── Build reclaim TX ──
    fee = 3000
    reclaim_amount = utxo_amount - fee

    tx = kaspa.create_transaction(
        [covenant_entry],
        [kaspa.PaymentOutput(kaspa.Address(a_addr_str), reclaim_amount)],
        0,
        b"",
    )

    # Set lock_time to timeout_daa (required for CLTV verification)
    tx.lock_time = timeout_daa

    print(f"📝 Reclaim TX")
    print(f"   Reclaim to A: {reclaim_amount / 1e8:.4f} tKAS")
    print(f"   Fee: {fee / 1e8:.5f} tKAS")
    print(f"   Lock time: {tx.lock_time}")

    # Sign with A's key
    sig = kaspa.create_input_signature(tx, 0, a_privkey, kaspa.SighashType.All)
    sig_bytes = bytes.fromhex(sig) if isinstance(sig, str) else sig

    # Build P2SH sig script: <sig> <OP_FALSE (0x00)> <push redeem_script>
    # OP_FALSE selects the ELSE branch (Alice reclaims after timeout)
    covenant_script = bytes.fromhex(info["covenant_script_hex"])
    sig_script = sig_bytes + bytes([0x00]) + push_data(covenant_script)

    tx.inputs[0].signature_script = sig_script
    tx.inputs[0].sig_op_count = 1

    print(f"   TX ID: {tx.id}")

    # ── Submit TX ──
    if client and not use_remote:
        try:
            r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
            print(f"\n✅ Reclaim TX submitted! Deposit returned to A.")
            print(f"   Result: {r}")
        except Exception as e:
            print(f"\n❌ Submit failed: {e}")
            print(f"\n   Debug info:")
            print(f"   TX version: {tx.version}")
            print(f"   TX lock_time: {tx.lock_time}")
            print(f"   TX ID: {tx.id}")
            d = tx.serialize_to_dict()
            print(f"   TX dict inputs[0] sig_script: {d['inputs'][0].get('signatureScript', 'N/A')[:80]}...")
        await client.disconnect()
    else:
        # Submit via Whisper API
        import urllib.request
        broadcast_url = f"{args.api_url}/api/broadcast"
        tx_dict = tx.serialize_to_dict()
        broadcast_body = {"signed_tx_dict": tx_dict}
        print(f"📡 Broadcasting reclaim TX via Whisper API...")
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
                reclaim_tx_id = result_data.get("tx_id", tx.id)
                print(f"\n✅ Reclaim TX broadcast via Whisper API! ID: {reclaim_tx_id}")
                print(f"   Deposit returned to {a_addr_str}")
        except Exception as e:
            err_body = ""
            if hasattr(e, 'read'):
                err_body = e.read().decode("utf-8", errors="replace")
            print(f"\n❌ Whisper API broadcast failed: {e}")
            if err_body:
                print(f"   Detail: {err_body}")


if __name__ == "__main__":
    asyncio.run(main())
