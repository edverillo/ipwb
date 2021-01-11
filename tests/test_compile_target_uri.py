from ipwb.replay import compile_target_uri


def test_empty_query_string():
    assert compile_target_uri('https://example.com', b'') == (
        'https://example.com'
    )


def test_unempty_query_string():
    assert compile_target_uri('https://example.com', b'foo=bar&boo=baz') == (
        'https://example.com?foo=bar&boo=baz'
    )
