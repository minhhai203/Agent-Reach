import pytest

from video_tracker import urls


@pytest.mark.parametrize(
    "url, platform",
    [
        ("https://www.youtube.com/watch?v=abcdefghijk", "youtube"),
        ("https://youtu.be/abcdefghijk", "youtube"),
        ("https://m.youtube.com/@abc", "youtube"),
        ("https://www.tiktok.com/@abc", "tiktok"),
        ("https://vm.tiktok.com/ZM123xyz/", "tiktok"),
        ("https://tiktok.com:443/@abc", "tiktok"),
    ],
)
def test_platform_of_accepts(url, platform):
    assert urls.platform_of(url) == platform


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://youtube.com/watch?v=abcdefghijk",
        "https://youtube.com.evil.test/watch?v=abcdefghijk",
        "https://user:pw@youtube.com/@abc",
        "https://youtube.com:8443/@abc",
        "https://example.com/@abc",
        "ftp://tiktok.com/@abc",
        "https://youtube.com:abc/@x",
        "https://www.youtube.com/@" + "a" * 3000,
    ],
)
def test_platform_of_rejects(url):
    with pytest.raises(ValueError):
        urls.platform_of(url)


@pytest.mark.parametrize(
    "platform, url, expected",
    [
        ("tiktok", "https://www.tiktok.com/@lan.anh_1", "https://www.tiktok.com/@lan.anh_1"),
        ("tiktok", "https://m.tiktok.com/@lan/", "https://www.tiktok.com/@lan"),
        ("tiktok", "  https://tiktok.com/@lan  ", "https://www.tiktok.com/@lan"),
        ("youtube", "https://youtube.com/@lan", "https://www.youtube.com/@lan"),
        ("youtube", "https://m.youtube.com/@lan/videos", "https://www.youtube.com/@lan"),
        ("youtube", "https://www.youtube.com/@lan/shorts/", "https://www.youtube.com/@lan"),
        (
            "youtube",
            "https://www.youtube.com/channel/UC1234567890abcdefghijkl",
            "https://www.youtube.com/channel/UC1234567890abcdefghijkl",
        ),
        ("youtube", "https://www.youtube.com/c/LanAnh", "https://www.youtube.com/c/LanAnh"),
    ],
)
def test_canonical_channel(platform, url, expected):
    assert urls.canonical_channel(platform, url) == expected


@pytest.mark.parametrize(
    "platform, url",
    [
        ("tiktok", "https://www.youtube.com/@lan"),  # wrong platform
        ("youtube", "https://www.tiktok.com/@lan"),
        ("tiktok", "https://vm.tiktok.com/ZM123xyz/"),  # redirector
        ("youtube", "https://youtu.be/abcdefghijk"),
        ("tiktok", "https://www.tiktok.com/@lan?lang=vi"),  # query
        ("youtube", "https://www.youtube.com/@lan#x"),  # fragment
        ("tiktok", "https://www.tiktok.com/@lan/video/1234567890123"),  # video link
        ("youtube", "https://www.youtube.com/watch?v=abcdefghijk"),
        ("youtube", "https://www.youtube.com/shorts/abcdefghijk"),
        ("youtube", "https://www.youtube.com/channel/notachannel"),
        ("youtube", "https://www.youtube.com/user/lan"),
        ("tiktok", "https://www.tiktok.com/lan"),
        ("instagram", "https://www.tiktok.com/@lan"),
    ],
)
def test_canonical_channel_rejects(platform, url):
    with pytest.raises(ValueError):
        urls.canonical_channel(platform, url)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://www.youtube.com/watch?v=abcdefghijk&t=10",
            ("youtube", "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
        ),
        (
            "https://youtu.be/abcdefghijk?t=2",
            ("youtube", "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
        ),
        (
            "https://youtube.com/shorts/abcdefghijk",
            ("youtube", "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
        ),
        (
            "https://m.youtube.com/live/abcdefghijk?feature=share",
            ("youtube", "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
        ),
        (
            "https://www.tiktok.com/@lan/video/7300000000000000001?lang=vi",
            ("tiktok", "7300000000000000001", "https://www.tiktok.com/@lan/video/7300000000000000001"),
        ),
        (
            "https://vm.tiktok.com/ZM123xyz/?share=1",
            ("tiktok", "short:ZM123xyz", "https://vm.tiktok.com/ZM123xyz/"),
        ),
        (
            "https://vt.tiktok.com/ZSabc123",
            ("tiktok", "short:ZSabc123", "https://vt.tiktok.com/ZSabc123/"),
        ),
    ],
)
def test_canonical_video(url, expected):
    assert urls.canonical_video(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/@lan",
        "https://www.youtube.com/shorts/",
        "https://youtu.be/",
        "https://www.tiktok.com/@lan",
        "https://www.tiktok.com/@lan/video/abc",
        "https://www.tiktok.com/ZM123xyz/",
        "https://vm.tiktok.com/@lan/video/1234567890123",
        "http://youtu.be/abcdefghijk",
    ],
)
def test_canonical_video_rejects(url):
    with pytest.raises(ValueError):
        urls.canonical_video(url)
