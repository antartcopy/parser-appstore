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
from io import StringIO, BytesIO


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
# Функции для работы со ссылкой и текстом
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


# -----------------------------
# Функции для сбора отзывов
# -----------------------------

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


# -----------------------------
# Функции для структурирования таблиц
# -----------------------------

def prepare_structured_tables(df: pd.DataFrame) -> dict:
    """
    Готовит несколько структурированных таблиц для анализа отзывов.
    """

    prepared_df = df.copy()

    # Приводим рейтинг к числу
    prepared_df["rating"] = pd.to_numeric(
        prepared_df["rating"],
        errors="coerce"
    )

    # Добавляем длину текста — удобно искать содержательные отзывы
    prepared_df["review_length"] = prepared_df["review_text"].fillna("").str.len()

    # Добавляем группу отзыва по оценке
    def get_review_group(rating):
        if rating in [1, 2]:
            return "Негативные"
        elif rating == 3:
            return "Нейтральные"
        elif rating in [4, 5]:
            return "Позитивные"
        return "Без оценки"

    prepared_df["review_group"] = prepared_df["rating"].apply(get_review_group)

    # Все отзывы: удобно сначала смотреть проблемные и содержательные
    all_reviews = prepared_df[
        ["rating", "review_group", "title", "review_text", "review_length"]
    ].sort_values(
        by=["rating", "review_length"],
        ascending=[True, False]
    )

    # Сводка по оценкам
    rating_summary = (
        prepared_df
        .groupby("rating", dropna=False)
        .size()
        .reset_index(name="reviews_count")
        .sort_values("rating")
    )

    # Сводка по группам
    group_summary = (
        prepared_df
        .groupby("review_group", dropna=False)
        .size()
        .reset_index(name="reviews_count")
        .sort_values("reviews_count", ascending=False)
    )

    # Негативные отзывы
    negative_reviews = prepared_df[
        prepared_df["rating"].isin([1, 2])
    ][
        ["rating", "title", "review_text", "review_length"]
    ].sort_values(
        by=["rating", "review_length"],
        ascending=[True, False]
    )

    # Нейтральные отзывы
    neutral_reviews = prepared_df[
        prepared_df["rating"] == 3
    ][
        ["rating", "title", "review_text", "review_length"]
    ].sort_values(
        by="review_length",
        ascending=False
    )

    # Позитивные отзывы
    positive_reviews = prepared_df[
        prepared_df["rating"].isin([4, 5])
    ][
        ["rating", "title", "review_text", "review_length"]
    ].sort_values(
        by=["rating", "review_length"],
        ascending=[False, False]
    )

    # Самые длинные отзывы
    long_reviews = prepared_df[
        ["rating", "review_group", "title", "review_text", "review_length"]
    ].sort_values(
        by="review_length",
        ascending=False
    ).head(100)

    # Общая аналитика
    total_reviews = len(prepared_df)
    average_rating = prepared_df["rating"].mean()

    negative_count = len(negative_reviews)
    neutral_count = len(neutral_reviews)
    positive_count = len(positive_reviews)

    analytics = pd.DataFrame([
        {
            "metric": "Всего отзывов",
            "value": total_reviews
        },
        {
            "metric": "Средняя оценка",
            "value": round(average_rating, 2) if pd.notna(average_rating) else ""
        },
        {
            "metric": "Негативных отзывов, 1–2 звезды",
            "value": negative_count
        },
        {
            "metric": "Нейтральных отзывов, 3 звезды",
            "value": neutral_count
        },
        {
            "metric": "Позитивных отзывов, 4–5 звезд",
            "value": positive_count
        },
        {
            "metric": "Доля негативных отзывов",
            "value": f"{round(negative_count / total_reviews * 100, 1)}%" if total_reviews else "0%"
        },
        {
            "metric": "Доля нейтральных отзывов",
            "value": f"{round(neutral_count / total_reviews * 100, 1)}%" if total_reviews else "0%"
        },
        {
            "metric": "Доля позитивных отзывов",
            "value": f"{round(positive_count / total_reviews * 100, 1)}%" if total_reviews else "0%"
        },
    ])

    return {
        "Все отзывы": all_reviews,
        "Аналитика": analytics,
        "Сводка по оценкам": rating_summary,
        "Сводка по группам": group_summary,
        "Негативные": negative_reviews,
        "Нейтральные": neutral_reviews,
        "Позитивные": positive_reviews,
        "Длинные отзывы": long_reviews,
    }


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Создает обычный CSV-файл для совместимости.
    В CSV остаются только три исходные колонки:
    rating, title, review_text.
    """

    csv_buffer = StringIO()

    df[["rating", "title", "review_text"]].to_csv(
        csv_buffer,
        index=False,
        encoding="utf-8-sig"
    )

    return csv_buffer.getvalue().encode("utf-8-sig")


def structured_tables_to_excel_bytes(tables: dict) -> bytes:
    """
    Создает Excel-файл с несколькими листами.
    """

    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, table in tables.items():
            # Excel ограничивает название листа 31 символом
            safe_sheet_name = sheet_name[:31]

            table.to_excel(
                writer,
                sheet_name=safe_sheet_name,
                index=False
            )

            worksheet = writer.sheets[safe_sheet_name]

            # Закрепляем верхнюю строку
            worksheet.freeze_panes = "A2"

            # Включаем автофильтр
            if worksheet.max_row > 1 and worksheet.max_column > 1:
                worksheet.auto_filter.ref = worksheet.dimensions

            # Настраиваем ширину колонок
            for column_cells in worksheet.columns:
                max_length = 0
                column_letter = column_cells[0].column_letter

                for cell in column_cells:
                    try:
                        cell_value = str(cell.value) if cell.value is not None else ""
                        max_length = max(max_length, len(cell_value))
                    except Exception:
                        pass

                adjusted_width = min(max(max_length + 2, 12), 70)
                worksheet.column_dimensions[column_letter].width = adjusted_width

    output.seek(0)

    return output.getvalue()


# -----------------------------
# Интерфейс Streamlit
# -----------------------------

st.set_page_config(
    page_title="Сбор отзывов App Store",
    page_icon="📱",
    layout="wide"
)

st.title("Сбор отзывов из App Store")

st.write(
    "Приложение собирает русскоязычные отзывы из российского App Store "
    "за последние 12 месяцев, показывает структурированные таблицы "
    "и сохраняет результат в CSV и Excel."
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

    col1, col2, col3 = st.columns(3)

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

    with col3:
        st.metric(
            "Ошибок страниц",
            len(stats["page_errors"])
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

    # -----------------------------
    # Структурированные таблицы
    # -----------------------------

    structured_tables = prepare_structured_tables(df)

    st.subheader("Структурированные таблицы")

    tab_names = list(structured_tables.keys())
    tabs = st.tabs(tab_names)

    for tab, table_name in zip(tabs, tab_names):
        with tab:
            table = structured_tables[table_name]

            st.write(f"Строк в таблице: **{len(table)}**")

            st.dataframe(
                table,
                use_container_width=True,
                hide_index=True
            )

    # -----------------------------
    # Подготовка файлов
    # -----------------------------

    app_name = get_app_name_from_first_page(app_id=app_id, country=COUNTRY)
    safe_app_name = make_safe_filename_part(app_name)

    if safe_app_name:
        csv_filename = f"{safe_app_name}_appstore_reviews.csv"
        excel_filename = f"{safe_app_name}_appstore_reviews_structured.xlsx"
    else:
        csv_filename = "appstore_reviews.csv"
        excel_filename = "appstore_reviews_structured.xlsx"

    csv_bytes = dataframe_to_csv_bytes(df)
    excel_bytes = structured_tables_to_excel_bytes(structured_tables)

    st.subheader("Скачать файлы")

    download_col1, download_col2 = st.columns(2)

    with download_col1:
        st.download_button(
            label="Скачать обычный CSV",
            data=csv_bytes,
            file_name=csv_filename,
            mime="text/csv"
        )

    with download_col2:
        st.download_button(
            label="Скачать структурированный Excel",
            data=excel_bytes,
            file_name=excel_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    st.success("Файлы готовы к скачиванию.")