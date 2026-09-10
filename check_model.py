import aiohttp
import asyncio
import json

import os
from pathlib import Path

def _lay_api_key():
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return os.getenv("GEMINI_API_KEY")

GEMINI_API_KEY = _lay_api_key()

async def kiem_tra_danh_sach_model():
    url = f"https://generativelanguage.googleapis.com/v1/models?key={GEMINI_API_KEY}"
    
    print("Đang kết nối tới Google để lấy danh sách model...")
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            ket_qua = await response.json()
            
            if "models" in ket_qua:
                print("\n=== DANH SÁCH MODEL KHẢ DỤNG ===")
                for m in ket_qua["models"]:
                    ten_model = m.get("name", "").replace("models/", "")
                    phuong_thuc = m.get("supportedGenerationMethods", [])
                    # Chỉ hiển thị các model hỗ trợ sinh nội dung (generateContent)
                    if "generateContent" in phuong_thuc:
                        print(f"👉 {ten_model}")
            else:
                print("\n[!] Lỗi lấy danh sách:", json.dumps(ket_qua, ensure_ascii=False, indent=2))

asyncio.run(kiem_tra_danh_sach_model())