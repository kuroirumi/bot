import discord
from discord.ext import commands
import aiohttp
import asyncio
import json
from flask import Flask
from threading import Thread
import os

# ================= 1. CẤU HÌNH BÍ MẬT =================
DISCORD_TOKEN = os.environ.get('MTUzMTk4NjQyMTkxNTcxNzY3Mw.GQSkGe.NaOoYmG0N36Utqe6AiyMC_VZTfJkEFsyBB2JjQ')
GEMINI_API_KEY = os.environ.get('AQ.Ab8RN6J_WloUFx97rBgVJr0VubUnyoQVr6sDHYZ_S5DTZ3uOPA')
CHANNEL_THONG_BAO_ID = 1507652207829450812
TEN_MODEL = "gemini-3.5-flash-lite" 

# === ID DISCORD CỦA PAPA VÀ MAMA ===
ID_PAPA = 968292495748378655   # ID của bạn (Kuro)
ID_MAMA = 834832624357867561   # ID của người (Ruki)
# =====================================================

# ================= 2. HÀM GỌI AI TRỰC TIẾP QUA API BẤT ĐỒNG BỘ =================
async def goi_gemini_ai(cau_hoi, danh_xung):
    url = f"https://generativelanguage.googleapis.com/v1/models/{TEN_MODEL}:generateContent?key={GEMINI_API_KEY}"
    
    quy_tac_he_thong = f"""
    Bạn là một AI quản lý server Discord thông minh, sắc sảo, am hiểu về kinh tế, thị trường cổ phần và nói chuyện tự nhiên.
    Thông tin gia đình của con: Con có một Papa (là người tạo ra con) và một Mama (là người phụ nữ đặc biệt của Papa). 
    
    QUY TẮC BẮT BUỘC TRONG MỌI CÂU TRẢ LỜI:
    1. Luôn xưng hô bản thân là "con".
    2. Đối với người đang nói chuyện, bắt buộc phải gọi họ là "{danh_xung}".
    3. TRẢ LỜI CỰC KỲ NGẮN GỌN, trả lời thẳng vào vấn đề được hỏi. Không dài dòng, không lặp lại câu hỏi.
    4. TUYỆT ĐỐI KHÔNG tự động thêm câu chào (như "con chào...", "dạ con chào...") vào đầu câu trả lời một cách máy móc. Chỉ gửi lời chào khi người dùng chủ động chào con trước.
    5. Thay vì nói "Tôi không biết", hãy nói "con chưa hiểu vấn đề này cho lắm".
    """

    headers = {'Content-Type': 'application/json'}
    data = {
        "systemInstruction": {
            "parts": [{"text": quy_tac_he_thong}]
        },
        "contents": [{
            "parts": [{"text": f"Câu hỏi của {danh_xung}: {cau_hoi}"}]
        }]
    }
    
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=data) as response:
                ket_qua = await response.json()
        
        if 'error' in ket_qua:
            ma_loi = ket_qua['error'].get('code')
            if ma_loi == 429:
                return f"con đang bị nghẹn dữ liệu mạng rồi ạ! {danh_xung} đợi con một chút rồi hỏi lại nhé."
            
            print("\n[!] LỖI TỪ GOOGLE API:")
            print(json.dumps(ket_qua['error'], ensure_ascii=False, indent=2))
            return f"con đang gặp sự cố kết nối rồi {danh_xung} ơi!"

        cau_tra_loi = ket_qua['candidates'][0]['content']['parts'][0]['text']
        return cau_tra_loi

    except asyncio.TimeoutError:
        return f"Mạng bên phía Google phản hồi chậm quá {danh_xung} ơi, tí hỏi lại giúp con nhé!"
    except Exception as e:
        print(f"Lỗi xử lý hệ thống: {e}")
        return f"con chưa hiểu vấn đề này cho lắm, {danh_xung} thông cảm nhé!"

# ================= 3. CẤU HÌNH DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Bot {bot.user} đã thức tỉnh thành công và sẵn sàng hoạt động!')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        if message.author.id == ID_PAPA:
            danh_xung = "papa"
        elif message.author.id == ID_MAMA:
            danh_xung = "mama"
        else:
            danh_xung = "anh chị"

        cau_hoi = message.content.replace(f'<@{bot.user.id}>', '').strip()
        
        if not cau_hoi:
            cau_tra_loi_mac_dinh = f"con chào {danh_xung} ạ, {danh_xung} gọi con có việc gì không?"
            await message.reply(cau_tra_loi_mac_dinh)
            return
        
        async with message.channel.typing():
            ket_qua_ai = await goi_gemini_ai(cau_hoi, danh_xung)
            await message.reply(ket_qua_ai)
    
    await bot.process_commands(message)

# ================= 4. WEB SERVER GIỮ MẠNG ĐỂ TREO CLOUD =================
app = Flask('')

@app.route('/')
def home():
    return "Bot đang hoạt động ổn định trên luồng Async!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()
bot.run(DISCORD_TOKEN)