import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
import requests
import streamlit as st
from rulers import with_rulers

API = os.getenv("API_URL", "http://flask:5000")
DISPLAY_ZONE = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "Europe/Moscow"))
st.set_page_config(page_title="Обработка удостоверений", layout="wide")


def call(method, path, *, binary=False, **kwargs):
    auth = kwargs.pop("auth", st.session_state.get("auth"))
    try:
        response = requests.request(method, API + path, auth=auth, timeout=(5, 180), **kwargs)
        if not response.ok:
            try:
                message = response.json()["error"]
            except (ValueError, KeyError):
                message = f"Сервис вернул HTTP {response.status_code}"
            st.error(message)
            st.stop()
        return response.content if binary else response.json()
    except requests.RequestException:
        st.error("Не удалось связаться с сервисом обработки. Попробуйте обновить страницу.")
        st.stop()


def display_time(text):
    return datetime.fromisoformat(text).astimezone(DISPLAY_ZONE).strftime("%d.%m.%Y %H:%M %Z") if text else "—"


if "auth" not in st.session_state:
    st.title("Обработка удостоверений")
    with st.form("login"):
        username = st.text_input("Логин")
        password = st.text_input("Пароль", type="password")
        if st.form_submit_button("Войти", type="primary"):
            auth = (username, password)
            user = call("GET", "/api/me", auth=auth)
            st.session_state.update(auth=auth, user=user)
            st.rerun()
    st.stop()

user = st.session_state["user"]
if "new_job" in st.session_state:
    st.session_state["active_job"] = st.session_state.pop("new_job")
    st.session_state["section"] = "Мои задания"
st.sidebar.title("Удостоверения")
section = st.sidebar.radio("Рабочая область", ["Мои задания", "Загрузить PDF"], key="section")
st.sidebar.caption(user["username"])
if st.sidebar.button("Выйти"):
    st.session_state.clear()
    st.rerun()


if section == "Загрузить PDF":
    st.title("Новое задание")
    with st.form("upload"):
        file = st.file_uploader("PDF с протоколом и удостоверениями", type="pdf")
        a, b = st.columns(2)
        first = a.number_input("Первая страница протокола", min_value=1, value=1)
        last = b.number_input("Последняя страница протокола", min_value=1, value=1)
        a, b = st.columns(2)
        until = a.date_input(
            "Дата окончания удостоверения (необязательно)",
            value=None,
            min_value=date(1900, 1, 1),
            max_value=date(2199, 12, 31),
            format="DD.MM.YYYY",
        )
        qual = b.text_input("Квалификация", max_chars=200)
        column = st.number_input(
            "Столбец ФИО, если нужно указать вручную",
            min_value=0,
            max_value=100,
            value=None,
            help="Первый столбец имеет индекс 0.",
        )
        if st.form_submit_button("Начать обработку", type="primary"):
            if not file or last < first:
                st.error("Выберите PDF и проверьте диапазон страниц протокола.")
            else:
                data = dict(
                    protocol_start=first,
                    protocol_end=last,
                    qualification=qual,
                )
                if until:
                    data["expiry_date"] = until.isoformat()
                if column is not None:
                    data["fio_column"] = column
                with st.spinner("Передача PDF"):
                    job = call(
                        "POST",
                        "/api/jobs",
                        data=data,
                        files={"pdf": (file.name, file, "application/pdf")},
                    )
                st.session_state["new_job"] = job["job_id"]
                st.rerun()
    st.stop()

st.title("Мои задания")
jobs = call("GET", "/api/jobs")["jobs"]
if not jobs:
    st.info("Пока нет заданий. Загрузите первый PDF через меню слева.")
    st.stop()
items = {j["id"]: j for j in jobs}
selected = st.session_state.get("active_job")
if selected not in items:
    st.session_state["active_job"] = jobs[0]["id"]
key = st.selectbox(
    "Документ",
    list(items),
    key="active_job",
    format_func=lambda i: f"{items[i]['filename']} · {display_time(items[i]['created_at'])} · {i[:8]}",
)
base = f"/api/jobs/{key}"
job = call("GET", base)
if job["status"] in ("QUEUED", "RUNNING"):

    @st.fragment(run_every="4s")
    def progress():
        current = call("GET", base)
        if current["status"] not in ("QUEUED", "RUNNING"):
            st.rerun()
        st.progress(current["progress"] / 100, text=current["stage"])

    progress()
    st.stop()
if job["status"] == "FAILED":
    st.error(job["error"] or "Ошибка обработки")
    st.stop()
if job["status"] in ("DELETING", "EXPIRED"):
    st.info("Срок хранения файлов истёк. Для повторной обработки загрузите PDF заново.")
    st.stop()

result = call("GET", base + "/results")
rev = result["revision"]
cards = result["cards"]
col1, col2, col3 = st.columns(3)
col1.metric("Карточки", len(cards))
col2.metric("Требуют проверки", sum(c["needs_review"] for c in cards))
col3.metric("Версия", rev)
st.caption(f"Файлы доступны до {display_time(job['expires_at'])}")
if job["warnings"]:
    with st.expander("Предупреждения обработки"):
        for warning in job["warnings"]:
            st.write(warning)
download_key = f"archive_{key}_{rev}"
if st.button("Получить архив", type="primary"):
    archive = call("POST", base + "/downloads", binary=True)
    st.download_button(
        "Сохранить ZIP",
        archive,
        file_name=f"result-{key}.zip",
        mime="application/zip",
        key=download_key,
        on_click="ignore",
    )
    updated = call("GET", base)
    st.caption(f"После запроса файлы доступны до {display_time(updated['expires_at'])}")

review_tab, protocol_tab, dataset_tab = st.tabs(["Карточки", "Строки протокола", "Датасет"])
rows = result["protocol_rows"]
with protocol_tab:
    st.dataframe(
        [
            {
                "Страница": r["page_number"],
                "Строка": r["row_no"],
                "Тип": r["kind"],
                "ФИО": r["fio"],
                "Прочитанные ячейки": " | ".join(r["raw"].get("values", [])),
            }
            for r in rows
        ],
        hide_index=True,
    )
with review_tab:
    if cards:
        by_id = {c["card_no"]: c for c in cards}
        card_key = f"card_{key}"
        pending = st.session_state.pop(f"next_{key}", None)
        if pending in by_id:
            st.session_state[card_key] = pending
        number = st.selectbox(
            "Карточка",
            list(by_id),
            format_func=lambda n: f"{n} · {by_id[n]['fio'] or 'ФИО не найдено'}",
            key=card_key,
        )
        next_number = next((n for n in by_id if n > number), number)
        card = by_id[number]
        p = card["page_number"]
        preview = call("GET", base + f"/pages/{p}/preview", binary=True)
        st.image(
            with_rulers(preview, card["width"], card["height"]),
            caption=f"Страница {p} · X вправо, Y вниз · {card['width']} × {card['height']} пикселей",
        )
        st.write(f"Имя файла: {card['output_file']}")
        if user["role"] == "reviewer":
            with st.form(f"edit_{key}_{rev}_{number}"):
                fio = st.text_input("ФИО", value=card["fio"] or "")
                allowed = {r["row_no"]: r for r in rows if r["kind"] in ("data", "manual", "review")}
                options = [None, *allowed]
                row = st.selectbox(
                    "Строка протокола",
                    options,
                    index=options.index(card["row_no"]) if card["row_no"] in options else 0,
                    format_func=lambda n: (
                        "Новая строка" if n is None else f"{n} · {allowed[n]['fio'] or 'для проверки'}"
                    ),
                )
                cols = st.columns(4)
                box = [
                    col.number_input(label, min_value=0, max_value=limit, value=value)
                    for col, label, limit, value in zip(
                        cols, ["Лево", "Верх", "Право", "Низ"],
                        [card["width"], card["height"]] * 2, card["bbox"],
                    )
                ]
                if st.form_submit_button("Сохранить исправления"):
                    body = {"revision": rev, "card_no": number, "bbox": box}
                    if fio != (card["fio"] or "") or row != card["row_no"]:
                        body.update(fio=fio, row_no=row)
                    call("PATCH", base + "/corrections", json=body)
                    st.session_state[f"next_{key}"] = next_number
                    st.rerun()
            if card["annotation_status"] == "verified":
                st.success("Рамка проверена")
            elif st.button("Подтвердить рамку", key=f"verify_{key}_{number}"):
                call("POST", base + "/annotations/verify", json={"revision": rev, "card_no": number})
                st.session_state[f"next_{key}"] = next_number
                st.rerun()
    else:
        st.info("Карточки не найдены. Проверьте диапазон страниц протокола и качество скана.")
with dataset_tab:
    verified = sum(c["annotation_status"] == "verified" for c in cards)
    st.write(f"Подтверждено рамок: {verified}")
    if user["role"] == "reviewer" and verified:
        with st.form(f"dataset_{key}_{rev}"):
            basis = st.text_input("Основание для постоянного хранения")
            consent = st.checkbox("Разрешено сохранить весь исходный PDF и проверенные рамки")
            if st.form_submit_button("Сохранить в датасет"):
                answer = call(
                    "POST",
                    base + "/dataset-promotion",
                    json={"revision": rev, "storage_basis": basis, "consent": consent},
                )
                st.success(f"Датасет сохранён · {answer['dataset_id']}")
