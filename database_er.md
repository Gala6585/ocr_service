# Связи таблиц PostgreSQL

ER-диаграмма базы `ocr` по [SQL-миграциям](migrations/). На диаграмме показаны ключевые поля и фактические ограничения внешних ключей.

Для загрузки в редактор Mermaid используйте [database_er.mmd](database_er.mmd). В этом файле только код диаграммы.

```mermaid
erDiagram
    users ||--o{ jobs : owns
    jobs ||--o| documents : contains
    documents ||--o{ pages : contains
    jobs ||--o{ protocol_rows : extracts
    pages ||--o{ cards : contains
    protocol_rows |o--o| cards : manual_assignment
    cards ||--o| matches : has
    protocol_rows |o--o| matches : matched_to
    cards ||--o| annotations : has
    users |o--o{ annotations : verifies
    jobs ||--o{ corrections : records
    users ||--o{ corrections : makes
    jobs ||--o{ artifacts : produces
    jobs ||--o{ download_events : tracks
    artifacts ||--o{ download_events : downloaded
    users ||--o{ download_events : requests
    jobs ||--o{ datasets : provides_source
    users ||--o{ datasets : approves

    users {
        bigint id PK
        text username UK
        text role
    }
    jobs {
        uuid id PK
        bigint owner_id FK
        text status
        date expiry_date "необязательная дата удостоверения"
        timestamptz expires_at "срок хранения файлов"
        integer revision
        integer published_revision
    }
    documents {
        uuid job_id PK,FK
        text filename
        text source_relative_path
        integer page_count
    }
    pages {
        uuid job_id PK,FK
        integer page_number PK
        integer dpi
        integer rotation
    }
    protocol_rows {
        uuid job_id PK,FK
        integer row_no PK
        integer page_number
        text fio
        jsonb raw
    }
    cards {
        uuid job_id PK,FK
        integer card_no PK
        integer page_number FK
        integer manual_row_no FK
        jsonb bbox
    }
    matches {
        uuid job_id PK,FK
        integer card_no PK,FK
        integer row_no FK
        text fio
        real score
        boolean needs_review
        text output_file
    }
    annotations {
        uuid job_id PK,FK
        integer card_no PK,FK
        bigint annotator FK
        jsonb geometry
        text status
    }
    corrections {
        bigint id PK
        uuid job_id FK
        integer card_no
        bigint author_id FK
        integer revision
        jsonb before_value
        jsonb after_value
    }
    artifacts {
        bigint id PK
        uuid job_id FK
        integer revision
        text kind
        text relative_path
    }
    download_events {
        bigint id PK
        uuid job_id FK
        bigint user_id FK
        bigint artifact_id FK
        timestamptz requested_at
    }
    datasets {
        uuid id PK
        uuid job_id FK
        bigint approved_by FK
        integer revision
        text status
        text source_relative_path
        text annotations_relative_path
    }
```

- Карточка привязана к странице по паре `(job_id, page_number)`. Сопоставление и разметка ссылаются на карточку по паре `(job_id, card_no)`.
- Для ручного назначения используется `cards.manual_row_no`, для итогового сопоставления — `matches.row_no`. Оба поля могут быть пустыми. В каждой из этих таблиц ненулевая пара с `job_id` уникальна.
- `protocol_rows.page_number` и `corrections.card_no` хранят номер страницы и карточки соответственно. Для этих полей в базе нет отдельных внешних ключей.
- Артефакт уникален по `(job_id, revision, kind)`. PDF, изображения и ZIP лежат на диске, а в базе находятся пути к ним.
- Служебная таблица `schema_migrations` хранит историю применения миграций и не участвует в связях.
