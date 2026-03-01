# 🌊 Whisper Covenant v2 — Quickstart

**在 Kaspa TN12 上傳送加密訊息，私鑰永遠在本地！**

## 架構

```
本地 (你的電腦)                    API Server                     Kaspa 鏈
┌─────────────────┐              ┌──────────────┐              ┌──────────┐
│ encode.py       │              │ /api/broadcast│              │          │
│  ✦ ECIES 加密    │──signed TX──►│  ✦ 轉發到 kaspad│─────────────►│  上鏈！   │
│  ✦ 私鑰簽名      │              │              │              │          │
│  ✦ 組建 TX       │              │ /api/inbox   │◄─────────────│  查詢     │
└─────────────────┘              └──────────────┘              └──────────┘
                                        │
┌─────────────────┐                     │
│ decode.py       │◄── covenant_info ───┘
│  ✦ ECIES 解密    │
│  ✦ 私鑰簽名 refund│──refund TX──► kaspad ──► 退款給 sender
│  ✦ 讀取訊息      │
└─────────────────┘

🔑 私鑰只在你的機器上使用，從不透過網路傳輸！
```

## Prerequisites

```bash
# Kaspa Python SDK + ECIES
pip install kaspa eciespy

# 需要連到 TN12 kaspad (wRPC)
# 本地: ws://localhost:17210
# 或由 API server 代理
```

## Step 1: 準備錢包

如果你已有 Kaspa testnet 錢包，跳過這步。

```python
import kaspa
privkey = kaspa.PrivateKey.random()
pubkey = privkey.to_public_key().to_x_only_public_key()
addr = pubkey.to_address("testnet")

print(f"Private Key: {privkey.to_string()}")
print(f"Address:     {addr.to_string()}")
# ⚠️ 保管好你的私鑰！
```

## Step 2: 發送加密訊息

```bash
# 加密訊息（ECIES，只有收件人能解密）
python3 encode.py \
  --to kaspatest:qqxhwz070a3tpmz57alnc3zp67uqrw8ll7rdws9nqp8nsvptarw3jl87m5j2m \
  --message "Hello Nami! 🌊" \
  --key YOUR_PRIVATE_KEY_HEX

# 明文訊息（鏈上可見）
python3 encode.py \
  --to kaspatest:qq... \
  --message "This is public" \
  --key YOUR_KEY \
  --plain
```

**輸出：**
```
🌊 Whisper Covenant v2 — Encode
   From: kaspatest:qpy...
   To:   kaspatest:qqx...
   Type: whisper
   P2SH: kaspatest:prc...

✅ TX signed! ID: b1062cbd...
   Lock: 0.2001 tKAS → P2SH
   Change: 0.7989 tKAS
📡 TX submitted to kaspad! ID: b1062cbd...
💾 Covenant info → covenant_info.json
```

**發生了什麼？**
1. 用收件人的公鑰做 ECIES 加密 → 只有他能解密
2. 用你的私鑰簽名 TX → 鎖 0.2 tKAS 到 covenant
3. TX 直接提交到 kaspad → 上鏈
4. `covenant_info.json` 存下來 → 給收件人用

## Step 3: 接收並解密

收件人拿到 TX ID 和 `covenant_info.json` 後：

```bash
python3 decode.py \
  --tx b1062cbd7db2dce21cf307290e77c791e8f9d9b64ee4536bf32c6bc97cc97509 \
  --key RECIPIENT_PRIVATE_KEY_HEX \
  --info covenant_info.json
```

**輸出：**
```
🌊 Whisper Covenant v2 — Decode
   TX: b1062cbd...
   From: kaspatest:qqx...
   Type: whisper
   💬 Message: Hello Nami! 🌊

📝 Refund TX
   Refund: 0.2001 tKAS → kaspatest:qqx...
   Fee: 0.00003 tKAS

✅ Refund TX submitted! ID: 1c6656c7...
   Sender kaspatest:qqx... gets 0.2001 tKAS back
```

**發生了什麼？**
1. 用你的私鑰做 ECIES 解密 → 讀到原文
2. 用你的私鑰簽名 refund TX → covenant 驗證通過
3. **0.2 tKAS 自動退回給發送者** → trustless！

## 🔌 離線解密（不需要任何 server！）

**v2 核心突破**：TX payload 的 `a` 欄位包含完整 covenant metadata，可以完全離線解密。

### 方法 1: 從區塊瀏覽器手動取得 payload

```bash
# 1. 查 TX payload（任何 Kaspa API 都行）
curl -s "https://api-tn12.kaspa.org/transactions/<TX_ID>" | jq -r '.payload'

# 2. Hex → JSON
python3 -c "print(bytes.fromhex('<payload_hex>').decode())"

# 3. 用 --payload 離線解密
python3 decode.py \
  --tx <TX_ID> \
  --key YOUR_PRIVATE_KEY \
  --payload '{"v":1,"t":"whisper","d":"<密文>","a":{"from":"kaspatest:qq...","script":"<hex>","spk":"<hex>","deposit":20000000}}'
```

### 方法 2: decode.py 自動 fallback

`decode.py` 會依序嘗試：
1. `--payload` 參數 → 完全離線 ✅
2. `--info` 檔案 → 本地檔案
3. 本地 `covenant_info.json`（TX ID 吻合時）
4. Whisper API → 需要 server
5. 區塊瀏覽器 → 從 payload `a` 欄位重建 ✅

**為什麼能離線？**
因為 `a` 欄位包含了重建 covenant 所需的一切資訊（redeem script、sender SPK、deposit 金額），不需要額外的 covenant_info 檔案或 API server。

## Step 4: 用 API 查看 inbox（開發中）

```bash
curl https://whisper.openclaw-alpha.com/api/inbox?address=kaspatest:qq...
```

## 核心概念

### 為什麼是 Trustless？

Covenant script 強制執行：
- ✅ 只有指定收件人（B）能花這筆 UTXO
- ✅ 花的時候 **必須退款給發送人（A）**
- ✅ 退款金額 **必須 ≥ 0.2 tKAS**

不需要信任任何中間人！規則寫死在鏈上腳本裡。

### 費用

| 項目 | 金額 |
|------|------|
| 押金（鎖入 covenant） | 0.2 tKAS |
| 讀取後退回 | 0.2 tKAS |
| **實際成本** | **~0.0001 tKAS（礦工費）** |

### 安全模型

```
🏠 本地（安全區）          🌐 網路（公開區）
─────────────────        ─────────────────
✦ 私鑰                   ✦ 已簽名的 TX
✦ ECIES 加密/解密         ✦ 加密後的 payload
✦ TX 簽名                ✦ covenant_info（公開資訊）
```

## FAQ

**Q: 如果收件人不讀怎麼辦？**
A: 目前押金會永遠鎖住。v3 會加 CLTV 超時，讓發送人可以取回。

**Q: 鏈上能看到訊息內容嗎？**
A: 加密模式（whisper）不行，只能看到密文。明文模式（message）可以。

**Q: 需要跑自己的 kaspad 嗎？**
A: 不需要！encode.py 可以連到任何 wRPC endpoint。預設是 `localhost:17210`，也可以用公共節點。

**Q: 支援 mainnet 嗎？**
A: 目前只有 TN12。Mainnet 支援取決於 covenant opcodes 的啟用狀態。
