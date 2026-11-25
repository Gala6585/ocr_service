import os
from redis import Redis
from rq import Queue, SimpleWorker


if __name__ == "__main__":
    # SimpleWorker выполняет задания в одном процессе, сохраняя модель в памяти.
    conn = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    SimpleWorker([Queue("ocr", connection=conn)], connection=conn).work()
