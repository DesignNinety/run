# BotWedID.py
import os
import io
import re
import asyncio
from datetime import datetime
from typing import Optional, List

import aiohttp
import discord
from discord.ext import commands
import json

# ================== LOAD CONFIG ==================
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = config["COMMAND_PREFIX"]
API_URL = config["API_URL"]

DEFAULT_D = config["DEFAULT_D"]
MAX_DISCORD_FILE_MB = config["MAX_DISCORD_FILE_MB"]
HISTORY_FILE = config["HISTORY_FILE"]

OWNER_USER_ID = config["OWNER_USER_ID"]

ALLOWED_CHANNEL_IDS = set(config["ALLOWED_CHANNEL_IDS"])
HISTORY_CHANNEL_ID = config["HISTORY_CHANNEL_ID"]
LOG_CHANNEL_ID = config["LOG_CHANNEL_ID"]

BLOCKED_KEYWORDS = config["BLOCKED_KEYWORDS"]
ALLOWED_ROLE_IDS = set(config["ALLOWED_ROLE_IDS"])

# ================== BOT SETUP ==================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
session: Optional[aiohttp.ClientSession] = None
panel_sent = False  

@bot.event
async def on_ready():
    global session, panel_sent

    if session is None or session.closed:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=600)
        )

    print(f"✅ Logged in as {bot.user}")

    if not panel_sent:
        channel = bot.get_channel(HISTORY_CHANNEL_ID)

        if channel is None:
            channel = await bot.fetch_channel(HISTORY_CHANNEL_ID)

        if channel:
            await send_panel(channel)
            panel_sent = True

# ================== UTIL ==================
def has_permission(user: discord.Member):
    # Owner ใช้ได้เสมอ
    if user.id == OWNER_USER_ID:
        return True

    # ถ้าไม่มี role กำหนด = เปิดให้ทุกคน
    if not ALLOWED_ROLE_IDS:
        return True

    return any(role.id in ALLOWED_ROLE_IDS for role in user.roles)
def safe_filename(text: str, max_len: int = 80) -> str:
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"[^a-zA-Z0-9._-]", "_", text)
    return text[:max_len]

def split_bytes(data: bytes, filename: str) -> List[discord.File]:
    max_bytes = MAX_DISCORD_FILE_MB * 1024 * 1024
    if len(data) <= max_bytes:
        return [discord.File(io.BytesIO(data), filename=filename)]

    files = []
    for i in range(0, len(data), max_bytes):
        part = i // max_bytes + 1
        files.append(
            discord.File(
                io.BytesIO(data[i:i + max_bytes]),
                filename=f"{filename.replace('.txt','')}_part{part}.txt"
            )
        )
    return files

async def safe_send(ctx, **kwargs):
    if isinstance(ctx, discord.Interaction):
        return await ctx.followup.send(ephemeral=True, **kwargs)
    return await ctx.send(**kwargs)

def save_history(user, keyword, count, d, limit):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
            f"{user} ({user.id}) | {keyword} | {count} | d={d} limit={limit}\n"
        )

async def send_log(user, keyword, count, case_id, files, limit):
    channel = bot.get_channel(LOG_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except:
            return

    now_time = datetime.now().strftime("%d/%m/%Y %H:%M")

    log_embed = discord.Embed(
        title="📁 System Activity Log",
        color=discord.Color.from_rgb(150, 0, 0)
    )

    log_embed.add_field(
        name="ผู้ใช้งาน (User)",
        value=f"{user.display_name} ({user.id})",
        inline=False
    )

    log_embed.add_field(
        name="คำค้นหา (Keyword)",
        value=f"`{keyword}`",
        inline=True
    )

    log_embed.add_field(
        name="จำนวนข้อมูล (Records)",
        value=f"{count:,}",
        inline=True
    )

    log_embed.add_field(
        name="Result Limit",
        value=f"{limit if limit else 'ALL'}",
        inline=True
    )

    log_embed.add_field(
        name="Case ID",
        value=case_id,
        inline=False
    )

    #  ถ้าเกิน 500  เตือน
    mention_text = None

    if limit and int(limit) > 500:
        log_embed.color = discord.Color.red()

        log_embed.add_field(
            name="<a:qu:1478349699412136160> แจ้งเตือน",
            value="มีผู้ใช้ตั้งค่า Result Limit เกิน 500",
            inline=False
        )

        mention_text = f"<@{OWNER_USER_ID}> มีผู้ใช้ตั้งค่า Result Limit เกิน 500"

    log_embed.set_footer(text=f"Timestamp • {now_time}")

    await channel.send(content=mention_text, embed=log_embed, files=files)
# ================== API ==================
async def api_dump(keyword: str, d: int, limit: Optional[int]):
    assert session is not None

    params = {
        "q": keyword,
        "t": d,
        "mode": "clean",
        "fetch": "all",
        "out": "json",
    }
    if limit:
        params["limit"] = limit

    async with session.get(API_URL, params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"API HTTP {r.status}")
        return await r.json(content_type=None)

# ================== SEARCH CORE ==================
async def do_api_search(ctx, keyword: str, d: int, limit: Optional[int]):

    user = ctx.user if isinstance(ctx, discord.Interaction) else ctx.author

    if not has_permission(user):
        return await safe_send(ctx, content="<a:qu:1478349699412136160> คุณไม่มีสิทธิ์ใช้งานระบบนี้")

    if any(b in keyword.lower() for b in BLOCKED_KEYWORDS):
        return await safe_send(ctx, content="<a:qu:1478349699412136160> คำค้นหานี้ถูกระงับการใช้งาน")

    # ===== แสดงสถานะกำลังประมวลผล =====
    processing_embed = discord.Embed(
        title="<a:9754_Loading:1478349634391773318> อยู่ระหว่างดำเนินการ (Processing Request)",
        description=(
            f"คำค้นหา (Target): `{keyword}`\n"
            "สถานะ: กำลังตรวจสอบฐานข้อมูล...\n"
            "Status: Scanning secure database..."
        ),
        color=discord.Color.from_rgb(40, 120, 255)
    )

    msg = await safe_send(ctx, embed=processing_embed)

    try:
        js = await api_dump(keyword, d, limit)

        if js.get("status") != "success":
            raise RuntimeError(js.get("message", "API Error"))

        # ===== รวมผลลัพธ์ =====
        results = []
        for rows in js.get("data", {}).values():
            for r in rows:
                if "url" in r:
                    results.append(f"{r['url']}:{r['username']}:{r['password']}")
                else:
                    results.append(f"{r['username']}:{r['password']}")

        case_id = f"WIS-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # ===== สร้างไฟล์ =====
        data = "\n".join(results).encode("utf-8")

        # สร้าง 2 ชุด (ห้ามใช้ชุดเดียวกัน)
        files_dm = split_bytes(data, safe_filename(keyword) + ".txt")
        files_log = split_bytes(data, safe_filename(keyword) + ".txt")

        user = ctx.user if isinstance(ctx, discord.Interaction) else ctx.author
        now_time = datetime.now().strftime("%d/%m/%Y %H:%M")

        # =========================================
        # 📩 ส่ง DM พร้อมแนบไฟล์
        # =========================================
        try:
            dm_embed = discord.Embed(
                title="<:A05:1478353979367755778> รายงานผลการตรวจสอบ (Search Report)",
                color=discord.Color.from_rgb(50, 55, 65)
            )

            dm_embed.add_field(
                name="คำค้นหา (Target)",
                value=f"`{keyword}`",
                inline=True
            )

            dm_embed.add_field(
                name="จำนวนข้อมูล (Records Found)",
                value=f"`{len(results):,} รายการ`",
                inline=True
            )

            dm_embed.set_footer(
                text=f"Request by {user.display_name} • {now_time}"
            )

            await user.send(embed=dm_embed, files=files_dm)

        except discord.Forbidden:
            error_embed = discord.Embed(
                title="<a:qu:1478349699412136160> ไม่สามารถส่ง DM ได้ (Delivery Failed)",
                description="กรุณาเปิดรับข้อความส่วนตัวจากสมาชิกเซิร์ฟเวอร์",
                color=discord.Color.red()
            )
            await msg.edit(embed=error_embed)
            return

        # =========================================
        # 💬 แสดงผลในแชท (ไทย + อังกฤษ)
        # =========================================
        chat_embed = discord.Embed(
            title="<:A05:1478353979367755778> ผลการตรวจสอบข้อมูล (Search Result)",
            color=discord.Color.from_rgb(35, 40, 45)
        )

        chat_embed.add_field(
            name="คำค้นหา (Target)",
            value=f"`{keyword}`",
            inline=True
        )

        chat_embed.add_field(
            name="จำนวนข้อมูลที่พบ (Records)",
            value=f"**{len(results):,} รายการ**",
            inline=True
        )

        chat_embed.set_footer(
            text="ระบบดำเนินการส่งทาง DM เรียบร้อยแล้ว • Operation Completed"
        )

        await msg.edit(embed=chat_embed)

        # =========================================
        # 📁 ส่ง Log Channel พร้อมแนบไฟล์
        # =========================================
        await send_log(user, keyword, len(results), case_id, files_log, limit)

        save_history(user, keyword, len(results), d, limit)

    except Exception as e:
        fail_embed = discord.Embed(
            title="<a:qu:1478349699412136160> เกิดข้อผิดพลาดของระบบ (System Error)",
            description=f"`{str(e)}`",
            color=discord.Color.red()
        )
        await msg.edit(embed=fail_embed)
# ================== UI ==================

class LogModal(discord.ui.Modal, title="🔍 Secure Data Query Interface"):

    keyword = discord.ui.TextInput(
        label="Target Keyword",
        placeholder="กรอกโดเมน / เว็บไซต์ / คีย์เวิร์ดที่ต้องการตรวจสอบ",
        required=True,
        max_length=120
    )

    d = discord.ui.TextInput(
        label="Query Mode (0 or 1)",
        placeholder="0 = user:pass | 1 = URL:user:pass",
        required=True,
        max_length=1
    )

    limit = discord.ui.TextInput(
        label="Result Limit",
        placeholder="จำนวนข้อมูล 1-500 (หากใส่เกินแบนทุกกรณี)",
        required=True,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        d_value = int(self.d.value) if self.d.value.isdigit() else DEFAULT_D
        limit_value = int(self.limit.value) if self.limit.value.isdigit() else None

        await do_api_search(
            interaction,
            self.keyword.value,
            d_value,
            limit_value
        )


class MainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ปุ่มค้นหา (แดง)
    @discord.ui.button(
        label="ค้นหา",
        emoji="<a:HGWA_98:1478353398049935420>",
        style=discord.ButtonStyle.danger,
        custom_id="open_search_modal"
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(LogModal())
        except Exception as e:
            print("Modal error:", e)
            await interaction.response.send_message(
                "เกิดข้อผิดพลาดในการเปิดฟอร์ม",
                ephemeral=True
            )

    # ปุ่มวิธีใช้ (สีเทา)
    @discord.ui.button(
        label="วิธีการใช้งาน",
        emoji="<:1325question:1478749230788251743>",
        style=discord.ButtonStyle.secondary,
        custom_id="how_to_use"
    )
    async def howto(self, interaction: discord.Interaction, button: discord.ui.Button):

        embed = discord.Embed(
            title="<:1325question:1478749230788251743> วิธีการใช้งานระบบ",
            color=discord.Color.dark_gray()
        )

        embed.description = (
            "<:one:1478750126741655756> `กดปุ่ม ค้นหา`\n"
            "<:two:1478750158765293659> `ใส่ Keyword (เช่น example.com)`\n"
            "<:three:1478750193578151987> `เลือกโหมด (0=user:pass / 1=URL:user:pass)`\n"
            "<:four:1478750228931936346> `กำหนดจำนวน Result`\n"
            "<:five:1478750259080462376> `ระบบจะส่งผลลัพธ์ไปที่ DM`n\n"
            "𓋰  กรุณาเปิดรับข้อความส่วนตัวจากบอท"
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
# ================== COMMAND ==================
@bot.command()
async def panel(ctx):
    if ALLOWED_CHANNEL_IDS and ctx.channel.id not in ALLOWED_CHANNEL_IDS:
        return await ctx.send("<a:qu:1478349699412136160> ใช้ไม่ได้ในห้องนี้")

    await send_panel(ctx.channel)

async def send_panel(channel):
    embed = discord.Embed(
        title="<a:HGWS_85:1478349227603005510> **𝘞𝘦𝘣𝘴𝘪𝘵𝘦 𝘐𝘯𝘵𝘦𝘭𝘭𝘪𝘨𝘦𝘯𝘤𝘦 𝘚𝘺𝘴𝘵𝘦𝘮**",
        color=discord.Color.from_rgb(139, 0, 0)
    )


    embed.add_field(
        name="**รายละเอียดการให้บริการ**",
        value=(
            "<a:HGWS_90:1478349167175798884> ﹐ `ข้อมูลทั้งหมดถูกจัดเก็บภายใต้มาตรฐานความปลอดภัย`\n"
            "<a:HGWS_90:1478349167175798884> ﹐ `ผลการประมวลผลจะถูกจัดส่งผ่านข้อความส่วนตัว (Direct Message)`\n"
            "<a:HGWS_90:1478349167175798884> ﹐ `ข้อมูลทุกอย่างจะถูกเก็บเป็นความลับ`"
        ),
        inline=False
    )

    embed.set_footer(text="")

    await channel.send(embed=embed, view=MainView())

# ================== START ==================
async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("ไม่พบ DISCORD_TOKEN")

    async with bot:
        bot.add_view(MainView())
        await bot.start(DISCORD_TOKEN)
if __name__ == "__main__":
    asyncio.run(main())