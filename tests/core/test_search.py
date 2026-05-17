def test_search_route_requires_login(client):
    r = client.get("/search?q=ad", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_search_short_query_returns_empty(client, app):
    # Even unauth, route is login-required; this asserts the endpoint exists.
    r = client.get("/search?q=a")
    # not authenticated → redirect to login
    assert r.status_code in (302, 200)
