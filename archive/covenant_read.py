#!/usr/bin/env python3
"""
🌊 Whisper Covenant — Read (B spends covenant UTXO → A gets refund)

Usage: python3 covenant_read.py

Flow:
  1. Load covenant info from covenant_info.json
  2. Find the covenant UTXO on chain
  3. Build spend TX: output[0] = deposit → A (forced by covenant)
  4. Sign with B's key (P2SH signature script)
  5. A automatically gets refund
"""

import argparse
import asyncio
import json
import os

import kaspa

# ─── Config ───────────────────────────────────────────────────────
WRPC_URL = "ws://localhost:17210"
NETWORK_ID = "testnet-12"
NETWORK_TYPE = "testnet"

WALLET_PATH = os.path.expanduser("~/.secrets/testnet-wallet.json")


def push_data(data: bytes) -> bytes:
    """Script push opcode for arbitrary data."""
    n = len(data)
    if n <= 75:
        return bytes([n]) + data
    elif n <= 255:
        return bytes([0x4c, n]) + data
    else:
        return bytes([0x4d]) + n.to_bytes(2, "little") + data


async def main():
    parser = argparse.ArgumentParser(description="Whisper Covenant — Read")
    parser.add_argument("--key", "-k", default=None, help="Recipient private key (hex). Falls back to ~/.secrets/testnet-wallet.json")
    args = parser.parse_args()

    # Load covenant info
    info_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covenant_info.json")
    if not os.path.exists(info_path):
        print("❌ covenant_info.json not found. Run covenant_send.py first!")
        return

    with open(info_path) as f:
        info = json.load(f)

    print(f"🌊 Whisper Covenant — Read")
    print(f"   TX ID: {info['tx_id']}")
    print(f"   Message: {info['message']}")
    print(f"   Deposit: {info['deposit_sompi'] / 1e8:.2f} tKAS")
    print(f"   A address: {info['a_address']}")
    print()

    # Load private key
    if args.key:
        privkey_hex = args.key
    else:
        with open(WALLET_PATH) as f:
            wallet = json.load(f)
        privkey_hex = wallet["private_key"]

    b_privkey = kaspa.PrivateKey(privkey_hex)

    # Connect
    client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
    await client.connect()
    print("✅ Connected to kaspad")

    # Find the covenant UTXO at P2SH address
    p2sh_addr = info["p2sh_address"]
    result = await client.get_utxos_by_addresses({"addresses": [p2sh_addr]})
    entries = result.get("entries", [])

    print(f"   UTXOs at P2SH address: {len(entries)}")

    if not entries:
        print("❌ No covenant UTXO found! TX may not be confirmed yet.")
        await client.disconnect()
        return

    # Find the specific UTXO from our TX
    covenant_entry = None
    for e in entries:
        if e["outpoint"]["transactionId"] == info["tx_id"]:
            covenant_entry = e
            break

    if not covenant_entry:
        covenant_entry = entries[0]
        print(f"   ⚠️ Exact UTXO not found, using first available")

    utxo_outpoint = covenant_entry["outpoint"]
    utxo_amount = covenant_entry["utxoEntry"]["amount"]
    deposit = info["deposit_sompi"]

    print(f"   Found UTXO: {utxo_outpoint['transactionId']}:{utxo_outpoint['index']}")
    print(f"   Amount: {utxo_amount / 1e8:.4f} tKAS")
    print()

    # Build spend TX
    a_addr_str = info["a_address"]
    fee = 3000  # 0.00003 tKAS
    refund_amount = utxo_amount - fee

    if refund_amount < deposit:
        print(f"❌ UTXO too small for refund + fee! Need {deposit + fee}, have {utxo_amount}")
        await client.disconnect()
        return

    # Use create_transaction to get UTXO entry attached (needed for sighash)
    tx = kaspa.create_transaction(
        [covenant_entry],
        [kaspa.PaymentOutput(kaspa.Address(a_addr_str), refund_amount)],
        0,
        b"",
    )

    # Note: TN12 covpp branch supports covenant opcodes with version 0
    # No need to change version or call finalize()

    print(f"📝 Spend TX")
    print(f"   Refund to A: {refund_amount / 1e8:.4f} tKAS")
    print(f"   Fee: {fee / 1e8:.5f} tKAS")
    print(f"   TX version: {tx.version}")

    # Compute signature (sighash computed on this exact tx object)
    sig = kaspa.create_input_signature(tx, 0, b_privkey, kaspa.SighashType.All)
    sig_bytes = bytes.fromhex(sig) if isinstance(sig, str) else sig
    print(f"   Signature: {sig_bytes.hex()[:40]}... ({len(sig_bytes)} bytes)")

    # Build P2SH signature script: <sig_serialized> <OP_TRUE> <push redeem_script>
    # create_input_signature already returns script-serialized sig (with push opcode)
    # OP_TRUE (0x51) selects the IF branch (B reads)
    covenant_script = bytes.fromhex(info["covenant_script_hex"])
    sig_script = sig_bytes + bytes([0x51]) + push_data(covenant_script)
    print(f"   Sig script: {len(sig_script)} bytes")

    # Set signature script and sig_op_count directly on the SAME tx object
    # This ensures the submitted TX matches the one used for sighash computation
    tx.inputs[0].signature_script = sig_script
    tx.inputs[0].sig_op_count = 1  # 1 OP_CHECKSIG in redeem script

    print(f"   TX ID: {tx.id}")

    # Submit
    try:
        r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
        print(f"\n✅ Spend TX submitted! A gets refund automatically.")
        print(f"   Message was: {info['message']}")
        print(f"   Result: {r}")
    except Exception as e:
        print(f"\n❌ Submit failed: {e}")

        # Debug
        print(f"\n   Debug info:")
        print(f"   TX version: {tx.version}")
        print(f"   TX ID: {tx.id}")
        d = tx.serialize_to_dict()
        print(f"   TX dict inputs[0] sig_script: {d['inputs'][0].get('signatureScript', 'N/A')[:80]}...")
        print(f"   TX dict version: {d.get('version')}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
