# 🌊 Whisper Covenant — 這是什麼？

## 一句話

**在 Kaspa 區塊鏈上發加密私訊，不需要任何中間人。**

---

## 為什麼需要這個？

想像你要傳一則只有對方能看的訊息，但：
- ❌ 不想靠 Telegram / Signal 等中心化服務
- ❌ 不想信任任何第三方伺服器
- ✅ 要完全 **trustless**（不需要信任任何人）
- ✅ 私鑰永遠不離開你的電腦

Whisper 做到了：**用 Kaspa 的交易 payload 傳加密訊息，用 covenant script 保證押金安全。**

---

## 怎麼運作？（簡單版）

```
Alice 想傳密語給 Bob

  1️⃣ Alice 加密訊息（只有 Bob 的私鑰能解）
  2️⃣ Alice 鎖 0.2 KAS 到一個特殊合約（covenant）
  3️⃣ 合約上鏈，Bob 看到了

  然後有兩種結局：

  ✅ Bob 讀了 → 解密訊息 + 退回 0.2 KAS 給 Alice（合約強制的！）
  ⏰ Bob 沒讀 → 超時後 Alice 自己拿回 0.2 KAS（CLTV 保護）
```

**結果：不管 Bob 配不配合，Alice 的錢都不會卡住。**

---

## Covenant 合約是什麼？

一個鎖在區塊鏈上的 **智能腳本**，寫了兩條規則：

### 路徑一：IF（Bob 讀取）
- Bob 用私鑰簽名花掉這筆 UTXO
- 合約**強制** output[0] 必須付 ≥ 0.2 KAS 回 Alice
- Bob 不能把錢花到別的地方 → **trustless 退款**

### 路徑二：ELSE（Alice 超時取回）
- 超過設定的 DAA 時間後
- Alice 用自己的私鑰簽名取回 0.2 KAS
- 不需要 Bob 配合

```
OP_IF
  // Bob 讀取：驗證退款地址 + 金額 + Bob 簽名
  <Alice_SPK> 0 OP_TX_OUTPUT_SPK OP_EQUAL OP_VERIFY
  0 OP_TX_OUTPUT_AMOUNT <deposit> OP_GTE OP_VERIFY
  <Bob_pubkey> OP_CHECKSIG
OP_ELSE
  // Alice 取回：驗證超時 + Alice 簽名
  <timeout_daa> OP_CHECKLOCKTIMEVERIFY
  <Alice_pubkey> OP_CHECKSIG
OP_ENDIF
```

---

## 加密怎麼做？

- **ECIES 加密**（secp256k1，跟 Kaspa 錢包同一把 key）
- Alice 用 Bob 的**公鑰**加密 → 只有 Bob 的**私鑰**能解
- 密文放在 TX payload 裡上鏈
- 私鑰永遠不離開各自的電腦

---

## 費用

| 項目 | 金額 | 說明 |
|------|------|------|
| 押金（鎖定） | 0.2 KAS | 鎖在合約裡 |
| 退款（讀取後） | 0.2 KAS | 自動退回 sender |
| **實際成本** | **~0.0001 KAS** | 只有礦工手續費 |

**反垃圾機制**：不讀 = sender 的 0.2 被鎖住（直到超時），所以 spam 成本很高。

---

## 有哪些工具？

| 檔案 | 用途 |
|------|------|
| `encode.py` | 加密訊息 + 建立 covenant TX + 上鏈 |
| `decode.py` | 解密訊息 + 簽退款 TX |
| `covenant_send.py` | 低層：建立 CLTV covenant TX |
| `covenant_read.py` | 低層：Bob 走 IF 分支退款 |
| `covenant_reclaim.py` | 低層：Alice 走 ELSE 分支取回押金 |

---

## 常見問題

### Q: Bob 不退款怎麼辦？
**A:** 超時後你跑 `covenant_reclaim.py` 就能自己拿回 0.2 KAS。不需要 Bob 配合。

### Q: 時間到會自動退款嗎？
**A:** 不會。區塊鏈是被動的（UTXO 模型），要有人發一筆 TX 才能花。但可以用背景腳本自動掃描 + 自動 reclaim，效果一樣。

### Q: 別人能幫我 reclaim 嗎？
**A:** 不能。ELSE 分支要求 sender 的私鑰簽名，只有你自己能拿回你的錢。🔐

### Q: 需要信任 API server 嗎？
**A:** 不需要！所有 covenant 資訊都在 TX payload 的 `a` 欄位裡（鏈上自給自足）。API 只是方便查詢的快取。

### Q: 跟 Bitcoin 的 CLTV 有什麼不同？
**A:** Kaspa 的 `OP_CHECKLOCKTIMEVERIFY` 會 **pop stack**（Bitcoin 不會），所以不需要 `OP_DROP`。這是重要差異！

---

## 技術規格

- **網路**: Kaspa Testnet 12 (TN12)
- **加密**: ECIES (secp256k1)
- **合約**: P2SH + covenant introspection opcodes
- **Opcodes**: `OP_TX_OUTPUT_SPK` (0xc3), `OP_TX_OUTPUT_AMOUNT` (0xc2), `OP_CHECKLOCKTIMEVERIFY` (0xb0)
- **押金**: 0.2 KAS (20,000,000 sompi)
- **超時**: 可自訂 DAA score（測試用 +1000 ≈ 100秒）
- **Payload**: JSON `{v, t, d, a}` 格式

---

## 版本歷史

| 版本 | 日期 | 里程碑 |
|------|------|--------|
| v1 | 2026-02-28 | 基本 covenant + 明文訊息 |
| v2 | 2026-03-01 | ECIES 加密 + 本地簽名 + 離線解密 |
| v3 | 2026-03-01 | CLTV 超時退款（IF/ELSE 雙路徑）|

---

## 教學網站

🌐 **https://whisper.openclaw-alpha.com**

完整文件 + 互動範例 + 測試結果

---

*Built by Nami 🌊 & Ryan on Kaspa TN12*
*Trustless by design. Private by default.*
