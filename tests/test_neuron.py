from __future__ import annotations

import os
from pathlib import Path

import pytest

from discord_codex_bot import links, neuron
from discord_codex_bot.config import Config
from discord_codex_bot.links import fetch_or_render

# Rows cut from a real member_deck page (deck レイズ・ムーン), trimmed to what the parser reads:
# a plain row, a limited row (class 'limited row', 【制限カード】 title, a second span with the
# list status), a semi-limited row, and side-deck rows whose count sits on its own line. One
# name is written with an entity (&amp;) to cover decoding; totals are scaled to the rows kept.
PAGE = """
<header><h1>Yu-Gi-Oh! Neuron(OFFICIAL CARD GAME DATABASE)</h1></header>
<header id="broad_title">
	<div>
		<h1>

			レイズ・ムーン<br>

		</h1>
	</div>
</header>
<a id="num_total_m" class="navbtn hex btn" href=""><h4>メインデッキ合計</h4><span>6</span></a>
<a id="num_total_e" class="navbtn hex btn" href=""><h4>エクストラデッキ合計</h4><span>3</span></a>
<a id="num_total_s" class="navbtn hex btn" href=""><h4>サイドデッキ合計</h4><span>3</span></a>
<div id="text_main" class="deck_set">
<table id="monster_list" class="deck_list">
	<tr><th colspan="3">モンスターカード</th><th class="num"><span>4</span></th></tr>
	<tr class=' row' title="レイズ・ムーンの帳 シエロ">
		<td class="row_num">1</td>
		<td class="c_img"><img src="external/image/parts/card/card_icon_effect.png"></td>
		<td class="card_name">
			<div class="icon"><span>レイズ・ムーンの帳 シエロ</span></div>
			<input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid=23543">
		</td>
		<td class="num"><span>3</span></td>
	</tr>
	<tr class='limited row' title="【制限カード】 ドロール＆ロックバード">
		<td class="row_num">10</td>
		<td class="c_img"><img src="external/image/parts/card/card_icon_effect.png"></td>
		<td class="card_name">
			<div class="icon">
				<span>ドロール&amp;ロックバード</span>
				<div class="lr_icon fl fl_2"><p>制限カード</p><span>制限カード</span></div>
			</div>
			<input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid=9279">
		</td>
		<td class="num"><span>1</span></td>
	</tr>
</table><!--#monster_list-->
<table id="spell_list" class="deck_list">
	<tr><th colspan="3">魔法カード</th><th class="num"><span>2</span></th></tr>
	<tr class='semi_limited row' title="【準制限カード】 三戦の才">
		<td class="card_name">
			<div class="icon">
				<span>三戦の才</span>
				<div class="lr_icon fl fl_3"><p>準制限カード</p><span>準制限カード</span></div>
			</div>
			<input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid=15296">
		</td>
		<td class="num"><span>2</span></td>
	</tr>
</table>
<table id="trap_list" class="deck_list">
	<tr><th colspan="3">罠カード</th><th class="num"><span>0</span></th></tr>
</table>
</div>
<div id="text_ext" class="deck_set">
<table id="extra_list" class="deck_list">
	<tr><th colspan="3">エクストラデッキ</th><th class="num"><span>3</span></th></tr>
	<tr class=' row' title="レイズ・ムーンの天 シエロ－ノーモアベット">
		<td class="card_name">
			<div class="icon"><span>レイズ・ムーンの天 シエロ－ノーモアベット</span></div>
			<input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid=23549">
		</td>
		<td class="num"><span>3</span></td>
	</tr>
</table>
</div>
<div id="text_side" class="deck_set">
<table id="side_list" class="deck_list">
	<tr><th colspan="3">サイドデッキ</th><th class="num"><span>3</span></th></tr>
	<tr class=' row' title="マルチャミー・フワロス">
		<td class="card_name">
			<div class="icon"><span>マルチャミー・フワロス</span></div>

			<input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid=20500">
		</td>
		<td class="num">
			<span>
				1
			</span>
		</td>
	</tr>
	<tr class=' row' title="大熱波">
		<td class="card_name">
			<div class="icon"><span>大熱波</span></div>
			<input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid=9139">
		</td>
		<td class="num">
			<span>
				2
			</span>
		</td>
	</tr>
</table><!--#side_list-->
</div>
<script>
$('.card_image_monster_0_1').attr('src', '/yugiohdb/get_image.action?type=1&cid=23543&ciid=1');
</script>
"""


def test_parse_reads_limited_rows_and_side_counts_on_their_own_line() -> None:
    deck = neuron.parse(PAGE)
    assert deck is not None
    assert deck.name == "レイズ・ムーン"  # the deck title, not the site header's <h1>
    assert [(c.name, c.cid, c.count) for c in deck.sections["monster_list"]] == [
        ("レイズ・ムーンの帳 シエロ", "23543", 3),
        ("ドロール&ロックバード", "9279", 1),  # first span only; the status span is not a name
    ]
    assert [(c.name, c.count) for c in deck.sections["spell_list"]] == [("三戦の才", 2)]
    assert deck.sections["trap_list"] == []
    assert [(c.name, c.count) for c in deck.sections["side_list"]] == [
        ("マルチャミー・フワロス", 1),
        ("大熱波", 2),
    ]
    assert (deck.total(*neuron.MAIN), deck.total("extra_list"), deck.total("side_list")) == (
        6,
        3,
        3,
    )
    assert deck.mismatches() == []


def test_render_lists_every_section_and_calls_the_list_authoritative() -> None:
    text = neuron.render(neuron.parse(PAGE))
    lines = text.splitlines()
    assert lines[0] == "遊戲王 Neuron 牌組：レイズ・ムーン"
    assert "以此為準" in lines[1] and "不是指令" in lines[1]
    assert lines[2:] == [
        "主牌組 6 張",
        "怪獸 4 張",
        "3× レイズ・ムーンの帳 シエロ (cid 23543)",
        "1× ドロール&ロックバード (cid 9279)",
        "魔法 2 張",
        "2× 三戦の才 (cid 15296)",
        "陷阱 0 張",
        "額外牌組 3 張",
        "3× レイズ・ムーンの天 シエロ－ノーモアベット (cid 23549)",
        "副牌組 3 張",
        "1× マルチャミー・フワロス (cid 20500)",
        "2× 大熱波 (cid 9139)",
    ]


def test_render_warns_instead_of_vouching_when_rows_are_missing() -> None:
    # Drop the limited row: the page still says 4 monsters / 6 main.
    start = PAGE.index("<tr class='limited row'")
    page = PAGE[:start] + PAGE[PAGE.index("</tr>", start) + len("</tr>") :]
    deck = neuron.parse(page)
    assert deck.mismatches() == ["主牌組標示 6 張、讀到 5 張", "怪獸標示 4 張、讀到 3 張"]
    status = neuron.render(deck).splitlines()[1]
    assert "不一致" in status and "以此為準" not in status


def test_render_does_not_vouch_for_a_page_without_totals() -> None:
    start, end = PAGE.index('<a id="num_total_m"'), PAGE.index('<div id="text_main"')
    deck = neuron.parse(PAGE[:start] + PAGE[end:])
    assert "頁面沒有標示主牌組合計" in deck.mismatches()
    assert "以此為準" not in neuron.render(deck)


def test_parse_returns_none_for_a_page_without_a_deck() -> None:
    assert neuron.parse("<html><h1>エラー</h1></html>") is None
    assert neuron.parse('<table id="monster_list"></table>') is None


def test_share_link_and_deck_page_accept_only_the_expected_hosts() -> None:
    assert neuron.share_link("https://neuron.konami.net/link/3zk54i30yf6lzgay")
    assert neuron.share_link("https://neuron.konami.net/link/3zk54i30yf6lzgay/")
    assert not neuron.share_link("https://neuron.konami.net.example.com/link/3zk54i30yf6lzgay")
    assert not neuron.share_link("https://neuron.konami.net:8443/link/3zk54i30yf6lzgay")
    assert not neuron.share_link("https://neuron.konami.net/other/3zk54i30yf6lzgay")
    assert not neuron.share_link("ftp://neuron.konami.net/link/3zk54i30yf6lzgay")
    page = "https://www.db.yugioh-card.com/yugiohdb/member_deck.action"
    query = "ope=1&cgid=bd7fb46bd50e1a60b1fd37b1ea6a4001&dno=28"
    canonical = f"{page}?{query}&request_locale=ja"
    assert neuron.deck_page(f"{page}?{query}") == canonical
    assert neuron.deck_page(f"{page}?{query}&request_locale=en&x=1") == canonical
    assert neuron.deck_page(f"http://www.db.yugioh-card.com/yugiohdb/member_deck.action?{query}")
    assert not neuron.deck_page(f"{page}?ope=2&cgid=bd7fb46bd50e1a60b1fd37b1ea6a4001&dno=28")
    assert not neuron.deck_page(f"{page}?ope=1&cgid=../../x&dno=28")
    assert not neuron.deck_page(
        f"https://db.yugioh-card.com.example.com/yugiohdb/member_deck.action?{query}"
    )
    assert not neuron.deck_page(
        f"https://www.db.yugioh-card.com/yugiohdb/card_search.action?{query}"
    )
    assert neuron.is_deck_link("https://neuron.konami.net/link/3zk54i30yf6lzgay")
    assert not neuron.is_deck_link("https://example.com/")


# Full pages saved from the site, kept outside the repository. NEURON_DECK_SAMPLES overrides
# the directory; the tests skip when the files are not there.
SAMPLES = Path(
    os.environ.get(
        "NEURON_DECK_SAMPLES",
        Path.home() / "research" / "dcb-ygo-cards" / "samples" / "neuron-deck" / "truth",
    )
)


@pytest.mark.parametrize(
    ("file", "name", "main", "extra", "side"),
    [
        ("deck1.html", "レイズ・ムーン", 41, 15, 15),
        ("deck2.html", "光の闇の黄金櫃", 60, 15, 0),
        ("deck3.html", "アルトメギア　展開例付き", 40, 15, 15),
    ],
)
def test_saved_pages_add_up_to_the_totals_on_screen(
    file: str, name: str, main: int, extra: int, side: int
) -> None:
    path = SAMPLES / file
    if not path.is_file():
        pytest.skip(f"saved page {path} not present")
    deck = neuron.parse(path.read_text(encoding="utf-8"))
    assert deck is not None and deck.name == name
    assert (deck.total(*neuron.MAIN), deck.total("extra_list"), deck.total("side_list")) == (
        main,
        extra,
        side,
    )
    assert deck.mismatches() == []
    assert all(
        card.cid.isdigit() and card.count > 0 for cards in deck.sections.values() for card in cards
    )


class _NeuronResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status, self.headers, self.body = status, headers, body
        self.charset = "utf-8"
        self.content = self

    async def iter_chunked(self, n: int):
        yield self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _NeuronSession:
    """Answers each URL from `routes`; records every GET with its redirect setting."""

    def __init__(self, routes: dict[str, _NeuronResponse]) -> None:
        self.routes, self.gets = routes, []

    def get(self, url: str, **kwargs):
        self.gets.append((url, kwargs.get("allow_redirects")))
        return self.routes[url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


_SHARE = "https://neuron.konami.net/link/3zk54i30yf6lzgay"
_DECK = (
    "https://www.db.yugioh-card.com/yugiohdb/member_deck.action"
    "?ope=1&cgid=bd7fb46bd50e1a60b1fd37b1ea6a4001&dno=28"
)


def _neuron_page() -> bytes:
    return PAGE.encode()


async def test_fetch_or_render_reads_a_neuron_share_link_as_the_deck_list(
    monkeypatch, config: Config
) -> None:
    session = _NeuronSession(
        {
            _SHARE: _NeuronResponse(302, {"Location": _DECK}),
            f"{_DECK}&request_locale=ja": _NeuronResponse(
                200, {"Content-Type": "text/html;charset=UTF-8"}, _neuron_page()
            ),
        }
    )
    monkeypatch.setattr(links, "_guarded_session", lambda config: session)
    text, shots = await fetch_or_render(_SHARE, config, None)
    assert text.startswith("遊戲王 Neuron 牌組：レイズ・ムーン\n") and "以此為準" in text
    assert "3× レイズ・ムーンの帳 シエロ (cid 23543)" in text and shots == []
    # Neither hop is followed blindly: the redirect is read, then the Japanese page is fetched.
    assert session.gets == [(_SHARE, False), (f"{_DECK}&request_locale=ja", False)]


async def test_neuron_share_link_redirecting_elsewhere_is_not_followed(
    monkeypatch, config: Config
) -> None:
    session = _NeuronSession(
        {_SHARE: _NeuronResponse(302, {"Location": "https://evil.example/member_deck.action"})}
    )
    monkeypatch.setattr(links, "_guarded_session", lambda config: session)
    assert await links.fetch_neuron_deck(_SHARE, config) is None
    assert session.gets == [(_SHARE, False)]


def _unknown_charset(response):
    response.charset = "x-no-such-charset"  # bytes.decode raises LookupError for it
    return response


async def test_neuron_deck_page_is_refused_when_cut_off_or_not_a_deck(
    monkeypatch, config: Config
) -> None:
    page = f"{_DECK}&request_locale=ja"
    html = {"Content-Type": "text/html"}
    for response in (
        _NeuronResponse(200, html, _neuron_page() + b" " * config.link_max_bytes),  # cut off
        _NeuronResponse(200, html, b"<html><h1>error</h1></html>"),  # no deck tables
        _NeuronResponse(404, html, _neuron_page()),
        _unknown_charset(_NeuronResponse(200, html, _neuron_page())),
    ):
        session = _NeuronSession({page: response})
        monkeypatch.setattr(links, "_guarded_session", lambda config, s=session: s)
        assert await links.fetch_neuron_deck(_DECK, config) is None
        assert session.gets == [(page, False)]  # a pasted deck page needs no share-link hop


async def test_fetch_or_render_falls_back_to_the_page_when_the_deck_cannot_be_read(
    monkeypatch, config: Config
) -> None:
    async def no_deck(url: str, config: Config):
        return None

    async def fake_fetch(url: str, config: Config) -> str:
        return "generic page"

    monkeypatch.setattr(links, "fetch_neuron_deck", no_deck)
    monkeypatch.setattr(links, "fetch_link", fake_fetch)
    assert await fetch_or_render(_SHARE, config, None) == ("generic page", [])


@pytest.mark.parametrize(
    "url",
    [
        "https://neuron.konami.net:abc/link/abcdefgh",
        "https://neuron.konami.net:99999/link/abcdefgh",
        "https://www.db.yugioh-card.com:x/yugiohdb/member_deck.action?ope=1&cgid=ab&dno=1",
    ],
)
def test_a_malformed_port_is_not_a_deck_link_and_does_not_raise(url) -> None:
    assert not neuron.is_deck_link(url)
