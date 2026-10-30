"""Chosen login IDs mapped to private personalization profiles."""

from contextlib import closing
import hashlib, hmac, re, secrets, sqlite3, unicodedata


def normalize_username(value):
    name = unicodedata.normalize("NFKC", value).strip()
    if not re.fullmatch(r"[A-Za-z0-9가-힣_.-]{2,32}", name):
        raise ValueError(
            "아이디는 한글·영문·숫자·밑줄·점·하이픈으로 2~32자 입력해 주세요."
        )
    return name, name.casefold()


def password_digest(password, salt):
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )


class WebAccounts:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=30)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS web_accounts(
                username_key TEXT PRIMARY KEY,username TEXT NOT NULL,
                profile_id TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL)""")
            if "session_key" not in {
                r[1] for r in db.execute("PRAGMA table_info(web_accounts)")
            }:
                db.execute(
                    "ALTER TABLE web_accounts ADD COLUMN session_key TEXT NOT NULL DEFAULT ''"
                )
            for (profile,) in db.execute(
                "SELECT profile_id FROM web_accounts WHERE session_key='' "
            ).fetchall():
                db.execute(
                    "UPDATE web_accounts SET session_key=? WHERE profile_id=?",
                    (secrets.token_urlsafe(32), profile),
                )

    def register(self, username, password, profile_id):
        name, key = normalize_username(username)
        if not 8 <= len(password) <= 128:
            raise ValueError("비밀번호는 8~128자로 입력해 주세요.")
        salt = secrets.token_bytes(16)
        digest = password_digest(password, salt)
        try:
            with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                db.execute(
                    "INSERT INTO web_accounts(username_key,username,profile_id,salt,password_hash,session_key) VALUES(?,?,?,?,?,?)",
                    (
                        key,
                        name,
                        profile_id,
                        salt.hex(),
                        digest.hex(),
                        secrets.token_urlsafe(32),
                    ),
                )
        except sqlite3.IntegrityError:
            raise ValueError(
                "이미 사용 중인 아이디이거나 아이디가 설정된 기록입니다."
            ) from None
        return name

    def login(self, username, password):
        _, key = normalize_username(username)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            row = db.execute(
                "SELECT profile_id,salt,password_hash FROM web_accounts WHERE username_key=?",
                (key,),
            ).fetchone()
        salt = bytes.fromhex(row[1]) if row else bytes(16)
        digest = password_digest(password, salt)
        expected = bytes.fromhex(row[2]) if row else bytes(32)
        if not hmac.compare_digest(digest, expected) or row is None:
            raise ValueError("아이디 또는 비밀번호가 올바르지 않습니다.")
        return row[0]

    def username(self, profile_id):
        if not profile_id:
            return None
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            row = db.execute(
                "SELECT username FROM web_accounts WHERE profile_id=?", (profile_id,)
            ).fetchone()
        return row[0] if row else None

    def delete(self, profile_id):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("DELETE FROM web_accounts WHERE profile_id=?", (profile_id,))

    def session_key(self, profile_id):
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            row = db.execute(
                "SELECT session_key FROM web_accounts WHERE profile_id=?", (profile_id,)
            ).fetchone()
        return row[0] if row else None

    def valid_session(self, profile_id, key):
        expected = self.session_key(profile_id)
        # A profile identifier, including a legacy unnamed profile, is not proof
        # of login. A registered account and its secret cookie are both required.
        return bool(
            expected
            and isinstance(key, str)
            and key.isascii()
            and hmac.compare_digest(expected, key)
        )

    def change_password(self, profile_id, current, new):
        if not 12 <= len(new) <= 128:
            raise ValueError("새 비밀번호는 12~128자로 입력해 주세요.")
        if current == new:
            raise ValueError("현재 비밀번호와 다른 새 비밀번호를 입력해 주세요.")
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            row = db.execute(
                "SELECT salt,password_hash FROM web_accounts WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        if not row or not hmac.compare_digest(
            password_digest(current, bytes.fromhex(row[0])), bytes.fromhex(row[1])
        ):
            raise ValueError("현재 비밀번호가 올바르지 않습니다.")
        salt = secrets.token_bytes(16)
        digest = password_digest(new, salt)
        key = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            changed = db.execute(
                "UPDATE web_accounts SET salt=?,password_hash=?,session_key=? WHERE profile_id=? AND password_hash=?",
                (salt.hex(), digest.hex(), key, profile_id, row[1]),
            ).rowcount
            if not changed:
                raise ValueError("로그인 상태가 변경됐습니다. 다시 로그인해 주세요.")
        return key
