import sqlite3
from pathlib import Path

DB_PATH = Path("data/ashur.sqlite3")


class Store:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bots (
                bot_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                encrypted_token TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
        """)
        self.db.commit()

    def add_bot(self, bot_id, username, encrypted_token):
        self.db.execute(
            "INSERT INTO bots(bot_id, username, encrypted_token) VALUES(?,?,?)",
            (bot_id, username, encrypted_token),
        )
        self.db.commit()

    def has_bot(self, bot_id):
        return self.db.execute("SELECT 1 FROM bots WHERE bot_id=?", (bot_id,)).fetchone() is not None

    def list_bots(self):
        return self.db.execute("SELECT bot_id, username FROM bots ORDER BY bot_id").fetchall()

    def list_secrets(self):
        return self.db.execute("SELECT bot_id, username, encrypted_token FROM bots").fetchall()

    def delete_bot(self, bot_id):
        self.db.execute("DELETE FROM bots WHERE bot_id=?", (bot_id,))
        self.db.commit()

    def add_warning(self, chat_id, user_id):
        self.db.execute("""
            INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1
        """, (chat_id, user_id))
        self.db.commit()
        return self.db.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()[0]

    def reset_warnings(self, chat_id, user_id):
        self.db.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        self.db.commit()

    def close(self):
        self.db.close()
