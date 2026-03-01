#!/usr/bin/env python3
"""
🌊 Whisper Covenant — Send (A locks deposit + message for B)

Usage: python3 covenant_send.py [message]

Flow:
  1. Connect to TN12 kaspad
  2. Build covenant script: B can spend only if output[0] >= deposit to A
  3. Create P2SH of covenant script
  4. Send 0.2 tKAS to P2SH address with message in payload
  5. Print TX ID and covenant info for B to use
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
DEPOSIT_SOMPI = 20_000_000  # 0.2 tKAS
FEE_SOMPI = 10_000          # 0.0001 tKAS fee
FEE_BUFFER_SOMPI = 5_000    # extra sompi in covenant UTXO for B's spend fee
NATIVE_SUBNETWORK = "00" * 20

WALLET_PATH = os.path.expanduser("~/.secrets/testnet-wallet.json")

# ─── Helpers ──────────────────────────────────────────────────────

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


def build_covenant_script_with_timeout(
    a_spk_bytes: bytes, a_pubkey: bytes, b_pubkey: bytes, deposit: int, timeout_daa: int
) -> bytes:
    """
    Covenant script with CLTV timeout:
      OP_IF
        <a_spk> <0> OP_TX_OUTPUT_SPK OP_EQUAL OP_VERIFY
        <deposit> <0> OP_TX_OUTPUT_AMOUNT OP_GREATERTHANOREQUAL OP_VERIFY
        <b_pubkey> OP_CHECKSIG
      OP_ELSE
        <timeout_daa> OP_CHECKLOCKTIMEVERIFY OP_DROP
        <a_pubkey> OP_CHECKSIG
      OP_ENDIF
    """
    OP_IF = 0x63
    OP_ELSE = 0x67
    OP_ENDIF = 0x68
    OP_CLTV = 0xB0
    OP_DROP = 0x75
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
    # Note: Kaspa's CLTV pops the value (unlike Bitcoin's NOP behavior), so no OP_DROP needed
    s += bytes([OP_ELSE])
    s += push_int(timeout_daa)
    s += bytes([OP_CLTV])
    s += push_data(a_pubkey)
    s += bytes([OP_CHECKSIG])
    s += bytes([OP_ENDIF])
    return s


async def main():
    parser = argparse.ArgumentParser(description="Whisper Covenant — Send")
    parser.add_argument("--key", "-k", default=None, help="Sender private key (hex). Falls back to ~/.secrets/testnet-wallet.json")
    parser.add_argument("--to", default=None, help="Recipient address (default: send to self)")
    parser.add_argument("--timeout-offset", type=int, default=1000, help="CLTV timeout offset from current DAA (default: 1000 ≈ 100s)")
    parser.add_argument("message", nargs="*", default=["Hello from Whisper Covenant!"], help="Message text")
    args = parser.parse_args()

    message = " ".join(args.message) if args.message else "Hello from Whisper Covenant!"

    # Load private key
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
    a_spk = kaspa.pay_to_address_script(a_addr)
    # OP_TX_OUTPUT_SPK pushes: version(2 bytes BE) + script_bytes
    # ScriptPublicKey version for P2PK = 0
    a_spk_bytes = b'\x00\x00' + bytes.fromhex(a_spk.script)

    print(f"🌊 Whisper Covenant — Send")
    print(f"   A address: {a_addr_str}")
    print(f"   Message: {message}")
    print(f"   Deposit: {DEPOSIT_SOMPI / 1e8:.2f} tKAS")
    print()

    # Recipient
    if args.to:
        b_addr = kaspa.Address(args.to)
        b_spk = kaspa.pay_to_address_script(b_addr)
        # Extract x-only pubkey from P2PK script (skip version byte 0xac prefix etc)
        b_script_hex = b_spk.script
        # P2PK script: <32-byte-pubkey> OP_CHECKSIG → hex is 20{pubkey}ac
        b_pubkey_bytes = bytes.fromhex(b_script_hex[2:66])  # skip length byte "20", take 32 bytes
    else:
        b_pubkey_bytes = bytes.fromhex(a_xonly.to_string())
    a_pubkey_bytes = bytes.fromhex(a_xonly.to_string())

    # Connect to kaspad
    client = kaspa.RpcClient(url=WRPC_URL, encoding="borsh", network_id=NETWORK_ID)
    await client.connect()
    print("✅ Connected to kaspad")

    # Get current DAA score for timeout
    dag_info_pre = await client.get_block_dag_info()
    current_daa = int(dag_info_pre["virtualDaaScore"])
    timeout_daa = current_daa + args.timeout_offset
    print(f"   Current DAA: {current_daa}")
    print(f"   Timeout DAA: {timeout_daa} (current + 1000)")

    # Build covenant script with timeout
    covenant_script = build_covenant_script_with_timeout(
        a_spk_bytes, a_pubkey_bytes, b_pubkey_bytes, DEPOSIT_SOMPI, timeout_daa
    )
    print(f"📜 Covenant script ({len(covenant_script)} bytes): {covenant_script.hex()}")

    # Create P2SH
    p2sh_spk = kaspa.pay_to_script_hash_script(covenant_script)
    p2sh_addr = kaspa.address_from_script_public_key(p2sh_spk, NETWORK_TYPE)
    p2sh_addr_str = p2sh_addr.to_string()
    print(f"📦 P2SH address: {p2sh_addr_str}")
    print()

    # Get UTXOs
    result = await client.get_utxos_by_addresses({"addresses": [a_addr_str]})
    entries = result.get("entries", [])
    print(f"   Found {len(entries)} UTXOs")

    # Filter mature UTXOs (current_daa already fetched above)

    mature = []
    for e in entries:
        utxo = e["utxoEntry"]
        if utxo["isCoinbase"] and (current_daa - utxo["blockDaaScore"]) < 500:
            continue
        if utxo["amount"] >= DEPOSIT_SOMPI + FEE_SOMPI + 10000:
            mature.append(e)

    if not mature:
        print("❌ No suitable UTXO found!")
        await client.disconnect()
        return

    selected = mature[0]
    input_amount = selected["utxoEntry"]["amount"]
    print(f"   Selected UTXO: {selected['outpoint']['transactionId']}:{selected['outpoint']['index']}")
    print(f"   Amount: {input_amount / 1e8:.4f} tKAS")

    # Build TX using create_transaction (attaches UTXO entries for signing)
    lock_amount = DEPOSIT_SOMPI + FEE_BUFFER_SOMPI  # deposit + fee buffer for B's spend
    change = input_amount - lock_amount - FEE_SOMPI
    payload = json.dumps({
        "v": 2,
        "t": "message",
        "d": message,
        "a": {"from": a_addr_str, "timeout_daa": timeout_daa}
    }, ensure_ascii=False).encode("utf-8")

    tx = kaspa.create_transaction(
        [selected],
        [
            kaspa.PaymentOutput(kaspa.Address(p2sh_addr_str), lock_amount),
            kaspa.PaymentOutput(kaspa.Address(a_addr_str), change),
        ],
        0,  # fee already accounted for
        payload,
    )

    # Sign
    kaspa.sign_transaction(tx, [a_privkey], False)

    print(f"\n📝 TX ID: {tx.id}")
    print(f"   Outputs: lock={lock_amount/1e8:.4f} tKAS → P2SH (deposit {DEPOSIT_SOMPI/1e8:.2f} + fee buffer), change={change/1e8:.4f} tKAS → A")
    print(f"   Payload: {message}")

    # Submit
    try:
        r = await client.submit_transaction({"transaction": tx, "allow_orphan": False})
        tx_id = r.get("transactionId", tx.id)
        print(f"\n✅ TX submitted! ID: {tx_id}")
    except Exception as e:
        print(f"\n❌ Submit failed: {e}")
        await client.disconnect()
        return

    # Save covenant info for B
    covenant_info = {
        "tx_id": tx_id,
        "covenant_script_hex": covenant_script.hex(),
        "p2sh_address": p2sh_addr_str,
        "p2sh_spk": p2sh_spk.script,
        "a_address": a_addr_str,
        "a_spk": a_spk.script,
        "a_pubkey": a_xonly.to_string(),
        "b_pubkey": b_pubkey_bytes.hex(),
        "deposit_sompi": DEPOSIT_SOMPI,
        "timeout_daa": timeout_daa,
        "message": message,
        "output_index": 0,
    }

    info_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covenant_info.json")
    with open(info_path, "w") as f:
        json.dump(covenant_info, f, indent=2)
    print(f"💾 Covenant info saved to {info_path}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
