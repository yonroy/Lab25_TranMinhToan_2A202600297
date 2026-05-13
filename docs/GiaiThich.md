# Giải thích hệ thống — Dành cho học sinh cấp 1

---

## Hệ thống này là cái gì?

Hãy tưởng tượng bạn đang **hỏi một người trợ lý thông minh** (như hỏi ChatGPT). Nhưng đôi khi người trợ lý đó bị **mệt**, **bận**, hoặc **hỏng**. Hệ thống này giúp bạn **vẫn luôn nhận được câu trả lời**, dù có sự cố.

---

## 4 thành phần chính — như 4 người trong một đội

---

### 1. Provider (Người trả lời) — `providers.py`

```
🤖 Người trả lời
```

> **Provider** là người thực sự trả lời câu hỏi của bạn.

Giống như bạn hỏi thầy giáo. Nhưng đôi khi thầy **bận** (bị lỗi) hoặc trả lời **chậm**.

- Hệ thống có **2 thầy**: thầy chính (`primary`) và thầy phụ (`backup`)
- Thầy chính giỏi hơn nhưng đắt tiền hơn
- Nếu thầy chính bận → hỏi thầy phụ

```
Ví dụ thật trong code:
fail_rate = 0.3  → thầy này có 30% khả năng "bận không trả lời"
base_latency_ms = 200  → thầy này mất 200ms để suy nghĩ
```

---

### 2. Circuit Breaker (Cầu dao điện) — `circuit_breaker.py`

```
⚡ Cầu dao
```

> **Circuit Breaker** giống như **cái cầu dao điện** trong nhà bạn.

Khi điện bị chập → cầu dao **tự ngắt** để bảo vệ đồ điện. Khi sửa xong → cầu dao **bật lại**.

Cầu dao có **3 trạng thái**:

| Trạng thái | Màu đèn | Ý nghĩa |
|---|---|---|
| `CLOSED` (Đóng) | 🟢 Xanh | Bình thường, câu hỏi đi qua được |
| `OPEN` (Mở) | 🔴 Đỏ | Bị hỏng, chặn hết — không thử nữa |
| `HALF_OPEN` (Nửa mở) | 🟡 Vàng | Thử 1 lần xem đã sửa xong chưa |

```
Câu chuyện:
Thầy chính trả lời sai 3 lần liên tiếp
→ Cầu dao BẬT ĐỎ (OPEN) — không hỏi thầy chính nữa!
→ Chờ 2 giây
→ Cầu dao VÀNG (HALF_OPEN) — thử hỏi thầy chính 1 lần
→ Nếu OK: ĐÈN XANH (CLOSED) — bình thường trở lại
→ Nếu vẫn hỏng: ĐÈN ĐỎ lại — chờ tiếp
```

---

### 3. Cache (Sổ ghi nhớ) — `cache.py`

```
📒 Sổ ghi nhớ
```

> **Cache** giống như **cuốn sổ tay** — ghi lại câu hỏi đã hỏi và câu trả lời.

Lần sau nếu ai hỏi câu **tương tự** → đọc trong sổ luôn, **không cần hỏi thầy nữa** → nhanh hơn và tiết kiệm tiền hơn!

```
Ví dụ:
Lần 1: "Chính sách hoàn tiền là gì?" → hỏi thầy → ghi vào sổ
Lần 2: "Chính sách hoàn tiền như thế nào?" → tìm trong sổ → trả lời ngay!
```

Sổ ghi nhớ cũng có **quy tắc bảo mật**:
- ❌ Không ghi câu hỏi về mật khẩu, số tài khoản (thông tin riêng tư)
- ❌ Không dùng câu trả lời cũ nếu câu hỏi khác năm ("2024" ≠ "2026")
- ⏰ Câu trả lời cũ quá 5 phút → xóa đi, hỏi lại cho tươi

Có **2 loại sổ**:
- `ResponseCache` — sổ tay riêng trên máy tính của bạn
- `SharedRedisCache` — sổ tay **chung trên đám mây** (Redis), nhiều máy tính cùng dùng được

---

### 4. Gateway (Người điều phối) — `gateway.py`

```
🚦 Người điều phối
```

> **Gateway** là **người đứng ở cửa**, nhận câu hỏi của bạn và quyết định:

```
Bạn hỏi câu gì đó
        ↓
🚦 Gateway nhận câu hỏi
        ↓
📒 Tìm trong sổ ghi nhớ (Cache)?
   ✅ CÓ → trả lời ngay (nhanh!)
   ❌ KHÔNG → tiếp tục...
        ↓
⚡ Cầu dao thầy chính còn xanh?
   ✅ CÓ → hỏi thầy chính 🤖
   ❌ ĐỎ → hỏi thầy phụ 🤖
        ↓
   Nếu thầy phụ cũng hỏng...
        ↓
💬 Trả lời mặc định: "Hệ thống đang bảo trì, vui lòng thử lại sau"
```

---

## Tóm tắt bằng 1 câu chuyện

> Bạn gọi điện hỏi cửa hàng pizza về giờ mở cửa.
>
> - **Cache**: Nếu bạn đã hỏi hôm qua → nhân viên nhớ luôn, trả lời ngay
> - **Circuit Breaker**: Nếu điện thoại bị hỏng 3 lần → ngừng gọi số đó, chờ rồi thử lại
> - **Provider**: Gọi cửa hàng chính. Nếu bận → gọi cửa hàng dự phòng
> - **Gateway**: Người trực tổng đài — quyết định gọi ai, theo thứ tự nào

---

## Sơ đồ toàn hệ thống

```
Bạn (User)
    │
    ▼
🚦 Gateway (Người điều phối)
    │
    ├──► 📒 Cache (Sổ ghi nhớ) ──► Trả lời ngay nếu có
    │
    ├──► ⚡ Cầu dao thầy chính ──► 🤖 Thầy chính (Provider 1)
    │
    ├──► ⚡ Cầu dao thầy phụ  ──► 🤖 Thầy phụ (Provider 2)
    │
    └──► 💬 "Hệ thống đang bận" (Static Fallback)
```
