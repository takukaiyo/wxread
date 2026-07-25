from pathlib import Path

import qr_login


def test_build_curl_from_cookies():
    curl = qr_login.build_curl_from_cookies(
        [
            {"name": "wr_vid", "value": "123"},
            {"name": "wr_skey", "value": "abc"},
            {"name": "", "value": "ignored"},
        ],
        "UA",
    )

    assert "https://weread.qq.com/web/book/read" in curl
    assert "-H 'user-agent: UA'" in curl
    assert "-b 'wr_vid=123; wr_skey=abc'" in curl
