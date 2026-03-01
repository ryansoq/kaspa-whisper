# 🌊 Whisper Covenant v2

**Trustless encrypted messaging on Kaspa TN12 using covenant introspection opcodes.**

Private keys **NEVER** leave your machine. Sign locally, broadcast online.

## Concept

A sends a message to B by locking 0.2 KAS into a covenant script. When B reads the message (spends the UTXO), the covenant **enforces** that 0.2 KAS is refunded to A. Result: A only pays tx fees, B reads for free.

```
A (sender)                     Covenant UTXO                    B (receiver)
    │                               │                               │
    │  encode.py (local)            │                               │
    │  ├─ ECIES encrypt(B_pub)      │                               │
    │  ├─ sign TX(A_priv)           │                               │
    │  └─ submit to kaspad          │                               │
    ├──── lock 0.2 KAS ────────────►│ P2SH script                   │
    │     + JSON payload            │ enforces refund to A          │
    │                               │                               │
    │                               │  decode.py (local)            │
    │                               │  ├─ ECIES decrypt(B_priv)  ◄──┤
    │                               │  ├─ sign refund TX(B_priv)    │
    │◄──── 0.2 KAS refund ─────────│  └─ submit to kaspad          │
    │     (enforced by script)      │                               │
```

## Security Model

| Zone | Tools | What happens |
|------|-------|-------------|
| 🏠 Local | `encode.py`, `decode.py` | Encryption, signing, decryption — all with your private key |
| 🌐 API | contacts, inbox, broadcast | Public data queries and pre-signed TX relay |

The API server **never** sees private keys. Even if compromised, attackers can only see encrypted ciphertext and public keys.

## Message Format

JSON payload in transaction: `{v, t, d, a}`

| Field | Description |
|-------|-------------|
| `v` | Version, currently `1` |
| `t` | Type: `whisper` (encrypted) / `message` (plaintext) / `ack` (read receipt) |
| `d` | Data: ECIES ciphertext hex / plaintext string / original TX ID |
| `a` | Attributes: covenant metadata (see below) |

### The `a` Field — On-Chain Self-Containment 🆕

For `whisper`/`message` types, `a` contains **full covenant metadata**:

```json
{
  "from": "kaspatest:qq...",     // sender address
  "script": "0a0b0c...",         // covenant redeem script (hex)
  "spk": "20abcd...ac",          // sender's ScriptPublicKey
  "deposit": 20000000             // deposit in sompi
}
```

This means **anyone with the recipient's private key can fully reconstruct the covenant and decrypt offline** — no server, no file exchange needed. The chain IS the database.

For `ack` type: `{"time": <unix_timestamp>}`

### whisper — Encrypted Message

```json
{"v":1, "t":"whisper", "d":"<ECIES ciphertext hex>", "a":{"from":"kaspatest:qq..."}}
```

### message — Plaintext Message

```json
{"v":1, "t":"message", "d":"Hello Bob!", "a":{"from":"kaspatest:qq..."}}
```

### ack — Read Receipt

```json
{"v":1, "t":"ack", "d":"<original TX ID>", "a":{"time":1771322000}}
```

## Encryption

- **Algorithm**: ECIES (secp256k1)
- **Public key**: 33 bytes compressed (`02` prefix + x-only pubkey from Kaspa address)
- **Library**: Python `eciespy`
- Same keypair as Kaspa wallet — no extra keys needed

## Covenant Script

```
// Redeem script (inside P2SH):

<A_spk_bytes>          // A's ScriptPublicKey bytes (version + script)
OP_FALSE               // output index 0
OP_TX_OUTPUT_SPK       // introspect spending TX's output[0] SPK
OP_EQUAL
OP_VERIFY              // ✓ output[0] pays to A

<deposit_sompi>        // 0.2 KAS = 20000000 sompi
OP_FALSE               // output index 0
OP_TX_OUTPUT_AMOUNT    // introspect spending TX's output[0] amount
OP_GREATERTHANOREQUAL
OP_VERIFY              // ✓ output[0] ≥ 0.2 KAS

<B_pubkey>             // 32-byte Schnorr pubkey
OP_CHECKSIG            // ✓ only B can spend
```

### Opcode Reference (TN12)

| Opcode | Hex | Stack Effect |
|--------|-----|--------------|
| `OP_TX_OUTPUT_SPK` | `0xc3` | `<idx> → <spk_bytes>` |
| `OP_TX_OUTPUT_AMOUNT` | `0xc2` | `<idx> → <amount_i64>` |
| `OP_TX_INPUT_COUNT` | `0xb3` | `→ <count>` |
| `OP_TX_OUTPUT_COUNT` | `0xb4` | `→ <count>` |
| `OP_TX_INPUT_AMOUNT` | `0xbe` | `<idx> → <amount_i64>` |

### SPK Bytes Format

`OP_TX_OUTPUT_SPK` pushes `version(2 bytes BE) + script_bytes`:
- P2PK: `0x0000` + `[0x20 <32-byte-pubkey> 0xac]` = 36 bytes

## Tools

### encode.py — Local Encrypt + Sign

```bash
# Encrypted (type=whisper)
python3 encode.py --to <recipient_address> --message "Secret" --key <privkey>

# Plaintext (type=message)
python3 encode.py --to <recipient_address> --message "Hello!" --key <privkey> --plain

# Auto-broadcast via API + save covenant_info
python3 encode.py --to <recipient_address> --message "Secret" --key <privkey> --broadcast
```

Outputs:
- Signed TX submitted to kaspad
- `covenant_info.json` saved locally (needed by decode.py)

### decode.py — Local Decrypt + Refund

```bash
# Auto-fetch covenant info (API → explorer → payload)
python3 decode.py --tx <tx_id> --key <privkey>

# Fully offline: pass TX payload directly
python3 decode.py --tx <tx_id> --key <privkey> --payload '{"v":1,"t":"whisper","d":"...","a":{...}}'

# Decrypt only, no refund
python3 decode.py --tx <tx_id> --key <privkey> --no-refund

# Custom covenant_info path
python3 decode.py --tx <tx_id> --key <privkey> --info /path/to/covenant_info.json
```

**Fallback chain**: `--payload` → `--info` → local file → API → block explorer

Automatically:
1. Loads covenant info from `covenant_info.json`
2. Decrypts (ECIES) or reads plaintext
3. Builds refund TX (0.2 KAS → sender)
4. Signs with recipient's private key
5. Submits refund TX to kaspad

## Web API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/whisper/contacts` | GET | Contact directory (public keys) |
| `/whisper/contacts/{id}` | GET | Single contact |
| `/whisper/inbox/{address}` | GET | Inbox |
| `/whisper/register` | POST | Self-registration |
| `/whisper/broadcast` | POST | Relay pre-signed TX |
| `/whisper/contacts/{id}/webhook` | PUT | Set webhook |

**API never touches private keys.**

## Economics

| Item | Amount |
|------|--------|
| Communication deposit | 0.2 KAS |
| Read receipt refund | 0.2 KAS |
| Net cost | ~0.0005 KAS (mining fee) |

**Anti-spam**: Unread = sender loses 0.2 KAS. Read = full refund.

## Test Results (TN12)

### v2 ECIES End-to-End: Nami → Bob (2026-02-28)

**Send** (encode.py — encrypted whisper):
- **TX**: `b1062cbd7db2dce21cf307290e77c791e8f9d9b64ee4536bf32c6bc97cc97509`
- Locked 0.2005 tKAS to P2SH covenant address
- ECIES encrypted with Bob's public key
- Signed locally with Nami's private key

**Refund** (decode.py — decrypt + auto-refund):
- **TX**: `1c6656c74c9d280da10dd313de6df2114c73bdde01c4f6df57a6c04731d778d2`
- Bob decrypted message locally
- Covenant enforced: refund 0.2 tKAS → Nami's address ✅
- Signed locally with Bob's private key

### Bob Offline Decrypt (2026-03-01) 🆕

Bob decrypted a whisper using `--payload` flag — **no API server, no covenant_info file needed!**

- **Refund TX**: `a265dd564d15608bda5bc8f1a040a0b0e0a044e3f874519d004cbc292b177feb`
- Used `decode.py --payload '<JSON from on-chain TX>'` to reconstruct covenant info from the `a` field
- Proves the protocol is fully self-contained on-chain ✅

### v0.1 Plaintext Test (2026-02-28)

- Send TX: `18e496038976ae8b0dcf8d68b8dc3c738b5febf68fe14b3c06af1ea1efa22942`
- Read TX: `04c83afa2f82ff42587e1ae06363716362c5cece69b653aa74e3c57bc7936b28`

## Architecture Notes

### Why P2SH?

The covenant logic lives in a **redeem script** wrapped in P2SH. When A sends, the UTXO is locked to the P2SH hash. When B spends, B provides the full redeem script + signature in the sig script. The script engine verifies:
1. Redeem script hash matches
2. Covenant conditions (output SPK, amount)
3. B's signature

### Security Properties

- **Trustless refund**: The covenant script is the law — B cannot spend without refunding A
- **Only B can spend**: `OP_CHECKSIG` ensures only B's signature is valid
- **Amount guaranteed**: `OP_GREATERTHANOREQUAL` ensures full refund
- **SPK pinned**: `OP_TX_OUTPUT_SPK` + `OP_EQUAL` ensures refund goes to A's exact address
- **End-to-end encryption**: ECIES with recipient's Kaspa public key — only they can decrypt
- **Local signing**: Private keys never leave the machine, even the API server can't see them

### Design Philosophy

Recipients **can** decrypt on their own — that's a cryptographic right. But we **encourage using decode.py**:

```
Self-decrypt: read ✅  refund ❌  ack ❌  → broken loop
Use decode.py: read ✅  refund ✅  ack ✅  → complete loop 🔄
```

Not by restriction, but by incentive.

## Requirements

- Kaspa TN12 node (`kaspad --testnet --netsuffix=12`)
- Python kaspa SDK (`pip install kaspa`)
- eciespy (`pip install eciespy`)
- wRPC endpoint: `ws://127.0.0.1:17210`

## Status

- [x] Covenant script design & generation
- [x] P2SH address derivation
- [x] JSON payload format `{v, t, d, a}`
- [x] ECIES encryption (secp256k1)
- [x] Local signing — encode.py
- [x] Local decryption + refund — decode.py
- [x] End-to-end test on TN12 (encrypted) ✅
- [x] End-to-end test on TN12 (plaintext) ✅
- [x] Web API design
- [ ] Web API implementation
- [ ] TG Bot integration
- [ ] Group messaging
- [ ] On-chain contact registry

## Future

- Inbox polling via Kaspa API (no live listener needed)
- Heartbeat integration for AI agents
- Multi-recipient messaging
- Expiry mechanism (sender reclaim if unread)

---

*Whisper Covenant v2 — 2026-02-28 by Nami 🌊 & Bob & Ryan*
*First verified: Nami ↔ Bob bidirectional ECIES encrypted messaging on Kaspa Testnet*
