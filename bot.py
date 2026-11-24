import os
import io
import time
import uuid
import hashlib
import sqlite3
import discord
from discord import app_commands
import aiohttp

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("❌ Не найдена переменная окружения DISCORD_TOKEN")

# ============ НАСТРОЙКИ ДОСТУПА ============
ALLOWED_USER_IDS = [
    1411647769084563458,  # <- ЗАМЕНИ на свой ID (ПКМ по себе -> Копировать ID)
]
ADMIN_ROLE_ID = 1411647769084563458   # высшая админская роль
ROLE_NAME = "not-bot"                  # допусковая роль для базовых команд
WEBHOOK_NAME = "Bodycam News Bot"

SESSION_TTL = 30 * 60        # сессия после входа живёт 30 минут
MAX_ATTEMPTS = 3             # после 3 неверных паролей бот уходит с сервера

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # нужно для никнеймов, ролей и активности

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

bindings = {}
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MAX_FILE_SIZE = 8 * 1024 * 1024
TRANSLATE_CHUNK = 450

# ============ ЛОКАЛЬНАЯ БАЗА (SQLite) ============
db = sqlite3.connect("bot.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS passwords (
    guild_id INTEGER PRIMARY KEY, pw_hash TEXT, salt TEXT)""")
db.execute("""CREATE TABLE IF NOT EXISTS sessions (
    guild_id INTEGER, user_id INTEGER, until REAL,
    PRIMARY KEY (guild_id, user_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS attempts (
    guild_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS blacklist (
    guild_id INTEGER, user_id INTEGER, reason TEXT, added_by INTEGER, at INTEGER,
    PRIMARY KEY (guild_id, user_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS activity (
    guild_id INTEGER, user_id INTEGER, msgs INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id))""")
db.commit()


def db_run(query, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def db_one(query, params=()):
    return db.execute(query, params).fetchone()


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


# ============ ПРОВЕРКИ ДОСТУПА ============

def is_admin_member(member) -> bool:
    """Высшая админская роль, список ID или роль not-bot."""
    roles = getattr(member, "roles", [])
    if any(r.id == ADMIN_ROLE_ID for r in roles):
        return True
    if getattr(member, "id", 0) in ALLOWED_USER_IDS:
        return True
    return any(r.name.lower() == ROLE_NAME.lower() for r in roles)


def is_logged_in(guild_id: int, user_id: int) -> bool:
    row = db_one("SELECT until FROM sessions WHERE guild_id=? AND user_id=?",
                 (guild_id, user_id))
    return bool(row and row[0] > time.time())


def has_password(guild_id: int) -> bool:
    return db_one("SELECT 1 FROM passwords WHERE guild_id=?", (guild_id,)) is not None


async def deny(interaction: discord.Interaction, need_login: bool = False):
    msg = "🔒 Сначала введите пароль: `/login`." if need_login else "⛔ Нет доступа."
    if not interaction.response.is_done():
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.followup.send(msg, ephemeral=True)


async def admin_gate(interaction: discord.Interaction) -> bool:
    """Проверка: админ + пароль введён (если пароль на сервере установлен)."""
    if not is_admin_member(interaction.user):
        await deny(interaction)
        return False
    if has_password(interaction.guild_id) and not is_logged_in(interaction.guild_id, interaction.user.id):
        await deny(interaction, need_login=True)
        return False
    return True


# ============ ПЕРЕВОДЧИК ============

def split_for_translation(text, limit=TRANSLATE_CHUNK):
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def is_bad_translation(result: str) -> bool:
    if not result:
        return True
    return any(m in result.upper() for m in ("QUERY LENGTH", "LIMIT EXCEEDED", "MYMEMORY WARNING"))


async def google_translate(text: str, dest: str = "ru"):
    if not text or not text.strip():
        return None
    params = {"client": "gtx", "sl": "auto", "tl": dest, "dt": "t", "q": text}
    timeout = aiohttp.ClientTimeout(total=15)
    for _ in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(GOOGLE_URL, params=params) as r:
                    if r.status == 200:
                        data = await r.json()
                        result = "".join(p[0] for p in data[0] if p and p[0])
                        if result.strip() and not is_bad_translation(result):
                            return result
        except Exception as e:
            print(f"⚠️ Google: {e}")
    return None


async def mymemory_translate(text: str, dest: str = "ru"):
    if not text or not text.strip():
        return None
    results = []
    for chunk in split_for_translation(text, 450):
        params = {"q": chunk, "langpair": f"en|{dest}"}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(MYMEMORY_URL, params=params) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
            piece = data.get("responseData", {}).get("translatedText")
            if not piece or is_bad_translation(piece):
                return None
            results.append(piece)
        except Exception as e:
            print(f"⚠️ MyMemory: {e}")
            return None
    return " ".join(results)


async def translate_text(text: str, dest: str = "ru"):
    if not text or not text.strip():
        return None
    out = []
    for chunk in split_for_translation(text):
        result = await google_translate(chunk, dest)
        if result is None:
            result = await mymemory_translate(chunk, dest)
        if result is None:
            return None
        out.append(result)
    return " ".join(out).strip()


def extract_message_payload(message: discord.Message):
    text = message.content or ""
    embeds = list(message.embeds)
    attachments = list(message.attachments)
    if message.message_snapshots:
        snap = message.message_snapshots[0].message
        if not text.strip():
            text = snap.content or ""
        if not embeds:
            embeds = list(snap.embeds)
        if not attachments:
            attachments = list(snap.attachments)
    return text, embeds, attachments


def chunk_text(text, limit=1900):
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut + 1:]
    if text:
        parts.append(text)
    return parts


async def get_webhook(channel):
    try:
        for wh in await channel.webhooks():
            if wh.user == bot.user:
                return wh
        return await channel.create_webhook(name=WEBHOOK_NAME)
    except discord.Forbidden:
        print(f"❌ Нет прав на вебхук в #{channel.name}")
        return None


async def send_as_webhook(channel, content=None, files=None, embeds=None,
                          username=WEBHOOK_NAME, avatar_url=None):
    wh = await get_webhook(channel)
    if wh is not None:
        await wh.send(content=content, files=files, embeds=embeds,
                      username=username, avatar_url=avatar_url)
    else:
        await channel.send(content=content, files=files, embeds=embeds)


# ============ ВХОД ПО ПАРОЛЮ ============

class LoginModal(discord.ui.Modal, title="Вход по паролю"):
    password = discord.ui.TextInput(label="Пароль", required=True,
                                    style=discord.TextStyle.short, max_length=64)

    async def on_submit(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        row = db_one("SELECT pw_hash, salt FROM passwords WHERE guild_id=?", (gid,))
        if not row:
            await interaction.response.send_message(
                "ℹ️ Пароль ещё не установлен. Пусть админ использует `/setpassword`.", ephemeral=True)
            return
        pw_hash, salt = row
        if hash_password(str(self.password.value), salt) == pw_hash:
            db_run("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                   (gid, uid, time.time() + SESSION_TTL))
            db_run("DELETE FROM attempts WHERE guild_id=? AND user_id=?", (gid, uid))
            await interaction.response.send_message(
                f"✅ Вход выполнен. Сессия {SESSION_TTL // 60} минут.", ephemeral=True)
        else:
            row2 = db_one("SELECT count FROM attempts WHERE guild_id=? AND user_id=?", (gid, uid))
            count = (row2[0] if row2 else 0) + 1
            db_run("INSERT OR REPLACE INTO attempts VALUES (?,?,?)", (gid, uid, count))
            left = MAX_ATTEMPTS - count
            if count >= MAX_ATTEMPTS:
                await interaction.response.send_message(
                    "🚪 Слишком много неверных попыток. Бот покидает сервер.", ephemeral=True)
                await interaction.guild.leave()
            else:
                await interaction.response.send_message(
                    f"❌ Неверный пароль. Осталось попыток: {left}. "
                    f"После {MAX_ATTEMPTS} бот уйдёт с сервера.", ephemeral=True)


@tree.command(name="login", description="Вход по паролю для доступа к функциям бота")
async def login(interaction: discord.Interaction):
    if not has_password(interaction.guild_id):
        await interaction.response.send_message(
            "ℹ️ Пароль не установлен. Админ может задать его через `/setpassword`.", ephemeral=True)
        return
    await interaction.response.send_modal(LoginModal())


@tree.command(name="setpassword", description="Установить пароль (только высшая админка)")
@app_commands.describe(password="Новый пароль (4-64 символа)")
async def setpassword(interaction: discord.Interaction, password: str):
    if not is_admin_member(interaction.user):
        await deny(interaction)
        return
    if not (4 <= len(password) <= 64):
        await interaction.response.send_message("❌ Пароль от 4 до 64 символов.", ephemeral=True)
        return
    salt = uuid.uuid4().hex
    db_run("INSERT OR REPLACE INTO passwords VALUES (?,?,?)",
           (interaction.guild_id, hash_password(password, salt), salt))
    db_run("DELETE FROM sessions WHERE guild_id=?", (interaction.guild_id,))
    await interaction.response.send_message(
        "✅ Пароль установлен. Всем (включая админов) нужно войти заново через `/login`.", ephemeral=True)


@tree.command(name="logout", description="Выйти из сессии")
async def logout(interaction: discord.Interaction):
    db_run("DELETE FROM sessions WHERE guild_id=? AND user_id=?",
           (interaction.guild_id, interaction.user.id))
    await interaction.response.send_message("👋 Сессия закрыта.", ephemeral=True)


# ============ АДМИН-ФУНКЦИИ ============

@tree.command(name="blacklist_add", description="[админ] Внести в ЧС сервера")
@app_commands.describe(user="Кого в ЧС", reason="Причина")
async def blacklist_add(interaction: discord.Interaction, user: discord.Member, reason: str = "без причины"):
    if not await admin_gate(interaction):
        return
    if user.bot:
        await interaction.response.send_message("❌ Ботов в ЧС не добавляем.", ephemeral=True)
        return
    if user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Администратора в ЧС не внести.", ephemeral=True)
        return
    db_run("INSERT OR REPLACE INTO blacklist VALUES (?,?,?,?,?)",
           (interaction.guild_id, user.id, reason, interaction.user.id, int(time.time())))
    await interaction.response.send_message(
        f"🚫 {user.mention} внесён в ЧС. Причина: {reason}", ephemeral=True)


@tree.command(name="blacklist_remove", description="[админ] Убрать из ЧС сервера")
@app_commands.describe(user="Кого убрать из ЧС")
async def blacklist_remove(interaction: discord.Interaction, user: discord.User):
    if not await admin_gate(interaction):
        return
    db_run("DELETE FROM blacklist WHERE guild_id=? AND user_id=?",
           (interaction.guild_id, user.id))
    await interaction.response.send_message(f"✅ {user.mention} убран из ЧС.", ephemeral=True)


@tree.command(name="blacklist_list", description="[админ] Показать ЧС сервера")
async def blacklist_list(interaction: discord.Interaction):
    if not await admin_gate(interaction):
        return
    rows = db.execute("SELECT user_id, reason FROM blacklist WHERE guild_id=? ORDER BY at DESC",
                      (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("📭 ЧС пуст.", ephemeral=True)
        return
    lines = [f"• <@{uid}> — {reason}" for uid, reason in rows[:20]]
    await interaction.response.send_message("**🚫 Чёрный список:**\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="giverole", description="[админ] Выдать роль участнику")
@app_commands.describe(user="Кому выдать", role="Какую роль")
async def giverole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if not await admin_gate(interaction):
        return
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ Эта роль выше роли бота.", ephemeral=True)
        return
    try:
        await user.add_roles(role, reason=f"/giverole от {interaction.user}")
        await interaction.response.send_message(f"✅ Роль {role.mention} выдана {user.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не хватает прав (роль бота ниже или нет Manage Roles).", ephemeral=True)


@tree.command(name="removerole", description="[админ] Снять роль с участника")
@app_commands.describe(user="У кого снять", role="Какую роль")
async def removerole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if not await admin_gate(interaction):
        return
    try:
        await user.remove_roles(role, reason=f"/removerole от {interaction.user}")
        await interaction.response.send_message(f"✅ Роль {role.mention} снята с {user.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не хватает прав.", ephemeral=True)


@tree.command(name="nickname", description="[админ] Сменить никнейм участнику")
@app_commands.describe(user="Кому менять", nick="Новый никнейм (пусто — сбросить)")
async def nickname(interaction: discord.Interaction, user: discord.Member, nick: str = ""):
    if not await admin_gate(interaction):
        return
    try:
        await user.edit(nick=nick or None, reason=f"/nickname от {interaction.user}")
        await interaction.response.send_message(
            f"✅ Никнейм {user.mention} изменён на `{nick or user.name}`.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не хватает прав (роль пользователя выше бота).", ephemeral=True)


PERM_CHOICES = [
    app_commands.Choice(name="view (видеть канал)", value="view"),
    app_commands.Choice(name="send (писать)", value="send"),
    app_commands.Choice(name="read_history (читать историю)", value="read_history"),
]


@tree.command(name="perm", description="[админ] Изменить разрешения участника в канале")
@app_commands.describe(
    user="Кому менять права",
    channel="В каком канале (пусто — в текущем)",
    action="allow / deny / neutral",
    permission="Какое право",
)
@app_commands.choices(action=[
    app_commands.Choice(name="allow (разрешить)", value="allow"),
    app_commands.Choice(name="deny (запретить)", value="deny"),
    app_commands.Choice(name="neutral (сбросить на наследование)", value="neutral"),
], permission=PERM_CHOICES)
async def perm(interaction: discord.Interaction, user: discord.Member,
               action: app_commands.Choice[str], permission: app_commands.Choice[str],
               channel: discord.TextChannel = None):
    if not await admin_gate(interaction):
        return
    ch = channel or interaction.channel
    flag_map = {
        "view": "view_channel",
        "send": "send_messages",
        "read_history": "read_message_history",
    }
    val = {"allow": True, "deny": False, "neutral": None}[action.value]
    try:
        kw = {flag_map[permission.value]: val}
        await ch.set_permissions(user, reason=f"/perm от {interaction.user}", **kw)
        await interaction.response.send_message(
            f"✅ В {ch.mention} для {user.mention}: {permission.name} → {action.name}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не хватает прав менять разрешения.", ephemeral=True)


@tree.command(name="activity", description="[админ] Активность: топ-10 болтунов сервера")
async def activity(interaction: discord.Interaction):
    if not await admin_gate(interaction):
        return
    rows = db.execute(
        "SELECT user_id, msgs FROM activity WHERE guild_id=? ORDER BY msgs DESC LIMIT 10",
        (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("📭 Пока никто ничего не написал.", ephemeral=True)
        return
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"{medals[i]} <@{uid}> — {msgs} сообщ." for i, (uid, msgs) in enumerate(rows)]
    await interaction.response.send_message("**📊 Топ активности:**\n" + "\n".join(lines), ephemeral=True)


# ============ БАЗОВЫЕ КОМАНДЫ (тоже за паролем) ============

class TargetSelect(discord.ui.ChannelSelect):
    def __init__(self, view):
        super().__init__(placeholder="Канал-приёмник", channel_types=[discord.ChannelType.text])
        self.parent_view = view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.target = self.values[0]
        bindings[interaction.guild_id] = {
            "source": self.parent_view.source.id,
            "target": self.parent_view.target.id,
        }
        await interaction.response.edit_message(
            content=(f"✅ Готово!\n**Source:** <#{self.parent_view.source.id}>\n"
                     f"**Target:** <#{self.parent_view.target.id}>"),
            view=None)


class SourceSelect(discord.ui.ChannelSelect):
    def __init__(self, view):
        super().__init__(placeholder="Канал-источник", channel_types=[discord.ChannelType.text])
        self.parent_view = view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.source = self.values[0]
        self.parent_view.remove_item(self)
        self.parent_view.add_item(TargetSelect(self.parent_view))
        await interaction.response.edit_message(content="Теперь выбери канал-приёмник:",
                                                view=self.parent_view)


class BindView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.source = None
        self.target = None
        self.add_item(SourceSelect(self))


@tree.command(name="bind", description="Привязать source и target каналы")
async def bind(interaction: discord.Interaction):
    if not await admin_gate(interaction):
        return
    await interaction.response.send_message("Выбери канал-источник:", view=BindView(), ephemeral=True)


@tree.command(name="check", description="Проверить бота, каналы, права и переводчик")
async def check(interaction: discord.Interaction):
    if not is_admin_member(interaction.user):
        await deny(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    lines = ["✅ Бот работает"]
    lines.append("✅ Пароль введён" if is_logged_in(interaction.guild_id, interaction.user.id)
                 else "🔒 Пароль не введён — админ-функции закрыты")
    b = bindings.get(interaction.guild_id)
    if not b:
        lines.append("❌ Привязка не настроена — /bind")
    else:
        me = interaction.guild.me
        src = interaction.guild.get_channel(b["source"])
        tgt = interaction.guild.get_channel(b["target"])
        if src:
            p1 = src.permissions_for(me)
            lines.append(f"{'✅' if p1.read_messages else '❌'} Source <#{src.id}>: читать={p1.read_messages}")
        if tgt:
            p2 = tgt.permissions_for(me)
            lines.append(f"{'✅' if p2.manage_webhooks else '❌'} Target <#{tgt.id}>: вебхуки={p2.manage_webhooks}")
    test = await translate_text("hello world. " * 40)
    lines.append("✅ Переводчик работает" if test else "❌ Переводчик недоступен")
    await interaction.followup.send("\n".join(lines))


class SayChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Куда отправить", channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SayModal(self.values[0]))


class SayModal(discord.ui.Modal, title="Сообщение от имени бота"):
    author_name = discord.ui.TextInput(label="Имя отправителя", required=False,
                                       max_length=60, default=WEBHOOK_NAME)
    avatar_url = discord.ui.TextInput(label="Ссылка на аватарку (необязательно)",
                                      required=False, max_length=300)
    embed_title = discord.ui.TextInput(label="Заголовок (тема)", required=False, max_length=256)
    message_text = discord.ui.TextInput(label="Текст сообщения", style=discord.TextStyle.long,
                                        required=True, max_length=1800)
    embed_color = discord.ui.TextInput(label="Цвет (HEX или red/green/blue...)",
                                       default="#FF5500", required=False, max_length=20)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        color_map = {"red": discord.Color.red(), "green": discord.Color.green(),
                     "blue": discord.Color.blue(), "yellow": discord.Color.gold(),
                     "purple": discord.Color.purple(), "orange": discord.Color.orange(),
                     "pink": discord.Color.magenta(), "black": discord.Color.dark_grey()}
        color = color_map.get(self.embed_color.value.strip().lower(), discord.Color.orange())
        raw = self.embed_color.value.strip()
        if raw.startswith("#") and len(raw) == 7:
            try:
                color = discord.Color(int(raw[1:], 16))
            except ValueError:
                pass
        embed = discord.Embed(title=self.embed_title.value or None,
                              description=self.message_text.value, color=color)
        embed.set_footer(text=f"Отправлено: {interaction.user.display_name}")
        username = self.author_name.value.strip() or WEBHOOK_NAME
        avatar = self.avatar_url.value.strip() or None
        try:
            await send_as_webhook(self.channel, embeds=[embed],
                                  username=username, avatar_url=avatar)
            await interaction.followup.send(
                f"✅ Отправлено в <#{self.channel.id}> от имени **{username}**")
        except discord.Forbidden:
            await interaction.followup.send(f"❌ Нет прав писать в <#{self.channel.id}>")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}")


class SayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SayChannelSelect())


@tree.command(name="say", description="Отправить сообщение вебхуком в выбранный канал")
async def say(interaction: discord.Interaction):
    if not await admin_gate(interaction):
        return
    await interaction.response.send_message("Выбери канал:", view=SayView(), ephemeral=True)


@tree.command(name="translate", description="Перевести текст на русский")
@app_commands.describe(text="Текст для перевода")
async def translate(interaction: discord.Interaction, text: str):
    if not await admin_gate(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    result = await translate_text(text)
    if result is None:
        await interaction.followup.send("❌ Переводчик недоступен — оба сервиса не ответили")
    else:
        await interaction.followup.send(
            f"**Оригинал:**\n{text[:1500]}\n\n**Перевод:**\n{result[:1500]}")


# ============ ПРИЁМ НОВОСТЕЙ + АКТИВНОСТЬ + ЧС ============

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id or message.guild is None:
        return
    gid = message.guild.id

    # статистика активности
    if not message.author.bot:
        db_run("INSERT INTO activity VALUES (?,?,1) "
               "ON CONFLICT(guild_id, user_id) DO UPDATE SET msgs = msgs + 1",
               (gid, message.author.id))

    b = bindings.get(gid)
    if not b or message.channel.id != b["source"]:
        return

    # ЧС: сообщения от забаненных не пересылаем
    if db_one("SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
              (gid, message.author.id)):
        return

    if message.author.bot and message.webhook_id is None:
        return

    target = message.guild.get_channel(b["target"])
    if target is None:
        return

    text, embeds, attachments = extract_message_payload(message)

    translated = await translate_text(text) if text.strip() else None

    new_embeds = []
    for e in embeds[:10]:
        ne = discord.Embed()
        if e.title:
            ne.title = await translate_text(e.title) or e.title
        if e.description:
            ne.description = await translate_text(e.description) or e.description
        for f in e.fields[:10]:
            ne.add_field(name=await translate_text(f.name) or f.name,
                         value=await translate_text(f.value) or f.value, inline=f.inline)
        if e.footer and e.footer.text:
            ne.set_footer(text=await translate_text(e.footer.text) or e.footer.text)
        if e.image and e.image.url:
            ne.set_image(url=e.image.url)
        if e.thumbnail and e.thumbnail.url:
            ne.set_thumbnail(url=e.thumbnail.url)
        if e.url:
            ne.url = e.url
        if e.color:
            ne.color = e.color
        new_embeds.append(ne)

    files, failed_urls = [], []
    for att in attachments[:5]:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(att.url) as r:
                    if r.status != 200:
                        failed_urls.append(att.url)
                        continue
                    data = await r.read()
            if len(data) > MAX_FILE_SIZE:
                failed_urls.append(att.url)
                continue
            fname = att.filename or "file.png"
            files.append(discord.File(io.BytesIO(data), filename=fname))
        except Exception:
            failed_urls.append(att.url)

    try:
        if translated:
            parts = chunk_text(translated)
            await send_as_webhook(target, content=parts.pop(0), files=files or None)
            for part in parts:
                await send_as_webhook(target, content=part)
        elif text.strip():
            parts = chunk_text("🌐 *(переводчик недоступен)*\n" + text)
            await send_as_webhook(target, content=parts.pop(0), files=files or None)
            for part in parts:
                await send_as_webhook(target, content=part)
        elif files:
            await send_as_webhook(target, files=files)
        if new_embeds:
            await send_as_webhook(target, embeds=new_embeds)
        if failed_urls:
            await send_as_webhook(target, content="📎 " + "\n".join(failed_urls))
    except discord.Forbidden:
        print(f"❌ Нет прав писать в #{target.name}")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Бот запущен как {bot.user} | серверов: {len(bot.guilds)}")


bot.run(DISCORD_TOKEN)
