# КУМАР — передача проекта v0.14.0-orcarouter

## Что изменилось

Проект пересобран под OrcaRouter API. Ollama больше не запускается и не выбирается в интерфейсе. Ответы идут через OpenAI-compatible endpoint `https://api.orcarouter.ai/v1`, по умолчанию модель `deepseek/deepseek-v4.1-flash-free`.

Системный промпт Кумара привязан к OrcaRouter prompt `kumar-live`, версия `latest`. Локальная панель добавляет историю, память, backstage-фрагменты и текущую эмоцию. API-ключ хранится только в `.env` и не попадает в backup.

## Запуск

```bash
cd /home/kymar_x_ai/mira-memory-v1
cp .env.example .env
nano .env
chmod +x launcher/run-mira.sh
./launcher/run-mira.sh prepare
./launcher/run-mira.sh start
```

Минимальный `.env`:

```dotenv
ORCAROUTER_API_KEY=...
ORCAROUTER_BASE_URL=https://api.orcarouter.ai/v1
ORCAROUTER_SESSION_ID=kumar-panel
KUMAR_SEMANTIC_SEARCH=0
```

Панель: `http://127.0.0.1:8000`.

## Backstage

- **Перезапустить LLM** — сбрасывает runtime-сессию OrcaRouter;
- **Остановить/возобновить TTS** — переключает текущую заглушку, чтобы будущий TTS можно было заменить без изменения панели;
- **Внедрить фрагмент** — добавляет временное или постоянное воспоминание Кумара прямо во время эфира.

## Ограничения текущей сборки

- Бесплатные лимиты и доступность моделей определяет OrcaRouter; они не являются лимитом отдельных ключей.
- TTS пока пишет последнюю фразу в `data/last_tts.txt`, звука нет.
- Ollama и `qwen3-embedding` отключены. Поиск памяти пока словесный.
- Twitch и чат-платформа пока не подключены.

## Обновление

Не распаковывайте новую версию поверх работающей папки. Сначала остановите панель, скопируйте `data/`, затем распакуйте архив в новую папку и перенесите только свой `.env` и нужную базу.
