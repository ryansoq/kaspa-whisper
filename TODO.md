# Whisper Covenant — TODO

## Bob Code Review 建議（2026-03-01）

- [ ] 📦 `requirements.txt` — 鎖定依賴版本
- [ ] 🧪 pytest 單元測試
- [ ] 🔒 私鑰檔案權限檢查（chmod 600 警告）
- [ ] 📏 訊息長度限制（防過大 payload）
- [ ] 🔄 Retry 機制（網路不穩定時重試）
- [ ] 📊 Logging 系統（取代 print()）
- [ ] 🛡️ 輸入驗證（地址格式檢查）
- [ ] 💰 動態手續費估算

## 功能規劃

- [ ] TG Bot 整合
- [ ] 群組訊息
- [ ] 鏈上聯絡人註冊
- [ ] 自動回收排程（whisper_monitor.py）
