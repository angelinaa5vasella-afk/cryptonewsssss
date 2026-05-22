import os
import asyncio
import json
import logging
from datetime import datetime, date
from pathlib import Path

import aiohttp
import feedparser
from deep_translator import GoogleTranslator
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "9"))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "20"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "22"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"

COINS = {
    "bitcoin": ("BTC", "₿"),
    "ethereum": ("ETH", "⟠"),
    "solana": ("SOL", "◎"),
}

POSTED_FILE = Path(__file__).parent / "posted.json"
DIGEST_FILE = Path(__file__).parent / "digest.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%d.%m %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------- Tracking ----------

def load_posted() -> set:
    if POSTED_FILE.exists():
        return set(json.loads(POSTED_FILE.read_text(encoding="utf-8")))
    return set()


def save_posted(posted: set):
    urls = list(posted)[-300:]
    POSTED_FILE.write_text(json.dumps(urls, ensure_ascii=False, indent=2), encoding="utf-8")


def load_digest() -> list:
    """Список статей за сегодня для дайджеста."""
    if not DIGEST_FILE.exists():
        return []
    data = json.loads(DIGEST_FILE.read_text(encoding="utf-8"))
    if data.get("date") != str(date.today()):
        return []
    return data.get("articles", [])


def save_digest(articles: list):
    DIGEST_FILE.write_text(
        json.dumps({"date": str(date.today()), "articles": articles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------- Prices ----------

async def get_prices() -> dict:
    ids = ",".join(COINS.keys())
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return await r.json()


async def get_fear_greed() -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.alternative.me/fng/?limit=1", timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            data = await r.json()
            return data["data"][0]


def format_price_message(prices: dict, fg: dict) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [f"📊 *Крипторынок — {now} МСК*\n"]

    for coin_id, (symbol, icon) in COINS.items():
        p = prices.get(coin_id, {})
        price = p.get("usd", 0)
        change = p.get("usd_24h_change", 0) or 0
        arrow = "🟢" if change >= 0 else "🔴"
        sign = "+" if change >= 0 else ""
        lines.append(f"{arrow} *{symbol}* {icon}  `${price:,.2f}`  {sign}{change:.2f}%")

    val = int(fg["value"])
    label = fg["value_classification"]
    if val <= 25:
        fg_icon = "😱"
    elif val <= 45:
        fg_icon = "😰"
    elif val <= 55:
        fg_icon = "😐"
    elif val <= 75:
        fg_icon = "😏"
    else:
        fg_icon = "🤑"

    lines.append(f"\n{fg_icon} *Fear & Greed*: {val}/100 — {label}")
    lines.append("\n#крипта #биткоин #BTC #ETH #SOL")
    return "\n".join(lines)


# ---------- News (Coindesk RSS + Google Translate) ----------

def parse_rss() -> list[dict]:
    feed = feedparser.parse(COINDESK_RSS)
    articles = []
    for entry in feed.entries[:20]:
        image_url = None
        if hasattr(entry, "media_content") and entry.media_content:
            image_url = entry.media_content[0].get("url")
        if not image_url and hasattr(entry, "enclosures") and entry.enclosures:
            enc = entry.enclosures[0]
            if enc.get("type", "").startswith("image"):
                image_url = enc.get("href") or enc.get("url")
        if not image_url and hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            image_url = entry.media_thumbnail[0].get("url")

        summary = entry.get("summary", "") or ""
        if "<" in summary:
            summary = BeautifulSoup(summary, "html.parser").get_text()

        articles.append({
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "summary": summary[:600],
            "image_url": image_url,
        })
    return articles


async def fetch_article_body(session: aiohttp.ClientSession, url: str) -> str:
    """Тянем полный текст статьи с сайта CoinDesk."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12), headers=headers) as r:
            if r.status != 200:
                return ""
            html = await r.text()
            soup = BeautifulSoup(html, "html.parser")
            # Убираем ненужное
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            # Ищем параграфы внутри статьи
            paragraphs = soup.select("article p, .article-body p, [data-module-name] p")
            if not paragraphs:
                paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs[:6])
            return text[:800]
    except Exception:
        return ""


async def fetch_og_image(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), headers=headers) as r:
            if r.status != 200:
                return None
            html = await r.text()
            soup = BeautifulSoup(html, "html.parser")
            tag = soup.find("meta", property="og:image")
            if tag:
                return tag.get("content")
    except Exception:
        pass
    return None


def _translate(text: str) -> str:
    if not text or not text.strip():
        return ""
    try:
        return GoogleTranslator(source="en", target="ru").translate(text[:4500])
    except Exception:
        return text


def _make_post_sync(title: str, summary: str, body: str) -> str:
    """Формируем пост: переводим заголовок + делаем выжимку из тела статьи."""
    ru_title = _translate(title)

    # Используем тело статьи если есть, иначе summary из RSS
    source_text = body if len(body) > len(summary) else summary
    ru_body = _translate(source_text) if source_text else ""

    # Делаем выжимку — берём первые 2-3 предложения
    if ru_body:
        sentences = [s.strip() for s in ru_body.replace("…", ".").split(".") if len(s.strip()) > 20]
        excerpt = ". ".join(sentences[:3])
        if excerpt and not excerpt.endswith("."):
            excerpt += "."
    else:
        excerpt = ""

    lines = [f"📰 *{ru_title}*"]
    if excerpt:
        lines.append(f"\n{excerpt}")
    lines.append("\n#криптоновости #крипта #coindesk")
    return "\n".join(lines)


async def build_post(article: dict) -> str:
    loop = asyncio.get_event_loop()
    try:
        async with aiohttp.ClientSession() as s:
            body = await fetch_article_body(s, article["url"])
        return await loop.run_in_executor(
            None, _make_post_sync, article["title"], article["summary"], body
        )
    except Exception as e:
        log.warning(f"Ошибка формирования поста: {e}")
        ru_title = await asyncio.get_event_loop().run_in_executor(None, _translate, article["title"])
        return f"📰 *{ru_title}*\n\n#криптоновости #крипта #coindesk"


async def get_fresh_article(posted: set) -> dict | None:
    loop = asyncio.get_event_loop()
    articles = await loop.run_in_executor(None, parse_rss)
    fresh = [a for a in articles if a["url"] not in posted]
    if not fresh:
        log.warning("Все свежие новости уже опубликованы")
        return None

    with_image = [a for a in fresh if a["image_url"]]
    article = with_image[0] if with_image else fresh[0]

    if not article["image_url"]:
        async with aiohttp.ClientSession() as s:
            article["image_url"] = await fetch_og_image(s, article["url"])

    return article


# ---------- Evening digest ----------

def _make_digest_sync(articles: list[dict]) -> str:
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"🗞 *Крипто-дайджест за {today}*\n"]
    for i, a in enumerate(articles[:7], 1):
        ru_title = _translate(a["title"])
        lines.append(f"{i}. {ru_title}")
    lines.append("\n#дайджест #крипта #криптоновости")
    return "\n".join(lines)


async def post_digest(bot: Bot):
    articles = load_digest()
    if not articles:
        log.warning("Дайджест пуст — нет статей за сегодня")
        return
    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _make_digest_sync, articles)
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        log.info(f"Дайджест опубликован ({len(articles)} статей)")
    except Exception as e:
        log.error(f"Ошибка дайджеста: {e}")


# ---------- Post functions ----------

async def post_prices(bot: Bot):
    try:
        prices, fg = await asyncio.gather(get_prices(), get_fear_greed())
        text = format_price_message(prices, fg)
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.MARKDOWN)
        log.info("Котировки опубликованы")
    except Exception as e:
        log.error(f"Ошибка котировок: {e}")


async def post_news(bot: Bot):
    posted = load_posted()
    try:
        article = await get_fresh_article(posted)
        if not article:
            return

        caption = await build_post(article)

        if article["image_url"]:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=article["image_url"],
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption + f"\n\n🔗 [Читать]({article['url']})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )

        posted.add(article["url"])
        save_posted(posted)

        # Сохраняем в дайджест
        daily = load_digest()
        daily.append({"title": article["title"], "url": article["url"]})
        save_digest(daily)

        log.info(f"Новость опубликована: {article['title'][:60]}")
    except Exception as e:
        log.error(f"Ошибка новости: {e}")


async def morning_block(bot: Bot):
    """Утренний блок: котировки + первая новость дня."""
    log.info("Утренний блок")
    await post_prices(bot)
    await asyncio.sleep(60)
    await post_news(bot)


async def evening_block(bot: Bot):
    """Вечерний блок: котировки."""
    log.info("Вечерний блок")
    await post_prices(bot)


# ---------- Main ----------

async def main():
    import sys

    missing = [k for k in ("BOT_TOKEN", "CHANNEL_ID") if not os.getenv(k)]
    if missing:
        log.error(f"Не заданы переменные в .env: {', '.join(missing)}")
        return

    bot = Bot(token=BOT_TOKEN)
    try:
        me = await bot.get_me()
        log.info(f"Бот запущен: @{me.username}")
    except TelegramError as e:
        log.error(f"Ошибка Telegram: {e}")
        return

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Котировки: утром и вечером
    scheduler.add_job(morning_block, "cron", hour=MORNING_HOUR, minute=0, args=[bot])
    scheduler.add_job(evening_block, "cron", hour=EVENING_HOUR, minute=0, args=[bot])

    # Новости каждые 3 часа (12:00, 15:00, 18:00, 21:00)
    news_hours = [h for h in range(0, 24, 3) if h != MORNING_HOUR]
    scheduler.add_job(post_news, "cron", hour=",".join(map(str, news_hours)), minute=0, args=[bot])

    # Вечерний дайджест
    scheduler.add_job(post_digest, "cron", hour=DIGEST_HOUR, minute=0, args=[bot])

    scheduler.start()

    log.info(f"Котировки: {MORNING_HOUR}:00 и {EVENING_HOUR}:00")
    log.info(f"Новости: каждые 3 часа в {', '.join(map(str, news_hours))}:00")
    log.info(f"Дайджест: {DIGEST_HOUR}:00")

    if "--test" in sys.argv:
        log.info("Режим --test")
        await morning_block(bot)
        await asyncio.sleep(3)
        await post_news(bot)
        await asyncio.sleep(3)
        await post_digest(bot)
        return

    log.info("Бот работает. Ctrl+C для остановки.")
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен")
