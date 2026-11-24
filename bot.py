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

# ============ НАСТРОЙКИ ============
ALLOWED_USER_IDS = [
    123456789012345678,  # <- ЗАМЕНИ на свой ID (ПКМ по себе -> Копировать ID)
]
ADMIN_ROLE_ID = 1411647769084563458      # высшая админская роль
BLACKLIST_ROLE_ID = 1573933972260524113  # роль ЧС сервера
MUTED_ROLE_NAME = "ЧС Мут"
LOG_CHANNEL_NAME = "bot-логи"

SESSION_TTL = 30 * 60    # сессия после входа
MAX_ATTEMPTS = 3         # попыток ввода пароля
COOLDOWN = 30 * 60       # карантин после 3 неверных попыток
MAX_WARNS = 3            # 3/3 выговора = авто-ЧС

FEATURES = ["warn", "unwarn", "blacklist", "blacklist_remove",
            "giverole", "removerole", "nickname", "perm",
            "say", "bind", "activity", "actions"]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

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
    cooldown_until REAL, PRIMARY KEY (guild_id, user_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS blacklist (
    guild_id INTEGER, user_id INTEGER, reason TEXT, added_by INTEGER, at INTEGER,
    PRIMARY KEY (guild_id, user_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS activity (
    guild_id INTEGER, user_id INTEGER, msgs INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, actor_id INTEGER, action TEXT,
    target_id INTEGER, details TEXT, at INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, user_id INTEGER, reason TEXT,
    actor_id INTEGER, at INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS config (
    guild_id INTEGER, key TEXT, value TEXT,
    PRIMARY KEY (guild_id, key))""")
try:
    db.execute("ALTER TABLE attempts ADD COLUMN cooldown_until REAL")
except sqlite3.OperationalError:
    pass
db.commit()


def db_run(query, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def db_one(query, params=()):
    return db.execute(query, params).fetchone()


def cfg_get(guild_id: int, key: str):
    row = db_one("SELECT value FROM config WHERE guild_id=? AND key=?", (guild_id, key))
    return row[0] if row else None


def cfg_set(guild_id: int, key: str, value: str):
    db_run("INSERT OR REPLACE INTO config VALUES (?,?,?)", (guild_id, key, value))


def log_action(guild_id, actor_id, action, target_id, details):
    db_run("INSERT INTO actions (guild_id, actor_id, action, target_id, details, at) "
           "VALUES (?,?,?,?,?,?)",
           (guild_id, actor_id, action, target_id, details, int(time.time())))


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


async def dm_user(user, text: str):
    try:
        await user.send(text)
    except Exception:
        pass


# ============ ЛОГ-КАНАЛ ============

async def get_log_channel(guild: discord.Guild):
    cid = cfg_get(guild.id, "log_channel_id")
    if cid:
        ch = guild.get_channel(int(cid))
        if ch:
            return ch
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True)
    try:
        ch = await guild.create_text_channel(
            LOG_CHANNEL_NAME, overwrites=overwrites,
            reason="Приватный канал логирования систем бота")
        cfg_set(guild.id, "log_channel_id", str(ch.id))
        return ch
    except discord.Forbidden:
        print("❌ Нет прав создать лог-канал")
        return None


async def log_event(guild: discord.Guild, text: str):
    log_action(guild.id if guild else 0, 0, "log", 0, text)
    ch = await get_log_channel(guild) if guild else None
    if ch:
        try:
            await ch.send(text[:1900])
        except Exception:
            pass


# ============ ПРОВЕРКИ ДОСТУПА ============

def is_admin_member(member) -> bool:
    roles = getattr(member, "roles", [])
    if any(r.id == ADMIN_ROLE_ID for r in roles):
        return True
    return getattr(member, "id", 0) in ALLOWED_USER_IDS


def has_feature(member, guild_id: int, feature: str) -> bool:
    if is_admin_member(member):
        return True
    val = cfg_get(guild_id, f"perm:{feature}")
    if not val:
        return False
    role_ids = {int(x) for x in val.split(",") if x.strip()}
    return any(r.id in role_ids for r in getattr(member, "roles", []))


def is_logged_in(guild_id: int, user_id: int) -> bool:
    row = db_one("SELECT until FROM sessions WHERE guild_id=? AND user_id=?",
                 (guild_id, user_id))
    return bool(row and row[0] > time.time())


def has_password(guild_id: int) -> bool:
    return db_one("SELECT 1 FROM passwords WHERE guild_id=?", (guild_id,)) is not None


async def deny(interaction, msg: str):
    if not interaction.response.is_done():
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.followup.send(msg, ephemeral=True)


async def admin_gate(interaction, need_login: bool = True) -> bool:
    """Админ-команда: роль из настроек + введённый пароль."""
    feature = interaction.command.name
    if not has_feature(interaction.user, interaction.guild_id, feature):
        await deny(interaction, "⛔ Нет доступа к этой функции.")
        return False
    if need_login and has_password(interaction.guild_id) and \
       not is_logged_in(interaction.guild_id, interaction.user.id):
        await deny(interaction, "🔒 Сначала войдите по паролю: `/login`.")
        return False
    return True


def cooldown_until(guild_id: int, user_id: int):
    """Вернуть время до конца карантина или None."""
    row = db_one("SELECT count, cooldown_until FROM attempts WHERE guild_id=? AND user_id=?",
                 (guild_id, user_id))
    if not row:
        return None
    count, until = row
    if until and until > time.time():
        return until
    if until:  # карантин истёк — обнуляем попытки
        db_run("UPDATE attempts SET count=0, cooldown_until=NULL WHERE guild_id=? AND user_id=?",
               (guild_id, user_id))
    return None


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


async def download_file(url: str, filename: str):
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        if len(data) > MAX_FILE_SIZE:
            return None
        return discord.File(io.BytesIO(data), filename=filename or "file.png")
    except Exception as e:
        print(f"⚠️ Не удалось скачать файл: {e}")
        return None


# ============ РОЛИ ЧС И МУТА ============

async def get_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
    if role is not None:
        return role
    perms = discord.Permissions.none()
    perms.send_messages = False
    perms.add_reactions = False
    perms.speak = False
    perms.create_public_threads = False
    perms.create_private_threads = False
    perms.send_messages_in_threads = False
    role = await guild.create_role(name=MUTED_ROLE_NAME, permissions=perms,
                                   reason="Автосоздание роли мута для ЧС")
    for ch in guild.channels:
        try:
            await ch.set_permissions(role, send_messages=False, speak=False,
                                     add_reactions=False, reason="Мут для ЧС")
        except discord.Forbidden:
            pass
    return role


async def apply_blacklist_roles(guild: discord.Guild, member: discord.Member, reason: str):
    bl = guild.get_role(BLACKLIST_ROLE_ID)
    muted = await get_muted_role(guild)
    me_top = guild.me.top_role
    to_remove = [r for r in member.roles
                 if r != guild.default_role and r.id != BLACKLIST_ROLE_ID
                 and r.id != muted.id and r < me_top]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason=reason)
        except discord.Forbidden:
            print("❌ Нет прав Manage Roles для снятия ролей")
    to_add = [r for r in (bl, muted) if r is not None and r not in member.roles
              and r < me_top]
    if to_add:
        try:
            await member.add_roles(*to_add, reason=reason)
        except discord.Forbidden:
            print("❌ Нет прав выдать роль ЧС/мут")


def is_blacklisted(guild_id: int, user_id: int) -> bool:
    return db_one("SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
                  (guild_id, user_id)) is not None


async def warn_count(guild_id: int, user_id: int) -> int:
    return db_one("SELECT COUNT(*) FROM warns WHERE guild_id=? AND user_id=?",
                  (guild_id, user_id))[0]


# ============ ВХОД ПО ПАРОЛЮ (с карантином 30 минут) ============

class LoginModal(discord.ui.Modal, title="Вход по паролю"):
    password = discord.ui.TextInput(label="Пароль", required=True,
                                    style=discord.TextStyle.short, max_length=64)

    async def on_submit(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        until = cooldown_until(gid, uid)
        if until:
            await interaction.response.send_message(
                f"⏳ Ты в карантине после неверных попыток. Жди <t:{int(until)}:R>.", ephemeral=True)
            return
        row = db_one("SELECT pw_hash, salt FROM passwords WHERE guild_id=?", (gid,))
        if not row:
            await interaction.response.send_message(
                "ℹ️ Пароль ещё не установлен. Админ использует `/setpassword`.", ephemeral=True)
            return
        pw_hash, salt = row
        if hash_password(str(self.password.value), salt) == pw_hash:
            db_run("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                   (gid, uid, time.time() + SESSION_TTL))
            db_run("DELETE FROM attempts WHERE guild_id=? AND user_id=?", (gid, uid))
            await interaction.response.send_message(
                f"✅ Вход выполнен. Доступ открыт на {SESSION_TTL // 60} минут.", ephemeral=True)
            await log_event(interaction.guild,
                            f"🔑 {interaction.user.mention} вошёл по паролю")
        else:
            row2 = db_one("SELECT count FROM attempts WHERE guild_id=? AND user_id=?", (gid, uid))
            count = (row2[0] if row2 else 0) + 1
            if count >= MAX_ATTEMPTS:
                new_until = time.time() + COOLDOWN
                db_run("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                       (gid, uid, 0, new_until))
                await interaction.response.send_message(
                    f"🚫 {MAX_ATTEMPTS} неверных попыток. Карантин на {COOLDOWN // 60} минут: "
                    f"жди до <t:{int(new_until)}:R>.", ephemeral=True)
                await log_event(interaction.guild,
                                f"🚫 {interaction.user.mention} — карантин {COOLDOWN // 60} мин "
                                f"({MAX_ATTEMPTS} неверных паролей)")
            else:
                db_run("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                       (gid, uid, count, None))
                await interaction.response.send_message(
                    f"❌ Неверный пароль. Осталось попыток: {MAX_ATTEMPTS - count}. "
                    f"После {MAX_ATTEMPTS} — карантин {COOLDOWN // 60} минут.", ephemeral=True)


@tree.command(name="login", description="Вход по паролю (открывает админ-команды)")
async def login(interaction: discord.Interaction):
    until = cooldown_until(interaction.guild_id, interaction.user.id)
    if until:
        await interaction.response.send_message(
            f"⏳ Ты в карантине. Жди <t:{int(until)}:R>.", ephemeral=True)
        return
    if not has_password(interaction.guild_id):
        await interaction.response.send_message(
            "ℹ️ Пароль не установлен. Админ может задать его через `/setpassword`.", ephemeral=True)
        return
    await interaction.response.send_modal(LoginModal())


@tree.command(name="setpassword", description="[админ] Установить пароль")
@app_commands.describe(password="Новый пароль (4-64 символа)")
async def setpassword(interaction: discord.Interaction, password: str):
    if not is_admin_member(interaction.user):
        await deny(interaction, "⛔ Нет доступа.")
        return
    if not (4 <= len(password) <= 64):
        await interaction.response.send_message("❌ Пароль от 4 до 64 символов.", ephemeral=True)
        return
    salt = uuid.uuid4().hex
    db_run("INSERT OR REPLACE INTO passwords VALUES (?,?,?)",
           (interaction.guild_id, hash_password(password, salt), salt))
    db_run("DELETE FROM sessions WHERE guild_id=?", (interaction.guild_id,))
    await interaction.response.send_message(
        "✅ Пароль установлен. Всем нужно войти заново через `/login`.", ephemeral=True)
    await log_event(interaction.guild,
                    f"🔑 Пароль обновлён админом {interaction.user.mention}")


@tree.command(name="logout", description="Выйти из сессии")
async def logout(interaction: discord.Interaction):
    db_run("DELETE FROM sessions WHERE guild_id=? AND user_id=?",
           (interaction.guild_id, interaction.user.id))
    await interaction.response.send_message("👋 Сессия закрыта.", ephemeral=True)


# ============ МЕНЮ НАСТРОЙКИ ДОСТУПОВ ============

class FeatureSelect(discord.ui.Select):
    def __init__(self, parent):
        options = [discord.SelectOption(label=f, value=f) for f in FEATURES]
        super().__init__(placeholder="1️⃣ Выбери функцию", options=options, row=0)
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        self.parent.feature = self.values[0]
        await interaction.response.send_message(
            f"Выбрана функция `{self.values[0]}`. Теперь выбери роль и нажми кнопку.", ephemeral=True)


class RolePick(discord.ui.RoleSelect):
    def __init__(self, parent):
        super().__init__(placeholder="2️⃣ Выбери роль", row=1)
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        self.parent.role = self.values[0]
        await interaction.response.send_message(
            f"Выбрана роль {self.values[0].mention}. Нажми «Дать доступ» или «Забрать».", ephemeral=True)


class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.feature = None
        self.role = None

    @discord.ui.button(label="✅ Дать доступ", style=discord.ButtonStyle.green, row=2)
    async def grant(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.feature or not self.role:
            await interaction.response.send_message("❌ Сначала выбери функцию и роль.", ephemeral=True)
            return
        val = cfg_get(interaction.guild_id, f"perm:{self.feature}")
        ids = {int(x) for x in val.split(",") if x.strip()} if val else set()
        ids.add(self.role.id)
        cfg_set(interaction.guild_id, f"perm:{self.feature}", ",".join(map(str, ids)))
        log_action(interaction.guild_id, interaction.user.id, "setperm", 0,
                   f"{self.feature} → {self.role.name}")
        await interaction.response.edit_message(
            content=f"✅ Роль {self.role.mention} может: `{self.feature}`.",
            view=None)
        await log_event(interaction.guild,
                        f"⚙️ {interaction.user.mention}: роль {self.role.mention} → `{self.feature}`")

    @discord.ui.button(label="❌ Забрать доступ", style=discord.ButtonStyle.red, row=2)
    async def revoke(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.feature or not self.role:
            await interaction.response.send_message("❌ Сначала выбери функцию и роль.", ephemeral=True)
            return
        val = cfg_get(interaction.guild_id, f"perm:{self.feature}")
        if val:
            ids = {int(x) for x in val.split(",") if x.strip()}
            ids.discard(self.role.id)
            cfg_set(interaction.guild_id, f"perm:{self.feature}", ",".join(map(str, ids)))
        await interaction.response.edit_message(
            content=f"✅ Роль {self.role.mention} больше не может: `{self.feature}`.",
            view=None)
        await log_event(interaction.guild,
                        f"⚙️ {interaction.user.mention}: убрал роль {self.role.mention} → `{self.feature}`")


@tree.command(name="setup", description="[админ] Меню настройки: какая роль что может")
async def setup(interaction: discord.Interaction):
    if not is_admin_member(interaction.user):
        await deny(interaction, "⛔ Нет доступа.")
        return
    view = SetupView()
    view.add_item(FeatureSelect(view))
    view.add_item(RolePick(view))
    await interaction.response.send_message(
        "**⚙️ Настройка доступа**\n"
        "1. Выбери функцию из списка.\n"
        "2. Выбери роль.\n"
        "3. Нажми «Дать доступ» или «Забрать».\n"
        "Высшая админка и твой ID имеют доступ ко всему всегда.", view=SetupViewWrap(view), ephemeral=True)


class SetupViewWrap(discord.ui.View):
    def __init__(self, inner):
        super().__init__(timeout=300)
        self.inner = inner

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.inner.interaction_check(interaction)


@tree.command(name="permissions", description="[админ] Кто какие функции имеет")
async def permissions(interaction: discord.Interaction):
    if not is_admin_member(interaction.user):
        await deny(interaction, "⛔ Нет доступа.")
        return
    lines = ["**⚙️ Доступы по функциям:**"]
    for f in FEATURES:
        val = cfg_get(interaction.guild_id, f"perm:{f}")
        if val:
            roles = " ".join(f"<@&{rid}>" for rid in val.split(",") if rid.strip())
            lines.append(f"• `{f}` — {roles} + высшая админка")
        else:
            lines.append(f"• `{f}` — только высшая админка")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="setlogchannel", description="[админ] Создать/привязать приватный канал логов")
async def setlogchannel(interaction: discord.Interaction):
    if not is_admin_member(interaction.user):
        await deny(interaction, "⛔ Нет доступа.")
        return
    await interaction.response.defer(ephemeral=True)
    ch = await get_log_channel(interaction.guild)
    if ch:
        await interaction.followup.send(f"✅ Логи пишутся в {ch.mention}")
    else:
        await interaction.followup.send("❌ Не удалось создать канал — нет прав Manage Channels")


# ============ ЧС СЕРВЕРА ============

@tree.command(name="blacklist_add", description="Внести в ЧС: снять все роли, выдать роль ЧС + мут")
@app_commands.describe(user="Кого в ЧС", reason="Причина")
async def blacklist_add(interaction: discord.Interaction, user: discord.Member,
                        reason: str = "без причины"):
    if not await admin_gate(interaction):
        return
    if user.bot:
        await interaction.response.send_message("❌ Ботов в ЧС не добавляем.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Себя в ЧС не вносить.", ephemeral=True)
        return
    if user.guild_permissions.administrator or user.top_role >= interaction.user.top_role:
        await interaction.response.send_message(
            "❌ Нельзя: он админ или его роль не ниже твоей.", ephemeral=True)
        return
    db_run("INSERT OR REPLACE INTO blacklist VALUES (?,?,?,?,?)",
           (interaction.guild_id, user.id, reason, interaction.user.id, int(time.time())))
    log_action(interaction.guild_id, interaction.user.id, "blacklist_add",
               user.id, f"Причина: {reason}")
    await interaction.response.defer(ephemeral=True)
    await apply_blacklist_roles(interaction.guild, user, f"ЧС сервера: {reason}")
    await interaction.followup.send(
        f"🚫 {user.mention} внесён в ЧС. Причина: {reason}. Все роли сняты, осталась роль ЧС + мут.")
    await log_event(interaction.guild,
                    f"🚫 {user.mention} в ЧС. Причина: {reason}. Внёс: {interaction.user.mention}")
    await dm_user(user, f"🚫 **Тебя внесли в ЧС сервера «{interaction.guild.name}»**\n"
                        f"Причина: {reason}\nАдмин: {interaction.user}\n"
                        f"Все роли сняты. ЧС сохраняется при выходе/заходе.")


@tree.command(name="blacklist_remove", description="Снять ЧС сервера")
@app_commands.describe(user="Кого снять с ЧС")
async def blacklist_remove(interaction: discord.Interaction, user: discord.User):
    if not await admin_gate(interaction):
        return
    if not is_blacklisted(interaction.guild_id, user.id):
        await interaction.response.send_message("ℹ️ Его и так нет в ЧС.", ephemeral=True)
        return
    db_run("DELETE FROM blacklist WHERE guild_id=? AND user_id=?",
           (interaction.guild_id, user.id))
    log_action(interaction.guild_id, interaction.user.id, "blacklist_remove", user.id, "Снят ЧС")
    member = interaction.guild.get_member(user.id)
    if member:
        bl = interaction.guild.get_role(BLACKLIST_ROLE_ID)
        muted = discord.utils.get(interaction.guild.roles, name=MUTED_ROLE_NAME)
        to_remove = [r for r in (bl, muted) if r is not None and r in member.roles
                     and r < interaction.guild.me.top_role]
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="Снятие ЧС")
            except discord.Forbidden:
                await interaction.response.send_message("❌ Нет прав снимать роль ЧС.", ephemeral=True)
                return
    await interaction.response.send_message(
        f"✅ {user.mention} снят с ЧС. Роли выдавай заново через `/giverole`.")
    await log_event(interaction.guild,
                    f"✅ {user.mention} снят с ЧС. Разбанил: {interaction.user.mention}")
    await dm_user(user, f"✅ **С тебя сняли ЧС сервера «{interaction.guild.name}»**\n"
                        f"Админ: {interaction.user}\nРоли обнулены, обратись к администрации.")


@tree.command(name="blacklist_list", description="Показать ЧС сервера")
async def blacklist_list(interaction: discord.Interaction):
    if not await admin_gate(interaction):
        return
    rows = db.execute("SELECT user_id, reason, added_by FROM blacklist WHERE guild_id=? ORDER BY at DESC",
                      (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("📭 ЧС пуст.", ephemeral=True)
        return
    lines = [f"• <@{uid}> — {reason} (внёс <@{by}>)" for uid, reason, by in rows[:20]]
    await interaction.response.send_message("**🚫 Чёрный список:**\n" + "\n".join(lines), ephemeral=True)


# ============ ВЫГОВОРЫ (3/3 = ЧС) ============

@tree.command(name="warn", description="Выдать выговор. 3/3 — автоматический ЧС")
@app_commands.describe(user="Кому выговор", reason="Причина")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str = "без причины"):
    if not await admin_gate(interaction):
        return
    if user.bot or user.id == interaction.user.id:
        await interaction.response.send_message("❌ Себе и ботам выговор не выдать.", ephemeral=True)
        return
    if is_admin_member(user) or user.top_role >= interaction.user.top_role:
        await interaction.response.send_message(
            "❌ Нельзя: он админ или его роль не ниже твоей.", ephemeral=True)
        return
    db_run("INSERT INTO warns (guild_id, user_id, reason, actor_id, at) VALUES (?,?,?,?,?)",
           (interaction.guild_id, user.id, reason, interaction.user.id, int(time.time())))
    log_action(interaction.guild_id, interaction.user.id, "warn", user.id, reason)
    count = await warn_count(interaction.guild_id, user.id)
    await interaction.response.send_message(
        f"⚠️ {user.mention} получил выговор ({count}/{MAX_WARNS}). Причина: {reason}")
    await log_event(interaction.guild,
                    f"⚠️ Выговор {user.mention} ({count}/{MAX_WARNS}). Причина: {reason}. Выдал: {interaction.user.mention}")
    await dm_user(user, f"⚠️ **Выговор {count}/{MAX_WARNS}** на сервере «{interaction.guild.name}»\n"
                        f"Причина: {reason}\nАдмин: {interaction.user}")
    if count >= MAX_WARNS:
        db_run("INSERT OR REPLACE INTO blacklist VALUES (?,?,?,?,?)",
               (interaction.guild_id, user.id,
                f"3/3 выговора. Последний: {reason}", interaction.user.id, int(time.time())))
        log_action(interaction.guild_id, interaction.user.id, "blacklist_add",
                   user.id, "Автоматически: 3/3 выговора")
        await apply_blacklist_roles(interaction.guild, user, "3/3 выговора")
        db_run("DELETE FROM warns WHERE guild_id=? AND user_id=?", (interaction.guild_id, user.id))
        await interaction.followup.send(
            f"🚫 {user.mention} набрал 3/3 выговора — автоматически внесён в ЧС сервера.")
        await log_event(interaction.guild, f"🚫 {user.mention} — 3/3 выговора, авто-ЧС. Роли сняты.")
        await dm_user(user, f"🚫 **ЧС сервера «{interaction.guild.name}»**\nПричина: 3/3 выговора.")


@tree.command(name="unwarn", description="Снять последний выговор")
@app_commands.describe(user="У кого снять")
async def unwarn(interaction: discord.Interaction, user: discord.Member):
    if not await admin_gate(interaction):
        return
    row = db_one("SELECT id, reason FROM warns WHERE guild_id=? AND user_id=? "
                 "ORDER BY at DESC LIMIT 1", (interaction.guild_id, user.id))
    if not row:
        await interaction.response.send_message("ℹ️ У него нет выговоров.", ephemeral=True)
        return
    db_run("DELETE FROM warns WHERE id=?", (row[0],))
    log_action(interaction.guild_id, interaction.user.id, "unwarn", user.id, row[1])
    count = await warn_count(interaction.guild_id, user.id)
    await interaction.response.send_message(
        f"✅ Снят последний выговор у {user.mention} (был: {row[1]}). Осталось: {count}/{MAX_WARNS}.")
    await log_event(interaction.guild,
                    f"✅ Снят выговор с {user.mention} (был: {row[1]}). Снял: {interaction.user.mention}. Осталось {count}/{MAX_WARNS}")
    await dm_user(user, f"✅ С тебя сняли выговор на сервере «{interaction.guild.name}»\n"
                        f"Снял: {interaction.user}. Осталось: {count}/{MAX_WARNS}")


@tree.command(name="warns", description="Показать выговоры участника")
@app_commands.describe(user="Чьи выговоры")
async def warns(interaction: discord.Interaction, user: discord.Member):
    if not await admin_gate(interaction):
        return
    rows = db.execute("SELECT reason, actor_id, at FROM warns WHERE guild_id=? AND user_id=? "
                      "ORDER BY at DESC", (interaction.guild_id, user.id)).fetchall()
    count = await warn_count(interaction.guild_id, user.id)
    if not rows:
        await interaction.response.send_message(
            f"📭 У {user.mention} выговоров нет (0/{MAX_WARNS}).", ephemeral=True)
        return
    lines = [f"• {reason} — выдал <@{by}>, <t:{at}:d>" for reason, by, at in rows]
    await interaction.response.send_message(
        f"**⚠️ Выговоры {user.mention}: {count}/{MAX_WARNS}**\n" + "\n".join(lines), ephemeral=True)


# ============ РОЛИ, НИКИ, ПРАВА ============

@tree.command(name="giverole", description="Выдать роль участнику")
@app_commands.describe(user="Кому выдать", role="Какую роль")
async def giverole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if not await admin_gate(interaction):
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Себе роль выдать нельзя.", ephemeral=True)
        return
    if is_blacklisted(interaction.guild_id, user.id):
        await interaction.response.send_message("❌ Человек в ЧС — сначала сними ЧС.", ephemeral=True)
        return
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ Эта роль выше роли бота.", ephemeral=True)
        return
    try:
        await user.add_roles(role, reason=f"/giverole от {interaction.user}")
        log_action(interaction.guild_id, interaction.user.id, "giverole", user.id, role.name)
        await interaction.response.send_message(
            f"✅ Роль {role.mention} выдана {user.mention}.", ephemeral=True)
        await log_event(interaction.guild,
                        f"➕ {interaction.user.mention} выдал роль {role.mention} → {user.mention}")
        await dm_user(user, f"➕ Тебе выдали роль **{role.name}** на сервере «{interaction.guild.name}».\n"
                            f"Выдал: {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Не хватает прав (роль бота ниже или нет Manage Roles).", ephemeral=True)


@tree.command(name="removerole", description="Снять роль с участника")
@app_commands.describe(user="У кого снять", role="Какую роль")
async def removerole(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    if not await admin_gate(interaction):
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Себе роль снять нельзя.", ephemeral=True)
        return
    try:
        await user.remove_roles(role, reason=f"/removerole от {interaction.user}")
        log_action(interaction.guild_id, interaction.user.id, "removerole", user.id, role.name)
        await interaction.response.send_message(
            f"✅ Роль {role.mention} снята с {user.mention}.", ephemeral=True)
        await log_event(interaction.guild,
                        f"➖ {interaction.user.mention} снял роль {role.mention} → {user.mention}")
        await dm_user(user, f"➖ У тебя сняли роль **{role.name}** на сервере «{interaction.guild.name}».\n"
                            f"Снял: {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не хватает прав.", ephemeral=True)


@tree.command(name="nickname", description="Сменить никнейм участнику")
@app_commands.describe(user="Кому менять", nick="Новый никнейм (пусто — сбросить)")
async def nickname(interaction: discord.Interaction, user: discord.Member, nick: str = ""):
    if not await admin_gate(interaction):
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Свой ник меняй сам через Discord.", ephemeral=True)
        return
    try:
        await user.edit(nick=nick or None, reason=f"/nickname от {interaction.user}")
        log_action(interaction.guild_id, interaction.user.id, "nickname", user.id, nick or user.name)
        await interaction.response.send_message(
            f"✅ Никнейм {user.mention} изменён на `{nick or user.name}`.", ephemeral=True)
        await log_event(interaction.guild,
                        f"✏️ {interaction.user.mention} сменил ник {user.mention} → `{nick or user.name}`")
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Не хватает прав (роль пользователя выше бота).", ephemeral=True)


@tree.command(name="perm", description="Изменить разрешения участника в канале")
@app_commands.describe(
    user="Кому менять права",
    channel="В каком канале (пусто — в текущем)",
    action="allow / deny / neutral",
    permission="Какое право",
)
@app_commands.choices(action=[
    app_commands.Choice(name="allow (разрешить)", value="allow"),
    app_commands.Choice(name="deny (запретить)", value="deny"),
    app_commands.Choice(name="neutral (сбросить)", value="neutral"),
], permission=[
    app_commands.Choice(name="view (видеть канал)", value="view"),
    app_commands.Choice(name="send (писать)", value="send"),
    app_commands.Choice(name="read_history (читать историю)", value="read_history"),
])
async def perm(interaction: discord.Interaction, user: discord.Member,
               action: app_commands.Choice[str], permission: app_commands.Choice[str],
               channel: discord.TextChannel = None):
    if not await admin_gate(interaction):
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Себе права не менять.", ephemeral=True)
        return
    ch = channel or interaction.channel
    flag_map = {"view": "view_channel", "send": "send_messages", "read_history": "read_message_history"}
    val = {"allow": True, "deny": False, "neutral": None}[action.value]
    try:
        await ch.set_permissions(user, reason=f"/perm от {interaction.user}",
                                 **{flag_map[permission.value]: val})
        log_action(interaction.guild_id, interaction.user.id, "perm", user.id,
                   f"{permission.name} → {action.name} в #{ch.name}")
        await interaction.response.send_message(
            f"✅ В {ch.mention} для {user.mention}: {permission.name} → {action.name}.", ephemeral=True)
        await log_event(interaction.guild,
                        f"🔓 {interaction.user.mention}: {permission.name}={action.name} для {user.mention} в {ch.mention}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не хватает прав менять разрешения.", ephemeral=True)


@tree.command(name="activity", description="Активность: топ-10 болтунов сервера")
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


@tree.command(name="actions", description="История действий: по человеку или последние 15")
@app_commands.describe(user="Чью историю (пусто — последние)")
async def actions(interaction: discord.Interaction, user: discord.User = None):
    if not await admin_gate(interaction):
        return
    if user:
        rows = db.execute("SELECT actor_id, action, details, at FROM actions "
                          "WHERE guild_id=? AND target_id=? ORDER BY at DESC LIMIT 20",
                          (interaction.guild_id, user.id)).fetchall()
        if not rows:
            await interaction.response.send_message(f"📭 По {user.mention} действий нет.", ephemeral=True)
            return
        lines = [f"• <t:{at}:d> `{act}` — {details} (инициатор <@{ac}>)" for ac, act, details, at in rows]
        await interaction.response.send_message(
            f"**📜 История {user.mention}:**\n" + "\n".join(lines), ephemeral=True)
    else:
        rows = db.execute("SELECT actor_id, action, target_id, details, at FROM actions "
                          "WHERE guild_id=? ORDER BY at DESC LIMIT 15",
                          (interaction.guild_id,)).fetchall()
        if not rows:
            await interaction.response.send_message("📭 Действий ещё не было.", ephemeral=True)
            return
        lines = [f"• <t:{at}:d> `{act}` → <@{tid}> — {details} (<@{ac}>)"
                 for ac, act, tid, details, at in rows]
        await interaction.response.send_message("**📜 Последние действия:**\n" + "\n".join(lines), ephemeral=True)


# ============ ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ ============

@tree.command(name="help", description="Список команд бота")
async def help_cmd(interaction: discord.Interaction):
    lines = [
        "**🌐 Для всех:**",
        "`/help` — этот список",
        "`/translate текст` — перевести текст на русский",
        "`/myinfo` — твои выговоры и статус ЧС",
        "",
        "**🛡 Админ-команды** (нужны роль + `/login`):",
        "`/login`, `/logout`, `/setpassword`",
        "`/warn`, `/unwarn`, `/warns` — выговоры (3/3 = ЧС)",
        "`/blacklist_add`, `/blacklist_remove`, `/blacklist_list` — ЧС сервера",
        "`/giverole`, `/removerole` — роли",
        "`/nickname`, `/perm` — ники и права каналов",
        "`/activity`, `/actions` — активность и история",
        "`/say`, `/translate` — сообщения и перевод",
        "`/bind`, `/check` — привязка каналов новостей",
        "",
        "**⚙️ Настройка (только для тебя):**",
        "`/setup` — меню: какая роль что может",
        "`/permissions` — посмотреть доступы",
        "`/setlogchannel` — приватный канал логов",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="myinfo", description="Твои выговоры и статус ЧС")
async def myinfo(interaction: discord.Interaction):
    uid, gid = interaction.user.id, interaction.guild_id
    parts = [f"**👤 {interaction.user.mention}**"]
    if is_blacklisted(gid, uid):
        row = db_one("SELECT reason FROM blacklist WHERE guild_id=? AND user_id=?", (gid, uid))
        parts.append(f"🚫 Ты в ЧС сервера. Причина: {row[0] if row else 'не указана'}")
    else:
        parts.append("✅ В ЧС не состоишь")
    count = await warn_count(gid, uid)
    parts.append(f"⚠️ Выговоры: {count}/{MAX_WARNS}")
    rows = db.execute("SELECT reason, actor_id, at FROM warns WHERE guild_id=? AND user_id=? "
                      "ORDER BY at DESC LIMIT 5", (gid, uid)).fetchall()
    for reason, by, at in rows:
        parts.append(f"• {reason} — выдал <@{by}>, <t:{at}:d>")
    await interaction.response.send_message("\n".join(parts), ephemeral=True)


@tree.command(name="translate", description="Перевести текст на русский")
@app_commands.describe(text="Текст для перевода")
async def translate(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True)
    result = await translate_text(text)
    if result is None:
        await interaction.followup.send("❌ Переводчик недоступен — оба сервиса не ответили")
    else:
        await interaction.followup.send(
            f"**Оригинал:**\n{text[:1200]}\n\n**Перевод:**\n{result[:1200]}")


# ============ БИНД И SAY ============

class TargetSelect(discord.ui.ChannelSelect):
    def __init__(self, view):
        super().__init__(placeholder="Канал-приёмник (куда кидать перевод)",
                         channel_types=[discord.ChannelType.text])
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
        super().__init__(placeholder="Канал-источник (откуда брать новости)",
                         channel_types=[discord.ChannelType.text])
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
        await deny(interaction, "⛔ Нет доступа.")
        return
    await interaction.response.defer(ephemeral=True)
    lines = ["✅ Бот работает"]
    lines.append("✅ Пароль введён" if is_logged_in(interaction.guild_id, interaction.user.id)
                 else "🔒 Пароль не введён — админ-команды закрыты")
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
        else:
            lines.append("❌ Source-канал не найден, сделай /bind заново")
        if tgt:
            p2 = tgt.permissions_for(me)
            lines.append(f"{'✅' if p2.send_messages else '❌'} Target <#{tgt.id}>: писать={p2.send_messages}")
        else:
            lines.append("❌ Target-канал не найден, сделай /bind заново")
    test = await translate_text("hello world. " * 40)
    lines.append("✅ Переводчик работает" if test else "❌ Переводчик недоступен")
    await interaction.followup.send("\n".join(lines))


class SayChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Куда отправить", channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SayModal(self.values[0]))


class SayModal(discord.ui.Modal, title="Сообщение от имени бота"):
    embed_title = discord.ui.TextInput(label="Заголовок", required=False, max_length=256)
    message_text = discord.ui.TextInput(label="Текст", style=discord.TextStyle.long,
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
        try:
            await self.channel.send(embed=embed)
            await interaction.followup.send(f"✅ Отправлено в <#{self.channel.id}>")
        except discord.Forbidden:
            await interaction.followup.send(f"❌ Нет прав писать в <#{self.channel.id}>")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}")


class SayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SayChannelSelect())


@tree.command(name="say", description="Отправить сообщение в выбранный канал")
async def say(interaction: discord.Interaction):
    if not await admin_gate(interaction):
        return
    await interaction.response.send_message("Выбери канал:", view=SayView(), ephemeral=True)


# ============ СОБЫТИЯ ============

@bot.event
async def on_member_join(member: discord.Member):
    """ЧС сохраняется при выходе и заходе."""
    if is_blacklisted(member.guild.id, member.id):
        await apply_blacklist_roles(member.guild, member, "ЧС сервера (перезаход)")
        await log_event(member.guild,
                        f"🔁 {member.mention} зашёл снова — ЧС применён заново, роли сняты")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Защита: во время ЧС вручную выданные роли снимаются."""
    if before.roles == after.roles:
        return
    if not is_blacklisted(after.guild.id, after.id):
        return
    bl = after.guild.get_role(BLACKLIST_ROLE_ID)
    muted = discord.utils.get(after.guild.roles, name=MUTED_ROLE_NAME)
    keep = {after.guild.default_role.id}
    if bl:
        keep.add(bl.id)
    if muted:
        keep.add(muted.id)
    extra = [r for r in after.roles if r.id not in keep and r != after.guild.default_role
             and r < after.guild.me.top_role]
    if extra:
        try:
            await after.remove_roles(*extra, reason="Пользователь в ЧС — ручная выдача ролей отменена")
            names = ", ".join(r.name for r in extra)
            await log_event(after.guild,
                            f"🛡 {after.mention} в ЧС: вручную выданные роли сняты ({names})")
        except discord.Forbidden:
            pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id or message.guild is None:
        return
    gid = message.guild.id

    if not message.author.bot:
        db_run("INSERT INTO activity VALUES (?,?,1) "
               "ON CONFLICT(guild_id, user_id) DO UPDATE SET msgs = msgs + 1",
               (gid, message.author.id))

    b = bindings.get(gid)
    if not b or message.channel.id != b["source"]:
        return

    if db_one("SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
              (gid, message.author.id)):
        return

    if message.author.bot and message.webhook_id is None:
        return

    target = message.guild.get_channel(b["target"])
    if target is None:
        print(f"⚠️ Target-канал {b['target']} не найден")
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
        f = await download_file(att.url, att.filename)
        if f is not None:
            files.append(f)
        else:
            failed_urls.append(att.url)

    try:
        if translated:
            parts = chunk_text(translated)
            await target.send(content=parts[0], files=files or None)
            for part in parts[1:]:
                await target.send(content=part)
        elif text.strip():
            parts = chunk_text("🌐 *(переводчик недоступен)*\n" + text)
            await target.send(content=parts[0], files=files or None)
            for part in parts[1:]:
                await target.send(content=part)
        elif files:
            await target.send(files=files)
        for ne in new_embeds:
            await target.send(embed=ne)
        if failed_urls:
            await target.send(content="📎 " + "\n".join(failed_urls))
    except discord.Forbidden:
        print(f"❌ Нет прав писать в #{target.name} — дай боту Send Messages в этом канале")
    except Exception as err:
        print(f"⚠️ Ошибка отправки: {err}")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Бот запущен как {bot.user} | серверов: {len(bot.guilds)}")


bot.run(DISCORD_TOKEN)
