-- Схема накочується автоматично під час першого старту бази на порожньому томі
-- (docker-entrypoint-initdb.d). Повторний запуск скрипта безпечний.

CREATE TABLE IF NOT EXISTS studies (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code       text         NOT NULL CHECK (char_length(patient_code) BETWEEN 1 AND 64),
    modality           text         NOT NULL CHECK (char_length(modality) BETWEEN 1 AND 16),
    slice_count        integer      NOT NULL CHECK (slice_count > 0),
    slice_thickness_mm numeric(5,3) NOT NULL CHECK (slice_thickness_mm BETWEEN 0.5 AND 10),
    device             text         NOT NULL CHECK (char_length(device) BETWEEN 1 AND 128),
    study_date         date         NOT NULL,
    created_at         timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now()
);

-- Фільтр за модальністю + стабільне сортування за id для пагінації
CREATE INDEX IF NOT EXISTS studies_modality_id_idx ON studies (modality, id);
