import os
import io
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
