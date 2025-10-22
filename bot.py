# bot.py — 統合版（自販機 / 配布 / PayPay / 認証 / チケット 永続化対応）
import discord
import asyncio
from discord.ext import commands
from discord import app_commands, Interaction, Embed, ButtonStyle, ui
import json, os
from datetime import datetime, timedelta, timezone
from PayPaython_mobile import PayPay, PayPayLoginError
from Crypto.Cipher import AES
from base64 import b64encode, b64decode
import hashlib
from dotenv import load_dotenv
from typing import Optional
from ticket import TicketView
import os
import sys
import time

# ------------------------
# 設定
# ------------------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN") or "YOUR_TOKEN"  # .env に BOT_TOKEN=... を入れるか、この行を直接書き換えてください
BASE_DATA_DIR = "data"
GLOBAL_LOG_CHANNEL_ID = 1373603824773763092
DISCORD_CHANNEL = 1373603824773763092

intents = discord.Intents.all()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ------------------------
# ユーティリティ関数
# ------------------------
def get_data_path(guild_id: int, filename: str) -> str:
    guild_dir = os.path.join(BASE_DATA_DIR, str(guild_id))
    os.makedirs(guild_dir, exist_ok=True)
    return os.path.join(guild_dir, filename)

def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def encrypt_token(token, key):
    key = hashlib.sha256(key.encode()).digest()
    cipher = AES.new(key, AES.MODE_EAX)
    ciphertext, tag = cipher.encrypt_and_digest(token.encode())
    return b64encode(cipher.nonce + tag + ciphertext).decode()

def decrypt_token(encrypted: str, key: str) -> str:
    key = hashlib.sha256(key.encode()).digest()
    data = b64decode(encrypted)
    nonce, tag, ciphertext = data[:16], data[16:32], data[32:]
    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode()

def is_already_used(link_id: str, guild_id: int) -> bool:
    used_links = load_json(get_data_path(guild_id, "used_links.json"))
    return link_id in used_links

def mark_link_as_used(link_id: str, guild_id: int):
    used_links = load_json(get_data_path(guild_id, "used_links.json"))
    used_links[link_id] = True
    save_json(get_data_path(guild_id, "used_links.json"), used_links)

def format_item_stock_display(item: dict) -> str:
    if "accounts" in item:
        if item.get("stock", 0) > 0 and item.get("accounts"):
            return str(item["stock"])
        else:
            return "在庫なし"
    if str(item.get("stock")) in ("∞", "∞️"):
        return "∞"
    try:
        stock = int(item.get("stock", 0))
        return str(stock) if stock > 0 else "在庫なし"
    except Exception:
        return "在庫エラー"

# ------------------------
# ビュー／ボタン類（自販機・配布系）
# ------------------------
class PurchaseView(ui.View):
    def __init__(self, panel_id):
        super().__init__(timeout=None)
        self.add_item(PurchaseButton(panel_id))
        self.add_item(CheckStockButton(panel_id))

class PurchaseButton(ui.Button):
    def __init__(self, panel_id):
        super().__init__(label="購入する", style=discord.ButtonStyle.green, custom_id=f"purchase:{panel_id}")
        self.panel_id = panel_id

    async def callback(self, interaction: Interaction):
        guild_id = interaction.guild_id
        items = load_json(get_data_path(guild_id, "items.json"))
        options = []
        for item_id, item in items.items():
            if item.get("panel_id") != self.panel_id:
                continue
            if "accounts" in item:
                if item.get("stock", 0) > 0 and item.get("accounts"):
                    stock_display = str(item["stock"])
                else:
                    continue
            else:
                stock_display = "♾️" if item.get("stock", 0) == 0 else f"残: {item.get('stock')}"
            options.append(discord.SelectOption(label=item.get("name", "不明"), value=item_id, description=f"¥{item.get('price',0)} {stock_display}"))

        if not options:
            await interaction.response.send_message("在庫がありません。", ephemeral=True)
            return

        view = ItemSelectView(options)
        await interaction.response.send_message("商品を選んでください", view=view, ephemeral=True)

class CheckStockButton(ui.Button):
    def __init__(self, panel_id):
        super().__init__(label="在庫確認", style=discord.ButtonStyle.blurple, custom_id=f"stock:{panel_id}")
        self.panel_id = panel_id

    async def callback(self, interaction: Interaction):
        guild_id = interaction.guild_id
        items = load_json(get_data_path(guild_id, "items.json"))
        item_texts = []
        count = 1
        for item in items.values():
            if item.get("panel_id") == self.panel_id:
                stock_display = format_item_stock_display(item)
                item_texts.append(f"{count}. {item.get('name','不明')}\n{item.get('description','説明なし')}\n価格: ¥{item.get('price',0)}（在庫: {stock_display}）")
                count += 1
        embed = Embed(title="📦在庫一覧", description="\n\n".join(item_texts) if item_texts else "商品がありません。", color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ItemSelectView(ui.View):
    def __init__(self, options):
        super().__init__(timeout=None)
        select = ui.Select(placeholder="商品を選択", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: Interaction):
        selected_id = interaction.data['values'][0]
        await interaction.response.send_modal(PayModal(selected_id, interaction.user.id))

# ------------------------
# 支払いモーダル（PayPay） + ロジック
# ------------------------
class PayModal(ui.Modal, title="支払い情報を入力"):
    def __init__(self, item_id, buyer_id):
        super().__init__()
        self.item_id = item_id
        self.buyer_id = buyer_id
        self.link_input = ui.TextInput(label="PayPayリンク", required=True)
        self.count_input = ui.TextInput(label="購入個数", placeholder="例: 1", required=True)
        self.password_input = ui.TextInput(label="パスワード（任意）", required=False)
        self.add_item(self.link_input)
        self.add_item(self.count_input)
        self.add_item(self.password_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        items = load_json(get_data_path(guild_id, "items.json"))
        config = load_json(get_data_path(guild_id, "config.json"))
        pay_link = self.link_input.value.strip()
        link_id = pay_link.rstrip("/").split("/")[-1]

        item = items.get(self.item_id)
        if not item:
            return await interaction.followup.send("指定された商品が存在しません。", ephemeral=True)

        item_name = item.get("name", "不明な商品")
        try:
            item_price = int(item.get("price", 0))
        except Exception:
            item_price = 0

        try:
            purchase_count = int(self.count_input.value.strip())
        except ValueError:
            return await interaction.followup.send("購入個数は数値で入力してください。", ephemeral=True)

        total_price = item_price * purchase_count

        embed = Embed(title="お支払い状況", description="🕒支払いリンクを確認中...", color=discord.Color.orange())
        status_msg = await interaction.followup.send(embed=embed, ephemeral=True)

        secret_key = os.getenv("TOKEN_ENCRYPT_KEY")
        try:
            access_token = decrypt_token(config["paypay_access_token"], secret_key)
        except Exception as e:
            return await status_msg.edit(embed=Embed(title="復号エラー", description=f"アクセストークンの復号に失敗しました。\n```\n{e.__class__.__name__}: {str(e)}\n```", color=discord.Color.red()))

        # PayPay 接続
        try:
            paypay = PayPay(access_token=access_token)
            paypay.alive()
            profile = paypay.get_profile()
            paypay.user_id = getattr(profile, "userId", None) or getattr(profile, "externalId", None)
        except PayPayLoginError:
            if "paypay_refresh_token" in config:
                try:
                    refresh_token = decrypt_token(config["paypay_refresh_token"], secret_key)
                    temp = PayPay()
                    temp.token_refresh(refresh_token)
                    config["paypay_access_token"] = encrypt_token(temp.access_token, secret_key)
                    config["paypay_refresh_token"] = encrypt_token(temp.refresh_token, secret_key)
                    save_json(get_data_path(guild_id, "config.json"), config)
                    paypay = PayPay(access_token=temp.access_token)
                except Exception:
                    return await status_msg.edit(embed=Embed(title="トークン更新失敗", description="リフレッシュトークンでの再ログインに失敗しました。", color=discord.Color.red()))
            else:
                return await status_msg.edit(embed=Embed(title="ログイン失敗", description="アクセストークンが無効で、リフレッシュトークンも存在しません。", color=discord.Color.red()))

        # 支払いリンク確認
        try:
            link_info = paypay.link_check(pay_link)
            sender_name = link_info.get("payload", {}).get("sender", {}).get("displayName", "不明な送信者")
            sender_icon = link_info.get("payload", {}).get("sender", {}).get("photoUrl", None)
            sender_id = link_info.get("payload", {}).get("sender", {}).get("externalId", "不明なID")
            amount = link_info.get("payload", {}).get("pendingP2PInfo", {}).get("amount", 0)
            status = link_info.get("payload", {}).get("message", {}).get("data", {}).get("status", "UNKNOWN")

            if amount <= 0:
                raise ValueError("支払い金額が取得できませんでした。")

            updated_embed = Embed(title="お支払い状況", description="🕒支払い確認中...", color=discord.Color.orange())
            updated_embed.add_field(name="PayPay送信者", value=f"```{sender_name}```", inline=False)
            updated_embed.add_field(name="送信者ID", value=f"```{sender_id}```", inline=False)
            updated_embed.add_field(name="金額", value=f"```¥{amount}```", inline=False)
            if sender_icon:
                updated_embed.set_thumbnail(url=sender_icon)
            await status_msg.edit(embed=updated_embed)

        except Exception as e:
            return await status_msg.edit(embed=Embed(title="リンクエラー", description=f"支払いリンクの確認に失敗しました。\n```\n{str(e)}\n```", color=discord.Color.red()))

        if amount < total_price:
            return await status_msg.edit(embed=Embed(title="支払い金額不足", description=f"必要金額: {total_price}円\n支払金額: {amount}円", color=discord.Color.red()))

        if status in ["COMPLETED", "SUCCESS"]:
            mark_link_as_used(link_id, guild_id)
            return await status_msg.edit(embed=Embed(title="受け取り済みリンク", description="このリンクはすでに受け取り済みです。", color=discord.Color.red()))

        # 受け取り処理
        receive_password = (self.password_input.value or "").strip() or None
        try:
            if receive_password:
                paypay.link_receive(pay_link, receive_password)
            else:
                paypay.link_receive(pay_link)
        except Exception as e:
            return await status_msg.edit(embed=Embed(title="受け取り失敗", description=f"支払いの受け取りに失敗しました。\n```\n{str(e)}\n```", color=discord.Color.red()))

        # 商品送信
        try:
            buyer = await bot.fetch_user(self.buyer_id)
            success = await give_item_automatically(buyer, guild_id, self.item_id, count=purchase_count)
        except Exception as e:
            return await status_msg.edit(embed=Embed(title="商品送信失敗", description=f"商品送信時にエラーが発生しました。\n```\n{str(e)}\n```", color=discord.Color.red()))

        if success:
            await status_msg.edit(embed=Embed(title="商品送信完了", description=f"{buyer.mention} に商品（{item_name} x{purchase_count}）を送信しました。", color=discord.Color.green()))
        else:
            await status_msg.edit(embed=Embed(title="商品送信失敗", description="商品をDMに送る際に問題が発生しました。", color=discord.Color.red()))

        mark_link_as_used(link_id, guild_id)

        pay_channel = bot.get_channel(config.get("pay_channel"))
        if pay_channel:
            log_embed = Embed(title="購入ログ", description=f"{sender_name} (PayPay ID: {sender_id}) が {item_name} x{purchase_count} を購入しました。", color=discord.Color.blue())
            log_embed.add_field(name="合計金額", value=f"¥{total_price}")
            log_embed.add_field(name="リンク", value=pay_link, inline=False)
            if sender_icon:
                log_embed.set_thumbnail(url=sender_icon)
            log_embed.set_footer(text=f"DiscordユーザーID: {buyer.id}")
            await pay_channel.send(embed=log_embed)

# ------------------------
# パネル管理 / 商品管理 / 在庫操作（元コードを統合）
# ------------------------
class NewPanelModal(ui.Modal, title="新しいパネルを作成"):
    def __init__(self):
        super().__init__()
        self.add_item(ui.TextInput(label="パネルの識別ID（英数字）", placeholder="例: panel1"))
        self.add_item(ui.TextInput(label="パネルタイトル", placeholder="例: 販売", required=False))
        self.add_item(ui.TextInput(label="購入者に付与するロール", placeholder="ロールID", required=False))

    async def on_submit(self, interaction: Interaction):
        guild_id = interaction.guild_id
        panels = load_json(get_data_path(guild_id, "panels.json"))

        panel_id = self.children[0].value.strip()
        title = self.children[1].value.strip() or "自販機パネル"
        role_id_input = self.children[2].value.strip()

        if panel_id in panels:
            await interaction.response.send_message("同じIDのパネルが存在します。", ephemeral=True)
            return

        # データ保存
        panel_data = {
            "channel": interaction.channel.id,
            "title": title
        }
        if role_id_input.isdigit():
            panel_data["reward_role"] = int(role_id_input)

        embed = Embed(title=title, description="現在商品はありません", color=discord.Color.green())
        message = await interaction.channel.send(embed=embed, view=PurchaseView(panel_id))

        panels[str(message.id)] = panel_data
        save_json(get_data_path(guild_id, "panels.json"), panels)

        await interaction.response.send_message(f"パネルを作成しました！（ID: {message.id}）", ephemeral=True)

async def update_panel(guild_id, panel_id):
    items = load_json(get_data_path(guild_id, "items.json"))
    panels = load_json(get_data_path(guild_id, "panels.json"))

    if panel_id not in panels:
        print("パネルが存在しません")
        return

    channel_id = panels[panel_id]["channel"]
    channel = bot.get_channel(channel_id)
    if not channel:
        print("チャンネルが見つかりません")
        return

    try:
        message = await channel.fetch_message(int(panel_id))
    except discord.NotFound:
        print("パネルメッセージが存在しません。削除します。")
        del panels[panel_id]
        save_json(get_data_path(guild_id, "panels.json"), panels)
        return

    title = panels[panel_id].get("title", "自販機パネル")
    panel_items = {k: v for k, v in items.items() if v.get("panel_id") == panel_id}

    item_list = []
    for idx, (item_id, item) in enumerate(panel_items.items(), 1):
        description = item.get("description", "説明なし")
        item_list.append(f"**{idx}. {item.get('name','不明')}**\n{description}\n価格: ¥{item.get('price',0)}")

    description = "\n".join(item_list) if item_list else "現在商品はありません"
    description += "\n\n※DMに商品が送られますのでご注意ください"

    embed = message.embeds[0] if message.embeds else Embed()
    embed.title = title
    embed.description = description

    view = ui.View(timeout=None)
    view.add_item(PurchaseButton(panel_id))
    view.add_item(CheckStockButton(panel_id))

    await message.edit(embed=embed, view=view)

async def give_item_automatically(user, guild_id, item_id, count=1):
    items = load_json(get_data_path(guild_id, "items.json"))
    config = load_json(get_data_path(guild_id, "config.json"))
    panels = load_json(get_data_path(guild_id, "panels.json"))

    item = items.get(item_id)
    if not item:
        return False

    panel_id = item.get("panel_id")
    role_id = panels.get(panel_id, {}).get("reward_role")

    count = item.get("purchase_count", 1)
    try:
        count = int(count)
    except (ValueError, TypeError):
        count = 1

    accounts_to_send = []

    if "accounts" in item:
        stock_data = item.get("accounts", [])
        if len(stock_data) < count:
            return False

        for _ in range(count):
            acc = stock_data.pop(0)
            accounts_to_send.append(acc)

        item["accounts"] = stock_data
        item["stock"] = len(stock_data)

    else:
        stock = item.get("stock")

        if isinstance(stock, str):
            if stock.isdigit():
                stock = int(stock)
                item["stock"] = stock
            elif stock in ("∞", "∞️"):
                pass
            else:
                return False

        if isinstance(item.get("stock"), int):
            if item["stock"] < count:
                return False
            item["stock"] = max(item["stock"] - count, 0)
        elif item.get("stock") in ("∞", "∞️"):
            pass
        else:
            return False

    save_json(get_data_path(guild_id, "items.json"), items)
    await update_panel(guild_id, panel_id)

    embed = discord.Embed(title=f"{item.get('name','不明')} を {count}個 購入しました！", color=discord.Color.green())

    if accounts_to_send:
        for idx, acc in enumerate(accounts_to_send, 1):
            embed.add_field(name=f"[{idx}] メールアドレス", value=acc.get("email",""), inline=False)
            embed.add_field(name=f"[{idx}] パスワード", value=acc.get("password",""), inline=False)
    else:
        if item.get("url"):
            embed.description = f"[商品をクリック]({item.get('url')})"
        else:
            embed.description = "ご購入ありがとうございます！"

    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        return False

    # ロール付与
    if role_id:
        guild = bot.get_guild(guild_id)
        member = guild.get_member(user.id)
        role = guild.get_role(role_id)
        if member and role:
            try:
                await member.add_roles(role, reason="商品購入によるロール付与")
            except discord.Forbidden:
                pass

    # ログ送信
    log_channel = bot.get_channel(config.get("log_channel"))
    if log_channel:
        log_embed = discord.Embed(title="🛒購入実績", description=f"{user.mention} が **{item.get('name','不明')}** を {count}個 購入しました！", color=discord.Color.blue())
        log_embed.set_thumbnail(url=user.display_avatar.url)
        await log_channel.send(embed=log_embed)

    return True

def format_item_stock_display(item: dict) -> str:
    if "accounts" in item and isinstance(item["accounts"], list):
        return str(item.get("stock", 0))
    if str(item.get("stock")) == "∞":
        return "∞"
    try:
        stock = int(item.get("stock", 0))
        return str(stock) if stock > 0 else "在庫なし"
    except (ValueError, TypeError):
        return "在庫エラー"

async def send_existing_panel(channel, guild_id, panel_id):
    items = load_json(get_data_path(guild_id, "items.json"))
    panels = load_json(get_data_path(guild_id, "panels.json"))

    item_lines = []
    count = 1
    for item_id, item in items.items():
        if item.get("panel_id") == panel_id:
            description = item.get("description", "説明なし")
            item_lines.append(f"**{count}. {item.get('name','不明')}**\n{description}\n価格: ¥{item.get('price',0)}")
            count += 1

    title = panels.get(panel_id, {}).get("title", "自販機パネル")
    embed = Embed(title=title, description="\n".join(item_lines) if item_lines else "現在商品はありません", color=discord.Color.green())
    await channel.send(embed=embed, view=PurchaseView(panel_id))

class AddItemModal(ui.Modal, title="商品を追加"):
    def __init__(self):
        super().__init__()
        self.add_item(ui.TextInput(label="商品リンク"))
        self.add_item(ui.TextInput(label="商品名"))
        self.add_item(ui.TextInput(label="値段（数字のみ・円不要）"))
        self.add_item(ui.TextInput(label="在庫数（∞で在庫減りません）"))
        self.add_item(ui.TextInput(label="パネルタイトル（空欄で変更なし）", required=False))

    async def on_submit(self, interaction: Interaction):
        guild_id = interaction.guild_id
        items = load_json(get_data_path(guild_id, "items.json"))
        panels = load_json(get_data_path(guild_id, "panels.json"))

        select = ui.Select(placeholder="追加先パネルを選んでください", options=[discord.SelectOption(label=f"パネル {pid}", value=pid) for pid in panels])
        async def callback(inner: Interaction):
            panel_id = select.values[0]
            item_id = f"{panel_id}_{len(items)+1}"

            try:
                price = int(self.children[2].value.replace("円", "").strip())
                stock_input = self.children[3].value.strip()
                if stock_input == "∞":
                    stock = "∞"
                else:
                    stock = int(stock_input)
            except ValueError:
                await inner.response.send_message("値段は数字で、在庫は数字または「∞」で入力してください。", ephemeral=True)
                return

            items[item_id] = {
                "panel_id": panel_id,
                "name": self.children[1].value,
                "price": price,
                "stock": stock,
                "url": self.children[0].value,
                "buyer": None
            }

            panel_title = self.children[4].value.strip()
            if panel_title:
                panels[panel_id]["title"] = panel_title
                save_json(get_data_path(guild_id, "panels.json"), panels)

            save_json(get_data_path(guild_id, "items.json"), items)
            await update_panel(guild_id, panel_id)
            await inner.response.send_message("商品とパネルタイトルを追加/更新しました", ephemeral=True)

        select.callback = callback
        view = ui.View()
        view.add_item(select)
        await interaction.response.send_message("どのパネルに追加しますか？", view=view, ephemeral=True)

class AddProductModal(ui.Modal, title="商品を追加"):
    def __init__(self, panel_id: str):
        super().__init__()
        self.panel_id = panel_id
        self.add_item(ui.TextInput(label="商品名"))
        self.add_item(ui.TextInput(label="価格（数字のみ）"))
        self.add_item(ui.TextInput(label="説明（任意）", required=False))

    async def on_submit(self, interaction: Interaction):
        guild_id = interaction.guild_id
        items = load_json(get_data_path(guild_id, "items.json"))

        try:
            price = int(self.children[1].value.strip())
        except ValueError:
            await interaction.response.send_message("数値のみ入力してください", ephemeral=True)
            return

        panel_items = [i for i in items if items[i].get("panel_id") == self.panel_id]
        item_number = len(panel_items) + 1
        item_id = f"{self.panel_id}_{item_number}"

        items[item_id] = {
            "panel_id": self.panel_id,
            "name": self.children[0].value,
            "price": price,
            "stock": 0,
            "description": self.children[2].value or "説明なし",
            "url": None,
            "buyer": None,
            "accounts": []
        }

        save_json(get_data_path(guild_id, "items.json"), items)
        await update_panel(guild_id, self.panel_id)
        await interaction.response.send_message("商品を追加しました（在庫は0）", ephemeral=True)

class AccountStockModal(ui.Modal, title="アカウント在庫を追加"):
    def __init__(self, item_id: str):
        super().__init__()
        self.item_id = item_id
        self.add_item(ui.TextInput(label="メールアドレス", placeholder="example@example.com"))
        self.add_item(ui.TextInput(label="パスワード", placeholder="password123"))

    async def on_submit(self, interaction: Interaction):
        guild_id = interaction.guild_id
        items = load_json(get_data_path(guild_id, "items.json"))

        item = items.get(self.item_id)
        if not item:
            await interaction.response.send_message("商品が見つかりませんでした。", ephemeral=True)
            return

        if "accounts" not in item:
            item["accounts"] = []

        item["accounts"].append({
            "email": self.children[0].value,
            "password": self.children[1].value
        })

        item["stock"] = item.get("stock", 0) + 1

        save_json(get_data_path(guild_id, "items.json"), items)
        await update_panel(guild_id, item["panel_id"])
        await interaction.response.send_message("アカウント在庫を追加しました！", ephemeral=True)

async def is_payment_confirmed(paypay: PayPay, link: str, price: int) -> bool:
    try:
        note = link.split("/")[-1]
        transfers = paypay.search_transfer(note)
        for tx in transfers:
            if int(tx["amount"]) == price:
                return True
        return False
    except Exception as e:
        print(f"支払確認エラー: {e}")
        return False

class PaypayRegisterModal(ui.Modal, title="PayPay-SMS認証URL入力"):
    def __init__(self, paypay: PayPay, guild_id: int):
        super().__init__()
        self.paypay = paypay
        self.guild_id = guild_id
        self.auth_link_input = ui.TextInput(label="SMSに送られてきた認証URLを入力してください", min_length=50, max_length=100, required=True)
        self.add_item(self.auth_link_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            self.paypay.login(self.auth_link_input.value)
            config_path = get_data_path(self.guild_id, "config.json")
            config = load_json(config_path)
            secret_key = os.getenv("TOKEN_ENCRYPT_KEY")
            config["paypay_access_token"] = encrypt_token(self.paypay.access_token, secret_key)
            config["paypay_refresh_token"] = encrypt_token(self.paypay.refresh_token, secret_key)
            save_json(config_path, config)
            await interaction.followup.send("PayPayログイン成功", ephemeral=True)
        except PayPayLoginError:
            embed = discord.Embed(title="PayPayログインエラー", description="認証URLが正しいか確認してください。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)

class PaypayRegisterView(ui.View):
    def __init__(self, paypay: PayPay, guild_id: int):
        super().__init__()
        self.paypay = paypay
        self.guild_id = guild_id

    @ui.button(label="認証URLを入力", style=discord.ButtonStyle.primary, custom_id="auth_url_input")
    async def auth_url_input_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = PaypayRegisterModal(self.paypay, self.guild_id)
        await interaction.response.send_modal(modal)

class Paginator(ui.View):
    def __init__(self, pages):
        super().__init__(timeout=120)
        self.pages = pages
        self.current = 0

    async def update_message(self, interaction):
        embed = Embed(description=self.pages[self.current])
        embed.set_footer(text=f"Page {self.current+1} / {len(self.pages)}")
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="◀️", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.current > 0:
            self.current -= 1
            await self.update_message(interaction)

    @ui.button(label="▶️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.current < len(self.pages) - 1:
            self.current += 1
            await self.update_message(interaction)

# ------------------------
# スラッシュコマンド（自販機 / 管理コマンド群）
# ------------------------
@tree.command(name="自販機パネル設置", description="パネルを新規作成または再表示")
@app_commands.checks.has_permissions(administrator=True)
async def setpanel(interaction: Interaction):
    guild = interaction.guild
    if guild is None:
        return
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    panels = load_json(get_data_path(interaction.guild_id, "panels.json"))
    options = [discord.SelectOption(label="新規パネルを作成", value="new"), discord.SelectOption(label="既存パネルを再表示", value="existing") if panels else None]
    options = [opt for opt in options if opt is not None]
    select = ui.Select(placeholder="操作を選んでください", options=options)
    async def callback(i: Interaction):
        choice = select.values[0]
        if choice == "new":
            await i.response.send_modal(NewPanelModal())
        elif choice == "existing":
            panel_options = [discord.SelectOption(label=f"{panels[pid].get('title','自販機パネル')} (ID: {pid})", value=pid) for pid in panels]
            panel_select = ui.Select(placeholder="再表示するパネルを選択", options=panel_options)
            async def panel_callback(i2: Interaction):
                await send_existing_panel(i2.channel, interaction.guild_id, panel_select.values[0])
                await i2.response.send_message("パネルを再表示しました！", ephemeral=True)
            panel_select.callback = panel_callback
            view2 = ui.View()
            view2.add_item(panel_select)
            await i.response.send_message("再表示するパネルを選んでください：", view=view2, ephemeral=True)
    select.callback = callback
    view = ui.View()
    view.add_item(select)
    await interaction.response.send_message("操作を選んでください：", view=view, ephemeral=True)

@tree.command(name="url型商品追加", description="URL型の商品を追加")
@app_commands.checks.has_permissions(administrator=True)
async def additem(interaction: Interaction):
    guild = interaction.guild
    if guild is None:
        return
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    await interaction.response.send_modal(AddItemModal())

@tree.command(name="実績報告設定", description="実績報告チャンネルを設定します")
@app_commands.checks.has_permissions(administrator=True)
async def setchannels(interaction: Interaction):
    guild = interaction.guild
    if guild is None:
        return
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ使用できます。", ephemeral=True)
        return
    guild_id = interaction.guild_id
    config_path = get_data_path(guild_id, "config.json")
    config = load_json(config_path)
    channels = [discord.SelectOption(label=channel.name, value=str(channel.id)) for channel in guild.text_channels][:25]
    select_log = ui.Select(placeholder="実績報告チャンネルを選んでください", options=channels)
    async def log_selected_callback(log_interaction: Interaction):
        config["log_channel"] = int(select_log.values[0])
        save_json(config_path, config)
        await log_interaction.response.send_message("実績報告チャンネルを設定しました！", ephemeral=True)
    select_log.callback = log_selected_callback
    view = ui.View()
    view.add_item(select_log)
    await interaction.response.send_message("実績報告チャンネルを選択してください", view=view, ephemeral=True)

@tree.command(name="商品削除", description="商品を削除します（パネル→商品選択）")
@app_commands.checks.has_permissions(administrator=True)
async def deleteitem(interaction: Interaction):
    guild_id = interaction.guild_id
    items = load_json(get_data_path(guild_id, "items.json"))
    panels = load_json(get_data_path(guild_id, "panels.json"))
    panel_options = [discord.SelectOption(label=f"パネル {pid}", value=pid) for pid in panels]
    if not panel_options:
        await interaction.response.send_message("パネルが存在しません。", ephemeral=True)
        return
    panel_select = ui.Select(placeholder="パネルを選択してください", options=panel_options)
    async def panel_selected(i: Interaction):
        selected_panel_id = panel_select.values[0]
        item_options = [discord.SelectOption(label=item.get("name",""), value=item_id) for item_id, item in items.items() if item.get("panel_id") == selected_panel_id]
        if not item_options:
            await i.response.send_message("商品がありません。", ephemeral=True)
            return
        item_select = ui.Select(placeholder="削除する商品を選んでください", options=item_options)
        async def item_selected(i2: Interaction):
            selected_item_id = item_select.values[0]
            if selected_item_id in items:
                del items[selected_item_id]
                save_json(get_data_path(guild_id, "items.json"), items)
                await update_panel(guild_id, selected_panel_id)
                await i2.response.send_message("🗑️商品を削除しました。", ephemeral=True)
            else:
                await i2.response.send_message("商品が見つかりません。", ephemeral=True)
        item_select.callback = item_selected
        view = ui.View()
        view.add_item(item_select)
        await i.response.send_message("商品を選んでください", view=view, ephemeral=True)
    panel_select.callback = panel_selected
    view = ui.View()
    view.add_item(panel_select)
    await interaction.response.send_message("パネルを選んでください", view=view, ephemeral=True)

@tree.command(name="パネル削除", description="パネルを削除します")
@app_commands.checks.has_permissions(administrator=True)
async def delete_panel(interaction: Interaction):
    guild_id = interaction.guild_id
    panels = load_json(get_data_path(guild_id, "panels.json"))
    items = load_json(get_data_path(guild_id, "items.json"))
    if not panels:
        await interaction.response.send_message("削除可能なパネルが存在しません。", ephemeral=True)
        return
    select = ui.Select(placeholder="削除するパネルを選んでください", options=[discord.SelectOption(label=f"{panels[pid].get('title','自販機パネル')}（ID: {pid}）", value=pid) for pid in panels])
    async def callback(i: Interaction):
        selected_id = select.values[0]
        channel = bot.get_channel(panels[selected_id]["channel"])
        try:
            msg = await channel.fetch_message(int(selected_id))
            await msg.delete()
        except Exception:
            pass
        del panels[selected_id]
        items = {k: v for k, v in items.items() if v.get("panel_id") != selected_id}
        save_json(get_data_path(guild_id, "panels.json"), panels)
        save_json(get_data_path(guild_id, "items.json"), items)
        await i.response.send_message(f"パネルと関連商品を削除しました（ID: {selected_id}）", ephemeral=True)
    select.callback = callback
    view = ui.View()
    view.add_item(select)
    await interaction.response.send_message("削除するパネルを選んでください：", view=view, ephemeral=True)

@tree.command(name="パネル一覧", description="このサーバーの自販機パネル一覧を表示")
async def listpanels(interaction: Interaction):
    guild = interaction.guild
    if guild is None:
        return
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    guild_id = interaction.guild_id
    panels = load_json(get_data_path(guild_id, "panels.json"))
    items = load_json(get_data_path(guild_id, "items.json"))
    if not panels:
        await interaction.response.send_message("📭 パネルが存在しません。", ephemeral=True)
        return
    embed = Embed(title="パネル一覧", color=discord.Color.orange())
    for panel_id, info in panels.items():
        count = sum(1 for i in items.values() if i.get("panel_id") == panel_id)
        embed.add_field(name=f"パネルID: {panel_id}", value=f"商品数: {count}｜チャンネルID: {info.get('channel')}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="アカウント型商品追加", description="商品をパネルに追加します")
@app_commands.checks.has_permissions(administrator=True)
async def add_product(interaction: Interaction):
    guild = interaction.guild
    if guild is None:
        return
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    guild_id = guild.id
    panels = load_json(get_data_path(guild_id, "panels.json"))
    if not panels:
        await interaction.response.send_message("パネルが存在しません。先に /自販機パネル設置 で作成してください。", ephemeral=True)
        return
    select = ui.Select(placeholder="パネルを選択してください", options=[discord.SelectOption(label=f"パネル {pid}", value=pid) for pid in panels])
    async def callback(i: Interaction):
        await i.response.send_modal(AddProductModal(select.values[0]))
    select.callback = callback
    view = ui.View()
    view.add_item(select)
    await interaction.response.send_message("商品を追加するパネルを選んでください", view=view, ephemeral=True)

@tree.command(name="アカウント在庫追加", description="アカウント型商品の在庫を追加します")
@app_commands.checks.has_permissions(administrator=True)
async def add_stock(interaction: Interaction):
    guild = interaction.guild
    if guild is None:
        return
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    guild_id = interaction.guild_id
    items = load_json(get_data_path(guild_id, "items.json"))
    panels = load_json(get_data_path(guild_id, "panels.json"))
    panel_options = [discord.SelectOption(label=f"パネル {pid}", value=pid) for pid in panels]
    if not panel_options:
        await interaction.response.send_message("パネルが存在しません。", ephemeral=True)
        return
    panel_select = ui.Select(placeholder="パネルを選んでください", options=panel_options)
    async def panel_selected_callback(panel_interaction: Interaction):
        selected_panel_id = panel_select.values[0]
        item_options = [discord.SelectOption(label=item.get("name",""), value=item_id) for item_id, item in items.items() if item.get("panel_id") == selected_panel_id]
        if not item_options:
            await panel_interaction.response.send_message("このパネルには商品がありません。", ephemeral=True)
            return
        item_select = ui.Select(placeholder="在庫を追加する商品を選択", options=item_options)
        async def item_selected_callback(item_interaction: Interaction):
            selected_item_id = item_select.values[0]
            await item_interaction.response.send_modal(AccountStockModal(selected_item_id))
        item_select.callback = item_selected_callback
        view = ui.View()
        view.add_item(item_select)
        await panel_interaction.response.send_message("商品を選んでください", view=view, ephemeral=True)
    panel_select.callback = panel_selected_callback
    view = ui.View()
    view.add_item(panel_select)
    await interaction.response.send_message("パネルを選んでください", view=view, ephemeral=True)

@tree.command(name="paypay登録", description="PayPayのアカウント情報を登録します")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(phone="電話番号", password="パスワード")
async def paypay_register(interaction: discord.Interaction, phone: str, password: str):
    if not interaction.user or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        paypay = PayPay(phone, password)
    except PayPayLoginError:
        embed = discord.Embed(title="PayPayログインエラー", description="電話番号・パスワードが合っているか確認してください。", color=discord.Color.red())
        await interaction.followup.send(embed=embed)
        return
    view = PaypayRegisterView(paypay, interaction.guild_id)
    await interaction.followup.send(content="認証URLを送信しました。\n__**認証URLには絶対にアクセスしないでください。**__\nログインが失敗する可能性があります。", view=view, ephemeral=True)


# ------------------------
# 起動時の自販機ボタン再登録等
# ------------------------
@bot.event
async def on_ready():
    print(f"✅ ログイン完了: {bot.user}（ID: {bot.user.id}）")

    # ギルド毎のパネル／ボタン再登録（既存パネルがあれば view を再登録）
    for guild in bot.guilds:
        guild_id = guild.id
        panels = load_json(get_data_path(guild_id, "panels.json"))
        for panel_id in panels:
            view = ui.View(timeout=None)
            view.add_item(PurchaseButton(panel_id))
            view.add_item(CheckStockButton(panel_id))
            bot.add_view(view)
    print("✅ 自販機パネルのボタンを再登録しました。")

    # 起動完了メッセージ（任意のチャンネルが設定されていれば送信）
    if DISCORD_CHANNEL:
        ch = bot.get_channel(int(DISCORD_CHANNEL))
        if ch:
            try:
                await ch.send(embed=Embed(title="Bot起動", description=f"{bot.user} がオンラインになりました！", color=discord.Color.green()))
            except Exception:
                pass

    # アクティビティ更新タスク
    bot.loop.create_task(update_activity())

# ------------------------
# setup_hook：チケット永続化・Cogロード・コマンド同期（ここで sync する）
# ------------------------
@bot.event
async def setup_hook():
    # チケット永続化（ticket_view_config.json があれば再登録）
    try:
        if os.path.exists("ticket_view_config.json"):
            with open("ticket_view_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)

            log_channel = bot.get_channel(config.get("log_channel"))
            category = bot.get_channel(config.get("category")) if config.get("category") else None
            staff_role = None
            if log_channel and config.get("staff_role"):
                staff_role = log_channel.guild.get_role(config.get("staff_role"))
            ticket_limit = config.get("ticket_limit", 1)
            open_message = config.get("open_message", "サポートチケットが作成されました。")

            bot.add_view(TicketView(log_channel, category, staff_role, ticket_limit, open_message))
            print("✅ チケットボタンを再登録しました。")
        else:
            print("ℹ️ 初回起動: ticket_view_config.json がありません。")
    except Exception as e:
        print(f"⚠️ チケット永続化エラー: {e}")

    # Cog（拡張）をロード
    try:
        # ticket と 認証 はファイル名（拡張名）に応じて import される想定
        await bot.load_extension("ticket")
        await bot.load_extension("認証")
        await bot.load_extension("embed")  
        await bot.load_extension("参加ログ")
        await bot.load_extension("vending_giveaway_stats")




        print("✅ ticket と 認証 の Cog をロードしました。")
    except Exception as e:
        print(f"⚠️ Cogロードエラー: {e}")

    # スラッシュコマンド同期（ここで行う）
    try:
        synced = await bot.tree.sync()
        print(f"✅ スラッシュコマンド同期完了: {len(synced)}件")
    except Exception as e:
        print(f"⚠️ スラッシュコマンド同期エラー: {e}")


    try:
        if os.path.exists("ticket_view_config.json"):
            with open("ticket_view_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)

            log_channel = bot.get_channel(config["log_channel"])
            category = bot.get_channel(config["category"]) if config["category"] else None
            staff_role = log_channel.guild.get_role(config["staff_role"]) if config["staff_role"] else None
            ticket_limit = config["ticket_limit"]
            open_message = config["open_message"]

            bot.add_view(TicketView(log_channel, category, staff_role, ticket_limit, open_message))
            print("✅ チケット作成ボタンを永続化しました。")
        else:
            print("ℹ️ ticket_view_config.json が存在しません。初回起動です。")
    except Exception as e:
        print(f"⚠️ チケットViewの永続化に失敗しました: {e}")

# ------------------------
# プレイ中のステータス更新
# ------------------------
async def update_activity():
    while True:
        now = datetime.now()
        hour = now.hour
        guild_count = len(bot.guilds)
        member_count = sum(g.member_count for g in bot.guilds)
        activity = discord.Game(f"サーバー数: {guild_count} | ユーザー数: {member_count}")
        try:
            await bot.change_presence(activity=activity)
        except Exception:
            pass
        await asyncio.sleep(3600)

if __name__ == "__main__":
    while True:
        try:
            bot.run("")
        except Exception as e:
            print(f"\n[⚠️ Botがエラーで停止しました] {e}")
            print("3秒後に完全再起動します...")
            time.sleep(3)
            os.execv(sys.executable, ["python"] + sys.argv)
