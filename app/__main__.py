"""Entry point: python -m app"""


def main() -> None:
    print("llm-kb: Personal LLM-curated knowledge base")
    print("Components:")
    print("  python -m app.worker_resources  -- resource worker (parse + quality gate)")
    print("  python -m app.worker_ingest     -- ingest worker (wiki mutations)")
    print("  python -m app.scheduler         -- scheduler (cron jobs + sweeper)")
    print("  python -m app.bot               -- Telegram bot")


if __name__ == "__main__":
    main()
