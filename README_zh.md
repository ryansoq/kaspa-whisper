# 🌊 Whisper Covenant v3

**Kaspa 鏈上無信任加密通訊協議（TN12 測試網）**

私鑰**永遠不會**離開你的電腦。本地簽名，鏈上傳輸。

## 概念

A 發一則加密訊息給 B，同時把 0.2 KAS 押金鎖進 covenant 腳本。B 讀取訊息時（花費 UTXO），covenant **強制**退還 0.2 KAS 給 A。如果 B 一直不讀，A 可以在超時後自己取回押金（CLTV）。

```
A（發送者）                    Covenant UTXO                    B（接收者）
    │                               │                               │
    │  encode.py（本地）            │                               │
    │  ├─ ECIES 加密(B_pub)         │                               │
    │  ├─ 簽名 TX(A_priv)          │                               │
    │  └─ 提交到 kaspad / REST API  │                               │
    ├──── 鎖定 0.2 KAS ───────────►│ P2SH 腳本                     │
    │     + JSON payload            │ 強制退款給 A                  │
    │                               │                               │
    │                               │         decode.py（本地）     │
    │                               │  ├─ ECIES 解密(B_priv)     ◄──┤
    │                               │  ├─ 簽名退款 TX(B_priv)      │
    │◄──── 0.2 KAS 退款 ──────────│  └─ 提交到 kaspad / REST API   │
    │     （腳本強制執行）          │                               │
    │                               │                               │
    │  ⏰ 超時？                     │                               │
    │  covenant_reclaim.py（本地）   │                               │
    │  ├─ CLTV 驗證超時             │                               │
    │  ├─ 簽名取回 TX(A_priv)       │                               │
    │◄──── 0.2 KAS 取回 ──────────│  B 沒讀 → A 自己拿回           │
```

## v3 新功能：CLTV 超時取回 🆕

v2 的問題：如果 B 永遠不讀，A 的 0.2 KAS 押金就永遠鎖死。

v3 用 Kaspa 的 `OP_CHECKLOCKTIMEVERIFY` 解決：

```
IF
  <B_pubkey> OP_CHECKSIG              // B 可以讀取（證明已讀）
ELSE
  <timeout_daa> OP_CHECKLOCKTIMEVERIFY // 超時後...
  <A_pubkey> OP_CHECKSIG              // A 可以取回押金
ENDIF
```

兩條路徑，一個 UTXO：
- **超時前**：只有 B 能花費（讀取回條）
- **超時後**：A 可以取回（訊息過期）

> ⚠️ **Kaspa 特殊行為**：`OP_CHECKLOCKTIMEVERIFY` (0xb0) 會**彈出**堆疊頂部的值！這跟 Bitcoin 不同（Bitcoin 會留在堆疊上需要 OP_DROP）。

## 安全模型

| 區域 | 工具 | 發生什麼 |
|------|------|----------|
| 🏠 本地 | `encode.py`, `decode.py`, `covenant_reclaim.py` | 加密、簽名、解密 — 全部用你的私鑰 |
| 🌐 API | 聯絡人、收件匣、廣播 | 公開資料查詢和已簽名 TX 中繼 |

API 伺服器**永遠**看不到私鑰。即使被入侵，攻擊者只能看到加密密文和公鑰。

## 訊息格式

交易 payload 中的 JSON：`{v, t, d, a}`

| 欄位 | 說明 |
|------|------|
| `v` | 版本，目前為 `3` |
| `t` | 類型：`whisper`（加密）/ `message`（明文）/ `ack`（已讀回條）|
| `d` | 資料：ECIES 密文 hex / 明文字串 / 原始 TX ID |
| `a` | 屬性：covenant 元資料（見下方）|

### `a` 欄位 — 鏈上自給自足 🆕

對 `whisper`/`message` 類型，`a` 包含**完整 covenant 元資料**：

```json
{
  "from": "kaspatest:qq...",     // 發送者地址
  "script": "0a0b0c...",         // covenant 贖回腳本 (hex)
  "spk": "20abcd...ac",          // 發送者的 ScriptPublicKey
  "deposit": 20000000,            // 押金（sompi）
  "timeout_daa": 17316681         // CLTV 超時 DAA
}
```

這代表**任何擁有接收者私鑰的人都能完全重建 covenant 並離線解密** — 不需要伺服器、不需要交換檔案。區塊鏈就是資料庫。

## 加密

- **演算法**：ECIES (secp256k1)
- **公鑰**：33 bytes 壓縮格式（`02` 前綴 + Kaspa 地址的 x-only 公鑰）
- **套件**：Python `eciespy`
- 跟 Kaspa 錢包用同一對金鑰 — 不需要額外的密鑰

## 工具

### encode.py — 本地加密 + 簽名 + 發送

```bash
# 加密發送（type=whisper）
python3 encode.py --to <收件者地址> --message "秘密" --key <私鑰>

# 明文發送（type=message）
python3 encode.py --to <收件者地址> --message "你好！" --key <私鑰> --plain

# 不需要本地 kaspad 節點（用公開 REST API）
python3 encode.py --to <收件者地址> --message "秘密" --key <私鑰> --remote

# 設定 CLTV 超時偏移（預設 1000 DAA ≈ 100 秒）
python3 encode.py --to <收件者地址> --message "秘密" --key <私鑰> --remote --timeout-offset 5000
```

### decode.py — 本地解密 + 退款

```bash
# 自動取得 covenant info（API → explorer → payload）
python3 decode.py --tx <tx_id> --key <私鑰>

# 不需要本地 kaspad 節點
python3 decode.py --tx <tx_id> --key <私鑰> --remote

# 完全離線：直接傳入 TX payload
python3 decode.py --tx <tx_id> --key <私鑰> --payload '{"v":3,"t":"whisper","d":"...","a":{...}}'

# 只解密，不退款
python3 decode.py --tx <tx_id> --key <私鑰> --no-refund
```

**Fallback 鏈**：`--payload` → `--info` → 本地檔案 → API → 區塊瀏覽器

### covenant_reclaim.py — 超時取回押金

```bash
# 從本地 covenant_info.json 取回（需要 kaspad）
python3 covenant_reclaim.py

# 不需要本地 kaspad 節點
python3 covenant_reclaim.py --remote

# 指定 covenant info 路徑
python3 covenant_reclaim.py --remote --info /path/to/covenant_info.json
```

## 經濟模型

| 項目 | 金額 |
|------|------|
| 通訊押金 | 0.2 KAS |
| 讀取退款 | 0.2 KAS |
| 淨成本 | ~0.0005 KAS（礦工手續費）|
| 超時取回 | 0.2 KAS（扣手續費）|

**防垃圾郵件**：不讀 = 發送者損失 0.2 KAS。讀了 = 全額退款。

## Covenant 腳本（v3 CLTV 版）

```
// P2SH 內的贖回腳本：

IF
  // ── 路徑 1：B 讀取 ──
  <A_spk_bytes>          // A 的 ScriptPublicKey（版本 + 腳本）
  OP_FALSE               // 輸出索引 0
  OP_TX_OUTPUT_SPK       // 內省：花費 TX 的 output[0] SPK
  OP_EQUAL
  OP_VERIFY              // ✓ output[0] 付給 A

  <deposit_sompi>        // 0.2 KAS = 20,000,000 sompi
  OP_FALSE               // 輸出索引 0
  OP_TX_OUTPUT_AMOUNT    // 內省：花費 TX 的 output[0] 金額
  OP_GREATERTHANOREQUAL
  OP_VERIFY              // ✓ output[0] ≥ 0.2 KAS

  <B_pubkey>             // 32-byte Schnorr 公鑰
  OP_CHECKSIG            // ✓ 只有 B 能花費

ELSE
  // ── 路徑 2：A 超時取回 ──
  <timeout_daa>                    // 超時 DAA 分數
  OP_CHECKLOCKTIMEVERIFY           // ✓ 必須超過超時時間（注意：會 pop stack！）
  <A_pubkey>                       // 32-byte Schnorr 公鑰
  OP_CHECKSIG                      // ✓ 只有 A 能取回

ENDIF
```

### Opcode 參考（TN12）

| Opcode | Hex | 堆疊效果 |
|--------|-----|----------|
| `OP_TX_OUTPUT_SPK` | `0xc3` | `<idx> → <spk_bytes>` |
| `OP_TX_OUTPUT_AMOUNT` | `0xc2` | `<idx> → <amount_i64>` |
| `OP_CHECKLOCKTIMEVERIFY` | `0xb0` | `<daa> →`（Kaspa 會 pop！）|
| `OP_IF` | `0x63` | 條件分支 |
| `OP_ELSE` | `0x67` | 替代分支 |
| `OP_ENDIF` | `0x68` | 結束分支 |

## 測試結果（TN12）

### v3 CLTV 完整測試（2026-03-01）✅

**第一輪：B 讀取 + 退款**
- 發送 TX：`c610ffee8c687068a43a67db2cdc52a72fce84ba118a76cf44008ac9953a8a71`
- Bob 解密成功，退款 TX：`ca152ce442c1b532fbd548952c3ff0da3fc83c58dd67cd024b76679a14c6aeb0`
- API 403 時自動從 block explorer 重建 covenant info ✅

**第二輪：B 不讀 → A 超時取回**
- 發送 TX：`f5830e7b48a10eb32be542bd3269c42e7165ba29471ba74db548e19f1bdccee7`
- Timeout DAA 17316681 過期後，Nami 成功取回押金
- 取回 TX：`bc8a5cf7ff2b41dfe9f1700036912e9f28b9c195a314e39606938a47fbf618d3`

### v2 ECIES 端對端測試（2026-02-28）

- 發送 TX：`b1062cbd7db2dce21cf307290e77c791e8f9d9b64ee4536bf32c6bc97cc97509`
- 退款 TX：`1c6656c74c9d280da10dd313de6df2114c73bdde01c4f6df57a6c04731d778d2`
- Bob 離線解密 TX：`a265dd564d15608bda5bc8f1a040a0b0e0a044e3f874519d004cbc292b177feb`

## 需求

```bash
pip install kaspa eciespy
```

- **有 kaspad 節點**：連接 `ws://127.0.0.1:17210`（wRPC borsh）
- **沒有節點**：使用 `--remote` 模式（公開 REST API：`api-tn12.kaspa.org`）

## 狀態

- [x] Covenant 腳本設計與生成
- [x] CLTV 超時取回
- [x] ECIES 加密（secp256k1）
- [x] 本地簽名 — encode.py
- [x] 本地解密 + 退款 — decode.py
- [x] 超時取回 — covenant_reclaim.py
- [x] `--remote` 模式（不需要本地節點）
- [x] TN12 端對端測試（加密 + CLTV）✅
- [x] 鏈上自給自足（`a` 欄位包含所有元資料）
- [ ] TG Bot 整合
- [ ] 群組訊息
- [ ] 鏈上聯絡人註冊

## 設計哲學

接收者**可以**自己解密 — 那是密碼學賦予的權利。但我們**鼓勵使用 decode.py**：

```
自行解密：讀取 ✅  退款 ❌  已讀回條 ❌  → 迴圈中斷
用 decode.py：讀取 ✅  退款 ✅  已讀回條 ✅  → 完整迴圈 🔄
```

不是靠限制，是靠激勵。

## 未來

- CLTV 超時自動回收（排程監控）
- AI Agent 心跳整合
- 多收件人訊息
- 鏈上 indexer

---

*Whisper Covenant v3 — 2026-03-01 by Nami 🌊 & Bob 🔧 & Ryan*
*Kaspa TN12 上首個完整驗證的 ECIES + CLTV 加密通訊協議*
