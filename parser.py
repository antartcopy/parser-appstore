# app.py

# ============================================================
# Streamlit-приложение для сбора русскоязычных отзывов
# из российского App Store за последние 12 месяцев.
#
# Источник данных:
# публичный RSS endpoint Apple:
# https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json
#
# Важно:
# Apple RSS не гарантирует выдачу абсолютно всех отзывов.
# Скрипт делает максимально возможный сбор через публичный endpoint
# без API-ключей и авторизации.
# ============================================================

import re
import time
import random
import requests
import pandas as pd
import streamlit as st

from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from urllib.parse import urlparse
from io import StringIO


# -----------------------------
# Настройки
# -----------------------------

COUNTRY = "ru"
MAX_PAGES = 10
REQUEST_TIMEOUT = 20
MIN_PAUSE = 1.0
MAX_PAUSE = 2.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


# -----------------------------
# Функции
# -----------------------------

def extract_app_id(app_url: str) -> str:
    """
    Извлекает app_id из ссылки App Store.

    Пример:
    https://apps.apple.com/ru/app/example-app/id1234567890
    """

    if not app_url or not isinstance(app_url, str):
        raise ValueError("Ссылка пустая или некорректная.")

    app_url = app_url.strip()
    parsed = urlparse(app_url)

    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Некорректная ссылка. Вставьте полный URL App Store.")

    if "apps.apple.com" not in parsed.netloc:
        raise ValueError("Это не ссылка на App Store. Ожидается домен apps.apple.com.")

    match = re.search(r"/id(\d+)", app_url)

    if not match:
        match = re.search(r"id(\d+)", app_url)

    if not match:
        raise ValueError("Не удалось извлечь app_id из ссылки.")

    return match.group(1)


def has_cyrillic(text: str) -> bool:
    """
    Проверяет, есть ли в тексте кириллические символы.
    Если есть — считаем отзыв русскоязычным.
    """

    if not text:
        return False

    return bool(re.search(r"[А-Яа-яЁё]", text))


def safe_get_label_value(obj: dict) -> str:
    """
    Безопасно извлекает значение из структуры Apple RSS вида:
    {'label': '...'}
    """

    if isinstance(obj, dict):
        return obj.get("label", "")

    return ""


def parse_review_entry(entry: dict) -> dict:
    """
    Извлекает данные из одного отзыва.
    Дата нужна только для внутренней фильтрации.
    В итоговый CSV она не попадет.
    """

    rating = safe_get_label_value(entry.get("im:rating", {}))
    title = safe_get_label_value(entry.get("title", {}))
    review_text = safe_get_label_value(entry.get("content", {}))
    updated_raw = safe_get_label_value(entry.get("updated", {}))

    published_at = None

    if updated_raw:
        try:
            published_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
        except Exception:
            published_at = None

    return {
        "rating": rating,
        "title": title,
        "review_text": review_text,
        "published_at": published_at,
    }


def fetch_reviews_page(app_id: str, page: int, country: str = COUNTRY) -> list:
    """
    Загружает одну страницу отзывов из публичного RSS endpoint Apple.
    """

    url = (
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"page={page}/id={app_id}/sortby=mostrecent/json"
    )

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"App Store вернул статус {response.status_code} для страницы {page}"
            )

        data = response.json()

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Ошибка запроса к App Store на странице {page}: {e}")

    except ValueError:
        raise RuntimeError(f"App Store вернул некорректный JSON на странице {page}")

    feed = data.get("feed", {})
    entries = feed.get("entry", [])

    # На первой странице Apple может вернуть первым entry карточку приложения,
    # а не отзыв. У отзывов есть поле im:rating.
    reviews = [
        entry for entry in entries
        if isinstance(entry, dict) and "im:rating" in entry
    ]

    return reviews


def get_app_name_from_first_page(app_id: str, country: str = COUNTRY) -> str:
    """
    Пытается получить название приложения из первой страницы RSS.
    Если не получилось — возвращает пустую строку.
    """

    url = (
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"page=1/id={app_id}/sortby=mostrecent/json"
    )

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()
        data = response.json()

        entries = data.get("feed", {}).get("entry", [])

        for entry in entries:
            if isinstance(entry, dict) and "im:name" in entry:
                return safe_get_label_value(entry.get("im:name", {}))

    except Exception:
        pass

    return ""


def make_safe_filename_part(text: str) -> str:
    """
    Делает безопасную часть имени файла из названия приложения.
    """

    if not text:
        return ""

    text = text.strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9_\-]", "", text)

    return text[:50]


def collect_reviews(app_id: str, progress_bar=None, log_container=None) -> tuple[pd.DataFrame, dict]:
    """
    Основная функция сбора отзывов.

    Возвращает:
    - DataFrame с колонками rating, title, review_text
    - словарь со статистикой
    """

    cutoff_date = datetime.now(timezone.utc) - relativedelta(months=12)

    all_reviews = []
    page_errors = []

    for page in range(1, MAX_PAGES + 1):
        if progress_bar:
            progress_bar.progress(page / MAX_PAGES)

        if log_container:
            log_container.write(f"Обрабатывается страница: {page}")

        try:
            page_entries = fetch_reviews_page(
                app_id=app_id,
                page=page,
                country=COUNTRY
            )

        except RuntimeError as e:
            page_errors.append({
                "page": page,
                "error": str(e)
            })

            if log_container:
                log_container.warning(
                    f"Страница {page} недоступна. Ошибка: {e}. "
                    "Пробуем продолжить сбор."
                )

            time.sleep(random.uniform(MIN_PAUSE, MAX_PAUSE))
            continue

        if not page_entries:
            if log_container:
                log_container.info("Отзывы на странице не найдены. Сбор остановлен.")
            break

        parsed_page_reviews = [
            parse_review_entry(entry)
            for entry in page_entries
        ]

        all_reviews.extend(parsed_page_reviews)

        if log_container:
            log_container.write(
                f"Получено отзывов на странице: {len(parsed_page_reviews)}"
            )
            log_container.write(
                f"Всего получено отзывов до фильтрации: {len(all_reviews)}"
            )

        dated_reviews = [
            review for review in parsed_page_reviews
            if review["published_at"] is not None
        ]

        # Так как используется sortby=mostrecent,
        # если вся страница старше 12 месяцев,
        # дальше обычно идут еще более старые отзывы.
        if dated_reviews and all(
            review["published_at"] < cutoff_date
            for review in dated_reviews
        ):
            if log_container:
                log_container.info(
                    "Отзывы на странице старше 12 месяцев. Сбор остановлен."
                )
            break

        time.sleep(random.uniform(MIN_PAUSE, MAX_PAUSE))

    filtered_reviews = []

    for review in all_reviews:
        published_at = review.get("published_at")

        if published_at is None:
            continue

        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        is_recent = published_at >= cutoff_date
        is_russian = has_cyrillic(
            f"{review.get('title', '')} {review.get('review_text', '')}"
        )

        if is_recent and is_russian:
            filtered_reviews.append({
                "rating": review.get("rating", ""),
                "title": review.get("title", ""),
                "review_text": review.get("review_text", ""),
            })

    df = pd.DataFrame(
        filtered_reviews,
        columns=["rating", "title", "review_text"]
    )

    stats = {
        "total_collected": len(all_reviews),
        "filtered_count": len(filtered_reviews),
        "page_errors": page_errors,
        "cutoff_date": cutoff_date.date(),
    }

    return df, stats


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Преобразует DataFrame в CSV в кодировке utf-8-sig.
    utf-8-sig нужен, чтобы файл корректно открывался в Excel.
    """

    csv_buffer = StringIO()
    df.to_csv(csv_buffer, index=False, encoding="utf-8-sig")

    return csv_buffer.getvalue().encode("utf-8-sig")


# -----------------------------
# Интерфейс Streamlit
# -----------------------------

st.set_page_config(
    page_title="Сбор отзывов App Store",
    page_icon="📱",
    layout="centered"
)

st.title("Сбор отзывов из App Store")
st.write(
    "Приложение собирает русскоязычные отзывы из российского App Store "
    "за последние 12 месяцев и сохраняет результат в CSV."
)

st.info(
    "Apple RSS не всегда отдает все отзывы. "
    "Скрипт собирает максимально доступные данные через публичный endpoint "
    "без API-ключей и авторизации."
)

app_url = st.text_input(
    "Ссылка на приложение в App Store",
    placeholder="https://apps.apple.com/ru/app/example-app/id1234567890"
)

start_button = st.button("Собрать отзывы", type="primary")

if start_button:
    if not app_url.strip():
        st.error("Вставьте ссылку на приложение App Store.")
        st.stop()

    try:
        app_id = extract_app_id(app_url)
    except ValueError as e:
        st.error(f"Ошибка: {e}")
        st.stop()

    st.success(f"Найден app_id: {app_id}")
    st.write(f"Страна App Store: `{COUNTRY}`")

    progress_bar = st.progress(0)
    log_container = st.container()

    with st.spinner("Собираем отзывы..."):
        df, stats = collect_reviews(
            app_id=app_id,
            progress_bar=progress_bar,
            log_container=log_container
        )

    progress_bar.progress(1.0)

    st.subheader("Итоговая статистика")

    col1, col2 = st.columns(2)

    with col1:
        st.metric(
            "Получено отзывов всего",
            stats["total_collected"]
        )

    with col2:
        st.metric(
            "После фильтрации",
            stats["filtered_count"]
        )

    st.write(f"Отзывы учитывались с даты: `{stats['cutoff_date']}`")

    if stats["page_errors"]:
        st.warning("Часть страниц не удалось обработать.")

        with st.expander("Показать ошибки по страницам"):
            for error in stats["page_errors"]:
                st.write(
                    f"Страница {error['page']}: {error['error']}"
                )

    if stats["total_collected"] == 0:
        st.error(
            "Отзывы не найдены. Возможные причины: у приложения нет отзывов "
            "в российском App Store, Apple RSS не отдал данные или app_id некорректный."
        )
        st.stop()

    if df.empty:
        st.error(
            "После фильтрации не осталось отзывов. Возможные причины: "
            "нет русскоязычных отзывов за последние 12 месяцев "
            "или Apple RSS отдал неполные данные."
        )
        st.stop()

    st.subheader("Предпросмотр данных")
    st.dataframe(df.head(20), use_container_width=True)

    app_name = get_app_name_from_first_page(app_id=app_id, country=COUNTRY)
    safe_app_name = make_safe_filename_part(app_name)

    if safe_app_name:
        output_filename = f"{safe_app_name}_appstore_reviews.csv"
    else:
        output_filename = "appstore_reviews.csv"

    csv_bytes = dataframe_to_csv_bytes(df)

    st.download_button(
        label="Скачать CSV",
        data=csv_bytes,
        file_name=output_filename,
        mime="text/csv"
    )

    st.success(f"Файл готов: {output_filename}")