from app.core.auth.strategies.local import LocalAuthStrategy


def test_hash_and_verify():
    hashed = LocalAuthStrategy.hash_password("secret123")
    assert hashed != "secret123"
    # verify is done inside authenticate(); passlib ctx is tested here indirectly
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["argon2"], deprecated="auto")
    assert ctx.verify("secret123", hashed)


def test_login_page(client):
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert b"ScrapeMind" in r.data
