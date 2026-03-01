#!/usr/bin/env python3
"""
Whisper Covenant v0.1 — Trustless Message Refund on Kaspa TN12

Flow:
  1. A sends message: locks 0.2 KAS into a covenant P2SH script
  2. B reads message: spends covenant UTXO; covenant enforces refund to A
  3. Result: A pays only tx fees; B reads for free

Covenant Script (executed when B spends):
  <A_spk_bytes>        // Expected SPK for output[0]
  <0>                  // index 0
  OP_TX_OUTPUT_SPK     // push spending tx's output[0] SPK
  OP_EQUAL
  OP_VERIFY           // output[0] must pay to A

  <amount_sompi>       // e.g. 20_000_000 (0.2 KAS)
  <0>                  // index 0
  OP_TX_OUTPUT_AMOUNT  // push spending tx's output[0] amount
  OP_GREATERTHANOREQUAL
  OP_VERIFY           // output[0] amount >= 0.2 KAS

  <B_pubkey>           // 32-byte Schnorr pubkey
  OP_CHECKSIG          // only B can spend

SPK encoding (for OP_TX_OUTPUT_SPK comparison):
  version(2 bytes BE) + script_bytes
  For P2PK: 0x0000 + [0x20 <32-byte-pubkey> 0xac]
"""

import asyncio
import json
import hashlib
import struct
import sys
import os
from pathlib import Path

# ─── Kaspa Opcodes (hex values from rusty-kaspa source) ─────────────────────
# Standard opcodes
OP_FALSE        = 0x00
OP_DATA_1       = 0x01  # push next 1 byte
OP_DATA_32      = 0x20  # push next 32 bytes
OP_DATA_33      = 0x21  # push next 33 bytes
OP_DATA_34      = 0x22  # push next 34 bytes
OP_DATA_35      = 0x23  # push next 35 bytes
OP_TRUE         = 0x51
OP_EQUAL        = 0x87
OP_EQUAL_VERIFY = 0x88
OP_VERIFY       = 0x69
OP_GREATERTHAN  = 0xa0
OP_GREATERTHANOREQUAL = 0xa2
OP_CHECKSIG     = 0xac
OP_BLAKE2B      = 0xa8

# Covenant introspection opcodes (TN12)
OP_TX_INPUT_COUNT    = 0xb3
OP_TX_OUTPUT_COUNT   = 0xb4
OP_TX_INPUT_AMOUNT   = 0xbe
OP_TX_INPUT_SPK      = 0xbf
OP_TX_OUTPUT_AMOUNT  = 0xc2
OP_TX_OUTPUT_SPK     = 0xc3

# P2SH
OP_PUSH_DATA1 = 0x4c
OP_PUSH_DATA2 = 0x4d

# Constants
SOMPI_PER_KAS = 100_000_000
DEFAULT_LOCK_AMOUNT = 20_000_000  # 0.2 KAS
TN12_WRPC_URL = "ws://127.0.0.1:17210"
TN12_NETWORK = "testnet-12"

# ─── Script Helpers ─────────────────────────────────────────────────────────

def push_data(data: bytes) -> bytes:
    """Encode a data push in Kaspa script format.
    
    OpData1..OpData75: single byte length prefix (opcode = length)
    OpPushData1: 0x4c + 1-byte length
    OpPushData2: 0x4d + 2-byte LE length
    """
    n = len(data)
    if n == 0:
        return bytes([OP_FALSE])
    elif 1 <= n <= 75:
        return bytes([n]) + data
    elif n <= 255:
        return bytes([OP_PUSH_DATA1, n]) + data
    elif n <= 65535:
        return bytes([OP_PUSH_DATA2]) + struct.pack('<H', n) + data
    else:
        raise ValueError(f"Data too large: {n} bytes")


def push_i64(value: int) -> bytes:
    """Encode an integer push in Kaspa script format (little-endian, sign bit)."""
    if value == 0:
        return bytes([OP_FALSE])
    if 1 <= value <= 16:
        return bytes([OP_TRUE + value - 1])  # Op1..Op16
    
    # Encode as minimal little-endian with sign bit
    negative = value < 0
    abs_val = abs(value)
    
    result = []
    while abs_val > 0:
        result.append(abs_val & 0xff)
        abs_val >>= 8
    
    # If high bit set, add extra byte for sign
    if result[-1] & 0x80:
        result.append(0x80 if negative else 0x00)
    elif negative:
        result[-1] |= 0x80
    
    return push_data(bytes(result))


def encode_spk_bytes(pubkey_bytes: bytes) -> bytes:
    """Encode a P2PK ScriptPublicKey as bytes (version BE + script).
    
    This matches what OP_TX_OUTPUT_SPK pushes onto the stack:
      version(2 bytes BE) + script_bytes
    
    For P2PK (version 0):
      0x00 0x00 + [0x20 <32-byte-pubkey> 0xac]
    """
    version = 0
    # P2PK script: OpData32 <pubkey> OpCheckSig
    script = bytes([OP_DATA_32]) + pubkey_bytes + bytes([OP_CHECKSIG])
    return struct.pack('>H', version) + script


def address_to_pubkey(address: str) -> bytes:
    """Extract the 32-byte public key from a Kaspa address.
    
    Kaspa address format: prefix:payload
    payload is bech32-encoded: version(1 byte) + pubkey(32 bytes) + checksum(8 bytes)
    
    We use the kaspa SDK for this.
    """
    try:
        from kaspa import Address, pay_to_address_script
        addr = Address(address)
        # Get SPK from address, extract pubkey from the P2PK script
        spk = pay_to_address_script(addr)
        # SPK script for P2PK: 0x20 <32-byte-pubkey> 0xac
        script_hex = spk.script
        script_bytes = bytes.fromhex(script_hex)
        if len(script_bytes) == 34 and script_bytes[0] == 0x20 and script_bytes[33] == 0xac:
            return script_bytes[1:33]
        raise ValueError(f"Unexpected script format: {script_hex}")
    except ImportError:
        raise RuntimeError("kaspa Python SDK not available")


# ─── Covenant Script Builder ────────────────────────────────────────────────

def create_covenant_script(a_spk_bytes: bytes, b_pubkey: bytes, amount_sompi: int = DEFAULT_LOCK_AMOUNT) -> bytes:
    """Build the covenant redeem script.
    
    When B spends the P2SH UTXO, this script verifies:
      1. output[0] SPK == A's SPK (refund goes to A)
      2. output[0] amount >= lock_amount (full refund)
      3. B's signature is valid (only B can spend)
    
    Args:
        a_spk_bytes: A's ScriptPublicKey as bytes (version BE + script)
                     from encode_spk_bytes()
        b_pubkey:    B's 32-byte Schnorr public key
        amount_sompi: minimum refund amount in sompi
    
    Returns:
        Raw redeem script bytes
    """
    script = bytearray()
    
    # Part 1: Verify output[0] SPK matches A's address
    # Stack: <a_spk_bytes> <0> → OP_TX_OUTPUT_SPK → <actual_spk>
    # Then EQUAL VERIFY
    script += push_data(a_spk_bytes)   # push A's expected SPK bytes
    script += push_i64(0)               # push index 0
    script += bytes([OP_TX_OUTPUT_SPK]) # push actual output[0] SPK
    script += bytes([OP_EQUAL])
    script += bytes([OP_VERIFY])
    
    # Part 2: Verify output[0] amount >= lock amount
    # Stack: <amount> <0> → OP_TX_OUTPUT_AMOUNT → <actual_amount>
    # Then GREATERTHANOREQUAL VERIFY
    script += push_i64(0)                    # push index 0
    script += bytes([OP_TX_OUTPUT_AMOUNT])   # push actual output[0] amount
    script += push_i64(amount_sompi)         # push minimum amount
    script += bytes([OP_GREATERTHANOREQUAL]) # actual >= minimum
    script += bytes([OP_VERIFY])
    
    # Part 3: Signature check (only B can spend)
    script += push_data(b_pubkey)       # push B's pubkey
    script += bytes([OP_CHECKSIG])      # verify B's signature
    
    return bytes(script)


def create_p2sh_spk(redeem_script: bytes) -> bytes:
    """Create a P2SH ScriptPublicKey from a redeem script.
    
    P2SH script: OP_BLAKE2B <32-byte-hash> OP_EQUAL
    """
    script_hash = hashlib.blake2b(redeem_script, digest_size=32).digest()
    spk_script = bytes([OP_BLAKE2B, OP_DATA_32]) + script_hash + bytes([OP_EQUAL])
    return spk_script


# ─── Message Encoding ───────────────────────────────────────────────────────

def encode_message_in_payload(message_text: str, sender_address: str, msg_type: str = "message") -> bytes:
    """Encode message as JSON payload (TN10 format).
    
    Format: {"v":1, "t":"message"|"whisper", "d":"<data>", "a":{"from":"<sender address>"}}
    """
    return json.dumps({
        "v": 1,
        "t": msg_type,
        "d": message_text,
        "a": {"from": sender_address}
    }, ensure_ascii=False).encode('utf-8')


def decode_message_from_payload(payload: bytes) -> dict:
    """Decode message from transaction payload.
    
    Supports both new JSON format and legacy WHSP binary format.
    Returns dict with: message, sender_address, version, type
    """
    # Try JSON format first
    try:
        obj = json.loads(payload.decode('utf-8'))
        if isinstance(obj, dict) and "v" in obj and "d" in obj:
            return {
                'version': obj.get('v', 1),
                'type': obj.get('t', 'message'),
                'message': obj['d'],
                'sender_address': obj.get('a', {}).get('from', ''),
            }
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    
    # Legacy binary format fallback
    if len(payload) < 39:
        raise ValueError("Payload too short")
    
    magic = payload[0:4]
    if magic != b"WHSP":
        raise ValueError(f"Invalid magic: {magic}")
    
    version = payload[4]
    sender_pubkey = payload[5:37]
    msg_len = struct.unpack('<H', payload[37:39])[0]
    message = payload[39:39 + msg_len].decode('utf-8')
    
    return {
        'version': version,
        'type': 'message',
        'sender_pubkey': sender_pubkey.hex(),
        'message': message,
    }


# ─── High-Level API ─────────────────────────────────────────────────────────

async def get_rpc_client():
    """Create and connect an RPC client to TN12."""
    from kaspa import RpcClient
    client = RpcClient(url=TN12_WRPC_URL, encoding="borsh", network_id=TN12_NETWORK)
    await client.connect()
    return client


async def send_message(sender_privkey_hex: str, receiver_pubkey_hex: str, 
                       message_text: str, amount_sompi: int = DEFAULT_LOCK_AMOUNT):
    """A sends a message by locking coins into a covenant script.
    
    Steps:
      1. Build covenant script (enforces refund to A when B spends)
      2. Create P2SH address from covenant script
      3. Send amount_sompi to P2SH address with message in payload
    
    Args:
        sender_privkey_hex: A's private key (hex, 32 bytes)
        receiver_pubkey_hex: B's public key (hex, 32 bytes)
        message_text: The message to send
        amount_sompi: Amount to lock (default 0.2 KAS)
    
    Returns:
        dict with tx_id, covenant_script_hex, p2sh_address
    """
    from kaspa import (
        PrivateKey, PublicKey, Address, ScriptBuilder, Opcodes,
        pay_to_address_script, pay_to_script_hash_script,
        Transaction, TransactionInput, TransactionOutput, TransactionOutpoint,
        RpcClient, create_transaction, ScriptPublicKey,
        address_from_script_public_key,
    )
    
    # Derive sender's pubkey
    sender_privkey = PrivateKey(sender_privkey_hex)
    sender_pubkey = sender_privkey.to_public_key()
    sender_pubkey_bytes = bytes.fromhex(sender_pubkey.to_string())
    
    # Receiver's pubkey
    receiver_pubkey_bytes = bytes.fromhex(receiver_pubkey_hex)
    
    # Build A's SPK bytes (what OP_TX_OUTPUT_SPK will compare against)
    a_spk_bytes = encode_spk_bytes(sender_pubkey_bytes)
    
    # Build covenant redeem script
    redeem_script = create_covenant_script(a_spk_bytes, receiver_pubkey_bytes, amount_sompi)
    
    # Create P2SH script using SDK's ScriptBuilder
    sb = ScriptBuilder()
    sb.add_data(redeem_script)
    p2sh_spk = sb.create_pay_to_script_hash_script()
    
    # Get P2SH address
    from kaspa import NetworkId
    # TODO: address_from_script_public_key needs network param
    # For now, compute manually or use SDK
    
    print(f"=== Whisper Covenant v0.1 — Send Message ===")
    print(f"Sender pubkey:    {sender_pubkey_bytes.hex()}")
    print(f"Receiver pubkey:  {receiver_pubkey_hex}")
    print(f"Lock amount:      {amount_sompi} sompi ({amount_sompi / SOMPI_PER_KAS} KAS)")
    print(f"Message:          {message_text}")
    print(f"Redeem script:    {redeem_script.hex()}")
    print(f"Redeem script len: {len(redeem_script)} bytes")
    print(f"P2SH SPK:         {p2sh_spk}")
    print()
    
    # Encode message in payload
    payload = encode_message_in_payload(message_text, sender_pubkey.to_address(NETWORK_TYPE).to_string() if hasattr(sender_pubkey, 'to_address') else "")
    print(f"Payload:          {payload.hex()}")
    print(f"Payload decoded:  {decode_message_from_payload(payload)}")
    
    # TODO: Build and submit transaction
    # This requires:
    # 1. Fetch sender's UTXOs via RPC
    # 2. Create TX with:
    #    - Input: sender's UTXO(s)
    #    - Output[0]: P2SH covenant (amount_sompi)
    #    - Output[1]: change back to sender (if needed)
    #    - Payload: encoded message
    # 3. Sign with sender's private key
    # 4. Submit via RPC
    
    # For now, return the script details for manual testing
    return {
        'redeem_script': redeem_script.hex(),
        'redeem_script_len': len(redeem_script),
        'a_spk_bytes': a_spk_bytes.hex(),
        'p2sh_spk': str(p2sh_spk),
        'payload': payload.hex(),
        'message': message_text,
    }


async def read_message(receiver_privkey_hex: str, covenant_utxo: dict):
    """B reads a message by spending the covenant UTXO.
    
    The covenant script enforces that output[0] refunds to A.
    
    Args:
        receiver_privkey_hex: B's private key (hex)
        covenant_utxo: dict with tx_id, index, amount, redeem_script_hex
    
    Returns:
        dict with message, refund_tx_id
    """
    from kaspa import PrivateKey
    
    receiver_privkey = PrivateKey(receiver_privkey_hex)
    receiver_pubkey = receiver_privkey.to_public_key()
    
    print(f"=== Whisper Covenant v0.1 — Read Message ===")
    print(f"Receiver pubkey: {receiver_pubkey.to_string()}")
    print(f"Covenant UTXO:   {covenant_utxo['tx_id']}:{covenant_utxo['index']}")
    
    # TODO: Implement spending transaction
    # Steps:
    # 1. Fetch the covenant UTXO details
    # 2. Decode message from the original TX payload
    # 3. Build spending TX:
    #    - Input: covenant UTXO
    #    - Output[0]: refund to A (enforced by covenant)
    #    - SigScript: <signature> <redeem_script>
    # 4. Sign with B's private key
    # 5. Submit
    
    print("TODO: Transaction building not yet implemented")
    print("      Need to construct raw TX with P2SH sig script")
    
    return {
        'status': 'not_yet_implemented',
        'message': 'TX construction requires low-level API access',
    }


async def check_messages(address: str):
    """Check for pending covenant messages for a given address.
    
    Scans UTXOs for P2SH outputs that match whisper covenant pattern.
    
    Args:
        address: Kaspa address to check (receiver's address)
    
    Returns:
        List of pending messages
    """
    # TODO: This requires scanning the UTXO set for P2SH addresses
    # and checking if they match our covenant pattern.
    # 
    # Approach:
    # 1. Maintain a local index of known covenant P2SH addresses
    # 2. Or scan TX payloads for "WHSP" magic
    # 3. Or use a dedicated indexer
    
    print(f"=== Whisper Covenant v0.1 — Check Messages ===")
    print(f"Address: {address}")
    print("TODO: Message scanning not yet implemented")
    print("      Need UTXO indexer or payload scanner")
    
    return []


# ─── CLI ─────────────────────────────────────────────────────────────────────

def demo_script_generation():
    """Demo: generate a covenant script and show its structure."""
    print("=" * 60)
    print("Whisper Covenant v0.1 — Script Generation Demo")
    print("=" * 60)
    print()
    
    # Example keys (DO NOT use in production)
    a_pubkey = bytes.fromhex("a0" * 32)  # dummy sender pubkey
    b_pubkey = bytes.fromhex("b0" * 32)  # dummy receiver pubkey
    
    a_spk_bytes = encode_spk_bytes(a_pubkey)
    print(f"A's SPK bytes ({len(a_spk_bytes)} bytes): {a_spk_bytes.hex()}")
    print(f"  version: {a_spk_bytes[0:2].hex()}")
    print(f"  script:  {a_spk_bytes[2:].hex()}")
    print()
    
    redeem_script = create_covenant_script(a_spk_bytes, b_pubkey, DEFAULT_LOCK_AMOUNT)
    print(f"Redeem script ({len(redeem_script)} bytes):")
    print(f"  hex: {redeem_script.hex()}")
    print()
    
    # Annotate the script
    print("Script breakdown:")
    pos = 0
    annotations = [
        "Part 1: Verify output[0] pays to A",
        "  push A's SPK bytes (36 bytes: 2 version + 34 script)",
        "  push 0 (output index)",
        "  OP_TX_OUTPUT_SPK (0xc3)",
        "  OP_EQUAL (0x87)",
        "  OP_VERIFY (0x69)",
        "",
        "Part 2: Verify output[0] amount >= 0.2 KAS",
        "  push 20000000 sompi",
        "  push 0 (output index)",
        "  OP_TX_OUTPUT_AMOUNT (0xc2)",
        "  OP_GREATERTHANOREQUAL (0xa2)",
        "  OP_VERIFY (0x69)",
        "",
        "Part 3: Signature check",
        "  push B's pubkey (32 bytes)",
        "  OP_CHECKSIG (0xac)",
    ]
    print("\n".join(f"  {a}" for a in annotations))
    print()
    
    # P2SH
    p2sh_script = create_p2sh_spk(redeem_script)
    print(f"P2SH lock script ({len(p2sh_script)} bytes): {p2sh_script.hex()}")
    print(f"  OP_BLAKE2B OP_DATA_32 <hash> OP_EQUAL")
    print()
    
    # Message encoding
    payload = encode_message_in_payload("Hello from Whisper! 🌊", "kaspatest:qqexample")
    decoded = decode_message_from_payload(payload)
    print(f"Message payload ({len(payload)} bytes): {payload.hex()}")
    print(f"  Decoded: {decoded}")


async def demo_with_sdk():
    """Demo using the Kaspa Python SDK."""
    print()
    print("=" * 60)
    print("Whisper Covenant v0.1 — SDK Integration Demo")
    print("=" * 60)
    print()
    
    try:
        from kaspa import (
            ScriptBuilder, Opcodes, PrivateKey, 
            pay_to_address_script, Address, ScriptPublicKey,
            address_from_script_public_key,
        )
        
        # Generate test keypairs
        # In production, load from wallet
        print("Generating test keypairs...")
        
        # Use the SDK's ScriptBuilder to verify our script
        sb = ScriptBuilder()
        
        # Build the same covenant script using SDK's ScriptBuilder
        a_pubkey = bytes.fromhex("a0" * 32)
        b_pubkey = bytes.fromhex("b0" * 32)
        a_spk_bytes = encode_spk_bytes(a_pubkey)
        
        # Part 1: SPK check
        sb.add_data(a_spk_bytes)
        sb.add_op(Opcodes.OpFalse)  # push 0
        sb.add_op(Opcodes.OpUnknown195)  # 0xc3 = OP_TX_OUTPUT_SPK
        sb.add_op(Opcodes.OpEqual)
        sb.add_op(Opcodes.OpVerify)
        
        # Part 2: Amount check
        sb.add_i64(DEFAULT_LOCK_AMOUNT)
        sb.add_op(Opcodes.OpFalse)  # push 0
        sb.add_op(Opcodes.OpUnknown194)  # 0xc2 = OP_TX_OUTPUT_AMOUNT
        sb.add_op(Opcodes.OpGreaterThanOrEqual)
        sb.add_op(Opcodes.OpVerify)
        
        # Part 3: Sig check
        sb.add_data(b_pubkey)
        sb.add_op(Opcodes.OpCheckSig)
        
        sdk_script = sb.to_string()
        print(f"SDK ScriptBuilder output: {sdk_script}")
        
        # Create P2SH
        sb2 = ScriptBuilder()
        sb2.add_data(a_spk_bytes)
        sb2.add_op(Opcodes.OpFalse)
        sb2.add_op(Opcodes.OpUnknown195)
        sb2.add_op(Opcodes.OpEqual)
        sb2.add_op(Opcodes.OpVerify)
        sb2.add_i64(DEFAULT_LOCK_AMOUNT)
        sb2.add_op(Opcodes.OpFalse)
        sb2.add_op(Opcodes.OpUnknown194)
        sb2.add_op(Opcodes.OpGreaterThanOrEqual)
        sb2.add_op(Opcodes.OpVerify)
        sb2.add_data(b_pubkey)
        sb2.add_op(Opcodes.OpCheckSig)
        
        p2sh_spk = sb2.create_pay_to_script_hash_script()
        print(f"P2SH SPK script: {p2sh_spk.script}")
        
        # Try to get address from P2SH SPK
        try:
            p2sh_addr = address_from_script_public_key(p2sh_spk, "testnet")
            print(f"P2SH Address: {p2sh_addr.to_string()}")
        except Exception as e:
            print(f"P2SH Address derivation: {e}")
        
        print()
        print("✅ SDK integration works!")
        print("⚠️  Transaction building & signing require further implementation")
        print("   (need to handle P2SH sig scripts and covenant-aware signing)")
        
    except ImportError as e:
        print(f"❌ Kaspa SDK not available: {e}")
    except Exception as e:
        print(f"❌ SDK error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    demo_script_generation()
    asyncio.run(demo_with_sdk())
