#!/usr/bin/env python3
"""
🌊 Whisper Covenant PoC — Trustless Decode Ack on Kaspa TN12

A sends a message to B:
  1. A creates a covenant UTXO that locks 0.2 tKAS
  2. The covenant script enforces: when B spends it, output[0] must pay ≥ 0.2 tKAS back to A
  3. B spends the UTXO (= "read ack") → A automatically gets refund
  4. A's net cost = TX fee only

Covenant Script (pseudo):
  <A_spk_bytes> <0> OP_TX_OUTPUT_SPK OP_EQUAL OP_VERIFY    // output[0] must go to A
  <deposit>     <0> OP_TX_OUTPUT_AMOUNT OP_GREATERTHANOREQUAL OP_VERIFY  // ≥ deposit
  <B_pubkey> OP_CHECKSIG                                     // only B can spend

TN12 Opcodes used:
  0xc3 = OpTxOutputSpk(idx)    → push output[idx].script_public_key
  0xc2 = OpTxOutputAmount(idx) → push output[idx].value
  0xb9 = OpTxInputIndex         → push current input index
"""

import asyncio
import json
import hashlib
import sys
import os

# Add kaspa SDK path if needed
sys.path.insert(0, os.path.expanduser("~/nami-backpack/projects/nami-kaspa-miner"))

# ─── Config ───────────────────────────────────────────────────────
TESTNET = True
GRPC_HOST = "localhost"
GRPC_PORT = 16210  # TN12 gRPC port
WRPC_PORT = 17210  # TN12 wRPC port

DEPOSIT_SOMPI = 20_000_000  # 0.2 tKAS

# Wallet paths
WALLET_A_PATH = os.path.expanduser("~/.secrets/testnet-wallet.json")
# B wallet - we'll generate or use a second key

# ─── Script Opcodes ──────────────────────────────────────────────
# Standard opcodes
OP_DUP          = 0x76
OP_HASH256      = 0xaa  # not used here but reference
OP_EQUALVERIFY  = 0x88
OP_CHECKSIG     = 0xac
OP_EQUAL        = 0x87
OP_VERIFY       = 0x69
OP_GREATERTHANOREQUAL = 0xa2
OP_0            = 0x00
OP_1            = 0x51
OP_TRUE         = 0x51

# TN12 Covenant introspection opcodes
OP_TX_VERSION          = 0xb2
OP_TX_INPUT_COUNT      = 0xb3
OP_TX_OUTPUT_COUNT     = 0xb4
OP_TX_LOCKTIME         = 0xb5
OP_TX_PAYLOAD_SUBSTR   = 0xb8
OP_TX_INPUT_INDEX      = 0xb9
OP_TX_INPUT_SEQ        = 0xbd
OP_TX_INPUT_AMOUNT     = 0xbe
OP_TX_INPUT_SPK        = 0xbf
OP_TX_OUTPUT_AMOUNT    = 0xc2
OP_TX_OUTPUT_SPK       = 0xc3
OP_TX_PAYLOAD_LEN      = 0xc4


def push_data(data: bytes) -> bytes:
    """Create script push for arbitrary data."""
    n = len(data)
    if n == 0:
        return bytes([OP_0])
    if n <= 75:
        return bytes([n]) + data
    elif n <= 255:
        return bytes([0x4c, n]) + data  # OP_PUSHDATA1
    elif n <= 65535:
        return bytes([0x4d]) + n.to_bytes(2, 'little') + data  # OP_PUSHDATA2
    else:
        return bytes([0x4e]) + n.to_bytes(4, 'little') + data  # OP_PUSHDATA4


def push_int(val: int) -> bytes:
    """Push a small integer onto the stack (script number encoding)."""
    if val == 0:
        return bytes([OP_0])
    if 1 <= val <= 16:
        return bytes([0x50 + val])  # OP_1 through OP_16
    if val == -1:
        return bytes([0x4f])  # OP_1NEGATE
    # CScriptNum encoding
    neg = val < 0
    abs_val = abs(val)
    result = []
    while abs_val > 0:
        result.append(abs_val & 0xff)
        abs_val >>= 8
    if result[-1] & 0x80:
        result.append(0x80 if neg else 0x00)
    elif neg:
        result[-1] |= 0x80
    return push_data(bytes(result))


def build_covenant_script(a_spk_bytes: bytes, b_pubkey: bytes, deposit_sompi: int) -> bytes:
    """
    Build the covenant locking script.
    
    When B spends this UTXO, the script verifies:
    1. output[0].script_public_key == A's address (refund goes to A)
    2. output[0].amount >= deposit (full refund amount)
    3. B's signature is valid (only B can spend)
    
    Script:
        <a_spk_bytes> <0> OP_TX_OUTPUT_SPK OP_EQUAL OP_VERIFY
        <deposit> <0> OP_TX_OUTPUT_AMOUNT OP_GREATERTHANOREQUAL OP_VERIFY
        <b_pubkey> OP_CHECKSIG
    """
    script = b""
    
    # Part 1: Verify output[0] pays to A
    script += push_data(a_spk_bytes)    # push A's SPK bytes
    script += push_int(0)               # push index 0
    script += bytes([OP_TX_OUTPUT_SPK]) # get output[0].spk
    script += bytes([OP_EQUAL])         # compare
    script += bytes([OP_VERIFY])        # must be equal
    
    # Part 2: Verify output[0] amount >= deposit
    script += push_int(deposit_sompi)   # push deposit amount
    script += push_int(0)               # push index 0
    script += bytes([OP_TX_OUTPUT_AMOUNT])  # get output[0].amount
    script += bytes([OP_GREATERTHANOREQUAL])  # amount >= deposit
    script += bytes([OP_VERIFY])        # must be true
    
    # Part 3: Only B can spend (signature check)
    script += push_data(b_pubkey)       # push B's pubkey
    script += bytes([OP_CHECKSIG])      # verify B's signature
    
    return script


def build_covenant_script_with_timeout(
    a_spk_bytes: bytes, 
    a_pubkey: bytes,
    b_pubkey: bytes, 
    deposit_sompi: int,
    timeout_daa: int
) -> bytes:
    """
    Extended version with timeout — if B doesn't read within timeout,
    A can reclaim the deposit.
    
    Script (simplified):
        OP_IF
            // B reads: covenant check + B signs
            <a_spk_bytes> <0> OP_TX_OUTPUT_SPK OP_EQUAL OP_VERIFY
            <deposit> <0> OP_TX_OUTPUT_AMOUNT OP_GREATERTHANOREQUAL OP_VERIFY
            <b_pubkey> OP_CHECKSIG
        OP_ELSE
            // A reclaims after timeout
            <timeout_daa> OP_CHECKLOCKTIMEVERIFY OP_DROP
            <a_pubkey> OP_CHECKSIG
        OP_ENDIF
    """
    OP_IF       = 0x63
    OP_ELSE     = 0x67
    OP_ENDIF    = 0x68
    OP_CLTV     = 0xb0
    OP_DROP     = 0x75
    
    script = b""
    
    # IF branch: B reads message
    script += bytes([OP_IF])
    script += push_data(a_spk_bytes)
    script += push_int(0)
    script += bytes([OP_TX_OUTPUT_SPK])
    script += bytes([OP_EQUAL])
    script += bytes([OP_VERIFY])
    script += push_int(deposit_sompi)
    script += push_int(0)
    script += bytes([OP_TX_OUTPUT_AMOUNT])
    script += bytes([OP_GREATERTHANOREQUAL])
    script += bytes([OP_VERIFY])
    script += push_data(b_pubkey)
    script += bytes([OP_CHECKSIG])
    
    # ELSE branch: A reclaims after timeout
    script += bytes([OP_ELSE])
    script += push_int(timeout_daa)
    script += bytes([OP_CLTV])
    script += bytes([OP_DROP])
    script += push_data(a_pubkey)
    script += bytes([OP_CHECKSIG])
    
    script += bytes([OP_ENDIF])
    
    return script


# ─── Demo / Test ─────────────────────────────────────────────────

def demo():
    """Demo: build and display covenant scripts."""
    # Fake keys for demonstration
    a_spk = bytes.fromhex("20" + "aa" * 32)  # P2PK-style SPK placeholder
    b_pubkey = bytes(range(32))               # Schnorr pubkey (32 bytes)
    a_pubkey = bytes(range(32, 64))
    
    print("🌊 Whisper Covenant PoC")
    print("=" * 60)
    
    # Basic version
    script = build_covenant_script(a_spk, b_pubkey, DEPOSIT_SOMPI)
    print(f"\n📜 Basic Covenant Script ({len(script)} bytes):")
    print(f"   Hex: {script.hex()}")
    print(f"   Deposit: {DEPOSIT_SOMPI} sompi ({DEPOSIT_SOMPI / 1e8:.1f} tKAS)")
    
    # With timeout
    script2 = build_covenant_script_with_timeout(
        a_spk, a_pubkey, b_pubkey, DEPOSIT_SOMPI, 
        timeout_daa=100_000  # ~10,000 seconds ≈ ~2.8 hours
    )
    print(f"\n📜 Covenant + Timeout Script ({len(script2)} bytes):")
    print(f"   Hex: {script2.hex()}")
    print(f"   Timeout: 100,000 DAA (~2.8 hours)")
    
    print("\n" + "=" * 60)
    print("📋 Flow:")
    print("  1. A → covenant UTXO (lock 0.2 tKAS + encrypted msg in payload)")
    print("  2. B monitors for covenant UTXOs addressed to B's pubkey")
    print("  3. B spends UTXO → covenant forces output[0] = 0.2 to A")
    print("  4. A gets refund automatically. Cost = fee only! ✨")
    print("\n  Timeout path:")
    print("  5. If B doesn't read → A reclaims after timeout via ELSE branch")
    
    print("\n" + "=" * 60)
    print("🔧 Next steps:")
    print("  - [ ] Connect to TN12 gRPC and broadcast real TX")
    print("  - [ ] Build spend TX (B reads message)")
    print("  - [ ] Test timeout reclaim (A reclaims)")
    print("  - [ ] Add encrypted payload (ECDH shared secret)")
    print("  - [ ] Integrate with Whisper protocol")


if __name__ == "__main__":
    demo()
