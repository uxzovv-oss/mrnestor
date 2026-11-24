import os
import re
import time
import asyncio
import sqlite3
import discord
from discord import app_commands
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("❌ Не найдена переменная окружения DISCORD_TOKEN")

# ============ НАСТРОЙКИ ============
NEWS_URL = "https://www.reissad.com/bodycam/news"
# АВТОМАТИКА: Проверяем каждые 30 секунд. Для мгновенной реакции это максимум, что можно сделать без API сайта.
POLL_INTERVAL = 30          
# При первом запуске НЕ помечаем всё прочитанным, а сразу публикуем новые посты
FIRST_RUN_SILENT = False    
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en,ru;q=0.8",
}
TRANSLATE_CHUNK = 450
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ============ БАЗА ДАННЫХ ============
db = sqlite3.connect("newsbot.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS bindings (
    guild_id INTEGER, channel_id INTEGER, categories TEXT,
    PRIMARY KEY (guild_id, channel_id))""")
db.execute("""CREATE TABLE IF NOT EXISTS seen_news (
    url TEXT PRIMARY KEY, posted_at INTEGER)""")
db.commit()

def db_run(query, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur

def db_one(query, params=()):
    return db.execute(query, params).fetchone()

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

# ============ ПЕРЕВОДЧИК (Google + MyMemory fallback) ============

def split_for_translation(text, limit=TRANSLATE_CHUNK):
    parts = 
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
    for _ in range(2):
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as s:
                async with s.get(GOOGLE_URL, params=params) as r:
                    if r.status == 200:
                        data = await r.json()
                        result = "".join(p for p in data if p and p)
                        if result.strip() and not is_bad_translation(result):
                            return result
        except Exception as e:
            print(f"⚠️ Google: {e}")
    return None

async def mymemory_translate(text: str, dest: str = "ru"):
    if not text or not text.strip():
        return None
    results = 
    for chunk in split_for_translation(text, 450):
        params = {"q": chunk, "langpair": f"en|{dest}"}
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as s:
                async with s.get(MYMEMORY_URL, params=params) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
            piece = data.get("responseData", {}).get("translatedText")
            if not piece or is_bad_translation(piece):
                return None
            results.append(piece)
        except Exception:
            return None
    return " ".join(results)

async def translate_text(text: str, dest: str = "ru"):
    if not text or not text.strip():
        return None
    out = 
    for chunk in split_for_translation(text):
        result = await google_translate(chunk, dest)
        if result is None:
            result = await mymemory_translate(chunk, dest)
        if result is None:
            return None
        out.append(result)
    return " ".join(out).strip()

# ============ СКАПЕР САЙТА REISSAD ============

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

async def fetch_html(url: str) -> str:
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=HEADERS) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    print(f"⚠️ {url}: HTTP {r.status}")
                    return ""
                return await r.text(errors="replace")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить {url}: {e}")
        return ""

def extract_category(container) -> str:
    """Ищем подписанную на сайте категорию поста."""
    # 1) элементы с классом, похожим на категорию/тег
    for el in container.select("[class*=categor], [class*=tag], [class*=badge], [class*=label]"):
        txt = clean_text(el.get_text())
        if 2 <= len(txt) <= 40 and not txt.lower().startswith(("read", "подробнее", "more")):
            return txt
    # 2) бейдж-ссылка внутри карточки
    for a in container.select("a"):
        cls = " ".join(a.get("class", ))
        if any(k in cls.lower() for k in ("categor", "tag", "badge", "label")):
            txt = clean_text(a.get_text())
            if 2 <= len(txt) <= 40:
                return txt
    return ""

def parse_news(html: str) -> list:
    """Разбор страницы новостей."""
    soup = BeautifulSoup(html, "lxml")
    posts = {}

    # Стратегия A: JSON-LD (структурированные данные) - самый надежный способ
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in ("NewsArticle", "Article", "BlogPosting"):
                    url = item.get("url") or item.get("mainEntityOfPage")
                    if isinstance(url, dict):
                        url = url.get("@id")
                    if not url:
                        continue
                    posts[url] = {
                        "url": url,
                        "title": clean_text(item.get("headline") or item.get("name") or ""),
                        "category": clean_text(item.get("articleSection") or ""),
                        "summary": clean_text(item.get("description") or item.get("abstract") or ""),
                        "image": (item.get("image") or [""]) if isinstance(item.get("image"), list) else (item.get("image") or ""),
                        "date": clean_text(item.get("datePublished") or ""),
                    }
        except Exception:
            pass

    # Стратегия B: Карточки на странице (fallback)
    selectors = ("article",
                 "div[class*=news]", "div[class*=article]", "div[class*=post]", "div[class*=card]",
                 "li[class*=news]", "li[class*=post]", "section[class*=news]")
    seen_urls = set()
    for sel in selectors:
        for container in soup.select(sel):
            link = container.find("a", href=True)
            if not link:
                continue
            url = urljoin(NEWS_URL, link["href"]).split("#").split("?")
            if not url or url.rstrip("/") == NEWS_URL.rstrip("/"):
                continue
            if not re.search(r"/(news|blog|post|article)", url, re.I):
                continue
            if url in seen_urls or url in posts:
                continue
            heading = container.find(re.compile("^h[1-4]\$"))
            title = clean_text(heading.get_text()) if heading else clean_text(container.get_text())[:120]
            if len(title) < 5:
                continue
            paragraphs = [clean_text(p.get_text()) for p in container.find_all("p")]
            summary = max(paragraphs, key=len) if paragraphs else ""
            img = container.find("img")
            image = img.get("src") or img.get("data-src") or "" if img else ""
            if image and image.startswith("/"):
                image = urljoin(NEWS_URL, image)
            seen_urls.add(url)
            posts[url] = {
                "url": url,
                "title": title,
                "category": extract_category(container),
                "summary": summary[:800],
                "image": image,
                "date": "",
            }
    return list(posts.values())

async def enrich_post(post: dict):
    """Дотягиваем og:image и полный текст со страницы самого поста."""
    html = await fetch_html(post["url"])
    if not html:
        return
    soup = BeautifulSoup(html, "lxml")
    for prop in ("og:image",):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            post["image"] = urljoin(post["url"], tag["content"])
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content") and len(og_desc["content"]) > len(post.get("summary", "")):
        post["summary"] = clean_text(og_desc["content"])[:1500]
    # полный текст: самые длинные абзацы статьи
    paras = [clean_text(p.get_text()) for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) > 60]
    if paras:
        full = " ".join(paras)[:2500]
        if len(full) > len(post.get("summary", "")):
            post["summary"] = full

# ============ ПУБЛИКАЦИЯ ============

class OriginalLink(discord.ui.View):
    def __init__(self, url: str):
        super().__init__()
        self.add_item(discord.ui.Button(label="Открыть оригинал", url=url))

async def build_post_embed(post: dict) -> discord.Embed:
    title = post.get("title", "")
    summary = post.get("summary", "")
    category = post.get("category", "")

    # Перевод всех полей
    t_title = await translate_text(title)
    t_summary = await translate_text(summary[:1500])
    t_cat = await translate_text(category)

    header = f"[{t_cat or category or 'Reissad Studio'}]"
    embed = discord.Embed(
        title=f"{header} {t_title or title}",
        url=post["url"],
        description=(t_summary or summary)[:1500] or None,
        color=discord.Color.orange(),
    )
    if post.get("image"):
        embed.set_image(url=post["image"])
    embed.set_footer(text="Reissad Studio • Bodycam • перевод бота")
    return embed

async def deliver_post(post: dict):
    rows = db.execute("SELECT guild_id, channel_id, categories FROM bindings").fetchall()
    if not rows:
        return
    embed = await build_post_embed(post)
    cat = (post.get("category") or "").lower().strip()
    
    for gid, cid, cats in rows:
        # Проверка фильтра категорий
        if cats and cat:
            allowed = {c.strip().lower() for c in cats.split(",") if c.strip()}
            if allowed and cat not in allowed:
                continue
        
        ch = bot.get_channel(int(cid))
        if ch is None:
            continue
        try:
            await ch.send(embed=embed, view=OriginalLink(post["url"]))
            print(f"✅ Опубликовано: {post['title'][:30]}... в канал {cid}")
        except discord.Forbidden:
            print(f"❌ Нет прав писать в канал {cid} на сервере {gid}")
        except Exception as e:
            print(f"⚠️ Ошибка публикации: {e}")

async def poll_news(post_now: bool = False):
    html = await fetch_html(NEWS_URL)
    if not html:
        print("⚠️ Страница новостей пуста")
        return 
    
    posts = parse_news(html)
    fresh = 
    
    for p in posts:
        # Если новость уже в базе - пропускаем
        if db_one("SELECT 1 FROM seen_news WHERE url=?", (p["url"],)):
            continue
        
        # Если это первый запуск и FIRST_RUN_SILENT=False, мы хотим опубликовать всё новое
        # Если это обычный цикл, мы тоже публикуем всё, что не в базе
        await enrich_post(p)
        fresh.append(p)
    
    # Сохраняем в базу и публикуем
    if fresh:
        for p in fresh:
            db_run("INSERT OR IGNORE INTO seen_news VALUES (?,?)", (p["url"], int(time.time())))
        for p in fresh:
            await deliver_post(p)
            
    return posts

async def news_loop():
    await bot.wait_until_ready()
    print("🔄 Авто-мониторинг новостей запущен (каждые 30 сек)...")
    
    # Первый прогон при старте
    await poll_news(post_now=True)
    
    while not bot.is_closed():
        await asyncio.sleep(POLL_INTERVAL)
        try:
            await poll_news(post_now=True)
        except Exception as e:
            print(f"⚠️ Ошибка опроса ленты: {e}")

@bot.event
async def setup_hook():
    bot.loop.create_task(news_loop())

# ============ КОМАНДЫ ============

@tree.command(name="bind", description="[админ] Привязать канал к рассылке новостей")
@app_commands.describe(
    channel="Канал для постов (пусто — текущий)",
    categories="Фильтр категорий через запятую (пусто — все)",
)
async def bind(interaction: discord.Interaction,
               channel: discord.TextChannel = None, categories: str = ""):
    if not is_admin(interaction):
        await interaction.response.send_message("⛔ Только для администраторов.", ephemeral=True)
        return
    ch = channel or interaction.channel
    cats = ",".join(c.strip() for c in categories.split(",") if c.strip())
    db_run("INSERT OR REPLACE INTO bindings VALUES (?,?,?)",
           (interaction.guild_id, ch.id, cats or None))
    msg = f"✅ Канал {ch.mention} привязан к рассылке."
    if cats:
        msg += f"\nФильтр категорий: `{cats}`"
    msg += "\n💡 Точные названия категорий смотри через `/newstest`."
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="unbind", description="[админ] Отвязать канал от рассылки")
@app_commands.describe(channel="Какой канал отвязать (пусто — текущий)")
async def unbind(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not is_admin(interaction):
        await interaction.response.send_message("⛔ Только для администраторов.", ephemeral=True)
        return
    ch = channel or interaction.channel
    row = db_one("SELECT 1 FROM bindings WHERE guild_id=? AND channel_id=?",
                 (interaction.guild_id, ch.id))
    if not row:
        await interaction.response.send_message("ℹ️ Этот канал не привязан.", ephemeral=True)
        return
    db_run("DELETE FROM bindings WHERE guild_id=? AND channel_id=?",
           (interaction.guild_id, ch.id))
    await interaction.response.send_message(f"✅ Канал {ch.mention} отвязан.", ephemeral=True)

@tree.command(name="bindings", description="[админ] Список привязанных каналов этого сервера")
async def bindings_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("⛔ Только для администраторов.", ephemeral=True)
        return
    rows = db.execute("SELECT channel_id, categories FROM bindings WHERE guild_id=?",
                      (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("📭 Привязок на этом сервере нет.", ephemeral=True)
        return
    lines = [f"• <#{cid}> — {('фильтр: ' + cats) if cats else 'все категории'}" for cid, cats in rows]
    await interaction.response.send_message("📡 Привязки:\n" + "\n".join(lines), ephemeral=True)

@tree.command(name="newstest", description="[админ] Проверить парсер: что бот видит на сайте прямо сейчас")
async def newstest(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("⛔ Только для администраторов.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    posts = await poll_news(post_now=False)
    if not posts:
        await interaction.followup.send(
            "❌ Бот не смог разобрать страницу. Сайт, вероятно, отдаёт контент только через JS.")
        return
    cats = sorted({p.get("category") for p in posts if p.get("category")})
    lines = [f"**Найдено постов: {len(posts)}**"]
    if cats:
        lines.append("**Категории на сайте:** " + ", ".join(f"`{c}`" for c in cats))
    lines.append("")
    for p in posts[:10]:
        lines.append(f"• [{p.get('category') or '?'}] {p['title'][:80]} — {p['url']}")
    await interaction.followup.send("\n".join(lines))

@tree.command(name="help", description="Справка по боту новостей")
async def help_cmd(interaction: discord.Interaction):
    lines = [
        "**📰 Бот новостей Bodycam (reissad.com):**",
        "Бот работает АВТОМАТИЧЕСКИ: проверяет сайт каждые 30 секунд.",
        "`/bind` — привязать канал (можно с фильтром категорий)",
        "`/unbind` — отвязать канал",
        "`/bindings` — список привязок сервера",
        "`/newstest` — проверить, какие категории видит бот",
        "",
        "Посты приходят с переводом, картинкой и кнопкой «Открыть оригинал»."
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Бот запущен как {bot.user} | серверов: {len(bot.guilds)}")

bot.run(DISCORD_TOKEN)
