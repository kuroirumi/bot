import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from threading import Thread
from google import genai
from google.genai import types

try:
    from flask import Flask
except ImportError:  # Flask may not be installed in all environments
    Flask = None

# ================= 1. CẤU HÌNH BÍ MẬT =================
def _load_env_file():
    """Tự động đọc cấu hình từ file .env nếu có."""
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception as e:
            print(f"Lỗi đọc .env: {e}")

_load_env_file()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_THONG_BAO_ID = int(os.getenv("CHANNEL_THONG_BAO_ID"))

# Danh sách model được sắp xếp ưu tiên từ nhanh mượt, ổn định nhất xuống các lựa chọn dự phòng
MODEL_LIST = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
]
TEN_MODEL = MODEL_LIST[0]

# === ID DISCORD CỦA PAPA VÀ MAMA ===
ID_PAPA = int(os.getenv("ID_PAPA"))   # ID của (Kuro)
ID_MAMA = int(os.getenv("ID_MAMA"))   # ID của (Ruki)
# =====================================================

# Khởi tạo client chính thức của Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

# Khóa đơn-instance để tránh khởi động 2 bot cùng lúc gây trả lời 2 lần
BOT_LOCK_FILE = Path(__file__).resolve().parent / ".bot_genai.lock"


def _is_pid_running(pid: int) -> bool:
    """Kiểm tra an toàn tiến trình có đang chạy trên cả Windows và Linux."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _acquire_single_instance_lock() -> bool:
    try:
        with BOT_LOCK_FILE.open("x", encoding="utf-8") as lock_file:
            lock_file.write(str(os.getpid()))
            lock_file.flush()
        return True
    except FileExistsError:
        try:
            with BOT_LOCK_FILE.open("r", encoding="utf-8") as lock_file:
                pid_text = (lock_file.read() or "").strip()
            if pid_text and pid_text.isdigit():
                pid = int(pid_text)
                if pid != os.getpid() and _is_pid_running(pid):
                    print(f"Bot đã đang chạy ở PID {pid}. Không khởi động thêm instance mới.")
                    return False
                else:
                    BOT_LOCK_FILE.unlink(missing_ok=True)
                    return _acquire_single_instance_lock()
        except Exception:
            pass
        return False
    except Exception as exc:
        print(f"Không tạo được lock bot: {exc}")
        return False


def _release_single_instance_lock():
    try:
        BOT_LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


if not _acquire_single_instance_lock():
    raise SystemExit(0)

import atexit
atexit.register(_release_single_instance_lock)

# Bộ nhớ hội thoại theo kênh/server
conversation_memory = {}
MEMORY_FILE = Path(os.getenv("CONVERSATION_MEMORY_FILE", Path(__file__).resolve().parent / "conversation_memory.json"))
MAX_MEMORY_ENTRIES = 5000
MAX_CONTEXT_TURNS = 200
MAX_CHANNEL_HISTORY = 5000
MEMORY_RETENTION_DAYS = 15  # Tự động xóa các đoạn chat cũ quá 15 ngày

# Bộ nhớ kiểm tra tin nhắn trùng lặp
seen_message_signatures = {}
MAX_MESSAGE_SIGNATURES = 2000
DUPLICATE_SIGNATURE_TTL = 5  # 5 giây

# Chống xử lý trùng cùng 1 tin nhắn nhiều lần
processed_message_ids = {}
PROCESSED_MESSAGE_TTL = 5

# Bộ đếm spam theo cửa sổ trượt (Sliding Window)
user_message_timestamps = {}
SPAM_WINDOW_SECONDS = 5   # Trong 5 giây
SPAM_THRESHOLD = 4        # Gửi từ 4 tin nhắn trong 5 giây sẽ bị mute
MUTE_DURATION_SECONDS = 300  # 5 phút


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _compute_message_signature(message: discord.Message) -> str:
    user_id = str(message.author.id)
    content = (message.content or "").strip().lower()
    # Chữ ký phải bao gồm cả user_id để tránh việc 2 người khác nhau chat cùng 1 câu bị xóa nhầm
    signature = hashlib.sha256(f"{user_id}:{content}".encode("utf-8"))

    for attachment in message.attachments:
        try:
            data = await attachment.read()
        except Exception as e:
            print(f"Không thể đọc attachment {attachment.url}: {e}")
            continue
        signature.update(data)

    return signature.hexdigest()


def _expire_old_signatures():
    now = asyncio.get_event_loop().time()
    expired = [sig for sig, ts in seen_message_signatures.items() if now - ts > DUPLICATE_SIGNATURE_TTL]
    for sig in expired:
        seen_message_signatures.pop(sig, None)


async def _should_delete_message(message: discord.Message) -> bool:
    if not message.content and not message.attachments:
        return False

    _expire_old_signatures()
    signature = await _compute_message_signature(message)
    if signature in seen_message_signatures:
        return True

    seen_message_signatures[signature] = asyncio.get_event_loop().time()
    if len(seen_message_signatures) > MAX_MESSAGE_SIGNATURES:
        oldest = sorted(seen_message_signatures.items(), key=lambda item: item[1])
        for sig, _ in oldest[: len(seen_message_signatures) - MAX_MESSAGE_SIGNATURES]:
            seen_message_signatures.pop(sig, None)

    return False


def _expire_old_processed_messages():
    now = asyncio.get_event_loop().time()
    expired = [msg_id for msg_id, ts in processed_message_ids.items() if now - ts > PROCESSED_MESSAGE_TTL]
    for msg_id in expired:
        processed_message_ids.pop(msg_id, None)


async def _should_ignore_duplicate_message(message: discord.Message) -> bool:
    _expire_old_processed_messages()
    msg_id = message.id
    if msg_id in processed_message_ids:
        return True
    processed_message_ids[msg_id] = asyncio.get_event_loop().time()
    return False


async def _handle_spam_mute(message: discord.Message):
    """Xử lý mute khi spam bằng thuật toán Sliding Window."""
    if message.author.bot or not message.guild:
        return

    user_id = str(message.author.id)
    now = asyncio.get_event_loop().time()

    # Chỉ tính các tin nhắn gửi trong 5 giây gần nhất
    timestamps = user_message_timestamps.get(user_id, [])
    timestamps = [ts for ts in timestamps if now - ts < SPAM_WINDOW_SECONDS]
    timestamps.append(now)
    user_message_timestamps[user_id] = timestamps

    if len(timestamps) >= SPAM_THRESHOLD:
        bot_member = message.guild.me
        if not bot_member.guild_permissions.moderate_members:
            await message.channel.send("con thiếu quyền Moderate Members nên không mute được nè")
            print("Bot không có quyền Moderate Members")
            return

        if message.author.top_role >= bot_member.top_role:
            await message.channel.send("con không mute được vì role của người này ngang hoặc cao hơn con")
            print("Role người dùng không thấp hơn bot")
            return

        try:
            await message.author.timeout(
                discord.utils.utcnow() + datetime.timedelta(seconds=MUTE_DURATION_SECONDS),
                reason="spam"
            )
            await message.channel.send(
                f"con đã mute {message.author.display_name} 5 phút vì spam rồi nè"
            )
            user_message_timestamps[user_id] = []  # Reset danh sách sau khi đã xử phạt
        except discord.Forbidden as e:
            await message.channel.send("con không mute được vì thiếu quyền hoặc role bị chặn")
            print(f"Không mute được người dùng {message.author}: {e}")
        except Exception as e:
            print(f"Không mute được người dùng {message.author}: {e}")


def _normalize_memory_key(key):
    key_str = str(key)
    if key_str.startswith("user:") or ":" in key_str:
        return key_str
    return f"user:{key_str}"


def _prune_expired_memory(memory_list):
    """Lọc bỏ các đoạn chat cũ quá 15 ngày."""
    if not memory_list:
        return []
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=MEMORY_RETENTION_DAYS)
    valid_entries = []
    for item in memory_list:
        raw_ts = item.get("timestamp")
        if raw_ts:
            try:
                msg_time = datetime.datetime.fromisoformat(raw_ts)
                if msg_time.tzinfo is None:
                    msg_time = msg_time.replace(tzinfo=datetime.timezone.utc)
                if msg_time >= cutoff:
                    valid_entries.append(item)
                continue
            except Exception:
                pass
        # Nếu là tin nhắn cũ trước đây chưa có timestamp: giữ lại nếu chưa vượt giới hạn
        valid_entries.append(item)
    return valid_entries


def _load_memory_from_disk():
    global conversation_memory
    if MEMORY_FILE.exists():
        try:
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                normalized = {}
                pruned_total = 0
                for raw_key, value in data.items():
                    if isinstance(value, list):
                        before = len(value)
                        pruned = _prune_expired_memory(value)
                        pruned_total += (before - len(pruned))
                        if pruned:
                            normalized[_normalize_memory_key(raw_key)] = pruned
                conversation_memory = normalized
                if pruned_total > 0:
                    print(f"Đã tự động dọn dẹp {pruned_total} tin nhắn cũ quá {MEMORY_RETENTION_DAYS} ngày khi nạp memory.")
                return
        except Exception as e:
            print(f"Không thể đọc memory file {MEMORY_FILE}: {e}")
    conversation_memory = {}


def _save_memory_to_disk():
    """Ghi memory an toàn (atomic write) để chống hỏng file khi bot bị tắt bất ngờ."""
    try:
        temp_file = MEMORY_FILE.with_suffix(".tmp")
        # Ghi JSON không thụt lề để giảm kích thước file và tăng tốc I/O
        temp_file.write_text(json.dumps(conversation_memory, ensure_ascii=False), encoding="utf-8")
        temp_file.replace(MEMORY_FILE)
    except Exception as e:
        print(f"Không thể ghi memory file {MEMORY_FILE}: {e}")


# Load memory from disk khi bot khởi động
_load_memory_from_disk()

def _get_conversation_key(message: discord.Message) -> str:
    guild_id = getattr(message.guild, "id", "dm")
    channel_id = getattr(message.channel, "id", "dm")
    return f"{guild_id}:{channel_id}"


# Hàm đọc quy tắc từ file prompt.txt bên ngoài
def lay_quy_tac_he_thong(danh_xung):
    base_dir = Path(__file__).resolve().parent
    prompt_path = Path(os.getenv("PROMPT_FILE", base_dir / "prompt.txt"))

    if not prompt_path.is_absolute():
        prompt_path = base_dir / prompt_path

    try:
        with prompt_path.open("r", encoding="utf-8") as f:
            noi_dung = f.read()
        return noi_dung.format(danh_xung=danh_xung)
    except Exception as e:
        print(f"Không đọc được file quy tắc tại {prompt_path}: {e}")
        return f"Bạn là AI quản lý Discord. Luôn xưng con, gọi người hỏi là {danh_xung}."

# ================= 2. HÀM GỌI AI TỐC ĐỘ CAO =================
def _get_channel_memory(message: discord.Message):
    key = _get_conversation_key(message)
    if key not in conversation_memory:
        conversation_memory[key] = []
    return conversation_memory[key]


def _get_user_memory(user_key):
    key = f"user:{user_key}"
    if key not in conversation_memory:
        conversation_memory[key] = []
    return conversation_memory[key]


def _append_channel_memory(message: discord.Message, role: str, content: str):
    memory = _get_channel_memory(message)
    name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "unknown")
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    memory.append({
        "role": role,
        "content": content,
        "user_id": str(message.author.id),
        "user_name": name,
        "timestamp": now_iso,
    })
    # Tự động lọc các đoạn chat cũ quá 15 ngày
    memory[:] = _prune_expired_memory(memory)
    if len(memory) > MAX_CHANNEL_HISTORY:
        memory[:] = memory[-MAX_CHANNEL_HISTORY:]
    _save_memory_to_disk()


def _get_context_memory(message: discord.Message):
    channel_memory = _prune_expired_memory(_get_channel_memory(message))
    user_memory = _prune_expired_memory(_get_user_memory(str(message.author.id)))

    merged = []
    if channel_memory:
        merged.extend(channel_memory)
    if user_memory:
        merged.extend(user_memory)

    if not merged:
        return []

    recent = merged[-MAX_CONTEXT_TURNS:]
    seen = set()
    clean = []
    for item in recent:
        marker = (item.get("role"), item.get("user_id"), item.get("content"))
        if marker in seen:
            continue
        seen.add(marker)
        clean.append(item)
    return clean


def _get_server_emoji_names(guild):
    if guild is None or not getattr(guild, "emojis", None):
        return []

    names = []
    for emoji in sorted(guild.emojis, key=lambda e: (getattr(e, 'animated', False), (getattr(e, 'name', '') or '').lower())):
        name = getattr(emoji, 'name', None)
        if name:
            names.append(name)
    return names[:30]


def _get_server_emoji_mentions(guild):
    if guild is None or not getattr(guild, "emojis", None):
        return {}

    mentions = {}
    for emoji in guild.emojis:
        name = getattr(emoji, "name", None)
        mention = getattr(emoji, "mention", None)
        if name and mention:
            mentions[name] = mention
            mentions[name.lower()] = mention
            mentions[f":{name}:"] = mention
            mentions[f":{name.lower()}:"] = mention
    return mentions


def _get_server_snapshot(guild):
    if guild is None:
        return ""

    emoji_names = _get_server_emoji_names(guild)

    channel_names = []
    if getattr(guild, "channels", None):
        for channel in sorted(guild.channels, key=lambda ch: ch.position if hasattr(ch, 'position') else 0)[:20]:
            if hasattr(channel, "name"):
                channel_names.append(f"#{channel.name}")

    server_lines = [
        f"Tên server: {guild.name}",
        f"Số thành viên: {guild.member_count}",
    ]

    if emoji_names:
        server_lines.append("Emoji custom có sẵn trong server: " + ", ".join(emoji_names))
    else:
        server_lines.append("Emoji custom có sẵn trong server: không có")

    if channel_names:
        server_lines.append("Kênh chính: " + ", ".join(channel_names[:10]))

    return "\n".join(server_lines)


def _replace_emoji_names_with_mentions(text: str, guild):
    if not text or guild is None:
        return text

    emoji_mentions = _get_server_emoji_mentions(guild)
    if not emoji_mentions:
        return text

    text = text.strip()

    for key in sorted(emoji_mentions.keys(), key=lambda item: len(item), reverse=True):
        if key.startswith(":"):
            text = text.replace(key, emoji_mentions[key])

    for name in sorted({k for k in emoji_mentions if not k.startswith(":")}, key=lambda item: len(item), reverse=True):
        mention = emoji_mentions[name]
        pattern = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])', re.IGNORECASE)
        text = pattern.sub(mention, text)

    return text


async def _delete_requested_messages(message: discord.Message) -> bool:
    content_lower = (message.content or "").lower()
    if "xóa tin nhắn" not in content_lower:
        return False

    if not message.guild:
        await message.channel.send("con chỉ xóa được trong server thôi nhé.")
        return True

    bot_member = message.guild.me
    if bot_member is None:
        await message.channel.send("con chưa thấy thông tin server, thử lại sau nhé.")
        return True

    if not message.channel.permissions_for(bot_member).manage_messages:
        await message.channel.send("con cần quyền Manage Messages để xóa tin nhắn.")
        return True

    target_user = message.author
    if message.mentions:
        for user in message.mentions:
            if user != bot.user:
                target_user = user
                break

    deleted = 0
    async for msg in message.channel.history(limit=100, before=message.created_at):
        if msg.author == target_user and not msg.pinned:
            try:
                await msg.delete()
                deleted += 1
            except discord.Forbidden as e:
                print(f"Không xóa được tin nhắn {msg.id} vì bị Forbidden: {e}")
                await message.channel.send(
                    "con không xóa được một số tin nhắn vì thiếu quyền xóa tin nhắn cũ.",
                    delete_after=10,
                )
                return True
            except Exception as e:
                print(f"Không xóa được tin nhắn {msg.id}: {e}")

    if deleted == 0:
        await message.channel.send(
            f"con không tìm thấy tin nhắn của {target_user.display_name} để xóa trong kênh này.",
            delete_after=10,
        )
    else:
        await message.channel.send(
            f"con đã xóa {deleted} tin nhắn gần nhất của {target_user.display_name} nhé.",
            delete_after=10,
        )
    return True


def _update_user_memory(user_key, role, content):
    memory = _get_user_memory(user_key)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    memory.append({
        "role": role,
        "content": content,
        "timestamp": now_iso,
    })
    memory[:] = _prune_expired_memory(memory)
    if len(memory) > MAX_MEMORY_ENTRIES:
        memory[:] = memory[-MAX_MEMORY_ENTRIES:]
    _save_memory_to_disk()


async def _build_multimodal_prompt_parts(message: discord.Message, cau_hoi: str):
    prompt = cau_hoi.strip() if cau_hoi.strip() else "Hãy mô tả và giải thích nội dung trong ảnh/file đính kèm này."
    parts = [prompt]

    for attachment in message.attachments:
        filename = attachment.filename or "file"
        mime_type = attachment.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        try:
            payload = await attachment.read()
        except Exception as exc:
            parts.append(f"[Không đọc được file {filename}: {exc}]")
            continue

        if mime_type.startswith("image/"):
            parts.append(types.Part.from_bytes(data=payload, mime_type=mime_type))
            continue

        if mime_type in {
            "application/pdf",
            "text/plain",
            "text/csv",
            "application/json",
            "application/xml",
            "text/markdown",
            "text/xml",
            "application/octet-stream",
        } or filename.lower().endswith((
            ".txt", ".md", ".csv", ".json", ".log", ".xml", ".yaml", ".yml",
            ".js", ".py", ".java", ".c", ".cpp", ".html", ".css"
        )):
            parts.append(types.Part.from_bytes(data=payload, mime_type=mime_type if mime_type != "application/octet-stream" else "text/plain"))
            continue

        parts.append(f"[File đính kèm: {filename} ({mime_type}), URL: {attachment.url}]")

    return parts


async def goi_gemini_ai(cau_hoi, danh_xung, message):
    quy_tac_he_thong = lay_quy_tac_he_thong(danh_xung)
    memory = _get_context_memory(message)
    guild = getattr(message, "guild", None)
    server_snapshot = _get_server_snapshot(guild)
    emoji_names = _get_server_emoji_names(guild)
    emoji_mentions = _get_server_emoji_mentions(guild)

    history_text = ""
    if memory:
        history_text = "\n".join(
            f"{item.get('user_name', item.get('user_id', 'unknown'))} ({item['role']}): {item['content']}"
            for item in memory
        )

    prompt = f"Câu hỏi của {danh_xung}: {cau_hoi if cau_hoi.strip() else 'Hãy phân tích ảnh/file đính kèm bên dưới.'}"
    if history_text:
        prompt = (
            "Đây là một cuộc hội thoại nhóm trong cùng kênh Discord. "
            "Hãy đọc toàn bộ lịch sử gần đây của tất cả mọi người, trả lời mạch lạc, "
            "có liên quan đến chủ đề đang nói và không bỏ sót ngữ cảnh trước đó.\n\n"
            f"Lịch sử trò chuyện gần đây:\n{history_text}\n\n"
            f"Câu hỏi mới của {danh_xung}: {cau_hoi if cau_hoi.strip() else 'Phân tích ảnh/file đính kèm.'}"
        )

    if server_snapshot:
        emoji_hint = " ".join(list(emoji_mentions.values())[:3]) if emoji_mentions else "emoji server"
        prompt = (
            f"{prompt}\n\nThông tin server hiện tại:\n{server_snapshot}\n\n"
            f"Nếu cần dùng emoji custom, hãy dùng exact mention string sau: {emoji_hint}. "
            "Không viết tên emoji thuần; chỉ dùng exact mention string của emoji trong server."
        )

    content_parts = [prompt]
    if message.attachments:
        content_parts.extend(await _build_multimodal_prompt_parts(message, cau_hoi))

    last_error = None
    for model_name in MODEL_LIST:
        try:
            if hasattr(client, "aio") and hasattr(client.aio, "models"):
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=content_parts,
                    config={"system_instruction": quy_tac_he_thong},
                )
            else:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda m=model_name, parts=content_parts: client.models.generate_content(
                        model=m,
                        contents=parts,
                        config={"system_instruction": quy_tac_he_thong},
                    )
                )
            if response and getattr(response, "text", None):
                text = response.text.strip()
                if text:
                    text = _replace_emoji_names_with_mentions(text, getattr(message, "guild", None))
                    _append_channel_memory(message, "user", cau_hoi or "[ảnh/file]")
                    _append_channel_memory(message, "assistant", text)
                    if len(text) > 700:
                        # Cắt theo ranh giới từ gần nhất để tránh đứt chữ
                        cut_idx = text[:700].rfind(" ")
                        if cut_idx > 400:
                            return text[:cut_idx] + "..."
                        return text[:700] + "..."
                    return text
        except Exception as e:
            last_error = f"Model {model_name} lỗi: {e}"

    print(f"Lỗi API cuối cùng: {last_error}")
    return f"con đang bận chút xíu, {danh_xung} hỏi lại con nha!"

# ================= 3. CẤU HÌNH DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Tác vụ chạy nền định kỳ mỗi 6 tiếng để dọn dẹp các tin nhắn cũ quá 15 ngày
@tasks.loop(hours=6)
async def periodic_prune_memory_task():
    global conversation_memory
    pruned_total = 0
    for key in list(conversation_memory.keys()):
        before = len(conversation_memory[key])
        conversation_memory[key] = _prune_expired_memory(conversation_memory[key])
        after = len(conversation_memory[key])
        pruned_total += (before - after)
        if not conversation_memory[key]:
            conversation_memory.pop(key, None)
    if pruned_total > 0:
        print(f"[Định kỳ] Đã dọn dẹp {pruned_total} tin nhắn cũ quá {MEMORY_RETENTION_DAYS} ngày khỏi memory.")
        _save_memory_to_disk()

@bot.event
async def on_ready():
    if not periodic_prune_memory_task.is_running():
        periodic_prune_memory_task.start()
    print("--------------------------------------------------")
    print(f"Bot {bot.user} đã thức tỉnh thành công!")
    print(f"Danh sách model ưu tiên: {MODEL_LIST}")
    print(f"Model khởi chạy mặc định: {TEN_MODEL}")
    print(f"Prompt file đang dùng: {Path(__file__).resolve().parent / 'prompt.txt'}")
    print(f"Cơ chế bộ nhớ: Tự động dọn dẹp tin nhắn cũ hơn {MEMORY_RETENTION_DAYS} ngày")
    print("Trạng thái: Sẵn sàng, tốc độ cao & đang đọc prompt từ file ngoài!")
    print("--------------------------------------------------")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if await _should_ignore_duplicate_message(message):
        return

    if await _delete_requested_messages(message):
        return

    if await _should_delete_message(message):
        if message.guild:
            bot_member = message.guild.me
            if not message.channel.permissions_for(bot_member).manage_messages:
                await message.channel.send(
                    "Bot cần quyền `Manage Messages` để xóa tin nhắn trùng lặp.",
                    delete_after=10,
                )
                print("Bot không có quyền Manage Messages để xóa tin nhắn.")
                return

        try:
            await message.delete()
            print(f"Đã xóa tin nhắn trùng lặp từ {message.author}.")
            if message.guild:
                await message.channel.send(
                    f"con đã xóa tin nhắn mà {message.author.display_name} spam rồi nè",
                    delete_after=5,
                )
                await _handle_spam_mute(message)
        except Exception as e:
            print(f"Không xóa được tin nhắn trùng lặp: {e}")
            if message.guild:
                await message.channel.send(
                    "Bot đã cố xóa nhưng gặp lỗi. Kiểm tra quyền và trạng thái tin nhắn.",
                    delete_after=10,
                )
        return

    if bot.user.mentioned_in(message):
        if message.author.id == ID_PAPA:
            danh_xung = "papa"
        elif message.author.id == ID_MAMA:
            danh_xung = "mama"
        else:
            danh_xung = message.author.display_name or message.author.name

        # Lọc bỏ mention của bot (hỗ trợ cả 2 dạng <@ID> và <@!ID>)
        cau_hoi = message.content
        cau_hoi = cau_hoi.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
        
        # Tự động thay thế các thẻ @ của người khác thành tên hiển thị để AI nhận diện được
        if message.guild:
            for user in message.mentions:
                if user.id != bot.user.id:
                    cau_hoi = cau_hoi.replace(f'<@{user.id}>', f'@{user.display_name}').replace(f'<@!{user.id}>', f'@{user.display_name}')

        if not cau_hoi and not message.attachments:
            cau_tra_loi_mac_dinh = f"con chào {danh_xung} ạ, {danh_xung} gọi con có việc gì không?"
            await message.reply(cau_tra_loi_mac_dinh)
            return

        async with message.channel.typing():
            ket_qua_ai = await goi_gemini_ai(cau_hoi, danh_xung, message)
            await message.reply(ket_qua_ai)
    
    await bot.process_commands(message)

# ================= 4. WEB SERVER GIỮ MẠNG =================
if Flask is not None:
    app = Flask('')

    @app.route('/')
    def home():
        return f"Bot đang chạy mượt với model: {TEN_MODEL}!"

    def run():
        port = int(os.environ.get("PORT", 8080))
        app.run(host='0.0.0.0', port=port)

    def keep_alive():
        t = Thread(target=run)
        t.start()

    keep_alive()

bot.run(DISCORD_TOKEN)