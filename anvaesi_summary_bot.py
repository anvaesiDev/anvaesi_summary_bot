import logging
import re
import time
import urllib.parse
import requests
import yt_dlp
import openai
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegraph import Telegraph
import io
from bs4 import BeautifulSoup, NavigableString, Tag
from youtube_transcript_api import YouTubeTranscriptApi

# Monkey patch для youtube_transcript_api: устанавливаем кастомный User-Agent, имитирующий браузер
import youtube_transcript_api._api as yt_api
def patched_make_request(url, params=None, proxies=None):
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/92.0.4515.107 Safari/537.36')
    }
    response = requests.get(url, params=params, headers=headers, proxies=proxies)
    response.raise_for_status()
    return response.text
yt_api._make_request = patched_make_request

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация Telegraph с сохранённым access_token
telegraph = Telegraph(
    access_token="97c85721eb376ac68cd430907a7f48d3688ca22b3d4feed45e17a899ac40"
)

# Настройки OpenAI API
openai.api_base = "https://openrouter.ai/api/v1"
openai.api_key = "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"

# Функция для извлечения video_id из URL
def extract_video_id(url: str) -> str | None:
    # Проверяем наличие youtube домена
    if "youtube.com" not in url and "youtu.be" not in url:
        return None
    match = re.search(r'youtu\.be/([^?&]+)', url)
    if match:
        return match.group(1)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "v" in query:
        return query["v"][0]
    return None

# Функция для обработки видео: получение заголовка, транскрипта и генерация саммари
def process_video(video_url: str) -> list:
    video_id = extract_video_id(video_url)
    if not video_id:
        return ["Пожалуйста, отправьте корректную ссылку на YouTube видео.", ""]

    # Получаем информацию о видео через yt_dlp
    try:
        ydl_opts = {
            'skip_download': True,
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        video_title = info.get('title')
        if not video_title:
            return ["Пожалуйста, отправьте корректную ссылку на YouTube видео.", ""]
    except Exception as e:
        return ["Пожалуйста, отправьте корректную ссылку на YouTube видео.", ""]

    # Получаем транскрипт видео на нужном языке: сначала пытаемся получить русский, иначе выбираем первый доступный
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            transcript = transcript_list.find_transcript(['ru'])
        except Exception:
            # Если транскрипт на русском недоступен, выбираем первый из доступных языков
            available_langs = [t.language_code for t in transcript_list]
            transcript = transcript_list.find_transcript(available_langs)
        transcript_segments = transcript.fetch()
        transcript_text = " ".join(segment["text"] for segment in transcript_segments)
    except Exception as e:
        return [f"Ошибка при получении транскрипта: {e}", video_title]

    # Формируем промпт для ИИ с инструкциями для HTML-разметки
    prompt = f"""Ты — эксперт по конспектированию видеоконтента. На основе представленного ниже заголовка и транскрипта YouTube‑видео составь структурированное и красиво отформатированное саммари, идеально подходящее для публикации на Telegra.ph с использованием HTML-разметки.

ВНИМАНИЕ:
1. Для жирного текста используй теги <b>текст</b>.
2. Для курсива используй теги <i>текст</i>.
3. Для зачёркнутого текста используй теги <s>текст</s>.
4. Для подчеркнутого текста используй теги <u>текст</u>.
5. Для моноширинного текста используй теги <code>текст</code>.
6. Для кода используй такой формат:
<pre><code class="language-python">
Предварительно отформатированный блок кода на языке Python
</code></pre>
7. Также возможны вариации: комбинации тегов, например, <b><i>Жирный наклонный текст</i></b> или <i><u>Наклонный подчеркнутый текст</u></i>, или <b><i><u>Жирный наклонный подчеркнутый текст</u></i></b>.
8. Для спойлеров можно использовать тег <span style="background-color:#000; color:#000;">текст</span>.
9. Не используй теги <h1> и подобные для заголовков. Если нужно выделить заголовок, просто сделай его жирным или используй дополнительные декоративные элементы.
10. Используй спецсимволы вроде • для списков (при необходимости их экранируй, если требуется).
11. Не добавляй в текст ничего, кроме самого саммари, не добавляй приветствий и прочего, только саммари.
12. Структура саммари:
   • <b>💡 Ключевые моменты</b>: Перечисли все значимые идеи и моменты видео. Если информация в видео уже представлена в нумерованном виде, сохрани этот формат. Не ограничивайся каким-либо фиксированным количеством пунктов – выведи все важные моменты, независимо от длительности видео. Каждый пункт должен содержать максимально подробное описание ключевой идеи. Подчеркивай какие-то важные моменты в тексте с помощью форматирования.
   • <b>📚 Определения и пояснения</b>: Если в видео упоминаются сложные термины или понятия, кратко объясни их значение.
   • <b>📝 Вывод</b>: 3–5 предложений с основными итогами.
13. Используй как можно больше подходящих по смыслу видов форматирования, чтобы текст не был просто стеной, а читался максимально понятно.
14. Ты ОБЯЗАН не использовать ```html``` в своем ответе. ПЕРЕПРОВЕРЬ ЭТО ДВАЖДЫ.
<b>Заголовок видео:</b> {video_title}

<b>Транскрипт видео:</b> {transcript_text}
"""

    # Отправляем запрос к OpenAI для получения саммари с повторной попыткой в случае ошибки, связанной с "choices"
    try:
        max_attempts = 3
        attempt = 0
        while attempt < max_attempts:
            try:
                response = openai.ChatCompletion.create(
                    model="google/gemini-2.0-flash-lite-preview-02-05:free",
                    messages=[{"role": "user", "content": prompt}]
                )
                break  # Если запрос успешен, выходим из цикла
            except Exception as e:
                if "choices" in str(e):
                    attempt += 1
                    time.sleep(5)  # Задержка 5 секунд перед повтором
                else:
                    raise e
        else:
            return [f"Ошибка при вызове ИИ: {e}", video_title]
        summary = response.choices[0].message.content
    except Exception as e:
        return [f"Ошибка при вызове ИИ: {e}", video_title]

    # Удаляем обёртки ```html``` если они присутствуют
    summary = re.sub(r"```html\s*", "", summary)
    summary = re.sub(r"\s*```", "", summary)

    return [summary, video_title]

# Функция для преобразования HTML в узлы, понятные Telegra.ph
def html_to_telegraph_nodes(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")

    def convert_element(element):
        if isinstance(element, NavigableString):
            text = str(element)
            return text if text.strip() else None
        elif isinstance(element, Tag):
            node = {"tag": element.name}
            children = []
            for child in element.children:
                converted = convert_element(child)
                if converted is not None:
                    if isinstance(converted, list):
                        children.extend(converted)
                    else:
                        children.append(converted)
            if children:
                node["children"] = children
            return node
        return None

    root = soup.body if soup.body else soup
    nodes = []
    for child in root.contents:
        conv = convert_element(child)
        if conv:
            nodes.append(conv)
    return nodes

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь мне ссылку на YouTube видео, и я пришлю тебе его саммари."
    )

# Обработчик текстовых сообщений (ожидается ссылка на видео)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    video_url = update.message.text.strip()
    # Проверяем, что ссылка является ссылкой на YouTube
    if not (video_url.startswith("http") and extract_video_id(video_url)):
        await update.message.reply_text(
            "Пожалуйста, отправьте корректную ссылку на YouTube видео.")
        return

    processing_msg = await update.message.reply_text(
        "Обрабатываю видео, подожди, пожалуйста...")
    try:
        summary_and_title = process_video(video_url)
        summary = summary_and_title[0]
        video_title = summary_and_title[1]
        if summary.startswith("Ошибка") or summary.startswith("Пожалуйста"):
            await update.message.reply_text(summary)
            return

        nodes = html_to_telegraph_nodes(summary)

        try:
            page = telegraph.create_page(title=f"{video_title}",
                                         author_name="anvaesi_summary_bot",
                                         content=nodes)
            page_url = page['url']
            await update.message.reply_text(
                f"Саммари видео опубликовано на Telegra.ph:\n{page_url}")
        except Exception as e:
            max_length = 4000
            if len(summary) > max_length:
                for i in range(0, len(summary), max_length):
                    chunk = summary[i:i + max_length]
                    await update.message.reply_text(chunk,
                                                    parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(summary,
                                                parse_mode=ParseMode.HTML)
    finally:
        await processing_msg.delete()

def main() -> None:
    telegram_token = "7740753488:AAEmlrqxkd9p8NvcuofolfBLDBrUdWGJDso"
    application = Application.builder().token(telegram_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()

if __name__ == '__main__':
    main()
