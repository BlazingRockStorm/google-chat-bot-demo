# Google Chat Drive RAG Bot (Gemini 2.5 Flash)

## 1) Tổng quan

Dự án xây dựng một **chatbot trên Google Chat** để hỏi đáp về **ISMS trong công ty** trong bối cảnh demo.
Nguồn tri thức được lấy từ **Google Drive folder** chứa các tài liệu/quy định **ISMS dạng PDF**.

Kiến trúc sử dụng mô hình **RAG (Retrieval-Augmented Generation)**:

- **Indexing phase (`reindex`)**: đọc file từ Drive, trích xuất nội dung, chia chunk, tạo embedding, lưu vào Firestore.
- **Question answering phase**: embedding câu hỏi, truy hồi Top-K chunk liên quan từ Firestore, gửi context vào Gemini để sinh câu trả lời.

---

## 2) Kiến trúc hệ thống

```text
[User in Google Chat]
        |
        v
[Google Chat App (Workspace Add-ons)]
        |
        v
[Cloud Run service: chatbot (Python)]
   |---------------------------|
   |                           |
   | A) reindex                | B) question answering
   | - Read Drive files        | - Embed query
   | - Extract text            | - Retrieve Top-K chunks
   | - Chunking                | - Prompt Gemini 2.5 Flash
   | - Embedding               | - Return answer + sources
   | - Save to Firestore       |
   |---------------------------|

External services:
- Google Drive API (nguồn dữ liệu)
- Vertex AI (Gemini + Embeddings)
- Firestore (index/vector cache)
- Cloud Logging (log/monitoring)
```

---

## 3) Công nghệ sử dụng

- **Runtime**: Python + Functions Framework
- **Hosting**: Cloud Run
- **Chat channel**: Google Chat API (Workspace Add-ons mode)
- **LLM**: `gemini-2.5-flash` (Vertex AI)
- **Embedding model**: `text-embedding-004`
- **Vector/index store**: Cloud Firestore (named DB: `chatapp`)
- **Document parsing**:
  - Google Docs (export text)
  - TXT
  - PDF (`pypdf`)
  - DOCX (`python-docx`)

---

## 4) Chức năng chính

1. **Nhận message từ Google Chat**
2. **Lệnh `reindex`**
   - quét folder Drive theo kiểu đệ quy (recursive)
   - đọc và trích xuất nội dung file
   - chunking + embedding
   - lưu vào Firestore collection `drive_rag_index_v2`
3. **Q&A theo tài liệu ISMS nội bộ**
   - truy hồi ngữ nghĩa Top-K
   - trả lời từ context
   - đính kèm danh sách nguồn (sources)
4. **Lệnh hỗ trợ**
   - `ping` → kiểm tra bot sống

---

## 5) Biến môi trường (Environment Variables)

| Variable | Mô tả |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | Project ID |
| `GOOGLE_CLOUD_LOCATION` | Region Vertex AI |
| `DRIVE_FOLDER_ID` | Folder ID nguồn tri thức |
| `GEMINI_MODEL` | Model trả lời |
| `GEMINI_EMBED_MODEL` | Model embedding |
| `FIRESTORE_DATABASE_ID` | Firestore database name |
| `MAX_FILES` | Số file tối đa khi index |
| `CHUNK_SIZE` | Kích thước chunk |
| `CHUNK_OVERLAP` | Overlap giữa các chunk |
| `TOP_K` | Số chunk truy hồi |

---

## 6) Setup trên Google Cloud Console

### 6.1 Bật API cần thiết
- Google Chat API
- Vertex AI API
- Google Drive API
- Cloud Firestore API

### 6.2 Firestore
- Tạo Firestore database (Native mode) với tên database phù hợp (ví dụ: `chatapp`)

### 6.3 IAM cho service account chạy Cloud Run
Cấp quyền:
- `Vertex AI User`
- `Cloud Datastore User`

### 6.4 Quyền đọc Google Drive
- Share folder Drive nguồn cho **service account của Cloud Run** với quyền Viewer.

> Lưu ý: Service account của Chat Add-ons và service account runtime Cloud Run có thể khác nhau.

### 6.5 Cloud Run timeout
- Tăng request timeout (ví dụ 900s) để tránh timeout khi `reindex` dữ liệu lớn.

---

## 7) Cài dependencies

```bash
pip install -r requirements.txt
```

Ví dụ `requirements.txt`:

```text
functions-framework==3.9.1
google-genai>=1.12.1
google-api-python-client>=2.164.0
google-auth>=2.38.0
google-cloud-firestore>=2.16.0
pypdf>=5.3.0
python-docx>=1.1.2
```

---

## 8) Deploy (tham khảo)

Deploy Cloud Run/Functions Gen2 theo cấu hình hiện tại của dự án, bảo đảm:
- entrypoint đúng (`chat_webhook`)
- env vars đầy đủ
- service account đúng quyền

---

## 9) Cách reindex không cần vào Google Chat

Dùng **Google Cloud Shell**:

```bash
curl -s -X POST "https://chatbot-62920486971.asia-northeast1.run.app/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -d '{
    "chat": {
      "messagePayload": {
        "message": {
          "text": "reindex",
          "argumentText": "reindex"
        }
      }
    }
  }'
```

Kết quả kỳ vọng:
- `Reindex completed ✅`
- số lượng `Files` và `Chunks` > 0

---

## 10) Luồng xử lý nghiệp vụ

### 10.1 Reindex flow
1. Nhận command `reindex`
2. Liệt kê file từ Drive folder (recursive)
3. Trích xuất text theo định dạng file
4. Chia chunk + tạo embedding
5. Ghi chunk + vector + metadata vào Firestore

### 10.2 Q&A flow
1. Nhận câu hỏi user
2. Embedding câu hỏi
3. Tính similarity với các vector đã index
4. Lấy Top-K chunk
5. Gửi context + question vào Gemini 2.5 Flash
6. Trả lời + nguồn tham chiếu

---

## 11) Troubleshooting nhanh

### Lỗi `Index is empty. Run reindex first.`
- Chưa chạy reindex hoặc reindex ra 0 file/chunk.

### Lỗi `Drive API ... is disabled`
- Bật Google Drive API trong đúng project.

### Lỗi `(default) database does not exist`
- Đang trỏ nhầm Firestore DB `(default)`; kiểm tra `FIRESTORE_DATABASE_ID` và code khởi tạo Firestore client.

### Reindex chạy nhưng `Files: 0`
- Folder Drive chưa share cho runtime service account của Cloud Run.

---

## 12) Định hướng cải thiện trong tương lai

- Nâng chất lượng format câu trả lời để dễ đọc hơn trên Google Chat.
- Tối ưu hiệu năng truy hồi và thời gian phản hồi.
- Bổ sung hoàn thiện IAM/security khi chuyển từ phạm vi demo sang vận hành thực tế.
- Siết phạm vi dữ liệu để chatbot chỉ sử dụng nguồn tri thức từ Google Drive theo chính sách.
- Refactor mã nguồn theo module để giảm độ phức tạp và dễ bảo trì.

> Ghi chú: Đây là định hướng tổng quan cho giai đoạn tiếp theo; tài liệu demo không đi vào kế hoạch triển khai chi tiết.

---

## 13) Đánh giá hiện trạng demo

### 13.1 Phần đã hoàn thiện
- Đã thực hiện được các tính năng cơ bản của một chatbot trên Google Chat.
- Đúng mục tiêu demo: hỏi đáp về tài liệu **ISMS** trong công ty.

### 13.2 Phần chưa đạt
- Format câu trả lời chưa đẹp, chưa tối ưu trải nghiệm đọc.
- Dự án làm nhanh để demo và chưa có nhiều kinh nghiệm Google Cloud nên đang tạm bỏ qua một số phần IAM và security.
- Tốc độ phản hồi còn chậm.
- Chưa khóa chặt phạm vi dữ liệu để chatbot chỉ sử dụng nguồn từ Google Drive theo chính sách mong muốn.
- Cấu trúc mã nguồn còn mang tính "spaghetti code", cần refactor để dễ bảo trì/mở rộng.

---

## 14) Kết luận

Giải pháp đã triển khai được một chatbot nội bộ trên Google Chat dùng RAG với Google Drive làm nguồn tri thức ISMS, đáp ứng được mục tiêu demo hỏi đáp cơ bản.  
Ở trạng thái hiện tại, hệ thống cần tiếp tục cải thiện về định dạng câu trả lời, hiệu năng, bảo mật/IAM và chất lượng mã nguồn trước khi đưa vào vận hành production.