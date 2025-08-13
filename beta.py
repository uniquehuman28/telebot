import os
import re
import uuid
import asyncio
import shutil
from pathlib import Path
from typing import List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ========= Config =========
BOT_TOKEN = os.getenv("BOT_TOKEN")  # set via env var di hosting/VPS
BASE_DIR = Path(__file__).parent.resolve()
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

ADMIN_IDS = [123456789]  # <-- Ganti dengan Telegram user_id kamu (int)

# ========= Helpers =========

def format_number(raw: str, default_cc="+62", min_len=8, max_len=15):
    raw = (raw or "").strip()
    m = re.search(r"\+?\d{3,}", raw)
    if not m:
        return None
    token = m.group(0)

    if token.startswith("00"):
        token = "+" + token[2:]
    elif token.startswith("0"):
        token = default_cc + token[1:]
    elif not token.startswith("+"):
        token = "+" + token

    digits = re.sub(r"\D", "", token)
    if not (min_len <= len(digits) <= max_len):
        return None

    patterns = {
        "+62": r"(\+62)(\d{3,4})(\d+)",
        "+852": r"(\+852)(\d{4})(\d{4})",
        "+60": r"(\+60)(\d{2,3})(\d+)",
        "+65": r"(\+65)(\d{4})(\d{4})",
        "+91": r"(\+91)(\d{5})(\d{5})",
        "+92": r"(\+92)(\d{3,4})(\d+)",
        "+880": r"(\+880)(\d{3,4})(\d+)",
        "+966": r"(\+966)(\d{3})(\d+)",
        "+971": r"(\+971)(\d{2,3})(\d+)",
        "+63": r"(\+63)(\d{3})(\d+)",
        "+234": r"(\+234)(\d{3})(\d+)",
        "+1": r"(\+1)(\d{3})(\d{3})(\d{4})",
    }
    for code, pattern in patterns.items():
        if token.startswith(code):
            m2 = re.match(pattern, token)
            if m2:
                return " ".join(m2.groups())
            break
    return token

def remove_duplicates(numbers: List[str]) -> List[str]:
    seen, result = set(), []
    for num in numbers:
        if num not in seen:
            seen.add(num)
            result.append(num)
    return result

def list_txt_files(folder_path: Path) -> List[Path]:
    return sorted(Path(folder_path).glob("*.txt"))

def write_vcard_batch(vcf_path: Path, contact_fullname_number_pairs: List[Tuple[str, str]]):
    temp_path = str(vcf_path) + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="") as vcf:
        for fullname, num in contact_fullname_number_pairs:
            vcf.write("BEGIN:VCARD\r\n")
            vcf.write("VERSION:3.0\r\n")
            vcf.write(f"FN:{fullname}\r\n")
            parts = fullname.split(" ", 1)
            family = parts[1] if len(parts) > 1 else ""
            given = parts[0]
            vcf.write(f"N:{family};{given};;;\r\n")
            vcf.write(f"UID:{uuid.uuid4()}\r\n")
            vcf.write(f"TEL;TYPE=CELL:{num}\r\n")
            vcf.write("END:VCARD\r\n\r\n")
    os.replace(temp_path, vcf_path)

def plan_outputs(src_folder: Path, base_file_name: str, per_file: int, output_dir: Path):
    plan = []
    conflicts = set()
    total_contacts = 0
    batch_idx_global = 0

    txt_files = list_txt_files(src_folder)
    if not txt_files:
        raise ValueError("Folder tidak berisi file .txt.")

    all_numbers = []
    invalid_count = 0
    for src in txt_files:
        with open(src, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                n = format_number(line)
                if n:
                    all_numbers.append(n)
                else:
                    invalid_count += 1

    all_numbers = remove_duplicates(all_numbers)
    total_contacts = len(all_numbers)
    if total_contacts == 0:
        return [], 0, conflicts, invalid_count

    for idx in range(0, len(all_numbers), per_file):
        batch_idx_global += 1
        target_name = f"{base_file_name} {batch_idx_global}.vcf"
        target_path = output_dir / target_name
        if target_path.exists():
            conflicts.add(str(target_path))
        plan.append((all_numbers[idx:idx+per_file], target_path))

    return plan, total_contacts, conflicts, invalid_count

# ========= FSM States =========
class UploadStates(StatesGroup):
    collecting = State()
    ask_contact = State()
    ask_outbase = State()
    ask_perfile = State()
    processing = State()

# ========= Bot setup =========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def session_paths(user_id: int) -> Tuple[Path, Path]:
    in_dir = SESSIONS_DIR / str(user_id) / "in"
    out_dir = SESSIONS_DIR / str(user_id) / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    return in_dir, out_dir

def clear_session(user_id: int):
    user_dir = SESSIONS_DIR / str(user_id)
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)

# ========= Commands & Handlers =========

@dp.message(Command("start"))
async def start_cmd(msg: Message, state: FSMContext):
    clear_session(msg.from_user.id)
    session_paths(msg.from_user.id)
    await state.set_state(UploadStates.collecting)
    await state.update_data(uploaded_files=[])
    await msg.answer(
        "Halo! Kirimkan satu atau beberapa file .txt (satu nomor per baris).\n"
        "Jika sudah selesai, ketik /konfirmasi."
    )

@dp.message(UploadStates.collecting, F.document)
async def handle_document(msg: Message, state: FSMContext):
    doc = msg.document
    if not (doc.file_name.lower().endswith(".txt") or doc.mime_type == "text/plain"):
        await msg.reply("❌ Hanya mendukung file .txt.")
        return

    in_dir, _ = session_paths(msg.from_user.id)
    dest = in_dir / doc.file_name

    # Unduh ke server
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=dest)

    # Simpan daftar file ke state
    data = await state.get_data()
    uploaded = data.get("uploaded_files", [])
    uploaded.append(doc.file_name)
    await state.update_data(uploaded_files=uploaded)

    await msg.reply(
        f"✅ {doc.file_name} tersimpan. Total: {len(uploaded)} file.\n"
        f"Ketik /konfirmasi jika sudah selesai."
    )

@dp.message(Command("konfirmasi"))
async def cmd_konfirmasi(msg: Message, state: FSMContext):
    data = await state.get_data()
    uploaded = data.get("uploaded_files", [])
    if not uploaded:
        await msg.reply("Belum ada file .txt yang diunggah. Unggah dulu lalu ketik /konfirmasi.")
        return

    summary = "\n".join([f"• {name}" for name in uploaded])
    await msg.reply(
        f"📂 File yang diunggah ({len(uploaded)}):\n{summary}\n\n"
        f"Sekarang masukkan Nama Kontak Dasar:"
    )
    await state.set_state(UploadStates.ask_contact)

@dp.message(Command("hapus_cache"))
async def hapus_cache_cmd(msg: Message, state: FSMContext):
    clear_session(msg.from_user.id)
    await state.clear()
    await msg.reply("🧹 Cache & file sementara untuk sesi kamu sudah dihapus dari server.")

@dp.message(Command("hapus_semua_cache"))
async def hapus_semua_cache_cmd(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.reply("❌ Kamu tidak punya izin menjalankan perintah ini.")
        return

    count = 0
    for folder in SESSIONS_DIR.glob("*"):
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            count += 1

    await msg.reply(f"🧹 Semua cache ({count} user) sudah dihapus dari server.")

@dp.message(UploadStates.ask_contact)
async def ask_outbase(msg: Message, state: FSMContext):
    contact = (msg.text or "").strip()
    if not contact:
        await msg.reply("Nama kontak tidak boleh kosong. Coba lagi:")
        return
    await state.update_data(contact_name=contact)
    await state.set_state(UploadStates.ask_outbase)
    await msg.reply("Masukkan Nama File Output Dasar (mis. Kontak, Teman, dsb):")

@dp.message(UploadStates.ask_outbase)
async def ask_perfile(msg: Message, state: FSMContext):
    outbase = (msg.text or "").strip()
    if not outbase:
        await msg.reply("Nama file dasar tidak boleh kosong. Coba lagi:")
        return
    await state.update_data(base_file=outbase)
    await state.set_state(UploadStates.ask_perfile)
    await msg.reply("Berapa kontak maksimal per file .vcf? (angka, mis. 500)")

@dp.message(UploadStates.ask_perfile)
async def process_inputs(msg: Message, state: FSMContext):
    try:
        per_file = int((msg.text or "").strip())
        if per_file <= 0:
            raise ValueError
    except ValueError:
        await msg.reply("Harus berupa angka > 0. Coba lagi:")
        return

    data = await state.get_data()
    contact_name = data["contact_name"]
    base_file_name = data["base_file"]

    in_dir, out_dir = session_paths(msg.from_user.id)
    await state.set_state(UploadStates.processing)
    status = await msg.reply("⏳ Memproses...")

    try:
        plan, total_contacts, conflicts, invalid_count = plan_outputs(
            src_folder=in_dir,
            base_file_name=base_file_name,
            per_file=per_file,
            output_dir=out_dir
        )

        if total_contacts == 0:
            await status.edit_text("Tidak ada nomor valid di file yang diunggah.")
            clear_session(msg.from_user.id)
            await state.clear()
            return

        # Tulis VCF
        processed = 0
        pad = len(str(total_contacts))
        for batch, target_path in plan:
            pairs = []
            for num in batch:
                processed += 1
                fullname = f"{contact_name} {str(processed).zfill(pad)}"
                pairs.append((fullname, num))
            write_vcard_batch(target_path, pairs)

        # Kirim hasil
        vcf_files = sorted(out_dir.glob("*.vcf"))
        summary = (
            f"✅ Selesai!\n"
            f"• Total kontak valid: {total_contacts}\n"
            f"• Baris di-skip (invalid): {invalid_count}\n"
            f"• File VCF: {len(vcf_files)}"
        )
        await status.edit_text(summary)
        for fp in vcf_files:
            await msg.answer_document(document=fp.open("rb"), caption=fp.name)

    except Exception as e:
        await status.edit_text(f"❌ Terjadi error: {e}")
    finally:
        clear_session(msg.from_user.id)
        await state.clear()

# ========= Entry Point =========

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN environment variable.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
