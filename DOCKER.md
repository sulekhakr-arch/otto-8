# Run Otto with Docker

## What your friend needs

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows or Mac) or Docker Engine on Linux
- An **OpenAI API key**

## One-time setup

1. Copy the environment template:

   ```bash
   cp env.example .env
   ```

2. Edit `.env` and set `OPENAI_API_KEY`.

## Start everything

From the project folder (where `docker-compose.yml` is):

```bash
docker compose up --build
```

First start can take several minutes (image build + Parlant warming up).

## URLs

| Service   | URL                     |
|----------|-------------------------|
| Streamlit UI | http://localhost:8501 |
| REST API   | http://localhost:8801/docs |
| Parlant    | http://localhost:8800 |

MongoDB is included for persistence (`localhost:27017`).

## Stop

`Ctrl+C`, or in another terminal:

```bash
docker compose down
```

To remove data volume as well:

```bash
docker compose down -v
```

## Notes

- If `api` fails to start, wait until `parlant` is healthy (Parlant can take 1–2 minutes on first run).
- Optional: set `COMPOSIO_API_KEY` in `.env` for Composio tools in `server.py`.
- Streamlit **Edit bot (forms)** tab edits profile, guidelines, and journeys; **Heyo (Riva)** template is `data/heyo_bot_spec.json` (load via the Create-bot button).
- Image upload in chat uses `OPENAI_API_KEY` (vision) to turn screenshots into text for the bot.
