import io
import os
import json
import hashlib
from typing import List, Dict, Any, Tuple

import google.auth
from google import genai
from google.genai.types import HttpOptions
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.cloud import firestore
from pypdf import PdfReader
from docx import Document

# -----------------------------
# ENV
# -----------------------------
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
MODEL = os.environ.get("GEMINI_MODEL")
EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL")
MAX_FILES = int(os.environ.get("MAX_FILES", "200"))
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "150"))
TOP_K = int(os.environ.get("TOP_K", "6"))
FIRESTORE_DATABASE_ID = os.environ.get("FIRESTORE_DATABASE_ID")
INDEX_COL = "drive_rag_index_v2"

# -----------------------------
# Clients (ONE of each, no duplicates)
# -----------------------------
genai_client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
    http_options=HttpOptions(api_version="v1")
)

db = firestore.Client(
    project=PROJECT_ID,
    database=FIRESTORE_DATABASE_ID
)

# -----------------------------
# Response helpers
# -----------------------------
def _json_response(body: dict, code: int = 200):
    return (json.dumps(body), code, {"Content-Type": "application/json; charset=utf-8"})

def _chat_message(text: str):
    return _json_response({
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"text": text}
                }
            }
        }
    })

# -----------------------------
# Event parsing
# -----------------------------
def extract_user_text(event: dict) -> str:
    msg = (((event.get("chat") or {}).get("messagePayload") or {}).get("message") or {})
    text = (msg.get("argumentText") or msg.get("text") or "").strip()
    if not text:
        legacy = event.get("message", {}) or {}
        text = (legacy.get("argumentText") or legacy.get("text") or "").strip()
    return text

# -----------------------------
# Drive helpers
# -----------------------------
def get_drive_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def list_all_files_recursive(drive, root_folder_id: str, max_files: int = 200) -> List[Dict[str, Any]]:
    result = []
    queue = [root_folder_id]

    while queue and len(result) < max_files:
        folder_id = queue.pop(0)
        page_token = None

        while True:
            q = f"'{folder_id}' in parents and trashed=false"
            resp = drive.files().list(
                q=q,
                fields="nextPageToken, files(id,name,mimeType,modifiedTime,parents)",
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True
            ).execute()

            files = resp.get("files", [])
            for f in files:
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    queue.append(f["id"])
                else:
                    result.append(f)
                    if len(result) >= max_files:
                        break

            if len(result) >= max_files:
                break

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    return result

def download_file_bytes(drive, file_id: str) -> bytes:
    req = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()

def export_google_doc_text(drive, file_id: str) -> str:
    req = drive.files().export_media(fileId=file_id, mimeType="text/plain")
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue().decode("utf-8", errors="ignore")

def extract_text_from_file(drive, f: Dict[str, Any]) -> str:
    mime = f.get("mimeType", "")
    file_id = f["id"]

    if mime == "application/vnd.google-apps.document":
        return export_google_doc_text(drive, file_id)

    if mime.startswith("text/"):
        return download_file_bytes(drive, file_id).decode("utf-8", errors="ignore")

    if mime == "application/pdf":
        raw = download_file_bytes(drive, file_id)
        reader = PdfReader(io.BytesIO(raw))
        pages = [p.extract_text() or "" for p in reader.pages]
        return "\n".join(pages).strip()

    if mime in [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword"
    ]:
        raw = download_file_bytes(drive, file_id)
        doc = Document(io.BytesIO(raw))
        return "\n".join([p.text for p in doc.paragraphs]).strip()

    return ""

# -----------------------------
# Chunking + embeddings
# -----------------------------
def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> List[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = max(0, end - overlap)
    return chunks

def embed_texts(texts: List[str]) -> List[List[float]]:
    vectors = []
    batch_size = 20
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        resp = genai_client.models.embed_content(
            model=EMBED_MODEL,
            contents=batch
        )
        for e in resp.embeddings:
            vectors.append(e.values)
    return vectors

def cosine_sim(a: List[float], b: List[float]) -> float:
    import math
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)) + 1e-12
    nb = math.sqrt(sum(y*y for y in b)) + 1e-12
    return dot / (na * nb)

# -----------------------------
# Firestore index
# -----------------------------
def make_doc_id(file_id: str, chunk_idx: int, modified_time: str) -> str:
    s = f"{file_id}:{chunk_idx}:{modified_time}"
    return hashlib.sha256(s.encode()).hexdigest()[:40]

def upsert_index(folder_id: str) -> Tuple[int, int]:
    drive = get_drive_service()
    files = list_all_files_recursive(drive, folder_id, max_files=MAX_FILES)

    files_processed = 0
    chunks_written = 0

    for f in files:
        try:
            text = extract_text_from_file(drive, f)
        except Exception:
            continue

        if not text or len(text.strip()) < 20:
            continue

        chunks = chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
        if not chunks:
            continue

        vectors = embed_texts(chunks)

        batch = db.batch()
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            doc_id = make_doc_id(f["id"], idx, f.get("modifiedTime", ""))
            ref = db.collection(INDEX_COL).document(doc_id)
            batch.set(ref, {
                "folderId": folder_id,
                "fileId": f["id"],
                "fileName": f.get("name", ""),
                "mimeType": f.get("mimeType", ""),
                "modifiedTime": f.get("modifiedTime", ""),
                "chunkIndex": idx,
                "text": chunk,
                "embedding": vec,
            })
            chunks_written += 1

        batch.commit()
        files_processed += 1

    return files_processed, chunks_written

def load_index(folder_id: str, limit: int = 5000) -> List[Dict[str, Any]]:
    docs = db.collection(INDEX_COL).where("folderId", "==", folder_id).limit(limit).stream()
    return [d.to_dict() for d in docs]

# -----------------------------
# Retrieval + answer
# -----------------------------
def retrieve_top_k(question: str, index_rows: List[Dict[str, Any]], top_k: int = 6):
    q_vec = embed_texts([question])[0]
    scored = []
    for row in index_rows:
        sim = cosine_sim(q_vec, row.get("embedding", []))
        scored.append((sim, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]

def answer_with_rag(question: str, top_rows: List[Tuple[float, Dict[str, Any]]]) -> str:
    context_blocks = []
    sources = []
    for score, row in top_rows:
        context_blocks.append(
            f"[Source: {row.get('fileName')} | score={score:.3f}]\n{row.get('text','')}"
        )
        sources.append(row.get("fileName", "unknown"))

    context = "\n\n".join(context_blocks)
    prompt = f"""
You are a helpful assistant.
Answer ONLY using the context below.
If not found in context, say exactly:
"I can't find that information in the Drive folder."

Context:
{context}

Question:
{question}
"""

    resp = genai_client.models.generate_content(
        model=MODEL,
        contents=prompt
    )
    text = (resp.text or "").strip() or "I can't find that information in the Drive folder."

    uniq_sources = []
    for s in sources:
        if s not in uniq_sources:
            uniq_sources.append(s)

    src_text = "\n".join([f"- {s}" for s in uniq_sources[:10]]) if uniq_sources else "- none"
    final = f"{text}\n\nSources:\n{src_text}"
    return final[:3900]

# -----------------------------
# Entry point
# -----------------------------
def chat_webhook(request):
    event = request.get_json(silent=True) or {}
    user_text = extract_user_text(event)

    if not DRIVE_FOLDER_ID:
        return _chat_message("Missing env var DRIVE_FOLDER_ID.")

    if not user_text:
        return _chat_message(
            "Ask me a question from Drive knowledge.\n"
            "Commands:\n"
            "- /reindex (or reindex)\n"
            "- any question"
        )

    cmd = user_text.strip().lower()

    try:
        if cmd in ("/reindex", "reindex"):
            files_processed, chunks_written = upsert_index(DRIVE_FOLDER_ID)
            return _chat_message(f"Reindex completed ✅\nFiles: {files_processed}\nChunks: {chunks_written}")

        if cmd == "ping":
            return _chat_message("pong ✅")

        index_rows = load_index(DRIVE_FOLDER_ID)
        if not index_rows:
            return _chat_message("Index is empty. Run `reindex` first.")

        top_rows = retrieve_top_k(user_text, index_rows, top_k=TOP_K)
        answer = answer_with_rag(user_text, top_rows)
        return _chat_message(answer)

    except Exception as e:
        return _chat_message(f"Error: {str(e)}")