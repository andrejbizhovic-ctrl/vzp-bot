import asyncio
import datetime
import os
import time
from collections import defaultdict, deque

import discord

TOKEN = os.environ.get('VZP_TOKEN')
if not TOKEN:
    raise SystemExit('VZP_TOKEN не задан')
GUILD_ID = 1557060322052014100
NOVICE_ID = 1557090179209240738
BALENCIAGA_ID = 1557097921139449958
VZP_ROLE_ID = 1557089169946189848
MUTED_ID = 1557089174371176629
ROLE18_ID = 1557094239379128491
MSG_ID = 1557095291646378006
MSG18_ID = 1557123383819247729
EMOJI = '\U0001F694'    # 🚔
EMOJI18 = '\U0001F51E'  # 🔞
PHOTO_CH = 1557094257683337247
GUEST_VC = 1557060324580921479
ADMIN_CH = 1557089250707509338
TICKET_CH = 1557106127425118269
MOD_ROLE = 1557089165961854997
SRMOD_ROLE = 1557089162212151316
OWNER_ROLE = 1557089149180321892
DEP_ROLE = 1557089152900669450
ADMIN_ROLE = 1557089156922875935
STAFF_IDS = {OWNER_ROLE, DEP_ROLE, ADMIN_ROLE, SRMOD_ROLE, MOD_ROLE}
PUNISH_ROLES = {ADMIN_ROLE, SRMOD_ROLE, MOD_ROLE}
TRUSTED_ROLES = {OWNER_ROLE, DEP_ROLE}

SPAM_WINDOW = 10   # сек
SPAM_MEMBER = 5    # сообщений за окно = спам
SPAM_STAFF = 10    # для должностей планка выше
WARN_RESET = 3600  # предупреждения сгорают через час
ROLE_ABUSE = 4     # снятых ролей за минуту = беспредел
DEL_ABUSE = 10     # удалённых сообщений за минуту = беспредел
HELP_COOLDOWN = 60
LOG = os.environ.get('VZP_LOG', r'C:\Users\lobas\AppData\Roaming\VZPBot\bot.log')

intents = discord.Intents.default()
intents.members = True
intents.reactions = True
intents.message_content = True

client = discord.Client(intents=intents)

msg_times = defaultdict(deque)
warns = defaultdict(int)
last_viol = defaultdict(float)
role_strips = defaultdict(deque)   # remover_id -> (t, victim_id, role_id)
deletes = defaultdict(deque)       # remover_id -> t
_last_hint = defaultdict(float)
_last_help = 0.0


def log(s):
    try:
        ts = datetime.datetime.now().strftime('%d.%m %H:%M:%S')
        line = f'[{ts}] {s}'
        print(line, flush=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(f'{line}\n')
    except Exception:
        pass


async def start_keepalive():
    """Render требует, чтобы web-сервис слушал порт. Отвечаем 'ok'."""
    port = os.environ.get('PORT')
    if not port:
        return
    try:
        from aiohttp import web

        async def _ka(request):
            return web.Response(text='VZP bot alive')

        app = web.Application()
        app.router.add_get('/', _ka)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', int(port))
        await site.start()
        log(f'keepalive HTTP on port {port}')
    except Exception as e:
        log(f'ERR keepalive: {e}')


async def get_member(guild, user_id):
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception as e:
            log(f'member {user_id} not found: {e}')
            return None
    return member


async def admin_alert(text):
    ch = client.get_channel(ADMIN_CH)
    if ch is None:
        log('ERR alert: admin channel not in cache')
        return
    try:
        await ch.send(text)
    except Exception as e:
        log(f'ERR alert: {e}')


async def quarantine(member):
    """Карантин вместо кика: снять все роли, надеть глухой Мьют."""
    guild = member.guild
    to_remove = []
    for rid in (NOVICE_ID, BALENCIAGA_ID, VZP_ROLE_ID, ROLE18_ID):
        r = guild.get_role(rid)
        if r is not None and r in member.roles:
            to_remove.append(r)
    muted = guild.get_role(MUTED_ID)
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason='auto: карантин за спам')
        if muted is not None:
            await member.add_roles(muted, reason='auto: карантин за спам')
        return True
    except Exception as e:
        log(f'ERR quarantine: {e}')
        return False


async def notify_staff(text):
    global _last_help
    now = time.time()
    if now - _last_help < HELP_COOLDOWN:
        return False
    _last_help = now
    await admin_alert(text)
    return True


async def punish(member, reason, restore=None):
    """Снять должности с провинившегося и вернуть роли жертвам."""
    role_ids = {r.id for r in member.roles}
    strip = [member.guild.get_role(r) for r in PUNISH_ROLES if r in role_ids]
    strip = [r for r in strip if r is not None]
    try:
        if strip:
            await member.remove_roles(*strip, reason=f'auto: {reason}')
        restored = 0
        if restore:
            for victim_id, role_id in restore:
                v = member.guild.get_member(victim_id)
                r = member.guild.get_role(role_id)
                if v is not None and r is not None:
                    try:
                        await v.add_roles(r, reason='auto-restore')
                        restored += 1
                    except Exception:
                        pass
        await admin_alert(
            f'⛔ {member.mention} **автоматически лишён должности**: {reason}. '
            f'Восстановлено ролей: {restored}. Решение может пересмотреть только Owner.')
        log(f'PUNISH {member.name}: {reason} (восстановлено {restored})')
    except Exception as e:
        log(f'ERR punish: {e}')


def extract_removed(entry):
    """Из записи аудита о смене ролей достаём список снятых role_id."""
    removed = []
    try:
        before = entry.before or []
        after = entry.after or []
        after_ids = {r.id for r in after}
        removed = [r.id for r in before if r.id not in after_ids]
    except Exception:
        pass
    if not removed:
        try:
            for ch in (entry.changes or []):
                key = str(getattr(ch, 'key', ''))
                if key.endswith('remove'):
                    val = getattr(ch, 'new', None) or getattr(ch, 'old', None)
                    if val:
                        for r in val:
                            rid = getattr(r, 'id', None)
                            if rid is None and isinstance(r, dict):
                                rid = r.get('id')
                            if rid:
                                removed.append(int(rid))
        except Exception:
            pass
    return removed


async def watchdog():
    """Смотрит аудит-лог: ловит массовое снятие ролей и удаление сообщений."""
    await client.wait_until_ready()
    guild = client.get_guild(GUILD_ID)
    while not client.is_closed():
        try:
            now_dt = discord.utils.utcnow()
            cutoff = now_dt - datetime.timedelta(seconds=60)
            async for entry in guild.audit_logs(limit=15, action=discord.AuditLogAction.member_role_update):
                if entry.created_at < cutoff:
                    break
                if entry.user_id == client.user.id:
                    continue
                remover = guild.get_member(entry.user_id)
                if remover is None or remover.bot:
                    continue
                rids = {r.id for r in remover.roles}
                if TRUSTED_ROLES & rids:
                    continue
                removed = extract_removed(entry)
                if not removed:
                    continue
                target_id = getattr(entry.target, 'id', None)
                if target_id is None:
                    continue
                dq = role_strips[entry.user_id]
                for rid in removed:
                    dq.append((time.time(), target_id, rid))
                while dq and time.time() - dq[0][0] > 60:
                    dq.popleft()
                if len(dq) >= ROLE_ABUSE:
                    restore = list(dq)
                    dq.clear()
                    await punish(remover, f'массовое снятие ролей ({len(restore)} шт за минуту)',
                                 restore=restore)
            async for entry in guild.audit_logs(limit=25, action=discord.AuditLogAction.message_delete):
                if entry.created_at < cutoff:
                    break
                if entry.user_id == client.user.id:
                    continue
                remover = guild.get_member(entry.user_id)
                if remover is None or remover.bot:
                    continue
                rids = {r.id for r in remover.roles}
                if TRUSTED_ROLES & rids:
                    continue
                count = 1
                try:
                    extra = entry.extra
                    if isinstance(extra, dict) and extra.get('count'):
                        count = int(extra['count'])
                except Exception:
                    pass
                dq = deletes[entry.user_id]
                for _ in range(count):
                    dq.append(time.time())
                while dq and time.time() - dq[0] > 60:
                    dq.popleft()
                if len(dq) >= DEL_ABUSE:
                    dq.clear()
                    await punish(remover, f'массовое удаление сообщений ({count}+)')
        except Exception as e:
            log(f'ERR watchdog: {e}')
        await asyncio.sleep(20)


@client.event
async def on_ready():
    log(f'UP as {client.user}')
    await start_keepalive()
    if not getattr(client, '_ticket_view_added', False):
        client._ticket_view_added = True
        client.add_view(TicketButtonView())
    if not getattr(client, '_watchdog_started', False):
        client._watchdog_started = True
        client.loop.create_task(watchdog())


@client.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID:
        return
    log(f'JOIN {member.name}')
    try:
        role = member.guild.get_role(NOVICE_ID)
        if role is not None:
            await member.add_roles(role, reason='novice on join')
            log(f'+novice {member.name}')
    except Exception as e:
        log(f'ERR join: {e}')


@client.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != GUILD_ID:
        return
    if payload.message_id == MSG_ID:
        log(f'REACT {payload.emoji} by {payload.user_id}')
    elif payload.message_id == MSG18_ID:
        log(f'REACT18 {payload.emoji} by {payload.user_id}')
    else:
        return
    if payload.user_id == client.user.id:
        return
    emoji = str(payload.emoji)
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        log('ERR react: guild not in cache')
        return
    member = await get_member(guild, payload.user_id)
    if member is None or member.bot:
        return
    try:
        if emoji == EMOJI and payload.message_id == MSG_ID:
            bal = guild.get_role(BALENCIAGA_ID)
            nov = guild.get_role(NOVICE_ID)
            if bal is not None and bal not in member.roles:
                await member.add_roles(bal, reason='Balenciaga via reaction')
                log(f'+balenciaga {member.name}')
            if nov is not None and nov in member.roles:
                await member.remove_roles(nov, reason='got Balenciaga')
                log(f'-novice {member.name}')
        elif emoji == EMOJI18:
            r18 = guild.get_role(ROLE18_ID)
            if r18 is not None and r18 not in member.roles:
                await member.add_roles(r18, reason='18+ via reaction')
                log(f'+18 {member.name}')
    except Exception as e:
        log(f'ERR react: {e}')


@client.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id != GUILD_ID or payload.message_id not in (MSG_ID, MSG18_ID):
        return
    if str(payload.emoji) != EMOJI18:
        return
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    member = await get_member(guild, payload.user_id)
    if member is None or member.bot:
        return
    try:
        r18 = guild.get_role(ROLE18_ID)
        if r18 is not None and r18 in member.roles:
            await member.remove_roles(r18, reason='18+ reaction removed')
            log(f'-18 {member.name}')
    except Exception as e:
        log(f'ERR unreact: {e}')


@client.event
async def on_voice_state_update(member, before, after):
    if member.guild.id != GUILD_ID or member.bot:
        return
    if after.channel is None or after.channel.id != GUEST_VC:
        return
    if before.channel is not None and before.channel.id == GUEST_VC:
        return
    role_ids = {r.id for r in member.roles}
    if STAFF_IDS & role_ids or BALENCIAGA_ID in role_ids:
        return
    ok = await notify_staff(
        f'🔔 <@&{MOD_ROLE}> <@&{SRMOD_ROLE}> новичок **{member.display_name}** '
        f'зашёл в **Гостевой** войс — нужен мод в помощь')
    if ok:
        log(f'guest join: {member.name}')


class TicketModal(discord.ui.Modal, title='Вступление в фаму VZP'):
    nick = discord.ui.TextInput(label='Ник в игре', placeholder='Твой ник на сервере',
                                min_length=1, max_length=50, required=True)
    hours = discord.ui.TextInput(label='Сколько часов играешь в GTA 5 RP?',
                                 placeholder='Например: 500', max_length=60, required=True)
    cheat = discord.ui.TextInput(label='С каким читом играешь или без?',
                                 placeholder='Например: без чита', max_length=100, required=True)
    about = discord.ui.TextInput(label='О себе', style=discord.TextStyle.paragraph,
                                 placeholder='Возраст, чем занимаешься, почему тебя должны взять в VZP',
                                 max_length=1000, required=True)

    async def on_submit(self, interaction):
        try:
            await interaction.response.send_message(
                '✅ Заявка на вступление в фаму отправлена штабу! Жди решения.',
                ephemeral=True)
        except Exception:
            pass
        ch = client.get_channel(ADMIN_CH)
        if ch is not None:
            try:
                await ch.send(
                    f'🎫 **ЗАЯВКА НА ВСТУПЛЕНИЕ В ФАМУ** от {interaction.user.mention} ({interaction.user.name})\n'
                    f'👤 Ник: {self.nick.value.strip()}\n'
                    f'⏱ Играет (часов): {self.hours.value.strip()}\n'
                    f'🛡 Чит: {self.cheat.value.strip()}\n'
                    f'📝 О себе: {self.about.value.strip()[:1000]}\n\n'
                    f'<@&{MOD_ROLE}> <@&{SRMOD_ROLE}> — рассмотрите заявку')
            except Exception as e:
                log(f'ERR ticket send: {e}')
        log(f'ticket from {interaction.user.name}')


class TicketButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Вступить в фаму', style=discord.ButtonStyle.green,
                       emoji='🎫', custom_id='ticket_button')
    async def ticket_button(self, interaction, button):
        try:
            log(f'BUTTON {interaction.user.name}')
            await interaction.response.send_modal(TicketModal())
        except Exception as e:
            log(f'ERR button: {e}')


@client.event
async def on_message(message):
    if message.guild is None or message.guild.id != GUILD_ID:
        return
    if message.author.bot:
        return
    role_ids = {r.id for r in message.author.roles}
    is_staff = bool(STAFF_IDS & role_ids)
    is_trusted = bool(TRUSTED_ROLES & role_ids)

    # тикет-канал: подсказка к кнопке (антиспам не считает)
    if message.channel.id == TICKET_CH:
        if not is_staff:
            now = time.time()
            if now - _last_hint[message.author.id] > 60:
                _last_hint[message.author.id] = now
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.channel.send(
                    f'{message.author.mention} заявка подаётся кнопкой **«Вступить в фаму»** '
                    f'в закреплённом сообщении 🎫')
        return

    # 18+ канал: только фото
    if message.channel.id == PHOTO_CH:
        if not is_staff and not message.attachments:
            try:
                await message.delete()
                log(f'del text in 18+ from {message.author.name}')
            except Exception as e:
                log(f'ERR del: {e}')
        return

    # антиспам
    now = time.time()
    uid = message.author.id
    dq = msg_times[uid]
    dq.append(now)
    while dq and now - dq[0] > SPAM_WINDOW:
        dq.popleft()

    if not is_trusted and len(dq) >= (SPAM_STAFF if is_staff else SPAM_MEMBER):
        dq.clear()
        if is_staff:
            await punish(message.author, 'спам (автоматически, должность снята)')
            return
        if now - last_viol[uid] > WARN_RESET:
            warns[uid] = 0
        last_viol[uid] = now
        warns[uid] += 1
        w = warns[uid]
        if w >= 3:
            try:
                await message.author.kick(reason='спам после 2 предупреждений')
                log(f'KICK {message.author.name} (спам)')
                await admin_alert(
                    f'⛔ **{message.author.name}** кикнут автоматически: спам после 2 предупреждений')
            except Exception:
                ok = await quarantine(message.author)
                if ok:
                    log(f'QUARANTINE {message.author.name} (спам)')
                    await admin_alert(
                        f'🚫 {message.author.mention} **отправлен в карантин (Мьют)** — спам '
                        f'после 2 предупреждений. Кик пока недоступен из-за иерархии ролей: '
                        f'можешь добить вручную (ПКМ по нику → Кик).')
        else:
            try:
                await message.author.send(
                    f'⚠️ Предупреждение {w}/2 за спам на сервере «ВОЙНА ЗА БИЗНЕС». '
                    f'Ещё одно нарушение — и ты будешь исключён.')
                log(f'WARN{w} {message.author.name}')
            except Exception:
                log(f'WARN{w} {message.author.name} (ЛС закрыты, не доставлено)')
            await admin_alert(
                f'⚠️ **{message.author.name}** получил предупреждение {w}/2 за спам')

    # просьба выдать роль
    low = message.content.lower()
    if not is_staff and ('выдайте роль' in low or 'дайте роль' in low
                         or 'выдай роль' in low or 'дай роль' in low
                         or 'получение роли' in low or 'получить роль' in low):
        ok = await notify_staff(
            f'🔔 <@&{MOD_ROLE}> <@&{SRMOD_ROLE}> {message.author.mention} '
            f'просит выдать роль (<#{message.channel.id}>)')
        if ok:
            log(f'help request from {message.author.name}')


while True:
    try:
        client.run(TOKEN)
    except Exception as e:
        log(f'FATAL: {e}')
    log('перезапуск бота через 20 сек (ждём сеть)...')
    try:
        time.sleep(20)
    except KeyboardInterrupt:
        break
