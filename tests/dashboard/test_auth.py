import json
import os
import stat

import pytest

from zozobox.dashboard import COOKIE_NAME, LoginLimiter, PasswordFile, SessionCodec

from dash_helpers import PASSWORD

PROTECTED_GET = ["/", "/approvals", "/recordings", "/archive", "/archive?kind=prints", "/mood",
                 "/stats", "/reprint", "/settings", "/archive/item/abc",
                 "/media/prints/x.png"]
PROTECTED_POST = ["/approvals/abc/approve", "/approvals/abc/reject", "/recordings/abc/withhold",
                  "/reprint/1", "/settings", "/settings/password", "/archive/purge"]


# -- first run / setup ------------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_first_run_redirects_to_setup(dash, path):
    r = dash.client().get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_login_page_redirects_to_setup_before_password(dash):
    r = dash.client().get("/login")
    assert r.status_code == 303 and r.headers["location"].startswith("/setup")


def test_setup_creates_0600_scrypt_file_and_logs_in(dash):
    c = dash.client()
    r = c.setup()
    assert r.status_code == 303 and r.headers["location"] == "/"
    path = dash.app.state.passwords.path
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    text = path.read_text()
    assert PASSWORD not in text
    d = json.loads(text)
    assert d["kdf"] == "scrypt" and len(bytes.fromhex(d["salt"])) == 16
    assert c.get("/").status_code == 200


def test_setup_salt_is_random(tmp_path):
    a = PasswordFile(tmp_path / "a.json", scrypt_n=2**10)
    b = PasswordFile(tmp_path / "b.json", scrypt_n=2**10)
    a.create("samepassword")
    b.create("samepassword")
    da, db = json.loads(a.path.read_text()), json.loads(b.path.read_text())
    assert da["salt"] != db["salt"] and da["hash"] != db["hash"]
    assert a.verify("samepassword") and not a.verify("samepasswordX")


def test_setup_validation(dash):
    c = dash.client()
    r = c.post("/setup", {"password": "short", "confirm": "short"}, page="/setup")
    assert r.status_code == 400 and "at least" in r.text
    r = c.post("/setup", {"password": "longenough1", "confirm": "different1"}, page="/setup")
    assert r.status_code == 400 and "match" in r.text
    assert not dash.app.state.passwords.exists()


def test_setup_cannot_be_rerun(dash):
    dash.client().setup()
    other = dash.client()
    assert other.get("/setup").headers["location"] == "/login"
    token = other.csrf("/login")
    r = other.post("/setup", {"password": "attacker-pass", "confirm": "attacker-pass"},
                   token=token)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert dash.app.state.passwords.verify(PASSWORD)
    assert not dash.app.state.passwords.verify("attacker-pass")
    assert other.get("/").status_code == 303


def test_setup_requires_csrf(dash):
    c = dash.client()
    r = c.c.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 403
    assert not dash.app.state.passwords.exists()


# -- login / logout ------------------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_unauthenticated_get_redirects_to_login(dash, path):
    dash.client().setup()
    r = dash.client().get(path)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", PROTECTED_POST)
def test_unauthenticated_post_redirects_and_does_nothing(dash, path):
    dash.client().setup()
    items = dash.populate()
    c = dash.client()
    token = c.csrf("/login")
    r = c.post(path, {"rec_cap_s": "10", "item_id": items["story"].id, "target": "content",
                      "confirm": "PURGE"}, token=token)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert dash.store.content.find(items["story"].id) is not None
    assert dash.printer.count == 0


def test_login_redirects_to_next(dash):
    dash.client().setup()
    c = dash.client()
    r = c.get("/stats?days=7")
    assert r.headers["location"] == "/login?next=%2Fstats%3Fdays%3D7"
    r = c.login(next="/stats?days=7")
    assert r.status_code == 303 and r.headers["location"] == "/stats?days=7"


@pytest.mark.parametrize("bad", ["//evil.example/", "https://evil.example/", "/\\evil.example",
                                 "javascript:alert(1)", ""])
def test_login_next_is_never_offsite(dash, bad):
    dash.client().setup()
    r = dash.client().login(next=bad)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_wrong_password(dash):
    dash.client().setup()
    c = dash.client()
    r = c.login("nope nope nope")
    assert r.status_code == 401 and "That password isn" in r.text
    assert c.get("/").status_code == 303


def test_session_cookie_flags(dash):
    c = dash.client()
    r = c.setup()
    sc = r.headers["set-cookie"]
    assert sc.startswith(COOKIE_NAME + "=")
    low = sc.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low
    assert "secure" not in low.replace("samesite", "")


def test_secure_cookie_option(tmp_path):
    from dash_helpers import Dash

    d = Dash(tmp_path)
    d.config.cookie_secure = True
    r = d.client().setup()
    assert "secure" in r.headers["set-cookie"].lower().replace("samesite", "")
    d.store.close()


def test_logout(client):
    r = client.post("/logout", page="/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/").status_code == 303


def test_logout_requires_csrf(client):
    r = client.post("/logout", token="wrong")
    assert r.status_code == 403
    assert client.get("/").status_code == 200


def test_tampered_cookie_is_anonymous(dash, client):
    token = client.cookies.get(COOKIE_NAME)
    body, sig = token.split(".")
    payload = json.loads(__import__("base64").urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["auth"] is True
    forged_body = body[:-2] + ("AA" if body[-2:] != "AA" else "BB")
    c2 = dash.client()
    c2.c.cookies.set(COOKIE_NAME, f"{forged_body}.{sig}")
    assert c2.get("/").status_code == 303
    # A cookie signed with another key is rejected too.
    c3 = dash.client()
    c3.c.cookies.set(COOKIE_NAME, SessionCodec.encode(payload, b"x" * 32))
    assert c3.get("/").status_code == 303


def test_session_expires(dash, client):
    dash.clock.advance(days=31)
    assert client.get("/").status_code == 303


def test_password_change_logs_out_other_sessions(dash, client):
    other = dash.client()
    assert other.login().status_code == 303
    assert other.get("/").status_code == 200
    r = client.post("/settings/password", {"current": PASSWORD, "password": "new password 1",
                                           "confirm": "new password 1"})
    assert r.status_code == 303 and "password_changed" in r.headers["location"]
    assert client.get("/").status_code == 200  # this session got a fresh cookie
    assert other.get("/").status_code == 303
    assert dash.client().login("new password 1").status_code == 303
    assert dash.client().login(PASSWORD).status_code == 401


def test_password_change_checks_current(dash, client):
    r = client.post("/settings/password", {"current": "wrong", "password": "new password 1",
                                           "confirm": "new password 1"})
    assert r.status_code == 400
    assert dash.app.state.passwords.verify(PASSWORD)


def test_corrupt_auth_file_disables_login_but_not_setup_takeover(dash):
    dash.client().setup()
    dash.app.state.passwords.path.write_text("{not json")
    c = dash.client()
    assert c.get("/setup").headers["location"] == "/login"
    assert c.login().status_code == 401
    assert c.get("/").status_code == 303


def test_loose_permissions_are_tightened(dash):
    dash.client().setup()
    p = dash.app.state.passwords.path
    os.chmod(p, 0o644)
    os.utime(p, None)
    assert dash.client().login().status_code == 303
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


# -- rate limiting ------------------------------------------------------------------------


def test_login_rate_limit(dash):
    dash.client().setup()
    c = dash.client()
    for _ in range(5):
        assert c.login("wrong password").status_code == 401
    r = c.login()  # even the right password is refused while locked out
    assert r.status_code == 429 and "Too many" in r.text
    assert int(r.headers["retry-after"]) > 0
    dash.clock.advance(seconds=301)
    assert c.login().status_code == 303


def test_success_resets_failures(dash):
    dash.client().setup()
    c = dash.client()
    for _ in range(4):
        c.login("wrong password")
    assert c.login().status_code == 303
    c2 = dash.client()
    for _ in range(4):
        assert c2.login("wrong password").status_code == 401


def test_limiter_global_cap():
    t = [0.0]
    lim = LoginLimiter(lambda: t[0], max_failures=5, max_global_failures=6, window_s=60)
    for i in range(6):
        lim.failure(f"10.0.0.{i}")
    assert lim.retry_after("10.9.9.9") > 0
    t[0] = 61
    assert lim.retry_after("10.9.9.9") == 0


# -- CSRF ---------------------------------------------------------------------------------


def test_csrf_missing_or_wrong_rejected(dash, client):
    items = dash.populate()
    r = client.c.post(f"/approvals/{items['story'].id}/approve", data={})
    assert r.status_code == 403
    r = client.post(f"/approvals/{items['story'].id}/approve", token="forged-token-value-123")
    assert r.status_code == 403
    assert dash.store.content.get(items["story"].id).approval.value == "pending"


def test_csrf_token_from_other_session_rejected(dash, client):
    items = dash.populate()
    other = dash.client()
    token = other.csrf("/login")
    r = client.post(f"/approvals/{items['story'].id}/approve", token=token)
    assert r.status_code == 403


def test_cross_origin_post_rejected(dash, client):
    items = dash.populate()
    token = client.csrf()
    r = client.c.post(f"/approvals/{items['story'].id}/approve", data={"csrf_token": token},
                      headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.c.post(f"/approvals/{items['story'].id}/approve", data={"csrf_token": token},
                      headers={"Origin": "http://testserver"})
    assert r.status_code == 303


def test_login_requires_csrf(dash):
    dash.client().setup()
    c = dash.client()
    r = c.c.post("/login", data={"password": PASSWORD})
    assert r.status_code == 403
    assert c.get("/").status_code == 303


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_static_and_health_are_public(dash):
    c = dash.client()
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/healthz").json() == {"ok": True}


# -- concurrent attempts (check-then-act race) -----------------------------------------


def test_limiter_reserve_counts_before_the_check():
    t = [0.0]
    lim = LoginLimiter(lambda: t[0], max_failures=5, max_global_failures=20, window_s=60)
    got = [lim.reserve("a") for _ in range(8)]  # 8 in flight, none finished yet
    assert [w == 0 for w, _ in got] == [True] * 5 + [False] * 3
    assert all(tok is None for w, tok in got if w > 0)


def test_limiter_success_returns_reserved_slot():
    t = [0.0]
    lim = LoginLimiter(lambda: t[0], max_failures=5, max_global_failures=3, window_s=60)
    for i in range(3):
        w, tok = lim.reserve(f"c{i}")
        assert w == 0
        lim.success(f"c{i}", tok)
    assert lim.retry_after("other") == 0  # successes don't fill the global cap
    assert lim.reserve("other")[0] == 0


def test_concurrent_wrong_logins_are_capped(dash):
    """Many threads posting wrong passwords at once: at most max_failures
    password checks run, and never more than max_concurrent_kdf at a time."""
    import threading
    import time

    dash.client().setup()
    c = dash.client()
    token = c.csrf("/login")
    pw = dash.app.state.passwords
    real = pw.verify
    lock = threading.Lock()
    state = {"calls": 0, "now": 0, "peak": 0}

    def slow_verify(password):
        with lock:
            state["calls"] += 1
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.2)
        try:
            return real(password)
        finally:
            with lock:
                state["now"] -= 1

    pw.verify = slow_verify
    codes = []

    def attempt():
        r = c.c.post("/login", data={"password": "wrong password", "next": "/",
                                     "csrf_token": token})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert len(codes) == 20
    assert state["calls"] <= 5
    assert codes.count(401) <= 5 and codes.count(429) >= 15
    assert state["peak"] <= dash.config.max_concurrent_kdf


def test_password_change_is_throttled_by_reservation(dash, client):
    token = client.csrf()
    for _ in range(5):
        r = client.post("/settings/password", {"current": "wrong", "password": "new password 1",
                                               "confirm": "new password 1"}, token=token)
        assert r.status_code == 400
    r = client.post("/settings/password", {"current": PASSWORD, "password": "new password 1",
                                           "confirm": "new password 1"}, token=token)
    assert r.status_code == 429
    assert dash.app.state.passwords.verify(PASSWORD)
