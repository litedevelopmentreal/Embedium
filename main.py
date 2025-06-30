import discord
import os
import random
from dotenv import load_dotenv
from discord.ext import commands
import aiosqlite
from datetime import datetime, timezone, timedelta
import asyncio
import io

# .env dosyasındaki ortam değişkenlerini yükle
load_dotenv()

# Discord bot token'ını .env dosyasından al
TOKEN = os.getenv('DISCORD_TOKEN')
if TOKEN is None:
    print("HATA: DISCORD_TOKEN ortam değişkeni bulunamadı. .env dosyasını kontrol edin.")
    exit()

# Bot sahibinin Discord kullanıcı ID'si
OWNER_ID = 1239252682515152917  # <--- BURAYI KENDİ DISCORD KULLANICI ID'NİZLE DEĞİŞTİRİN!

# Intents (ayrıcalıklı yetkiler) ayarları
intents = discord.Intents.default()
intents.message_content = True  # Mesaj içeriklerini okumak için
intents.members = True          # on_member_join, on_member_remove ve kullanıcı bilgisi için gerekli
intents.presences = True        # Botun durumunu ayarlamak için gerekli

# Bot client yerine commands.Bot kullanıyoruz
# PREFIX 'e!' olarak ayarlandı
bot = commands.Bot(command_prefix='e!', intents=intents, help_command=None) # help_command=None ile varsayılan yardım kapatılır

# Botun başlangıç zamanı (uptime için)
bot_start_time = datetime.now(timezone.utc)

# Veritabanını başlatma ve tablo oluşturma fonksiyonu
async def setup_db():
    async with aiosqlite.connect('bot_settings.db') as db:
        # Sunucu ayarları için tablo (hoş geldin kanalı vb.)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER
            )
        ''')
        # Botun genel kilitleme durumunu tutacak tablo
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bot_status (
                status_name TEXT PRIMARY KEY,
                is_locked INTEGER -- 0 for unlocked (açık), 1 for locked (kilitli)
            )
        ''')
        # Reaksiyon rolleri için tablo
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id INTEGER,
                message_id INTEGER,
                emoji TEXT,
                role_id INTEGER,
                PRIMARY KEY (guild_id, message_id, emoji)
            )
        ''')
        # Sessiz kanallar için tablo
        await db.execute('''
            CREATE TABLE IF NOT EXISTS silent_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER
            )
        ''')
        # Otomatik roller için tablo
        await db.execute('''
            CREATE TABLE IF NOT EXISTS autoroles (
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER
            )
        ''')
        # Ticket ayarları için tablo (log kanalı, kategori, moderatör rolü vb.)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id INTEGER PRIMARY KEY,
                ticket_category_id INTEGER,
                ticket_log_channel_id INTEGER,
                ticket_moderator_role_id INTEGER -- Ticketları yönetecek rolün ID'si
            )
        ''')
        # Açık ticket'ları takip etmek için tablo
        await db.execute('''
            CREATE TABLE IF NOT EXISTS active_tickets (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                user_id INTEGER,
                opened_at TEXT
            )
        ''')
        # 'command_lock' kaydı yoksa, varsayılan olarak KİLİDİ AÇIK (0) olarak ekle
        await db.execute("INSERT OR IGNORE INTO bot_status (status_name, is_locked) VALUES (?, ?)", ('command_lock', 0))
        await db.commit()
    print("Veritabanı hazır ve bağlantı başarılı.")

# --- Ticket Sistemi İçin View Sınıfı ---
# Hatanın ana kaynağı burasıydı. timeout=None ve custom_id'ler eklendi.
class TicketView(discord.ui.View):
    def __init__(self, bot_instance, mod_role_id):
        # Kalıcı bir View için timeout=None olmalıdır.
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.mod_role_id = mod_role_id

        # "Ticket Aç" butonu
        # custom_id her zaman benzersiz ve sabit olmalıdır.
        self.add_item(discord.ui.Button(
            label="Ticket Aç",
            style=discord.ButtonStyle.primary,
            custom_id="create_ticket_button", # Benzersiz ve sabit ID
            emoji="✉️"
        ))

    @discord.ui.button(label="Ticket Aç", style=discord.ButtonStyle.primary, custom_id="create_ticket_button", emoji="✉️")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        async with aiosqlite.connect('bot_settings.db') as db:
            cursor = await db.execute("SELECT ticket_category_id, ticket_log_channel_id FROM ticket_settings WHERE guild_id = ?", (guild.id,))
            settings = await cursor.fetchone()

        if not settings:
            return await interaction.response.send_message("Ticket sistemi bu sunucuda ayarlanmamış.", ephemeral=True)

        category_id, log_channel_id = settings
        category = guild.get_channel(category_id)
        log_channel = guild.get_channel(log_channel_id)

        if not category:
            return await interaction.response.send_message("Ayarlanan ticket kategorisi bulunamadı.", ephemeral=True)
        if not log_channel:
            return await interaction.response.send_message("Ayarlanan ticket log kanalı bulunamadı.", ephemeral=True)

        # Kullanıcının zaten açık bir ticket'ı var mı kontrol et
        async with aiosqlite.connect('bot_settings.db') as db:
            cursor = await db.execute("SELECT channel_id FROM active_tickets WHERE user_id = ? AND guild_id = ?", (user.id, guild.id))
            existing_ticket = await cursor.fetchone()

        if existing_ticket:
            existing_channel = guild.get_channel(existing_ticket[0])
            if existing_channel:
                return await interaction.response.send_message(f"Zaten açık bir ticket'ınız var: {existing_channel.mention}", ephemeral=True)
            else:
                # Eğer kanal yoksa veritabanından sil (çöp temizliği)
                async with aiosqlite.connect('bot_settings.db') as db:
                    await db.execute("DELETE FROM active_tickets WHERE channel_id = ?", (existing_ticket[0],))
                    await db.commit()

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True)
        }

        # Moderatör rolüne izin ver
        if self.mod_role_id:
            mod_role = guild.get_role(self.mod_role_id)
            if mod_role:
                overwrites[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True)
            else:
                print(f"[Ticket Hatası] Moderatör rolü bulunamadı: {self.mod_role_id}")
        
        try:
            ticket_channel = await guild.create_text_channel(f'ticket-{user.name}-{user.discriminator or user.id}', category=category, overwrites=overwrites)
            
            # Ticket'ı veritabanına kaydet
            async with aiosqlite.connect('bot_settings.db') as db:
                await db.execute("INSERT INTO active_tickets (channel_id, guild_id, user_id, opened_at) VALUES (?, ?, ?, ?)",
                                 (ticket_channel.id, guild.id, user.id, datetime.now(timezone.utc).isoformat()))
                await db.commit()

            embed = discord.Embed(
                title="🎟️ Destek Talebi Açıldı",
                description=f"{user.mention} tarafından yeni bir destek talebi oluşturuldu. Lütfen sorununuzu detaylıca açıklayın.",
                color=discord.Color.blue()
            )
            embed.set_footer(text="Ticket'ı kapatmak için 'Kapat' butonunu kullanın.")
            embed.timestamp = datetime.now(timezone.utc)

            close_view = TicketCloseView(self.bot, self.mod_role_id) # Kapatma butonu için yeni bir View
            await ticket_channel.send(embed=embed, view=close_view)
            await ticket_channel.send(f"{user.mention}, <@&{self.mod_role_id}>", delete_after=0.1) # Moderatör rolünü etiketle

            await interaction.response.send_message(f"Ticket'ınız açıldı: {ticket_channel.mention}", ephemeral=True)

            # Log kanalına bildirim gönder
            log_embed = discord.Embed(
                title="Yeni Ticket Açıldı",
                description=f"**Açan:** {user.mention}\n**Kanal:** {ticket_channel.mention}",
                color=discord.Color.green()
            )
            log_embed.timestamp = datetime.now(timezone.utc)
            await log_channel.send(embed=log_embed)
            print(f"[Ticket] {user.name} tarafından yeni bir ticket açıldı: {ticket_channel.name}")

        except discord.Forbidden:
            await interaction.response.send_message("Ticket kanalı oluşturma yetkim yok. Lütfen yetkilerimi kontrol edin.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Bir hata oluştu: {e}", ephemeral=True)
            print(f"[Ticket Hatası] Ticket oluşturulurken hata: {e}")

# Ticket kapatma ve silme butonları için yeni bir View sınıfı
class TicketCloseView(discord.ui.View):
    def __init__(self, bot_instance, mod_role_id):
        super().__init__(timeout=None) # Kalıcı bir View için timeout=None
        self.bot = bot_instance
        self.mod_role_id = mod_role_id

    @discord.ui.button(label="Kapat", style=discord.ButtonStyle.red, custom_id="close_ticket_button", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        guild = interaction.guild
        user_id_from_ticket = None

        # Sadece ticket sahibinin veya moderatörün kapatabilmesini sağla
        async with aiosqlite.connect('bot_settings.db') as db:
            cursor = await db.execute("SELECT user_id FROM active_tickets WHERE channel_id = ?", (channel.id,))
            result = await cursor.fetchone()
            if result:
                user_id_from_ticket = result[0]
            else:
                return await interaction.response.send_message("Bu bir ticket kanalı gibi görünmüyor veya veritabanında bulunamadı.", ephemeral=True)

        if interaction.user.id != user_id_from_ticket:
            if self.mod_role_id:
                mod_role = guild.get_role(self.mod_role_id)
                if not mod_role or mod_role not in interaction.user.roles:
                    return await interaction.response.send_message("Sadece ticket sahibi veya moderatörler bu ticket'ı kapatabilir.", ephemeral=True)
            else:
                 return await interaction.response.send_message("Sadece ticket sahibi bu ticket'ı kapatabilir. Moderatör rolü ayarlanmamış.", ephemeral=True)

        await interaction.response.send_message("Ticket kapatılıyor... Lütfen bekleyin.")
        
        # Transcript (mesaj geçmişi) al ve log kanalına gönder
        log_channel_id = None
        async with aiosqlite.connect('bot_settings.db') as db:
            cursor = await db.execute("SELECT ticket_log_channel_id FROM ticket_settings WHERE guild_id = ?", (guild.id,))
            log_result = await cursor.fetchone()
            if log_result:
                log_channel_id = log_result[0]

        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel:
                transcript_content = f"### Ticket Transkripti (Kanal ID: {channel.id}, Kapatan: {interaction.user})\n\n"
                async for message in channel.history(limit=None, oldest_first=True):
                    transcript_content += f"[{message.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {message.author.display_name}: {message.clean_content}\n"
                    for attachment in message.attachments:
                        transcript_content += f"[Ek: {attachment.url}]\n"
                
                transcript_file = discord.File(io.StringIO(transcript_content), filename=f"ticket-{channel.id}-transcript.txt")
                transcript_embed = discord.Embed(
                    title="Ticket Kapatıldı",
                    description=f"**Ticket:** {channel.name}\n**Kapatan:** {interaction.user.mention}",
                    color=discord.Color.dark_red()
                )
                transcript_embed.timestamp = datetime.now(timezone.utc)
                await log_channel.send(embed=transcript_embed, file=transcript_file)
                print(f"[Ticket] {channel.name} ticket'ı kapatıldı ve log kanalına transkript gönderildi.")
            else:
                print(f"[Ticket Hatası] Log kanalı bulunamadı: {log_channel_id}")


        # Veritabanından ticket'ı sil
        async with aiosqlite.connect('bot_settings.db') as db:
            await db.execute("DELETE FROM active_tickets WHERE channel_id = ?", (channel.id,))
            await db.commit()

        # Kanalı 5 saniye sonra sil
        await channel.send("Bu ticket kanalı 5 saniye içinde silinecektir.")
        await asyncio.sleep(5)
        try:
            await channel.delete()
            print(f"[Ticket] Ticket kanalı silindi: {channel.name}")
        except discord.Forbidden:
            print(f"[Ticket Hatası] Ticket kanalı silme yetkim yok: {channel.name}")
        except Exception as e:
            print(f"[Ticket Hatası] Ticket kanalı silerken hata: {e}")


# Bot Discord'a başarıyla bağlandığında çalışacak olay
@bot.event
async def on_ready():
    await setup_db() # Bot hazır olduğunda veritabanını hazırla
    print(f'🎉 {bot.user} olarak Discord\'a giriş yaptık!')
    print(f'Botunuz şu anda {len(bot.guilds)} sunucuda aktif.')
    
    # Tüm sunucular için aktif TicketView'ları yükle
    async with aiosqlite.connect('bot_settings.db') as db:
        cursor = await db.execute("SELECT guild_id, ticket_moderator_role_id FROM ticket_settings")
        settings_results = await cursor.fetchall()
    
    for guild_id, mod_role_id in settings_results:
        # Her sunucu için ayrı bir TicketView oluşturup bota ekliyoruz.
        # Bu, botun yeniden başlatılmasında butonların işlevsel kalmasını sağlar.
        # view = TicketView(bot_instance=bot, mod_role_id=mod_role_id) # Bu satırda hata alıyorduk
        # persistent view'lar için bot.add_view() yalnızca view sınıfını almalıdır.
        # İnteraction'da View oluştururken parametreleri aktarırız.
        
        # Ticket Aç butonu için
        bot.add_view(TicketView(bot_instance=bot, mod_role_id=mod_role_id))
        # Ticket Kapat butonu için (bu genellikle bir mesajla birlikte gönderildiğinden on_ready'de ayrıca eklenmesine gerek yoktur)
        # Ama eğer bir sebepten dolayı o da kalıcı olacaksa, onun da eklenmesi gerekir.
        # Mevcut kullanımınızda, TicketView içinde TicketCloseView oluşturuluyor.
        # Eğer kapat butonu mesajı da kalıcı ise, onun için de bot.add_view(TicketCloseView(...)) yapmalısınız.
        # Eğer bu ticket kapatma butonu mesajı sürekli aynı ID'ye sahipse (mesela bir "genel ticket mesajı" gibi),
        # o zaman her sunucu için o mesaja bir TicketCloseView yükleyebilirsiniz.
        # Ancak genellikle kapatma butonu ticket kanalı oluşturulduğunda gönderilir ve kanal silinir,
        # bu nedenle bu view'ın "kalıcı" olmasına gerek yoktur. Sizin kodunuzda da bu şekilde,
        # sadece "create_ticket_button" kalıcı olması gerekiyor.
        
        print(f"Sunucu {guild_id} için TicketView (Ticket Aç butonu) yüklendi.")


    # Durum güncellendi, yeni prefix'e göre yardım komutu
    await bot.change_presence(activity=discord.Game(name="Embedium | e!yardım"))

# Hata yakalama (komutlar için)
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Bot kilitliyse ve sahibi değilse CommandNotFound hatası görmesin
        is_locked = await is_bot_locked_status()
        # Eğer bot kilitliyse ve kullanan bot sahibi değilse, komut bulunamadı mesajı gönderme
        if is_locked and ctx.author.id != OWNER_ID:
            pass
        else:
            await ctx.send("Üzgünüm, böyle bir komut bulamadım. `e!yardım` yazarak komutları görebilirsiniz.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Komutu yanlış kullandınız. Eksik argüman: `{error.param.name}`. `e!yardım` kontrol edin.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("Bu komutu kullanmak için yeterli yetkiniz yok.")
    elif isinstance(error, commands.BotMissingPermissions):
        # Botun yapması gereken eylem için yetkisi yoksa
        if "kick_members" in str(error) or "ban_members" in str(error):
            await ctx.send("Bu işlemi yapmak için benim yetkim yok. Rol hiyerarşimi ve yetkilerimi kontrol edin.")
        elif "manage_messages" in str(error):
            await ctx.send("Mesajları silmek için yetkim yok.")
        elif "manage_channels" in str(error):
            await ctx.send("Kanal izinlerini yönetmek için yetkim yok.")
        elif "manage_roles" in str(error):
            await ctx.send("Rolleri yönetmek için yetkim yok.")
        else:
            await ctx.send(f"Bu komutu çalıştırmak için benim yeterli yetkim yok: {error}")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("Bu komut sadece bot sahibine özeldir!")
    elif isinstance(error, commands.CheckFailure):
        # check_bot_unlocked_or_owner veya check_not_silent_channel fonksiyonundan gelen özel hata mesajını göster
        await ctx.send(f"{error}")
    elif isinstance(error, commands.CommandOnCooldown):
        remaining = round(error.retry_after, 1)
        await ctx.send(f"Bu komutu tekrar kullanmak için `{remaining}` saniye beklemeniz gerekiyor.")
    else:
        print(f"Bilinmeyen bir hata oluştu: {type(error).__name__} - {error}")
        # await ctx.send("Beklenmeyen bir hata oluştu. Lütfen geliştiriciye bildirin.")
        # raise error # Hatanın tam izini görmek için bu satırı etkinleştirebilirsiniz

# --- Kilit Durumu Kontrolü Fonksiyonları ---
async def is_bot_locked_status():
    """Botun komutlarının kilitli olup olmadığını veritabanından döner."""
    async with aiosqlite.connect('bot_settings.db') as db:
        cursor = await db.execute("SELECT is_locked FROM bot_status WHERE status_name = 'command_lock'")
        result = await cursor.fetchone()
        # Eğer kayıt yoksa veya 1 ise kilitli (True), 0 ise açık (False)
        return bool(result[0]) if result else False # Varsayılan olarak KİLİDİ AÇIK (False) olsun

def check_bot_unlocked_or_owner():
    """
    Bu bir komut kontrolüdür.
    Eğer komutu kullanan bot sahibi ise her zaman True döner.
    Eğer bot kilitli değilse (is_locked=0) her zaman True döner.
    Aksi takdirde False döner ve hata mesajı fırlatır.
    """
    async def predicate(ctx):
        if ctx.author.id == OWNER_ID:
            return True # Bot sahibi her zaman komutları kullanabilir
        
        locked = await is_bot_locked_status()
        if not locked: # Eğer bot kilitli DEĞİLSE, herkes kullanabilir
            return True
        
        # Eğer bot kilitliyse ve kullanan bot sahibi değilse hata ver
        raise commands.CheckFailure("Bot şu anda geliştirme modunda kilitlidir. Komutları sadece bot sahibi kullanabilir.")
    return commands.check(predicate)

# --- Sessiz Kanal Kontrolü Fonksiyonu ---
def check_not_silent_channel():
    """
    Bu bir komut kontrolüdür.
    Eğer komutu kullanan bot sahibi ise her zaman True döner.
    Eğer komut sessiz bir kanalda kullanılmıyorsa True döner.
    Aksi takdirde False döner ve hata mesajı fırlatır.
    """
    async def predicate(ctx):
        if ctx.author.id == OWNER_ID:
            return True # Bot sahibi her zaman komutları kullanabilir

        async with aiosqlite.connect('bot_settings.db') as db:
            cursor = await db.execute("SELECT channel_id FROM silent_channels WHERE channel_id = ?", (ctx.channel.id,))
            is_silent = await cursor.fetchone()
        
        if is_silent:
            raise commands.CheckFailure("Bu kanal sessiz moda alınmıştır. Burada komutlara yanıt veremiyorum.")
        return True
    return commands.check(predicate)


# Yeni bir üye sunucuya katıldığında çalışacak olay
@bot.event
async def on_member_join(member):
    guild = member.guild
    async with aiosqlite.connect('bot_settings.db') as db:
        # Hoş geldin kanalı ayarını al
        cursor = await db.execute("SELECT welcome_channel_id FROM guild_settings WHERE guild_id = ?", (guild.id,))
        welcome_result = await cursor.fetchone()
        
        # Otorol ayarını al
        cursor = await db.execute("SELECT role_id FROM autoroles WHERE guild_id = ?", (guild.id,))
        autorole_result = await cursor.fetchone()

    # Hoş geldin mesajı gönderme kısmı
    if welcome_result:
        welcome_channel_id = welcome_result[0]
        welcome_channel = guild.get_channel(welcome_channel_id)

        if welcome_channel:
            embed = discord.Embed(
                title=f"Sunucumuza Hoş Geldiniz, {member.display_name}!",
                description=f"{member.mention}, {guild.name} sunucusuna katıldı! Aramıza hoş geldin! 🎉",
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
            embed.set_footer(text=f"Şu an {guild.member_count} üyeyiz.")
            embed.timestamp = datetime.now(timezone.utc)

            await welcome_channel.send(embed=embed)
            print(f"[Hoş Geldin] {member.name} sunucuya katıldı. Mesaj '{welcome_channel.name}' kanalına gönderildi.")
        else:
            print(f"[Hoş Geldin Hatası] Veritabanındaki ID ({welcome_channel_id}) ile hoş geldin kanalı bulunamadı. Kanal silinmiş olabilir.")
    else:
        print(f"[Hoş Geldin] {member.name} sunucuya katıldı. Bu sunucu için hoş geldin kanalı ayarlanmamış.")

    # Otorol verme kısmı
    if autorole_result:
        role_id = autorole_result[0]
        role = guild.get_role(role_id)
        
        if role:
            # Botun rol hiyerarşisi kontrolü
            if guild.me.top_role <= role:
                print(f"[Otorol Hatası] Botun rolü '{role.name}' rolünden düşük. Otorol verilemedi.")
                # Bu hata mesajını kullanıcıya göndermemek daha iyi, çünkü on_member_join arka planda çalışır.
                return
            
            try:
                await member.add_roles(role)
                print(f"[Otorol] {member.name} kullanıcısına '{role.name}' rolü otomatik olarak verildi.")
            except discord.Forbidden:
                print(f"[Otorol Hatası] Yetki hatası: {member.name} kullanıcısına '{role.name}' rolü verilemedi. Botun rolünü kontrol edin.")
            except Exception as e:
                print(f"[Otorol Hatası] Rol verme hatası: {e}")
        else:
            print(f"[Otorol Hatası] Veritabanındaki ID ({role_id}) ile otorol bulunamadı. Rol silinmiş olabilir.")

# Bir üye sunucudan ayrıldığında çalışacak olay
@bot.event
async def on_member_remove(member):
    guild = member.guild
    async with aiosqlite.connect('bot_settings.db') as db:
        cursor = await db.execute("SELECT welcome_channel_id FROM guild_settings WHERE guild_id = ?", (guild.id,))
        result = await cursor.fetchone()

    if result:
        leave_log_channel_id = result[0] # Genellikle hoş geldin kanalı log kanalı olarak da kullanılır
        leave_channel = guild.get_channel(leave_log_channel_id)

        if leave_channel:
            embed = discord.Embed(
                title=f"Güle Güle, {member.display_name}!",
                description=f"{member.name} sunucudan ayrıldı. Üye sayısı: {guild.member_count}",
                color=discord.Color.red()
            )
            embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
            embed.timestamp = datetime.now(timezone.utc)
            await leave_channel.send(embed=embed)
            print(f"[Ayrılık] {member.name} sunucudan ayrıldı. Mesaj '{leave_channel.name}' kanalına gönderildi.")
        else:
            print(f"[Ayrılık Hatası] Veritabanındaki ID ({leave_log_channel_id}) ile ayrılık kanalı bulunamadı. Kanal silinmiş olabilir.")
    else:
        print(f"[Ayrılık] {member.name} sunucudan ayrıldı. Bu sunucu için ayrılık kanalı ayarlanmamış (varsayılan hoş geldin kanalı kullanıldı).")

# on_message olayı sadece komutları işlemek için kullanılır
# Artık sessiz kanal veya kilitli bot kontrolleri direkt komut decorator'larında yapılıyor
@bot.event
async def on_message(message):
    # Botun kendi mesajlarını yok say
    if message.author == bot.user:
        return
    
    # Komutları işlemek için
    await bot.process_commands(message)

# --- Genel Komutlar ---

@bot.command(name='ping', help='Botun gecikmesini gösterir.')
# Ping komutu, botun çalışıp çalışmadığını kontrol etmek için her zaman erişilebilir olmalı.
# O yüzden sessiz kanal veya bot kilidi kontrolü eklenmez.
async def ping(ctx):
    await ctx.send(f'Pong! Gecikme: {round(bot.latency * 1000)}ms')
    print(f"[{ctx.author}] e!ping komutunu kullandı.")

@bot.command(name='yardım', help='Kullanılabilir komutları listeler ve açıklar.')
# Yardım komutu, bot kilitliyse bile (farklı bir mesajla) her zaman çalışmalı.
# Sessiz kanal kontrolü de burada uygulanmaz.
async def yardim(ctx):
    print(f"DEBUG: 'yardım' komutu çağrıldı. Kullanıcı: {ctx.author.name}, ID: {ctx.author.id}")
    
    # Bot kilitli mi kontrol et
    locked = await is_bot_locked_status()
    
    embed = discord.Embed(
        title="Bot Komutları",
        description="İşte kullanabileceğim komutlar:",
        color=discord.Color.blue()
    )

    if locked and ctx.author.id != OWNER_ID:
        # Eğer bot kilitliyse ve kullanan sahip değilse
        embed.description = "Bot şu anda geliştirme modunda kilitlidir. Komutları sadece bot sahibi kullanabilir.\n\n"
        embed.add_field(name="Sadece Sahibe Özel Komutlar", 
                             value="`e!kilitle_bot`, `e!kilidi_aç_bot`, `e!kapat`, `e!değiştir_durum`", 
                             inline=False)
    else:
        # Bot kilitli değilse veya sahibi kullanıyorsa tüm komutları göster
        genel_komutlar = ""
        moderasyon_komutlar = ""
        eğlence_komutlar = ""
        bilgi_komutlar = ""
        ayar_komutlar = ""
        sahibe_ozel_komutlar = ""

        # Kategorize edilmemiş komutlar için boş bir dize başlat
        kategorize_edilmemis_komutlar = ""

        for command in bot.commands:
            # Gizli komutları sahibinden başkasına gösterme
            if command.hidden and ctx.author.id != OWNER_ID:
                continue 

            cmd_info = f"`e!{command.name}`: {command.help or 'Açıklama yok.'}\n"
            
            # Komutları kategorize et
            if command.name in ['ping', 'yardım']:
                genel_komutlar += cmd_info
            elif command.name in ['kick', 'ban', 'unban', 'clear', 'kanala_mesaj', 'duyuru']: 
                moderasyon_komutlar += cmd_info
            elif command.name in ['zar', 'yazıtura', '8ball']:
                eğlence_komutlar += cmd_info
            elif command.name in ['sunucu_bilgi', 'kullanıcı_bilgi']:
                bilgi_komutlar += cmd_info
            elif command.name in ['ayarla_hosgeldin', 'sifirla_hosgeldin', 'reaksiyon_rolu_ayarla', 
                                     'sessiz_kanal_ayarla', 'sessiz_kanal_sifirla', 
                                     'otorol_ayarla', 'otorol_sifirla', 
                                     'ayarla_ticket', 'ticket_aç', 'ticket_kapat',
                                     'gönder_ticket_butonu']: 
                ayar_komutlar += cmd_info
            elif command.name in ['kapat', 'değiştir_durum', 'kilitle_bot', 'kilidi_aç_bot']: 
                # Sahibe özel komutları sadece sahip görsün
                if ctx.author.id == OWNER_ID:
                    sahibe_ozel_komutlar += cmd_info
            else:
                # Hiçbir kategoriye uymayan komutlar için
                kategorize_edilmemis_komutlar += cmd_info
            
        # 1024 karakter limitini aşmamak için kontrol ekleyelim
        if genel_komutlar: embed.add_field(name="Genel Komutlar", value=genel_komutlar[:1020], inline=False)
        if moderasyon_komutlar: embed.add_field(name="Moderasyon Komutları", value=moderasyon_komutlar[:1020], inline=False)
        if eğlence_komutlar: embed.add_field(name="Eğlence Komutları", value=eğlence_komutlar[:1020], inline=False)
        if bilgi_komutlar: embed.add_field(name="Bilgi Komutları", value=bilgi_komutlar[:1020], inline=False)
        if ayar_komutlar: embed.add_field(name="Ayarlar", value=ayar_komutlar[:1020], inline=False)
        if sahibe_ozel_komutlar: embed.add_field(name="Sahibe Özel Komutlar", value=sahibe_ozel_komutlar[:1020], inline=False)
        if kategorize_edilmemis_komutlar: embed.add_field(name="Diğer Komutlar", value=kategorize_edilmemis_komutlar[:1020], inline=False)

    await ctx.send(embed=embed)
    print(f"[{ctx.author}] e!yardım komutunu kullandı.")

# --- Bilgilendirme Komutları ---

@bot.command(name='sunucu_bilgi', aliases=['sunucu', 'sbilgi'], help='Sunucu hakkında bilgi gösterir.')
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(
        title=f"{guild.name} Sunucu Bilgisi",
        description=f"**{guild.name}** sunucusu hakkında detaylı bilgi.",
        color=discord.Color.gold()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    embed.add_field(name="Sunucu Sahibi", value=guild.owner.mention if guild.owner else "Bilinmiyor", inline=True)
    embed.add_field(name="Üye Sayısı", value=guild.member_count, inline=True)
    embed.add_field(name="Kanal Sayısı", value=len(guild.channels), inline=True)
    embed.add_field(name="Rol Sayısı", value=len(guild.roles), inline=True)
    embed.add_field(name="Oluşturulma Tarihi", value=guild.created_at.strftime("%d.%m.%Y %H:%M:%S"), inline=False)
    embed.add_field(name="Boost Seviyesi", value=f"Seviye {guild.premium_tier} ({guild.premium_subscription_count} Boost)", inline=True)
    embed.add_field(name="ID", value=guild.id, inline=True)
    
    await ctx.send(embed=embed)
    print(f"[{ctx.author}] e!sunucu_bilgi komutunu kullandı.")

@bot.command(name='kullanıcı_bilgi', aliases=['kullanici', 'kbilgi'], help='Bir kullanıcı hakkında bilgi gösterir. Kullanım: `e!kullanıcı_bilgi [@kullanıcı]`')
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def userinfo(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author # Eğer belirtilmezse komutu kullananı göster

    embed = discord.Embed(
        title=f"{member.display_name} Kullanıcı Bilgisi",
        description="İşte bu kullanıcı hakkında detaylar:",
        color=discord.Color.dark_purple()
    )
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)

    embed.add_field(name="Kullanıcı Adı", value=member.name, inline=True)
    if member.discriminator and member.discriminator != '0': # Eski etiket sistemi için
          embed.add_field(name="Etiket", value=member.discriminator, inline=True)
    else: # Yeni kullanıcı adı sistemi için
        embed.add_field(name="Tam Ad", value=member.global_name if member.global_name else member.name, inline=True)

    embed.add_field(name="ID", value=member.id, inline=False)
    embed.add_field(name="Oluşturulma Tarihi", value=member.created_at.strftime("%d.%m.%Y %H:%M:%S"), inline=True)
    embed.add_field(name="Sunucuya Katılma Tarihi", value=member.joined_at.strftime("%d.%m.%Y %H:%M:%S") if member.joined_at else "Bilinmiyor", inline=True)
    
    roles = [role.mention for role in member.roles if role.name != "@everyone"] # @everyone rolünü hariç tut
    if roles:
        # Maksimum 10 rol gösterelim, daha fazlası çok uzun olabilir
        if len(roles) > 10:
            roles_display = ", ".join(roles[:10]) + f" ve {len(roles) - 10} diğer rol..."
        else:
            roles_display = ", ".join(roles)
        embed.add_field(name="Roller", value=roles_display, inline=False)
    else:
        embed.add_field(name="Roller", value="Yok", inline=False)
    
    embed.add_field(name="Bot Mu?", value="Evet" if member.bot else "Hayır", inline=True)
    embed.add_field(name="Durum", value=str(member.status).capitalize(), inline=True) # Çevrimiçi, Boşta vb.

    await ctx.send(embed=embed)
    print(f"[{ctx.author}] e!kullanıcı_bilgi komutunu kullandı. Kullanıcı: {member.name}")

# --- Moderasyon Komutları ---

@bot.command(name='kick', help='Bir üyeyi sunucudan atar. Kullanım: `e!kick @kullanıcı [sebep]`')
@commands.has_permissions(kick_members=True) # Üye atma yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner() # Bot kilitli değilse veya sahipse çalışır
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def kick(ctx, member: discord.Member, *, reason: str = "Belirtilmemiş"):
    if member.id == ctx.author.id:
        await ctx.send("Kendinizi atamazsınız!")
        return
    if member.id == bot.user.id:
        await ctx.send("Beni atamazsın!")
        return
    if member.id == OWNER_ID: # Bot sahibini atma engeli
        await ctx.send("Bot sahibini atamazsınız!")
        return
    # Yetki hiyerarşisi kontrolü (sahip her zaman atabilir)
    if ctx.author.top_role <= member.top_role and ctx.author.id != ctx.guild.owner.id:
        await ctx.send("Bu üyeyi atmak için yeterli yetkiniz yok (rolünüz onunkinden düşük veya eşit).")
        return

    try:
        await member.kick(reason=reason)
        embed = discord.Embed(
            title="Üye Atıldı",
            description=f"{member.mention} sunucudan atıldı.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Sebep", value=reason, inline=False)
        embed.add_field(name="Yetkili", value=ctx.author.mention, inline=False)
        embed.set_footer(text=f"ID: {member.id}")
        await ctx.send(embed=embed)
        print(f"[{ctx.author}] '{member.name}' adlı üyeyi attı. Sebep: {reason}")
    except discord.Forbidden:
        await ctx.send("Bu üyeyi atmak için yetkim yok. Rol hiyerarşimi ve yetkilerimi kontrol edin.")
    except Exception as e:
        await ctx.send(f"Üye atarken bir hata oluştu: {e}")
        print(f"Kick hatası: {e}")

@bot.command(name='ban', help='Bir üyeyi sunucudan yasaklar. Kullanım: `e!ban @kullanıcı [sebep]`')
@commands.has_permissions(ban_members=True) # Üye yasaklama yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner() # Bot kilitli değilse veya sahipse çalışır
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def ban(ctx, member: discord.Member, *, reason: str = "Belirtilmemiş"):
    if member.id == ctx.author.id:
        await ctx.send("Kendinizi yasaklayamazsınız!")
        return
    if member.id == bot.user.id:
        await ctx.send("Beni yasaklayamazsın!")
        return
    if member.id == OWNER_ID: # Bot sahibini yasaklama engeli
        await ctx.send("Bot sahibini yasaklayamazsınız!")
        return
    # Yetki hiyerarşisi kontrolü (sahip her zaman yasaklayabilir)
    if ctx.author.top_role <= member.top_role and ctx.author.id != ctx.guild.owner.id:
        await ctx.send("Bu üyeyi yasaklamak için yeterli yetkiniz yok (rolünüz onunkinden düşük veya eşit).")
        return

    try:
        await member.ban(reason=reason)
        embed = discord.Embed(
            title="Üye Yasaklandı",
            description=f"{member.mention} sunucudan yasaklandı.",
            color=discord.Color.red()
        )
        embed.add_field(name="Sebep", value=reason, inline=False)
        embed.add_field(name="Yetkili", value=ctx.author.mention, inline=False)
        embed.set_footer(text=f"ID: {member.id}")
        await ctx.send(embed=embed)
        print(f"[{ctx.author}] '{member.name}' adlı üyeyi yasakladı. Sebep: {reason}")
    except discord.Forbidden:
        await ctx.send("Bu üyeyi yasaklamak için yetkim yok. Rol hiyerarşimi ve yetkilerimi kontrol edin.")
    except Exception as e:
        await ctx.send(f"Üye yasaklarken bir hata oluştu: {e}")
        print(f"Ban hatası: {e}")

@bot.command(name='unban', help='Yasaklı bir kullanıcının yasağını kaldırır. Kullanım: `e!unban <kullanıcı_ID> [sebep]`')
@commands.has_permissions(ban_members=True) # Üye yasaklama yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner() # Bot kilitli değilse veya sahipse çalışır
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def unban(ctx, user_id: int, *, reason: str = "Belirtilmemiş"):
    try:
        user = await bot.fetch_user(user_id) # ID'den kullanıcıyı çek
        await ctx.guild.unban(user, reason=reason)
        embed = discord.Embed(
            title="Yasak Kaldırıldı",
            description=f"{user.mention} ({user.name}) kullanıcısının yasağı kaldırıldı.",
            color=discord.Color.green()
        )
        embed.add_field(name="Sebep", value=reason, inline=False)
        embed.add_field(name="Yetkili", value=ctx.author.mention, inline=False)
        embed.set_footer(text=f"ID: {user.id}")
        await ctx.send(embed=embed)
        print(f"[{ctx.author}] '{user.name}' ({user.id}) adlı kullanıcının yasağını kaldırdı. Sebep: {reason}")
    except discord.NotFound:
        await ctx.send(f"ID'si `{user_id}` olan yasaklı bir kullanıcı bulunamadı.")
    except discord.Forbidden:
        await ctx.send("Yasak kaldırmak için yetkim yok.")
    except Exception as e:
        await ctx.send(f"Yasak kaldırırken bir hata oluştu: {e}")
        print(f"Unban hatası: {e}")

@bot.command(name='clear', aliases=['temizle'], help='Belirtilen sayıdaki mesajı siler. Kullanım: `e!clear <sayı>`')
@commands.has_permissions(manage_messages=True) # Mesajları yönetme yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner() # Bot kilitli değilse veya sahipse çalışır
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def clear(ctx, amount: int):
    if amount <= 0:
        await ctx.send("Lütfen 0'dan büyük bir sayı girin.")
        return
    if amount > 100: # Discord API limiti 100'dür (purge komutu tek seferde 100'den fazla silmez)
        await ctx.send("Bir seferde en fazla 100 mesaj silebilirsiniz.")
        amount = 100

    try:
        # ctx.message.channel.purge ile mesajları sil. limit=amount+1 kendi komut mesajını da siler
        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"✅ Başarıyla **{len(deleted) - 1}** mesaj silindi.", delete_after=5) # Kendi komut mesajı hariç
        print(f"[{ctx.author}] '{ctx.channel.name}' kanalında {len(deleted) - 1} mesaj sildi.")
    except discord.Forbidden:
        await ctx.send("Mesajları silmek için yetkim yok. Rol hiyerarşimi ve yetkilerimi kontrol edin.")
    except Exception as e:
        await ctx.send(f"Mesaj silerken bir hata oluştu: {e}")
        print(f"Clear hatası: {e}")

# --- Kanal Duyuru Komutu (Özel Kanala Gönderir) ---
@bot.command(name='kanala_mesaj', help='Belirtilen kanala embed mesajı gönderir. Kullanım: `e!kanala_mesaj #kanal <mesajınız>` (Kanal Yönetme yetkisi gerekir)')
@commands.has_permissions(manage_channels=True) # Kanal yönetme yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def send_message_to_channel(ctx, channel: discord.TextChannel, *, message_content: str):
    """
    Belirtilen kanala kullanıcı tarafından sağlanan metni bir embed içinde gönderir.
    """
    if not message_content.strip():
        await ctx.send("Lütfen gönderilecek mesaj içeriğini belirtin.")
        return

    try:
        embed = discord.Embed(
            title="Kanal Duyurusu",
            description=message_content,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Gönderen: {ctx.author.display_name}")
        embed.timestamp = datetime.now(timezone.utc)

        await channel.send(embed=embed)
        await ctx.send(f"Mesaj başarıyla {channel.mention} kanalına gönderildi!")
        print(f"[{ctx.author}] '{channel.name}' kanalına bir mesaj gönderdi. İçerik: '{message_content[:50]}...'")

    except discord.Forbidden:
        await ctx.send(f"**Hata:** {channel.mention} kanalına mesaj gönderme yetkim yok. Kanal yetkilerimi kontrol edin.")
    except Exception as e:
        await ctx.send(f"Bir hata oluştu: {e}")
        print(f"Kanal mesajı gönderme hatası: {e}")

# --- Duyuru Komutu (Sadece Kullanıldığı Kanala Gönderir) ---
@bot.command(name='duyuru', help='Kullanıldığı kanala gönderen bilgisiyle embed duyuru mesajı atar. Kullanım: `e!duyuru <mesajınız>` (Mesajları Yönet yetkisi gerekir)')
@commands.has_permissions(manage_messages=True) # Mesajları Yönet yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def duyuru(ctx, *, message: str):
    """
    Komutun kullanıldığı kanala, gönderen bilgisiyle birlikte bir embed duyuru mesajı gönderir.
    """
    if not message.strip():
        await ctx.send("Lütfen duyuru içeriğini belirtin.")
        return

    try:
        embed = discord.Embed(
            title="📣 Yeni Duyuru!",
            description=message,
            color=discord.Color.dark_blue() # Duyurular için farklı bir renk kullanabilirsiniz
        )
        # Gönderen bilgisi ve profil resmi
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url)
        embed.set_footer(text=f"Duyuran: {ctx.author.display_name} • {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')}")
        embed.timestamp = datetime.now(timezone.utc)

        # Mesajı direkt komutun kullanıldığı kanala gönderiyoruz
        await ctx.send(embed=embed)
        
        # Kullanıcının komut mesajını silebiliriz, duyuru embed'i yeterli
        await ctx.message.delete() 

        print(f"[{ctx.author}] '{ctx.channel.name}' kanalına bir duyuru gönderdi. İçerik: '{message[:50]}...'")

    except discord.Forbidden:
        await ctx.send(f"**Hata:** {ctx.channel.mention} kanalına mesaj gönderme yetkim yok. Kanal yetkilerimi kontrol edin.")
    except Exception as e:
        await ctx.send(f"Duyuru gönderirken bir hata oluştu: {e}")
        print(f"Duyuru komutu hatası: {e}")

# --- Eğlence Komutları ---

@bot.command(name='zar', help='Rastgele bir sayı atar. Kullanım: `e!zar` veya `e!zar <max_sayı>`')
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def zar(ctx, max_number: int = 6):
    if max_number <= 1:
        await ctx.send("Zar atışı için maksimum sayı 1'den büyük olmalıdır.")
        return
    result = random.randint(1, max_number)
    await ctx.send(f"🎲 Zar atıldı! Sonuç: **{result}**")
    print(f"[{ctx.author}] e!zar komutunu kullandı. Sonuç: {result}")

@bot.command(name='yazıtura', aliases=['yt'], help='Yazı tura atar.')
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def yazitura(ctx):
    choices = ["Yazı", "Tura"]
    result = random.choice(choices)
    await ctx.send(f"🪙 Yazı tura atıldı! Sonuç: **{result}**")
    print(f"[{ctx.author}] e!yazıtura komutunu kullandı. Sonuç: {result}")

@bot.command(name='8ball', aliases=['sekiztop'], help='Sihirli 8 topa soru sorun. Kullanım: `e!8ball <sorunuz>`')
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def eightball(ctx, *, question: str):
    responses = [
        "Kesinlikle.",
        "Hiç şüphe yok.",
        "Buna güvenebilirsin.",
        "Evet, kesinlikle.",
        "Gördüğüm kadarıyla evet.",
        "Büyük olasılıkla.",
        "İyi görünüyor.",
        "Evet.",
        "İşaretler evet diyor.",
        "Şüpheli, tekrar dene.",
        "Şimdi söylemek zor.",
        "Bunu tahmin edemem.",
        "Odaklan ve tekrar sor.",
        "Buna güvenme.",
        "Cevabım hayır.",
        "Kaynaklarım hayır diyor.",
        "Hiç iyi görünmüyor.",
        "Çok şüpheli."
    ]
    if not question.strip().endswith('?'):
        question = question.strip() + '?'
    embed = discord.Embed(
        title="🎱 Sihirli 8 Top",
        color=discord.Color.purple()
    )
    embed.add_field(name="Sorunuz", value=question, inline=False)
    embed.add_field(name="Cevap", value=random.choice(responses), inline=False)
    await ctx.send(embed=embed)
    print(f"[{ctx.author}] e!8ball komutunu kullandı. Soru: '{question}'")


# --- Ayar Komutları ---

@bot.command(name='ayarla_hosgeldin', help='Hoş geldin mesajlarının gönderileceği kanalı ayarlar. Kullanım: `e!ayarla_hosgeldin #kanal`')
@commands.has_permissions(manage_guild=True) # Sunucuyu yönetme yetkisi olanlar kullanabilir
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def set_welcome_channel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("INSERT OR REPLACE INTO guild_settings (guild_id, welcome_channel_id) VALUES (?, ?)",
                         (ctx.guild.id, channel.id))
        await db.commit()
    await ctx.send(f"✅ Hoş geldin mesajları artık {channel.mention} kanalına gönderilecek.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda hoş geldin kanalını '{channel.name}' olarak ayarladı.")

@bot.command(name='sifirla_hosgeldin', help='Hoş geldin mesajlarının gönderileceği kanalı sıfırlar.')
@commands.has_permissions(manage_guild=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def reset_welcome_channel(ctx):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (ctx.guild.id,))
        await db.commit()
    await ctx.send("✅ Hoş geldin kanalı ayarı sıfırlandı. Artık hoş geldin mesajı gönderilmeyecek.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda hoş geldin kanalını sıfırladı.")

@bot.command(name='reaksiyon_rolu_ayarla', help='Reaksiyon rolü mesajı oluşturur. Kullanım: `e!reaksiyon_rolu_ayarla <mesaj_id> <emoji> <@rol>`')
@commands.has_permissions(manage_roles=True) # Rolleri yönetme yetkisi olanlar kullanabilir
@commands.bot_has_permissions(manage_roles=True) # Botun rol yönetme yetkisi olmalı
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def set_reaction_role(ctx, message_id: int, emoji: str, role: discord.Role):
    try:
        message = await ctx.channel.fetch_message(message_id)
    except discord.NotFound:
        return await ctx.send("Belirtilen ID'ye sahip mesaj bulunamadı.")
    except discord.Forbidden:
        return await ctx.send("Mesajları okuma yetkim yok.")

    # Emoji kontrolü (basit bir kontrol, custom emojiler için daha karmaşık olabilir)
    if not discord.utils.get(ctx.guild.emojis, name=emoji.strip(':')) and not (emoji.startswith('<:') or emoji.startswith('<a:')) and not len(emoji) == 1:
        # Eğer özel bir emoji değilse ve tek karakterli normal bir emoji de değilse
        return await ctx.send("Lütfen geçerli bir emoji girin. (Örnek: 👍 veya <:emoji_adı:ID>)")
    
    # Botun rol hiyerarşisi kontrolü
    if ctx.guild.me.top_role <= role:
        return await ctx.send(f"Ayarlamaya çalıştığınız '{role.name}' rolü, benim rolümden yüksek veya eşit. Bu rolü atayamam.")

    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("INSERT OR REPLACE INTO reaction_roles (guild_id, message_id, emoji, role_id) VALUES (?, ?, ?, ?)",
                         (ctx.guild.id, message_id, emoji, role.id))
        await db.commit()
    
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        return await ctx.send("Emojiye tepki eklerken bir hata oluştu. Belki de bu emojiye tepki ekleyemiyorum?")

    await ctx.send(f"✅ Mesaj ID `{message_id}` için `{emoji}` reaksiyonu `{role.name}` rolü ile ayarlandı.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda reaksiyon rolü ayarladı: Mesaj ID {message_id}, Emoji: {emoji}, Rol: {role.name}")

@bot.event
async def on_raw_reaction_add(payload):
    if payload.member.bot:
        return # Bot kendi reaksiyonlarını yok say

    async with aiosqlite.connect('bot_settings.db') as db:
        cursor = await db.execute("SELECT role_id FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
                                 (payload.guild_id, payload.message_id, str(payload.emoji)))
        result = await cursor.fetchone()

    if result:
        guild = bot.get_guild(payload.guild_id)
        if not guild: return
        
        role = guild.get_role(result[0])
        member = guild.get_member(payload.user_id) # payload.member kullanamıyoruz, raw event
        
        if role and member:
            try:
                # Botun rol hiyerarşisi kontrolü
                if guild.me.top_role <= role:
                    print(f"[Reaksiyon Rolü Hatası] Botun rolü '{role.name}' rolünden düşük. Rol verilemedi.")
                    return # Rol verilemiyorsa devam etme

                await member.add_roles(role)
                print(f"[Reaksiyon Rolü] {member.name} kullanıcısına '{role.name}' rolü verildi.")
            except discord.Forbidden:
                print(f"[Reaksiyon Rolü Hatası] '{member.name}' kullanıcısına '{role.name}' rolü verme yetkim yok.")
            except Exception as e:
                print(f"[Reaksiyon Rolü Hatası] Rol verirken hata: {e}")

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.member and payload.member.bot:
        return # Bot kendi reaksiyonlarını yok say

    async with aiosqlite.connect('bot_settings.db') as db:
        cursor = await db.execute("SELECT role_id FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
                                 (payload.guild_id, payload.message_id, str(payload.emoji)))
        result = await cursor.fetchone()

    if result:
        guild = bot.get_guild(payload.guild_id)
        if not guild: return

        role = guild.get_role(result[0])
        member = guild.get_member(payload.user_id) # payload.member kullanamıyoruz, raw event

        if role and member:
            try:
                # Botun rol hiyerarşisi kontrolü
                if guild.me.top_role <= role:
                    print(f"[Reaksiyon Rolü Hatası] Botun rolü '{role.name}' rolünden düşük. Rol kaldırılamadı.")
                    return # Rol kaldırılamıyorsa devam etme

                await member.remove_roles(role)
                print(f"[Reaksiyon Rolü] {member.name} kullanıcısından '{role.name}' rolü kaldırıldı.")
            except discord.Forbidden:
                print(f"[Reaksiyon Rolü Hatası] '{member.name}' kullanıcısından '{role.name}' rolünü kaldırma yetkim yok.")
            except Exception as e:
                print(f"[Reaksiyon Rolü Hatası] Rol kaldırırken hata: {e}")


@bot.command(name='sessiz_kanal_ayarla', help='Belirtilen kanalı sessiz moda alır. Bot bu kanalda komutlara yanıt vermez. Kullanım: `e!sessiz_kanal_ayarla #kanal`')
@commands.has_permissions(manage_channels=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Bu komutun kendisi sessiz kanalda ayarlanamasın
async def set_silent_channel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("INSERT OR IGNORE INTO silent_channels (channel_id, guild_id) VALUES (?, ?)",
                         (channel.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ {channel.mention} kanalı artık sessiz moda alındı. Bot bu kanalda komutlara yanıt vermeyecek.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda '{channel.name}' kanalını sessiz olarak ayarladı.")

@bot.command(name='sessiz_kanal_sifirla', help='Belirtilen kanalı sessiz moddan çıkarır. Kullanım: `e!sessiz_kanal_sifirla #kanal`')
@commands.has_permissions(manage_channels=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Bu komutun kendisi sessiz kanalda sıfırlanamasın
async def reset_silent_channel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("DELETE FROM silent_channels WHERE channel_id = ?", (channel.id,))
        await db.commit()
    await ctx.send(f"✅ {channel.mention} kanalı sessiz moddan çıkarıldı. Bot artık bu kanalda komutlara yanıt verecek.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda '{channel.name}' kanalını sessiz moddan çıkardı.")

@bot.command(name='otorol_ayarla', help='Sunucuya yeni katılan üyelere otomatik olarak rol atar. Kullanım: `e!otorol_ayarla @rol`')
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def set_autorole(ctx, role: discord.Role):
    # Botun rol hiyerarşisi kontrolü
    if ctx.guild.me.top_role <= role:
        await ctx.send(f"Ayarlamaya çalıştığınız '{role.name}' rolü, benim rolümden yüksek veya eşit. Bu rolü atayamam.")
        return
    
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("INSERT OR REPLACE INTO autoroles (guild_id, role_id) VALUES (?, ?)",
                         (ctx.guild.id, role.id))
        await db.commit()
    await ctx.send(f"✅ Yeni katılan üyelere otomatik olarak `{role.name}` rolü verilecek.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda otorolü '{role.name}' olarak ayarladı.")

@bot.command(name='otorol_sifirla', help='Otorol ayarını sıfırlar.')
@commands.has_permissions(manage_roles=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel() # Sessiz kanalda çalışmasın
async def reset_autorole(ctx):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("DELETE FROM autoroles WHERE guild_id = ?", (ctx.guild.id,))
        await db.commit()
    await ctx.send("✅ Otorol ayarı sıfırlandı. Artık yeni üyelere otomatik rol verilmeyecek.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda otorolü sıfırladı.")

# --- Ticket Sistemi Komutları ---

@bot.command(name='ayarla_ticket', help='Ticket sistemini ayarlar. Kullanım: `e!ayarla_ticket <#kategori_kanal_adı> <#log_kanalı_adı> <@moderatör_rolü>`')
@commands.has_permissions(manage_channels=True, manage_roles=True)
@commands.bot_has_permissions(manage_channels=True, manage_roles=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel()
async def setup_ticket(ctx, category: discord.CategoryChannel, log_channel: discord.TextChannel, mod_role: discord.Role):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("INSERT OR REPLACE INTO ticket_settings (guild_id, ticket_category_id, ticket_log_channel_id, ticket_moderator_role_id) VALUES (?, ?, ?, ?)",
                         (ctx.guild.id, category.id, log_channel.id, mod_role.id))
        await db.commit()
    await ctx.send(f"✅ Ticket sistemi başarıyla ayarlandı:\n"
                   f"Kategori: {category.mention}\n"
                   f"Log Kanalı: {log_channel.mention}\n"
                   f"Moderatör Rolü: {mod_role.mention}")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda ticket sistemini ayarladı.")

@bot.command(name='gönder_ticket_butonu', help='Ticket açma butonunu belirtilen kanala gönderir. Kullanım: `e!gönder_ticket_butonu #kanal`')
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(send_messages=True, embed_links=True)
@check_bot_unlocked_or_owner()
@check_not_silent_channel()
async def send_ticket_button(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect('bot_settings.db') as db:
        cursor = await db.execute("SELECT ticket_moderator_role_id FROM ticket_settings WHERE guild_id = ?", (ctx.guild.id,))
        result = await cursor.fetchone()
        if not result:
            return await ctx.send("Ticket sistemi bu sunucuda ayarlanmamış. Lütfen önce `e!ayarla_ticket` komutunu kullanın.")
        mod_role_id = result[0]

    embed = discord.Embed(
        title="Destek Talebi Oluştur",
        description="Aşağıdaki butona tıklayarak bir destek talebi oluşturabilirsiniz. Lütfen sorununuzu açıkça belirtin.",
        color=discord.Color.blue()
    )
    # Butonun gönderildiği yerde TicketView'ı başlatıyoruz
    # Her mesaja özel bir View instance'ı kullanırız, bu View'ın kendisi kalıcı olmalıdır.
    view = TicketView(bot_instance=bot, mod_role_id=mod_role_id)
    await channel.send(embed=embed, view=view)
    await ctx.send(f"✅ Ticket açma butonu {channel.mention} kanalına gönderildi.")
    print(f"[{ctx.author}] '{ctx.guild.name}' sunucusunda ticket açma butonunu '{channel.name}' kanalına gönderdi.")


# --- Bot Sahibi Komutları (Hidden) ---

@bot.command(name='kapat', help='Botu kapatır.', hidden=True)
@commands.is_owner() # Sadece bot sahibi kullanabilir
async def shutdown(ctx):
    await ctx.send("Kapanıyorum...")
    print(f"[{ctx.author}] Botu kapattı.")
    await bot.close()

@bot.command(name='değiştir_durum', help='Botun durumunu değiştirir. Kullanım: `e!değiştir_durum <oynuyor|dinliyor|izliyor> <mesaj>`', hidden=True)
@commands.is_owner()
async def change_status(ctx, activity_type: str, *, message: str):
    activity_type = activity_type.lower()
    if activity_type == 'oynuyor':
        activity = discord.Game(name=message)
    elif activity_type == 'dinliyor':
        activity = discord.Activity(type=discord.ActivityType.listening, name=message)
    elif activity_type == 'izliyor':
        activity = discord.Activity(type=discord.ActivityType.watching, name=message)
    else:
        await ctx.send("Geçersiz etkinlik türü. `oynuyor`, `dinliyor` veya `izliyor` kullanın.")
        return

    await bot.change_presence(activity=activity)
    await ctx.send(f"Botun durumu başarıyla `{activity_type.capitalize()}: {message}` olarak ayarlandı.")
    print(f"[{ctx.author}] Botun durumunu '{activity_type}: {message}' olarak değiştirdi.")

@bot.command(name='kilitle_bot', help='Botun tüm komutlarını (sahibe özeller hariç) kilitler.', hidden=True)
@commands.is_owner()
async def lock_bot(ctx):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("REPLACE INTO bot_status (status_name, is_locked) VALUES (?, ?)", ('command_lock', 1))
        await db.commit()
    await ctx.send("🔒 Botun komutları kilitlendi. Sadece bot sahibi komutları kullanabilir.")
    print(f"[{ctx.author}] Botun komutlarını kilitledi.")

@bot.command(name='kilidi_aç_bot', help='Botun komutlarının kilidini açar.', hidden=True)
@commands.is_owner()
async def unlock_bot(ctx):
    async with aiosqlite.connect('bot_settings.db') as db:
        await db.execute("REPLACE INTO bot_status (status_name, is_locked) VALUES (?, ?)", ('command_lock', 0))
        await db.commit()
    await ctx.send("🔓 Botun komutlarının kilidi açıldı. Herkes komutları kullanabilir.")
    print(f"[{ctx.author}] Botun komutlarının kilidini açtı.")


# Botu çalıştır
bot.run(TOKEN)