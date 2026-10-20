import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import os

# ==========================================
# 設定（環境変数から安全に読み込む）
# ==========================================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
PAYPAY_ID = os.environ.get("PAYPAY_ID")
PAYPAY_NAME = os.environ.get("PAYPAY_NAME")
LOG_CHANNEL_ID = 0  # ログを送るチャンネルID。不要なら0のままでOK

DB = "vending.db"
bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

# ==========================================
# DB初期化
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price INTEGER NOT NULL DEFAULT 0,
            stock INTEGER NOT NULL DEFAULT 0,
            type TEXT NOT NULL,
            payload TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            price INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS topup_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_by INTEGER,
            reviewed_at TEXT
        );
        """)
        await db.commit()

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    print(f"ログインしました: {bot.user}")

# ==========================================
# 管理者用チェック
# ==========================================
def is_admin():
    return app_commands.check(lambda i: i.user.guild_permissions.administrator)

# ==========================================
# 一般ユーザー用コマンド
# ==========================================
class VendingView(discord.ui.View):
    def __init__(self, products):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=p["name"][:100],
                value=str(p["id"]),
                description=f"{p['price']}コイン / 在庫{'∞' if p['stock']==-1 else p['stock']}",
            ) for p in products[:25]
        ]
        self.add_item(ProductSelect(options))

class ProductSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="商品を選んでください", options=options)

    async def callback(self, interaction: discord.Interaction):
        pid = int(self.values[0])
        await purchase(interaction, pid)

@bot.tree.command(name="vending", description="自販機を表示します")
async def vending(interaction: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM products WHERE active=1 AND (stock=-1 OR stock>0)")).fetchall()
    if not rows:
        return await interaction.response.send_message("現在売り切れです", ephemeral=True)
    await interaction.response.send_message("🛒 **自販機**", view=VendingView(rows))

async def purchase(interaction, pid):
    uid = interaction.user.id
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            p = await (await db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,))).fetchone()
            if not p: raise ValueError("商品が見つかりません")

            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
            u = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (uid,))).fetchone()
            if u["balance"] < p["price"]:
                raise ValueError(f"残高不足（必要: {p['price']} / 所持: {u['balance']}）")

            if p["stock"] != -1:
                cur = await db.execute("UPDATE products SET stock=stock-1 WHERE id=? AND stock>0", (pid,))
                if cur.rowcount == 0: raise ValueError("売り切れです")

            await db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (p["price"], uid))
            await db.execute("?,?)", (uid, pid, p["price"]))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    try:
        if p["type"] == "role":
            role = interaction.guild.get_role(int(p["payload"]))
            await interaction.user.add_roles(role)
            msg = f"✅ **{p['name']}** を購入！ロールを付与しました。"
        else:
            msg = f"✅ **{p['name']}** を購入！\n{p['payload']}"
    except Exception:
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (p["price"], uid))
            await db.execute("UPDATE products SET stock=stock+1 WHERE id=? AND stock!=-1", (pid,))
            await db.commit()
        return await interaction.response.send_message("❌ 付与に失敗したため返金しました", ephemeral=True)

    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="balance", description="残高を確認します")
async def balance(interaction: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        row = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))).fetchone()
    await interaction.response.send_message(f"💰 残高: **{row[0] if row else 0}** コイン", ephemeral=True)

@bot.tree.command(name="charge", description="PayPayで残高をチャージ申請します")
async def charge(interaction: discord.Interaction, amount: int, note: str = ""):
    if amount <= 0 or amount > 100000:
        return await interaction.response.send_message("金額が不正です", ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("INSERT INTO topup_requests(user_id, amount, note) VALUES(?,?,?)", (interaction.user.id, amount, note))
        await db.commit()
        req_id = cur.lastrowid

    embed = discord.Embed(title="💰 チャージ申請を受け付けました", color=0x00bfff)
    embed.add_field(name="申請ID", value=str(req_id), inline=False)
    embed.add_field(name="金額", value=f"{amount}円", inline=True)
    embed.add_field(name="送金先 PayPay ID", value=PAYPAY_ID, inline=False)
    embed.add_field(name="送金名義", value=PAYPAY_NAME, inline=False)
    embed.add_field(name="次の手順", value=f"① PayPayで **{amount}円** を送金\n② 送金完了後、申請ID **{req_id}** を管理者に伝えてください", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

    if LOG_CHANNEL_ID != 0:
        log = bot.get_channel(LOG_CHANNEL_ID)
        if log:
            await log.send(f"📥 **新規チャージ申請** ID:{req_id}\nユーザー: {interaction.user.mention} ({interaction.user.id})\n金額: {amount}円 / メモ: {note or 'なし'}")

# ==========================================
# 管理者用コマンド
# ==========================================
@bot.tree.command(name="product_add", description="商品を追加します")
@is_admin()
@app_commands.choices(type=[
    app_commands.Choice(name="ロール付与", value="role"),
    app_commands.Choice(name="テキスト/リンク", value="text"),
])
async def product_add(interaction: discord.Interaction, name: str, price: int, stock: int, type: app_commands.Choice[str], payload: str, description: str = ""):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO products(name,description,price,stock,type,payload) VALUES(?,?,?,?,?,?)", (name, description, price, stock, type.value, payload))
        await db.commit()
    await interaction.response.send_message(f"✅ 追加: {name}（在庫{stock}）", ephemeral=True)

@bot.tree.command(name="product_stock", description="在庫数を変更します")
@is_admin()
async def product_stock(interaction: discord.Interaction, product_id: int, stock: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE products SET stock=? WHERE id=?", (stock, product_id))
        await db.commit()
    await interaction.response.send_message(f"📦 ID{product_id} の在庫を {stock} に変更", ephemeral=True)

@bot.tree.command(name="product_remove", description="商品を削除/非表示にします")
@is_admin()
async def product_remove(interaction: discord.Interaction, product_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE products SET active=0 WHERE id=?", (product_id,))
        await db.commit()
    await interaction.response.send_message(f"🗑️ ID{product_id} を非表示にしました", ephemeral=True)

@bot.tree.command(name="product_list", description="商品一覧を確認します")
@is_admin()
async def product_list(interaction: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute("SELECT id,name,price,stock,active FROM products")).fetchall()
    txt = "\n".join(f"ID{r[0]} | {r[1]} | {r[2]}コイン | 在庫{r[3]} | {'有効' if r[4] else '無効'}" for r in rows)
    await interaction.response.send_message(txt[:1900] or "商品なし", ephemeral=True)

@bot.tree.command(name="approve", description="チャージ申請を承認して残高加算")
@is_admin()
async def approve(interaction: discord.Interaction, request_id: int):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            r = await (await db.execute("SELECT * FROM topup_requests WHERE id=?", (request_id,))).fetchone()
            if not r or r["status"] != "pending": raise ValueError("無効な申請です")
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (r["user_id"],))
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (r["amount"], r["user_id"]))
            await db.execute("UPDATE topup_requests SET status='approved', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (interaction.user.id, request_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    try:
        user = await bot.fetch_user(r["user_id"])
        await user.send(f"✅ {r['amount']}円 のチャージが承認され、残高に加算されました。")
    except: pass

    await interaction.response.send_message(f"✅ 申請ID{request_id}: <@{r['user_id']}> に {r['amount']}円 を加算しました。", ephemeral=True)

@bot.tree.command(name="pending", description="未処理のチャージ申請一覧")
@is_admin()
async def pending(interaction: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute("SELECT id,user_id,amount,note,created_at FROM topup_requests WHERE status='pending' ORDER BY id")).fetchall()
    if not rows:
        return await interaction.response.send_message("未処理なし", ephemeral=True)
    txt = "\n".join(f"ID{r[0]} | <@{r[1]}> | {r[2]}円 | {r[3] or '-'} | {r[4]}" for r in rows)
    await interaction.response.send_message(txt[:1900], ephemeral=True)

# ==========================================
# 起動
# ==========================================
bot.run(DISCORD_TOKEN)
