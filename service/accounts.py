"""Создать пользователя локального сервиса или сменить его пароль."""

import argparse
from getpass import getpass
from werkzeug.security import generate_password_hash
from .common import database


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--role", choices=["operator", "reviewer"], default="operator")
    args = parser.parse_args()
    password = getpass("Пароль: ")
    if not password or password != getpass("Повторите пароль: "):
        parser.error("Введите одинаковый непустой пароль дважды")
    with database() as db:
        db.execute(
            "INSERT INTO users(username,password_hash,role) VALUES (%s,%s,%s) "
            "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,role=excluded.role",
            (args.username, generate_password_hash(password), args.role),
        )
